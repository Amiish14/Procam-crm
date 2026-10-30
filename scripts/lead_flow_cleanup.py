"""
CRM-LEAD-FLOW-01 §6 — the one-time clean-up, as read-only reports.

    .venv/bin/python scripts/lead_flow_cleanup.py
    .venv/bin/python scripts/lead_flow_cleanup.py --only stages

Six reports. Each prints counts and a sample and changes nothing:

    duplicates   open leads that are one enquiry split in two
    blocked      open leads for a client somebody blocked
    stages       "New Opportunity" -> "New"; the detailed stages
                 become Contacted + a sub-status
    industry     accounts that could fill a lead's blank Industry
                 and State
    stale        open or on-hold leads untouched for four months
    owners       open leads nobody holds

Two of these already have their own script and are not repeated
here: unlinked leads are `link_unplaced_leads.py --unlinked`, and
duplicate *accounts* are `account_duplicates.py`.

Applying needs the section named, an actor, and --apply together:

    .venv/bin/python scripts/lead_flow_cleanup.py \\
        --only stages --actor DIR12010 --apply

Nothing here deletes. A lead closed by `stale` is Lost with a
reason; a duplicate is merged and its row kept, pointing at the
survivor; a stage change is a stage change. Every one writes an
audit row naming this script and the day.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

REPORTS = ('duplicates', 'blocked', 'stages', 'industry', 'stale',
           'owners')

#: §2.5 — one stage for a fresh lead.
FRESH_FROM, FRESH_TO = 'New Opportunity', 'New'

#: §3.3 — the detailed stages become the sub-status of Contacted.
CONTACTED = 'Contacted'
SUB_STATUS = {
    'Call Done': 'Call done',
    'Profile Sent': 'Profile sent',
    'Business Discussion': 'Discussion',
    'Appointment': 'Appointment',
    'Visit Done': 'Visit done',
}

STALE_DAYS = 122          # four months
STALE_REASON = 'No response'


def _open_leads():
    from app import Lead
    from app.services import sales_rules
    return (Lead.query
            .filter(Lead.is_archived.isnot(True),
                    Lead.junk_deleted.isnot(True),
                    Lead.stage.in_(sales_rules.open_stages()))
            .order_by(Lead.id.desc()).all())


# ── 1. duplicates ────────────────────────────────────────────────────
def duplicates():
    from app.services import lead_merge
    found = lead_merge.groups()
    return {'groups': found,
            'leads': sum(len(g['members']) for g in found),
            'certain': sum(1 for g in found if g['certain'])}


def _say_duplicates(data):
    out = [f'{len(data["groups"])} group(s) covering '
           f'{data["leads"]} lead(s); {data["certain"]} certain.', '']
    for group in data['groups'][:25]:
        out.append(f'  keep #{group["survivor"]}  {group["company"]}'
                   f'  ·  {", ".join(group["why"])}'
                   + ('' if group['certain'] else '  (suggestion)'))
        folds = [str(m['lead_id']) for m in group['members']
                 if not m['keep']]
        out.append(f'      merge in: {" ".join(folds)}')
    if len(data['groups']) > 25:
        out.append(f'  … and {len(data["groups"]) - 25} more groups')
    return out


# ── 2. blocked clients ───────────────────────────────────────────────
def blocked():
    from app.services import client_restrictions as cr
    rows = []
    for lead in _open_leads():
        verdict = cr.check(company_name=lead.company,
                           email=lead.email,
                           account_id=lead.company_id)
        if getattr(verdict, 'blocked', False):
            rows.append({'lead_id': lead.id,
                         'company': lead.company or '',
                         'stage': lead.stage or '',
                         'value': getattr(lead, 'opportunity_value', None),
                         'owner': lead.assigned_to or '',
                         'why': getattr(verdict, 'company_name', '')
                         or 'on the block register'})
    return {'rows': rows}


def _say_blocked(data):
    rows = data['rows']
    out = [f'{len(rows)} open lead(s) for a blocked client.', '']
    for row in rows[:40]:
        value = row['value']
        money = f'  ·  ₹{value:,.0f}' if value else ''
        out.append(f'  #{row["lead_id"]}  {row["company"]}  ·  '
                   f'{row["stage"]}{money}')
        out.append(f'      blocked as: {row["why"]}')
    if rows:
        out.append('')
        out.append('  These are closed as Lost "Client blocked", '
                   'never deleted. Read the list first — one of '
                   'them is large.')
    return out


# ── 3. stages ────────────────────────────────────────────────────────
def stages():
    from app import Lead
    fresh = Lead.query.filter(Lead.stage == FRESH_FROM).count()
    detailed = Counter()
    for old in SUB_STATUS:
        n = Lead.query.filter(Lead.stage == old).count()
        if n:
            detailed[old] = n
    return {'fresh': fresh, 'detailed': detailed,
            'total': fresh + sum(detailed.values())}


def _say_stages(data):
    out = [f'{data["total"]} lead(s) would move.', '']
    out.append(f'  "{FRESH_FROM}" -> "{FRESH_TO}": {data["fresh"]}')
    for old, n in data['detailed'].most_common():
        out.append(f'  "{old}" -> "{CONTACTED}" '
                   f'(sub-status "{SUB_STATUS[old]}"): {n}')
    if not data['total']:
        out.append('  Nothing to move.')
    return out


# ── 4. industry and state ────────────────────────────────────────────
def industry():
    from app import Company, db
    rows = []
    for lead in _open_leads():
        if not lead.company_id:
            continue
        missing = [f for f in ('industry', 'state')
                   if not (getattr(lead, f, '') or '').strip()]
        if not missing:
            continue
        account = db.session.get(Company, lead.company_id)
        if account is None:
            continue
        fill = {}
        for field in missing:
            value = (getattr(account, field, '') or '').strip()
            if value:
                fill[field] = value
        if fill:
            rows.append({'lead_id': lead.id,
                         'company': lead.company or '', 'fill': fill})
    return {'rows': rows}


def _say_industry(data):
    rows = data['rows']
    out = [f'{len(rows)} lead(s) could be filled from their '
           f'account.', '']
    for row in rows[:25]:
        bits = ', '.join(f'{k}={v}' for k, v in row['fill'].items())
        out.append(f'  #{row["lead_id"]}  {row["company"]}  ·  {bits}')
    if len(rows) > 25:
        out.append(f'  … and {len(rows) - 25} more')
    out.append('')
    out.append('  Industry is left blank where the account has none: '
               'the owner picks it from a dropdown rather than the '
               'CRM guessing.')
    return out


# ── 5. stale ─────────────────────────────────────────────────────────
def stale():
    from app import Lead
    cutoff = datetime.utcnow() - timedelta(days=STALE_DAYS)
    rows = []
    for lead in _open_leads():
        touched = lead.updated_at or lead.created_at
        made = lead.created_at
        if not touched or not made:
            continue
        if touched < cutoff and made < cutoff:
            rows.append({'lead_id': lead.id,
                         'company': lead.company or '',
                         'stage': lead.stage or '',
                         'owner': lead.assigned_to or '',
                         'last': str(touched)[:10]})
    return {'rows': rows}


def _say_stale(data):
    rows = data['rows']
    out = [f'{len(rows)} lead(s) untouched for {STALE_DAYS} days '
           f'on a lead at least that old.', '']
    for row in rows[:25]:
        out.append(f'  #{row["lead_id"]}  {row["company"]}  ·  '
                   f'{row["stage"]}  ·  last touched {row["last"]}')
    if len(rows) > 25:
        out.append(f'  … and {len(rows) - 25} more')
    out.append('')
    out.append(f'  These close as Lost "{STALE_REASON}". Never '
               f'deleted, and the figure they leave is honest: '
               f'they were not being worked.')
    return out


# ── 6. owners ────────────────────────────────────────────────────────
def owners():
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'unowned_open_leads.py')
    spec = importlib.util.spec_from_file_location('uol_for_flow', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rows = mod.unowned()
    by_verdict, _ = mod.counts(rows)
    return {'rows': rows, 'by_verdict': by_verdict, 'module': mod}


def _say_owners(data):
    out = [f'{len(data["rows"])} open lead(s) with no owner.', '']
    for name, n in data['by_verdict'].items():
        out.append(f'  {name:<16} {n:>4}')
    out.append('')
    out.append('  The detail, and the apply path, are in '
               'scripts/unowned_open_leads.py.')
    return out


READERS = {'duplicates': (duplicates, _say_duplicates),
           'blocked': (blocked, _say_blocked),
           'stages': (stages, _say_stages),
           'industry': (industry, _say_industry),
           'stale': (stale, _say_stale),
           'owners': (owners, _say_owners)}


# ── the writes ───────────────────────────────────────────────────────
def apply_stages(*, actor):
    """"New Opportunity" -> "New"; detailed stages -> Contacted."""
    from app import Lead, db
    from app.services import audit

    moved = Counter()
    for lead in Lead.query.filter(Lead.stage == FRESH_FROM).all():
        lead.stage = FRESH_TO
        moved[FRESH_FROM] += 1
        audit.record('lead.stage_renamed', 'lead', lead.id,
                     old={'stage': FRESH_FROM}, new={'stage': FRESH_TO},
                     actor=actor,
                     reason='CRM-LEAD-FLOW-01: one stage for a fresh '
                            'lead')
    for old, sub in SUB_STATUS.items():
        for lead in Lead.query.filter(Lead.stage == old).all():
            lead.stage = CONTACTED
            lead.sub_status = sub
            moved[old] += 1
            audit.record('lead.stage_renamed', 'lead', lead.id,
                         old={'stage': old},
                         new={'stage': CONTACTED, 'sub_status': sub},
                         actor=actor,
                         reason='CRM-LEAD-FLOW-01: the detail becomes '
                                'a sub-status')
    db.session.commit()
    return dict(moved)


def apply_industry(*, actor):
    from app import Company, db
    from app.services import audit

    filled = 0
    for row in industry()['rows']:
        from app import Lead
        lead = db.session.get(Lead, row['lead_id'])
        if lead is None:
            continue
        for field, value in row['fill'].items():
            setattr(lead, field, value)
        audit.record('lead.filled_from_account', 'lead', lead.id,
                     new=row['fill'], actor=actor,
                     reason='CRM-LEAD-FLOW-01: taken from the account '
                            'master')
        filled += 1
    db.session.commit()
    return filled


def apply_stale(*, actor):
    from app import Lead, db
    from app.services import audit

    closed = 0
    for row in stale()['rows']:
        lead = db.session.get(Lead, row['lead_id'])
        if lead is None:
            continue
        was = lead.stage
        lead.stage = 'Lost'
        lead.lost_reason = STALE_REASON
        audit.record('lead.closed_stale', 'lead', lead.id,
                     old={'stage': was},
                     new={'stage': 'Lost',
                          'lost_reason': STALE_REASON},
                     actor=actor,
                     reason=f'CRM-LEAD-FLOW-01: untouched for '
                            f'{STALE_DAYS} days')
        closed += 1
    db.session.commit()
    return closed


def apply_blocked(*, actor):
    from app import Lead, db
    from app.services import audit

    closed = 0
    for row in blocked()['rows']:
        lead = db.session.get(Lead, row['lead_id'])
        if lead is None:
            continue
        was = lead.stage
        lead.stage = 'Lost'
        lead.lost_reason = 'Client blocked'
        audit.record('lead.closed_blocked', 'lead', lead.id,
                     old={'stage': was},
                     new={'stage': 'Lost',
                          'lost_reason': 'Client blocked'},
                     actor=actor,
                     reason=f'CRM-LEAD-FLOW-01: {row["why"]}')
        closed += 1
    db.session.commit()
    return closed


def apply_duplicates(*, actor):
    from app import Lead, db
    from app.services import lead_merge

    done, refused = 0, 0
    for group in duplicates()['groups']:
        if not group['certain']:
            # A subject-only match is a suggestion. It draws a
            # banner on the screen; it does not merge in a sweep.
            refused += 1
            continue
        keep = db.session.get(Lead, group['survivor'])
        for member in group['members']:
            if member['keep']:
                continue
            fold = db.session.get(Lead, member['lead_id'])
            ok, _detail = lead_merge.merge(
                fold, keep, actor=actor,
                reason=f'Duplicate of #{keep.id} '
                       f'(CRM-LEAD-FLOW-01 clean-up)')
            done += 1 if ok else 0
            refused += 0 if ok else 1
    return done, refused


WRITERS = {'stages': apply_stages, 'industry': apply_industry,
           'stale': apply_stale, 'blocked': apply_blocked,
           'duplicates': apply_duplicates}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--only', choices=REPORTS,
                    help='one report instead of all six')
    ap.add_argument('--actor', help='who is doing this (emp code)')
    ap.add_argument('--apply', action='store_true',
                    help='write the change for --only')
    args = ap.parse_args()

    import app as main_app
    with main_app.app.app_context():
        if args.apply:
            if not args.only or not args.actor:
                print('--apply needs --only and --actor. Nothing was '
                      'changed.')
                return 2
            writer = WRITERS.get(args.only)
            if writer is None:
                print(f'{args.only} has no apply path here — see the '
                      f'script named in its report.')
                return 2
            print(f'=== {args.only} ===')
            print(writer(actor=args.actor))
            return 0

        for name in ([args.only] if args.only else REPORTS):
            reader, say = READERS[name]
            print(f'=== {name} ===')
            try:
                for line in say(reader()):
                    print(line)
            except Exception as exc:                  # noqa: BLE001
                print(f'  !! could not read: '
                      f'{type(exc).__name__}: {str(exc)[:160]}')
            print()
        print('Nothing was changed. To apply one section:')
        print('  --only <report> --actor <EMP> --apply')
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
