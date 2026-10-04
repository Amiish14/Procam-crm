"""
Is the company allowed? One answer, one place.

Everything that can start business with a client asks this module: the
lead form, the email intake, the Excel import, the RFQ, the quote, the
business-card scanner, the API, and the TMS over HTTP. There is exactly
one function that decides, because a rule enforced in nine places is a
rule with nine ways to be wrong — and the one that matters here is the
way where a blocked client slips through because somebody added a new
route.

    verdict = restrictions.check(company_name='Walchandnagar Industries Ltd')
    if verdict.blocked:
        ...refuse
    if verdict.caution and not acknowledged:
        ...make them read it first

Hiding a button is not enforcement. Every check here runs on the
server, after the request has been parsed and before anything is
written.

Matching, in the order it is tried
    1. The account id, GSTIN, PAN or an exact email. No ambiguity, so
       no scoring.
    2. The email's domain, but only for an entry whose scope is
       `group` — a dispute with one company on a shared domain must not
       silently block its sister company.
    3. The name. Lower-cased, punctuation stripped, and the company
       suffixes that people type differently every time — Ltd,
       Limited, Limit, Pvt, Private, Co, Company, Inc, LLP, Corp —
       removed, so "Walchandnagar Industries Limited", "Walchandnagar
       Industries Ltd" and "Walchandnagar Industries Limit" all reduce
       to the same key. Aliases are matched the same way.
    4. What is left goes through a similarity score. At 0.90 and above
       it is the client. Between 0.80 and 0.90 it is "possibly the
       client", which is a caution rather than a block, because being
       wrong in that band must cost somebody a dialog and not a deal.

A blocked verdict from a fuzzy match is never returned: only an exact
or normalised-exact match can stop business. A near miss warns.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

#: At or above this, a fuzzy name match is the same company.
SURE = 0.90
#: Between this and SURE, it might be — warn, never block.
MAYBE = 0.80

#: Words people write six different ways and never mean anything by.
_SUFFIXES = {
    'ltd', 'limited', 'limite', 'limit', 'pvt', 'private', 'co',
    'company', 'inc', 'incorporated', 'llp', 'llc', 'corp',
    'corporation', 'plc', 'gmbh', 'bv', 'sa', 'ag', 'pte', 'fze',
    'fzc', 'wll', 'pjsc', 'jsc', 'and', '&',
}

#: Cache of the live rows, rebuilt when the register changes. The check
#: runs on every lead, every import row and every inbound email, and
#: the register is a handful of rows that change a few times a year.
_CACHE = {'rows': None}


def cache_clear():
    _CACHE['rows'] = None


# ── normalising ──────────────────────────────────────────────────────
def normalise(name):
    """A company name reduced to what is actually distinctive about it."""
    text = (name or '').lower()
    text = re.sub(r'[^a-z0-9 ]+', ' ', text)
    words = [w for w in text.split() if w and w not in _SUFFIXES]
    return ' '.join(words).strip()


def similarity(left, right):
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def domain_of(email):
    addr = (email or '').strip().lower()
    if '<' in addr and '>' in addr:
        addr = addr[addr.rfind('<') + 1:addr.rfind('>')].strip()
    if addr.count('@') != 1:
        return ''
    return addr.rsplit('@', 1)[1].strip().strip('.')



def matches_row(row, company_name=None, email=None, domain=None, gstin=None,
                pan=None, account_id=None):
    """Does this one entry cover this client, whatever its status?

    The live check only looks at entries in force. An administrator
    deciding whether to approve a recommendation needs the opposite:
    what *would* this catch. Both go through here, so the preview and
    the enforcement cannot disagree.
    """
    email = (email or '').strip().lower()
    domain = (domain or '').strip().lower().lstrip('@') or domain_of(email)
    gstin = (gstin or '').strip().upper()
    pan = (pan or '').strip().upper()

    if account_id and row.linked_account_id \
            and int(account_id) == int(row.linked_account_id):
        return 'account', 1.0
    if gstin and (row.gstin or '').strip().upper() == gstin:
        return 'gstin', 1.0
    if pan and (row.pan or '').strip().upper() == pan:
        return 'pan', 1.0
    if email and email in row.emails:
        return 'email', 1.0

    from app.models.restriction import SCOPE_GROUP
    if domain and row.scope == SCOPE_GROUP and domain in row.domains:
        return 'domain', 1.0

    key = normalise(company_name)
    if not key:
        return None, 0.0

    best = 0.0
    for candidate in ([row.name_key or normalise(row.company_name)]
                      + [normalise(a) for a in row.aliases]):
        if not candidate:
            continue
        if candidate == key:
            return 'name', 1.0
        score = similarity(key, candidate)
        if score > best and _same_head(key, candidate):
            best = score
    if best >= SURE:
        return 'name-fuzzy', best
    if best >= MAYBE:
        return 'name-possible', best
    return None, best


def _same_head(left, right):
    """Do two names start with recognisably the same word?

    Without this, "Nagar Industries" scores 0.80 against
    "Walchandnagar Industries" purely because one contains the other,
    and an unrelated company is warned about for ever. Comparing the
    first significant word separates that from a real typo:
    walchandnagr/walchandnagar scores 0.96, nagar/walchandnagar 0.55.
    """
    lhead = left.split(' ', 1)[0] if left else ''
    rhead = right.split(' ', 1)[0] if right else ''
    if not lhead or not rhead:
        return False
    return lhead == rhead or similarity(lhead, rhead) >= 0.75


# ── the verdict ──────────────────────────────────────────────────────
class Verdict:
    """What the check decided, and enough to tell somebody why."""

    __slots__ = ('level', 'restriction_id', 'company_name', 'reason_summary',
                 'matched_on', 'score', 'row')

    def __init__(self, level='none', row=None, matched_on='', score=1.0,
                 reason_summary=''):
        self.level = level
        self.row = row
        self.restriction_id = getattr(row, 'id', None)
        self.company_name = getattr(row, 'company_name', '') or ''
        self.matched_on = matched_on
        self.score = score
        self.reason_summary = reason_summary or _summary(row)

    @property
    def blocked(self):
        return self.level == 'blocked'

    @property
    def caution(self):
        return self.level == 'caution'

    @property
    def clear(self):
        return self.level == 'none'

    def to_dict(self):
        return {
            'level': self.level,
            'restriction_id': self.restriction_id,
            'company_name': self.company_name,
            'reason_summary': self.reason_summary,
            'matched_on': self.matched_on,
            'score': round(self.score, 3),
            'message': self.message(),
        }

    def message(self):
        if self.blocked:
            row = self.row
            when = str(getattr(row, 'approved_at', '') or
                       getattr(row, 'recommended_at', ''))[:10]
            who = getattr(row, 'approved_by', '') or 'management'
            return (f'This client is blocked by management. '
                    f'Reason: {getattr(row, "reason_category", "") or "not stated"}. '
                    f'Blocked on {when} by {who}. You cannot create or '
                    f'progress any business with this client. Contact '
                    f'Admin if you think this is wrong.')
        if self.caution:
            if self.matched_on == 'name-possible':
                return (f'This looks like it may be '
                        f'{self.company_name}, which is on the caution '
                        f'register. Check before you go ahead.')
            return (f'{self.company_name} is on the caution register. '
                    f'{self.reason_summary}')
        return ''


def _summary(row):
    if row is None:
        return ''
    bits = [getattr(row, 'reason_category', '') or '']
    detail = (getattr(row, 'reason_detail', '') or '').strip()
    if detail:
        bits.append(detail[:300])
    legal = getattr(row, 'legal_status', '') or ''
    if legal and legal != 'None':
        bits.append(f'Legal status: {legal}.')
    return ' — '.join(b for b in bits if b)


# ── loading ──────────────────────────────────────────────────────────
def live_rows():
    """Every decision currently in force. Cached."""
    if _CACHE['rows'] is not None:
        return _CACHE['rows']
    try:
        from app.models.restriction import ClientRestriction, LIVE_STATUSES
        rows = (ClientRestriction.query
                .filter(ClientRestriction.status.in_(LIVE_STATUSES))
                .all())
    except Exception:
        # The table arrives in a migration; until then nothing is
        # restricted, which is the behaviour that existed before.
        rows = []
    _CACHE['rows'] = rows
    return rows


def _level_for(row):
    from app.models.restriction import BLOCKED
    return 'blocked' if row.status == BLOCKED else 'caution'


# ── the check ────────────────────────────────────────────────────────
def check(company_name=None, email=None, domain=None, gstin=None, pan=None,
          account_id=None, business_unit=None):
    """The one decision. Never raises; a check that cannot run says
    'none', because a register that is briefly unreadable must not stop
    the company trading."""
    try:
        return _check(company_name, email, domain, gstin, pan, account_id,
                      business_unit)
    except Exception:
        try:
            from flask import current_app
            current_app.logger.exception('restriction check failed')
        except Exception:
            pass
        return Verdict('none')


def _check(company_name, email, domain, gstin, pan, account_id,
           business_unit):
    rows = [r for r in live_rows() if r.covers_unit(business_unit)]
    if not rows:
        return Verdict('none')

    best = None
    for row in rows:
        matched_on, score = matches_row(
            row, company_name=company_name, email=email, domain=domain,
            gstin=gstin, pan=pan, account_id=account_id)
        if not matched_on:
            continue
        if matched_on != 'name-possible':
            # Certain enough to act on: an identifier, an exact name,
            # or a near-identical one.
            return Verdict(_level_for(row), row, matched_on, score=score)
        # Possible only. Remember the best and keep looking for a
        # certain match on another entry.
        if best is None or score > best[1]:
            best = (row, score)

    if best is not None:
        # Deliberately a caution even when the entry is a block: being
        # wrong in this band should cost a dialog, not a deal.
        return Verdict('caution', best[0], 'name-possible', score=best[1])
    return Verdict('none')


def check_lead_payload(payload, business_unit=None):
    """The check as the lead routes need it, from a request body."""
    data = payload or {}
    return check(company_name=data.get('company'),
                 email=data.get('email') or data.get('email2'),
                 gstin=data.get('gstin'), pan=data.get('pan'),
                 account_id=data.get('company_id'),
                 business_unit=business_unit)


# ── recording what happened ──────────────────────────────────────────
def log_event(restriction_id, action, *, user_id=None, note='', payload=None,
              commit=True):
    """Append to the history. Never raises."""
    import json

    from app import db
    from app.models.restriction import ClientRestrictionEvent

    try:
        db.session.add(ClientRestrictionEvent(
            restriction_id=restriction_id, action=action,
            user_id=(user_id or '')[:20] or None, note=(note or '')[:2000],
            payload_json=json.dumps(payload or {})[:8000]))
        if commit:
            db.session.commit()
        return True
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        return False


def record_attempt(verdict, *, what, user_id=None, detail=None):
    """Somebody tried to start business with a restricted client.

    Logged whether it was refused or merely warned about, because the
    question an administrator asks later is "who keeps trying?", and a
    register that only records the refusals cannot answer it.
    """
    if verdict is None or verdict.clear or not verdict.restriction_id:
        return
    log_event(verdict.restriction_id, 'attempt_blocked',
              user_id=user_id, note=f'{what} refused',
              payload={'what': what, 'level': verdict.level,
                       'matched_on': verdict.matched_on,
                       **(detail or {})})


def record_acknowledgement(verdict, *, what, user_id=None, entity_id=None):
    if verdict is None or not verdict.restriction_id:
        return
    log_event(verdict.restriction_id, 'caution_acknowledged',
              user_id=user_id,
              note=f'{what} — acknowledged and continued',
              payload={'what': what, 'entity_id': entity_id})
