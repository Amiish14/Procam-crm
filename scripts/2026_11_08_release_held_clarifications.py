"""The clarifications that were composed and never sent. DRY RUN.

CRM-FINAL-CLOSEOUT-01 §2: "Mail: send from tms@ with a [CRM] subject
tag (decided 2 Nov) instead of the missing crm@. Release the held
clarification mails through it."

    .venv/bin/python scripts/2026_11_08_release_held_clarifications.py --check
    .venv/bin/python scripts/2026_11_08_release_held_clarifications.py \
          --check --rfq 41
    .venv/bin/python scripts/2026_11_08_release_held_clarifications.py \
          --apply

WHAT WAS HELD, AND WHY THERE IS NOTHING TO "RESEND"

Screening finds the RFQ is missing drawings, or a site address, or a
scope, and `rfq_clarify.send` composes the question and hands it to
the outbox. With `CRM_EMAIL_ALLOW_EXTERNAL` unset, `mail_policy`
refuses every address outside Procam, the outbox declines the row and
returns None, and the step is left waiting for a customer who was
never asked.

Nothing is sitting in a queue. The outbox has no row, because the row
was refused before it was written, so this cannot be a matter of
flipping rows to `queued`. What exists is the audit trail:
`rfq.clarification_held`, one row per attempt, carrying the RFQ, the
outcome and the address it was going to. This script reads those,
recomposes each question from the RFQ as it stands today, and hands
it back to the same `rfq_clarify.send` — one code path for a
clarification sent today and one released now, because two would
disagree about the wording within a month.

Recomposed, not replayed, on purpose. The RFQ may have been corrected
since: if somebody filled in the site address by hand, the question
has changed, and sending the old one makes Procam look like it is not
reading its own file.

WHAT IT REFUSES TO SEND

Four checks, and each one exists because sending the message anyway
would reach a customer:

  * the step is no longer waiting      somebody got the answer, by
                                       phone or by hand. Asking again
                                       is worse than never asking.
  * the RFQ is gone, or has no lead    there is nothing to ask about.
  * no customer address                `recipient()` refuses internal
                                       addresses, and a clarification
                                       mailed to ourselves closes a
                                       step on nothing.
  * the address is still refused       external mail is still off.
                                       Then this reports it as STILL
                                       HELD rather than claiming a
                                       send, which is the whole point
                                       of running a check first.

It sends nothing itself. `rfq_clarify.send` enqueues, and the outbox
worker sends — so the kill switch, the notification preferences, the
retry schedule and the [CRM] subject tag all still apply, exactly as
they do for a clarification raised on the screen this afternoon.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import OrderedDict

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from app import Lead, app, db                              # noqa: E402

HELD_ACTION = 'rfq.clarification_held'
SENT_ACTION = 'rfq.clarification_sent'

#: Who the audit row is written as when this script releases one.
#: A release is a different act from a person clicking Screening, and
#: the trail should say which it was.
ACTOR = 'FINAL-CLOSEOUT-01'

SKIP_STEP = 'the step is no longer waiting for the customer'
SKIP_GONE = 'the RFQ no longer exists'
SKIP_NO_LEAD = 'the RFQ has no lead behind it'
SKIP_SENT = 'a clarification has since been sent for this outcome'


def held_attempts():
    """{(rfq_id, outcome): audit row} — the latest attempt for each.

    Keyed on the pair because one RFQ can be held for two different
    missing things, and those are two questions. Latest wins: if
    screening was run twice for the same outcome, the second attempt
    is the live one.
    """
    from app.models.audit import AuditEvent

    rows = (AuditEvent.query
            .filter(AuditEvent.action == HELD_ACTION,
                    AuditEvent.entity_type == 'rfq')
            .order_by(AuditEvent.occurred_at.asc())
            .all())
    out = OrderedDict()
    for row in rows:
        payload = row.new_value if isinstance(row.new_value, dict) else {}
        outcome = (payload.get('outcome') or '').strip()
        try:
            rfq_id = int(row.entity_id)
        except (TypeError, ValueError):
            continue
        out[(rfq_id, outcome)] = row
    return out


def already_sent():
    """{(rfq_id, outcome)} that have since gone out."""
    from app.models.audit import AuditEvent

    out = set()
    rows = (AuditEvent.query
            .filter(AuditEvent.action == SENT_ACTION,
                    AuditEvent.entity_type == 'rfq')
            .all())
    for row in rows:
        payload = row.new_value if isinstance(row.new_value, dict) else {}
        try:
            out.add((int(row.entity_id),
                     (payload.get('outcome') or '').strip()))
        except (TypeError, ValueError):
            continue
    return out


def _still_waiting(rfq):
    """Whether screening is still open and waiting on the customer.

    Read off the step the screening route actually sets, rather than
    inferred from the RFQ's own columns: `waiting_for_customer` on
    step 3 is the flag a person clears when the answer arrives, so it
    is the only thing that knows somebody got it by phone.
    """
    from app.services import rfq_chain as rc

    try:
        task = rc.steps_for(rfq).get(3)
    except Exception:                                     # noqa: BLE001
        return None
    if task is None:
        return False
    return bool(getattr(task, 'waiting_for_customer', False))


def survey():
    """One record per held clarification, with what would happen."""
    from app.models.rfq import RFQ
    from app.services import mail_policy, rfq_clarify

    sent = already_sent()
    out = []
    for (rfq_id, outcome), event in held_attempts().items():
        payload = event.new_value if isinstance(event.new_value, dict) else {}
        record = {
            'rfq_id': rfq_id,
            'outcome': outcome,
            'held_at': str(event.occurred_at)[:16],
            'held_to': (payload.get('to') or ''),
            'rfq_number': '',
            'release': False,
            'why': '',
            'to': '',
            'subject': '',
            'policy_ok': False,
        }

        rfq = RFQ.query.get(rfq_id)
        if rfq is None:
            record['why'] = SKIP_GONE
            out.append(record)
            continue
        record['rfq_number'] = rfq.rfq_number or ''

        if (rfq_id, outcome) in sent:
            record['why'] = SKIP_SENT
            out.append(record)
            continue

        waiting = _still_waiting(rfq)
        if waiting is False:
            record['why'] = SKIP_STEP
            out.append(record)
            continue

        lead = (db.session.get(Lead, rfq.lead_id) if rfq.lead_id else None)
        if lead is None:
            record['why'] = SKIP_NO_LEAD
            out.append(record)
            continue

        to, _name = rfq_clarify.recipient(rfq, lead)
        record['to'] = to
        if not to:
            record['why'] = ('no customer address on the RFQ, the lead '
                             'or the account — ask by hand')
            out.append(record)
            continue

        # Recomposed from the RFQ as it stands now. See the docstring.
        subject, _html = rfq_clarify.compose(rfq, lead, outcome)
        record['subject'] = subject

        allowed, refused = mail_policy.check([to])
        record['policy_ok'] = bool(allowed and not refused)
        if not record['policy_ok']:
            record['why'] = ('external mail is still switched off — '
                             'this would be held again, not sent')
            out.append(record)
            continue

        record['release'] = True
        out.append(record)
    return out


def report(records):
    ready = [r for r in records if r['release']]
    print(f'\n{len(records)} held clarification(s) on record; '
          f'{len(ready)} would be released.\n')
    for r in records:
        mark = '→' if r['release'] else ' '
        print(f'{mark} {r["rfq_number"] or ("rfq #" + str(r["rfq_id"])):<16}'
              f' held {r["held_at"]}  {r["outcome"]}')
        print(f'    to: {r["to"] or r["held_to"] or "—"}')
        if r['subject']:
            print(f'    subject: {r["subject"]}')
        if r['why']:
            print(f'    NOT released: {r["why"]}')
    if not records:
        print('  Nothing was ever held on this installation.')
    return ready


def release(records, *, apply):
    """Hand each one back to rfq_clarify.send."""
    from app.models.rfq import RFQ
    from app.services import rfq_clarify

    done, still_held = [], []
    for r in records:
        rfq = RFQ.query.get(r['rfq_id'])
        lead = (db.session.get(Lead, rfq.lead_id)
                if rfq is not None and rfq.lead_id else None)
        if rfq is None or lead is None:
            continue
        if not apply:
            done.append(r)
            continue
        status, _detail = rfq_clarify.send(rfq, lead, r['outcome'],
                                           actor=ACTOR, commit=False)
        if status == 'queued':
            done.append(r)
        else:
            # Said, not swallowed. A release that silently held again
            # would leave the step waiting and the report claiming it
            # had been sent.
            r['why'] = f'the outbox would not take it ({status})'
            still_held.append(r)
    if apply:
        db.session.commit()
    return done, still_held


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--check', action='store_true',
                    help='report only. The default.')
    ap.add_argument('--apply', action='store_true',
                    help='queue the releasable ones through the outbox')
    ap.add_argument('--rfq', action='append', default=None, metavar='ID',
                    help='only this RFQ id. Repeatable — how to release '
                         'one and watch what the customer does before '
                         'releasing the rest.')
    args = ap.parse_args(argv)

    wanted = set()
    for raw in (args.rfq or []):
        for part in str(raw).replace(' ', ',').split(','):
            if part:
                try:
                    wanted.add(int(part))
                except ValueError:
                    raise SystemExit(f'--rfq wants an id; got {part!r}')

    with app.app_context():
        from app.services import mailer

        print(f'database: {db.engine.url}')
        name, addr = mailer.sender()
        tag = mailer.subject_tag()
        print(f'sending as: {name} <{addr}>'
              + (f', subject tagged {tag}' if tag else
                 ', WITH NO SUBJECT TAG'))
        problem = mailer.sender_problem()
        if problem:
            print(f'  !! {problem}')
        print('CRM-FINAL-CLOSEOUT-01 §2 — held clarifications.'
              + ('' if args.apply else ' REPORT ONLY.'))

        records = survey()
        if wanted:
            records = [r for r in records if r['rfq_id'] in wanted]
            print(f'  limited to rfq {sorted(wanted)}')

        ready = report(records)

        if not args.apply:
            print('\nDRY RUN — nothing queued. --apply to release '
                  'the ones marked →.')
            if problem:
                print('Fix the sending address first: every released '
                      'mail would go out from it.')
            db.session.rollback()
            return 0

        if problem:
            print('\n  !! Refusing to release: the sending address is '
                  'wrong, and these go to customers. Fix it and run '
                  'again.')
            db.session.rollback()
            raise SystemExit(2)

        done, still_held = release(ready, apply=True)
        print(f'\n{len(done)} queued. The outbox worker sends them; '
              f'the kill switch and the subject tag still apply.')
        for r in still_held:
            print(f'  !! {r["rfq_number"]}: {r["why"]}')
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
