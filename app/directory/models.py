"""The two tables Global CRM adds, and why they are tables.

A contact needs three facts the ``contacts`` table has never carried: the
relationship Procam has with that person, a second person looking after
them, and which desk (vertical) owns them. The obvious move is three
columns on ``contacts``.

They are a side table instead, for one reason: a model column that the
database does not yet have breaks *every* query on that table, not only
the new ones. ``contacts`` is read by the lead screens, the Copilot, the
card scanner, Data Quality and the imports. A deploy that landed before
the migration ran would take all of them down together. A new table
cannot do that — code that has not been told about it carries on, and
this module degrades to "nothing recorded yet" if the migration has not
run.

``ContactAssignment`` is the history. It is append-only and mirrors
``presales.models.AccountAssignmentHistory``: the next person to look
after a contact can read the whole handover chain, with the reason each
time, which is the thing a reassignment usually destroys.
"""
from datetime import datetime

from app import db


class ContactRelationship(db.Model):
    """One row per contact, holding what the contacts table cannot.

    Absent row means "nothing recorded", not "no relationship" — the
    search falls back to the account's vertical and the contact's legacy
    ``agent_type`` so that a person nobody has classified still appears
    under the account's desk.
    """
    __tablename__ = 'contact_relationships'

    id                = db.Column(db.Integer, primary_key=True)
    contact_id        = db.Column(db.Integer, db.ForeignKey('contacts.id'),
                                  nullable=False, unique=True, index=True)
    #: A code from the Master Data list 'relationship'. Never validated
    #: against a list written in Python — see service.relationship_types().
    relationship_type = db.Column(db.String(40), nullable=True, index=True)
    #: A colleague who also looks after this person. The primary stays on
    #: contacts.assigned_to, where every other screen already reads it.
    secondary_pic     = db.Column(db.String(20), nullable=True, index=True)
    #: Which Procam desk owns the person. The account's vertical when
    #: blank; set here when the person is worked by a different desk.
    vertical          = db.Column(db.String(80), nullable=True, index=True)

    updated_at        = db.Column(db.DateTime, default=datetime.utcnow,
                                  onupdate=datetime.utcnow)
    updated_by        = db.Column(db.String(20), nullable=True)

    def to_dict(self):
        return {
            'relationship_type': self.relationship_type or '',
            'secondary_pic': self.secondary_pic or '',
            'vertical': self.vertical or '',
            'updated_at': str(self.updated_at)[:16] if self.updated_at else '',
            'updated_by': self.updated_by or '',
        }


class ContactAssignment(db.Model):
    """Append-only history of who looked after a contact, and why.

    ``changes`` carries whatever else moved in the same edit (vertical,
    relationship type, account) so one edit is one row rather than four
    tables' worth of partial truth.
    """
    __tablename__ = 'contact_assignments'

    id                = db.Column(db.Integer, primary_key=True)
    contact_id        = db.Column(db.Integer, db.ForeignKey('contacts.id'),
                                  nullable=False, index=True)
    previous_pic_code = db.Column(db.String(20))       # blank at first assignment
    new_pic_code      = db.Column(db.String(20))
    assigned_by       = db.Column(db.String(20), nullable=False)
    reason            = db.Column(db.Text)
    changes           = db.Column(db.JSON, default=dict)
    assigned_at       = db.Column(db.DateTime, default=datetime.utcnow,
                                  index=True)

    def to_dict(self):
        return {
            'id': self.id,
            'previous_pic': self.previous_pic_code or '',
            'new_pic': self.new_pic_code or '',
            'assigned_by': self.assigned_by or '',
            'reason': self.reason or '',
            'changes': dict(self.changes or {}),
            'assigned_at': str(self.assigned_at)[:16] if self.assigned_at else '',
        }


#: Tables this package owns. ``service.ensure_tables()`` creates any that
#: are missing, so a boot before the migration script runs is harmless.
TABLES = (ContactRelationship.__table__, ContactAssignment.__table__)
