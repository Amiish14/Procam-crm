"""
What the sender rules would have held, before they hold anything.

    .venv/bin/python scripts/system_sender_report.py --check
    .venv/bin/python scripts/system_sender_report.py --seed

READ ONLY by default. `--seed` writes the starting patterns into
the Master Data list `system_sender` so an administrator can edit
them; it blocks nothing on its own, because the rules only run
when FEATURE_SENDER_BLOCK is on.

Run this first and read it. A rule that holds mail is not a rule
to switch on from a dry run nobody looked at: the cost of a wrong
pattern is a missed enquiry, which is the one failure this CRM is
built to avoid.
"""
import argparse
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Lead, app, db                             # noqa: E402
from app.services import system_senders                   # noqa: E402

DAYS = 90


def _sender(lead):
    for value in (lead.original_email_from, lead.email):
        text = (value or '').strip()
        if '<' in text and '>' in text:
            text = text[text.rfind('<') + 1:text.rfind('>')]
        if '@' in text:
            return text.strip().lower()
    return ''


def survey(days=DAYS, rules=None):
    since = datetime.utcnow() - timedelta(days=days)
    leads = (Lead.query
             .filter(Lead.source == 'email',
                     Lead.created_at >= since)
             .order_by(Lead.created_at.asc()).all())
    rules = rules if rules is not None else system_senders.patterns()

    hit, missed = [], []
    for lead in leads:
        sender = _sender(lead)
        blocked, pattern, why = system_senders.blocks(sender,
                                                      rules=rules)
        (hit if blocked else missed).append(
            (lead, sender, pattern, why))
    return leads, hit, missed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true', default=True)
    ap.add_argument('--seed', action='store_true')
    ap.add_argument('--days', type=int, default=DAYS)
    ap.add_argument('--actor', default='system')
    args = ap.parse_args()

    with app.app_context():
        print(f'database: {db.engine.url}')
        print(f'MODE:     {"SEED" if args.seed else "READ ONLY"}\n')

        rules = system_senders.patterns()
        source = ('the Master Data list'
                  if _list_exists() else 'the built-in seeds')
        print(f'  {len(rules)} pattern(s), from {source}:')
        for pattern, why in rules:
            print(f'    {pattern:<36} {why}')

        leads, hit, missed = survey(args.days, rules)
        print(f'\n  email leads in the last {args.days} days  '
              f'{len(leads)}')
        print(f'    would have been held             {len(hit)}')
        print(f'    would have come through          {len(missed)}')

        if hit:
            print('\n  by pattern:')
            for pattern, n in Counter(
                    p for _l, _s, p, _w in hit).most_common():
                print(f'    {n:>4}  {pattern}')

            print('\n  by sender:')
            by_sender = Counter(s for _l, s, _p, _w in hit)
            for sender, n in by_sender.most_common(25):
                print(f'    {n:>4}  {sender}')

            print('\n  what they became:')
            for stage, n in Counter(
                    (l.stage or '') for l, _s, _p, _w in hit
            ).most_common():
                print(f'    {n:>4}  {stage}')

            # The number that decides it: anything held that a
            # person had worked is a rule that is too wide.
            #
            # Except when the work was agreeing with the rule. The
            # first run of this flagged five Google Alerts leads
            # as worked; a person had been assigned each one and
            # closed it "system / no-reply email, not a customer".
            # That is the rule being corroborated by hand, five
            # times, and reading it as a warning would have been
            # exactly backwards.
            AGREES = ('not a customer', 'no-reply', 'noreply',
                      'system', 'not a lead', 'newsletter',
                      'spam', 'junk')
            worked, agreed = [], []
            for lead, _s, _p, _w in hit:
                touched = ((lead.stage or '') not in
                           ('New', 'Lost', 'Not Interested', '')
                           or (lead.assigned_to or '').strip()
                           or (lead.estimated_value_inr or 0))
                if not touched:
                    continue
                reason = (lead.lost_reason or '').lower()
                if any(k in reason for k in AGREES):
                    agreed.append(lead)
                else:
                    worked.append(lead)

            if agreed:
                print(f'\n  {len(agreed)} were closed by a person as '
                      f'system mail — the rule agreeing with a '
                      f'judgement somebody already made by hand:')
                for l in agreed[:10]:
                    print(f'      #{l.id} {(l.company or "")[:24]:<26}'
                          f'{(l.lost_reason or "")[:52]}')

            print(f'\n  of those, ones somebody actually worked: '
                  f'{len(worked)}')
            if worked:
                print('    !! read these before switching it on — a '
                      'pattern that catches a worked lead is too '
                      'wide:')
                for l in worked[:15]:
                    print(f'      #{l.id} {(l.company or "")[:28]:<30}'
                          f'{str(l.stage):<16}{_sender(l)}')
                    print(f'            closed: '
                          f'{(l.lost_reason or "(no reason)")[:70]}')
            else:
                print('    none — every one was either untouched or '
                      'closed by a person as system mail.')

        print('\n  senders that came through, most frequent first '
              '(to spot a robot the list misses):')
        for sender, n in Counter(
                s for _l, s, _p, _w in missed if s).most_common(12):
            print(f'    {n:>4}  {sender}')

        if not args.seed:
            print('\nREAD ONLY — nothing written. `--seed` puts the '
                  'patterns in Master Data for editing; the rules '
                  'only run when FEATURE_SENDER_BLOCK is on.')
            return

        made = system_senders.seed(actor=args.actor)
        db.session.commit()
        print(f'\n  {len(made)} pattern(s) written to Master Data '
              f'list "{system_senders.LIST_KEY}".')
        print('  Edit them in Admin → Master Data. Nothing is held '
              'until FEATURE_SENDER_BLOCK is turned on.')


def _list_exists():
    try:
        from app.models.master_data import MasterItem
        return bool(MasterItem.query.filter_by(
            list_key=system_senders.LIST_KEY).first())
    except Exception:                                     # noqa: BLE001
        return False


if __name__ == '__main__':
    main()
