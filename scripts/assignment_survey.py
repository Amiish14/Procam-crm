"""
CRM-AUTO-ASSIGN-01 Stage A — how leads get an owner, and how well.

READ-ONLY. Writes nothing.

    .venv/bin/python scripts/assignment_survey.py
    .venv/bin/python scripts/assignment_survey.py --days 30 --csv /tmp/a.csv

Four questions:

  ownership   open leads with nobody, with somebody who has left, and
              where the lead's owner and the account's owner disagree.
  routes      leads created in the window, split by how they got their
              owner: the ingest's account match, a name in a forward,
              a person on a form, an import, or nothing at all.
  matching    how often an inbound mail resolved to an account, and by
              which rule — contact email, account domain, website,
              GSTIN or company name.
  heads       whether every vertical a lead carries has a head to fall
              back on, which is what any auto-assignment would rest on.
"""
import argparse
import csv
import os
import sys
from collections import Counter
from datetime import datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Company, Employee, Lead, app, db          # noqa: E402


def open_stages():
    from app.services import sales_rules as rules
    return set(rules.open_stages())


def section(title):
    print(f'\n{"=" * 72}\n{title}\n{"=" * 72}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=30)
    ap.add_argument('--csv', default='')
    args = ap.parse_args()

    with app.app_context():
        print(f'database: {db.engine.url}')
        print('MODE:     READ-ONLY — nothing is written')
        since = datetime.utcnow() - timedelta(days=args.days)
        stages = open_stages()
        active = {e.emp_code for e in
                  Employee.query.filter(Employee.is_active.is_(True)).all()}
        everyone = {e.emp_code: e for e in Employee.query.all()}

        open_leads = (Lead.query
                      .filter(Lead.is_archived.isnot(True),
                              Lead.stage.in_(tuple(stages))).all())

        # ── 1. ownership ──
        section(f'OWNERSHIP — {len(open_leads)} open leads')
        unowned = [l for l in open_leads if not (l.assigned_to or '').strip()]
        gone = [l for l in open_leads
                if (l.assigned_to or '').strip()
                and l.assigned_to not in active]
        print(f'  no owner at all              {len(unowned):>6}')
        print(f'  owner is no longer active    {len(gone):>6}')
        print(f'  owner active                 '
              f'{len(open_leads) - len(unowned) - len(gone):>6}')

        if gone:
            print('\n  inactive owners holding open leads:')
            for code, n in Counter(l.assigned_to for l in gone).most_common():
                emp = everyone.get(code)
                print(f'      {code:<14} {(emp.name if emp else "NOT ON FILE")[:30]:<30} '
                      f'{n:>5} leads')

        # owner vs the account's owner
        accounts = {c.id: c for c in Company.query.all()}
        disagree = []
        for l in open_leads:
            acct = accounts.get(l.company_id) if l.company_id else None
            if acct is None:
                continue
            acct_owner = (getattr(acct, 'pic_emp_code', '') or '').strip()
            if acct_owner and (l.assigned_to or '').strip() \
                    and acct_owner != l.assigned_to:
                disagree.append((l, acct_owner))
        linked = [l for l in open_leads if l.company_id]
        print(f'\n  open leads linked to an account        {len(linked):>6}')
        print(f'  ... of those, owner != account owner   {len(disagree):>6}')
        print(f'  open leads with no account at all      '
              f'{len(open_leads) - len(linked):>6}')

        # ── 2. routes ──
        section(f'HOW THE LAST {args.days} DAYS GOT THEIR OWNER')
        recent = (Lead.query
                  .filter(Lead.is_archived.isnot(True),
                          db.func.coalesce(Lead.received_at,
                                           Lead.created_at) >= since).all())
        print(f'  {len(recent)} leads created in the window\n')
        by_source = Counter((l.source or 'unknown') for l in recent)
        for src, n in by_source.most_common():
            owned = sum(1 for l in recent
                        if (l.source or 'unknown') == src
                        and (l.assigned_to or '').strip())
            print(f'  source {src:<16} {n:>5}   with an owner: {owned:>5} '
                  f'({100 * owned // n if n else 0}%)')

        # The audit trail says which rule assigned it.
        try:
            from app.models.audit import AuditEvent
            ids = {str(l.id) for l in recent}
            notes = Counter()
            for ev in (AuditEvent.query
                       .filter(AuditEvent.entity_type.in_(('Lead', 'lead')),
                               AuditEvent.action == 'lead.ownership_change',
                               AuditEvent.occurred_at >= since).all()):
                if ev.entity_id in ids:
                    notes[(ev.reason or 'no reason recorded')[:60]] += 1
            if notes:
                print('\n  recorded reason on the assignment:')
                for reason, n in notes.most_common(10):
                    print(f'      {n:>5}  {reason}')
        except Exception:
            db.session.rollback()

        # ── 3. matching ──
        section(f'ACCOUNT MATCHING — last {args.days} days of ingested mail')
        try:
            from app.models.ingest_log import MailIngestLog
            from app.services import lead_intake_db as idb

            rows = (MailIngestLog.query
                    .filter(MailIngestLog.received_at >= since).all())
            print(f'  {len(rows)} messages in the ingest log')
            by_outcome = Counter(r.outcome for r in rows)
            for outcome, n in by_outcome.most_common():
                print(f'      {outcome:<12} {n:>5}')

            # Re-run the matcher over the senders, read-only.
            how = Counter()
            senders = [(r.from_addr or '').strip() for r in rows
                       if (r.from_addr or '').strip()]
            for addr in senders:
                try:
                    _acct, why = idb.resolve_account(addr)
                except Exception:
                    db.session.rollback()
                    why = 'error'
                how[why] += 1
            matched = sum(n for k, n in how.items() if k != 'unmapped')
            total = sum(how.values()) or 1
            print(f'\n  of {total} senders, {matched} resolved to an '
                  f'account ({100 * matched // total}%)')
            for why, n in how.most_common():
                print(f'      {why:<26} {n:>5}')
        except Exception as exc:                          # noqa: BLE001
            db.session.rollback()
            print(f'  ! could not read the ingest log: {str(exc)[:160]}')

        # ── 4. heads ──
        section('WHAT AUTO-ASSIGNMENT WOULD REST ON')
        from app.services import vertical_heads as vh
        vh.cache_clear()
        verticals = (db.session.query(Lead.procam_vertical,
                                      db.func.count(Lead.id))
                     .filter(Lead.is_archived.isnot(True),
                             Lead.stage.in_(tuple(stages)))
                     .group_by(Lead.procam_vertical).all())
        for value, n in sorted(verticals, key=lambda kv: -kv[1]):
            heads = vh.heads_for_vertical(value)
            ok = [h for h in heads if h in active]
            print(f'  {(value or "(blank)"):<22} {n:>6} open  -> '
                  f'{", ".join(ok) if ok else "NOBODY ACTIVE"}')

        print(f'\n  accounts with no owner: '
              f'{sum(1 for c in accounts.values() if not (getattr(c, "pic_emp_code", "") or "").strip())}'
              f' of {len(accounts)}')

        if args.csv and (unowned or gone):
            with open(args.csv, 'w', newline='', encoding='utf-8') as fh:
                w = csv.writer(fh)
                w.writerow(['lead_id', 'company', 'stage', 'vertical',
                            'owner', 'problem'])
                for l in unowned:
                    w.writerow([l.id, l.company, l.stage, l.procam_vertical,
                                '', 'no owner'])
                for l in gone:
                    w.writerow([l.id, l.company, l.stage, l.procam_vertical,
                                l.assigned_to, 'owner inactive'])
            print(f'\n  written to {args.csv}')

        print('\nREAD-ONLY. Nothing was written.')


if __name__ == '__main__':
    main()
