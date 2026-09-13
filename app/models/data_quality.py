"""Data Quality trend snapshots — §66.

A live count says how bad the data is today; it cannot say whether the
clean-up is working. One row per check per day, written by
scripts/data_quality_snapshot.py, is what the dashboard sparklines draw.

Counts only, never records: the snapshot holds no customer data, so it
needs no scope of its own beyond saying whose total it is.
"""
from datetime import datetime

from app import db


class DataQualitySnapshot(db.Model):
    __tablename__ = 'data_quality_snapshots'

    id            = db.Column(db.Integer, primary_key=True)
    snapshot_date = db.Column(db.Date, nullable=False, index=True)
    check_key     = db.Column(db.String(60), nullable=False, index=True)
    # Whose total this is. Only 'company' is written today; the column is
    # here so a per-vertical trend can be added without a migration.
    scope         = db.Column(db.String(20), nullable=False,
                              default='company')
    # NULL when the check failed that day — a gap, not a zero.
    count         = db.Column(db.Integer, nullable=True)
    taken_at      = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        # One row per day per check per scope is what makes the daily job
        # safe to re-run.
        db.UniqueConstraint('snapshot_date', 'check_key', 'scope',
                            name='uq_dq_snapshot_day'),
    )

    def to_dict(self):
        return {'date': str(self.snapshot_date), 'check': self.check_key,
                'scope': self.scope, 'count': self.count}
