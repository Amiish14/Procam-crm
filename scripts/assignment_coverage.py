"""
How much real intake can auto-assign — read-only.

969 accounts and 339 with owners says nothing useful on its own: most
accounts never write. What matters is the share of mail that actually
arrives from an account configured well enough to assign itself.

This ranks sender domains by how much they have sent, resolves each to
an account, and reports the volume covered rather than the account
count. The unconfigured domains come out in volume order, so the list
is a worklist: fill in the top few and most of the intake assigns
itself.

Two sources, because the classification log only starts when the engine
shipped. --from leads reads every lead ever ingested, which is the real
history and the honest basis for a coverage number; --from classified
reads the log, which is small but knows which messages were judged
genuine. The default uses leads whenever the log is too thin to mean
anything.

Writes nothing.

    python scripts/assignment_coverage.py
    python scripts/assignment_coverage.py --top 40
    python scripts/assignment_coverage.py --from classified
"""
import argparse
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app, EmailClassification                       # noqa: E402
from app.services import lead_intake_db as lidb                # noqa: E402


#: Below this the log cannot support a percentage. 13 messages
#: reporting "62% covered" is a number that looks like evidence.
_ENOUGH = 200


def _from_classifications(since):
    """(domain, an address, was it judged a lead) per message."""
    return [(d, a, k == 'A_new_lead') for d, a, k in
            EmailClassification.query
            .with_entities(EmailClassification.from_domain,
                           EmailClassification.from_addr,
                           EmailClassification.classification)
            .filter(EmailClassification.created_at >= since).all()]


def _from_leads(since):
    """The same shape, from every lead the mailbox ever produced.

    Every row here became a lead, so the third field is always True —
    which is the point: these are the senders whose mail turned into
    work, and they are exactly the ones worth an owner.
    """
    from app import Lead, db

    def query(fields):
        return (db.session.query(*fields)
                .filter(Lead.source == 'email',
                        Lead.created_at >= since).all())

    try:
        rows = query([Lead.original_email_from, Lead.email])
    except Exception:
        # A database that predates the original-email columns still has
        # the contact address, which is the same sender in most cases.
        db.session.rollback()
        rows = [(None, e) for (e,) in query([Lead.email])]

    out = []
    for original, contact in rows:
        addr = (original or contact or '').strip().lower()
        if '@' not in addr:
            continue
        out.append((addr.rsplit('@', 1)[1], addr, True))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--top', type=int, default=25,
                    help='how many unconfigured domains to list')
    ap.add_argument('--days', type=int, default=365)
    ap.add_argument('--from', dest='source', default='auto',
                    choices=('auto', 'leads', 'classified'),
                    help='auto uses the lead history when the '
                         'classification log is too small to mean anything')
    args = ap.parse_args()

    with app.app_context():
        since = (datetime.now(timezone.utc).replace(tzinfo=None)
                 - timedelta(days=args.days))
        source = args.source
        if source == 'auto':
            logged = (EmailClassification.query
                      .filter(EmailClassification.created_at >= since)
                      .count())
            source = 'classified' if logged >= _ENOUGH else 'leads'
            if source == 'leads':
                print(f'\n  Only {logged} classification(s) logged — too '
                      f'few to measure anything. Reading the lead history '
                      f'instead.')

        rows = (_from_classifications(since) if source == 'classified'
                else _from_leads(since))
        if not rows:
            print('  Nothing in the window.')
            return 0

        # One sender per domain is enough to resolve the account, and
        # resolving 2,000 addresses one by one would be slow for no gain.
        volume = Counter()
        a_sender = {}
        leads = Counter()
        for domain, addr, was_lead in rows:
            if not domain:
                continue
            volume[domain] += 1
            a_sender.setdefault(domain, addr)
            if was_lead:
                leads[domain] += 1

        covered = uncovered = unmapped = 0
        gaps = []
        for domain, n in volume.items():
            account, _how = (None, None)
            try:
                account, _how = lidb.resolve_account(a_sender[domain])
            except Exception:
                pass
            if account is None:
                unmapped += n
                gaps.append((n, leads[domain], domain, '(no account)'))
                continue
            primary, _secondary = lidb.owners_for(account)
            if primary:
                covered += n
            else:
                uncovered += n
                gaps.append((n, leads[domain], domain, account.name))

        total = covered + uncovered + unmapped
        pct = lambda n: f'{round(100 * n / total)}%' if total else '—'

        what = ('message(s)' if source == 'classified'
                else 'lead(s) from email')
        print(f'\n  {total} {what} from {len(volume)} sender domain(s), '
              f'last {args.days} days\n')
        print(f'    assigns itself      {covered:>6}  {pct(covered)}')
        print(f'    account, no owner   {uncovered:>6}  {pct(uncovered)}')
        print(f'    no account at all   {unmapped:>6}  {pct(unmapped)}')

        if not gaps:
            print('\n  Every sender is configured. Nothing to do.')
            return 0

        gaps.sort(reverse=True)
        print(f'\n  The worklist — highest volume first. Configure these at '
              f'/accounts/owners:\n')
        print(f'    {"mail":>5} {"leads":>6}  {"domain":<34} account')
        running = 0
        for n, lead_n, domain, account in gaps[:args.top]:
            running += n
            print(f'    {n:>5} {lead_n:>6}  {domain:<34} {account}')
        share = round(100 * running / total) if total else 0
        print(f'\n  Those {min(args.top, len(gaps))} would cover {share}% of '
              f'all intake.')
        print('  "leads" is how many of that domain\'s messages the engine '
              'judged\n  a genuine enquiry — the column to prioritise by.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
