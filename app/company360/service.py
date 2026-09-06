"""Assembling Company 360 — §12-16.

§12 requires that clicking a company anywhere in the CRM opens the same
view, whether it was clicked from Global CRM, an Account, a Competitor, an
Opportunity or a report.  So this is the only place that answers "what is
Procam's relationship with this organisation", and everything links here.

The header (§13), KPI cards (§14) and activity timeline (§16) are built
here; routes.py only serves them.
"""
from datetime import datetime

from sqlalchemy import or_

from app import db


def _model(path):
    import importlib
    module_path, _, attr = path.partition(':')
    try:
        return getattr(importlib.import_module(module_path), attr)
    except Exception:
        return None


def classifications(company_id):
    """The §5 relationship types held by this company."""
    tag_model = _model('presales.models:AccountRelationshipTag')
    if tag_model is None:
        return []
    return [t.tag for t in tag_model.query.filter_by(
        account_id=company_id).order_by(tag_model.tag).all()]


def header(company):
    """§13 — who this organisation is and who owns the relationship."""
    from app import Employee

    owner = None
    if company.pic_emp_code:
        owner = Employee.query.filter_by(
            emp_code=company.pic_emp_code).first()

    parent = None
    if getattr(company, 'parent_account_id', None):
        from app import Company
        parent = Company.query.get(company.parent_account_id)

    return {
        'id': company.id,
        'name': company.name,
        'classifications': classifications(company.id),
        'industry': company.industry or '',
        'country': company.country or '',
        'state': company.state or '',
        'city': company.city or '',
        'website': company.website or '',
        'phone': company.phone or '',
        'email': company.email or '',
        'linkedin': company.linkedin or '',
        'parent': {'id': parent.id, 'name': parent.name} if parent else None,
        'owner': {'emp_code': owner.emp_code, 'name': owner.name}
                 if owner else None,
        'relationship_stage': company.dev_stage or '',
        'strategic': bool(getattr(company, 'strategic_flag', False)),
        'priority': company.priority or '',
        'tier': company.tier or '',
        'is_active': bool(company.is_active),
        'last_activity_at': str(company.last_activity_at)[:16]
                            if company.last_activity_at else '',
        'next_action_at': str(company.next_action_at)
                          if company.next_action_at else '',
    }


def _leads_for(company_id):
    from app import Lead
    return Lead.query.filter(Lead.company_id == company_id)


def kpis(company):
    """§14 — the numbers, each computed from the linked records."""
    from app import Lead, Contact, Opportunity, LeadActivity

    cid = company.id
    lead_ids = [r[0] for r in
                db.session.query(Lead.id).filter(Lead.company_id == cid).all()]

    opps = Opportunity.query.filter(Opportunity.company_id == cid)
    won = opps.filter(Opportunity.stage == 'Won').all()
    lost = opps.filter(Opportunity.stage == 'Lost').count()
    won_value = sum(float(o.value_inr or 0) for o in won)
    decided = len(won) + lost

    activities = (LeadActivity.query.filter(LeadActivity.lead_id.in_(lead_ids))
                  .count() if lead_ids else 0)

    def _count(path, column, value):
        model = _model(path)
        if model is None:
            return 0
        try:
            return model.query.filter(getattr(model, column) == value).count()
        except Exception:
            return 0

    quotes = _model('app.models.quote:Quote')
    quote_value = 0.0
    if quotes is not None:
        try:
            quote_value = sum(
                float(q.total_amount or 0) for q in
                quotes.query.filter(quotes.account_id == cid).all())
        except Exception:
            quote_value = 0.0

    tasks = _model('app.models.task_engine:TaskInstance')
    open_tasks = overdue = 0
    if tasks is not None:
        try:
            from app.models.task_engine import TaskInstanceStatus
            q = tasks.query.filter(
                tasks.entity_type == 'Lead',
                tasks.entity_id.in_(lead_ids or [0]),
                tasks.status.in_(TaskInstanceStatus.OPEN))
            open_tasks = q.count()
            overdue = q.filter(tasks.due_at.isnot(None),
                               tasks.due_at < datetime.utcnow()).count()
        except Exception:
            pass

    return {
        'contacts':     Contact.query.filter(Contact.company_id == cid).count(),
        'leads':        len(lead_ids),
        'activities':   activities,
        'opportunities': opps.count(),
        'rfqs':         _count('app.models.rfq:RFQ', 'account_id', cid),
        'quotes':       _count('app.models.quote:Quote', 'account_id', cid),
        'quote_value':  round(quote_value, 2),
        'won':          len(won),
        'won_value':    round(won_value, 2),
        'lost':         lost,
        'win_rate':     round(len(won) / decided * 100, 1) if decided else None,
        'open_tasks':   open_tasks,
        'overdue_tasks': overdue,
    }


def people(company_id):
    """§15 — the People Master records attached to this company."""
    from app import Contact
    rows = Contact.query.filter(Contact.company_id == company_id)\
                        .order_by(Contact.name).all()
    return [{
        'id': c.id, 'name': c.name,
        'designation': c.designation or '', 'email': c.email or '',
        'phone': c.phone or c.mobile or '',
        'linkedin': getattr(c, 'linkedin', '') or '',
        'assigned_to': c.assigned_to or '',
    } for c in rows]


def opportunities(company_id, limit=200):
    from app import Opportunity
    rows = (Opportunity.query.filter(Opportunity.company_id == company_id)
            .order_by(Opportunity.id.desc()).limit(limit).all())
    return [{
        'id': o.id, 'number': o.opp_number or '',
        'stage': o.stage or '', 'value_inr': float(o.value_inr or 0),
        'owner': o.owner_emp_code or '',
    } for o in rows]


def leads(company_id, limit=200):
    from app import Lead
    rows = (_leads_for(company_id)
            .order_by(Lead.id.desc()).limit(limit).all())
    return [{
        'id': l.id, 'project': l.project or '', 'stage': l.stage or '',
        'assigned_to': l.assigned_to or '', 'source': l.source or '',
        'created_at': str(l.created_at)[:10] if l.created_at else '',
    } for l in rows]


def timeline(company_id, limit=120):
    """§16 — one chronological institutional history.

    Everything that happened with this organisation, from whichever table
    recorded it, in one list.
    """
    from app import Lead, LeadActivity, Opportunity

    lead_ids = [r[0] for r in
                db.session.query(Lead.id)
                .filter(Lead.company_id == company_id).all()]
    events = []

    if lead_ids:
        for a in (LeadActivity.query
                  .filter(LeadActivity.lead_id.in_(lead_ids))
                  .order_by(LeadActivity.occurred_at.desc())
                  .limit(limit).all()):
            events.append({
                'at': a.occurred_at or a.created_at,
                'kind': (a.kind or 'note').title(),
                'title': a.subject or (a.kind or 'Activity').title(),
                'body': (a.body or '')[:400],
                'who': a.performed_by or '',
                'route': f'/app?lead={a.lead_id}',
            })

        for l in Lead.query.filter(Lead.id.in_(lead_ids)).all():
            events.append({
                'at': l.created_at, 'kind': 'Lead',
                'title': f'Lead created — {l.project or l.company}',
                'body': '', 'who': l.assigned_to or '',
                'route': f'/app?lead={l.id}',
            })

    for o in Opportunity.query.filter(
            Opportunity.company_id == company_id).all():
        events.append({
            'at': getattr(o, 'created_at', None), 'kind': 'Opportunity',
            'title': f'{o.opp_number or "Opportunity"} — {o.stage or ""}',
            'body': '', 'who': o.owner_emp_code or '',
            'route': f'/app?opp={o.id}',
        })

    events = [e for e in events if e['at']]
    events.sort(key=lambda e: e['at'], reverse=True)
    for e in events:
        e['at'] = str(e['at'])[:16]
    return events[:limit]


def competitors_seen(company_id):
    """§15 — competitors encountered on this company's deals."""
    oc = _model('app.models.competitor:OpportunityCompetitor')
    cm = _model('app.models.competitor:CompetitorMaster')
    if oc is None:
        return []
    from app import Opportunity
    opp_ids = [r[0] for r in db.session.query(Opportunity.id)
               .filter(Opportunity.company_id == company_id).all()]
    if not opp_ids:
        return []
    out = []
    for row in oc.query.filter(oc.opportunity_id.in_(opp_ids)).all():
        name = ''
        if cm is not None and getattr(row, 'competitor_id', None):
            found = cm.query.get(row.competitor_id)
            name = found.name if found else ''
        out.append({'competitor': name,
                    'status': getattr(row, 'status', '') or '',
                    'opportunity_id': getattr(row, 'opportunity_id', None)})
    return out


def full(company):
    return {
        'header': header(company),
        'kpis': kpis(company),
        'people': people(company.id),
        'opportunities': opportunities(company.id),
        'leads': leads(company.id),
        'timeline': timeline(company.id),
        'competitors': competitors_seen(company.id),
    }
