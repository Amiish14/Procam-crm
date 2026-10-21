"""
Bring back the leads that were deleted, as Lost.

    .venv/bin/python scripts/restore_deleted_leads.py --check \
        --backup /tmp/sep06.db
    .venv/bin/python scripts/restore_deleted_leads.py --apply \
        --backup /tmp/sep06.db
    .venv/bin/python scripts/restore_deleted_leads.py --down --yes

830 leads present in the 6 September backup are absent from
production. 710 of them have a `deletion_audit` row carrying a full
snapshot of the lead as it was; the other 120 have no record at all
and exist only in the backup.

Restored as Lost, not as they were
    A deleted lead is not a live one, and putting 830 rows back into
    the open pipeline would bury the 945 leads somebody is actually
    working. They come back closed, with the reason saying what
    happened to them, so they are searchable, reportable and out of
    the way.

The original id, where it is still free
    SQLite hands a deleted row's id to the next insert, so some of
    these ids now belong to a different lead. Those are given a new
    id and the mapping is printed and audited — restoring over a
    live lead would be a far worse outcome than a changed id.

No tasks
    The task engine fires on a state *change* through the service
    layer. This writes rows directly and calls nothing, which is
    deliberate: 830 leads arriving as Lost must not create 830
    pieces of work about deals that ended weeks ago.
"""
import argparse
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Lead, app, db                             # noqa: E402

ACTION = 'lead.restored_after_deletion'

#: `to_dict()` renames eight keys. Mapped rather than guessed at,
#: because a snapshot key that silently matches no column is a
#: field quietly dropped on the way back in.
RENAMED = {
    'cost': 'cost_million',
    'designation': 'designation_pic',
    'estimated_value': 'estimated_value_inr',
    'quoted_amount': 'quoted_amount_inr',
    'followup': 'followup_date',
    'email_extracted': 'email_extracted_json',
}
#: Derived in `to_dict` from other rows or nested; not columns.
IGNORE = ('attachments', 'original_email')
#: Set by this script, never from the snapshot.
OURS = ('id', 'stage', 'lost_reason', 'updated_at')

DUPLICATE = re.compile(r'duplicate[^#]*#\s*(\d+)', re.I)


def _columns():
    return {c.name: c for c in Lead.__table__.columns}


def _coerce(column, value):
    """A JSON value into what the column will take."""
    import datetime as dt
    from decimal import Decimal, InvalidOperation

    if value is None or value == '':
        return None
    kind = column.type.__class__.__name__.lower()
    try:
        if 'datetime' in kind:
            if isinstance(value, str):
                return dt.datetime.fromisoformat(value[:19])
            return value
        if 'date' in kind:
            if isinstance(value, str):
                return dt.date.fromisoformat(value[:10])
            return value
        if 'numeric' in kind:
            return Decimal(str(value))
        if 'integer' in kind:
            return int(value)
        if 'boolean' in kind:
            return bool(value)
    except (ValueError, TypeError, InvalidOperation):
        return None
    if 'json' in kind:
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


def _reason_for(original):
    """The lost_reason this lead comes back with."""
    text = (original or '').strip()
    hit = DUPLICATE.search(text)
    if hit:
        return f'Others: duplicate of #{hit.group(1)}'
    if not text:
        return 'Restored — originally deleted: no record'
    return f'Restored — originally deleted: {text}'[:200]


def from_audit():
    """{id: (fields, original_reason)} from the snapshots."""
    from app.models.audit import DeletionAudit

    out = {}
    for row in (DeletionAudit.query
                .filter_by(entity_type='Lead')
                .order_by(DeletionAudit.id.asc()).all()):
        snap = row.snapshot
        if isinstance(snap, str):
            try:
                snap = json.loads(snap)
            except Exception:                             # noqa: BLE001
                continue
        if not isinstance(snap, dict) or not snap.get('id'):
            continue
        out[int(snap['id'])] = (snap, row.reason or '')
    return out


def from_backup(path):
    """{id: (row, '')} for leads the backup has."""
    if not path:
        return {}
    if not os.path.exists(path):
        raise SystemExit(
            f'{path} is not there. Copy the backup first:\n'
            f'  sudo install -m 640 -o procamapp -g procamapp \\\n'
            f'    /var/backups/procam-crm/procam_crm.db.safe-backup '
            f'{path}')
    con = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    return {r['id']: (dict(r), '')
            for r in con.execute('SELECT * FROM leads')}


def plan(backup_path):
    """What would come back, and how."""
    cols = _columns()
    live = {r[0] for r in db.session.query(Lead.id).all()}

    audit = from_audit()
    backup = from_backup(backup_path)

    #: Everything the backup knew that production no longer has,
    #: plus anything deleted since whose snapshot survives.
    wanted = sorted(set(audit) | set(backup))
    rows, why = [], Counter()
    for lid in wanted:
        if lid in live:
            continue                      # never deleted, or already back
        if lid in audit:
            snap, original = audit[lid]
            source = 'deletion_audit snapshot'
        elif lid in backup:
            snap, original = backup[lid]
            source = '6 Sep backup'
        else:
            continue
        why[source] += 1

        fields = {}
        for key, value in snap.items():
            if key in IGNORE or key in OURS:
                continue
            name = RENAMED.get(key, key)
            column = cols.get(name)
            if column is None:
                continue
            fields[name] = _coerce(column, value)
        rows.append({
            'id': lid, 'fields': fields, 'source': source,
            'original_reason': original,
            'lost_reason': _reason_for(original),
            'company': snap.get('company') or '',
            'stage_was': snap.get('stage') or '',
        })
    return rows, why


def apply_plan(rows, *, actor='CRM-RESTORE'):
    from app.services import audit as audit_trail

    live = {r[0] for r in db.session.query(Lead.id).all()}
    made, renumbered = [], []
    for row in rows:
        keep_id = row['id'] not in live
        lead = Lead(**row['fields'])
        if keep_id:
            lead.id = row['id']
        lead.stage = 'Lost'
        lead.lost_reason = row['lost_reason']
        lead.updated_at = datetime.utcnow()
        db.session.add(lead)
        db.session.flush()
        live.add(lead.id)
        if not keep_id:
            renumbered.append((row['id'], lead.id))
        audit_trail.record(
            ACTION, 'lead', str(lead.id),
            old={'deleted': True, 'original_id': row['id']},
            new={'stage': 'Lost', 'lost_reason': lead.lost_reason,
                 'id': lead.id, 'from': row['source']},
            actor=actor,
            reason=('restored after deletion'
                    + ('' if keep_id else
                       f'; original id {row["id"]} is now in use by '
                       f'another lead, so this came back as '
                       f'#{lead.id}')))
        made.append(lead)
    return made, renumbered


def undo(*, actor='CRM-RESTORE'):
    """Remove exactly what this script put back.

    The one place in this codebase allowed to delete a lead, and
    only ones it created itself, identified by its own audit rows.
    """
    from app.models.audit import AuditEvent

    ids = []
    for ev in (AuditEvent.query
               .filter(AuditEvent.action == ACTION,
                       AuditEvent.entity_type == 'lead').all()):
        try:
            ids.append(int(ev.entity_id))
        except (TypeError, ValueError):
            continue
    removed = 0
    for lid in ids:
        lead = db.session.get(Lead, lid)
        if lead is None:
            continue
        if (lead.lost_reason or '').startswith(
                ('Restored — originally deleted', 'Others: duplicate')):
            db.session.delete(lead)
            removed += 1
    return removed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--down', action='store_true')
    ap.add_argument('--yes', action='store_true')
    ap.add_argument('--backup', default=None,
                    help='a readable copy of the 6 Sep backup')
    ap.add_argument('--actor', default='CRM-RESTORE')
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
            print(f'  {n} restored lead(s) removed again.')
            return

        rows, why = plan(args.backup)
        live = {r[0] for r in db.session.query(Lead.id).all()}
        clashes = [r for r in rows if r['id'] in live]
        dups = [r for r in rows
                if r['lost_reason'].startswith('Others: duplicate')]
        no_record = [r for r in rows
                     if 'no record' in r['lost_reason']]

        print(f'  to restore                    {len(rows)}')
        for k, n in why.most_common():
            print(f'    from {k:<26} {n}')
        print(f'\n  keeping their original id     '
              f'{len(rows) - len(clashes)}')
        print(f'  id taken, would be renumbered {len(clashes)}')
        print(f'  marked as a duplicate of another lead  {len(dups)}')
        print(f'  no record of why they went    {len(no_record)}')
        print(f'\n  all come back stage=Lost, no tasks created.')

        print('\n  what the reasons will say:')
        for text, n in Counter(
                r['lost_reason'][:64] for r in rows).most_common(8):
            print(f'    {n:>4}  {text}')

        print('\n  a sample:')
        for r in rows[:8]:
            print(f'    #{r["id"]:<7}{(r["company"] or "")[:26]:<28}'
                  f'was {r["stage_was"][:16]:<18}{r["source"]}')

        if args.check:
            print('\nDRY RUN — nothing written. Re-run with --apply '
                  'to restore; --down --yes removes exactly what the '
                  'apply put back.')
            return

        made, renumbered = apply_plan(rows, actor=args.actor)
        try:
            db.session.commit()
        except Exception as exc:                          # noqa: BLE001
            db.session.rollback()
            print(f'\n  !! ROLLED BACK: {type(exc).__name__}: '
                  f'{str(exc)[:300]}')
            raise SystemExit(1)
        print(f'\n  {len(made)} lead(s) restored as Lost, each with '
              f'an audit row.')
        if renumbered:
            print(f'  {len(renumbered)} came back under a new id '
                  f'because the original was taken:')
            for was, now in renumbered[:20]:
                print(f'    {was} -> {now}')


if __name__ == '__main__':
    main()
