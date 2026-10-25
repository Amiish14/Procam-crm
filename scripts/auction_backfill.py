"""
CRM-FORWARD-01b — mark the auctions that are already here.

    .venv/bin/python scripts/auction_backfill.py --check
    .venv/bin/python scripts/auction_backfill.py --apply
    .venv/bin/python scripts/auction_backfill.py --down --yes

Classification runs at ingest, so the 254 that arrived before it
existed carry no `lead_type` and are protected by nothing. This
marks them.

What it will not do
    Reopen anything. 245 of the 254 are closed and they stay
    closed: the type is what changes, not the stage. A backfill
    that pushed 245 dead leads back into the pipeline would be a
    worse day than the one that deleted them.

The closing date is read only where the message states one
    Four of the nine open auctions carry a date in the subject.
    Two of those are "Eta at Haldia 20.10.26" — the cargo's
    arrival, not the auction's deadline — and presenting that as a
    closing time would send somebody to bid on a date that means
    nothing. A date is taken only from an auction context: a
    reverse-auction line, or "best rate up to <date>". Otherwise
    the row says the deadline is in the portal, which is true.
"""
import argparse
import os
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Lead, app, db                             # noqa: E402
from app.services import lead_kinds, sales_rules           # noqa: E402

ACTION = 'lead.type_backfilled'
DIGEST_EVENT = 'auction.backfill_digest'
DAYS = 90

#: A date that is an auction deadline, not a cargo ETA. The
#: context word has to be there: "reverse auction | 05/10/2026",
#: "best rate up to 14.09.2026", "auction start date-time".
RX_CLOSING = (
    re.compile(r'auction[^|\n]{0,40}\|\s*(\d{1,2}[/.]\d{1,2}[/.]\d{2,4})',
               re.I),
    re.compile(r'(?:best rate|quote|bid|offer)s?\s+(?:up\s*to|by|'
               r'before|till|until)\s*:?\s*'
               r'(\d{1,2}[/.]\d{1,2}[/.]\d{2,4})', re.I),
    re.compile(r'auction\s*(?:start)?\s*date[^0-9]{0,12}'
               r'(\d{1,2}[/.]\d{1,2}[/.]\d{2,4})', re.I),
)
RX_TIME = re.compile(r'@\s*(\d{1,2}[:.]\d{2}\s*(?:AM|PM))', re.I)


def subject_of(lead):
    for value in (lead.forwarded_subject, lead.original_email_subject):
        if (value or '').strip():
            return value
    row = db.session.execute(db.text(
        'SELECT subject FROM email_events WHERE '
        'internet_message_id = :m ORDER BY id DESC LIMIT 1'),
        {'m': lead.email_message_id or ''}).fetchone()
    return (row[0] if row else '') or ''


def closing_of(lead, subject):
    """(date, time, where) or (None, '', 'not stated')."""
    body = re.sub(r'\s+', ' ', (lead.original_email_body
                                or lead.notes or ''))[:1200]
    for text, where in ((subject, 'the subject'),
                        (body, 'the message')):
        for rx in RX_CLOSING:
            hit = rx.search(text or '')
            if not hit:
                continue
            raw = hit.group(1).replace('.', '/')
            for fmt in ('%d/%m/%Y', '%d/%m/%y'):
                try:
                    when = datetime.strptime(raw, fmt).date()
                except ValueError:
                    continue
                clock = RX_TIME.search(text or '')
                return (when, clock.group(1) if clock else '', where)
    return (None, '', 'not stated — the deadline is in the portal')


def survey(days=DAYS):
    since = datetime.utcnow() - timedelta(days=days)
    rules = lead_kinds.patterns()
    rows = []
    for lead in (Lead.query
                 .filter(Lead.source == 'email',
                         Lead.created_at >= since)
                 .order_by(Lead.id.asc()).all()):
        subject = subject_of(lead)
        kind, platform, why = lead_kinds.classify(
            subject=subject,
            sender=(lead.original_email_from or lead.email or ''),
            rules=rules)
        if not kind:
            continue
        # sales_rules owns what "open" means, and an archived lead
        # is not live however its stage reads.
        is_open = (sales_rules.is_open(lead.stage)
                   and not lead.is_archived)
        when, clock, where = (closing_of(lead, subject)
                              if is_open else (None, '', ''))
        rows.append({
            'lead': lead, 'open': is_open, 'platform': platform,
            'subject': subject, 'closing': when, 'clock': clock,
            'closing_from': where,
            'overdue': bool(when and when < date.today()),
            'already': (lead.lead_type or '') == lead_kinds.LEAD_TYPE,
        })
    return rows


def apply_rows(rows, *, actor='CRM-FORWARD-01b'):
    from app.services import audit

    owner = lead_kinds.owner()
    typed, assigned, watched = [], [], []
    for row in rows:
        lead = row['lead']
        if row['already']:
            continue
        was_type = lead.lead_type
        lead.lead_type = lead_kinds.LEAD_TYPE
        lead.lead_platform = row['platform'] or lead.lead_platform
        changes = {'lead_type': lead.lead_type,
                   'lead_platform': lead.lead_platform}

        # Only the open ones get an owner. A closed lead keeps the
        # person who closed it; reassigning 245 dead leads would
        # tell somebody they own work that ended weeks ago.
        if row['open'] and owner:
            current = (lead.assigned_to or '').strip()
            if current and current != owner:
                # The person who had it keeps sight of it rather
                # than losing it silently.
                lead.secondary_owner = current
                lead.secondary_owner_name = lead.assigned_name
                watched.append((lead, current))
                changes['secondary_owner'] = current
            if current != owner:
                lead.assigned_to = owner
                lead.assigned_name = _name_of(owner)
                changes['assigned_to'] = owner
                assigned.append(lead)

        audit.record(ACTION, 'lead', str(lead.id),
                     old={'lead_type': was_type},
                     new=changes, actor=actor,
                     reason='an auction or spot load that arrived '
                            'before the CRM could tell; marked so '
                            'the sweeps leave it alone. The stage '
                            'is untouched.')
        typed.append(lead)
    return typed, assigned, watched


def _name_of(emp_code):
    from app import Employee
    row = Employee.query.filter_by(emp_code=emp_code).first()
    return (row.name if row else '') or ''


def send_digest(rows, *, actor='CRM-FORWARD-01b'):
    """One list, once, to the owner. Returns the outbox row or None."""
    from app import Employee
    from app.services import outbox

    owner = lead_kinds.owner()
    if not owner:
        return None
    emp = Employee.query.filter_by(emp_code=owner).first()
    to = (getattr(emp, 'email', '') or '').strip()
    if not to:
        return None

    live = [r for r in rows if r['open']]
    if not live:
        return None
    overdue = [r for r in live if r['overdue']]

    def line(r):
        lead = r['lead']
        if r['closing']:
            when = (f"closing {r['closing']:%d %b %Y}"
                    + (f" at {r['clock']}" if r['clock'] else ''))
            if r['overdue']:
                when += ' — <b>already passed</b>'
        else:
            when = r['closing_from']
        return (f"<li><b>#{lead.id}</b> {_esc(r['subject'][:110])}"
                f"<br><small>{_esc(r['platform'] or 'platform not '
                                   'identified')} · {when}</small></li>")

    html = [
        f'<p>{len(live)} auction or spot-load enquiries were in the '
        f'CRM before it could recognise them. They are yours now, '
        f'and the junk sweep will leave them alone.</p>',
    ]
    if overdue:
        html.append(
            f'<p><b>{len(overdue)} of them closed before today.</b> '
            f'Those need closing as bid or not bid, with a reason — '
            f'that is what tells us which lanes are worth '
            f'bidding.</p>')
    html.append('<ul>' + ''.join(line(r) for r in live) + '</ul>')
    html.append('<p>Sent once, as a catch-up. New ones will reach '
                'you as they arrive.</p>')

    return outbox.enqueue(
        to=to,
        subject=f'{len(live)} auction enquiries now assigned to you',
        html='\n'.join(html),
        # One-time: keyed on the set, so a re-run cannot send twice.
        dedupe_key=outbox.make_key(DIGEST_EVENT,
                                   *sorted(r['lead'].id for r in live)),
        event_key=DIGEST_EVENT, user_code=owner,
        # The caller commits. Letting the outbox commit would put
        # the lead changes beyond the rollback below.
        commit=False)


def _esc(text):
    return (str(text or '').replace('&', '&amp;')
            .replace('<', '&lt;').replace('>', '&gt;'))


def undo(*, actor='CRM-FORWARD-01b'):
    from app.models.audit import AuditEvent
    from app.services import audit

    cleared = 0
    for ev in (AuditEvent.query
               .filter(AuditEvent.action == ACTION,
                       AuditEvent.entity_type == 'lead').all()):
        try:
            lead = db.session.get(Lead, int(ev.entity_id))
        except (TypeError, ValueError):
            continue
        if lead is None:
            continue
        before = (ev.old_value or {}).get('lead_type')
        lead.lead_type = before
        audit.record('lead.type_backfill_reverted', 'lead',
                     str(lead.id), actor=actor,
                     reason='backfill reverted; the stage was never '
                            'touched')
        cleared += 1
    return cleared


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--down', action='store_true')
    ap.add_argument('--yes', action='store_true')
    ap.add_argument('--days', type=int, default=DAYS)
    ap.add_argument('--actor', default='CRM-FORWARD-01b')
    args = ap.parse_args()
    if len([a for a in (args.check, args.apply, args.down) if a]) != 1:
        raise SystemExit('Choose one: --check, --apply or --down.')

    with app.app_context():
        print(f'database: {db.engine.url}')
        mode = ('APPLY' if args.apply else 'DOWN' if args.down
                else 'DRY RUN — nothing is written')
        print(f'MODE:     {mode}\n')

        if args.down:
            if not args.yes:
                raise SystemExit('--down needs --yes.')
            n = undo(actor=args.actor)
            db.session.commit()
            print(f'  {n} lead(s) had their type cleared. No stage '
                  f'was changed by the apply, so none is changed '
                  f'back.')
            return

        rows = survey(args.days)
        live = [r for r in rows if r['open']]
        closed = [r for r in rows if not r['open']]
        owner = lead_kinds.owner()

        print(f'  matching leads                {len(rows)}')
        print(f'    open                        {len(live)}')
        print(f'    closed (stage untouched)    {len(closed)}')
        print(f'    already typed               '
              f'{sum(1 for r in rows if r["already"])}')
        print(f'  auction owner                 {owner or "NOT SET"}')

        print('\n  by platform:')
        for key, n in Counter(r['platform'] or '(unidentified)'
                              for r in rows).most_common():
            print(f'    {n:>4}  {key}')
        print('\n  closed ones, by reason:')
        for key, n in Counter(
                (r['lead'].lost_reason or '(none)')[:58]
                for r in closed).most_common(5):
            print(f'    {n:>4}  {key}')

        print(f'\n  the {len(live)} open ones:')
        for r in sorted(live, key=lambda x: x['lead'].id):
            lead = r['lead']
            when = (f"{r['closing']:%d %b %Y}"
                    + (f" {r['clock']}" if r['clock'] else '')
                    if r['closing'] else r['closing_from'])
            flag = '  << CLOSED ALREADY' if r['overdue'] else ''
            print(f"    #{lead.id:<7}{(lead.company or '')[:16]:<18}"
                  f"{(r['platform'] or '?'):<13}"
                  f"owner={(lead.assigned_to or '-'):<11}{when}{flag}")
            print(f"           {r['subject'][:92]}")

        overdue = [r for r in live if r['overdue']]
        if overdue:
            print(f'\n  {len(overdue)} closed before today and need '
                  f'a bid / not-bid outcome.')
        undated = [r for r in live if not r['closing']]
        if undated:
            print(f'  {len(undated)} state no deadline in the mail — '
                  f'it is in the portal, not guessed at here.')
            # Name the ones that DO carry a date we refused to read,
            # so "no deadline" cannot be mistaken for "no dates in
            # the mail". A hardcoded sentence here was wrong the
            # first time the set changed.
            RX_ANY = re.compile(r'\d{1,2}[/.]\d{1,2}[/.]\d{2,4}')
            for r in undated:
                hit = RX_ANY.search(r['subject'] or '')
                if hit:
                    print(f"    #{r['lead'].id} carries "
                          f"{hit.group(0)}, but not as a deadline — "
                          f"read the subject before trusting it")

        if args.check:
            print('\nDRY RUN — nothing written. `--apply` sets the '
                  'type on all of them, assigns the open ones, and '
                  'sends the owner one list. No stage changes, '
                  'ever.')
            return

        if not owner:
            raise SystemExit(
                'No auction owner is set. Set '
                'admin_settings.auction_lead_owner first — '
                'assigning these to nobody is how the last 226 were '
                'swept.')

        typed, assigned, watched = apply_rows(rows, actor=args.actor)
        queued = send_digest(rows, actor=args.actor)
        try:
            db.session.commit()
        except Exception as exc:                          # noqa: BLE001
            db.session.rollback()
            print(f'  !! ROLLED BACK: {type(exc).__name__}: '
                  f'{str(exc)[:200]}')
            raise SystemExit(1)

        print(f'\n  {len(typed)} lead(s) typed, each with an audit '
              f'row. No stage was changed.')
        print(f'  {len(assigned)} open one(s) assigned to {owner}.')
        for lead, was in watched:
            print(f'    #{lead.id}: {was} kept as watcher')
        print(f'  digest: '
              + ('queued to the owner' if queued
                 else 'not queued — no live ones, or no address on '
                      'file'))


if __name__ == '__main__':
    main()
