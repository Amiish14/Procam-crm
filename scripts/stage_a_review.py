"""
The numbers Stage A needs, read from production. Writes nothing.

Section 1 of the CRM 2.0 brief asks for row counts, counts per status
value, data-health figures and a stage-mapping count. All of those
need the live database, so they are gathered here rather than guessed
at in the report.

Nothing is written, nothing is sent, no row is touched. Safe to run
on production during the day.

    .venv/bin/python scripts/stage_a_review.py
    .venv/bin/python scripts/stage_a_review.py --json stage_a.json
"""
import argparse
import json
import os
import sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import app, db                                   # noqa: E402

#: The brief's cut-off for a live lead (the data clean-up rule).
LIVE_LEAD_MONTHS = 4

#: Proposed mapping from the present pipeline onto the v2 stages in
#: §3.1. Reported as counts so Amiish can approve or change it before
#: anything is backfilled.
STAGE_V2_MAP = {
    'New': '1 New',
    'Call Done': '2 Accepted',
    'Profile Sent': '3 Qualified',
    'Business Discussion': '3 Qualified',
    'Appointment': '3 Qualified',
    'Visit Done': '3 Qualified',
    'RFQ Generated': '4 RFQ Received',
    'Quoted': '7 Quote Submitted',
    'Under Negotiation': '8 Negotiation',
    'Won': '9 Won',
    'Lost': '9 Lost',
    'Not Interested': '9 Lost',
    'On Hold': '9 On Hold (Nurture)',
}


def _count(sql, **params):
    from sqlalchemy import text
    try:
        return db.session.execute(text(sql), params).scalar() or 0
    except Exception as exc:                                  # noqa: BLE001
        return f'? ({str(exc)[:60]})'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--json', dest='json_path')
    args = ap.parse_args()

    out = {}
    with app.app_context():
        from sqlalchemy import inspect, text
        from app import Company, Contact, Employee, Lead

        print(f'database: {db.engine.url}\n')

        # ── row counts, every table ──────────────────────────────────
        print('ROW COUNTS')
        rows = {}
        for name in sorted(inspect(db.engine).get_table_names()):
            rows[name] = _count(f'SELECT COUNT(*) FROM "{name}"')
        for name, count in rows.items():
            if count:
                print(f'  {name:<36} {count:>8,}' if isinstance(count, int)
                      else f'  {name:<36} {count}')
        empty = [n for n, c in rows.items() if c == 0]
        print(f'\n  {len(rows)} tables, {len(empty)} of them empty')
        print(f'  empty: {", ".join(empty)}')
        out['row_counts'] = rows

        # ── status fields and their distribution ─────────────────────
        print('\nSTAGE DISTRIBUTION (leads.stage)')
        stages = Counter()
        for stage, count in db.session.execute(text(
                'SELECT COALESCE(stage, \'(null)\'), COUNT(*) '
                'FROM leads GROUP BY stage')).all():
            stages[stage] = count
        total_leads = sum(stages.values())
        for stage, count in stages.most_common():
            share = (count / total_leads * 100) if total_leads else 0
            print(f'  {stage:<24} {count:>8,}  {share:5.1f}%')
        out['stages'] = dict(stages)

        print('\nOTHER STATUS FIELDS')
        for label, sql in (
                ('companies.dev_stage',
                 "SELECT COALESCE(dev_stage,'(null)'), COUNT(*) "
                 "FROM companies GROUP BY dev_stage"),
                ('leads.relevance',
                 "SELECT COALESCE(relevance,'(null)'), COUNT(*) "
                 "FROM leads GROUP BY relevance"),
                ('leads.classification',
                 "SELECT COALESCE(classification,'(null)'), COUNT(*) "
                 "FROM leads GROUP BY classification"),
                ('email_classifications.review_state',
                 "SELECT COALESCE(review_state,'(null)'), COUNT(*) "
                 "FROM email_classifications GROUP BY review_state"),
                ('leads.lost_reason (closed only)',
                 "SELECT COALESCE(lost_reason,'(none given)'), COUNT(*) "
                 "FROM leads WHERE stage IN ('Lost','Not Interested') "
                 "GROUP BY lost_reason"),
        ):
            print(f'  {label}')
            try:
                for value, count in db.session.execute(text(sql)).all():
                    print(f'    {str(value):<30} {count:>8,}')
            except Exception as exc:                          # noqa: BLE001
                print(f'    ? {str(exc)[:70]}')

        # ── data health ──────────────────────────────────────────────
        print('\nDATA HEALTH')
        open_filter = ("stage NOT IN ('Won','Lost','On Hold',"
                       "'Not Interested') AND (is_archived IS NULL "
                       "OR is_archived = 0)")
        health = {
            'leads, total': _count('SELECT COUNT(*) FROM leads'),
            'leads, open': _count(
                f'SELECT COUNT(*) FROM leads WHERE {open_filter}'),
            'open leads with no owner': _count(
                f"SELECT COUNT(*) FROM leads WHERE {open_filter} "
                f"AND (assigned_to IS NULL OR assigned_to = '')"),
            'open leads with no next action': _count(
                f"SELECT COUNT(*) FROM leads WHERE {open_filter} "
                f"AND (next_action IS NULL OR next_action = '')"),
            'open leads with no due date': _count(
                f'SELECT COUNT(*) FROM leads WHERE {open_filter} '
                f'AND followup_date IS NULL'),
            'open leads with neither': _count(
                f"SELECT COUNT(*) FROM leads WHERE {open_filter} "
                f"AND followup_date IS NULL AND (next_action IS NULL "
                f"OR next_action = '')"),
            f'open leads older than {LIVE_LEAD_MONTHS} months': _count(
                f"SELECT COUNT(*) FROM leads WHERE {open_filter} "
                f"AND COALESCE(received_at, created_at) < "
                f"date('now', '-{LIVE_LEAD_MONTHS} months')"),
            'contacts, total': _count('SELECT COUNT(*) FROM contacts'),
            'contacts with no email': _count(
                "SELECT COUNT(*) FROM contacts WHERE email IS NULL "
                "OR email = ''"),
            'accounts, total': _count('SELECT COUNT(*) FROM companies'),
            'accounts with no PIC': _count(
                "SELECT COUNT(*) FROM companies WHERE pic_emp_code IS NULL "
                "OR pic_emp_code = ''"),
            'leads with no account link': _count(
                'SELECT COUNT(*) FROM leads WHERE company_id IS NULL'),
        }
        for label, value in health.items():
            print(f'  {label:<42} {value:>8,}' if isinstance(value, int)
                  else f'  {label:<42} {value}')
        out['health'] = health

        # ── duplicate accounts, by normalised name ───────────────────
        print('\nDUPLICATE ACCOUNTS (same name once Ltd/Pvt/punctuation '
              'is stripped)')
        try:
            from app.services import client_restrictions as norm
            groups = {}
            for company in Company.query.with_entities(
                    Company.id, Company.name).all():
                key = norm.normalise(company.name or '')
                if key:
                    groups.setdefault(key, []).append(company.name)
            dupes = {k: v for k, v in groups.items() if len(v) > 1}
            print(f'  {len(dupes)} name(s) held by more than one account, '
                  f'covering {sum(len(v) for v in dupes.values())} rows')
            for key, names in sorted(dupes.items(),
                                     key=lambda kv: -len(kv[1]))[:15]:
                print(f'    {len(names)}x  {" | ".join(names[:4])}')
            out['duplicate_accounts'] = {k: v for k, v in
                                         list(dupes.items())[:200]}
        except Exception as exc:                              # noqa: BLE001
            print(f'  ? {str(exc)[:80]}')

        # ── the proposed v2 mapping, with counts ─────────────────────
        print('\nPROPOSED stage -> stage_v2 MAPPING')
        mapped = Counter()
        unmapped = Counter()
        for stage, count in stages.items():
            target = STAGE_V2_MAP.get(stage)
            if target:
                mapped[(stage, target)] = count
            else:
                unmapped[stage] = count
        for (stage, target), count in sorted(mapped.items(),
                                              key=lambda kv: -kv[1]):
            print(f'  {stage:<24} -> {target:<22} {count:>8,}')
        if unmapped:
            print('  NOT MAPPED — needs a decision:')
            for stage, count in unmapped.most_common():
                print(f'    {stage:<24} {count:>8,}')
        out['mapping'] = {f'{s} -> {t}': c for (s, t), c in mapped.items()}
        out['unmapped'] = dict(unmapped)

        # ── what the brief assumes about the stack ───────────────────
        print('\nSTACK')
        print(f'  engine        {db.engine.name}')
        print(f'  employees     {Employee.query.count():,}')
        print(f'  feature_flags table present: '
              f'{inspect(db.engine).has_table("feature_flags")}')
        print(f'  crm_parameter table present: '
              f'{inspect(db.engine).has_table("crm_parameter")}')
        print(f'  holiday_calendar present:    '
              f'{inspect(db.engine).has_table("holiday_calendar")}')

    if args.json_path:
        with open(args.json_path, 'w', encoding='utf-8') as fh:
            json.dump(out, fh, indent=2, default=str)
        print(f'\nwritten to {args.json_path}')
    print('\n== READ-ONLY — nothing was written or sent ==')
    return 0


if __name__ == '__main__':
    sys.exit(main())
