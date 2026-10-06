"""
The seam between the CRM and the TMS.

They are two applications with two databases. Neither reads the
other's tables; they talk over an authenticated API. These two tables
are the CRM's side of that conversation.

`crm_tms_links` is the cross-reference and nothing else. The CRM lead
is not copied into the TMS and the TMS project is not copied into the
CRM — each system keeps its own record and this says which is which.
The identifiers are the stable ones: a numeric CRM lead id and
whatever id the TMS gives its project. Never a subject line and never
a company name, both of which get edited.

`integration_log` is every request in either direction, so that a
failure is something somebody can see and retry rather than a silence
between two systems that each think the other has it.
"""
from datetime import datetime

from app import db

INBOUND = 'inbound'     # TMS → CRM
OUTBOUND = 'outbound'   # CRM → TMS


class CrmTmsLink(db.Model):
    __tablename__ = 'crm_tms_links'

    id = db.Column(db.Integer, primary_key=True)
    crm_lead_id = db.Column(db.Integer, index=True)
    crm_account_id = db.Column(db.Integer, index=True)
    tms_project_id = db.Column(db.String(60), index=True)
    tms_job_id = db.Column(db.String(60), index=True)
    #: 'project' | 'job' | 'client' — what kind of thing is on the far
    #: side, so one lead can carry a project link and several job links
    #: without them being confused for one another.
    link_type = db.Column(db.String(20), default='project', index=True)

    created_by = db.Column(db.String(60))
    note = db.Column(db.String(400))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    __table_args__ = (
        # The same pairing cannot be recorded twice, so a retried
        # request is a no-op rather than a second link.
        db.UniqueConstraint('crm_lead_id', 'tms_project_id', 'link_type',
                            name='uq_crm_tms_link'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'crm_lead_id': self.crm_lead_id,
            'crm_account_id': self.crm_account_id,
            'tms_project_id': self.tms_project_id or '',
            'tms_job_id': self.tms_job_id or '',
            'link_type': self.link_type,
            'created_at': str(self.created_at)[:19] if self.created_at else '',
        }


class IntegrationLog(db.Model):
    __tablename__ = 'integration_log'

    id = db.Column(db.Integer, primary_key=True)
    #: The caller's own id for this attempt, echoed back. Unique per
    #: direction so a retry is recognisable as the same request.
    request_id = db.Column(db.String(80), index=True)
    #: The caller's idempotency key, when it sent one. This is what
    #: stops a retry creating a second lead.
    idempotency_key = db.Column(db.String(200), index=True)

    direction = db.Column(db.String(10), index=True, nullable=False)
    endpoint = db.Column(db.String(200), index=True)
    method = db.Column(db.String(10))

    crm_object_type = db.Column(db.String(40))
    crm_object_id = db.Column(db.String(60), index=True)
    tms_object_id = db.Column(db.String(60), index=True)

    #: ok | refused | error | replayed
    status = db.Column(db.String(16), index=True)
    status_code = db.Column(db.Integer)
    #: Trimmed, and never the authentication header.
    request_summary = db.Column(db.Text)
    response_summary = db.Column(db.Text)
    error = db.Column(db.String(500))

    caller = db.Column(db.String(80))
    duration_ms = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    def to_dict(self):
        return {
            'id': self.id, 'request_id': self.request_id or '',
            'direction': self.direction, 'endpoint': self.endpoint or '',
            'status': self.status or '', 'status_code': self.status_code,
            'crm_object': f'{self.crm_object_type or ""}:{self.crm_object_id or ""}',
            'tms_object_id': self.tms_object_id or '',
            'error': self.error or '',
            'caller': self.caller or '',
            'at': str(self.created_at)[:19] if self.created_at else '',
        }


class AppSetting(db.Model):
    """Small, administrator-editable settings that are not vocabulary.

    Master Data holds lists people choose from. This holds single
    values an administrator sets once — who gets admin copies of
    notifications, which events those copies cover. Kept here rather
    than in the environment because the brief is explicit that the
    admin recipient must not be hard-coded into application logic, and
    an environment variable is still a deploy.
    """
    __tablename__ = 'app_settings'

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(80), unique=True, index=True, nullable=False)
    value_json = db.Column(db.Text)
    updated_by = db.Column(db.String(20))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)
