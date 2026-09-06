"""Data Quality checks — §66.

The Phase 1 audit found these by running a script.  A finding nobody can
see is a finding nobody fixes, so the same checks run live here, each with
a count, an explanation of what it costs, and a link to the records.

Every check returns rows the user can act on.  A number with no way to
reach the records behind it is a complaint, not a tool.
"""
from datetime import datetime

from app import db


def _active_codes():
    from app import Employee
    return {e.emp_code for e in
            Employee.query.filter_by(is_active=True).all()} or {''}


def check_unowned_leads():
    from app import Lead
    q = Lead.query.filter((Lead.assigned_to.is_(None)) |
                          (Lead.assigned_to == ''))
    return q.count(), q


def check_leads_of_leavers():
    from app import Lead
    q = Lead.query.filter(Lead.assigned_to.isnot(None),
                          Lead.assigned_to != '',
                          ~Lead.assigned_to.in_(_active_codes()))
    return q.count(), q


def check_unowned_opportunities():
    from app import Opportunity
    q = Opportunity.query.filter((Opportunity.owner_emp_code.is_(None)) |
                                 (Opportunity.owner_emp_code == ''))
    return q.count(), q


def check_unlinked_opportunities():
    from app import Opportunity
    q = Opportunity.query.filter(Opportunity.company_id.is_(None))
    return q.count(), q


def check_unlinked_leads():
    from app import Lead
    q = Lead.query.filter(Lead.company_id.is_(None))
    return q.count(), q


def check_won_without_value():
    from app import Opportunity
    q = Opportunity.query.filter(Opportunity.stage == 'Won',
                                 (Opportunity.value_inr.is_(None)) |
                                 (Opportunity.value_inr == 0))
    return q.count(), q


def check_companies_without_pic():
    from app import Company
    q = Company.query.filter(Company.is_active.is_(True),
                             (Company.pic_emp_code.is_(None)) |
                             (Company.pic_emp_code == ''))
    return q.count(), q


def check_tasks_without_owner():
    try:
        from app.models.task_engine import TaskInstance
    except Exception:
        return 0, None
    q = TaskInstance.query.filter((TaskInstance.owner_user_id.is_(None)) |
                                  (TaskInstance.owner_user_id == ''))
    return q.count(), q


def check_tasks_of_leavers():
    try:
        from app.models.task_engine import TaskInstance
    except Exception:
        return 0, None
    q = TaskInstance.query.filter(TaskInstance.owner_user_id.isnot(None),
                                  TaskInstance.owner_user_id != '',
                                  ~TaskInstance.owner_user_id.in_(
                                      _active_codes()))
    return q.count(), q


def check_won_without_handover():
    from app import Opportunity
    try:
        from app.models.tms_handover import WonHandover
        done = {h.opportunity_id for h in WonHandover.query.all()
                if getattr(h, 'opportunity_id', None)}
    except Exception:
        done = set()
    q = Opportunity.query.filter(Opportunity.stage == 'Won')
    if done:
        q = q.filter(~Opportunity.id.in_(done))
    return q.count(), q


def check_lost_without_competitor():
    from app import Opportunity
    try:
        from app.models.competitor import OpportunityCompetitor
        known = {r.opportunity_id for r in OpportunityCompetitor.query.all()
                 if getattr(r, 'opportunity_id', None)}
    except Exception:
        known = set()
    q = Opportunity.query.filter(Opportunity.stage == 'Lost')
    if known:
        q = q.filter(~Opportunity.id.in_(known))
    return q.count(), q


def check_pending_mappings():
    try:
        from app.models.data_mapping import DataMappingQueue, MappingStatus
    except Exception:
        return 0, None
    q = DataMappingQueue.query.filter_by(status=MappingStatus.PENDING)
    return q.count(), q


def check_duplicate_companies():
    """Company names that still normalise identically."""
    from app import Company
    from app.services.company_match import norm
    seen = {}
    for c in Company.query.filter(Company.is_active.is_(True)).all():
        seen.setdefault(norm(c.name), []).append(c)
    dupes = {k: v for k, v in seen.items() if len(v) > 1}
    return len(dupes), dupes


# (key, label, why it matters, severity, where to fix it)
CHECKS = [
    ('unowned_leads', 'Leads with no owner',
     'Never appear in anyone\'s My Work — no task, no reminder, no '
     'escalation. Invisible rather than neglected.',
     'critical', '/app', check_unowned_leads),
    ('unowned_opps', 'Opportunities with no owner',
     'Nobody is accountable for progressing them.',
     'critical', '/app', check_unowned_opportunities),
    ('leads_of_leavers', 'Leads owned by someone who has left',
     'Assigned to an inactive employee, so the work sits with nobody.',
     'critical', '/app', check_leads_of_leavers),
    ('tasks_no_owner', 'Tasks with no owner',
     'Sit in the task engine and appear for nobody.',
     'critical', '/my-work', check_tasks_without_owner),
    ('tasks_of_leavers', 'Tasks owned by someone who has left',
     'Will never be completed or escalated.',
     'warning', '/my-work', check_tasks_of_leavers),
    ('unlinked_opps', 'Opportunities not linked to a company',
     'Excluded from every account report — the value is real but '
     'invisible.',
     'critical', '/companies', check_unlinked_opportunities),
    ('unlinked_leads', 'Leads not linked to a company',
     'Do not appear on their Company 360, so the account history is '
     'incomplete.',
     'warning', '/companies', check_unlinked_leads),
    ('pending_mappings', 'Company names awaiting a decision',
     'Queued because they could not be matched with certainty. Each one '
     'decided links every record carrying that name.',
     'warning', '/app', check_pending_mappings),
    ('won_no_value', 'Won deals with no value recorded',
     'Count as zero in every value report however well linked. Data '
     'entry, not a system fault.',
     'warning', '/app', check_won_without_value),
    ('no_pic', 'Companies with no account owner',
     'No one is accountable for the relationship.',
     'warning', '/companies', check_companies_without_pic),
    ('lost_no_competitor', 'Lost deals with no competitor recorded',
     'No competitive learning is captured from the loss (§20).',
     'info', '/competitors', check_lost_without_competitor),
    ('won_no_handover', 'Won deals with no TMS handover',
     'Blocked until TMS integration exists.',
     'info', '/handovers', check_won_without_handover),
    ('dupe_companies', 'Duplicate company records',
     'The same organisation held twice, so its history is split across '
     'two records.',
     'warning', '/companies', check_duplicate_companies),
]


def summary():
    out = []
    for key, label, why, severity, route, fn in CHECKS:
        try:
            count, _detail = fn()
        except Exception as exc:
            out.append({'key': key, 'label': label, 'why': why,
                        'severity': 'info', 'route': route,
                        'count': None, 'error': str(exc)[:120]})
            continue
        out.append({'key': key, 'label': label, 'why': why,
                    'severity': severity, 'route': route, 'count': count})
    return out
