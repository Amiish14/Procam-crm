"""
In-app Notification model (Phase 5 of the CRM upgrade).

Copied verbatim from `Procam-lr-main/app/models/notification.py`, with
two adaptations for CRM's shape:

  1. `from app.extensions import db` → `from app import db`.
  2. `user_id` FK → `employees.emp_code` (String) instead of `users.id`
     (Integer).

One row per user per notifiable event.  Created by the task engine on
TaskInstance insert for user-scoped tasks.  Consumed by the topbar
bell + /notifications inbox page.
"""
from datetime import datetime

from app import db


class NotificationKind:
    TASK_ASSIGNED  = 'task_assigned'
    TASK_RETURNED  = 'task_returned'
    TASK_ESCALATED = 'task_escalated'
    TASK_COMPLETED = 'task_completed'
    ALL = [TASK_ASSIGNED, TASK_RETURNED, TASK_ESCALATED, TASK_COMPLETED]


class Notification(db.Model):
    __tablename__ = 'notifications'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String(20),
                        db.ForeignKey('employees.emp_code'),
                        nullable=False, index=True)
    kind = db.Column(db.String(40), nullable=False,
                     default=NotificationKind.TASK_ASSIGNED)
    title = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text, nullable=True)
    entity_type = db.Column(db.String(40), nullable=True)
    entity_id = db.Column(db.Integer, nullable=True)
    task_instance_id = db.Column(db.Integer,
                                 db.ForeignKey('task_instances.id'),
                                 nullable=True)
    action_url = db.Column(db.String(300), nullable=True)

    is_read = db.Column(db.Boolean, nullable=False, default=False, index=True)
    created_at = db.Column(db.DateTime, nullable=False,
                           default=datetime.utcnow, index=True)
    read_at = db.Column(db.DateTime, nullable=True)

    def __repr__(self):
        return (f'<Notification {self.kind} user={self.user_id} '
                f'{(self.title or "")[:30]}>')
