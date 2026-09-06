"""Ambiguous historical mappings awaiting a human decision — §65.

"Do not guess uncertain matches. Create a DATA MAPPING REVIEW QUEUE for
ambiguous records."

A lead whose company name matches exactly is linked automatically.  A lead
whose name matches nothing, or matches several companies, lands here with
its candidates so a person can decide.  Nothing is guessed, and nothing is
silently dropped.
"""
from datetime import datetime

from app import db


class MappingStatus:
    PENDING  = 'Pending'
    RESOLVED = 'Resolved'
    SKIPPED  = 'Skipped'
    OPEN     = (PENDING,)


class DataMappingQueue(db.Model):
    __tablename__ = 'data_mapping_queue'

    id            = db.Column(db.Integer, primary_key=True)

    # What needs deciding
    entity_type   = db.Column(db.String(30), nullable=False, index=True)
    entity_id     = db.Column(db.Integer, nullable=False, index=True)
    field         = db.Column(db.String(40), nullable=False)
    raw_value     = db.Column(db.String(300))

    # Why it could not be decided automatically
    reason        = db.Column(db.String(40), index=True)   # no_match | ambiguous
    candidates    = db.Column(db.JSON, default=list)       # [{id,name,score}]

    status        = db.Column(db.String(16), default=MappingStatus.PENDING,
                              index=True)
    resolved_to   = db.Column(db.Integer)                  # chosen company id
    resolved_by   = db.Column(db.String(20))
    resolved_at   = db.Column(db.DateTime)
    note          = db.Column(db.Text)

    created_at    = db.Column(db.DateTime, default=datetime.utcnow,
                              index=True)

    __table_args__ = (
        db.Index('ix_dmq_entity', 'entity_type', 'entity_id', 'field'),
        db.Index('ix_dmq_status_reason', 'status', 'reason'),
    )

    def to_dict(self):
        return {
            'id': self.id, 'entity_type': self.entity_type,
            'entity_id': self.entity_id, 'field': self.field,
            'raw_value': self.raw_value or '',
            'reason': self.reason or '',
            'candidates': self.candidates or [],
            'status': self.status, 'resolved_to': self.resolved_to,
            'created_at': str(self.created_at)[:16] if self.created_at else '',
        }


class CompanyMergeLog(db.Model):
    """Every company merge, recorded so it can be understood and undone.

    §88 forbids destructive migration.  A merge repoints references and
    deactivates the loser rather than deleting it, and this row holds
    enough to reverse that.
    """
    __tablename__ = 'company_merge_log'

    id           = db.Column(db.Integer, primary_key=True)
    kept_id      = db.Column(db.Integer, nullable=False, index=True)
    merged_id    = db.Column(db.Integer, nullable=False, index=True)
    kept_name    = db.Column(db.String(200))
    merged_name  = db.Column(db.String(200))
    # {'opportunities': 12, 'account_relationship_tags': 3, ...}
    moved        = db.Column(db.JSON, default=dict)
    merged_by    = db.Column(db.String(20))
    merged_at    = db.Column(db.DateTime, default=datetime.utcnow)
    reverted_at  = db.Column(db.DateTime)

    def to_dict(self):
        return {
            'id': self.id, 'kept_id': self.kept_id,
            'merged_id': self.merged_id, 'kept_name': self.kept_name,
            'merged_name': self.merged_name, 'moved': self.moved or {},
            'merged_by': self.merged_by or '',
            'merged_at': str(self.merged_at)[:16] if self.merged_at else '',
            'reverted': self.reverted_at is not None,
        }
