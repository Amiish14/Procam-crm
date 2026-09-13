"""Batch correction for Data Quality findings — explicit, previewed, audited.

Nothing here runs by itself. An administrator picks records on a check's
detail page, picks one action and a value, and is shown exactly what each
record will change from and to. Only then can they apply it, with a
reason. Three rules make that preview binding rather than decorative:

  * The preview is signed. The token is an HMAC, under the app's secret
    key, of the check, action, value, the person, an expiry ten minutes
    out, and every record's before and after. Applying recomputes the
    change set from the database and refuses unless it signs the same —
    so what is applied is what was previewed. If somebody else edited one
    of those records in between, the befores differ and the batch is
    refused whole; nothing is half-applied.
  * The scope is checked on every record, at preview and again at apply.
    A record outside the viewer's Access Matrix boundary refuses the
    whole batch, and the answer does not say whether it exists.
  * Nothing is deleted. The strongest action is archive, through the bulk
    admin service, which is reversible from Bulk Leads.

Auditing: one data_quality.batch_fix event per batch (action, counts, every
before and after, the reason). Field changes on watched models are also
recorded per record by the session listener with the same reason; the one
field it does not watch (a lead's follow-up date) is recorded here.
"""
import hashlib
import hmac
import json
import logging
import time
import uuid
from datetime import date, timedelta

from app import db
from app.data_quality import definitions as defs
from app.data_quality import service as dq

log = logging.getLogger(__name__)

#: (action, entity) → the column it writes
FIELDS = {
    ('assign_owner', 'lead'): 'assigned_to',
    ('assign_owner', 'opportunity'): 'owner_emp_code',
    ('assign_owner', 'company'): 'pic_emp_code',
    ('set_followup', 'lead'): 'followup_date',
    ('set_close_date', 'opportunity'): 'expected_close_date',
    ('set_vertical', 'lead'): 'procam_vertical',
    ('set_vertical', 'company'): 'vertical',
    ('archive', 'lead'): 'is_archived',
    ('link_account', 'lead'): 'company_id',
}


class BatchRefused(Exception):
    """A batch that will not run, and why, in words for the administrator."""

    def __init__(self, message, status=400, **extra):
        super().__init__(message)
        self.message, self.status, self.extra = message, status, extra


def _model(entity):
    from app import Company, Lead, Opportunity
    return {'lead': Lead, 'opportunity': Opportunity,
            'company': Company}[entity]


def _scoped(entity, sc):
    from app.access import scope
    return {'lead': scope.leads, 'opportunity': scope.opportunities,
            'company': scope.companies}[entity](sc=sc)


def _name(entity, row):
    if entity == 'lead':
        return row.company or f'Lead #{row.id}'
    if entity == 'opportunity':
        return row.opp_number or f'Opportunity #{row.id}'
    return row.name or f'Account #{row.id}'


def _ids(raw):
    if not isinstance(raw, list) or not raw:
        raise BatchRefused('Select at least one record.')
    out = []
    for i in raw:
        # bool is an int to Python; "true" is not a record number.
        if isinstance(i, bool) or not str(i).strip().isdigit():
            raise BatchRefused('Record ids must be whole numbers.')
        n = int(str(i).strip())
        if n not in out:
            out.append(n)
    if len(out) > defs.BATCH_MAX:
        raise BatchRefused(
            f'{len(out):,} records selected — a batch is limited to '
            f'{defs.BATCH_MAX}. Correct them in smaller batches, so each '
            f'preview can actually be read.', 413)
    return out


def _value(action, raw):
    """(stored value, label for people) after validating it."""
    kind = defs.BATCH_ACTIONS[action][1]
    if kind is None:
        return None, ''
    text = str(raw if raw is not None else '').strip()
    if kind == 'employee':
        from app import Employee
        emp = Employee.query.filter_by(emp_code=text).first() if text else None
        if emp is None:
            raise BatchRefused('Choose an employee to assign to.')
        if not emp.is_active:
            raise BatchRefused(f'{emp.name} is not an active employee.')
        return emp.emp_code, f'{emp.name} ({emp.emp_code})'
    if kind == 'date':
        try:
            if len(text) != 10:
                raise ValueError
            d = date.fromisoformat(text)
        except ValueError:
            raise BatchRefused('Enter the date as YYYY-MM-DD.')
        today = date.today()
        if d < today:
            raise BatchRefused('The date is in the past — the records would '
                               'still be flagged tomorrow.')
        if d > today + timedelta(days=defs.MAX_FUTURE_DAYS):
            raise BatchRefused('That date is more than three years away; '
                               'check it for a typo.')
        return d.isoformat(), d.isoformat()
    if kind == 'vertical':
        from app.master_data import service as md
        # Labels, as the bulk lead tool writes them, so the two tools
        # cannot disagree about what a vertical is called.
        if text not in {i.label for i in md.items('vertical')}:
            raise BatchRefused('Choose a vertical from Master Data.')
        return text, text
    raise BatchRefused('Unknown value type.')                # pragma: no cover


def _plain(v):
    if isinstance(v, date):
        return v.isoformat()
    return v


def _account_for(lead):
    """The one active account whose name is exactly this lead's company
    name (ignoring case and outer spaces), or why there is not one."""
    from sqlalchemy import func
    from app import Company
    name = (lead.company or '').strip().lower()
    if not name:
        return None, 'the lead has no company name'
    hits = Company.query.filter(Company.is_active.isnot(False),
                                func.lower(func.trim(Company.name)) == name) \
        .order_by(Company.id).limit(2).all()
    if not hits:
        return None, 'no account has exactly this name'
    if len(hits) > 1:
        return None, ('more than one account has this name — decide it in '
                      'Data Mapping')
    return hits[0], ''


def plan(key, ids, action, value, sc):
    """What this batch would change: (check, changes, skipped, value).

    Refuses — changing nothing — when the check has no such action, when
    any record is outside the scope, or when any record no longer has the
    problem the administrator selected it for.
    """
    check = dq.find(key)
    if check is None:
        raise BatchRefused(f'Unknown check "{key}".', 404)
    if action not in check.actions:
        raise BatchRefused('That correction is not offered for this check.')
    entity = check.entity
    field = FIELDS[(action, entity)]
    model = _model(entity)
    ids = _ids(ids)
    value, value_label = _value(action, value)

    reachable = {r[0] for r in _scoped(entity, sc)
                 .filter(model.id.in_(ids)).with_entities(model.id).all()}
    refused = [i for i in ids if i not in reachable]
    if refused:
        # One message for "outside your scope" and "does not exist":
        # telling them apart would confirm which records exist.
        raise BatchRefused(
            f'{len(refused)} of the selected records are not available to '
            f'you. Nothing was changed.', 403, refused=refused)

    still = dq.affected_ids(key, ids, sc)
    resolved = [i for i in ids if i not in still]
    if resolved:
        raise BatchRefused(
            f'{len(resolved)} of the selected records no longer have this '
            f'problem. Reload the page and select again.', 409,
            resolved=resolved)

    changes, skipped = [], []
    for row in model.query.filter(model.id.in_(ids)).order_by(model.id):
        before = _plain(getattr(row, field))
        after, after_label, why_not = value, value_label, ''
        if action == 'archive':
            after, after_label = True, 'archived'
        elif action == 'link_account':
            account, why_not = _account_for(row)
            if account is not None:
                after, after_label = account.id, account.name
        if why_not:
            skipped.append({'id': row.id, 'name': _name(entity, row),
                            'reason': why_not})
            continue
        if before == after:
            skipped.append({'id': row.id, 'name': _name(entity, row),
                            'reason': 'already set to this value'})
            continue
        changes.append({'id': row.id, 'name': _name(entity, row),
                        'field': field, 'before': before, 'after': after,
                        'after_label': after_label})
    return check, changes, skipped, value


def _secret():
    from flask import current_app
    secret = current_app.secret_key
    if not secret:
        raise BatchRefused('Batch correction is unavailable: the server has '
                           'no secret key configured.', 500)
    return secret.encode() if isinstance(secret, str) else secret


def sign(key, action, value, actor, changes, expires):
    body = json.dumps({
        'check': key, 'action': action, 'value': value, 'actor': actor,
        'expires': int(expires),
        'changes': [[c['id'], c['field'], c['before'], c['after']]
                    for c in changes],
    }, sort_keys=True, separators=(',', ':'), default=str)
    return hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()


def preview(key, ids, action, value, *, sc, actor, now=None):
    check, changes, skipped, value = plan(key, ids, action, value, sc)
    expires = int(now if now is not None else time.time()) \
        + defs.PREVIEW_TTL_SECONDS
    token = (f'{expires}.{sign(key, action, value, actor, changes, expires)}'
             if changes else None)
    return {'check': key, 'action': action,
            'action_label': defs.BATCH_ACTIONS[action][0], 'value': value,
            'changes': changes, 'skipped': skipped, 'count': len(changes),
            'expires_at': expires, 'preview_token': token}


def apply(key, ids, action, value, reason, token, *, sc, actor, now=None):
    reason = (reason or '').strip()
    if len(reason) < 5:
        raise BatchRefused('Give a reason for this correction — it is kept '
                           'in the audit trail with every change.')
    try:
        expires_text, digest = str(token or '').split('.', 1)
        expires = int(expires_text)
    except ValueError:
        raise BatchRefused('Preview the correction before applying it.')
    now = int(now if now is not None else time.time())
    if expires < now:
        raise BatchRefused('The preview has expired. Preview again — nothing '
                           'was changed.', 409)
    if expires > now + defs.PREVIEW_TTL_SECONDS:
        raise BatchRefused('That preview is not valid. Preview again.')

    check, changes, skipped, value = plan(key, ids, action, value, sc)
    if not changes:
        raise BatchRefused('Nothing left to change.', 409)
    expected = sign(key, action, value, actor, changes, expires)
    if not hmac.compare_digest(expected, digest):
        raise BatchRefused(
            'This is not what was previewed: the records have changed since, '
            'or the request differs. Preview again — nothing was changed.',
            409)

    batch_ref = uuid.uuid4().hex[:12]
    try:
        _write(check, action, value, reason, actor, changes, skipped,
               batch_ref)
    except BatchRefused:
        db.session.rollback()
        raise
    except Exception:
        db.session.rollback()
        log.exception('data quality batch %s failed', batch_ref)
        raise BatchRefused('The correction could not be applied, and nothing '
                           'was changed. Try again, or report it.', 500)
    return {'applied': len(changes), 'skipped': len(skipped),
            'batch_ref': batch_ref}


def _write(check, action, value, reason, actor, changes, skipped, batch_ref):
    from flask import g
    from app.services import audit

    entity = check.entity
    ids = [c['id'] for c in changes]
    previous_reason = getattr(g, 'audit_reason', None)
    # The session listener attaches this to every per-record change event.
    g.audit_reason = reason
    try:
        audit.record('data_quality.batch_fix', 'data_quality', batch_ref,
                     actor=actor, reason=reason, strict=True,
                     new={'check': check.key, 'action': action,
                          'value': value, 'count': len(changes),
                          'skipped': len(skipped), 'ids': ids,
                          'changes': [{k: c[k] for k in
                                       ('id', 'field', 'before', 'after')}
                                      for c in changes]})
        if entity == 'lead' and action in ('assign_owner', 'archive',
                                           'set_vertical'):
            # The bulk admin service already does these properly —
            # assignment history, Copilot index, deletion-audit rows — so
            # it is reused rather than copied. It commits.
            from app.bulk_admin import service as bulk
            if action == 'assign_owner':
                bulk.assign(ids, value, actor)
            elif action == 'archive':
                bulk.archive(ids, reason, actor)
            else:
                bulk.set_field(ids, 'procam_vertical', value, actor)
            return

        model = _model(entity)
        rows = {r.id: r for r in model.query.filter(model.id.in_(ids))}
        for c in changes:
            row = rows[c['id']]
            if action in ('set_followup', 'set_close_date'):
                setattr(row, c['field'], date.fromisoformat(c['after']))
            else:
                setattr(row, c['field'], c['after'])
            if action == 'set_followup':
                # followup_date is not a watched field; record it here.
                audit.record('lead.update', 'lead', row.id, actor=actor,
                             old={'followup_date': c['before']},
                             new={'followup_date': c['after']},
                             reason=reason)
        db.session.commit()
    finally:
        g.audit_reason = previous_reason
