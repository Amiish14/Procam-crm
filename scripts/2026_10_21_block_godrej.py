"""
Decision 21 Oct 2026 (CRM questionnaire C6): block Godrej — bad paymaster.

    # 1. See every account and register entry that looks like Godrej.
    .venv/bin/python scripts/2026_10_21_block_godrej.py

    # 2. Block the ones management means, by account id.
    .venv/bin/python scripts/2026_10_21_block_godrej.py \
        --apply --ids 12,57 --approver DIR12010

Nothing is blocked by name match alone. Godrej is a group of separate
companies, and the decision has to say which of them it covers, so the
script lists and a person picks. Each picked account gets its own
register entry, linked to the account, at entity scope (not the whole
group's mail domain), approved by --approver — who must hold
admin.restrictions. Approving runs the usual close-out: open leads
closed with "Client blocked by management", open RFQs withdrawn, open
quotes lost. Won work is listed, never touched.

Undo: lift each entry on /admin/restrictions (needs a reason); lifting
does not reopen the closed records.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import app, db                                   # noqa: E402

PATTERN = 'godrej'
CATEGORY = 'Payment default / outstanding'
REASON = ('Bad paymaster: payments delayed or withheld. Blocked by '
          'management decision on 21 Oct 2026.')


def _candidates():
    from app import Company, Lead
    rows = (Company.query.filter(Company.name.ilike(f'%{PATTERN}%'))
            .order_by(Company.name).all())
    out = []
    for c in rows:
        open_leads = Lead.query.filter(Lead.company_id == c.id).filter(
            ~Lead.stage.in_(('Won', 'Lost', 'Not Interested'))).count()
        out.append((c, open_leads))
    return out


def _on_register():
    from app.models.restriction import ClientRestriction
    return ClientRestriction.query.filter(
        ClientRestriction.company_name.ilike(f'%{PATTERN}%')).all()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--ids', default='')
    ap.add_argument('--approver', default='')
    args = ap.parse_args()

    with app.app_context():
        from app.models.restriction import ClientRestriction  # noqa: F401
        print(f'database: {db.engine.url}\n')
        found = _candidates()
        print(f'Accounts whose name contains "{PATTERN}": {len(found)}')
        for c, open_leads in found:
            print(f'  id {c.id:>6}  {c.name[:60]:<60}  gstin '
                  f'{c.gstin or "-":<16} open leads {open_leads}')
        existing = _on_register()
        print(f'\nAlready on the register: {len(existing)}')
        for r in existing:
            print(f'  #{r.id} {r.company_name} — {r.status}'
                  f' (account {r.linked_account_id or "-"})')

        if not args.apply:
            print('\nPREVIEW ONLY — nothing written. Re-run with '
                  '--apply --ids <id,id> --approver <emp_code>.')
            return

        from app.services import restriction_register as reg
        ids = [int(x) for x in args.ids.split(',') if x.strip().isdigit()]
        if not ids:
            raise SystemExit('--apply needs --ids: the account ids to block.')
        if not args.approver or not reg.can_approve(args.approver):
            raise SystemExit('--approver must hold admin.restrictions.')
        known = {c.id: c for c, _n in found}
        linked = {r.linked_account_id for r in existing}
        for account_id in ids:
            acct = known.get(account_id)
            if acct is None:
                print(f'  !! account {account_id} is not in the list above '
                      '— skipped')
                continue
            if account_id in linked:
                print(f'  {acct.name}: already on the register — skipped')
                continue
            # The register matches leads by name. Leads linked to this
            # account but typed with a shorter name would be missed, so
            # their spellings go in as aliases.
            from app import Lead
            aliases = sorted({(l.company or '').strip() for l in
                              Lead.query.filter(Lead.company_id == acct.id)
                              if (l.company or '').strip()
                              and l.company.strip() != acct.name})
            row = reg.create({'company_name': acct.name,
                              'aliases': aliases,
                              'reason_category': CATEGORY,
                              'reason_detail': REASON,
                              'gstin': acct.gstin or '',
                              'linked_account_id': acct.id,
                              'scope': 'entity'},
                             actor=args.approver, status='blocked')
            # create() sets the state but does not close the open
            # business; approve() does, and accepts a blocked row.
            result = reg.approve(row, actor=args.approver,
                                 note='Decision 21 Oct 2026 (C6)')
            counts = (result or {}).get('counts', {}) \
                if isinstance(result, dict) else {}
            print(f'  + blocked {acct.name} (register #{row.id}) {counts}')


if __name__ == '__main__':
    main()
