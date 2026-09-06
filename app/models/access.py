"""Per-user access profiles — the single source of truth for who can see
what in the CRM.

Two independent axes, because they answer different questions:

    perms        WHICH screens a person may open   (checkboxes)
    data_scope   HOW MUCH of the company they see  (all / vertical / own)

A director who heads one vertical, for example, gets every report but only
their own vertical's rows: all report perms, ``data_scope='vertical'``.

Rows are optional.  When an employee has no profile, ``effective()`` in
app/access/service.py derives one from their role, so the portal behaves
exactly as it did before anyone touched this screen.
"""
from datetime import datetime

from app import db


class DataScope:
    ALL      = 'all'        # the whole company
    VERTICAL = 'vertical'   # their own vertical only
    OWN      = 'own'        # only records they personally own

    CHOICES = (ALL, VERTICAL, OWN)

    LABELS = {
        ALL:      'Whole company',
        VERTICAL: 'Own vertical only',
        OWN:      'Own records only',
    }


class AccessProfile(db.Model):
    """What one employee may open, and how much of it they see."""
    __tablename__ = 'access_profiles'

    id         = db.Column(db.Integer, primary_key=True)
    emp_code   = db.Column(db.String(20), unique=True, nullable=False,
                           index=True)
    data_scope = db.Column(db.String(12), nullable=False,
                           default=DataScope.OWN)
    # List of permission keys, e.g. ["reports.action", "module.quotes"].
    perms      = db.Column(db.JSON, nullable=False, default=list)

    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)
    updated_by = db.Column(db.String(20))

    def perm_set(self):
        return set(self.perms or [])

    def to_dict(self):
        return {
            'emp_code':   self.emp_code,
            'data_scope': self.data_scope or DataScope.OWN,
            'perms':      sorted(self.perms or []),
            'updated_at': str(self.updated_at)[:16] if self.updated_at else '',
            'updated_by': self.updated_by or '',
        }

    def __repr__(self):
        return f'<AccessProfile {self.emp_code} {self.data_scope}>'
