"""
Seven lost reasons, and every lead moved onto one of them.

    .venv/bin/python scripts/2026_10_25_lost_reason_mapping.py --check
    .venv/bin/python scripts/2026_10_25_lost_reason_mapping.py --apply
    .venv/bin/python scripts/2026_10_25_lost_reason_mapping.py --down --yes

Twenty-three reasons collapse to seven, decided 25 October. The
amendments to the first proposal are the interesting part:

    "Not interested" stays its own reason. A customer who says it
    has answered, and folding 1,213 of those into silence would
    lose the difference between a no and a non-reply — the one
    distinction a sales head actually acts on.

    "Client Decision" joins it rather than "No response": it
    describes a decision taken, not a silence.

    "Duplicate / Invalid" stops being a lost reason at all. A
    duplicate is not a commercial outcome. Where the text names
    the lead it duplicates, the lead is marked duplicate and
    pointed at it; where it names nothing, it falls to Others.

Free text is left alone. 1,336 leads carry a sentence rather than
a category — "Restored — originally deleted: …", "Others:
newsletter, not a client" — and those are records of what
happened, not vocabulary. Rewriting them would destroy the only
account of why those leads are closed.

`--down` restores every lead's original reason from the audit
trail and puts the old Master Data list back.
"""
import argparse
import os
import re
import sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from sqlalchemy import text                               # noqa: E402

from app import Lead, app, db                             # noqa: E402
from app.models.master_data import MasterItem             # noqa: E402

LIST_KEY = 'lost_reason'
ACTION = 'lead.lost_reason_mapped'
ACTION_LIST = 'master_list.reduced'

#: The seven. Order is the order they appear in the dropdown.
FINAL = [
    'No response',
    'Not interested',
    'Price',
    'Lost to competitor',
    'Customer cancelled or postponed',
    'We could not serve it',
    'Others',
]

MAPPING = {
    'No Response':                      'No response',
    'No Response from Client':          'No response',

    'Not interested':                   'Not interested',
    'Client Decision':                  'Not interested',

    'Price':                            'Price',
    'Price too high':                   'Price',
    'Commercial Terms':                 'Price',
    'Payment terms mismatch':           'Price',
    'Not commercially viable':          'Price',

    'Competition':                      'Lost to competitor',
    'Lost to competitor':               'Lost to competitor',
    'Client selected in-house transport': 'Lost to competitor',

    'Project Cancelled':                'Customer cancelled or postponed',
    'Project Postponed':                'Customer cancelled or postponed',
    'Customer postponed / on hold':     'Customer cancelled or postponed',
    'Timeline mismatch':                'Customer cancelled or postponed',

    'Technical / Capability':           'We could not serve it',
    'Vehicle/capacity unavailable':     'We could not serve it',
    'Service Constraint':               'We could not serve it',
    'Geography Constraint':             'We could not serve it',
    'Documentation / compliance issue': 'We could not serve it',

    'Other':                            'Others',
}

#: Not a lost reason. Handled separately.
DUPLICATE = 'Duplicate / Invalid'

RX_TARGET = re.compile(r'#\s*(\d{3,7})')

#: A sentence, not a category — left exactly as written.
MARKS = (':', '—', '–', '#')


def is_free_text(reason):
    reason = (reason or '').strip()
    return len(reason) > 40 or any(m in reason for m in MARKS)


def plan():
    rows = db.session.execute(text(
        "SELECT lost_reason, COUNT(*) FROM leads "
        "WHERE lost_reason IS NOT NULL AND TRIM(lost_reason) <> '' "
        "GROUP BY lost_reason")).fetchall()
    move, dupes, freetext, already = Counter(), [], Counter(), Counter()
    for reason, n in rows:
        reason = (reason or '').strip()
        if reason in FINAL:
            already[reason] += n
        elif reason == DUPLICATE:
            dupes.append((reason, n))
        elif is_free_text(reason):
            freetext[reason] += n
        elif reason in MAPPING:
            move[(reason, MAPPING[reason])] += n
        else:
            move[(reason, 'Others')] += n
    return move, dupes, freetext, already


def duplicate_leads():
    """Leads on 'Duplicate / Invalid', and the lead each names."""
    out = []
    for lead in Lead.query.filter(Lead.lost_reason == DUPLICATE).all():
        hit = RX_TARGET.search(f'{lead.lost_reason or ""} '
                               f'{lead.notes or ""}')
        out.append((lead, int(hit.group(1)) if hit else None))
    return out


def apply_plan(*, actor='DIR12010'):
    from app.services import audit

    moved = 0
    for lead in Lead.query.filter(
            Lead.lost_reason.isnot(None)).all():
        reason = (lead.lost_reason or '').strip()
        if not reason or reason in FINAL or is_free_text(reason) \
                or reason == DUPLICATE:
            continue
        target = MAPPING.get(reason, 'Others')
        lead.lost_reason = target
        audit.record(ACTION, 'lead', lead.id,
                     old={'lost_reason': reason},
                     new={'lost_reason': target}, actor=actor,
                     reason='lost reasons reduced to seven')
        moved += 1

    marked = 0
    for lead, target_id in duplicate_leads():
        was = lead.lost_reason
        if target_id:
            lead.lost_reason = f'Others: duplicate of #{target_id}'
            why = f'duplicate of #{target_id}, not a commercial loss'
        else:
            lead.lost_reason = 'Others'
            why = 'marked invalid with no lead named; Others'
        audit.record(ACTION, 'lead', lead.id,
                     old={'lost_reason': was},
                     new={'lost_reason': lead.lost_reason},
                     actor=actor, reason=why)
        marked += 1
    return moved, marked


def reduce_list(*, actor='DIR12010'):
    """Master Data holds only the seven."""
    from app.services import audit

    before = [i.code for i in MasterItem.query.filter_by(
        list_key=LIST_KEY).all()]
    MasterItem.query.filter_by(list_key=LIST_KEY).delete(
        synchronize_session=False)
    for order, code in enumerate(FINAL):
        db.session.add(MasterItem(list_key=LIST_KEY, code=code,
                                  label=code, sort_order=order,
                                  is_active=True))
    audit.record(ACTION_LIST, 'master_list', LIST_KEY,
                 old={'codes': sorted(before)}, new={'codes': FINAL},
                 actor=actor, reason='lost reasons reduced to seven')
    return len(before), len(FINAL)


def undo(*, actor='DIR12010'):
    from app.models.audit import AuditEvent
    from app.services import audit

    back = 0
    for ev in (AuditEvent.query
               .filter(AuditEvent.action == ACTION,
                       AuditEvent.entity_type == 'lead')
               .order_by(AuditEvent.id.desc()).all()):
        try:
            lead = db.session.get(Lead, int(ev.entity_id))
        except (TypeError, ValueError):
            continue
        if lead is None:
            continue
        was = (ev.old_value or {}).get('lost_reason')
        if was:
            lead.lost_reason = was
            back += 1

    ev = (AuditEvent.query.filter_by(action=ACTION_LIST)
          .order_by(AuditEvent.id.desc()).first())
    codes = (getattr(ev, 'old_value', None) or {}).get('codes') or []
    if codes:
        MasterItem.query.filter_by(list_key=LIST_KEY).delete(
            synchronize_session=False)
        for code in codes:
            db.session.add(MasterItem(list_key=LIST_KEY, code=code,
                                      label=code, is_active=True))
    audit.record('lead.lost_reason_mapping_reverted', 'lead', None,
                 actor=actor, reason=f'{back} lead(s) restored')
    return back, len(codes)


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
        print(f'database: {db.engine.url}')
        if args.down:
            if not args.yes:
                raise SystemExit('--down needs --yes.')
            leads, codes = undo(actor=args.actor)
            db.session.commit()
            print(f'  {leads} lead(s) restored, {codes} reason(s) '
                  f'put back in Master Data')
            return

        print(f'MODE:     {"APPLY" if args.apply else "DRY RUN"}\n')
        move, dupes, freetext, already = plan()

        print('  where each lead ends up:')
        totals = Counter()
        for (was, target), n in move.items():
            totals[target] += n
        for target, n in already.items():
            totals[target] += n
        for target in FINAL:
            print(f'    {target:<34}{totals.get(target, 0):>7}')
        print(f'    {"(free text, untouched)":<34}'
              f'{sum(freetext.values()):>7}')

        print('\n  the moves:')
        for (was, target), n in sorted(move.items(),
                                       key=lambda kv: -kv[1]):
            print(f'    {was:<34}-> {target:<34}{n:>6}')
        if not move:
            print('    (nothing to move)')

        rows = duplicate_leads()
        named = sum(1 for _l, t in rows if t)
        print(f'\n  "{DUPLICATE}": {len(rows)} lead(s) — '
              f'{named} name a target lead, {len(rows) - named} do not')

        print(f'\n  Master Data: {MasterItem.query.filter_by(list_key=LIST_KEY).count()}'
              f' reason(s) today -> {len(FINAL)}')

        if args.check:
            print('\nDRY RUN — nothing written.')
            return

        moved, marked = apply_plan(actor=args.actor)
        before, now = reduce_list(actor=args.actor)
        db.session.commit()
        print(f'\n  {moved} lead(s) remapped, {marked} duplicate(s) '
              f'handled, Master Data {before} -> {now}. '
              f'`--down --yes` restores both.')


if __name__ == '__main__':
    main()
