"""
Give the 1-3 September batch back to the customers it was sent to.

    .venv/bin/python scripts/recover_outbound_batch.py --check
    .venv/bin/python scripts/recover_outbound_batch.py --apply
    .venv/bin/python scripts/recover_outbound_batch.py --down --yes

167 messages a Procam employee sent *to* a customer were ingested
as 167 new leads under the placeholder "Unknown". They are live
RFQ and quotation threads. The customer was never missing — it
was in the To: line, which nothing read.

Three outcomes, and none of them closes anything:

    linked      the recipient resolves to an account. The lead is
                linked, the owner for that vertical is set, and
                the recipient is taught to the matcher so the next
                message from that thread places itself.

    suggested   the recipient is external but on no account yet.
                The lead goes to Account check with the address
                and a proposed name, so the person has what we
                worked out instead of starting again.

    flagged     no external recipient at all — internal mail that
                should probably not be a lead. Marked for review.
                Deliberately not closed: "probably not a lead" is
                a judgement, and 51 silent closures is how real
                work disappears.
"""
import argparse
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Lead, app, db                             # noqa: E402

PLACEHOLDER = 983
START, END = '2026-09-01', '2026-09-03'
ACTION_LINK = 'lead.account_recovered_from_recipient'
ACTION_FLAG = 'lead.flagged_not_a_lead'


def batch():
    from sqlalchemy import func
    return (Lead.query
            .filter(Lead.company_id == PLACEHOLDER,
                    Lead.source == 'email',
                    func.date(Lead.created_at) >= START,
                    func.date(Lead.created_at) <= END)
            .order_by(Lead.id).all())


def classify(lead):
    """('linked'|'suggested'|'flagged', account, address, name)."""
    from app.services import outbound_mail as om

    body = (lead.original_email_body or lead.notes or '')
    _dir, _sender, external = om.direction({}, body=body)
    if not external:
        return 'flagged', None, '', ''
    account, _rule, address = om.customer_of(external)
    if account is not None:
        return 'linked', account, address, account.name
    from app.services import account_match
    domain = account_match.domain_of(address)
    name = domain.split('.')[0].title() if domain else ''
    return 'suggested', None, address, name


def apply_plan(rows, *, actor='DIR12010'):
    from app.models.assignment import LeadAccountSuggestion
    from app.services import account_gate, account_match, audit
    from app.services import account_suggest

    done = Counter()
    for kind, lead, account, address, name in rows:
        if kind == 'linked':
            was = lead.company_id
            lead.company_id = account.id
            vertical = account_gate.vertical_of(lead)
            owner = account_gate.owner_for(account, vertical)
            if owner:
                account_suggest._assign(lead, owner)
            try:
                account_match.learn(address, account.id,
                                    lead_id=lead.id, actor=actor,
                                    commit=False)
            except Exception:                             # noqa: BLE001
                pass
            audit.record(ACTION_LINK, 'lead', lead.id,
                         old={'company_id': was},
                         new={'company_id': account.id,
                              'account': account.name,
                              'assigned_to': owner or lead.assigned_to},
                         actor=actor,
                         reason=f'our own mail to {address}; the '
                                f'recipient named the customer')
            done['linked'] += 1

        elif kind == 'suggested':
            row = LeadAccountSuggestion.query.filter_by(
                lead_id=lead.id).first()
            if row is None:
                row = LeadAccountSuggestion(lead_id=lead.id)
                db.session.add(row)
            row.account_id = None
            row.rule = 'outbound recipient'
            row.confidence = 0
            row.candidate_count = 0
            row.exact_single = False
            row.proposed_domain = account_match.domain_of(address)
            row.proposed_name = name
            row.sender_seen = address[:320]
            row.computed_at = datetime.utcnow()
            done['suggested'] += 1

        else:
            lead.needs_review = True
            audit.record(ACTION_FLAG, 'lead', lead.id,
                         new={'needs_review': True}, actor=actor,
                         reason='our own internal mail with no '
                                'external recipient — probably not '
                                'a lead; for a person to confirm, '
                                'not closed here')
            done['flagged'] += 1
    return done


def undo(*, actor='DIR12010'):
    from app.models.audit import AuditEvent
    from app.services import audit

    back = 0
    for ev in AuditEvent.query.filter(
            AuditEvent.action.in_((ACTION_LINK, ACTION_FLAG))).all():
        try:
            lead = db.session.get(Lead, int(ev.entity_id))
        except (TypeError, ValueError):
            continue
        if lead is None:
            continue
        if ev.action == ACTION_LINK:
            lead.company_id = (ev.old_value or {}).get('company_id')
        else:
            lead.needs_review = False
        audit.record('lead.outbound_recovery_reverted', 'lead',
                     lead.id, actor=actor, reason='recovery reverted')
        back += 1
    return back


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--down', action='store_true')
    ap.add_argument('--yes', action='store_true')
    ap.add_argument('--actor', default='DIR12010')
    args = ap.parse_args()
    if len([a for a in (args.check, args.apply, args.down) if a]) != 1:
        raise SystemExit('Choose one: --check, --apply or --down.')

    with app.app_context():
        from app.services import account_gate

        print(f'database: {db.engine.url}')
        if args.down:
            if not args.yes:
                raise SystemExit('--down needs --yes.')
            n = undo(actor=args.actor)
            db.session.commit()
            print(f'  {n} lead(s) put back')
            return

        print(f'MODE:     {"APPLY" if args.apply else "DRY RUN"}\n')
        rows = []
        for lead in batch():
            kind, account, address, name = classify(lead)
            rows.append((kind, lead, account, address, name))

        counts = Counter(k for k, *_rest in rows)
        print(f'  batch                {len(rows)}')
        for key in ('linked', 'suggested', 'flagged'):
            print(f'    {key:<18}{counts.get(key, 0):>5}')

        print('\n  per vertical and head:')
        heads = {}
        per = defaultdict(Counter)
        for kind, lead, _a, _ad, _n in rows:
            per[account_gate.vertical_of(lead) or '(none)'][kind] += 1
        for vertical, c in sorted(per.items(),
                                  key=lambda kv: -sum(kv[1].values())):
            if vertical not in heads:
                try:
                    from app.services import vertical_heads as vh
                    found = vh.heads_for_vertical(vertical) or []
                    heads[vertical] = found[0] if found else '—'
                except Exception:                         # noqa: BLE001
                    heads[vertical] = '—'
            print(f'    {vertical[:22]:<24}{heads[vertical]:<13}'
                  f'linked {c.get("linked", 0):>4}   '
                  f'suggested {c.get("suggested", 0):>4}   '
                  f'flagged {c.get("flagged", 0):>4}')

        print('\n  customers recovered:')
        for name, n in Counter(
                n for k, _l, _a, _ad, n in rows
                if k == 'linked').most_common(12):
            print(f'    {n:>4}  {name}')

        if args.check:
            print('\nDRY RUN — nothing written. Nothing is closed by '
                  'this script in any mode.')
            return

        done = apply_plan(rows, actor=args.actor)
        db.session.commit()
        print(f'\n  linked {done["linked"]}, suggested '
              f'{done["suggested"]}, flagged {done["flagged"]}. '
              f'No lead was closed.')


if __name__ == '__main__':
    main()
