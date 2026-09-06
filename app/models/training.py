"""Procam CRM Training Academy — §73-81.

§77 is the constraint that shapes everything here: "Training records must
not affect Production Dashboard, KPI, Funnel, Reports, My Work, Customer
data."

The safest way to guarantee that is not to isolate training writes but to
have none.  Practice exercises are validated inside this module against
what the learner submits; nothing is ever written to leads, companies,
opportunities or tasks.  There is therefore no filter to forget and no
training row that can leak into a report.
"""
from datetime import datetime

from app import db


class TrainingStatus:
    NOT_STARTED = 'Not Started'
    IN_PROGRESS = 'In Progress'
    COMPLETED   = 'Completed'


class TrainingProgress(db.Model):
    """One learner's state on one level."""
    __tablename__ = 'training_progress'

    id             = db.Column(db.Integer, primary_key=True)
    emp_code       = db.Column(db.String(20), nullable=False, index=True)
    level_key      = db.Column(db.String(40), nullable=False, index=True)

    status         = db.Column(db.String(16),
                               default=TrainingStatus.NOT_STARTED)
    learned_at     = db.Column(db.DateTime)      # read the material
    practised_at   = db.Column(db.DateTime)      # completed the exercise
    practice_score = db.Column(db.Integer)       # 0-100
    quiz_score     = db.Column(db.Integer)       # 0-100
    attempts       = db.Column(db.Integer, default=0)
    completed_at   = db.Column(db.DateTime)
    # Which version of the material was learned, so a later rewrite can
    # ask people to refresh rather than silently invalidating them (§78).
    content_version = db.Column(db.String(20), default='1')

    updated_at     = db.Column(db.DateTime, default=datetime.utcnow,
                               onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('emp_code', 'level_key', name='uq_train_prog'),
    )

    def to_dict(self):
        return {
            'level_key': self.level_key, 'status': self.status,
            'practice_score': self.practice_score,
            'quiz_score': self.quiz_score,
            'attempts': self.attempts or 0,
            'completed_at': str(self.completed_at)[:10]
                            if self.completed_at else '',
            'content_version': self.content_version or '1',
        }


class Certificate(db.Model):
    """§79 — awarded when every level is complete."""
    __tablename__ = 'training_certificates'

    id             = db.Column(db.Integer, primary_key=True)
    certificate_id = db.Column(db.String(24), unique=True, nullable=False,
                               index=True)
    emp_code       = db.Column(db.String(20), nullable=False, index=True)
    emp_name       = db.Column(db.String(150))
    levels_passed  = db.Column(db.Integer, default=0)
    average_score  = db.Column(db.Integer)
    issued_at      = db.Column(db.DateTime, default=datetime.utcnow)
    content_version = db.Column(db.String(20), default='1')
    revoked_at     = db.Column(db.DateTime)

    def to_dict(self):
        return {
            'certificate_id': self.certificate_id,
            'emp_code': self.emp_code, 'emp_name': self.emp_name,
            'levels_passed': self.levels_passed,
            'average_score': self.average_score,
            'issued_at': str(self.issued_at)[:10] if self.issued_at else '',
            'revoked': self.revoked_at is not None,
        }
