"""
Leads created against a restricted client since the gate went live.

    .venv/bin/python scripts/blocked_client_leaks.py --since 2026-10-31
    .venv/bin/python scripts/blocked_client_leaks.py --since 2026-10-31 \
        --csv /tmp/blocked-client-leaks.csv

Why this exists
    `client_restrictions.check()` held the register as a 30-second
    cache of ORM instances until CRM-VERTICAL-SAVE-500-01. A commit in
    the loading request expired those instances and the end of that
    request detached them, so reading one raised
    DetachedInstanceError — and `check()` catches everything and
    answers "not restricted". The gate therefore failed *open*: for up
    to thirty seconds after any commit, a blocked client could be let
    through with nothing on screen and only a logged "restriction
    check failed" behind it.

    The audit trail cannot answer whether that happened. It records
    attempts that were refused, not checks that silently passed. So
    the only way to find a leak is to ask the register, now that it
    works, about every lead created while it did not: a lead that
    matches a blocked entry and has no refusal logged against it is a
    lead the gate should have stopped.

Read-only
    No write of any kind. Nothing is committed, no audit row is
    recorded, no mail is sent, and the register's own cache is only
    read. It is safe to run against production while people are
    working.

What a row means
    WOULD HAVE BEEN REFUSED
        A blocked entry matched on something exact — the account,
        GSTIN, PAN, an exact address, a group domain, or the
        normalised name. The gate refuses these outright, so a lead
        here got past it.
    ALREADY CLOSED
        It matches a blocked entry and the lead is already closed as
        `Client blocked by management` — found and handled by the
        register's own sweep. Not a leak.
    WARNING ONLY
        A caution entry, or a fuzzy name score. These never refuse a
        lead; the person sees a warning and may go ahead. Listed so
        the near misses are visible, not because anything went wrong.

Dates
    `--since` and `--until` are IST calendar dates, because that is
    what a working day is here. `leads.created_at` is UTC, so the
    window is shifted by 5h30m and both ends are printed.
"""
import argparse
import csv
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Company, Lead, app, db                     # noqa: E402

#: IST. The database stores naive UTC, so the day boundary moves.
IST = timedelta(hours=5, minutes=30)

#: What `restriction_register` writes on a lead it closes.
CLOSED_REASON = 'Client blocked by management'

REFUSED = 'WOULD HAVE BEEN REFUSED'
CLOSED = 'ALREADY CLOSED'
WARNED = 'WARNING ONLY'

#: The ways `matches_row` can be certain. A blocked verdict is only
#: ever returned for one of these — a fuzzy name warns, it never
#: refuses — so this is the set that separates a leak from a near miss.
EXACT = ('account', 'gstin', 'pan', 'email', 'domain', 'name')


def _utcnow():
    """Naive UTC, which is what the database stores."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _window(since, until):
    """(start, end) in naive UTC for a pair of IST calendar dates."""
    start = datetime.strptime(since, '%Y-%m-%d') - IST
    end = ((datetime.strptime(until, '%Y-%m-%d') + timedelta(days=1)) - IST
           if until else _utcnow())
    return start, end


def _account(lead, cache):
    """The lead's account, if it has one. Cached — a hundred leads on
    one blocked client would otherwise be a hundred queries."""
    account_id = getattr(lead, 'company_id', None)
    if not account_id:
        return None
    if account_id not in cache:
        cache[account_id] = db.session.get(Company, account_id)
    return cache[account_id]


def _parts(lead, account):
    """Everything about this lead the register knows how to match.

    The lead's own name and address, plus whatever its account adds:
    the GSTIN, the PAN where the model carries one, and the account id
    itself, which is the register's most certain match of all.
    """
    from app.services.client_restrictions import domain_of

    emails = [(getattr(lead, 'email', '') or '').strip().lower(),
              (getattr(lead, 'email2', '') or '').strip().lower(),
              domain_of(getattr(lead, 'original_email_from', '') or '')
              and (getattr(lead, 'original_email_from', '') or '').strip()]
    emails = [e for e in emails if e and '@' in e]
    domains = sorted({domain_of(e) for e in emails} - {''})
    if account is not None:
        for value in (getattr(account, 'email_domains', None) or []):
            value = (str(value) or '').strip().lower().lstrip('@')
            if value and value not in domains:
                domains.append(value)
    return {
        'names': [n for n in [(getattr(lead, 'company', '') or '').strip(),
                              (getattr(account, 'name', '') or '').strip()
                              if account is not None else ''] if n],
        'emails': emails,
        'domains': domains,
        'gstin': (getattr(account, 'gstin', '') or '') if account else '',
        'pan': (getattr(account, 'pan', '') or '') if account else '',
        'account_id': getattr(lead, 'company_id', None),
    }


def _best_match(rows, parts):
    """(row, matched_on, score) for the strongest entry this lead hits.

    Every name, address and domain the lead carries is tried against
    every entry, because a lead's own typed name and its account's
    master name are often not the same string and only one of them
    needs to be the blocked client.
    """
    best = (None, '', 0.0)
    for row in rows:
        from app.services.client_restrictions import matches_row

        for name in (parts['names'] or [None]):
            for email in (parts['emails'] or [None]):
                matched_on, score = matches_row(
                    row, company_name=name, email=email,
                    gstin=parts['gstin'], pan=parts['pan'],
                    account_id=parts['account_id'])
                if matched_on and _rank(row, matched_on, score) > \
                        _rank(best[0], best[1], best[2]):
                    best = (row, matched_on, score)
        for domain in parts['domains']:
            matched_on, score = matches_row(
                row, company_name=None, domain=domain,
                gstin=parts['gstin'], pan=parts['pan'],
                account_id=parts['account_id'])
            if matched_on and _rank(row, matched_on, score) > \
                    _rank(best[0], best[1], best[2]):
                best = (row, matched_on, score)
    return best


def _rank(row, matched_on, score):
    """A blocked exact match outranks everything, then a blocked fuzzy
    one, then a caution. Within a tier, the higher score."""
    if row is None:
        return (0, 0.0)
    from app.models.restriction import BLOCKED

    blocked = row.status == BLOCKED
    exact = matched_on in EXACT
    return ((3 if blocked and exact else 2 if blocked else 1), score)


def _verdict(row, matched_on, lead):
    from app.models.restriction import BLOCKED

    if row.status != BLOCKED or matched_on not in EXACT:
        return WARNED
    if (getattr(lead, 'lost_reason', '') or '') == CLOSED_REASON:
        return CLOSED
    return REFUSED


def _attempts(start, end):
    """{restriction_id: [events]} — refusals logged in the window.

    A lead that matches a blocked entry *and* has a refusal logged
    against the same entry by the same person is not a leak: somebody
    was stopped and then created the lead under a name that did not
    match. Worth knowing, and worth not calling a gate failure.
    """
    from app.models.restriction import ClientRestrictionEvent

    out = {}
    rows = (ClientRestrictionEvent.query
            .filter(ClientRestrictionEvent.action == 'attempt_blocked',
                    ClientRestrictionEvent.created_at >= start,
                    ClientRestrictionEvent.created_at < end).all())
    for ev in rows:
        out.setdefault(ev.restriction_id, []).append(ev)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--since', default='2026-10-31',
                    help='IST date the gate went live (inclusive)')
    ap.add_argument('--until', default=None,
                    help='IST date to stop at (inclusive); default now')
    ap.add_argument('--include-archived', action='store_true',
                    help='also list archived and junk-deleted leads')
    ap.add_argument('--csv', default=None, help='write the rows to a file')
    args = ap.parse_args()

    start, end = _window(args.since, args.until)

    with app.app_context():
        print(f'database: {db.engine.url}')
        print('MODE:     READ ONLY — nothing is written\n')

        from app.models.restriction import BLOCKED, CAUTION
        from app.services import client_restrictions as restrictions

        rows = restrictions.live_rows()
        blocked = [r for r in rows if r.status == BLOCKED]
        caution = [r for r in rows if r.status == CAUTION]
        print(f'  register in force            {len(rows)} entr(ies)')
        print(f'    blocked                    {len(blocked)}')
        print(f'    caution                    {len(caution)}')
        if not rows:
            print('\n  Nothing is in force, so nothing can have leaked.')
            return 0
        for row in blocked:
            print(f'      BLOCKED  {row.company_name}')

        q = Lead.query.filter(Lead.created_at >= start, Lead.created_at < end)
        if not args.include_archived:
            q = q.filter(Lead.is_archived.isnot(True))
        leads = q.order_by(Lead.created_at).all()
        print(f'\n  window (IST)                 {args.since} '
              f'to {args.until or "now"}')
        print(f'  window (UTC, as stored)      {start:%Y-%m-%d %H:%M} '
              f'to {end:%Y-%m-%d %H:%M}')
        print(f'  leads created in it          {len(leads)}')

        attempts = _attempts(start, end)
        accounts, found, why = {}, [], Counter()
        for lead in leads:
            account = _account(lead, accounts)
            row, matched_on, score = _best_match(
                rows, _parts(lead, account))
            if row is None:
                continue
            verdict = _verdict(row, matched_on, lead)
            why[verdict] += 1
            found.append({
                'verdict': verdict,
                'lead_id': lead.id,
                'created_at_utc': str(lead.created_at)[:16],
                'created_at_ist': str((lead.created_at + IST))[:16]
                if lead.created_at else '',
                'company': lead.company or '',
                'account_id': lead.company_id or '',
                'email': lead.email or '',
                'source': lead.source or '',
                'stage': lead.stage or '',
                'owner': lead.assigned_to or '',
                'business_unit': lead.business_unit or '',
                'restriction_id': row.id,
                'restricted_client': row.company_name,
                'status': row.status,
                'matched_on': matched_on,
                'score': round(score, 3),
                'refusals_logged_for_this_entry':
                    len(attempts.get(row.id, ())),
                'lead_closed_as_blocked':
                    'yes' if (lead.lost_reason or '') == CLOSED_REASON
                    else 'no',
            })

        print('\n  matches')
        for label in (REFUSED, CLOSED, WARNED):
            print(f'    {label:<26} {why[label]}')

        for label in (REFUSED, CLOSED, WARNED):
            hits = [f for f in found if f['verdict'] == label]
            if not hits:
                continue
            print(f'\n  {label} ({len(hits)})')
            for f in hits:
                print(f'    #{f["lead_id"]:<7} {f["created_at_ist"]}  '
                      f'{f["company"][:34]:<34} -> '
                      f'{f["restricted_client"][:28]:<28} '
                      f'{f["matched_on"]}/{f["score"]}  '
                      f'src={f["source"]} owner={f["owner"] or "-"}')

        if args.csv:
            with open(args.csv, 'w', newline='') as fh:
                writer = csv.DictWriter(fh, fieldnames=list(found[0].keys())
                                        if found else ['verdict'])
                writer.writeheader()
                writer.writerows(found)
            print(f'\n  written: {args.csv}')

        print(f'\n  {why[REFUSED]} lead(s) the gate should have refused.')
        if why[REFUSED]:
            print('  Each one is a lead created against a blocked client '
                  'while the\n  register could not be read. Decide per lead: '
                  'close it as\n  "Client blocked by management" on the '
                  'register screen, which keeps\n  the row and its history, '
                  'or lift the block if it is wrong.')
        print('\nREAD ONLY — nothing was written. No lead, register entry '
              'or audit row\nwas changed by this script.')
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
