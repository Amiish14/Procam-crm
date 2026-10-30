"""
The account behind a lead: naming it, linking it, and fixing who owns it.

CRM-LEAD-ACCOUNT-01, behind `FEATURE_LEAD_ACCOUNT`.

A GeM tender for BEML — 17 high-speed train cars, ₹2 Cr — arrived on
29 Oct 2026 from a Procam address. The parser could not identify the
customer, so the lead was saved as **"To review"**. BEML LIMITED was
already an account with 40 leads and 2 wins, and there was no way to
join the two: the lead sheet has no account field, and the account
page offers Log Activity, Change Stage, Reassign PIC and Convert.
Fixing it meant closing the lead, going to Accounts, and coming back
to find you still could not link it.

This module answers four questions, so the screens do not each
invent their own answer:

    is_placeholder(name)    is this a company name at all?
    suggest(lead, q)        which accounts could this lead be?
    link(lead, account)     join them, and say what changed.
    owner_question(...)     the lead's PIC and the account's owner
                            disagree — which is right?

Nothing here is new machinery. `account_match` already finds
candidates, `presales.services.reassign_account` is already the one
path that writes PIC history, and `auto_assign` already reads the
account owner when the next lead arrives. The gap was that none of
it could be reached from the lead sheet.
"""
from __future__ import annotations

FLAG = 'FEATURE_LEAD_ACCOUNT'

#: Names that describe an empty row rather than a company. Seeded
#: from what live data actually holds; `placeholders()` reads the
#: Master Data list on top of this, so a new one does not need a
#: release. Compared lower-cased and stripped.
PLACEHOLDER_DEFAULTS = (
    'to review', 'unknown', 'new lead', 'new enquiry', 'email',
    'info', 'enquiry', 'n/a', 'na', 'tbd', 'test', 'customer',
    'client', 'none', 'no name', '-', '--', '.', '?',
)
SETTING_KEY = 'lead_placeholder_names'

#: A subject line wearing a company's clothes. "Re:" and "Fwd:" are
#: the giveaways the parser leaves behind when it gives up.
_SUBJECT_PREFIXES = ('re:', 'fw:', 'fwd:', 're :', 'fwd :')

#: Longer than any real company name in the master today (the
#: longest is 61 characters), and short enough to catch a subject.
_TOO_LONG = 70


def enabled(emp_code=None):
    from flask import has_request_context, session

    from app.services import flags

    if emp_code is None and has_request_context():
        return flags.on_for(FLAG, session.get('emp_code'))
    if emp_code is None:
        return flags.on(FLAG)
    return flags.on_for(FLAG, emp_code)


def placeholders():
    """The configured list, falling back to the seeded one.

    Admin → Settings holds `lead_placeholder_names` as a list or a
    comma-separated string. A broken setting falls back rather than
    emptying the list: an empty list would quietly switch the whole
    feature off.
    """
    from app.services import admin_settings

    try:
        raw = admin_settings.get(SETTING_KEY)
    except Exception:                                     # noqa: BLE001
        raw = None
    if isinstance(raw, str):
        raw = [part for part in raw.split(',')]
    if not isinstance(raw, (list, tuple)) or not raw:
        return frozenset(PLACEHOLDER_DEFAULTS)
    found = frozenset(str(x).strip().lower() for x in raw
                      if str(x).strip())
    return found or frozenset(PLACEHOLDER_DEFAULTS)


def is_placeholder(name):
    """Why this is not a company name, or ''.

    Deliberately conservative. A false positive blocks Save on a
    real customer, which is far worse than letting one odd name
    through — somebody can still link the account by hand.
    """
    text = (name or '').strip()
    if not text:
        return 'the company name is blank'
    low = text.lower()

    if low in placeholders():
        return f'"{text}" is a placeholder, not a company'
    if '@' in text:
        return f'"{text}" is an email address, not a company'
    if low.startswith(_SUBJECT_PREFIXES):
        return f'"{text[:40]}…" is a mail subject, not a company'
    if len(text) > _TOO_LONG:
        return 'that looks like a subject line, not a company name'
    ours = _is_ours(text)
    if ours:
        return ours
    staff = _is_staff(text)
    if staff:
        return staff
    return ''


def _is_ours(text):
    """Procam itself is not a customer.

    Read off our own mail domains rather than written out here, so
    a new one is known the moment it is added: "procamgroup.in"
    and "procamlogistics.com" both reduce to a stem that
    "Procam Logistics Pvt Ltd" starts with once the spaces go.
    """
    import re

    squashed = re.sub(r'[^a-z]', '', (text or '').lower())
    if not squashed:
        return ''
    try:
        from app.services.mail_signals import OUR_DOMAINS
    except Exception:                                     # noqa: BLE001
        return ''
    for domain in OUR_DOMAINS:
        stem = re.sub(r'[^a-z]', '', (domain or '').split('.')[0])
        if not stem:
            continue
        # The shared prefix of every one of our domains — "procam" —
        # is what a forwarded internal name actually carries.
        for length in range(len(stem), 5, -1):
            if squashed.startswith(stem[:length]):
                return (f'"{text}" is Procam, not the customer — '
                        f'the mail was forwarded from inside')
    return ''


def _is_staff(text):
    """A colleague's name, left behind when the forwarder was read
    as the customer. Only our own people, never a guess at whether
    a string looks like somebody's name: "Tata Steel" is two words
    and a capital letter too."""
    low = ' '.join((text or '').lower().split())
    if not low or ' ' not in low:
        return ''
    try:
        from app import Employee
        hit = (Employee.query.with_entities(Employee.name)
               .filter(db_func_lower(Employee.name) == low).first())
        if hit:
            return (f'"{text}" is a Procam colleague, not the '
                    f'customer')
    except Exception:                                     # noqa: BLE001
        pass
    return ''


def db_func_lower(column):
    from sqlalchemy import func
    return func.lower(column)


# ── what the lead sheet asks ─────────────────────────────────────────
def state(lead):
    """What the lead sheet needs to draw the account row.

    `blocked` is the one that matters: Save is refused while the
    company is a placeholder and no account is linked, because a
    lead nobody can find is a lead nobody works.
    """
    from app import Company, db

    account = (db.session.get(Company, lead.company_id)
               if getattr(lead, 'company_id', None) else None)
    why = is_placeholder(getattr(lead, 'company', ''))
    linked = account is not None
    return {
        'linked': linked,
        'account_id': getattr(account, 'id', None),
        'account': getattr(account, 'name', '') or '',
        'company': getattr(lead, 'company', '') or '',
        'placeholder': bool(why),
        'why': why,
        'blocked': bool(why) and not linked,
        'banner': ('Company not identified — choose or create account'
                   if why and not linked else ''),
    }


def save_refusal(lead, incoming=None):
    """The sentence to refuse a save with, or ''.

    `incoming` is the submitted payload: a save that links the
    account in the same request satisfies the rule, which is what
    the picker does.
    """
    if not enabled():
        return ''
    incoming = incoming or {}
    if incoming.get('company_id'):
        return ''
    found = state(lead)
    if not found['blocked']:
        return ''
    name = (incoming.get('company') or lead.company or '').strip()
    if name and not is_placeholder(name):
        # The user typed a real name in the same save. The picker
        # is still how the link gets made, but refusing here would
        # strand somebody who fixed the obvious thing first.
        return ''
    return ('Link or create the account before saving. Close as '
            'Lost, Mark duplicate and Delete – not relevant are '
            'still available.')


# ── finding the account ──────────────────────────────────────────────
def suggest(lead, query='', *, limit=8):
    """Accounts this lead could belong to, surest first.

    With no query this is the pre-fill: what the parser read, the
    sender's domain and the tender text, through the matcher that
    already runs at intake. With a query it is the type-ahead.
    """
    from app import Company

    query = (query or '').strip()
    rows, seen = [], set()

    if not query:
        for found in _from_the_mail(lead):
            if found['account_id'] in seen:
                continue
            seen.add(found['account_id'])
            rows.append(found)

    if query:
        for account in _by_name(query, limit=limit * 2):
            if account.id in seen:
                continue
            seen.add(account.id)
            rows.append(_row(account, _why_name(account, query)))

    # An account that can never be linked is not a suggestion, it is
    # noise. #709 is called "Email" and #983 "Unknown"; both answer a
    # name search for almost anything, and `link` refuses both. The
    # live run for the BEML tender offered "Email" as a candidate
    # alongside BEML LIMITED.
    return [row for row in rows if not row['not_a_customer']][:limit]


def _from_the_mail(lead):
    """The matcher's own answer for this lead, with its reasons.

    The parser's reading is used even when it could not be saved as
    the company: the BEML tender arrived with
    "Bharat Earth Movers Limited (BEML)" in the extract and
    "To review" in the company field, and the account it belonged
    to was called "BEML LIMITED". Matching on the placeholder finds
    nothing; matching on what the parser read finds it at once.
    """
    from app.services import account_match

    sender = (getattr(lead, 'email', '')
              or getattr(lead, 'original_email_from', '') or '')
    text = ' '.join(filter(None, (
        getattr(lead, 'original_email_subject', '') or '',
        getattr(lead, 'forwarded_subject', '') or '',
        (getattr(lead, 'original_email_body', '') or '')[:2000],
        getattr(lead, 'project', '') or '',
    )))

    out, seen = [], set()
    for name in _names_to_try(lead):
        try:
            found = account_match.candidates(
                sender, company_name=name, text=text) or []
        except Exception:                                 # noqa: BLE001
            continue
        for item in found:
            account = item.get('account')
            if account is None or account.id in seen:
                continue
            seen.add(account.id)
            rule = item.get('rule') or 'matched'
            also = item.get('also') or []
            if also:
                rule = rule + ' + ' + ' + '.join(also)
            if name:
                rule = f'{rule} (read as "{name}")'
            out.append(_row(account, rule,
                            exact=bool(item.get('exact')),
                            kind=item.get('kind') or 'name',
                            note=item.get('note') or ''))
    return out


def _names_to_try(lead):
    """The lead's own name when it is real, then the parser's.

    An acronym in brackets is tried on its own: accounts are filed
    under "BEML LIMITED", not under the full legal name the tender
    document uses.
    """
    import re

    names, seen = [], set()

    def add(value):
        text = (value or '').strip()
        if not text or text.lower() in seen:
            return
        seen.add(text.lower())
        names.append(text)

    own = getattr(lead, 'company', '') or ''
    if own and not is_placeholder(own):
        add(own)
    parsed = _parsed_company(lead)
    add(parsed)
    for source in (parsed, own):
        for inside in re.findall(r'\(([^)]{2,20})\)', source or ''):
            add(inside)
        # "Bharat Earth Movers Limited (BEML)" without the bracket.
        stripped = re.sub(r'\s*\([^)]*\)\s*', ' ', source or '').strip()
        if stripped and stripped != (source or '').strip():
            add(stripped)
    # The sender's own domain always runs, name or no name.
    names.append('')
    return names


def _parsed_company(lead):
    """What the inbound parser read as the customer, or ''."""
    import json

    raw = getattr(lead, 'email_extracted_json', None)
    if not raw:
        return ''
    try:
        found = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:                                     # noqa: BLE001
        return ''
    if not isinstance(found, dict):
        return ''
    name = (found.get('company') or found.get('company_name') or '')
    name = str(name).strip()
    return '' if is_placeholder(name) else name


def _by_name(query, *, limit=16):
    """Name, alias and domain, in one pass over the master."""
    from app import Company
    from sqlalchemy import or_

    like = f'%{query.lower()}%'
    clauses = [db_func_lower(Company.name).like(like)]
    for column in ('website', 'gstin'):
        if hasattr(Company, column):
            clauses.append(
                db_func_lower(getattr(Company, column)).like(like))
    rows = (Company.query
            .filter(Company.is_active.isnot(False))
            .filter(or_(*clauses))
            .order_by(Company.name).limit(limit).all())
    seen = {row.id for row in rows}
    # Aliases and mail domains are JSON lists, which SQLite cannot
    # search portably, so they are read in Python. Bounded, and
    # only to top up a thin result.
    if len(rows) < limit:
        low = query.lower()
        for account in (Company.query
                        .filter(Company.is_active.isnot(False))
                        .order_by(Company.id.desc()).limit(600).all()):
            if account.id in seen:
                continue
            haystack = ((getattr(account, 'aliases', None) or [])
                        + (getattr(account, 'email_domains', None) or []))
            if any(low in str(item).lower() for item in haystack):
                rows.append(account)
                seen.add(account.id)
            if len(rows) >= limit:
                break
    return rows[:limit]


def _why_name(account, query):
    low = (query or '').lower()
    name = (account.name or '').lower()
    if name.startswith(low):
        return 'name starts with what you typed'
    if low in name:
        return 'name contains what you typed'
    for alias in (getattr(account, 'aliases', None) or []):
        if low in str(alias).lower():
            return f'alias "{alias}" matches'
    for domain in (getattr(account, 'email_domains', None) or []):
        if low in str(domain).lower():
            return f'mail domain {domain} matches'
    if low in ((getattr(account, 'gstin', '') or '').lower()):
        return 'GSTIN matches'
    return 'website matches'


def _row(account, rule, *, exact=False, kind='name', note=''):
    from app.services import account_match, rfq_board

    owner = (getattr(account, 'pic_emp_code', '') or '')
    return {
        'account_id': account.id,
        'name': account.name or '',
        'rule': rule,
        'exact': exact,
        # 'exact', 'name' or 'resemblance'. The matcher is explicit
        # that a resemblance is "these two strings are 60% alike" and
        # no evidence at all, and the rule string cannot be told
        # apart from a real name match — both read "company name".
        # Anything deciding on its own needs this, not the words.
        'kind': kind,
        'note': note,
        'owner': owner,
        'owner_name': rfq_board.name_of(owner),
        'vertical': getattr(account, 'vertical', '') or '',
        'open_leads': _open_leads(account.id),
        'wins': _wins(account.id),
        'blocked': _blocked(account),
        'not_a_customer': account_match.not_a_customer(account),
    }


def _open_leads(account_id):
    from app import Lead, db
    from app.services import sales_rules
    try:
        return (db.session.query(Lead.id)
                .filter(Lead.company_id == account_id,
                        Lead.is_archived.isnot(True),
                        Lead.stage.in_(sales_rules.open_stages()))
                .count())
    except Exception:                                     # noqa: BLE001
        return 0


def _wins(account_id):
    from app import Lead, db
    try:
        return (db.session.query(Lead.id)
                .filter(Lead.company_id == account_id,
                        Lead.stage == 'Won').count())
    except Exception:                                     # noqa: BLE001
        return 0


def _blocked(account):
    try:
        from app.services import client_restrictions
        return bool(client_restrictions.check(
            company_name=account.name,
            gstin=getattr(account, 'gstin', None),
            account_id=account.id).blocked)
    except Exception:                                     # noqa: BLE001
        return False


# ── joining them ─────────────────────────────────────────────────────
def link(lead, account, *, actor, reason='', learn=True):
    """Attach the lead to the account. (ok, detail-or-why).

    Writes both histories — the lead's and the account's — because
    "when did this lead become BEML's?" is asked from both ends.
    """
    from app import db
    from app.services import account_match, audit

    if account is None:
        return False, 'Choose an account'
    if _blocked(account):
        return False, f'{account.name} is on the block register.'
    not_customer = account_match.not_a_customer(account)
    if not_customer:
        return False, not_customer

    was = {'company_id': lead.company_id, 'company': lead.company}
    changing = bool(lead.company_id) and lead.company_id != account.id
    previous_id = lead.company_id
    lead.company_id = account.id
    lead.company = account.name

    learned = False
    sender = (getattr(lead, 'email', '')
              or getattr(lead, 'original_email_from', '') or '')
    if learn and sender:
        try:
            account_match.learn(sender, account.id, lead_id=lead.id,
                                actor=actor, commit=False)
            learned = True
        except Exception:                                 # noqa: BLE001
            learned = False

    detail = {'company_id': account.id, 'company': account.name}
    audit.record('lead.account_linked', 'lead', lead.id,
                 old=was, new=detail, actor=actor,
                 reason=reason or 'linked from the lead sheet')
    audit.record('account.lead_linked', 'company', account.id,
                 new={'lead_id': lead.id, 'was': was.get('company')},
                 actor=actor,
                 reason=reason or 'linked from the lead sheet')
    if changing:
        # The account it left hears about it too: "which leads did
        # we lose?" is asked from that end, and an audit row only
        # on the new account cannot answer it.
        audit.record('account.lead_unlinked', 'company', previous_id,
                     old={'lead_id': lead.id,
                          'company': was.get('company')},
                     new={'moved_to': account.name,
                          'moved_to_id': account.id},
                     actor=actor,
                     reason=reason or 'moved from the lead sheet')
    db.session.flush()
    return True, {'account': account.name, 'account_id': account.id,
                  'learned': learned,
                  'owner_question': owner_question(lead, account)}


def owner_question(lead, account):
    """§2.4's prompt, in its two shapes.

    The spec names one: the lead's PIC is X, the account's owner is
    Y, keep or switch. Live data has a second and commoner one —
    the lead has no PIC at all. #12397, the BEML tender this was
    built for, was linked and left owned by nobody, and asking
    "keep nobody or switch?" is not a question. It becomes an
    offer: the account owner, one click.

    None when they already agree, or when the account has no owner
    either — then there is nothing to offer and the account is the
    thing to fix.
    """
    from app.services import rfq_board

    pic = (getattr(lead, 'assigned_to', '') or '').strip()
    owner = (getattr(account, 'pic_emp_code', '') or '').strip()
    if not owner or pic == owner:
        return None
    return {
        'unowned': not pic,
        'lead_pic': pic, 'lead_pic_name': rfq_board.name_of(pic),
        'account_owner': owner,
        'account_owner_name': rfq_board.name_of(owner),
        'account': account.name, 'account_id': account.id,
    }


# ── who owns the account (§2.5) ──────────────────────────────────────
def may_change_owner(account, emp_code):
    """The account's own owner, the vertical head, or an admin."""
    from app import Employee

    if account is None or not emp_code:
        return False
    if (getattr(account, 'pic_emp_code', '') or '') == emp_code:
        return True
    emp = Employee.query.filter_by(emp_code=emp_code).first()
    role = (getattr(emp, 'role', '') or '').lower()
    if role in ('admin', 'super admin', 'superadmin', 'director'):
        return True
    try:
        from app.services import vertical_heads
        vertical = (getattr(account, 'vertical', '') or ''
                    or getattr(emp, 'vertical', '') or '')
        return emp_code in (vertical_heads.heads_for_vertical(vertical)
                            or [])
    except Exception:                                     # noqa: BLE001
        return False


def other_open_leads(account, old_owner):
    """Open leads of this account still with the previous owner.

    Won and Lost are never offered: moving a closed lead rewrites
    history that somebody has already reported on.
    """
    from app import Lead
    from app.services import sales_rules

    if account is None or not old_owner:
        return []
    rows = (Lead.query
            .filter(Lead.company_id == account.id,
                    Lead.assigned_to == old_owner,
                    Lead.is_archived.isnot(True),
                    Lead.stage.in_(sales_rules.open_stages()))
            .order_by(Lead.id.desc()).limit(200).all())
    return [{'id': row.id, 'company': row.company or '',
             'stage': row.stage or '',
             'project': (row.project or '')[:80]} for row in rows]


def correct_owner(account, *, new_owner, actor, reason,
                  secondary=None, move_lead_ids=(), from_lead=None):
    """Set the account's owner, and move the leads that were chosen.

    (ok, detail-or-why). Nothing moves that was not named: the
    screen shows the list and the person picks, because a silent
    bulk reassignment is how a vertical head loses nine leads
    without being told.
    """
    from app import Lead, db
    from app.services import audit

    if account is None:
        return False, 'No account'
    new_owner = (new_owner or '').strip()
    if not new_owner:
        return False, 'Choose the owner'
    if not (reason or '').strip():
        return False, 'A reason is recorded in the PIC history — '\
                      'please give one'

    old_owner = (getattr(account, 'pic_emp_code', '') or '')
    from presales import services as presales_services
    try:
        row = presales_services.reassign_account(
            account=account, new_pic_code=new_owner,
            changed_by_code=actor, reason=reason,
            new_secondary_code=(secondary if secondary is not None
                                else presales_services.UNCHANGED))
    except presales_services.PreSalesError as exc:
        return False, str(exc)

    moved = []
    wanted = {int(x) for x in (move_lead_ids or []) if str(x).isdigit()}
    if wanted:
        for lead in Lead.query.filter(Lead.id.in_(wanted),
                                      Lead.company_id == account.id).all():
            if (lead.assigned_to or '') == new_owner:
                continue
            _move(lead, new_owner, actor=actor, account=account)
            moved.append(lead.id)

    _teach(account, new_owner, actor=actor)

    audit.record(
        'account.owner_corrected', 'company', account.id,
        old={'owner': old_owner},
        new={'owner': new_owner, 'leads_moved': moved,
             'from_lead': getattr(from_lead, 'id', None),
             'account': account.name},
        actor=actor, reason=reason)
    db.session.flush()
    return True, {'owner': new_owner, 'was': old_owner,
                  'moved': moved, 'changed': row is not None,
                  'account': account.name}


def _move(lead, owner, *, actor, account):
    from app.services import lead_assignment
    note = (f'Account owner corrected on {account.name} — '
            f'moved with the account')
    try:
        ok, _why = lead_assignment.assign(
            lead, primary_code=owner, actor=actor, note=note,
            _defer_commit=True)
        if ok:
            return
    except Exception:                                     # noqa: BLE001
        pass
    lead.assigned_to = owner


def _teach(account, owner, *, actor):
    """Record the correction where auto-assign will read it.

    `AccountVerticalOwner` is what `auto_assign.decide` consults
    first, so a correction that stops at `pic_emp_code` is undone
    by the next lead in a vertical that has its own row.
    """
    from app import db
    from app.models.assignment import AccountVerticalOwner
    from app.services import audit

    vertical = (getattr(account, 'vertical', '') or '').strip()
    if not vertical:
        return
    row = AccountVerticalOwner.query.filter_by(
        account_id=account.id, vertical=vertical).first()
    if row is None:
        db.session.add(AccountVerticalOwner(
            account_id=account.id, vertical=vertical, emp_code=owner,
            source='manual', note='corrected from the lead sheet'))
    else:
        row.active = True
    if row is not None and (row.emp_code or '') != owner:
        was = row.emp_code
        row.emp_code = owner
        audit.record('account.vertical_owner_corrected', 'company',
                     account.id,
                     old={'vertical': vertical, 'emp_code': was},
                     new={'vertical': vertical, 'emp_code': owner},
                     actor=actor,
                     reason='corrected from the lead sheet')


def corrections(*, limit=200):
    """Admin → Owner corrections. Read from the audit trail.

    No new table: `audit` is already the system of record for who
    changed what, and a second one would be the thing that drifts.
    """
    from app import Company, db
    from app.models.audit import AuditEvent
    from app.services import rfq_board

    rows = (AuditEvent.query
            .filter(AuditEvent.action == 'account.owner_corrected')
            .order_by(AuditEvent.id.desc()).limit(limit).all())
    out = []
    for row in rows:
        new = _payload(row, 'new')
        old = _payload(row, 'old')
        try:
            account_id = int(row.entity_id)
        except (TypeError, ValueError):
            account_id = None
        account = (db.session.get(Company, account_id)
                   if account_id else None)
        out.append({
            'at': str(getattr(row, 'occurred_at', '') or '')[:16],
            'account_id': account_id,
            'account': (new.get('account')
                        or getattr(account, 'name', '') or ''),
            'was': rfq_board.name_of(old.get('owner')),
            'now': rfq_board.name_of(new.get('owner')),
            'by': rfq_board.name_of(row.actor),
            'leads_moved': len(new.get('leads_moved') or []),
            'from_lead': new.get('from_lead'),
            'reason': (row.reason or '')[:200],
        })
    return out


def _payload(row, which):
    """`AuditEvent` stores these as JSON columns, not strings."""
    raw = getattr(row, f'{which}_value', None)
    return raw if isinstance(raw, dict) else {}


# ── the clean-up (§2.6) ──────────────────────────────────────────────
def unplaced(sc=None, *, limit=500):
    """Open leads whose company is a placeholder, through the matrix."""
    from app import Lead
    from app.access import scope as sc_mod
    from app.services import rfq_board, sales_rules

    query = (Lead.query
             .filter(Lead.is_archived.isnot(True),
                     Lead.stage.in_(sales_rules.open_stages())))
    query = sc_mod.leads(query, sc=sc)
    out = []
    for lead in query.order_by(Lead.id.desc()).limit(limit * 4).all():
        if lead.company_id:
            continue
        why = is_placeholder(lead.company)
        if not why:
            continue
        out.append({
            'lead_id': lead.id,
            'company': lead.company or '',
            'why': why,
            'sender': (lead.email or lead.original_email_from or ''),
            'subject': (lead.original_email_subject
                        or lead.forwarded_subject or '')[:120],
            'stage': lead.stage or '',
            'owner': rfq_board.name_of(lead.assigned_to),
            'created': str(lead.created_at or '')[:10],
        })
        if len(out) >= limit:
            break
    return out
