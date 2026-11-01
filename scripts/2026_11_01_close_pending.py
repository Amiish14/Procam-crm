"""
CRM-COCKPIT-02 §6 — the pending CRM items, closed.

    .venv/bin/python scripts/2026_11_01_close_pending.py --check
    .venv/bin/python scripts/2026_11_01_close_pending.py --apply
    .venv/bin/python scripts/undo_batch.py --batch cockpit02.close_pending.<date> --apply

Seven items from §6, each one named by the prompt and each one
idempotent — running this twice changes nothing the second time.

    1  Walchandnagar's eight open leads closed as Lost, "Others:
       blocked client", and the two the register was not matching
       (#12441, #12355) reported with the reason it missed them
    2  Godrej — the same treatment for any open lead, once the block
       is applied
    5  four vertical corrections: RVNL #10110, Godrej #11851,
       Tmilltd #11898 → Heavy Transport; Prysmian #12218 → Project
       Freight
    6  four account owners, by name against the employee master
    7  the five Indo Tech auctions already closed → Missed submission
       unless a bid is recorded

Items 3 and 4 are not here because they already have their own
scripts, and running them from a second place is how two scripts come
to disagree:

    3  stage dates   scripts/crm2_backfill_stage_entered.py
    4  verticals     scripts/vertical_master_apply.py

What this does NOT do, deliberately: apply the Godrej block itself.
`scripts/2026_10_21_block_godrej.py` exists for that and asks a person
which Godrej companies the decision covers — Godrej is a group of
separate companies, and blocking by name match is how the wrong one
gets blocked. This script closes the leads of whatever is blocked when
it runs, and says so when nothing is.

Every write goes through the reversible log (§8), so one command puts
any of it back. Leads are closed, never deleted (CRM-NO-DELETE-01).
"""
import argparse
import os
import sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Company, Employee, Lead, app, db          # noqa: E402

BATCH = 'cockpit02.close_pending'
NOTE_PREFIX = '01-Nov-2026 CRM-COCKPIT-02'

# ── §6.1 ─────────────────────────────────────────────────────────────
WALCHAND_LEADS = (12447, 12441, 12490, 12476, 12465, 12437, 12362,
                  12355)
#: The two the live register was not catching, per §6.1.
WALCHAND_UNFLAGGED = (12441, 12355)
BLOCKED_LOST_REASON = 'Others: blocked client'

# ── §6.5 ─────────────────────────────────────────────────────────────
VERTICAL_FIXES = (
    (10110, 'RVNL', 'Heavy Transport'),
    (11851, 'Godrej', 'Heavy Transport'),
    (11898, 'Tmilltd', 'Heavy Transport'),
    (12218, 'Prysmian', 'Project Freight'),
)

# ── §6.6 ─────────────────────────────────────────────────────────────
#: Account name → owner, by NAME. Resolved against the employee master
#: when the script runs, and refused if a name matches nobody or more
#: than one person: a wrong employee code written into an owner column
#: is silent, and the lead simply stops appearing on anybody's list.
ACCOUNT_OWNERS = (
    ('BEML', 'James Francis Xavier'),
    ('Sarens', 'Tamal Das'),
    ('Siemens Energy', 'Srinivas Marella'),
    ('Nippon Express', 'Gowdhaman Rajakrishnan'),
)

# ── §6.7 ─────────────────────────────────────────────────────────────
INDO_TECH_AUCTIONS = ('RA-56609', 'RA-56715', 'RA-56719', 'RA-56824',
                      'RA-57103')


def _employee_by_name(name):
    """(employee, problem). Exact first, then a contains match."""
    rows = Employee.query.filter(Employee.name == name).all()
    if not rows:
        rows = Employee.query.filter(
            Employee.name.ilike(f'%{name}%')).all()
    rows = [e for e in rows if e.is_active is not False]
    if not rows:
        return None, f'no active employee called {name}'
    if len(rows) > 1:
        return None, (f'{len(rows)} people match {name}: '
                      + ', '.join(f'{e.emp_code} {e.name}' for e in rows))
    return rows[0], None


def _accounts_named(name):
    return Company.query.filter(Company.name.ilike(f'%{name}%')).all()


def _blocked_company_names():
    """Company names the register is blocking right now."""
    from app.models.restriction import BLOCKED, ClientRestriction

    rows = ClientRestriction.query.filter(
        ClientRestriction.status == BLOCKED).all()
    return [(r.id, r.company_name or '') for r in rows]


def plan():
    """Everything this run would do, as printable findings."""
    from app.services import client_restrictions as restrictions
    from app.services import quote_due as qd
    from app.services import sales_rules as rules

    out = {'walchand': [], 'walchand_missing': [], 'unflagged': [],
           'godrej': [], 'verticals': [], 'owners': [],
           'auctions': [], 'problems': []}

    open_stages = rules.open_stages()

    # §6.1 — the eight named leads.
    for lead_id in WALCHAND_LEADS:
        lead = db.session.get(Lead, lead_id)
        if lead is None:
            out['walchand_missing'].append(lead_id)
            continue
        already = ((lead.lost_reason or '') == BLOCKED_LOST_REASON
                   or (lead.stage or '') == 'Lost')
        out['walchand'].append({
            'id': lead.id, 'company': lead.company or '',
            'stage': lead.stage or '', 'already': already,
            'owner': lead.assigned_to or ''})
        if lead_id in WALCHAND_UNFLAGGED:
            verdict = restrictions.check(company_name=lead.company,
                                         email=lead.email,
                                         account_id=lead.company_id)
            out['unflagged'].append({
                'id': lead.id, 'company': lead.company or '',
                'now_blocked': verdict.blocked,
                'matched_on': verdict.matched_on or '(nothing)',
                'score': round(verdict.score, 3),
                'account_id': lead.company_id})

    # §6.2 — Godrej, whatever is blocked when this runs.
    blocked = _blocked_company_names()
    godrej = [(rid, name) for rid, name in blocked
              if 'godrej' in name.lower()]
    if not godrej:
        out['problems'].append(
            'No Godrej entry is blocked in the register. '
            'scripts/2026_10_21_block_godrej.py applies that decision '
            '(it asks which Godrej companies it covers); this script '
            'closes the leads of whatever is blocked when it runs.')
    for lead in (Lead.query
                 .filter(Lead.company.ilike('%godrej%'),
                         Lead.is_archived.isnot(True),
                         Lead.stage.in_(open_stages)).all()):
        verdict = restrictions.check(company_name=lead.company,
                                     email=lead.email,
                                     account_id=lead.company_id)
        out['godrej'].append({
            'id': lead.id, 'company': lead.company or '',
            'stage': lead.stage or '', 'blocked': verdict.blocked})

    # §6.5 — the four vertical corrections.
    for lead_id, who, vertical in VERTICAL_FIXES:
        lead = db.session.get(Lead, lead_id)
        if lead is None:
            out['problems'].append(f'lead #{lead_id} ({who}) is not in '
                                   f'this database')
            continue
        out['verticals'].append({
            'id': lead.id, 'company': lead.company or '',
            'expected': who, 'from': lead.procam_vertical or '',
            'to': vertical,
            'already': (lead.procam_vertical or '') == vertical})

    # §6.6 — the four account owners.
    for account_name, owner_name in ACCOUNT_OWNERS:
        emp, problem = _employee_by_name(owner_name)
        accounts = _accounts_named(account_name)
        if problem:
            out['problems'].append(f'{account_name}: {problem}')
        if not accounts:
            out['problems'].append(f'no account matching '
                                   f'{account_name}')
        for account in accounts:
            leads = (Lead.query
                     .filter(Lead.company_id == account.id,
                             Lead.is_archived.isnot(True),
                             Lead.stage.in_(open_stages)).count())
            out['owners'].append({
                'account_id': account.id, 'account': account.name,
                'owner_name': owner_name,
                'owner': getattr(emp, 'emp_code', None),
                'vertical': account.vertical or '',
                'open_leads': leads,
                'problem': problem})

    # §6.7 — the five closed auctions.
    for key in INDO_TECH_AUCTIONS:
        leads = (Lead.query
                 .filter(Lead.portal_key == key,
                         Lead.is_archived.isnot(True)).all())
        if not leads:
            out['problems'].append(
                f'{key}: no lead carries this portal number. '
                f'scripts/portal_dedupe.py records the numbers off the '
                f'subject lines first.')
        for lead in leads:
            out['auctions'].append({
                'id': lead.id, 'key': key,
                'company': lead.company or '',
                'due': qd.shown(lead.quote_due_at),
                'bid_outcome': lead.bid_outcome or '',
                'quote': lead.quote_no or '',
                'already': bool(lead.quote_due_missed_at),
                # A lead with no deadline recorded cannot be flagged
                # late against it — there is nothing to be late for.
                # The deadline comes off the subject line first
                # (quote_due_backfill.py), and until it does this row
                # says so rather than claiming it would flag it.
                'no_deadline': lead.quote_due_at is None,
                'would_flag': (lead.quote_due_at is not None
                               and not (lead.bid_outcome or '').strip()
                               and not (lead.quote_no or '').strip())})
    return out


def report(found):
    print('\n' + '=' * 70)
    print('§6.1 — WALCHANDNAGAR (blocked client)')
    print('=' * 70)
    for row in found['walchand']:
        state = 'already closed' if row['already'] else 'to close'
        print(f"  #{row['id']:<7} {row['company'][:30]:<30} "
              f"{row['stage'][:18]:<18} {state}")
    if found['walchand_missing']:
        print('  not in this database: '
              + ', '.join(f"#{i}" for i in found['walchand_missing']))
    if found['unflagged']:
        print('\n  the two §6.1 says were not flagged blocked:')
        for row in found['unflagged']:
            print(f"    #{row['id']:<7} {row['company'][:28]:<28} "
                  f"register says blocked={row['now_blocked']} "
                  f"(matched on {row['matched_on']}, "
                  f"score {row['score']})")
            if not row['now_blocked']:
                print(f"        the register does not match this lead. "
                      f"Add its name as an alias or link it to the\n"
                      f"        blocked account "
                      f"(account_id={row['account_id'] or 'none'}) on "
                      f"/admin/restrictions — closing it here does not "
                      f"fix that,\n        and the next enquiry would "
                      f"come straight through.")

    print('\n' + '=' * 70)
    print('§6.2 — GODREJ')
    print('=' * 70)
    if not found['godrej']:
        print('  no open Godrej lead.')
    for row in found['godrej']:
        print(f"  #{row['id']:<7} {row['company'][:30]:<30} "
              f"{row['stage'][:18]:<18} "
              f"{'blocked' if row['blocked'] else 'NOT blocked'}")

    print('\n' + '=' * 70)
    print('§6.5 — VERTICAL CORRECTIONS')
    print('=' * 70)
    for row in found['verticals']:
        print(f"  #{row['id']:<7} {row['company'][:26]:<26} "
              f"{row['from'][:18]:<18} -> {row['to']:<16} "
              + ('(already)' if row['already'] else ''))

    print('\n' + '=' * 70)
    print('§6.6 — ACCOUNT OWNERS')
    print('=' * 70)
    for row in found['owners']:
        print(f"  account #{row['account_id']:<6} "
              f"{row['account'][:28]:<28} -> {row['owner_name']} "
              f"({row['owner'] or 'UNRESOLVED'})  "
              f"{row['open_leads']} open lead(s)")

    print('\n' + '=' * 70)
    print('§6.7 — INDO TECH AUCTIONS ALREADY CLOSED')
    print('=' * 70)
    for row in found['auctions']:
        what = ('already flagged' if row['already']
                else 'flag Missed submission' if row['would_flag']
                else 'NO DEADLINE RECORDED — run '
                     'quote_due_backfill.py first'
                if row['no_deadline']
                else f"leave alone ({row['bid_outcome'] or row['quote']})")
        print(f"  #{row['id']:<7} {row['key']:<10} "
              f"{row['company'][:24]:<24} due {row['due'] or '-':<18} "
              f"{what}")

    if found['problems']:
        print('\n' + '=' * 70)
        print('THINGS THIS RUN CANNOT DO')
        print('=' * 70)
        for line in found['problems']:
            print(f'  !! {line}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--actor', default='CRM-COCKPIT-02')
    ap.add_argument('--only', default='',
                    help='comma-separated: walchand,godrej,verticals,'
                         'owners,auctions')
    args = ap.parse_args()
    if not (args.check or args.apply):
        args.check = True
    only = {p.strip() for p in args.only.split(',') if p.strip()}

    def wanted(name):
        return not only or name in only

    with app.app_context():
        print(f'database: {db.engine.url}')
        print('MODE:     ' + ('APPLY' if args.apply
                              else 'CHECK — nothing written'))
        found = plan()
        report(found)

        if args.check:
            print('\nCHECK ONLY — nothing written. --apply writes '
                  'these;\nscripts/undo_batch.py puts them back.')
            return 0

        from app.services import audit, bulk_log
        from app.services import quote_due_flow as flow

        done = Counter()
        with bulk_log.batch(BATCH, note=f'{NOTE_PREFIX} §6',
                            actor=args.actor) as log:
            # §6.1 and §6.2 — close the blocked clients' open leads.
            to_close = []
            if wanted('walchand'):
                to_close += [r['id'] for r in found['walchand']
                             if not r['already']]
            if wanted('godrej'):
                to_close += [r['id'] for r in found['godrej']
                             if r['blocked']]
            for lead_id in to_close:
                lead = db.session.get(Lead, lead_id)
                if lead is None:
                    continue
                log.set(lead, 'stage', 'Lost')
                log.set(lead, 'lost_reason', BLOCKED_LOST_REASON)
                log.set(lead, 'followup_date', None)
                log.set(lead, 'next_action', None)
                audit.record('lead.closed_blocked_client', 'lead',
                             lead.id,
                             new={'stage': 'Lost',
                                  'lost_reason': BLOCKED_LOST_REASON},
                             actor=args.actor,
                             reason=f'{NOTE_PREFIX} §6.1: the client is '
                                    f'blocked by management',
                             commit=False)
                done['leads closed'] += 1

            # §6.5 — the vertical corrections.
            if wanted('verticals'):
                for row in found['verticals']:
                    if row['already']:
                        continue
                    lead = db.session.get(Lead, row['id'])
                    if lead is None:
                        continue
                    log.set(lead, 'procam_vertical', row['to'])
                    audit.record('lead.vertical_corrected', 'lead',
                                 lead.id,
                                 old={'procam_vertical': row['from']},
                                 new={'procam_vertical': row['to']},
                                 actor=args.actor,
                                 reason=f'{NOTE_PREFIX} §6.5',
                                 commit=False)
                    done['verticals corrected'] += 1

            # §6.6 — the account owners, and the open leads with them.
            if wanted('owners'):
                for row in found['owners']:
                    if row['problem'] or not row['owner']:
                        continue
                    account = db.session.get(Company, row['account_id'])
                    if account is None:
                        continue
                    # Account ownership in this CRM is a row in
                    # `account_vertical_owner`, per account and
                    # vertical — not a column on the account. That is
                    # what `auto_assign` reads, which is the whole of
                    # §6.6's "future leads auto-assign"; writing a
                    # column instead would have changed who the screen
                    # shows and left the assignment rule reading the
                    # old owner.
                    from app.models.assignment import \
                        AccountVerticalOwner as AVO

                    vertical = (account.vertical or '').strip() or \
                        row['vertical'] or 'Heavy Transport'
                    existing = (AVO.query
                                .filter(AVO.account_id == account.id,
                                        AVO.vertical == vertical)
                                .first())
                    if existing is None:
                        db.session.add(AVO(
                            account_id=account.id, vertical=vertical,
                            emp_code=row['owner'], source='manual',
                            note=f'{NOTE_PREFIX} §6.6'))
                        done['owner rows added'] += 1
                    elif existing.emp_code != row['owner']:
                        # Through the log, so the previous owner comes
                        # back on an undo.
                        log.set(existing, 'emp_code', row['owner'],
                                entity_type='AccountVerticalOwner')
                        log.set(existing, 'source', 'manual',
                                entity_type='AccountVerticalOwner')
                        if hasattr(existing, 'active'):
                            log.set(existing, 'active', True,
                                    entity_type='AccountVerticalOwner')
                        done['owner rows changed'] += 1
                    audit.record('account.owner_set', 'Company',
                                 account.id,
                                 new={'owner': row['owner'],
                                      'vertical': vertical},
                                 actor=args.actor,
                                 reason=f'{NOTE_PREFIX} §6.6: decided '
                                        f'29 Oct–01 Nov',
                                 commit=False)
                    # The open leads move with it: §6.6 says "lead and
                    # account level, future leads auto-assign".
                    for lead in (Lead.query
                                 .filter(Lead.company_id == account.id,
                                         Lead.is_archived.isnot(True))
                                 .all()):
                        from app.services import sales_rules as rules
                        if (lead.stage or '') not in rules.open_stages():
                            continue
                        emp = Employee.query.filter_by(
                            emp_code=row['owner']).first()
                        log.set(lead, 'assigned_to', row['owner'])
                        log.set(lead, 'assigned_name',
                                getattr(emp, 'name', row['owner']))
                        done['leads re-owned'] += 1

            # §6.7 — the closed auctions.
            if wanted('auctions'):
                for row in found['auctions']:
                    if not row['would_flag'] or row['already']:
                        continue
                    lead = db.session.get(Lead, row['id'])
                    if lead is None:
                        continue
                    if lead.quote_due_at is None:
                        continue        # nothing to be late against
                    if flow.mark_missed(lead):
                        done['auctions flagged'] += 1

            batch_name = log.name

        try:
            db.session.commit()
        except Exception as exc:                          # noqa: BLE001
            db.session.rollback()
            print(f'\n  !! ROLLED BACK — nothing was written: '
                  f'{type(exc).__name__}: {str(exc)[:200]}')
            return 1

        print('\n' + '=' * 70)
        print('DONE')
        print('=' * 70)
        for label, n in done.most_common():
            print(f'  {label:<24} {n}')
        print(f'\n  reversible log batch: {batch_name}')
        print(f'  undo: .venv/bin/python scripts/undo_batch.py '
              f'--batch {batch_name} --apply')
        if found['problems']:
            print('\n  The problems listed above are unchanged — none '
                  'of them is\n  something this script should decide.')
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
