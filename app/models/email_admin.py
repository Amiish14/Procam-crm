"""
Who hears about what, as data an administrator can change.

Three tables, and each one replaces something that was previously a
code change:

    vertical_head        which vertical belongs to whom
    vertical_alias       the many spellings of a vertical that leads
                         actually carry, mapped onto the few the
                         business manages
    notification_policy  the To and CC roles per event

The alias table is the one that matters most. `employees.vertical`
holds four values; leads carry nine. Nothing mapped between them, so
most leads resolved to no vertical head at all and the CC that was
supposed to reach a head reached nobody. An alias row is how that
gets fixed without anybody editing a lead.
"""
import json
from datetime import datetime

from app import db

#: Where a record lands when its vertical maps to nothing. Not a
#: silent drop: somebody senior is copied and the unmapped value is
#: reported in the administrators' morning email, so the gap is
#: visible rather than quiet.
FALLBACK_VERTICAL = 'Management'


class VerticalHead(db.Model):
    """One row per vertical the business manages."""
    __tablename__ = 'vertical_head'

    id = db.Column(db.Integer, primary_key=True)
    #: The master name. Alias rows point at this.
    vertical = db.Column(db.String(80), unique=True, index=True,
                         nullable=False)
    head_emp_code = db.Column(db.String(20), index=True)
    #: Copied alongside the head. Optional, and deliberately not a
    #: replacement — a vertical with a deputy and no head is a gap,
    #: not a working arrangement.
    deputy_emp_code = db.Column(db.String(20))
    active = db.Column(db.Boolean, default=True, index=True)

    note = db.Column(db.String(300))
    updated_by = db.Column(db.String(20))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    def to_dict(self):
        return {'id': self.id, 'vertical': self.vertical,
                'head_emp_code': self.head_emp_code or '',
                'deputy_emp_code': self.deputy_emp_code or '',
                'active': bool(self.active), 'note': self.note or '',
                'updated_by': self.updated_by or '',
                'updated_at': str(self.updated_at)[:16]
                              if self.updated_at else ''}


class VerticalAlias(db.Model):
    """A value a lead actually carries → the vertical it belongs to.

    `employees.vertical` has four values. `leads.procam_vertical` has
    nine. Without this table the overlap is three, and every lead in
    the other six resolved to no head.
    """
    __tablename__ = 'vertical_alias'

    id = db.Column(db.Integer, primary_key=True)
    #: As it appears on the record. Compared case-insensitively and
    #: with surrounding space stripped, so "Freight Forwarding " and
    #: "freight forwarding" are one alias.
    alias = db.Column(db.String(80), unique=True, index=True,
                      nullable=False)
    vertical = db.Column(db.String(80), index=True, nullable=False)
    updated_by = db.Column(db.String(20))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    def to_dict(self):
        return {'id': self.id, 'alias': self.alias,
                'vertical': self.vertical,
                'updated_by': self.updated_by or ''}


class NotificationPolicy(db.Model):
    """The To and CC roles for one event.

    The matrix in `notification_rules.py` stays as the catalogue —
    what each event is, what it says, what fires it. This table holds
    only the part an administrator changes: whether it emails at all,
    who it goes to, and who is copied. A row here overrides the
    matrix; no row means the matrix's own answer stands, so the table
    starts meaningful rather than needing all 31 rows before anything
    works.
    """
    __tablename__ = 'notification_policy'

    id = db.Column(db.Integer, primary_key=True)
    event = db.Column(db.String(60), unique=True, index=True,
                      nullable=False)

    email_enabled = db.Column(db.Boolean, default=True)
    bell_enabled = db.Column(db.Boolean, default=True)

    #: JSON lists of role names. See POLICY_ROLES.
    to_roles_json = db.Column(db.Text)
    cc_roles_json = db.Column(db.Text)

    #: Bumped on every save. Stamped onto each email in the log, so a
    #: question about why a message went where it did can be answered
    #: against the rules as they were, not as they are now.
    version = db.Column(db.Integer, default=1)

    updated_by = db.Column(db.String(20))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    @staticmethod
    def _load(raw):
        try:
            found = json.loads(raw or '[]')
            return [str(r) for r in found] if isinstance(found, list) else []
        except Exception:
            return []

    @property
    def to_roles(self):
        return self._load(self.to_roles_json)

    @to_roles.setter
    def to_roles(self, roles):
        self.to_roles_json = json.dumps(list(roles or []))

    @property
    def cc_roles(self):
        return self._load(self.cc_roles_json)

    @cc_roles.setter
    def cc_roles(self, roles):
        self.cc_roles_json = json.dumps(list(roles or []))

    def to_dict(self):
        return {'event': self.event, 'email_enabled': bool(self.email_enabled),
                'bell_enabled': bool(self.bell_enabled),
                'to_roles': self.to_roles, 'cc_roles': self.cc_roles,
                'version': self.version or 1,
                'updated_by': self.updated_by or '',
                'updated_at': str(self.updated_at)[:16]
                              if self.updated_at else ''}
