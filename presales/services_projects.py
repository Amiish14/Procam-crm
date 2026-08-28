"""
v2026-08 — Pre-Sales Phase 2/3/4 services.

Business logic for Project Intelligence, Account↔Project linking, and
conversion of a pre-sales record into an existing CRM Opportunity/Lead
without disturbing the existing pipeline.
"""
from datetime import datetime, date
from typing import Optional

from app import db, Company, Lead, Opportunity, Contact
from presales.services import PreSalesError
from presales.models_projects import (
    Project, ProjectUpdate, ProjectStageHistory,
    ProjectAccount, ProjectContact,
    OpportunitySourceLink,
    PROJECT_STAGES, PROJECT_UPDATE_TYPES,
    PROJECT_ACCOUNT_ROLES, OPPORTUNITY_SOURCE_TYPES,
)


# ─── Project stage ─────────────────────────────────────────────────────
def change_project_stage(*, project: Project, new_stage: str,
                         changed_by_code: str,
                         note: Optional[str] = None) -> ProjectStageHistory:
    new_stage = (new_stage or '').strip()
    if new_stage not in PROJECT_STAGES:
        raise PreSalesError(f'Unknown project stage: {new_stage!r}')
    prev = project.stage
    project.stage = new_stage
    row = ProjectStageHistory(
        project_id=project.id, from_stage=prev, to_stage=new_stage,
        changed_by=changed_by_code, note=note,
    )
    db.session.add(row)
    return row


# ─── Project update / timeline entry ───────────────────────────────────
def log_project_update(*, project: Project, summary: str,
                       updated_by_code: str,
                       update_type: str = 'Other',
                       update_date=None,
                       source: Optional[str] = None,
                       source_url: Optional[str] = None,
                       next_action: Optional[str] = None,
                       next_review_at=None) -> ProjectUpdate:
    if not (summary or '').strip():
        raise PreSalesError('summary is required')
    if update_type not in PROJECT_UPDATE_TYPES:
        update_type = 'Other'
    row = ProjectUpdate(
        project_id     = project.id,
        update_date    = update_date or date.today(),
        update_type    = update_type,
        source         = (source or '').strip() or None,
        source_url     = (source_url or '').strip() or None,
        summary        = summary.strip(),
        updated_by     = updated_by_code,
        next_action    = (next_action or '').strip() or None,
        next_review_at = next_review_at,
    )
    db.session.add(row)
    project.last_update_at = datetime.utcnow()
    if next_review_at and (not project.next_review_at
                           or next_review_at < project.next_review_at):
        project.next_review_at = next_review_at
    return row


# ─── Project ↔ Account / Contact linking ───────────────────────────────
def link_account_to_project(*, project: Project, account: Company,
                            role: str, added_by_code: str,
                            is_primary: bool = False,
                            note: Optional[str] = None) -> ProjectAccount:
    if role not in PROJECT_ACCOUNT_ROLES:
        raise PreSalesError(f'Unknown project-account role: {role!r}')
    # Idempotent per (project, account, role)
    existing = ProjectAccount.query.filter_by(
        project_id=project.id, account_id=account.id, role=role).first()
    if existing:
        if note:
            existing.note = note
        existing.is_primary = existing.is_primary or is_primary
        return existing
    row = ProjectAccount(
        project_id=project.id, account_id=account.id, role=role,
        is_primary=is_primary, note=note, added_by=added_by_code,
    )
    db.session.add(row)
    return row


def link_contact_to_project(*, project: Project, contact: Contact,
                            role_on_project: Optional[str],
                            added_by_code: str) -> ProjectContact:
    existing = ProjectContact.query.filter_by(
        project_id=project.id, contact_id=contact.id).first()
    if existing:
        if role_on_project:
            existing.role_on_project = role_on_project
        return existing
    row = ProjectContact(
        project_id=project.id, contact_id=contact.id,
        role_on_project=role_on_project, added_by=added_by_code,
    )
    db.session.add(row)
    return row


# ─── Phase 4 · Convert to Opportunity ──────────────────────────────────
def convert_to_opportunity(*, account: Optional[Company] = None,
                           project: Optional[Project] = None,
                           title: str, source_type: str,
                           linked_by_code: str,
                           value_inr: Optional[float] = None,
                           notes: Optional[str] = None) -> Opportunity:
    """Create an Opportunity that traces back to an Account and/or a
    Project. Does NOT re-create the sales-pipeline logic — it just spins
    up an Opportunity row and records the source link so the existing
    RFQ / Quote / Won-Lost flow can proceed from there.

    At least one of {account, project} must be provided."""
    if not account and not project:
        raise PreSalesError('One of account or project is required')
    if source_type not in OPPORTUNITY_SOURCE_TYPES:
        raise PreSalesError(f'Unknown source_type: {source_type!r}')
    if not (title or '').strip():
        raise PreSalesError('title is required')

    company_name = (account.name if account else None)
    opp = Opportunity(
        title             = title.strip(),
        company_id        = (account.id if account else None),
        stage             = 'Qualification',
        value_inr         = value_inr,
        owner_emp_code    = (getattr(account, 'pic_emp_code', None)
                             or getattr(project, 'pic_emp_code', None)
                             or linked_by_code),
        notes             = (notes or '').strip() or None,
        source_type       = source_type,
        source_account_id = (account.id if account else None),
        source_project_id = (project.id if project else None),
    )
    db.session.add(opp)
    db.session.flush()   # get opp.id

    # Audit link
    db.session.add(OpportunitySourceLink(
        opportunity_id    = opp.id,
        source_type       = source_type,
        source_account_id = (account.id if account else None),
        source_project_id = (project.id if project else None),
        linked_by         = linked_by_code,
        note              = f'Converted via presales conversion API',
    ))

    # Timeline entry on the originating record for visibility
    if account:
        from presales.services import log_activity
        log_activity(account=account, kind='RFQ Received',
                     performed_by_code=linked_by_code,
                     subject=f'Opportunity created: {title}',
                     body=f'Opportunity #{opp.id} raised from account development.')
    if project:
        log_project_update(project=project, update_type='RFQ',
                           summary=f'Opportunity created: {title} (Opp#{opp.id})',
                           updated_by_code=linked_by_code)

    return opp
