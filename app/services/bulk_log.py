"""
Every bulk change in CRM-COCKPIT-02, written down so it can be undone.

    from app.services import bulk_log

    with bulk_log.batch('cockpit02.followup_backfill',
                        note='follow-up set by rule 01-Nov-2026',
                        actor='CRM-COCKPIT-02') as log:
        for lead in leads:
            log.set(lead, 'followup_date', new_date)
        # the caller commits

§8 asks for a reversible log with old → new values and a one-command
undo, and this is it. The shape matters more than it looks:

  * `log.set()` writes the attribute AND the log row together, so a
    field cannot be changed without being recorded. Every back-fill in
    this release goes through it, which is why none of them needs its
    own undo script.
  * the old value is kept as text with a `was_null` flag beside it, so
    "it was empty" and "it was the word None" are not the same row —
    the mistake that makes a hand-rolled undo write the string 'None'
    into a date column.
  * the undo writes through the ORM attribute rather than SQL, so the
    model's own coercion turns '2026-11-04' back into a date.
  * a row that has been put back is stamped, so a second undo of the
    same batch is a no-op rather than an undo of the undo.

It does not commit. The caller owns the transaction, because a
back-fill that half-applied is worse than one that did not run, and
`audit.record(..., commit=False)` has the same contract.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal

log_ = logging.getLogger(__name__)

#: Appended to a batch name when the caller does not date it, so two
#: runs of the same back-fill on different days are separate undos.
DATE_FMT = '%Y-%m-%d'


def _as_text(value):
    """A value as the log stores it. None is the caller's business."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat(timespec='seconds')
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bool):
        return '1' if value else '0'
    return str(value)


def _from_text(text, like):
    """Text back to whatever the column holds, taking its cue from the
    value that is in the column now.

    `like` is the current value, which is the new one — so its type is
    the column's type. Where the column is empty now, the string goes
    back as a string and SQLAlchemy coerces it; where it is a date,
    the string is parsed first, because SQLite will happily store
    '2026-11-04' in a DATE column and then hand back a string that
    every date comparison in the CRM silently skips.
    """
    if text is None:
        return None
    if isinstance(like, bool):
        return text in ('1', 'True', 'true')
    if isinstance(like, datetime):
        return datetime.fromisoformat(text)
    if isinstance(like, date):
        return date.fromisoformat(text[:10])
    if isinstance(like, Decimal):
        return Decimal(text)
    if isinstance(like, int):
        return int(text)
    if isinstance(like, float):
        return float(text)
    return text


class Batch:
    """One run. Hands out `set()` and counts what it wrote."""

    def __init__(self, name, *, note='', actor=None):
        self.name = name
        self.note = note
        self.actor = actor
        self.rows = []

    def set(self, obj, field, value, *, entity_type=None):
        """Change one field and record it. Returns True if it changed.

        A no-op write is not logged: a back-fill that re-runs would
        otherwise fill the log with rows whose undo does nothing, and
        the count it prints would be a count of records it visited
        rather than records it changed.
        """
        from app import db
        from app.models.cockpit02 import BulkChangeLog

        was = getattr(obj, field, None)
        if was == value:
            return False
        setattr(obj, field, value)
        db.session.add(BulkChangeLog(
            batch=self.name,
            note=(self.note or '')[:300],
            entity_type=entity_type or type(obj).__name__,
            entity_id=str(getattr(obj, 'id', '')),
            field=field,
            old_value=_as_text(was),
            new_value=_as_text(value),
            was_null=was is None,
            actor=self.actor))
        self.rows.append((type(obj).__name__, getattr(obj, 'id', None),
                          field))
        return True

    def __len__(self):
        return len(self.rows)

    @property
    def fields(self):
        """{field: how many records} — what to print after a run."""
        out = {}
        for _entity, _id, field in self.rows:
            out[field] = out.get(field, 0) + 1
        return out


@contextmanager
def batch(name, *, note='', actor=None, dated=True, today=None):
    """A named run. `dated` stamps the day into the batch name."""
    if dated:
        stamp = (today or datetime.utcnow().date()).strftime(DATE_FMT)
        if not name.endswith(stamp):
            name = f'{name}.{stamp}'
    yield Batch(name, note=note, actor=actor)


# ── reading it back ──────────────────────────────────────────────────
def batches():
    """[(batch, rows, undone, first, last, note)] newest first."""
    from app import db
    from app.models.cockpit02 import BulkChangeLog

    rows = (db.session.query(
        BulkChangeLog.batch,
        db.func.count(BulkChangeLog.id),
        db.func.count(BulkChangeLog.undone_at),
        db.func.min(BulkChangeLog.created_at),
        db.func.max(BulkChangeLog.created_at),
        db.func.max(BulkChangeLog.note))
        .group_by(BulkChangeLog.batch)
        .order_by(db.func.max(BulkChangeLog.created_at).desc()).all())
    return rows


def rows_of(name, *, pending_only=True):
    from app.models.cockpit02 import BulkChangeLog

    q = BulkChangeLog.query.filter(BulkChangeLog.batch == name)
    if pending_only:
        q = q.filter(BulkChangeLog.undone_at.is_(None))
    return q.order_by(BulkChangeLog.id.desc()).all()


#: entity_type -> how to load one. A log row names a class; the undo
#: has to turn that back into a record, and guessing from the string
#: is how an undo writes to the wrong table.
def _model_for(entity_type):
    import app.models.cockpit02                           # noqa: F401
    from app import Company, Lead

    known = {'Lead': Lead, 'Company': Company}
    if entity_type in known:
        return known[entity_type]
    try:
        from app.models.assignment import AccountVerticalOwner
        known['AccountVerticalOwner'] = AccountVerticalOwner
    except Exception:
        pass
    return known.get(entity_type)


def undo(name, *, actor=None, limit=None):
    """Put a batch back. Returns (restored, skipped, problems).

    Newest row first, so a field changed twice in one batch ends on
    the value it started with rather than on the middle one.
    """
    from app import db

    restored, skipped, problems = 0, 0, []
    rows = rows_of(name, pending_only=True)
    if limit:
        rows = rows[:limit]
    for row in rows:
        model = _model_for(row.entity_type)
        if model is None:
            problems.append(f'{row.entity_type}: no model')
            skipped += 1
            continue
        try:
            obj = db.session.get(model, int(row.entity_id))
        except (TypeError, ValueError):
            obj = None
        if obj is None:
            problems.append(f'{row.entity_type}#{row.entity_id}: gone')
            skipped += 1
            continue
        now = getattr(obj, row.field, None)
        if _as_text(now) != row.new_value:
            # Somebody has edited this field since the batch ran.
            # Their value wins: an undo that overwrites a person's
            # later decision is worse than one that reports it.
            problems.append(
                f'{row.entity_type}#{row.entity_id}.{row.field}: '
                f'changed since (now {_as_text(now)!r})')
            skipped += 1
            continue
        back = None if row.was_null else _from_text(row.old_value, now)
        try:
            setattr(obj, row.field, back)
        except Exception as exc:                          # noqa: BLE001
            problems.append(f'{row.entity_type}#{row.entity_id}.'
                            f'{row.field}: {type(exc).__name__}')
            skipped += 1
            continue
        row.undone_at = datetime.utcnow()
        row.undone_by = actor
        restored += 1
    return restored, skipped, problems
