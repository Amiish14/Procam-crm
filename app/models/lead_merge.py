"""What a lead merge moved, so it can be moved back.

CRM-WA-01 §2: the merge must be "reversible".

WHY A LEDGER AND NOT A FLAG

`leads.merged_into_id` already says a lead was folded into another
one, and that is enough to *display* the merge. It is not enough to
undo it. Undoing means putting every activity, mail, contact,
attachment and quote back on the lead it came from — and once those
rows carry the survivor's id there is nothing left to say which of
them were the survivor's all along.

The old merge recorded counts in its audit row: "lead_activities
.lead_id: 14". Fourteen of what? After the merge the survivor has
thirty activities and no way to tell which fourteen arrived with the
loser. So the counts made the merge auditable and still irreversible.

This records the row ids. One header per merge, with the moves as
JSON, and the loser's own pre-merge state beside them — because
reversing also means putting `stage`, `lost_reason` and `is_archived`
back to what they were, and the merge overwrote all three.

WHEN A MERGE CANNOT BE REVERSED, IT SAYS SO BEFORE IT HAPPENS

Row ids can only be recorded for a table with a single-column primary
key. Everything in this CRM has one, but a future association table
might not, so `partial` marks a merge whose moves were counted rather
than listed, and `unmerge` refuses one. A reversal that silently put
back some of the rows would be worse than no reversal at all.
"""
from __future__ import annotations

from datetime import datetime

from app import db


class LeadMerge(db.Model):
    """One merge: what moved, from where, and how to put it back."""

    __tablename__ = 'lead_merges'

    id = db.Column(db.Integer, primary_key=True)

    #: The lead that was folded away.
    lead_id = db.Column(db.Integer, nullable=False, index=True)
    #: The lead it was folded into.
    into_id = db.Column(db.Integer, nullable=False, index=True)

    #: {'table.column': [row_id, ...]} — every row repointed, by id.
    #: JSON rather than a child table: a merge moves a few hundred
    #: rows at most, and the set is written once and read once.
    moves_json = db.Column(db.JSON)

    #: The loser's own columns before the merge overwrote them:
    #: stage, lost_reason, is_archived, junk_reason_note. Reversing
    #: without these would leave the lead un-merged and still Lost.
    before_json = db.Column(db.JSON)

    #: True when a table's moved rows could only be counted, not
    #: listed. `unmerge` refuses a partial merge rather than putting
    #: some of the rows back.
    partial = db.Column(db.Boolean, nullable=False, default=False)

    reason = db.Column(db.String(400))
    merged_at = db.Column(db.DateTime, default=datetime.utcnow,
                          nullable=False, index=True)
    merged_by = db.Column(db.String(20), index=True)

    #: Set when the merge is reversed. The row stays either way: a
    #: merge that happened and was undone is two events, and a
    #: deleted ledger row would make it none.
    reversed_at = db.Column(db.DateTime, nullable=True, index=True)
    reversed_by = db.Column(db.String(20))

    __table_args__ = (
        db.Index('ix_lead_merges_live', 'lead_id', 'reversed_at'),
    )

    @property
    def reversed(self):
        return self.reversed_at is not None

    def moved_total(self):
        total = 0
        for ids in (self.moves_json or {}).values():
            total += len(ids) if isinstance(ids, list) else int(ids or 0)
        return total

    def to_dict(self):
        return {
            'id': self.id,
            'lead_id': self.lead_id,
            'into_id': self.into_id,
            'moves': self.moves_json or {},
            'moved_total': self.moved_total(),
            'partial': bool(self.partial),
            'reason': self.reason or '',
            'merged_at': (str(self.merged_at)[:19]
                          if self.merged_at else ''),
            'merged_by': self.merged_by or '',
            'reversed': self.reversed,
            'reversed_at': (str(self.reversed_at)[:19]
                            if self.reversed_at else ''),
            'reversed_by': self.reversed_by or '',
        }

    def __repr__(self):
        return (f'<LeadMerge {self.lead_id}→{self.into_id}'
                f'{" reversed" if self.reversed else ""}>')
