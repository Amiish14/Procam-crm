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
from app.services import lead_intake as li                     # noqa: E402
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


def _samples(since, domains):
    """One real message per domain, to judge it with."""
    from app import Lead, db

    want = set(domains)
    found = {}
    try:
        rows = (db.session.query(Lead.original_email_from, Lead.email,
                                 Lead.original_email_subject,
                                 Lead.original_email_body,
                                 Lead.project, Lead.notes)
                .filter(Lead.source == 'email', Lead.created_at >= since)
                .all())
    except Exception:
        db.session.rollback()
        return found

    for original, contact, subject, body, project, notes in rows:
        addr = (original or contact or '').strip().lower()
        if '@' not in addr:
            continue
        domain = addr.rsplit('@', 1)[1]
        if domain not in want:
            continue
        # Only the preserved original. notes is deliberately NOT used
        # as a fallback: that is the field the Notes-overwrites-the-
        # enquiry bug corrupted, so on older leads it holds whatever a
        # salesperson typed over the email. Judging a customer by that
        # is how this script reported Siemens as non-business.
        text = (body or '').strip()
        line = (subject or project or '').strip()
        if not text:
            continue        # judged on nothing is not judged
        best = found.get(domain)
        if best and len(best['body']['content']) >= len(text):
            continue
        found[domain] = {
            'subject': line,
            # Graph always sends this and the parser's emptiness check
            # reads it. Omitting it made every subject-less lead look
            # like an empty message, which is why sixty real customers
            # came back "no cargo, RFQ, route or contact signal".
            'bodyPreview': text[:255],
            'body': {'content': text, 'contentType': 'text'},
            'from': {'emailAddress': {'address': addr}},
            'toRecipients': [{'emailAddress': {
                'address': 'leads@procamgroup.in'}}],
            'ccRecipients': [],
        }
    return found


#: Mapping one of these as an account domain would attach every person
#: who uses that provider to a single customer. The enquiries are real;
#: the domain is not an account.
_FREE_MAIL = {
    'gmail.com', 'googlemail.com', 'yahoo.com', 'yahoo.co.in',
    'yahoo.co.uk', 'hotmail.com', 'outlook.com', 'live.com', 'msn.com',
    'rediffmail.com', 'rediff.com', 'aol.com', 'icloud.com', 'me.com',
    'protonmail.com', 'proton.me', 'zoho.com', 'mail.com', 'gmx.com',
    'yandex.com', 'qq.com', '163.com', '126.com',
}

#: Words that suggest the sender is in the trade. Not a verdict — an
#: overseas agent asking us to move their client's cargo is a genuine
#: enquiry, and a shipping line quoting us is not. Only a person knows
#: which, so this asks rather than decides.
_TRADE_WORDS = ('logistic', 'cargo', 'shipping', 'freight', 'forward',
                'transport', 'shipp', 'marine', 'lines', 'express',
                'hlag', 'maersk', 'dhl', 'kuehne', 'dsv', 'panalpina')


def _caution(domain):
    """A warning to print beside a domain, or ''. """
    if domain in _FREE_MAIL:
        return ('free mail — map the CONTACT, never this domain as an '
                'account')
    if any(w in domain for w in _TRADE_WORDS):
        return 'in the trade — customer, overseas agent, or vendor?'
    return ''


def _preserved(since):
    """How much of the history kept its enquiry text.

    Without this the reader cannot tell an engine that judged badly from
    an engine that had nothing to read.
    """
    from app import Lead, db
    try:
        total = (db.session.query(Lead.id)
                 .filter(Lead.source == 'email',
                         Lead.created_at >= since).count())
        kept = (db.session.query(Lead.id)
                .filter(Lead.source == 'email', Lead.created_at >= since,
                        Lead.original_email_body.isnot(None),
                        Lead.original_email_body != '').count())
    except Exception:
        db.session.rollback()
        return 'could not measure how many enquiries were preserved'
    pct = round(100 * kept / total) if total else 0
    return (f'{kept} of {total} email lead(s) still have their original '
            f'enquiry text ({pct}%).')


def _verdicts(samples):
    """What the engine running today would call each of them.

    The lead history was captured under the old policy, where every
    message became a lead. Reading that as "this domain sends us work"
    is how a worklist ends up telling you to create an account for an
    auction-notification robot.
    """
    from app.services import lead_intake as li
    from app.services import lead_intake_db as lidb

    out = {}
    try:
        ctx = lidb.build_context()
    except Exception:
        return out
    for domain, msg in samples.items():
        try:
            out[domain] = li.classify(msg, ctx)
        except Exception:
            pass
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--top', type=int, default=25,
                    help='how many unconfigured domains to list')
    ap.add_argument('--days', type=int, default=365)
    ap.add_argument('--show', type=int, default=0, metavar='N',
                    help='print what was actually sampled for the top N '
                         'domains, so a verdict can be checked')
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
        head = gaps[:max(args.top * 3, 60)]
        samples = ({} if source == 'classified'
                   else _samples(since, [g[2] for g in head]))
        verdicts = _verdicts(samples)

        print(f'\n  {_preserved(since)}')
        print(f'  {len(verdicts)} of {len(head)} top domains had a stored '
              f'enquiry to judge.')
        if args.show:
            for domain, msg in list(samples.items())[:args.show]:
                body = msg['body']['content']
                print(f'\n    --- {domain} ---')
                print(f'    subject: {msg["subject"][:100]}')
                print(f'    body   : {body[:240]!r}')

        worth, noise, unknown = [], [], []
        for n, lead_n, domain, account in head:
            d = verdicts.get(domain)
            if d is None:
                # No stored email to read. Absence of evidence is not a
                # verdict: the first version of this called Siemens and
                # Godrej robots because their bodies were never kept.
                unknown.append((n, domain, account))
            elif d.klass == li.Klass.NEW_LEAD:
                worth.append((n, domain, account,
                              f'{d.confidence or 0}% new enquiry'))
            elif d.klass == li.Klass.REVIEW:
                worth.append((n, domain, account, 'needs review'))
            else:
                # step and reason, because "Non-business" alone cannot
                # be argued with — and the last two versions of this
                # script were both wrong in ways the reason would have
                # shown immediately.
                noise.append((n, domain, account,
                              f'{li.Klass.LABELS.get(d.klass, d.klass)} '
                              f'[step {d.step}: {d.reason}]'))

        print(f'\n  Worth an owner — the engine running today still calls '
              f'these enquiries:\n')
        print(f'    {"leads":>6}  {"domain":<34} {"account":<34} verdict')
        running = 0
        for n, domain, account, why in worth[:args.top]:
            running += n
            print(f'    {n:>6}  {domain:<32} {account[:30]:<32} {why}')
            care = _caution(domain)
            if care:
                print(f'            ^ {care}')
        if not worth:
            print('    (none)')
        share = round(100 * running / total) if total else 0
        print(f'\n  Those {min(args.top, len(worth))} cover {share}% of the '
              f'history.')

        if unknown:
            unknown_total = sum(n for n, *_ in unknown)
            print(f'\n  Cannot tell — {unknown_total} lead(s) whose email '
                  f'body was never stored,\n  so there is nothing to judge. '
                  f'Decide these by the name:\n')
            for n, domain, account in unknown[:args.top]:
                print(f'    {n:>6}  {domain:<34} {account}')

        if noise:
            noise_total = sum(n for n, *_ in noise)
            print(f'\n  NOT worth an owner — {noise_total} historical '
                  f'lead(s) the engine\n  would now file rather than '
                  f'create. Creating accounts for these\n  would be '
                  f'building a customer list out of robots:\n')
            for n, domain, account, klass in noise[:args.top]:
                print(f'    {n:>6}  {domain:<30} {account[:28]:<30}')
                print(f'            {klass}')
            print(f'\n  These already exist as leads, captured before the '
                  f'engine. Cleaning\n  them up is a separate, destructive '
                  f'decision — nothing here touches them.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
