"""
Link leads filed under a placeholder to the account they belong to.

CRM-LEAD-ACCOUNT-01 §2.6, and §5's one-off: the BEML tender saved
as "To review" on 29 Oct 2026 needs joining to BEML LIMITED.

Read-only by default. It prints what it would do and changes
nothing:

    .venv/bin/python scripts/link_unplaced_leads.py
    .venv/bin/python scripts/link_unplaced_leads.py --lead 12437

Applying needs the leads named, the account named, and an actor —
no "link everything it guessed", because a wrong link is harder to
find afterwards than an unlinked lead:

    .venv/bin/python scripts/link_unplaced_leads.py \\
        --ids 12437 --account 318 --actor DIR12010 --apply

Nothing is deleted and nothing is closed. A lead that is already
linked is skipped and said so.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


def candidates(lead_ids=()):
    """Every unplaced open lead, with what the matcher suggests."""
    from app import Lead, db
    from app.services import lead_account as la

    rows = []
    if lead_ids:
        leads = [db.session.get(Lead, int(x)) for x in lead_ids]
        leads = [x for x in leads if x is not None]
    else:
        from app.services import sales_rules
        leads = (Lead.query
                 .filter(Lead.company_id.is_(None),
                         Lead.is_archived.isnot(True),
                         Lead.stage.in_(sales_rules.open_stages()))
                 .order_by(Lead.id.desc()).limit(500).all())
    for lead in leads:
        why = la.is_placeholder(lead.company)
        if not why and not lead_ids:
            continue
        rows.append({
            'lead': lead,
            'why': why or '(named, but no account linked)',
            'suggestions': la.suggest(lead, limit=3),
        })
    return rows


def report(rows):
    if not rows:
        return 'Nothing unplaced. Every open lead has an account.'
    out = [f'{len(rows)} lead(s) with no account:', '']
    for row in rows:
        lead = row['lead']
        out.append(f'  #{lead.id}  {lead.company or "(blank)"}')
        out.append(f'      {row["why"]}')
        out.append(f'      from {lead.email or "(no sender)"}'
                   f'  ·  {lead.stage}')
        if row['suggestions']:
            for found in row['suggestions']:
                out.append(f'      suggests: {found["name"]} '
                           f'(id {found["account_id"]}) — '
                           f'{found["rule"]}')
        else:
            out.append('      suggests: nothing — create the account')
        out.append('')
    out.append('Nothing was changed. To link, name the leads and the '
               'account:')
    out.append('  --ids <lead ids> --account <id> --actor <EMP> '
               '--apply')
    return '\n'.join(out)


def apply(lead_ids, *, account_id, actor):
    """Link each named lead to the named account. (done, refused)."""
    from app import Company, Lead, db
    from app.services import lead_account as la

    account = db.session.get(Company, int(account_id))
    if account is None:
        return [], [(None, f'no account {account_id}')]

    done, refused = [], []
    for raw in lead_ids:
        lead = db.session.get(Lead, int(raw))
        if lead is None:
            refused.append((raw, 'no such lead'))
            continue
        if lead.company_id:
            refused.append((lead.id, f'already linked to '
                                     f'{lead.company}'))
            continue
        ok, detail = la.link(lead, account, actor=actor,
                             reason='one-off clean-up')
        if not ok:
            refused.append((lead.id, detail))
            continue
        done.append((lead.id, account.name))
    db.session.commit()
    return done, refused


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--lead', action='append', default=[],
                    help='look at one lead, placeholder or not')
    ap.add_argument('--ids', nargs='*', default=[],
                    help='the leads to link, with --apply')
    ap.add_argument('--account', help='the account id to link to')
    ap.add_argument('--actor', help='who is doing this (emp code)')
    ap.add_argument('--apply', action='store_true',
                    help='write the links')
    args = ap.parse_args()

    import app as main_app
    with main_app.app.app_context():
        if not args.apply:
            print(report(candidates(args.lead or args.ids)))
            return 0

        if not args.ids or not args.account or not args.actor:
            print('--apply needs --ids, --account and --actor. '
                  'Nothing was changed.')
            return 2
        done, refused = apply(args.ids, account_id=args.account,
                              actor=args.actor)
        for lead_id, name in done:
            print(f'linked  #{lead_id} -> {name}')
        for lead_id, why in refused:
            print(f'refused #{lead_id}: {why}')
        print(f'\n{len(done)} linked, {len(refused)} refused.')
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
