"""Leads whose only named person is us. UAT 19 Nov item 1.

READ ONLY. No --apply, and not because it was left out: what to do
with one of these leads is a different answer each time. Some need the
customer's own contact found and typed in, some are internal forwards
that should never have become leads, and some are ours legitimately
because the enquiry came from a Procam office. A script that picked
one of those three would be wrong two thirds of the time.

    .venv/bin/python scripts/2026_11_19_procam_pic_leads.py
    ... --out docs/procam_pic_leads.csv
    ... --open-only            # leads still worth chasing

WHAT IT LISTS

Every live lead whose PIC is us and NOT a customer person -- our own
email domain (procamgroup.in, procamlogistics.com, procamlogitech.com,
orsayinternational.com and whatever the mail modules add to that list),
or a PIC whose first word is "Procam" or "Orsay". Lead #10029 is the
one that started this: PIC "Procam Forward", company "Unknown", on
`procamadmin@procamgroup.in`.

These are now hidden from Find a person and from the duplicate check
-- `people_search.not_a_customer_person` -- so this list is what is
left to FIX rather than what is still leaking. A lead with no reachable
customer on it is a lead nobody can progress, whether or not a search
offers it.

The CSV carries the owner and the stage, because what to do about one
is the owner's call and a closed lead is not worth anybody's morning.
"""
import argparse
import csv
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEAD_STAGES = {'lost', 'closed', 'dropped', 'junk', 'cancelled',
               'closed lost', 'closed-lost', 'archived'}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default='docs/procam_pic_leads.csv')
    ap.add_argument('--open-only', dest='open_only', action='store_true',
                    help='leave out the lost and closed ones')
    a = ap.parse_args()

    os.environ.setdefault('SECRET_KEY', 'read-only')
    os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'read-only-0000000')
    from app import app as flask_app, Lead
    from app.services import own_people

    with flask_app.app_context():
        rows, why_count, counted = [], defaultdict(int), 0
        q = Lead.query.filter(Lead.is_archived.isnot(True),
                              Lead.junk_deleted.isnot(True),
                              Lead.merged_into_id.is_(None))
        for lead in q.all():
            counted += 1
            pic = (lead.pic or '').strip()
            if not pic:
                continue        # no person at all is a different survey
            addresses = [a for a in (lead.email,
                                     getattr(lead, 'email2', None)) if a]
            ours, why = own_people.is_us(pic, *addresses)
            if not ours:
                continue
            stage = (getattr(lead, 'stage', None) or '').strip()
            if a.open_only and stage.lower() in DEAD_STAGES:
                continue
            why_count[why] += 1
            rows.append({
                'lead_id': lead.id,
                'lead_number': getattr(lead, 'lead_number', '') or '',
                'pic': pic,
                'email': lead.email or '',
                'company': (lead.company or '').strip() or '(blank)',
                'stage': stage or '(blank)',
                'owner': (getattr(lead, 'owner', None)
                          or getattr(lead, 'owner_code', None) or ''),
                'city': getattr(lead, 'city', '') or '',
                'created': str(getattr(lead, 'created_at', '') or '')[:10],
                'why_it_is_us': why,
            })

        rows.sort(key=lambda r: (r['why_it_is_us'], -int(r['lead_id'])))
        path = os.path.abspath(a.out)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fields = ['lead_id', 'lead_number', 'pic', 'email', 'company',
                  'stage', 'owner', 'city', 'created', 'why_it_is_us']
        with open(path, 'w', newline='', encoding='utf-8') as fh:
            w = csv.DictWriter(fh, fieldnames=fields, extrasaction='ignore')
            w.writeheader()
            w.writerows(rows)

        print(f'{counted} live lead(s) read.')
        print(f'{len(rows)} have us as their only named person'
              + (' (open ones only)' if a.open_only else ''))
        for why, n in sorted(why_count.items(), key=lambda kv: -kv[1]):
            print(f'  {n:>5}  {why}')
        if rows:
            print('\n  lead   PIC                        company'
                  '                    stage')
            for r in rows[:20]:
                print(f'  #{r["lead_id"]:<6} {r["pic"][:26]:<26} '
                      f'{r["company"][:25]:<25} {r["stage"][:14]}')
            if len(rows) > 20:
                print(f'  … and {len(rows) - 20} more in the CSV')
        print(f'\nCSV: {path}')
        print('\nNothing was changed. These are already hidden from Find a '
              'person and from\nthe duplicate check; this is the list of '
              'leads to put a real person on.')


if __name__ == '__main__':
    main()
