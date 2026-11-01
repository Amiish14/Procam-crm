"""
When did anything last happen — for every lead already in the CRM.

    .venv/bin/python scripts/last_activity_backfill.py --check
    .venv/bin/python scripts/last_activity_backfill.py --apply
    .venv/bin/python scripts/undo_batch.py --batch cockpit02.last_activity.<date> --apply

CRM-COCKPIT-02 §4.1: "Back-fill from mails and the audit trail; if
nothing exists, use the created date."

The three No-activity tiles are the reason. They read zero for every
lead in the CRM while the ageing chart beside them showed 584 leads at
16–30 days, because the column they count did not exist. From now on a
session listener keeps it; this fills in the past.

Where the date comes from, newest wins
    a mail on the lead (in or out), an activity row (call, meeting,
    note), a note, a stage-history row, a quote date, or an audit
    event about that lead. If none of those exists, the lead's own
    received or created date — which is true: a lead nobody has
    touched has been quiet since it arrived.

`updated_at` is not used, for the same reason the stage-date back-fill
refused it: one bulk edit touched 2,381 leads in a single day, so it
would report most of the book as worked on that Tuesday and every
No-activity tile would read zero all over again.
"""
import argparse
import os
import sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Lead, app, db                             # noqa: E402

BATCH = 'cockpit02.last_activity'
NOTE = ('01-Nov-2026 CRM-COCKPIT-02: last activity dated from mails, '
        'activities, notes, stage history and the audit trail '
        '(rule: §4.1; created date where nothing exists)')


def _newest_per_lead(sql, params=None):
    from sqlalchemy import text

    out = {}
    try:
        for lead_id, when in db.session.execute(text(sql),
                                                params or {}):
            if lead_id is None or when is None:
                continue
            try:
                lead_id = int(lead_id)
            except (TypeError, ValueError):
                continue
            if lead_id not in out or str(when) > str(out[lead_id]):
                out[lead_id] = when
    except Exception:                                     # noqa: BLE001
        db.session.rollback()
    return out


def sources():
    """{kind: {lead_id: when}} — every trace of activity we hold."""
    from app.services import last_activity as la

    return {
        la.MAIL_IN: _newest_per_lead(
            'SELECT lead_id, MAX(COALESCE(received_at, sent_at)) '
            'FROM lead_emails GROUP BY lead_id'),
        la.CALL: _newest_per_lead(
            'SELECT lead_id, MAX(occurred_at) FROM lead_activities '
            'GROUP BY lead_id'),
        la.NOTE: _newest_per_lead(
            'SELECT lead_id, MAX(created_at) FROM lead_notes '
            'GROUP BY lead_id'),
        la.STAGE: _newest_per_lead(
            'SELECT lead_id, MAX(changed_at) FROM lead_stage_history '
            'GROUP BY lead_id'),
        la.TASK: _newest_per_lead(
            "SELECT entity_id, MAX(occurred_at) FROM audit_events "
            "WHERE entity_type = 'lead' GROUP BY entity_id"),
    }


def plan():
    """[(lead, when, kind)] for every lead with no stamp yet."""
    from app.services import last_activity as la

    found = sources()
    out, why = [], Counter()
    leads = Lead.query.filter(Lead.last_activity_at.is_(None)).all()
    for lead in leads:
        best, best_kind = None, None
        for kind, rows in found.items():
            when = rows.get(lead.id)
            if when is None:
                continue
            if isinstance(when, str):
                from datetime import datetime
                try:
                    when = datetime.fromisoformat(when)
                except ValueError:
                    continue
            if best is None or when > best:
                best, best_kind = when, kind
        if best is None:
            best = lead.received_at or lead.created_at
            best_kind = la.CREATED
        if best is None:
            why['no date of any kind — skipped'] += 1
            continue
        why[f'dated from {best_kind}'] += 1
        out.append((lead, best, best_kind))
    return out, why


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--actor', default='CRM-COCKPIT-02')
    args = ap.parse_args()
    if not (args.check or args.apply):
        args.check = True

    with app.app_context():
        from app.services import bulk_log, sales_rules as rules

        print(f'database: {db.engine.url}')
        print('MODE:     ' + ('APPLY' if args.apply
                              else 'CHECK — nothing written') + '\n')

        total = Lead.query.count()
        have = Lead.query.filter(
            Lead.last_activity_at.isnot(None)).count()
        rows, why = plan()
        print(f'  leads                          {total}')
        print(f'    already stamped              {have}')
        print(f'    would be stamped             {len(rows)}')
        print()
        for label, n in why.most_common():
            print(f'    {label:<34} {n}')

        # What the tiles will say afterwards, which is the number §4.2
        # asks to reconcile with the ageing chart.
        if rows:
            from datetime import datetime
            now = datetime.utcnow()
            buckets = Counter()
            for _lead, when, _kind in rows:
                days = (now - when).days
                for mark in (30, 15, 7):
                    if days >= mark:
                        buckets[f'no activity {mark}D+'] += 1
                        break
            print('\n  after this, among the leads stamped here:')
            for label in ('no activity 30D+', 'no activity 15D+',
                          'no activity 7D+'):
                print(f'    {label:<34} {buckets.get(label, 0)}')

        if args.check:
            print('\nCHECK ONLY — nothing written. --apply writes '
                  'these;\nscripts/undo_batch.py puts them back.')
            return 0

        with bulk_log.batch(BATCH, note=NOTE, actor=args.actor) as log:
            for lead, when, kind in rows:
                if log.set(lead, 'last_activity_at', when):
                    log.set(lead, 'last_activity_kind', kind)
            batch_name, written = log.name, len(log)

        try:
            db.session.commit()
        except Exception as exc:                          # noqa: BLE001
            db.session.rollback()
            print(f'\n  !! ROLLED BACK — nothing was written: '
                  f'{type(exc).__name__}: {str(exc)[:200]}')
            return 1

        print(f'\n  {written} field(s) written across '
              f'{len(rows)} lead(s).')
        print(f'\n  reversible log batch: {batch_name}')
        print(f'  undo: .venv/bin/python scripts/undo_batch.py '
              f'--batch {batch_name} --apply')
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
