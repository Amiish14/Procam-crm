"""Leads filed under the portal that delivered them, not the buyer.

CRM-UAT-01 item 1, from the UAT of 14 November on live. Leads #12642,
#12644 (RFX-56172, ArcelorMittal Nippon Steel India) and #12645
(RFQ-112165) carry the account **"Procol"** — which is not a company.
The parser reads a company out of the sender's domain, and on a
procurement portal's mail the sender is the portal, so the buyer of
each RFQ became the robot that delivered it and three different
buyers' enquiries pooled under one row.

The intake no longer does this: the buyer read out of the body wins,
and `account_match.not_a_customer` refuses a portal-named account, so
nothing can be linked to one again. This moves the leads that were
already filed that way.

    .venv/bin/python scripts/2026_11_14_portal_accounts.py --check
    .venv/bin/python scripts/2026_11_14_portal_accounts.py --check --csv
    .venv/bin/python scripts/2026_11_14_portal_accounts.py --apply \
          --actor DIR12010 --add-alias 652="ArcelorMittal Nippon Steel"
    .venv/bin/python scripts/2026_11_14_portal_accounts.py --apply \
          --only-link --actor DIR12010
    .venv/bin/python scripts/2026_11_14_portal_accounts.py --apply --actor DIR12010
    .venv/bin/python scripts/2026_11_14_portal_accounts.py --undo --batch PACC-20261114-1

READ FROM THE WHOLE TRAIL, NOT THE FIRST MAIL

14 Nov, second review. Five of these leads -- #12644 (RFX-56172),
#12642 (RFX-55831), #12643 (RFX-56168), #11971 and #12163 -- were
created by a bare "Technical is now live" notice, and the mail that
names the buyer arrived after it: "Project Invitation from
ArcelorMittal Nippon Steel ...". The event number held the set
together; the name was one mail away and nothing was reading it. So
the buyer is read from the lead's own mail AND every row of
`lead_emails`, oldest first, and the report says which mail answered.

ONE ACCOUNT PER CUSTOMER

ArcelorMittal Nippon Steel India is the existing account **"Amns"**
(#652). `account_for` matches a name against an account's aliases,
against its acronym -- "Amns" is A-M-N-S, and camel case is read, so
"ArcelorMittal" is two words for that purpose -- and against
containment either way round, on names long enough for containment to
mean anything. Where the match was not on the name, `--apply` writes
the buyer's spelling onto that account as an alias, so the next RFQ
matches on the name and a person reading the account can see what its
mail arrives under. `--add-alias 652="ArcelorMittal Nippon Steel"`
states it outright, which beats every reading above.

WHERE EACH LEAD GOES

The buyer is read from the mail the lead was made from, by the same
`portal_rfq.describe` the intake uses — the buyer-organisation line,
the event owner, "X has invited you", "invited … by X".

  * **the buyer's existing account**, where a name matches one. Asked
    through `company_match.norm`, so "Arcelormittal Nippon Steel
    India Ltd." and "ArcelorMittal Nippon Steel India" are one
    customer.
  * **a new account**, where the buyer is named and no account
    answers to it. Name and vertical only: NO email domain, ever. The
    sender is `no-reply@procol.in`, and writing that domain onto a
    buyer's account would make the portal's next RFQ -- for a
    different buyer -- link straight to this one.
  * **"To review"**, with no account, where no mail on the trail
    names a buyer AND somebody is working the lead (a quote, an RFQ,
    a call, a note, a mail we sent, or a stage past New) or it is
    younger than 30 days. The account-check queue is where a lead
    whose customer is unknown belongs; nothing is lost, because the
    mail trail is on the lead and the tender number is on the sheet.
  * **archived**, reason "portal notice — no buyer", where no mail
    names a buyer, nothing has been done on the lead, and it is more
    than 30 days old. A portal notice nobody can attach to a buyer
    is not work -- it is noise in the review queue, and a queue with
    noise in it stops being read. Through `lead_archive`, which asks
    whether this actor may archive this lead, so the sweep is not a
    way round a permission; reversible with `--undo`.

ONE PASS OR TWO

`--only-link` applies the link and create rows and leaves the rest,
which is the right order when those have been read and agreed and the
review-or-archive split has not. Running it again later with no flag
does the remainder.

The portal's own account rows are left exactly as they are. Nothing is
deleted and nothing is merged: an account with no leads on it is
harmless, and deciding what to do with a row somebody may have typed
notes on is not a sweep's business. They are listed at the end.

UNDO

Every write carries an audit row with the batch id. `--undo --batch`
puts each lead back on the account it was on and names the accounts
the batch created, which it deactivates rather than deletes -- a lead
somebody has since linked to one of them by hand keeps it, and is
reported.
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import re
import sys
from collections import Counter
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from app import app, db                                    # noqa: E402

ACTOR = 'CRM-UAT-01'
ACTION = 'lead.portal_account_corrected'
UNDONE = 'lead.portal_account_restored'
BATCH_PREFIX = 'PACC'
DEFAULT_CSV = os.path.join(_ROOT, 'data', 'portal_accounts_14NOV.csv')

#: What a lead says when its customer is not known. The same word the
#: intake uses, so the account-check queue holds one spelling.
TO_REVIEW = 'To review'


def _rule(n=72):
    print('─' * n)


# ── finding them ────────────────────────────────────────────────────
def leads_on_a_portal():
    """[(lead, what)] — leads whose account or company is a portal.

    Both ends: the linked account's name, and the free-text company,
    because a lead can carry "Procol" with no account at all and it
    reads the same way on every screen.
    """
    from app import Company, Lead
    from app.services import portal_rfq

    portal_accounts = {}
    for account in Company.query.all():
        if portal_rfq.is_portal_name(account.name):
            portal_accounts[account.id] = account

    out = []
    rows = (Lead.query
            .filter(Lead.is_archived.isnot(True))
            .order_by(Lead.id.asc()).all())
    for lead in rows:
        if lead.company_id and lead.company_id in portal_accounts:
            out.append((lead, f'account "{lead.company}"'))
        elif portal_rfq.is_portal_name(lead.company or ''):
            out.append((lead, f'company "{lead.company}" (no account)'))
    return out, portal_accounts


def trail_of(lead):
    """[(what, subject, body)] — every mail on this lead, oldest first.

    The lead's own mail first, then `lead_emails`. 14 Nov, from the
    second review: the buyer was being read from the FIRST mail only,
    and on five of these leads the mail that names the buyer is
    further down the trail -- "Project Invitation from ArcelorMittal
    Nippon Steel ..." arrived after the bare RFX notice that created
    the lead. The number held them together; the name was one mail
    away and nobody was reading it.
    """
    out = [('the lead\'s own mail',
            lead.original_email_subject or '',
            lead.original_email_body or '')]
    try:
        from app import LeadEmail
        rows = (LeadEmail.query
                .filter(LeadEmail.lead_id == lead.id)
                .order_by(LeadEmail.sent_or_received_at.asc(),
                          LeadEmail.id.asc()).all())
    except Exception:                                     # noqa: BLE001
        rows = []
    for row in rows:
        when = str(row.sent_or_received_at or '')[:10]
        out.append((f'{row.direction or "mail"} {when}'.strip(),
                    row.subject or '', row.body or ''))
    return out


def buyer_of(lead):
    """(name, how) read from anywhere on this lead's mail trail.

    The first mail that names one wins, oldest first: an invitation
    states whose event it is, and a later notice quoting it says the
    same thing less clearly.
    """
    from app.services import portal_rfq

    for what, subject, body in trail_of(lead):
        buyer, how = portal_rfq.buyer_from(subject, body)
        if buyer:
            return buyer, f'{how}, in {what}'
    return '', ''


def _words(name):
    """The words of a company name, camel case split.

    "ArcelorMittal Nippon Steel India" is four words to a person and
    five to an acronym: Arcelor, Mittal, Nippon, Steel, India. Both
    readings are tried.
    """
    import re as _re

    text = _re.sub(r'[^A-Za-z0-9 ]+', ' ', str(name or ''))
    plain = [w for w in text.split() if w]
    split = []
    for word in plain:
        parts = _re.findall(r'[A-Z][a-z0-9]+|[A-Z]{2,}(?![a-z])|[a-z0-9]+',
                            word)
        split.extend(parts or [word])
    return plain, split


#: Words that carry no identity and are dropped before an acronym is
#: built: "AMNS" is ArcelorMittal Nippon Steel, not ANSIL.
_SUFFIXES = {'limited', 'ltd', 'private', 'pvt', 'plc', 'inc',
             'corporation', 'corp', 'company', 'co', 'llp', 'india',
             'the', 'and', 'of'}


def acronyms_of(name):
    """{'AMNS', 'AMNSI', ...} — the initials this name could be known by.

    Both word readings, and each with and without the trailing
    place-and-form words, because "ArcelorMittal Nippon Steel India
    Limited" is filed under AMNS and could as easily have been AMNSI.
    """
    out = set()
    for words in _words(name):
        kept = [w for w in words if w.lower() not in _SUFFIXES]
        for run in (words, kept):
            if len(run) >= 2:
                out.add(''.join(w[0] for w in run).upper())
    # Three letters at least: "TS" for Tata Steel is a coincidence
    # waiting to happen, and an account genuinely called TS would
    # collect every two-word customer in the book.
    return {a for a in out if 3 <= len(a) <= 8}


#: An account merged away is renamed "<name> (merged into #652)" by
#: `app/account_merge` and marked inactive. The number in that name is
#: how a name that no longer belongs to anybody still leads to the
#: customer it was folded into.
_MERGED_INTO = re.compile(r'merged\s+into\s+#?(\d+)', re.I)


def names_of(account):
    """Every name this account answers to: its own and its aliases.

    14 Nov, and the fault that put a new "ArcelorMittal Nippon Steel
    India ..." row on live in batch PACC-20261114-2 (#11970) with
    `--add-alias 652=...` on the same command line. The alias WAS
    written first and committed first. Only the first reading below
    ever looked at it -- exact equality -- and the buyer's name
    carried "India Limited" on top of the alias, so nothing matched
    and the acronym reading could not save it either: "Arcelormittal"
    with a small m is one word, and its initials are ANS, not AMNS.

    An alias is somebody saying "this account answers to this name".
    Every reading has to hear it, not just the strictest one.
    """
    out = [account.name or '']
    for alias in (getattr(account, 'aliases', None) or []):
        text = str(alias or '').strip()
        if text:
            out.append(text)
    # A merged row is renamed "<name> (merged into #652)". The name it
    # answers to is the part in front of that, which is the whole
    # point of looking at it: the buyer whose RFQs still arrive under
    # the old spelling has to reach the survivor.
    for name in list(out):
        bare = _MERGED_INTO.sub('', name)
        bare = bare.replace('()', '').strip(' ()-–—')
        if bare and bare != name:
            out.append(bare)
    return [n for n in out if n]


def _follow_merge(account):
    """The survivor, where this row was merged into another."""
    found = _MERGED_INTO.search(account.name or '')
    if not found:
        return None
    from app import Company

    survivor = db.session.get(Company, int(found.group(1)))
    return survivor if survivor is not None else None


def _candidates(*, active_only=True):
    from app import Company
    from app.services import account_match, portal_rfq

    rows = Company.query.all()
    out = []
    for account in rows:
        if active_only and getattr(account, 'is_active', True) is False:
            continue
        if portal_rfq.is_portal_name(account.name):
            continue
        if active_only and account_match.not_a_customer(account):
            continue
        out.append(account)
    return out


def account_for(name):
    """An existing account that answers to this name, or None.

    Four readings, strongest first, and **every one of them over every
    name the account answers to** -- its own and its aliases:

      1. the normalised name;
      2. the name is the ACRONYM of the one we are looking for --
         "Amns" is ArcelorMittal Nippon Steel, account #652;
      3. the name, whole, inside the one we are looking for --
         "ArcelorMittal Nippon Steel" inside "ArcelorMittal Nippon
         Steel India Limited", which is what an alias is FOR;
      4. and the same the other way round.

    2-4 return the account AND the fact that the match was not on an
    exact name, so `--apply` writes the buyer's spelling on as an
    alias and the next RFQ matches on reading 1.
    """
    from app.services import company_match

    want = company_match.norm(name or '')
    if not want or len(want) < 3:
        return None, ''
    wanted_acronyms = acronyms_of(name)
    rows = [(account, [(n, company_match.norm(n)) for n in names_of(account)])
            for account in _candidates()]

    for account, names in rows:
        for _raw, norm in names:
            if norm == want:
                return account, ''

    for account, names in rows:
        for raw, _norm in names:
            if raw.strip().upper() in wanted_acronyms:
                return account, f'"{raw}" is the acronym of "{name}"'

    # Containment, and only on names long enough for it to mean
    # something: on four characters there is no reading that separates
    # BHEL from BEML, which is why `account_match` refuses to resemble
    # a short name at all.
    for account, names in rows:
        for raw, norm in names:
            if len(norm) < 8 or len(want) < 8:
                continue
            if norm in want:
                return account, (f'"{raw}" is "{name}" without the '
                                 f'rest of it')
            if want in norm:
                return account, (f'"{name}" is "{raw}" without the '
                                 f'rest of it')
    return None, ''


def namesake_elsewhere(name):
    """(account, why) for a name that exists on a row we skipped.

    The second half of "make sure a re-run does not recreate it". An
    account merged away keeps its row, inactive and renamed "<name>
    (merged into #652)", and an account somebody deactivated keeps
    its name too. Neither is a candidate to link to -- but both mean
    a row with this name already exists, and creating a second one is
    how a master gets two rows for one customer twice over.

    Where the row says which account it was merged into, that
    survivor is the answer and the lead is linked there. Where it does
    not, nothing is created and nothing is guessed: it is reported for
    a person.
    """
    from app.services import company_match

    want = company_match.norm(name or '')
    if not want or len(want) < 3:
        return None, ''
    for account in _candidates(active_only=False):
        if getattr(account, 'is_active', True) is not False:
            continue
        for raw in names_of(account):
            if company_match.norm(raw) != want:
                continue
            survivor = _follow_merge(account)
            if survivor is not None:
                return survivor, (f'"{raw}" is account #{account.id}, '
                                  f'merged into #{survivor.id} '
                                  f'{survivor.name!r}')
            return None, (f'"{raw}" already exists as account '
                          f'#{account.id}, which somebody deactivated. '
                          f'Not recreated and not linked to — look at '
                          f'it first.')
    return None, ''


#: A lead younger than this is left in the review queue whatever else
#: is true of it. A portal notice that arrived last week may still be
#: followed by the invitation that names its buyer -- `trail_of` reads
#: the whole trail, so time is the only thing that can decide that
#: nothing more is coming.
ARCHIVE_AFTER_DAYS = 30

#: What the archived ones say. The words asked for on 14 Nov, stored
#: verbatim: a portal notice nobody could attach to a buyer is not
#: junk mail and not a lost opportunity, and filing it under either
#: would put a number in a report that means something else.
ARCHIVE_REASON = 'portal notice — no buyer'

#: Stages that mean nobody has done anything yet.
UNTOUCHED_STAGES = ('', 'New Opportunity', 'New', 'New Lead')


def work_on(lead):
    """What has been done on this lead. [] means nothing has.

    Asked of the things a person does, not of the fields the intake
    fills: a quote, an RFQ, a call, a note, a mail we sent, or a stage
    somebody moved. Any one of them means a human has handled this
    enquiry, and a sweep does not archive what somebody is working on
    -- whatever the mail it came from failed to say.
    """
    done = []
    if getattr(lead, 'quote_date', None) or \
            getattr(lead, 'quoted_amount_inr', None):
        done.append('quoted')
    if getattr(lead, 'rfq_date', None):
        done.append('RFQ')
    if getattr(lead, 'phone_call_date', None):
        done.append('called')
    if getattr(lead, 'meeting_date', None):
        done.append('met')
    for module, table, label in (
            ('app.models.quote', 'Quote', 'a quote record'),
            ('app.models.rfq', 'RFQ', 'an RFQ record')):
        try:
            mod = __import__(module, fromlist=[table])
            model = getattr(mod, table)
            if model.query.filter(model.lead_id == lead.id).count():
                done.append(label)
        except Exception:                                 # noqa: BLE001
            pass
    try:
        from app import LeadNote
        if LeadNote.query.filter(LeadNote.lead_id == lead.id).count():
            done.append('a note')
    except Exception:                                     # noqa: BLE001
        pass
    try:
        from app import LeadEmail
        sent = (LeadEmail.query
                .filter(LeadEmail.lead_id == lead.id,
                        LeadEmail.direction != 'inbound').count())
        if sent:
            done.append('mail sent')
    except Exception:                                     # noqa: BLE001
        pass
    stage = (getattr(lead, 'stage', '') or '').strip()
    if stage not in UNTOUCHED_STAGES:
        done.append(f'stage {stage}')
    return done


def age_days(lead, now=None):
    now = now or datetime.utcnow()
    when = (getattr(lead, 'original_email_received_at', None)
            or getattr(lead, 'created_at', None))
    if not when:
        return None
    try:
        return (now - when).days
    except Exception:                                     # noqa: BLE001
        return None


def plan(found, *, now=None):
    """What each lead would do. A list of dicts, one per lead.

    Four buckets, and the split of the last one is 14 Nov's second
    review: "to review" was collecting both the leads somebody is
    working on and the portal notices nobody can ever name a buyer
    for. The first belong in the account-check queue. The second are
    noise in it, and a queue with noise in it stops being read.
    """
    out = []
    for lead, what in found:
        buyer, how = buyer_of(lead)
        done = work_on(lead)
        age = age_days(lead, now)
        row = {'lead': lead, 'now': what, 'buyer': buyer, 'how': how,
               'account': None, 'alias': '', 'work': done, 'age': age,
               'portal': portal_of(lead), 'verdict': 'to review',
               'why': ''}
        if buyer:
            account, alias = account_for(buyer)
            if account is None:
                # Before creating anything: does this name already
                # exist on a row we skipped -- merged away, or
                # deactivated? Creating a second row for a customer
                # who has one is the fault this whole script exists
                # to undo, and doing it twice would be worse than
                # leaving the lead where it is.
                account, alias = namesake_elsewhere(buyer)
                if account is None and alias:
                    row.update({'verdict': 'held', 'why': alias})
                    out.append(row)
                    continue
            if account is not None:
                row.update({'verdict': 'link', 'account': account,
                            'alias': alias})
            else:
                row['verdict'] = 'create'
            out.append(row)
            continue
        if done:
            row['why'] = 'worked on: ' + ', '.join(done)
        elif age is None:
            row['why'] = 'no date on the lead, so its age is unknown'
        elif age <= ARCHIVE_AFTER_DAYS:
            row['why'] = (f'{age} day(s) old — the invitation that '
                          f'names the buyer may still arrive')
        else:
            row.update({'verdict': 'archive',
                        'why': f'{age} day(s) old, nothing done on it'})
        out.append(row)
    return out


def portal_of(lead):
    """Which portal this lead came in through."""
    from app.services import portal_rfq

    found = portal_rfq.is_portal(lead.original_email_from or '')
    if found:
        return found
    platform = (lead.lead_platform or '').strip().lower()
    return platform or 'not stated'


def report(planned, portal_accounts):
    from collections import defaultdict

    print(f'\n══ {len(planned)} lead(s) filed under a procurement '
          f'portal ══')
    if not planned:
        print('  none. No lead carries a portal as its customer.')
        return

    buckets = defaultdict(list)
    for row in planned:
        buckets[row['verdict']].append(row)

    print('\n  ── by bucket ──')
    new_names = {row['buyer'].strip().lower()
                 for row in buckets['create'] if row['buyer']}
    for verdict in ('link', 'create', 'held', 'to review', 'archive'):
        rows = buckets.get(verdict) or []
        if not rows:
            continue
        label = verdict
        if verdict == 'held':
            label = 'held — a row with that name already exists'
        if verdict == 'create':
            label = (f'create — {len(new_names)} new account(s) for '
                     f'{len(rows)} lead(s)')
        print(f'  {len(rows):>5}  {label}')

    print('\n  ── by bucket and portal ──')
    pairs = Counter((row['verdict'], row['portal']) for row in planned)
    for (verdict, portal), n in sorted(pairs.items()):
        print(f'  {n:>5}  {verdict:<11} {portal}')

    for verdict, heading in (
            ('link', 'onto an account that already exists'),
            ('create', 'onto a new account for the buyer'),
            ('held', 'nothing written: the buyer\'s name is already on '
                     'an account somebody deactivated'),
            ('to review', 'to "To review" — somebody is working these, '
                          'or they are too young to give up on'),
            ('archive', f'archived, "{ARCHIVE_REASON}" — nothing done '
                        f'on them and no buyer anywhere on the trail')):
        rows = buckets.get(verdict) or []
        if not rows:
            continue
        print(f'\n  ══ {len(rows)} {verdict.upper()}: {heading}')
        for row in rows:
            lead = row['lead']
            if verdict == 'link':
                becomes = f"#{row['account'].id} {row['account'].name}"
            elif verdict == 'create':
                becomes = f'NEW "{row["buyer"]}"'
            elif verdict == 'held':
                becomes = 'held for a person'
            elif verdict == 'archive':
                becomes = 'archived'
            else:
                becomes = '"To review", no account'
            age = row['age'] if row['age'] is not None else '?'
            print(f'  {lead.id:>7}  {row["portal"][:16]:<16} '
                  f'{becomes[:34]:<34} {age}d')
            print(f'  {"":>7}  {(lead.original_email_subject or "")[:64]!r}')
            if row['how']:
                print(f'  {"":>7}  buyer from {row["how"]}')
            if row['alias']:
                print(f'  {"":>7}  !! {row["alias"]} — --apply writes '
                      f'the buyer\'s spelling onto that account as an '
                      f'alias, so the next RFQ matches on the name')
            if row['why']:
                print(f'  {"":>7}  {row["why"]}')
            if lead.portal_key:
                print(f'  {"":>7}  tender {lead.portal_key}')

    if portal_accounts:
        print('\n  the portal account row(s), left exactly as they are:')
        for account in portal_accounts.values():
            from app import Lead
            held = Lead.query.filter(Lead.company_id == account.id).count()
            print(f'    #{account.id} {account.name!r} — {held} lead(s) '
                  f'on it now')
        print('    Nothing is deleted or merged here. Once the leads '
              'have moved, an empty one can be archived from '
              'Accounts → Merge if somebody wants it gone; the '
              'matcher already refuses to link to it.')


def write_csv(planned, path):
    buf = io.StringIO()
    out = csv.writer(buf)
    out.writerow(['lead_id', 'portal', 'now', 'verdict', 'buyer',
                  'account_id', 'account', 'read_from', 'alias_note',
                  'work', 'age_days', 'tender', 'subject'])
    for row in planned:
        lead = row['lead']
        account = row['account']
        out.writerow([
            lead.id, row['portal'], row['now'], row['verdict'],
            row['buyer'], account.id if account is not None else '',
            account.name if account is not None else '',
            row['how'], row['alias'], '; '.join(row['work']),
            row['age'] if row['age'] is not None else '',
            lead.portal_key or '',
            (lead.original_email_subject or '')[:120]])
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', newline='') as fh:
        fh.write(buf.getvalue())
    print(f'\nCSV: {path}')


# ── writing ─────────────────────────────────────────────────────────
def next_batch_id(now=None):
    from app.models.audit import AuditEvent

    now = now or datetime.utcnow()
    prefix = f'{BATCH_PREFIX}-{now.strftime("%Y%m%d")}-'
    try:
        rows = (AuditEvent.query.filter(AuditEvent.action == ACTION)
                .order_by(AuditEvent.id.desc()).limit(500).all())
    except Exception:                                     # noqa: BLE001
        rows = []
    used = set()
    for row in rows:
        payload = row.new_value if isinstance(row.new_value, dict) else {}
        batch = str(payload.get('batch') or '')
        if batch.startswith(prefix):
            used.add(batch)
    return f'{prefix}{len(used) + 1}'


def _create_account(buyer, lead, actor):
    """A new account for a buyer nobody has on file.

    Name and vertical. **No email domain**: the sender is the
    portal's robot, and writing that domain here would hand this
    account the next buyer the portal sends for.
    """
    from app import Company
    from app.services import audit

    account = Company(name=buyer[:200], is_active=True)
    for column, value in (('vertical', lead.procam_vertical or None),
                          ('created_by', actor)):
        if hasattr(account, column) and value:
            setattr(account, column, value)
    db.session.add(account)
    db.session.flush()
    audit.record('account.created', 'company', account.id,
                 new={'name': account.name, 'from_lead': lead.id,
                      'read_from': 'the body of a portal RFQ'},
                 actor=actor,
                 reason='CRM-UAT-01 item 1: the buyer of a portal RFQ, '
                        'which had been filed under the portal',
                 commit=False)
    return account


def _add_alias(account, name, *, actor, batch):
    """Record the buyer's spelling on the account it matched.

    So the next RFQ from that buyer matches on the name rather than on
    an acronym, and a person reading the account can see the name its
    mail arrives under. Nothing is renamed: `aliases` is a list, and
    the account keeps the name somebody gave it.
    """
    from app.services import audit, company_match

    text = (name or '').strip()
    if not text:
        return False
    held = list(getattr(account, 'aliases', None) or [])
    want = company_match.norm(text)
    if company_match.norm(account.name or '') == want:
        return False
    for alias in held:
        if company_match.norm(str(alias)) == want:
            return False
    account.aliases = held + [text[:200]]
    audit.record('account.alias_added', 'company', account.id,
                 old={'aliases': held},
                 new={'aliases': account.aliases, 'batch': batch},
                 actor=actor,
                 reason=(f'batch {batch}: the name this buyer\'s RFQs '
                         f'arrive under, so the next one matches on '
                         f'the name'),
                 commit=False)
    return True


def add_aliases(pairs, *, actor):
    """`652=ArcelorMittal Nippon Steel` → an alias on account #652.

    Its own step rather than part of the sweep: "these two names are
    one customer" is a statement about the master, it outlives this
    backlog, and it is the one reading that beats every heuristic
    below. Audited, and `--undo` is not needed for it -- an alias is
    added, never replaced, and the audit row holds what was there.
    """
    from app import Company
    from app.services import audit

    batch = next_batch_id()
    done = 0
    for pair in pairs:
        text = str(pair or '')
        if '=' not in text:
            print(f'  {text!r} is not ID=NAME')
            return 0
        head, _, name = text.partition('=')
        try:
            account_id = int(head.strip())
        except ValueError:
            print(f'  {head!r} is not an account id')
            return 0
        account = db.session.get(Company, account_id)
        if account is None:
            print(f'  there is no account #{account_id}')
            return 0
        if _add_alias(account, name.strip(), actor=actor, batch=batch):
            print(f'  #{account_id} {account.name!r} also answers to '
                  f'{name.strip()!r}')
            done += 1
        else:
            print(f'  #{account_id} {account.name!r} already answers '
                  f'to {name.strip()!r}')
    db.session.commit()
    return done or len(pairs)


def apply(planned, *, batch, actor, only_link=False):
    """Write the plan. `only_link` does the link and create rows only.

    Asked for on 14 Nov, and the right order to work in: the links
    and the new accounts are the part somebody has just read and
    agreed with, and they can go in while the review-or-archive split
    is still being argued about. Running it again later with no flag
    does the rest, and the rows already done are no longer in the
    plan.
    """
    from app.services import audit, company_match, lead_account

    done = Counter()
    made = {}
    archived_ids = []
    try:
        for row in planned:
            lead = row['lead']
            verdict = row['verdict']
            if verdict == 'held':
                # Named, but the name is on a row somebody put aside.
                # Nothing is written -- not a link, not a new account,
                # not even "To review": the lead stays exactly as it
                # is until a person has looked at that row.
                print(f'  #{lead.id}: held — {row["why"]}')
                done['held for a person'] += 1
                continue
            if only_link and verdict not in ('link', 'create'):
                done['left for the second pass'] += 1
                continue
            was = {'company_id': lead.company_id,
                   'company': lead.company}

            if verdict == 'create':
                key = company_match.norm(row['buyer'] or '')
                account = made.get(key)
                if account is None:
                    account = _create_account(row['buyer'], lead, actor)
                    made[key] = account
                    done['account created'] += 1
                else:
                    done['linked to an account made in this batch'] += 1
                verdict, row['account'] = 'link', account

            if verdict == 'link':
                account = row['account']
                if row['alias'] and _add_alias(
                        account, row['buyer'], actor=actor, batch=batch):
                    done['alias recorded'] += 1
                # `learn=False`: the sender is `no-reply@procol.in`,
                # and learning it as this buyer's address would send
                # every later portal RFQ -- for any buyer -- here.
                ok, detail = lead_account.link(
                    lead, account, actor=actor, learn=False,
                    reason=(f'CRM-UAT-01 item 1, batch {batch}: the '
                            f'buyer read from the mail, which had '
                            f'been filed under the portal'))
                if not ok:
                    print(f'  #{lead.id}: refused — {detail}')
                    done['refused'] += 1
                    continue
                done['linked'] += 1

            elif verdict == 'archive':
                # Through the archive service, which asks whether this
                # actor may archive this lead. A sweep that wrote
                # `is_archived` itself would be a way round a
                # permission, and the permission is the reason the
                # column is trustworthy.
                from app.services import lead_archive
                lead.company_id = None
                lead.company = TO_REVIEW
                try:
                    lead_archive.archive(lead, actor=actor,
                                         reason=ARCHIVE_REASON)
                except lead_archive.Refused as exc:
                    print(f'  #{lead.id}: not archived — {exc}')
                    done['archive refused'] += 1
                    continue
                archived_ids.append(lead.id)
                done['archived'] += 1

            else:
                lead.company_id = None
                lead.company = TO_REVIEW
                if hasattr(lead, 'needs_review'):
                    lead.needs_review = True
                done['to review'] += 1

            lead.updated_at = datetime.utcnow()
            audit.record(ACTION, 'lead', lead.id,
                         old=was,
                         new={'company_id': lead.company_id,
                              'company': lead.company,
                              'verdict': verdict,
                              'archived': lead.id in archived_ids,
                              'buyer_read_from': row['how'] or '',
                              'batch': batch},
                         actor=actor,
                         reason=(f'batch {batch}: a procurement portal '
                                 f'is not a customer'),
                         commit=False)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    print()
    for what, n in done.most_common():
        print(f'  {n:>5}  {what}')
    print(f'\nBatch {batch}. Undo: --undo --batch {batch}')
    if only_link:
        print('Only the link and create rows were written. Run it '
              'again with no --only-link for the rest.')
    return done


def undo(batch):
    from app import Company, Lead
    from app.models.audit import AuditEvent
    from app.services import audit

    rows = (AuditEvent.query
            .filter(AuditEvent.action == ACTION)
            .order_by(AuditEvent.id.asc()).all())
    mine = []
    for row in rows:
        payload = row.new_value if isinstance(row.new_value, dict) else {}
        if str(payload.get('batch') or '') == batch:
            mine.append((row, payload))
    if not mine:
        print(f'\nno batch {batch} on file.')
        return 0

    back, left, touched, restored = 0, 0, set(), 0
    for row, payload in mine:
        lead = db.session.get(Lead, int(row.entity_id))
        if lead is None:
            continue
        # An archive is undone first, through the service, so the
        # restore is as audited as the archive was. The actor is the
        # person who ran the apply: they had the permission then, and
        # an undo must not be a way of acting without one.
        if payload.get('archived'):
            from app.services import lead_archive
            if lead_archive.is_archived(lead):
                try:
                    lead_archive.restore(lead, actor=row.actor or ACTOR)
                    restored += 1
                except lead_archive.Refused as exc:
                    print(f'  #{lead.id}: still archived — {exc}')
                    left += 1
                    continue
        if (lead.company_id or 0) != (payload.get('company_id') or 0):
            left += 1
            print(f'  #{lead.id}: its account has changed since the '
                  f'batch — left alone')
            continue
        old = row.old_value if isinstance(row.old_value, dict) else {}
        if payload.get('company_id'):
            touched.add(int(payload['company_id']))
        lead.company_id = old.get('company_id')
        lead.company = old.get('company')
        audit.record(UNDONE, 'lead', lead.id,
                     old={'company_id': payload.get('company_id')},
                     new={'company_id': lead.company_id,
                          'company': lead.company, 'batch': batch},
                     actor=ACTOR, reason=f'undo of batch {batch}',
                     commit=False)
        back += 1

    # Accounts this batch created, deactivated rather than deleted --
    # and only where nothing is left on them.
    made = 0
    for row in (AuditEvent.query
                .filter(AuditEvent.action == 'account.created')
                .order_by(AuditEvent.id.asc()).all()):
        payload = row.new_value if isinstance(row.new_value, dict) else {}
        if payload.get('read_from') != 'the body of a portal RFQ':
            continue
        try:
            account_id = int(row.entity_id)
        except (TypeError, ValueError):
            continue
        if account_id not in touched:
            continue
        held = Lead.query.filter(Lead.company_id == account_id).count()
        account = db.session.get(Company, account_id)
        if account is None:
            continue
        if held:
            print(f'  account #{account_id} {account.name!r} still has '
                  f'{held} lead(s) — left active')
            continue
        account.is_active = False
        audit.record('account.deactivated', 'company', account_id,
                     new={'batch': batch}, actor=ACTOR,
                     reason=(f'undo of batch {batch}: created for a '
                             f'portal RFQ buyer and now empty. '
                             f'Deactivated, never deleted.'),
                     commit=False)
        made += 1
    # And the aliases this batch wrote, back to what they were.
    aliases = 0
    for row in (AuditEvent.query
                .filter(AuditEvent.action == 'account.alias_added')
                .order_by(AuditEvent.id.asc()).all()):
        payload = row.new_value if isinstance(row.new_value, dict) else {}
        if str(payload.get('batch') or '') != batch:
            continue
        account = db.session.get(Company, int(row.entity_id))
        if account is None:
            continue
        now_held = list(getattr(account, 'aliases', None) or [])
        if now_held != list(payload.get('aliases') or []):
            print(f'  account #{account.id}: its aliases have changed '
                  f'since the batch — left alone')
            continue
        account.aliases = list((row.old_value or {}).get('aliases') or [])
        audit.record('account.alias_removed', 'company', account.id,
                     old={'aliases': now_held},
                     new={'aliases': account.aliases, 'batch': batch},
                     actor=ACTOR, reason=f'undo of batch {batch}',
                     commit=False)
        aliases += 1

    db.session.commit()
    print(f'\n{back} lead(s) put back; {restored} un-archived; '
          f'{left} left alone; {aliases} alias(es) removed; '
          f'{made} account(s) this batch created deactivated.')
    return back


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--check', action='store_true',
                    help='report only. The default.')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--undo', action='store_true')
    ap.add_argument('--batch', default=None, metavar='ID')
    ap.add_argument('--actor', default=None, metavar='EMP_CODE',
                    help='who is running it — required for --apply')
    ap.add_argument('--add-alias', action='append', default=None,
                    dest='add_alias', metavar='ID=NAME',
                    help='name an account answers to as well, e.g. '
                         '--add-alias 652="ArcelorMittal Nippon '
                         'Steel". Repeatable, audited, and the surest '
                         'way to say "these are the same customer".')
    ap.add_argument('--only-link', action='store_true',
                    dest='only_link',
                    help='apply the link and create rows only, and '
                         'leave the review-or-archive split for a '
                         'second pass')
    ap.add_argument('--csv', nargs='?', const=DEFAULT_CSV, default=None,
                    metavar='PATH')
    args = ap.parse_args(argv)

    with app.app_context():
        print(f'database: {db.engine.url}')
        print('CRM-UAT-01 item 1 — a procurement portal is not a '
              'customer.'
              + ('' if (args.apply or args.undo) else ' REPORT ONLY.'))
        _rule()

        if args.undo:
            if not args.batch:
                print('\n--undo needs --batch PACC-YYYYMMDD-n')
                return 2
            undo(args.batch)
            return 0

        if args.add_alias:
            # Before the sweep, deliberately: an alias is the surest
            # statement that two names are one customer, and every
            # reading below is weaker than somebody saying so.
            if not args.actor:
                print('\n--add-alias needs --actor <EMP_CODE>.')
                return 2
            if not args.apply:
                for pair in args.add_alias:
                    print(f'  would add: {pair}')
                print('\nDRY RUN — no alias written. Add --apply.')
                db.session.rollback()
                return 0
            written = add_aliases(args.add_alias,
                                  actor=args.actor.strip().upper())
            if not written:
                return 2
            print()
            _rule()

        found, portal_accounts = leads_on_a_portal()
        planned = plan(found)
        report(planned, portal_accounts)
        if args.csv:
            write_csv(planned, args.csv)

        if not args.apply:
            linkable = sum(1 for row in planned
                           if row['verdict'] in ('link', 'create'))
            print(f'\nDRY RUN — nothing written. --apply --actor '
                  f'<EMP_CODE> moves the {len(planned)} lead(s) above; '
                  f'--apply --only-link --actor <EMP_CODE> does the '
                  f'{linkable} link/create row(s) and leaves the rest.')
            db.session.rollback()
            return 0
        if not args.actor:
            print('\n--apply needs --actor <EMP_CODE>: an audit row '
                  'with nobody on it is half a record.')
            return 2
        apply(planned, batch=next_batch_id(),
              actor=args.actor.strip().upper(),
              only_link=args.only_link)
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
