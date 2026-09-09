"""
Lead triage — what has come in and nobody has picked up.

Leads arrive from the shared mailbox with no owner (email_ingest builds
them with `source='email'` and no assigned_to), so until somebody assigns
them they appear in nobody's My Work: not neglected, invisible.  This is
the view that makes them visible.

Two definitions everything here rests on, both taken from the code that
creates and works the records rather than assumed:

arrival time
    `Lead.created_at`.  The ingest pipeline sets it to the message's
    received time, not the time the row was written, so the age of an
    unassigned lead is the age of the customer's email.

actioned
    a LeadActivity row, or a stage change away from the arrival stage.
    LeadActivity is the CRM's own activity model — call, email, meeting,
    rfq, note, visit — and is what every other part of the portal counts.
"""
from datetime import datetime, timedelta

from sqlalchemy import or_


#: Hours. The last bucket is open-ended.
AGE_BUCKETS = ((0, 4), (4, 8), (8, 24), (24, None))

#: How long an assigned lead may sit with no activity before it is
#: flagged for the monitor. Not a scheduling system — just a threshold.
DEFAULT_SLA_HOURS = 24

#: Where an inbound lead starts. A lead still sitting here has not been
#: worked, whatever else has happened to it.
ARRIVAL_STAGES = ('New Opportunity', 'New')


def _unassigned_filter():
    from app import Lead
    return or_(Lead.assigned_to.is_(None), Lead.assigned_to == '')


def _live():
    """Leads that are actually in play — archived rows are not triage."""
    from app import Lead
    q = Lead.query
    if hasattr(Lead, 'is_archived'):
        q = q.filter(or_(Lead.is_archived.is_(False),
                         Lead.is_archived.is_(None)))
    return q


def bucket_label(low, high):
    if high is None:
        return f'> {low}h'
    if low == 0:
        return f'< {high}h'
    return f'{low}–{high}h'


def _age_hours(lead, now=None):
    now = now or datetime.utcnow()
    if not lead.created_at:
        return None
    return (now - lead.created_at).total_seconds() / 3600.0


def unassigned(limit=500):
    from app import Lead
    return (_live().filter(_unassigned_filter())
            .order_by(Lead.created_at.asc())      # oldest first: the queue
            .limit(limit).all())


def headline(now=None):
    """The three numbers at the top of the dashboard."""
    from app import Lead
    now = now or datetime.utcnow()

    total = _live().filter(_unassigned_filter()).count()

    midnight = datetime(now.year, now.month, now.day)
    # "Assigned today" is counted from the assignment history, not from
    # Lead.updated_at — updated_at moves for any edit, which would report
    # a corrected phone number as a triage decision.
    from app import LeadAssignmentHistory
    assigned_today = (LeadAssignmentHistory.query
                      .filter(LeadAssignmentHistory.changed_at >= midnight,
                              LeadAssignmentHistory.to_primary.isnot(None))
                      .count())

    oldest = (_live().filter(_unassigned_filter())
              .filter(Lead.created_at.isnot(None))
              .order_by(Lead.created_at.asc()).first())
    oldest_hours = _age_hours(oldest, now) if oldest else None

    return {
        'unassigned_total': total,
        'assigned_today': assigned_today,
        'oldest_hours': round(oldest_hours, 1) if oldest_hours else None,
        'oldest_lead_id': oldest.id if oldest else None,
        'oldest_company': (oldest.company or '') if oldest else '',
    }


def buckets(now=None):
    """Unassigned leads grouped by how long they have been waiting."""
    now = now or datetime.utcnow()
    out = [{'label': bucket_label(lo, hi), 'low': lo, 'high': hi,
            'count': 0, 'stale': hi is None} for lo, hi in AGE_BUCKETS]
    undated = 0

    for lead in unassigned(limit=5000):
        age = _age_hours(lead, now)
        if age is None:
            undated += 1
            continue
        for i, (lo, hi) in enumerate(AGE_BUCKETS):
            if age >= lo and (hi is None or age < hi):
                out[i]['count'] += 1
                break
    if undated:
        # Never silently dropped: a lead with no arrival time is still
        # unassigned, and the bucket totals must reconcile with the
        # headline count or the dashboard is lying.
        out.append({'label': 'no arrival time', 'low': None, 'high': None,
                    'count': undated, 'stale': True})
    return out


def _row(lead, now=None):
    age = _age_hours(lead, now)
    return {
        'id': lead.id,
        'company': lead.company or f'Lead #{lead.id}',
        'contact': lead.pic or '',
        'contact_email': lead.email or '',
        'source': lead.source or '',
        'vertical': lead.procam_vertical or '',
        'arrived': str(lead.created_at)[:16] if lead.created_at else '',
        'age_hours': round(age, 1) if age is not None else None,
        'age_label': _human_age(age),
        'stage': lead.stage or '',
        'assigned_to': lead.assigned_to or '',
        'assigned_name': lead.assigned_name or '',
        'secondary_owner': lead.secondary_owner or '',
        'secondary_owner_name': lead.secondary_owner_name or '',
    }


def _human_age(hours):
    if hours is None:
        return 'unknown'
    if hours < 1:
        return f'{int(hours * 60)}m'
    if hours < 48:
        return f'{int(hours)}h'
    return f'{int(hours // 24)}d'


def unassigned_rows(now=None, limit=500):
    now = now or datetime.utcnow()
    return [_row(lead, now) for lead in unassigned(limit)]


def not_actioned(sla_hours=DEFAULT_SLA_HOURS, now=None, limit=500):
    """Assigned, past the SLA, and nothing logged against it.

    This is the list the secondary PIC exists for: their primary took the
    lead and then nothing happened.
    """
    from app import Lead, LeadActivity, LeadStageHistory
    now = now or datetime.utcnow()
    cutoff = now - timedelta(hours=sla_hours)

    candidates = (_live()
                  .filter(Lead.assigned_to.isnot(None), Lead.assigned_to != '')
                  .filter(Lead.created_at.isnot(None),
                          Lead.created_at <= cutoff)
                  .order_by(Lead.created_at.asc())
                  .limit(limit * 4).all())
    if not candidates:
        return []

    ids = [c.id for c in candidates]
    # One query each rather than per-lead lookups — this runs against
    # 11,000 leads.
    acted = {r[0] for r in (
        LeadActivity.query.with_entities(LeadActivity.lead_id)
        .filter(LeadActivity.lead_id.in_(ids)).distinct().all())}
    moved = {r[0] for r in (
        LeadStageHistory.query.with_entities(LeadStageHistory.lead_id)
        .filter(LeadStageHistory.lead_id.in_(ids)).distinct().all())}

    out = []
    for lead in candidates:
        if lead.id in acted or lead.id in moved:
            continue
        if (lead.stage or '') not in ARRIVAL_STAGES:
            # Moved on without leaving a history row (an older import, or
            # an edit that predates the stage log) — treat as worked.
            continue
        row = _row(lead, now)
        row['sla_hours'] = sla_hours
        row['overdue_by'] = (round(row['age_hours'] - sla_hours, 1)
                             if row['age_hours'] else None)
        out.append(row)
        if len(out) >= limit:
            break
    return out


def summary(sla_hours=DEFAULT_SLA_HOURS, now=None):
    now = now or datetime.utcnow()
    return {
        'headline': headline(now),
        'buckets': buckets(now),
        'unassigned': unassigned_rows(now),
        'not_actioned': not_actioned(sla_hours, now),
        'sla_hours': sla_hours,
        'generated_at': str(now)[:16],
    }
