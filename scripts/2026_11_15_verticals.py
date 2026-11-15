"""Thirteen spellings of ten desks, made one.

CRM-SEARCH-SCAN-01 §3. A filter cannot AND across spellings: somebody
choosing "Heavy Transport" and getting two thirds of the heavy
transport leads is worse served than by no filter at all, because the
answer looks complete.

    .venv/bin/python scripts/2026_11_15_verticals.py --check
    .venv/bin/python scripts/2026_11_15_verticals.py --check --csv
    .venv/bin/python scripts/2026_11_15_verticals.py --apply --actor DIR12010
    .venv/bin/python scripts/2026_11_15_verticals.py --undo --batch VERT-20261115-1

THE MAPPING IS PROPOSED, NOT ASSUMED

`--check` prints every spelling in the data beside the canonical name
`vertical_master.canonical` proposes for it, with the row count, and
**lists separately anything it will not place**. Those are left
exactly as they are: "I cannot tell which desk this is" and "this is
the Other desk" are different answers, and a sweep that files the
first under the second loses the question.

WHAT IT TOUCHES

`Lead.procam_vertical` and `Company.vertical`, and it seeds the ten
into Admin → Master data so the filter and every form offer the same
list. An audit row per lead and per account, carrying the spelling it
replaced, so `--undo --batch` puts the old words back.
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from app import app, db                                    # noqa: E402

ACTOR = 'CRM-SEARCH-SCAN-01'
ACTION = 'vertical.respelled'
UNDONE = 'vertical.respell_undone'
BATCH_PREFIX = 'VERT'
DEFAULT_CSV = os.path.join(_ROOT, 'data', 'verticals_15NOV.csv')

#: (model, column, what it is called in the report)
TARGETS = (('Lead', 'procam_vertical', 'leads'),
           ('Company', 'vertical', 'accounts'))


def _rule(n=76):
    print('─' * n)


def _model(name):
    import app as _app

    return getattr(_app, name)


def survey():
    """{(model, column): {spelling: (count, proposed)}}"""
    from app.services import vertical_master as vm

    out = {}
    for model_name, column, _what in TARGETS:
        model = _model(model_name)
        col = getattr(model, column, None)
        if col is None:
            continue
        counts = Counter()
        for (value,) in db.session.query(col).all():
            text = (value or '').strip()
            if text:
                counts[text] += 1
        out[(model_name, column)] = {
            spelling: (n, vm.canonical(spelling))
            for spelling, n in counts.items()}
    return out


def report(found):
    from app.services import vertical_master as vm

    print('\n══ the spellings in the data, and what each becomes ══')
    unplaced = defaultdict(list)
    changes = 0
    for (model_name, column), rows in found.items():
        what = dict((m, w) for m, c, w in TARGETS)[model_name]
        print(f'\n  {model_name}.{column} — {len(rows)} spelling(s) '
              f'across the {what}')
        for spelling, (n, proposed) in sorted(
                rows.items(), key=lambda kv: -kv[1][0]):
            if not proposed:
                unplaced[(model_name, column)].append((spelling, n))
                continue
            mark = '   (already)' if proposed == spelling else ''
            if proposed != spelling:
                changes += n
            print(f'    {n:>7}  {spelling[:34]:<34} → {proposed}{mark}')

    if unplaced:
        print('\n  !! NOT PLACED — left exactly as they are:')
        for (model_name, column), rows in unplaced.items():
            for spelling, n in sorted(rows, key=lambda kv: -kv[1]):
                print(f'    {n:>7}  {model_name}.{column}: {spelling!r}')
        print('    Each of these is either a desk that belongs on the '
              'master list, or a')
        print('    typo for one. Tell me which and it goes in '
              '`vertical_master.ALIASES`;')
        print('    nothing here guesses.')

    print(f'\n  {changes} row(s) would be rewritten.')
    print(f'  The master list would hold: '
          f'{", ".join(vm.CANONICAL)}')
    return changes


def write_csv(found, path):
    buf = io.StringIO()
    out = csv.writer(buf)
    out.writerow(['model', 'column', 'spelling', 'rows', 'becomes',
                  'placed'])
    for (model_name, column), rows in found.items():
        for spelling, (n, proposed) in sorted(rows.items()):
            out.writerow([model_name, column, spelling, n,
                          proposed or '', 'yes' if proposed else 'NO'])
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', newline='') as fh:
        fh.write(buf.getvalue())
    print(f'\nCSV: {path}')


def next_batch_id(now=None):
    from app.models.audit import AuditEvent

    now = now or datetime.utcnow()
    prefix = f'{BATCH_PREFIX}-{now.strftime("%Y%m%d")}-'
    try:
        rows = (AuditEvent.query.filter(AuditEvent.action == ACTION)
                .order_by(AuditEvent.id.desc()).limit(500).all())
    except Exception:                                     # noqa: BLE001
        rows = []
    used = {str((r.new_value or {}).get('batch') or '')
            for r in rows if isinstance(r.new_value, dict)}
    used = {b for b in used if b.startswith(prefix)}
    return f'{prefix}{len(used) + 1}'


def apply(*, batch, actor):
    from app.services import audit, vertical_master as vm

    done = Counter()
    try:
        added = vm.seed(actor=actor)
        if added:
            print(f'  master list: added {", ".join(added)}')
        for model_name, column, what in TARGETS:
            model = _model(model_name)
            col = getattr(model, column, None)
            if col is None:
                continue
            entity = 'lead' if model_name == 'Lead' else 'company'
            for row in model.query.filter(col.isnot(None)).all():
                was = (getattr(row, column) or '').strip()
                if not was:
                    continue
                now = vm.canonical(was)
                if not now or now == was:
                    done['left as it is' if now else 'not placed'] += 1
                    continue
                setattr(row, column, now)
                audit.record(ACTION, entity, row.id,
                             old={column: was}, new={column: now,
                                                     'batch': batch},
                             actor=actor,
                             reason=(f'batch {batch}: one spelling per '
                                     f'vertical, so a filter can find '
                                     f'all of them'),
                             commit=False)
                done[f'{what} rewritten'] += 1
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    print()
    for what, n in done.most_common():
        print(f'  {n:>7}  {what}')
    print(f'\nBatch {batch}. Undo: --undo --batch {batch}')
    return done


def undo(batch):
    from app.models.audit import AuditEvent
    from app.services import audit

    rows = (AuditEvent.query.filter(AuditEvent.action == ACTION)
            .order_by(AuditEvent.id.asc()).all())
    back, left = 0, 0
    for row in rows:
        payload = row.new_value if isinstance(row.new_value, dict) else {}
        if str(payload.get('batch') or '') != batch:
            continue
        model = _model('Lead' if row.entity_type == 'lead' else 'Company')
        column = ('procam_vertical' if row.entity_type == 'lead'
                  else 'vertical')
        record = db.session.get(model, int(row.entity_id))
        if record is None:
            continue
        if (getattr(record, column) or '') != (payload.get(column) or ''):
            left += 1
            continue
        old = row.old_value if isinstance(row.old_value, dict) else {}
        setattr(record, column, old.get(column))
        audit.record(UNDONE, row.entity_type, record.id,
                     old={column: payload.get(column)},
                     new={column: old.get(column), 'batch': batch},
                     actor=ACTOR, reason=f'undo of batch {batch}',
                     commit=False)
        back += 1
    db.session.commit()
    print(f'\n{back} row(s) put back; {left} changed since the batch '
          f'and left alone.')
    return back


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--undo', action='store_true')
    ap.add_argument('--batch', default=None, metavar='ID')
    ap.add_argument('--actor', default=None, metavar='EMP_CODE')
    ap.add_argument('--csv', nargs='?', const=DEFAULT_CSV, default=None,
                    metavar='PATH')
    args = ap.parse_args(argv)

    with app.app_context():
        print(f'database: {db.engine.url}')
        print('one spelling per vertical.'
              + ('' if (args.apply or args.undo) else ' REPORT ONLY.'))
        _rule()
        if args.undo:
            if not args.batch:
                print('\n--undo needs --batch VERT-YYYYMMDD-n')
                return 2
            undo(args.batch)
            return 0

        found = survey()
        changes = report(found)
        if args.csv:
            write_csv(found, args.csv)
        if not args.apply:
            print(f'\nDRY RUN — nothing written. --apply --actor '
                  f'<EMP_CODE> rewrites the {changes} row(s) above and '
                  f'seeds the master list.')
            db.session.rollback()
            return 0
        if not args.actor:
            print('\n--apply needs --actor <EMP_CODE>.')
            return 2
        apply(batch=next_batch_id(), actor=args.actor.strip().upper())
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
