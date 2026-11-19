"""City and state that disagree — "Mumbai · Gujarat", "Kolkata · Jharkhand".

CRM-SEARCH-SCAN-01 fix 7 (UAT 17 Nov).

    .venv/bin/python scripts/2026_11_19_location_mismatch.py --check
    .venv/bin/python scripts/2026_11_19_location_mismatch.py --check --csv
    .venv/bin/python scripts/2026_11_19_location_mismatch.py --apply --actor DIR12010
    .venv/bin/python scripts/2026_11_19_location_mismatch.py --undo --batch LOCFIX-20261119-1

A row is a MISMATCH when its city is one `india_places` knows and its
state names a DIFFERENT state. The city is the more specific of the
two and is what a person types first, so the state is corrected to the
city's state. Rows whose city is unknown, or appears in two states
(Aurangabad), are never touched. A blank state is a gap, not a
mismatch, and is left alone. Contacts, leads and accounts.

Every write is audited with its batch id; --undo puts the old state
back on every row the batch changed, unless somebody has edited the
state since.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import Counter
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from app import app, db                                    # noqa: E402

ACTION = 'location.state_corrected'
UNDONE = 'location.state_restored'
BATCH_PREFIX = 'LOCFIX'
DEFAULT_CSV = os.path.join(_ROOT, 'data', 'location_mismatch_19NOV.csv')


def _models():
    from app import Company, Contact, Lead
    return (('contact', Contact), ('lead', Lead), ('account', Company))


def survey():
    """[(kind, row, city, stored_state, correct_state)]"""
    from app.services import india_places as ip

    out = []
    for kind, model in _models():
        q = model.query.filter(model.city.isnot(None), model.city != '',
                               model.state.isnot(None), model.state != '')
        for row in q.order_by(model.id.asc()).all():
            want = ip.state_for_city(row.city)
            if not want:
                continue
            have = ip.canonical_state(row.state) or (row.state or '').strip()
            if have.lower() == want.lower():
                continue
            out.append((kind, row, row.city, row.state, want))
    return out


def _name(kind, row):
    if kind == 'contact':
        return row.name or ''
    if kind == 'lead':
        return f'{row.pic or ""} @ {row.company or ""}'
    return row.name or ''


def report(found):
    print(f'\n══ {len(found)} row(s) whose state is not the city\'s state ══')
    for kind, n in Counter(k for k, *_ in found).most_common():
        print(f'  {n:>7}  {kind}')
    pairs = Counter((c.strip(), s.strip(), w) for _k, _r, c, s, w in found)
    print('\n  most common:')
    for (city, state, want), n in pairs.most_common(20):
        print(f'    {n:>5}  {city} · {state}  →  {want}')


def write_csv(found, path):
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', newline='') as fh:
        out = csv.writer(fh)
        out.writerow(['record', 'id', 'name', 'city', 'state_on_file',
                      'state_of_city'])
        for kind, row, city, state, want in found:
            out.writerow([kind, row.id, _name(kind, row), city, state, want])
    print(f'\nCSV: {path}')


def next_batch_id(now=None):
    from app.models.audit import AuditEvent

    now = now or datetime.utcnow()
    prefix = f'{BATCH_PREFIX}-{now.strftime("%Y%m%d")}-'
    used = set()
    for r in (AuditEvent.query.filter(AuditEvent.action == ACTION)
              .order_by(AuditEvent.id.desc()).limit(2000).all()):
        b = str((r.new_value or {}).get('batch') or '') \
            if isinstance(r.new_value, dict) else ''
        if b.startswith(prefix):
            used.add(b)
    return f'{prefix}{len(used) + 1}'


def apply(found, *, batch, actor):
    from app.services import audit

    n = 0
    try:
        for kind, row, city, state, want in found:
            if (row.state or '') != (state or ''):
                continue            # edited since the survey
            row.state = want
            audit.record(ACTION, kind, row.id,
                         old={'state': state},
                         new={'state': want, 'city': city, 'batch': batch},
                         actor=actor,
                         reason=f'batch {batch}: {city} is in {want}',
                         commit=False)
            n += 1
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    print(f'\n{n} state(s) corrected. Batch {batch}. '
          f'Undo: --undo --batch {batch}')
    return n


def undo(batch, actor):
    from app.models.audit import AuditEvent
    from app.services import audit

    models = dict(_models())
    back, left = 0, 0
    for ev in (AuditEvent.query.filter(AuditEvent.action == ACTION)
               .order_by(AuditEvent.id.asc()).all()):
        new = ev.new_value if isinstance(ev.new_value, dict) else {}
        if str(new.get('batch') or '') != batch:
            continue
        model = models.get(ev.entity_type)
        row = db.session.get(model, int(ev.entity_id)) if model else None
        if row is None:
            continue
        if (row.state or '') != (new.get('state') or ''):
            left += 1
            continue
        old = (ev.old_value or {}).get('state') \
            if isinstance(ev.old_value, dict) else None
        row.state = old
        audit.record(UNDONE, ev.entity_type, row.id,
                     old={'state': new.get('state')},
                     new={'state': old, 'batch': batch}, actor=actor,
                     reason=f'undo of batch {batch}', commit=False)
        back += 1
    db.session.commit()
    print(f'\n{back} state(s) put back; {left} left because they were '
          f'edited after the batch.')
    return back


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--undo', action='store_true')
    ap.add_argument('--batch', default=None)
    ap.add_argument('--actor', default=None)
    ap.add_argument('--csv', nargs='?', const=DEFAULT_CSV, default=None)
    args = ap.parse_args(argv)
    with app.app_context():
        if args.undo:
            if not args.batch:
                ap.error('--undo needs --batch')
            return undo(args.batch, args.actor or 'DIR12010')
        found = survey()
        report(found)
        if args.csv:
            write_csv(found, args.csv)
        if args.apply:
            if not args.actor:
                ap.error('--apply needs --actor')
            return apply(found, batch=next_batch_id(), actor=args.actor)
        print('\nDry run. Nothing written. Add --apply --actor DIR12010 '
              'to correct these.')
    return 0


if __name__ == '__main__':
    sys.exit(main() and 0)
