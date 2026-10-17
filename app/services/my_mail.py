"""
What happened to the mail you forwarded to leads@.

Sanjeev forwarded two enquiries on 23 September and told Seema the
next day that neither was in the CRM. One of them was: it had been
attached to lead #11807, which is stage Lost, so it was nowhere he
would look. The other had been dropped as a reply. Both decisions
were recorded — in a log only administrators can open, which did not
exist for most of the period in question.

Nothing was broken. The CRM simply never told him, and gave him
nowhere to find out.

This is the data behind `/my-mail`: every message the leads mailbox
received that is his business, with what the CRM did about it and
why. "His business" is either of:

    he forwarded it in        — `forwarded_by_emp`
    it landed on his lead     — the lead's PIC or secondary PIC

Scope is the access matrix's. An administrator sees everything and
can filter by forwarder; everybody else sees their own, which is the
whole point — a page that showed one person another's client mail
would be a worse problem than the one it fixes.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

#: How far back the page looks. Long enough to cover "I sent that
#: weeks ago and heard nothing", short enough that the query stays
#: cheap on a table that grows by every message the mailbox receives.
WINDOW_DAYS = 90

#: The badge on the nav counts these, over this window.
BADGE_DAYS = 7

OUTCOME_LABELS = {
    'created': 'Created',
    'attached': 'Attached',
    'reopened': 'Reopened',
    'skipped': 'Skipped',
    'error': 'Error',
}


def _since(days=WINDOW_DAYS):
    return datetime.utcnow() - timedelta(days=days)


def _my_lead_ids(emp_code):
    """Leads this person is PIC or secondary PIC on."""
    from app import Lead, db

    if not emp_code:
        return set()
    try:
        q = db.session.query(Lead.id).filter(
            db.or_(Lead.assigned_to == emp_code,
                   Lead.secondary_owner == emp_code))
        return {r[0] for r in q.all()}
    except Exception:
        db.session.rollback()
        try:
            q = db.session.query(Lead.id).filter(
                Lead.assigned_to == emp_code)
            return {r[0] for r in q.all()}
        except Exception:
            db.session.rollback()
            return set()


def visible(emp_code, *, is_admin=False, days=WINDOW_DAYS,
            outcome='', search='', forwarded_by='', limit=500):
    """The rows this person may see, newest first.

    An administrator sees everything. Everybody else sees mail they
    forwarded, plus mail that landed on a lead of theirs — and
    nothing else, whatever is in the query string.
    """
    from app import db
    from app.models.ingest_log import MailIngestLog

    q = MailIngestLog.query.filter(
        MailIngestLog.received_at >= _since(days))

    if is_admin:
        if forwarded_by:
            q = q.filter(MailIngestLog.forwarded_by_emp
                         == forwarded_by.strip().upper())
    else:
        lead_ids = _my_lead_ids(emp_code)
        mine = [MailIngestLog.forwarded_by_emp == emp_code]
        if lead_ids:
            mine.append(MailIngestLog.lead_id.in_(lead_ids))
        q = q.filter(db.or_(*mine))

    if outcome:
        q = q.filter(MailIngestLog.outcome == outcome)
    if search:
        like = f'%{search.strip().lower()}%'
        q = q.filter(db.or_(
            db.func.lower(MailIngestLog.subject).like(like),
            db.func.lower(MailIngestLog.from_addr).like(like),
            db.func.lower(db.func.coalesce(
                MailIngestLog.forwarded_by, '')).like(like)))

    return (q.order_by(MailIngestLog.received_at.desc())
            .limit(min(int(limit or 500), 2000)).all())


def rows(emp_code, **kw):
    """`visible`, decorated with what the screen shows per row.

    The lead's stage *now* as well as what it was when the mail
    landed: "attached to a Lost lead" explains the complaint, and
    "and it is still Lost" is the part that needs doing something
    about.
    """
    from app import Lead, db

    found = visible(emp_code, **kw)
    lead_ids = {r.lead_id for r in found if r.lead_id}
    leads = {}
    if lead_ids:
        try:
            for l in Lead.query.filter(Lead.id.in_(lead_ids)).all():
                leads[l.id] = l
        except Exception:
            db.session.rollback()

    out = []
    for r in found:
        lead = leads.get(r.lead_id)
        item = r.to_dict()
        item['outcome_label'] = OUTCOME_LABELS.get(r.outcome, r.outcome)
        item['lead_company'] = (lead.company or '') if lead else ''
        item['lead_stage'] = (lead.stage or '') if lead else ''
        item['lead_pic'] = (lead.assigned_to or '') if lead else ''
        item['stage_then'] = r.lead_stage_at_ingest or ''
        item['stage_moved'] = bool(
            item['stage_then'] and item['lead_stage']
            and item['stage_then'] != item['lead_stage'])
        item['route'] = f'/app?lead={r.lead_id}' if r.lead_id else ''
        out.append(item)
    return out


def badge(emp_code, *, is_admin=False):
    """How many of this person's mails were skipped in the last week.

    The number on the nav. Skipped is the only outcome that needs a
    person: created, attached and reopened all did something.
    """
    try:
        found = visible(emp_code, is_admin=is_admin, days=BADGE_DAYS,
                        outcome='skipped', limit=200)
        return len(found)
    except Exception:
        log.exception('could not count skipped mail for %s', emp_code)
        return 0


def counts(emp_code, *, is_admin=False, days=WINDOW_DAYS):
    """{outcome: n} over the window, for the filter chips."""
    found = visible(emp_code, is_admin=is_admin, days=days, limit=2000)
    out = {k: 0 for k in OUTCOME_LABELS}
    for r in found:
        out[r.outcome] = out.get(r.outcome, 0) + 1
    out['all'] = len(found)
    return out


def may_see(emp_code, row, *, is_admin=False):
    """Can this person see this particular row?

    Checked again at the point of acting on a row, not only when
    listing them: a row id in a URL is not proof of anything.
    """
    if row is None:
        return False
    if is_admin:
        return True
    if (row.forwarded_by_emp or '') == emp_code:
        return True
    return bool(row.lead_id and row.lead_id in _my_lead_ids(emp_code))
