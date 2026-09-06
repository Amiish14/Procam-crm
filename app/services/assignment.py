"""
Assignment helpers — shared surface for the RBAC member model
(Phase 2 of the CRM upgrade).

Every business action that needs to know "who owns this record?" or
"who else is on the team?" routes through here.  Falls back to the
legacy single-owner fields when no explicit members have been added
yet — so pre-existing rows continue to behave as they always did.

Public API (all functions are safe to call before the migration; they
short-circuit if the tables don't exist):

    add_member(record, role_key, user_emp_code,
               is_primary=False, assigned_by=None) -> member row
    members_of(record, role_key=None) -> list[member row]
    primary_owner_of(record) -> emp_code | None
    is_member(record, user_emp_code, role_key=None) -> bool
    role_id_for(role_key) -> int | None
"""
from datetime import datetime

from sqlalchemy.exc import SQLAlchemyError

from app import db


# ---------------------------------------------------------------------------
# Lazy-imported so this module is safe to import at boot even if the
# rbac tables haven't been created yet.
# ---------------------------------------------------------------------------
def _rbac():
    from app.models.rbac import (
        Role, AccountMember, DealMember, LeadMember, ProjectMember,
        MEMBER_MODELS,
    )
    return {
        'Role': Role,
        'AccountMember': AccountMember,
        'DealMember': DealMember,
        'LeadMember': LeadMember,
        'ProjectMember': ProjectMember,
        'MEMBER_MODELS': MEMBER_MODELS,
    }


def _classify(record):
    """Return (member_model, fk_column_name, fk_value) for the record.

    Uses class-name matching so we don't force a circular import of the
    legacy models (Lead / Company / Opportunity / Project all live in
    the top-level app.py or in presales/).
    """
    rbac = _rbac()
    cls_name = type(record).__name__.lower()
    tblname = getattr(getattr(record, '__table__', None), 'name', '') or ''
    key = None
    if cls_name == 'company' or tblname == 'companies':
        key = 'account'
    elif cls_name == 'opportunity' or tblname == 'opportunities':
        key = 'opportunity'
    elif cls_name == 'lead' or tblname == 'leads':
        key = 'lead'
    elif cls_name == 'project' or tblname == 'projects':
        key = 'project'
    if not key:
        return None, None, None
    for k, model, fk in rbac['MEMBER_MODELS']:
        if k == key:
            return model, fk, getattr(record, 'id', None)
    return None, None, None


def _legacy_owner(record):
    """The legacy single-owner column for a record, or None."""
    for attr in ('assigned_to', 'pic_emp_code', 'owner_emp_code'):
        v = getattr(record, attr, None)
        if v:
            return str(v)
    return None


def role_id_for(role_key):
    """Look up the crm_roles.id for a key.  Returns None on miss."""
    try:
        rbac = _rbac()
        r = rbac['Role'].query.filter_by(key=role_key).first()
        return r.id if r else None
    except SQLAlchemyError:
        return None
    except Exception:
        return None


def add_member(record, role_key, user_emp_code,
               is_primary=False, assigned_by=None, remarks=None):
    """Insert a member row.  Dedupes on active (record, user, role).

    Returns the existing / new member row, or None if the record type
    is unknown or the role key doesn't resolve.
    """
    if not record or not role_key or not user_emp_code:
        return None
    model, fk_col, fk_val = _classify(record)
    if model is None or fk_val is None:
        return None
    role_id = role_id_for(role_key)
    if role_id is None:
        return None
    # Dedup — active member for same record + user + role
    q = model.query.filter(getattr(model, fk_col) == fk_val,
                           model.user_id == user_emp_code,
                           model.role_id == role_id,
                           model.is_active.is_(True))
    existing = q.first()
    if existing:
        if is_primary and not existing.is_primary:
            existing.is_primary = True
        return existing
    row = model(**{fk_col: fk_val})
    row.user_id     = user_emp_code
    row.role_id     = role_id
    row.is_primary  = bool(is_primary)
    row.assigned_by = assigned_by
    row.assigned_at = datetime.utcnow()
    row.is_active   = True
    row.remarks     = remarks
    db.session.add(row)
    return row


def members_of(record, role_key=None):
    """Return every active member row for the record, optionally
    filtered by role_key."""
    if not record:
        return []
    model, fk_col, fk_val = _classify(record)
    if model is None or fk_val is None:
        return []
    try:
        q = model.query.filter(getattr(model, fk_col) == fk_val,
                               model.is_active.is_(True))
        if role_key:
            rid = role_id_for(role_key)
            if rid is None:
                return []
            q = q.filter(model.role_id == rid)
        return q.all()
    except Exception:
        return []


def primary_owner_of(record):
    """Return the primary owner's emp_code for the record.

    Preference: `is_primary=True` member → any active member →
    legacy single-owner column.
    """
    if not record:
        return None
    model, fk_col, fk_val = _classify(record)
    if model is not None and fk_val is not None:
        try:
            primary = (model.query
                       .filter(getattr(model, fk_col) == fk_val,
                               model.is_active.is_(True),
                               model.is_primary.is_(True))
                       .first())
            if primary:
                return primary.user_id
            any_member = (model.query
                          .filter(getattr(model, fk_col) == fk_val,
                                  model.is_active.is_(True))
                          .order_by(model.assigned_at.asc())
                          .first())
            if any_member:
                return any_member.user_id
        except Exception:
            pass
    return _legacy_owner(record)


def is_member(record, user_emp_code, role_key=None):
    """True iff `user_emp_code` is an active member (any or specific
    role) of `record`, OR the legacy owner when no explicit members
    exist yet."""
    if not record or not user_emp_code:
        return False
    model, fk_col, fk_val = _classify(record)
    if model is not None and fk_val is not None:
        try:
            q = model.query.filter(getattr(model, fk_col) == fk_val,
                                   model.user_id == user_emp_code,
                                   model.is_active.is_(True))
            if role_key:
                rid = role_id_for(role_key)
                if rid is None:
                    return False
                q = q.filter(model.role_id == rid)
            if q.first():
                return True
        except Exception:
            pass
    # Legacy fallback
    if not role_key:
        return _legacy_owner(record) == user_emp_code
    return False
