"""
Sales review — the actions a review meeting agrees, and what became of
them.

A review that does not write down what was agreed is a conversation.
The value of the meeting is the list it leaves behind, and the list is
only worth keeping if the *next* meeting opens with it. So an action
that has not been resolved is not something anyone has to remember to
raise: `app/review/service.py::meeting()` opens every agenda with the
unresolved actions for the same review scope, which is what makes the
sequence self-correcting.

`review_scope` is the subject of the review, not the person who ran it:

    emp:<EMP CODE>        an individual sales review
    vertical:<Vertical>   a vertical review

Two reviews of the same person by two managers therefore share one list,
which is the point — an action is owed to the business, not to whoever
chaired the meeting that raised it.

`linked_entity_type` / `linked_entity_id` tie the action to the record it
is about (a lead, an account, an RFQ), so the agenda can offer the way
through to it rather than a sentence describing it.
"""
from datetime import datetime

from app import db


class ReviewActionStatus:
    OPEN = 'open'
    DONE = 'done'
    #: Not dropped and not done: re-raised with a new date, as a fresh
    #: row that points back here through `carried_from_id`. Keeping the
    #: original row closed rather than editing its due date is what makes
    #: "this has slipped three times" visible at all.
    CARRIED = 'carried'

    CHOICES = (OPEN, DONE, CARRIED)
    #: Statuses that still owe somebody something.
    UNRESOLVED = (OPEN,)


#: The record types an action may be attached to. A short, closed list so
#: the agenda can always work out where to send the reader.
LINKED_TYPES = ('lead', 'account', 'rfq', 'quote', 'opportunity',
                'employee', '')


class ReviewAction(db.Model):
    """One thing somebody agreed to do at a sales review."""

    __tablename__ = 'review_actions'

    id                 = db.Column(db.Integer, primary_key=True)
    #: 'emp:<CODE>' or 'vertical:<Name>' — see the module docstring.
    review_scope       = db.Column(db.String(80), nullable=False, index=True)
    #: The person being reviewed. Blank for a vertical review that is not
    #: about one person. Scope checks read this column.
    subject_emp_code   = db.Column(db.String(20), index=True)
    #: Who has to do it. Usually, but not always, the subject.
    owner_emp_code     = db.Column(db.String(20), index=True)
    due_date           = db.Column(db.Date, index=True)
    description        = db.Column(db.String(500), nullable=False)
    linked_entity_type = db.Column(db.String(20))
    linked_entity_id   = db.Column(db.Integer)
    status             = db.Column(db.String(10), nullable=False,
                                   default=ReviewActionStatus.OPEN,
                                   index=True)
    created_by         = db.Column(db.String(20))
    created_at         = db.Column(db.DateTime, default=datetime.utcnow,
                                   index=True)
    closed_at          = db.Column(db.DateTime)
    #: The action this one replaces, when it was carried forward.
    carried_from_id    = db.Column(db.Integer,
                                   db.ForeignKey('review_actions.id'),
                                   nullable=True, index=True)

    def route(self):
        """Where the agenda sends someone who wants the record itself."""
        kind, rid = (self.linked_entity_type or ''), self.linked_entity_id
        if not rid:
            return ''
        return {
            'lead': f'/app?lead={rid}',
            'account': f'/companies/{rid}',
            'rfq': f'/rfqs/{rid}',
            'quote': f'/quotes/{rid}',
        }.get(kind, '')

    def is_overdue(self, today=None):
        from app.services import sales_rules as rules
        if self.status != ReviewActionStatus.OPEN or not self.due_date:
            return False
        return self.due_date < (today or rules.business_today())

    def to_dict(self, today=None):
        return {
            'id': self.id,
            'review_scope': self.review_scope,
            'subject_emp_code': self.subject_emp_code or '',
            'owner_emp_code': self.owner_emp_code or '',
            'due_date': str(self.due_date) if self.due_date else '',
            'description': self.description or '',
            'linked_entity_type': self.linked_entity_type or '',
            'linked_entity_id': self.linked_entity_id,
            'status': self.status,
            'created_by': self.created_by or '',
            'created_at': str(self.created_at)[:16] if self.created_at else '',
            'closed_at': str(self.closed_at)[:16] if self.closed_at else '',
            'carried_from_id': self.carried_from_id,
            'overdue': self.is_overdue(today),
            'route': self.route(),
        }
