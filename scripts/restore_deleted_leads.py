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


def _reason_for(original, *, audited=False, by=None, when=None):
    """The lost_reason this lead comes back with.

    Three cases, kept apart because they are three different
    stories. A lead whose deletion was recorded by a named person
    on a named day, with no reason typed, is not the same as one
    nobody recorded at all — the first can be asked about, the
    second cannot.
    """
    text = (original or '').strip()
    hit = DUPLICATE.search(text)
    if hit:
        return f'Others: duplicate of #{hit.group(1)}'
    if text:
        return f'Restored — originally deleted: {text}'[:200]
    if audited:
        day = str(when)[:10] if when else 'an unrecorded date'
        return (f'Restored — originally deleted: no reason given '
                f'(deleted by {by or "unknown"} on {day})')[:200]
    return 'Restored — originally deleted: no record' 


def from_audit():
    """{id: (fields, original_reason)} from the snapshots."""
    from app.models.audit import DeletionAudit

    out, unusable = {}, []
    for row in (DeletionAudit.query
                .filter_by(entity_type='Lead')
                .order_by(DeletionAudit.id.asc()).all()):
        snap = row.snapshot
        if isinstance(snap, str):
            try:
                snap = json.loads(snap)
            except Exception:                             # noqa: BLE001
                unusable.append((row, 'the snapshot is not readable'))
                continue
        if not isinstance(snap, dict):
            unusable.append((row, 'no snapshot was kept'))
            continue
        if not snap.get('id'):
            unusable.append((row, 'the snapshot has no lead id'))
            continue
        out[int(snap['id'])] = (snap, row.reason or '',
                                row.performed_by, row.performed_at)
    return out, unusable


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
    return {r['id']: (dict(r), '', None, None)
            for r in con.execute('SELECT * FROM leads')}


def plan(backup_path, *, include_pre_backup=False):
    """What would come back, and how.

    The set is the 830 the 6 September backup knew and production
    does not. `deletion_audit` holds more snapshots than that —
    137 of them are leads deleted *before* that backup was taken,
    so the backup never saw them and they were not in the count
    anybody approved. They are restorable and they are a separate
    decision, behind `--include-pre-backup`.
    """
    cols = _columns()
    live = {r[0] for r in db.session.query(Lead.id).all()}

    audit, unusable = from_audit()
    backup = from_backup(backup_path)

    wanted = set(audit) | set(backup)
    pre_backup = sorted(
        i for i in (set(audit) - set(backup)) if i not in live)
    if not include_pre_backup:
        wanted &= set(backup)
    wanted = sorted(wanted)
    rows, why = [], Counter()
    for lid in wanted:
        if lid in live:
            continue                      # never deleted, or already back
        if lid in audit:
            snap, original, by, when = audit[lid]
            source = 'deletion_audit snapshot'
            audited = True
        elif lid in backup:
            snap, original, by, when = backup[lid]
            source = '6 Sep backup'
            audited = False
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
            'lost_reason': _reason_for(
                original, audited=audited, by=by, when=when),
            'company': snap.get('company') or '',
            'stage_was': snap.get('stage') or '',
        })
    return rows, why, pre_backup, unusable


#: Unique on `leads`, so a restored row carrying the same value as
#: a live one is refused by the database — the whole insert, and
#: with it the other 136. Found the hard way on the first apply of
#: the pre-backup set.
UNIQUE_COLUMNS = ('email_message_id', 'crn')


def _taken():
    """{column: {value: live lead id}} for the unique columns."""
    out = {}
    for name in UNIQUE_COLUMNS:
        column = getattr(Lead, name, None)
        if column is None:
            continue
        out[name] = {v: i for i, v in
                     db.session.query(Lead.id, column)
                     .filter(column.isnot(None)).all() if v}
    return out


def apply_plan(rows, *, actor='CRM-RESTORE'):
    from app.services import audit as audit_trail

    live = {r[0] for r in db.session.query(Lead.id).all()}
    taken = _taken()
    made, renumbered, duplicates = [], [], []
    for row in rows:
        keep_id = row['id'] not in live
        fields = dict(row['fields'])

        # A live lead already holding this message id is not a
        # coincidence: the enquiry was deleted and the same email
        # was ingested again. So the one coming back is a
        # duplicate of the one that is live, which is exactly the
        # case the reason rule was written for. The id is cleared
        # because the column is unique, and kept in the audit row
        # so nothing is lost.
        clash_with, cleared = None, {}
        for name, index in taken.items():
            value = fields.get(name)
            if value and value in index:
                clash_with = clash_with or index[value]
                cleared[name] = value
                fields[name] = None

        lead = Lead(**fields)
        if keep_id:
            lead.id = row['id']
        lead.stage = 'Lost'
        lead.lost_reason = (f'Others: duplicate of #{clash_with}'
                            if clash_with else row['lost_reason'])
        lead.updated_at = datetime.utcnow()
        db.session.add(lead)
        db.session.flush()
        live.add(lead.id)
        for name, value in cleared.items():
            taken.setdefault(name, {})
        if not keep_id:
            renumbered.append((row['id'], lead.id))
        if clash_with:
            duplicates.append((row['id'], clash_with, cleared))
        audit_trail.record(
            ACTION, 'lead', str(lead.id),
            old={'deleted': True, 'original_id': row['id']},
            new={'stage': 'Lost', 'lost_reason': lead.lost_reason,
                 'id': lead.id, 'from': row['source'],
                 **({'cleared_unique': cleared,
                     'duplicate_of': clash_with} if clash_with
                    else {})},
            actor=actor,
            reason=('restored after deletion'
                    + ('' if keep_id else
                       f'; original id {row["id"]} is now in use by '
                       f'another lead, so this came back as '
                       f'#{lead.id}')))
        made.append(lead)
    return made, renumbered, duplicates


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


def _export_pre_backup(ids, audit, out_dir=None):
    """The ones the backup never saw, with what is known of each.

    Everything here comes from the snapshot the deletion left
    behind. There is no second source to check it against, which
    is the point: for these leads the audit row is the only record
    that they existed at all.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    root = out_dir or os.path.join(os.path.dirname(_HERE),
                                   'docs', 'exports')
    os.makedirs(root, exist_ok=True)
    from datetime import date as _date
    path = os.path.join(
        root, f'pre_backup_deleted_leads_{_date.today().isoformat()}'
              f'.xlsx')

    wb = Workbook()
    head = Font(bold=True, color='FFFFFF')
    fill = PatternFill('solid', fgColor='1F2937')
    ws = wb.active
    ws.title = 'Read me'
    for line in (
        'Leads deleted before the 6 September backup.',
        '',
        'They are not in the 830 that were restored. The backup '
        'never held them, so no one counted them, and the only '
        'record that they ever existed is the deletion_audit '
        'snapshot each row below is read from.',
        '',
        'Nothing has been restored from this list. '
        '--include-pre-backup on scripts/restore_deleted_leads.py '
        'would add them to the restore, on the same terms as the '
        '830: back as Lost, original id where free, no tasks.',
        '',
        'There is no second source to check any of this against.',
    ):
        ws.append([line])
        ws.cell(ws.max_row, 1).alignment = Alignment(
            wrap_text=True, vertical='top')
    ws.column_dimensions['A'].width = 96
    ws['A1'].font = Font(bold=True, size=13)

    cols = ('id', 'company', 'stage', 'owner', 'vertical', 'source',
            'email', 'created', 'deleted_by', 'deleted_at',
            'reason', 'value_inr')
    s = wb.create_sheet('Deleted before 6 Sep')
    s.append(list(cols))
    for c in s[1]:
        c.font, c.fill = head, fill
    for lid in sorted(ids):
        snap, reason, by, when = audit[lid]
        s.append([
            lid, snap.get('company') or '', snap.get('stage') or '',
            snap.get('assigned_to') or '',
            snap.get('procam_vertical') or '',
            snap.get('source') or '', snap.get('email') or '',
            str(snap.get('created_at') or '')[:19],
            by or '', str(when or '')[:19],
            reason or '(none given)',
            snap.get('estimated_value') or '',
        ])
    for col, w in (('A', 8), ('B', 34), ('C', 18), ('D', 12),
                   ('E', 20), ('F', 12), ('G', 30), ('H', 20),
                   ('I', 12), ('J', 20), ('K', 44), ('L', 12)):
        s.column_dimensions[col].width = w
    s.freeze_panes = 'A2'
    wb.save(path)
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--down', action='store_true')
    ap.add_argument('--yes', action='store_true')
    ap.add_argument('--backup', default=None,
                    help='a readable copy of the 6 Sep backup')
    ap.add_argument('--export-pre-backup',
                    dest='export_pre_backup', action='store_true',
                    help='write the 137 to a workbook for a decision')
    ap.add_argument('--include-pre-backup',
                    dest='include_pre_backup', action='store_true',
                    help='also restore leads deleted before the 6 '
                         'September backup, which only a snapshot '
                         'remembers')
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

        rows, why, pre_backup, unusable = plan(
            args.backup,
            include_pre_backup=args.include_pre_backup)
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

        if unusable:
            # Named rather than counted: a deletion recorded with no
            # usable snapshot is a lead nothing can bring back, and
            # the row is the only sign it existed.
            print(f'\n  {len(unusable)} deletion(s) cannot be '
                  f'restored — no usable snapshot:')
            for row, told in unusable[:20]:
                print(f'    audit #{row.id} lead #{row.entity_id} '
                      f'deleted by {row.performed_by} on '
                      f'{str(row.performed_at)[:10]} — {told}')
            if len(unusable) > 20:
                print(f'    … and {len(unusable) - 20} more')
        if pre_backup and not args.include_pre_backup:
            print(f'\n  NOT included: {len(pre_backup)} lead(s) '
                  f'deleted before the 6 September backup.')
            print('    Only a deletion_audit snapshot remembers '
                  'them — the backup never saw them, so they were '
                  'not in the 830 anybody approved.')
            print('    --include-pre-backup adds them.')

        print('\n  what the reasons will say:')
        for text, n in Counter(
                r['lost_reason'][:64] for r in rows).most_common(8):
            print(f'    {n:>4}  {text}')

        print('\n  a sample:')
        for r in rows[:8]:
            print(f'    #{r["id"]:<7}{(r["company"] or "")[:26]:<28}'
                  f'was {r["stage_was"][:16]:<18}{r["source"]}')

        if args.export_pre_backup and pre_backup:
            path = _export_pre_backup(pre_backup,
                                      from_audit()[0])
            print(f'\n  {path}')

        if args.check:
            print('\nDRY RUN — nothing written. Re-run with --apply '
                  'to restore; --down --yes removes exactly what the '
                  'apply put back.')
            return

        made, renumbered, duplicates = apply_plan(
            rows, actor=args.actor)
        try:
            db.session.commit()
        except Exception as exc:                          # noqa: BLE001
            db.session.rollback()
            print(f'\n  !! ROLLED BACK: {type(exc).__name__}: '
                  f'{str(exc)[:300]}')
            raise SystemExit(1)
        print(f'\n  {len(made)} lead(s) restored as Lost, each with '
              f'an audit row.')
        if duplicates:
            print(f'  {len(duplicates)} were the same enquiry as a '
                  f'lead that is already live — the email was '
                  f'ingested again after the delete. They came back '
                  f'as duplicates of it:')
            for was, of, cleared in duplicates[:20]:
                print(f'    #{was} -> duplicate of #{of} '
                      f'({", ".join(cleared)} cleared)')
            if len(duplicates) > 20:
                print(f'    … and {len(duplicates) - 20} more')
        if renumbered:
            print(f'  {len(renumbered)} came back under a new id '
                  f'because the original was taken:')
            for was, now in renumbered[:20]:
                print(f'    {was} -> {now}')


if __name__ == '__main__':
    main()
