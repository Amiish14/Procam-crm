"""Data Quality checks — §66.

The Phase 1 audit found these by running a script.  A finding nobody can
see is a finding nobody fixes, so the same checks run live here, each with
a count, an explanation of what it costs, and a link to the records.

Every check returns rows the user can act on.  A number with no way to
reach the records behind it is a complaint, not a tool.

What each check is — its title, severity, cost, suggestion and the
thresholds it uses — lives in definitions.py, shared with the read-only
report. This module is how each is measured against the live database.

Scope
    Every check is measured inside the viewer's Access Matrix boundary
    (app.access.scope). An administrator with Own scope sees the problems
    in their own records, not a company-wide count they could not open.
    Records with no owner of their own — an email whose lead is gone, a
    classifier decision — are shown only to a viewer who sees everything,
    which is the direction scope.may_view already fails in.

    Outside a request (the snapshot job, a script, a test) there is no
    viewer, and the checks measure the whole company.
"""
import inspect as _inspect
import math
from datetime import date, datetime, timedelta

from sqlalchemy import and_, func, or_, select

from app import db
from app.data_quality import definitions as defs


# ── scope ────────────────────────────────────────────────────────────
def system_scope():
    """The whole company, for callers with no signed-in viewer."""
    from app.access.scope import Scope
    from app.models.access import DataScope
    return Scope('system', '', None, set(), DataScope.ALL)


def resolve_scope(sc=None):
    """The boundary a check runs inside.

    Inside a request it is always the signed-in viewer's — an anonymous
    request gets the empty scope, never the whole company.
    """
    if sc is not None:
        return sc
    from flask import has_request_context
    if has_request_context():
        from app.access import scope
        return scope.current()
    return system_scope()


def _everything(sc):
    return sc.codes is None


def _nothing(q):
    return q.filter(False)


# ── shared filters ───────────────────────────────────────────────────
def _blank(col):
    return or_(col.is_(None), func.trim(col) == '')


def _filled(col):
    return and_(col.isnot(None), func.trim(col) != '')


def _active_codes():
    from app import Employee
    return {e.emp_code for e in
            Employee.query.filter_by(is_active=True).all()} or {''}


def _live_leads():
    from app import Lead
    return Lead.query.filter(Lead.is_archived.isnot(True))


def _open_leads():
    from app import Lead
    # A NULL stage is open: NOT IN alone would drop it silently.
    return _live_leads().filter(or_(
        Lead.stage.is_(None),
        Lead.stage.not_in(defs.LEAD_TERMINAL_STAGES)))


def _open_opps():
    from app import Opportunity
    return Opportunity.query.filter(
        or_(Opportunity.stage.is_(None),
            Opportunity.stage.not_in(defs.OPP_CLOSED)),
        Opportunity.won_at.is_(None), Opportunity.lost_at.is_(None))


def _active_companies():
    from app import Company
    # NULL is active, as in the report: the column arrived after the rows.
    return Company.query.filter(Company.is_active.isnot(False))


def _today():
    return date.today()


# ── ownership ────────────────────────────────────────────────────────
def check_unowned_leads(sc=None):
    from app import Lead
    from app.access import scope
    q = scope.leads(_live_leads().filter(_blank(Lead.assigned_to)),
                    resolve_scope(sc))
    return q.count(), q


def check_leads_of_leavers(sc=None):
    from app import Lead
    from app.access import scope
    q = scope.leads(_live_leads().filter(
        _filled(Lead.assigned_to),
        ~Lead.assigned_to.in_(_active_codes())), resolve_scope(sc))
    return q.count(), q


def check_unowned_opportunities(sc=None):
    from app import Opportunity
    from app.access import scope
    q = scope.opportunities(Opportunity.query.filter(
        _blank(Opportunity.owner_emp_code)), resolve_scope(sc))
    return q.count(), q


def check_opportunities_of_leavers(sc=None):
    from app import Opportunity
    from app.access import scope
    q = scope.opportunities(Opportunity.query.filter(
        _filled(Opportunity.owner_emp_code),
        ~Opportunity.owner_emp_code.in_(_active_codes())), resolve_scope(sc))
    return q.count(), q


def check_companies_without_pic(sc=None):
    from app import Company
    from app.access import scope
    q = scope.companies(_active_companies().filter(
        _blank(Company.pic_emp_code)), resolve_scope(sc))
    return q.count(), q


def check_companies_of_leavers(sc=None):
    from app import Company
    from app.access import scope
    q = scope.companies(_active_companies().filter(
        _filled(Company.pic_emp_code),
        ~Company.pic_emp_code.in_(_active_codes())), resolve_scope(sc))
    return q.count(), q


def _tasks(sc):
    try:
        from app.models.task_engine import TaskInstance
    except Exception:
        return None, None
    q = TaskInstance.query
    if not _everything(sc):
        q = (q.filter(TaskInstance.owner_user_id.in_(sc.codes))
             if sc.codes else _nothing(q))
    return TaskInstance, q


def check_tasks_without_owner(sc=None):
    TaskInstance, q = _tasks(resolve_scope(sc))
    if TaskInstance is None:
        return 0, None
    q = q.filter(_blank(TaskInstance.owner_user_id))
    return q.count(), q


def check_tasks_of_leavers(sc=None):
    TaskInstance, q = _tasks(resolve_scope(sc))
    if TaskInstance is None:
        return 0, None
    q = q.filter(_filled(TaskInstance.owner_user_id),
                 ~TaskInstance.owner_user_id.in_(_active_codes()))
    return q.count(), q


# ── activity and pipeline hygiene ────────────────────────────────────
def check_stale_leads(sc=None):
    """Open leads nobody has contacted in NO_CONTACT_DAYS.

    "Contact" is app.services.contact's definition — an activity or an
    email, never a field edit — so this agrees with the Sales Intelligence
    tiles and the Copilot. A lead younger than the threshold has not had
    the chance to be neglected, so it is not listed.
    """
    from app import Lead
    from app.access import scope
    from app.services import contact
    cutoff = datetime.utcnow() - timedelta(days=defs.NO_CONTACT_DAYS)
    q = scope.leads(_open_leads().filter(
        or_(Lead.created_at.is_(None), Lead.created_at < cutoff),
        Lead.id.not_in(contact.contacted_since_select(cutoff))),
        resolve_scope(sc))
    return q.count(), q


def check_leads_without_followup(sc=None):
    from app import Lead
    from app.access import scope
    q = scope.leads(_open_leads().filter(or_(
        Lead.followup_date.is_(None), Lead.followup_date < _today())),
        resolve_scope(sc))
    return q.count(), q


def check_stale_opportunities(sc=None):
    """Open deals past their expected close date, or untouched.

    Untouched means no update to the deal and no contact on its lead for
    STALE_OPPORTUNITY_DAYS. Everything the report lists as overdue to
    close is in here too.
    """
    from app import Opportunity
    from app.access import scope
    from app.services import contact
    cutoff = datetime.utcnow() - timedelta(days=defs.STALE_OPPORTUNITY_DAYS)
    touched = func.coalesce(Opportunity.updated_at, Opportunity.created_at)
    q = scope.opportunities(_open_opps().filter(or_(
        and_(Opportunity.expected_close_date.isnot(None),
             Opportunity.expected_close_date < _today()),
        and_(or_(touched.is_(None), touched < cutoff),
             or_(Opportunity.lead_id.is_(None),
                 Opportunity.lead_id.not_in(
                     contact.contacted_since_select(cutoff)))))),
        resolve_scope(sc))
    return q.count(), q


def check_opportunities_without_close_date(sc=None):
    from app import Opportunity
    from app.access import scope
    q = scope.opportunities(_open_opps().filter(
        Opportunity.expected_close_date.is_(None)), resolve_scope(sc))
    return q.count(), q


def check_rfqs_without_quote(sc=None):
    from app.access import scope
    from app.models.quote import Quote
    from app.models.rfq import RFQ
    quoted = select(Quote.rfq_id).where(Quote.rfq_id.isnot(None))
    q = scope.rfqs(RFQ.query.filter(
        RFQ.quote_by_date.isnot(None), RFQ.quote_by_date < _today(),
        or_(RFQ.status.is_(None),
            RFQ.status.not_in(defs.RFQ_NO_QUOTE_NEEDED)),
        RFQ.id.not_in(quoted)), resolve_scope(sc))
    return q.count(), q


# ── deals won ────────────────────────────────────────────────────────
def check_won_without_value(sc=None):
    from app import Opportunity
    from app.access import scope
    q = scope.opportunities(Opportunity.query.filter(
        Opportunity.stage.in_(defs.OPP_WON),
        (Opportunity.value_inr.is_(None)) | (Opportunity.value_inr == 0)),
        resolve_scope(sc))
    return q.count(), q


def check_won_without_po(sc=None):
    """Won deals whose handover has no PO recorded.

    Since the process changed, the Project and Job are created against
    the customer PO, so a handover with no PO recorded is a won deal that
    cannot move — and the longer it sits, the harder the PO is to chase.
    A cancelled handover is not waiting for anything.
    """
    from app.access import scope
    from app.models.tms_handover import WonHandover, HandoverStatus
    q = scope.handovers(WonHandover.query.filter(
        func.coalesce(WonHandover.status, '') != HandoverStatus.CANCELLED,
        _blank(WonHandover.po_ref)), resolve_scope(sc))
    return q.count(), q


def check_duplicate_po_refs(sc=None):
    """One PO carrying two handovers — two Projects under one PO.

    The rule is enforced on entry, so anything here predates it or was
    written straight to the database.
    """
    from app.access import scope
    from app.models.tms_handover import WonHandover, HandoverStatus
    from app.handover.po import norm_ref
    base = scope.handovers(WonHandover.query.filter(
        WonHandover.po_ref.isnot(None),
        WonHandover.status != HandoverStatus.CANCELLED), resolve_scope(sc))
    rows = [h for h in base.all() if h.po_ref]
    groups = {}
    for h in rows:
        key = (h.account_id or ('name:' + norm_ref(h.account_name)),
               norm_ref(h.po_ref))
        groups.setdefault(key, []).append(h)
    clashing = [h for g in groups.values() if len(g) > 1 for h in g]
    return len(clashing), clashing


def check_won_without_handover(sc=None):
    """Won — by stage or by won_at, whichever moved — with no live
    handover. The report reads it the same way: a deal somebody forgot to
    move to Won is still won."""
    from app import Opportunity
    from app.access import scope
    from app.models.tms_handover import WonHandover, HandoverStatus
    handed = select(WonHandover.opportunity_id).where(
        WonHandover.opportunity_id.isnot(None),
        func.coalesce(WonHandover.status, '') != HandoverStatus.CANCELLED)
    q = scope.opportunities(Opportunity.query.filter(
        or_(Opportunity.stage.in_(defs.OPP_WON),
            Opportunity.won_at.isnot(None)),
        Opportunity.lost_at.is_(None),
        Opportunity.id.not_in(handed)), resolve_scope(sc))
    return q.count(), q


def check_lost_without_competitor(sc=None):
    from app import Opportunity
    from app.access import scope
    q = Opportunity.query.filter(Opportunity.stage.in_(defs.OPP_LOST))
    try:
        from app.models.competitor import OpportunityCompetitor
        q = q.filter(Opportunity.id.not_in(
            select(OpportunityCompetitor.opportunity_id).where(
                OpportunityCompetitor.opportunity_id.isnot(None))))
    except Exception:
        pass
    q = scope.opportunities(q, resolve_scope(sc))
    return q.count(), q


# ── linking and duplicates ───────────────────────────────────────────
def check_unlinked_opportunities(sc=None):
    from app import Opportunity
    from app.access import scope
    q = scope.opportunities(Opportunity.query.filter(
        Opportunity.company_id.is_(None)), resolve_scope(sc))
    return q.count(), q


def check_unlinked_leads(sc=None):
    from app import Lead
    from app.access import scope
    q = scope.leads(_live_leads().filter(Lead.company_id.is_(None)),
                    resolve_scope(sc))
    return q.count(), q


def check_pending_mappings(sc=None):
    try:
        from app.models.data_mapping import DataMappingQueue, MappingStatus
    except Exception:
        return 0, None
    q = DataMappingQueue.query.filter_by(status=MappingStatus.PENDING)
    if not _everything(resolve_scope(sc)):
        # A queued name carries no owner, and its candidates name
        # accounts the viewer may not reach.
        q = _nothing(q)
    return q.count(), q


def check_duplicate_companies(sc=None):
    """Accounts that share a normalised name, a GSTIN or a mail domain.

    Only accounts inside the viewer's scope are compared: telling an Own
    viewer that their account matches one they cannot open would name it.
    """
    from app.access import scope
    rows = scope.companies(_active_companies(), resolve_scope(sc)).all()
    groups = {}
    for c in rows:
        keys = []
        name = defs.norm_name(c.name)
        if name:
            keys.append(f'Same name — {name}')
        if not defs.blank(c.gstin):
            keys.append(f'Same GSTIN — {c.gstin.strip().upper()}')
        for d in sorted(defs.email_domains(c.email_domains)):
            keys.append(f'Same mail domain — {d}')
        for k in keys:
            groups.setdefault(k, [])
            if c not in groups[k]:
                groups[k].append(c)
    dupes = {k: v for k, v in groups.items() if len(v) > 1}
    return len(dupes), dupes


def check_duplicate_contacts(sc=None):
    from app import Contact
    from app.access import scope
    rows = scope.contacts(Contact.query.filter(Contact.is_active.isnot(False)),
                          resolve_scope(sc)).all()
    groups = {}
    for c in rows:
        keys = []
        if not defs.blank(c.email):
            keys.append(f'Same email — {c.email.strip().lower()}')
        for p in sorted({defs.norm_phone(c.phone),
                         defs.norm_phone(c.mobile)} - {''}):
            keys.append(f'Same phone — {p}')
        for k in keys:
            groups.setdefault(k, []).append(c)
    dupes = {k: v for k, v in groups.items() if len(v) > 1}
    return len(dupes), dupes


# ── integrity ────────────────────────────────────────────────────────
def check_orphan_records(sc=None):
    """Rows whose lead or account no longer exists.

    SQLite does not enforce foreign keys here, and it reuses a deleted id
    for the next insert — so an orphan today is a stranger's history
    attached to a new lead tomorrow.
    """
    from app import (Company, Lead, LeadActivity, LeadEmail, LeadNote,
                     Opportunity)
    from app.access import scope
    sc = resolve_scope(sc)
    lead_ids = select(Lead.id)
    company_ids = select(Company.id)
    out = []
    if _everything(sc):
        # Their only link to an owner is the lead that is gone.
        for model, label in ((LeadEmail, 'email'), (LeadNote, 'note'),
                             (LeadActivity, 'activity')):
            for r in _light(model.query.filter(
                    model.lead_id.isnot(None),
                    model.lead_id.not_in(lead_ids))) \
                    .order_by(model.id).all():
                out.append((r, f'{label} on missing lead #{r.lead_id}'))
    for o in scope.opportunities(Opportunity.query.filter(or_(
            and_(Opportunity.lead_id.isnot(None),
                 Opportunity.lead_id.not_in(lead_ids)),
            and_(Opportunity.company_id.isnot(None),
                 Opportunity.company_id.not_in(company_ids)))), sc) \
            .order_by(Opportunity.id).all():
        out.append((o, 'points at a lead or account that is gone'))
    for lead in _light(scope.leads(Lead.query.filter(
            Lead.company_id.isnot(None),
            Lead.company_id.not_in(company_ids)), sc)) \
            .order_by(Lead.id).all():
        out.append((lead, f'account #{lead.company_id} is gone'))
    return len(out), out


def check_broken_relationships(sc=None):
    from sqlalchemy.orm import aliased
    from app import Company, Contact, Lead, Opportunity
    from app.access import scope
    sc = resolve_scope(sc)
    source = aliased(Lead)
    out = []
    for o in scope.opportunities(
            Opportunity.query.join(source, source.id == Opportunity.lead_id)
            .filter(Opportunity.company_id.isnot(None),
                    source.company_id.isnot(None),
                    source.company_id != Opportunity.company_id), sc) \
            .order_by(Opportunity.id).all():
        out.append((o, f'its lead #{o.lead_id} belongs to another account'))
    for c in scope.contacts(Contact.query.filter(
            Contact.company_id.isnot(None),
            Contact.company_id.not_in(select(Company.id))), sc) \
            .order_by(Contact.id).all():
        out.append((c, f'account #{c.company_id} is gone'))
    return len(out), out


def check_empty_mandatory(sc=None):
    from app import Company, Lead, Opportunity
    from app.access import scope
    sc = resolve_scope(sc)
    out = []
    for lead in _light(scope.leads(_live_leads().filter(or_(
            _blank(Lead.company), _blank(Lead.stage))), sc)) \
            .order_by(Lead.id).all():
        missing = [f for f, v in (('company', lead.company),
                                  ('stage', lead.stage)) if defs.blank(v)]
        out.append((lead, 'no ' + ' or '.join(missing)))
    for o in scope.opportunities(Opportunity.query.filter(
            _blank(Opportunity.stage)), sc).order_by(Opportunity.id).all():
        out.append((o, 'no stage'))
    for c in scope.companies(_active_companies().filter(
            _blank(Company.name)), sc).order_by(Company.id).all():
        out.append((c, 'no name'))
    return len(out), out


def check_lead_account_mismatch(sc=None):
    """Leads whose own company name normalises differently from the
    account they are linked to — the link is probably wrong."""
    from app import Company, Lead
    from app.access import scope
    sc = resolve_scope(sc)
    # Names only: the comparison has to read every linked lead, and a
    # whole Lead row carries the original email body.
    pairs = scope.leads(
        _live_leads().join(Company, Company.id == Lead.company_id), sc) \
        .with_entities(Lead.id, Lead.company, Company.name).all()
    norms = {}

    def norm(text):
        # Names repeat across thousands of leads; normalise each once.
        if text not in norms:
            norms[text] = defs.norm_name(text)
        return norms[text]

    ids = [lid for lid, name, account in pairs
           if norm(name) and norm(account) and norm(name) != norm(account)]
    q = Lead.query.filter(Lead.id.in_(ids)) if ids else _nothing(Lead.query)
    return len(ids), q


def _vertical_values():
    """Every active vertical in Master Data, by label or code — the bulk
    tool writes labels, older imports wrote codes, and both are real."""
    try:
        from app.master_data import service as md
        items = md.items('vertical')
    except Exception:
        return set()
    return {i.label for i in items} | {i.code for i in items}


def check_leads_with_unknown_vertical(sc=None):
    from app import Lead
    from app.access import scope
    known = _vertical_values()
    q = _live_leads()
    if not known:
        # An unconfigured list would flag every lead; that is a setup
        # problem, not thousands of data problems.
        q = _nothing(q)
    else:
        q = q.filter(_filled(Lead.procam_vertical),
                     Lead.procam_vertical.not_in(known))
    q = scope.leads(q, resolve_scope(sc))
    return q.count(), q


def check_companies_with_unknown_vertical(sc=None):
    from app import Company
    from app.access import scope
    known = _vertical_values()
    q = _active_companies()
    q = (_nothing(q) if not known else
         q.filter(_filled(Company.vertical), Company.vertical.not_in(known)))
    q = scope.companies(q, resolve_scope(sc))
    return q.count(), q


# ── email intake ─────────────────────────────────────────────────────
def check_email_leads_classified_non_lead(sc=None):
    from app import Lead
    from app.access import scope
    q = scope.leads(_live_leads().filter(
        Lead.source == 'email', _filled(Lead.classification),
        Lead.classification.not_in(
            defs.LEAD_CREATING_CLASSES + (defs.REVIEW_CLASS,))),
        resolve_scope(sc))
    return q.count(), q


def check_review_backlog(sc=None):
    from app import EmailClassification, Lead
    from app.access import scope
    sc = resolve_scope(sc)
    cutoff = datetime.utcnow() - timedelta(days=defs.REVIEW_BACKLOG_DAYS)
    out = []
    for lead in _light(scope.leads(_open_leads().filter(
            Lead.classification == defs.REVIEW_CLASS,
            Lead.created_at < cutoff), sc)).order_by(Lead.id).all():
        out.append((lead, 'classified as needing review'))
    if _everything(sc):
        for r in _light(EmailClassification.query.filter(
                EmailClassification.review_state == 'pending',
                EmailClassification.created_at < cutoff)) \
                .order_by(EmailClassification.id).all():
            out.append((r, 'waiting in Lead Review'))
    return len(out), out


def check_classification_orphans(sc=None):
    from app import EmailClassification, Lead
    q = EmailClassification.query.filter(
        EmailClassification.created_lead_id.isnot(None),
        EmailClassification.created_lead_id.not_in(select(Lead.id)))
    if not _everything(resolve_scope(sc)):
        q = _nothing(q)
    return q.count(), q


def check_quotes_filed_as_sent(sc=None):
    """Emails from outside Procam filed as if Procam sent them.

    Nothing is corrected automatically — some of those leads have since
    been quoted for real. See the report's docstring for the history.
    """
    import os
    from app import LeadEmail
    from app.access import scope
    internal = defs.internal_domains(os.environ.get('INTERNAL_EMAIL_DOMAINS'))
    rows = scope.emails(LeadEmail.query.filter(
        LeadEmail.direction == 'outbound',
        LeadEmail.intake_class.in_(defs.RECEIVED_AS_SENT_CLASSES)),
        resolve_scope(sc)).with_entities(LeadEmail.id,
                                         LeadEmail.from_addr).all()
    ids = []
    for eid, from_addr in rows:
        domain = (from_addr or '').rsplit('@', 1)[-1].lower().strip('> ')
        if domain and domain not in internal:
            ids.append(eid)
    q = (LeadEmail.query.filter(LeadEmail.id.in_(ids)) if ids
         else _nothing(LeadEmail.query))
    return len(ids), q


# ── customers ────────────────────────────────────────────────────────
def check_inactive_customers(sc=None):
    from app import Company, Lead, Opportunity
    from app.access import scope
    from app.services import contact
    cutoff = datetime.utcnow() - timedelta(
        days=30 * defs.INACTIVE_CUSTOMER_MONTHS)
    recent_leads = select(Lead.company_id).where(
        Lead.company_id.isnot(None),
        or_(Lead.created_at >= cutoff, Lead.updated_at >= cutoff))
    recent_opps = select(Opportunity.company_id).where(
        Opportunity.company_id.isnot(None),
        or_(Opportunity.created_at >= cutoff,
            Opportunity.updated_at >= cutoff))
    contacted = select(Lead.company_id).where(
        Lead.company_id.isnot(None),
        Lead.id.in_(contact.contacted_since_select(cutoff)))
    q = scope.companies(_active_companies().filter(
        or_(Company.created_at.is_(None), Company.created_at < cutoff),
        or_(Company.last_activity_at.is_(None),
            Company.last_activity_at < cutoff),
        Company.id.not_in(recent_leads), Company.id.not_in(recent_opps),
        Company.id.not_in(contacted)), resolve_scope(sc))
    return q.count(), q


# ── the registry ─────────────────────────────────────────────────────
class Check(tuple):
    """One check. Still the (key, label, why, severity, route, fn) tuple
    CHECKS always held, so code that unpacks or indexes it keeps working;
    the rest of its definition rides along as attributes."""

    def __new__(cls, key, label, why, severity, route, fn, meta=None):
        self = tuple.__new__(cls, (key, label, why, severity, route, fn))
        meta = meta or {}
        self.key, self.label, self.why = key, label, why
        self.severity, self.route, self.fn = severity, route, fn
        self.suggestion = meta.get('suggestion', '')
        self.entity = meta.get('entity', '')
        self.actions = tuple(meta.get('actions', ()))
        self.report = meta.get('report')
        return self

    def public(self):
        return {'key': self.key, 'label': self.label, 'title': self.label,
                'why': self.why, 'cost': self.why,
                'severity': self.severity, 'route': self.route,
                'suggestion': self.suggestion, 'entity': self.entity,
                'actions': [{'key': a, 'label': defs.BATCH_ACTIONS[a][0],
                             'value': defs.BATCH_ACTIONS[a][1]}
                            for a in self.actions],
                'batch_fix': bool(self.actions)}


_FUNCTIONS = {
    'unowned_leads': check_unowned_leads,
    'leads_of_leavers': check_leads_of_leavers,
    'unowned_opps': check_unowned_opportunities,
    'opps_of_leavers': check_opportunities_of_leavers,
    'no_pic': check_companies_without_pic,
    'accounts_of_leavers': check_companies_of_leavers,
    'tasks_no_owner': check_tasks_without_owner,
    'tasks_of_leavers': check_tasks_of_leavers,
    'stale_leads': check_stale_leads,
    'leads_no_followup': check_leads_without_followup,
    'stale_opps': check_stale_opportunities,
    'opps_no_close_date': check_opportunities_without_close_date,
    'rfq_no_quote': check_rfqs_without_quote,
    'won_no_value': check_won_without_value,
    'won_no_po': check_won_without_po,
    'dupe_po_refs': check_duplicate_po_refs,
    'won_no_handover': check_won_without_handover,
    'lost_no_competitor': check_lost_without_competitor,
    'unlinked_opps': check_unlinked_opportunities,
    'unlinked_leads': check_unlinked_leads,
    'pending_mappings': check_pending_mappings,
    'dupe_companies': check_duplicate_companies,
    'dupe_contacts': check_duplicate_contacts,
    'orphan_records': check_orphan_records,
    'broken_relationships': check_broken_relationships,
    'empty_mandatory': check_empty_mandatory,
    'lead_account_mismatch': check_lead_account_mismatch,
    'lead_unknown_vertical': check_leads_with_unknown_vertical,
    'account_unknown_vertical': check_companies_with_unknown_vertical,
    'email_leads_non_lead': check_email_leads_classified_non_lead,
    'review_backlog': check_review_backlog,
    'classification_orphans': check_classification_orphans,
    'quotes_filed_as_sent': check_quotes_filed_as_sent,
    'inactive_customers': check_inactive_customers,
}

CHECKS = [Check(d['key'], d['title'], d['cost'], d['severity'], d['route'],
                _FUNCTIONS[d['key']], d) for d in defs.CHECKS]


def _coerce(entry):
    return entry if isinstance(entry, Check) else Check(*tuple(entry)[:6])


def find(key):
    for entry in CHECKS:
        c = _coerce(entry)
        if c.key == key:
            return c
    return None


def run(check, sc):
    """(count, detail) for one check inside a scope."""
    fn = check.fn
    try:
        takes_scope = bool(_inspect.signature(fn).parameters)
    except (TypeError, ValueError):
        takes_scope = True
    return fn(sc) if takes_scope else fn()


# ── describing records ───────────────────────────────────────────────
def _describe(key, row):
    """Turn a record into something a person can act on.

    A row may arrive as (record, note) when the check knows something the
    record alone does not say — which lead is missing, which account it
    is wrongly linked to.
    """
    note = ''
    if isinstance(row, tuple):
        row, note = row
    d = _describe_record(row)
    # Routes are joined to the URL prefix and opened by the page. Only a
    # path inside the CRM is a route: a task's stored action_route could
    # otherwise be "javascript:" or another site.
    route = d.get('route') or ''
    if not route.startswith('/') or route.startswith('//'):
        d['route'] = ''
    if note:
        d['meta'] = ' · '.join(x for x in (note, d['meta']) if x)
    return d


def _describe_record(row):
    from app import (Company, Contact, EmailClassification, Lead,
                     LeadActivity, LeadEmail, LeadNote, Opportunity)

    if isinstance(row, Lead):
        return {'id': row.id, 'kind': 'Lead',
                'name': row.company or f'Lead #{row.id}',
                'meta': ' · '.join(x for x in (
                    row.project, row.stage,
                    (f'owner {row.assigned_to}' if row.assigned_to
                     else 'no owner'),
                    (f'follow-up {row.followup_date}' if row.followup_date
                     else ''),
                    row.procam_vertical) if x),
                'route': f'/app?lead={row.id}'}
    if isinstance(row, Opportunity):
        return {'id': row.id, 'kind': 'Opportunity',
                'name': row.opp_number or f'Opportunity #{row.id}',
                'meta': ' · '.join(x for x in (
                    row.stage,
                    (f'{float(row.value_inr):,.0f} INR'
                     if row.value_inr else 'no value'),
                    (f'owner {row.owner_emp_code}' if row.owner_emp_code
                     else 'no owner'),
                    (f'closes {row.expected_close_date}'
                     if row.expected_close_date else '')) if x),
                'route': (f'/companies/{row.company_id}' if row.company_id
                          else f'/app?opp={row.id}')}
    if isinstance(row, Company):
        return {'id': row.id, 'kind': 'Account',
                'name': row.name or f'Account #{row.id}',
                'meta': ' · '.join(x for x in (
                    row.industry, row.city, row.vertical,
                    (f'PIC {row.pic_emp_code}' if row.pic_emp_code
                     else 'no account owner')) if x),
                'route': f'/companies/{row.id}'}
    if isinstance(row, Contact):
        return {'id': row.id, 'kind': 'Contact', 'name': row.name,
                'meta': ' · '.join(x for x in (
                    row.designation, row.company, row.email,
                    row.mobile or row.phone) if x),
                'route': f'/app?contact={row.id}'}
    if isinstance(row, LeadEmail):
        return {'id': row.id, 'kind': 'Email',
                'name': row.subject or f'Email #{row.id}',
                'meta': ' · '.join(str(x) for x in (
                    row.intake_class, row.from_addr,
                    f'lead #{row.lead_id}') if x),
                'route': f'/app?lead={row.lead_id}'}
    if isinstance(row, (LeadNote, LeadActivity)):
        kind = 'Note' if isinstance(row, LeadNote) else 'Activity'
        return {'id': row.id, 'kind': kind, 'name': f'{kind} #{row.id}',
                'meta': f'lead #{row.lead_id}', 'route': ''}
    if isinstance(row, EmailClassification):
        return {'id': row.id, 'kind': 'Classification',
                'name': row.subject or f'Classification #{row.id}',
                'meta': ' · '.join(str(x) for x in (
                    row.classification, row.from_addr, row.review_state,
                    (f'created lead #{row.created_lead_id}'
                     if row.created_lead_id else '')) if x),
                'route': '/lead-review'}

    name = type(row).__name__
    if name == 'WonHandover':
        return {'id': row.id, 'kind': 'Handover',
                'name': row.account_name or f'Handover #{row.id}',
                'meta': ' · '.join(str(x) for x in (
                    row.status,
                    (f'{row.po_type or "PO"} {row.po_ref}' if row.po_ref
                     else 'no PO recorded'),
                    (f'{float(row.won_value):,.0f} INR' if row.won_value
                     else '')) if x),
                'route': f'/handovers?id={row.id}'}
    if name == 'RFQ':
        return {'id': row.id, 'kind': 'RFQ',
                'name': row.rfq_number or f'RFQ #{row.id}',
                'meta': ' · '.join(str(x) for x in (
                    row.subject, row.status,
                    f'quote by {row.quote_by_date}' if row.quote_by_date
                    else '',
                    (f'driver {row.lead_driver}' if row.lead_driver
                     else 'no driver')) if x),
                'route': f'/rfqs/{row.id}'}
    if name == 'DataMappingQueue':
        return {'id': row.id, 'kind': 'Mapping',
                'name': row.raw_value or f'Mapping #{row.id}',
                'meta': ' · '.join(str(x) for x in (
                    row.entity_type, row.reason) if x),
                'route': '/app'}

    # TaskInstance and anything else
    return {'id': getattr(row, 'id', None), 'kind': 'Task',
            'name': (getattr(row, 'entity_display', None)
                     or getattr(row, 'task_key', None)
                     or f'Record #{getattr(row, "id", "?")}'),
            'meta': ' · '.join(str(x) for x in (
                getattr(row, 'status', ''),
                getattr(row, 'owner_user_id', '') or 'no owner') if x),
            'route': getattr(row, 'action_route', '') or ''}


#: The columns a record's description reads, per model. Checks and exports
#: can list thousands of leads and emails, and a whole row drags the stored
#: email body along with it.
_DESCRIBED = {
    'Lead': ('id', 'company', 'project', 'stage', 'assigned_to',
             'followup_date', 'procam_vertical', 'company_id'),
    'LeadEmail': ('id', 'subject', 'intake_class', 'from_addr', 'lead_id'),
    'EmailClassification': ('id', 'subject', 'classification', 'from_addr',
                            'review_state', 'created_lead_id'),
}


def _light(q):
    from sqlalchemy.orm import load_only
    try:
        entity = q.column_descriptions[0]['entity']
        cols = _DESCRIBED.get(entity.__name__)
        if cols:
            return q.options(load_only(*[getattr(entity, c) for c in cols]))
    except Exception:
        pass
    return q


def _ordered(q):
    try:
        entity = q.column_descriptions[0]['entity']
        return _light(q).order_by(entity.id)
    except Exception:
        return q


def records_for(key, limit=500, *, sc=None, page=1, per_page=None):
    """The records behind one Data Quality number, one page at a time.

    A count with no way to reach what it counts is a complaint, not a
    tool — this is what makes each figure actionable.
    """
    check = find(key)
    if check is None:
        return None
    sc = resolve_scope(sc)
    per_page = max(1, int(per_page or limit))
    page = max(1, int(page or 1))
    start = (page - 1) * per_page

    count, detail = run(check, sc)
    base = dict(check.public(), count=count, page=page, per_page=per_page)

    # Duplicates come back as {what matched: [record, ...]} rather than a
    # query, because the finding IS the grouping.
    if isinstance(detail, dict):
        names = sorted(detail)
        groups = [{'name': name,
                   'records': [_describe(key, r) for r in detail[name]]}
                  for name in names[start:start + per_page]]
        return dict(base, grouped=True, groups=groups,
                    truncated=len(names) > start + per_page,
                    pages=max(1, math.ceil(len(names) / per_page)))

    rows = []
    total = count or 0
    if isinstance(detail, (list, tuple)):
        # Some checks do their grouping in Python and hand back a plain
        # list. Without this they showed a count and drilled into
        # nothing, which is the one thing this function exists to avoid.
        total = len(detail)
        rows = list(detail[start:start + per_page + 1])
    elif detail is not None:
        try:
            rows = _ordered(detail).offset(start).limit(per_page + 1).all()
        except Exception:
            db.session.rollback()
            try:
                rows = detail.all()[start:start + per_page + 1]
            except Exception:
                rows = []

    truncated = len(rows) > per_page
    return dict(base, grouped=False,
                records=[_describe(key, r) for r in rows[:per_page]],
                truncated=truncated,
                pages=max(1, math.ceil(total / per_page)))


def affected_ids(key, ids, sc):
    """Which of `ids` this check still flags, inside the scope."""
    check = find(key)
    if check is None or not ids:
        return set()
    _count, detail = run(check, sc)
    wanted = set(ids)
    if isinstance(detail, dict):
        return {r.id for rows in detail.values() for r in rows
                if r.id in wanted}
    if isinstance(detail, (list, tuple)):
        return {(r[0] if isinstance(r, tuple) else r).id for r in detail
                if (r[0] if isinstance(r, tuple) else r).id in wanted}
    if detail is None:
        return set()
    entity = detail.column_descriptions[0]['entity']
    return {r[0] for r in detail.filter(entity.id.in_(wanted))
            .with_entities(entity.id).all()}


def summary(sc=None):
    sc = resolve_scope(sc)
    out = []
    for entry in CHECKS:
        check = _coerce(entry)
        row = check.public()
        try:
            count, _detail = run(check, sc)
        except Exception as exc:
            # One failing check must not take the dashboard down, and a
            # failed statement must not poison the ones after it.
            db.session.rollback()
            out.append(dict(row, severity='low', count=None,
                            error=str(exc)[:120]))
            continue
        out.append(dict(row, count=count))
    return out


def export_rows(key, sc=None):
    """(header, rows) for a CSV of one check, inside the scope.

    Values are raw here; the caller passes every row through
    spreadsheet_safe, because names and subjects are written by outsiders.
    """
    data = records_for(key, sc=sc, per_page=defs.EXPORT_MAX)
    if data is None:
        return None
    header = ['check', 'kind', 'id', 'name', 'details', 'link']
    rows = []
    if data['grouped']:
        header = ['check', 'group', 'kind', 'id', 'name', 'details', 'link']
        for g in data['groups']:
            for r in g['records']:
                rows.append([key, g['name'], r.get('kind', ''), r['id'],
                             r['name'], r['meta'], r['route']])
    else:
        for r in data['records']:
            rows.append([key, r.get('kind', ''), r['id'], r['name'],
                         r['meta'], r['route']])
    return header, rows, data['truncated']


# ── trends ───────────────────────────────────────────────────────────
SNAPSHOT_SCOPE = 'company'


def snapshot_all(today=None):
    """Write today's company-wide count for every check. Idempotent.

    Run daily by scripts/data_quality_snapshot.py. A second run on the
    same day replaces that day's counts rather than adding a row, so a
    retried timer cannot draw a spike. Counts are measured first and
    written together, so a check that fails (recorded as no count) cannot
    roll back the rows before it.
    """
    from app.models.data_quality import DataQualitySnapshot as Snap
    today = today or date.today()
    sc = system_scope()
    counts = []
    for entry in CHECKS:
        check = _coerce(entry)
        try:
            count, _detail = run(check, sc)
        except Exception:
            db.session.rollback()
            count = None
        counts.append((check.key, count))

    now = datetime.utcnow()
    existing = {r.check_key: r for r in Snap.query.filter_by(
        snapshot_date=today, scope=SNAPSHOT_SCOPE).all()}
    for key, count in counts:
        row = existing.get(key)
        if row is None:
            row = Snap(snapshot_date=today, check_key=key,
                       scope=SNAPSHOT_SCOPE)
            db.session.add(row)
        row.count = count
        row.taken_at = now
    db.session.commit()
    return [{'key': k, 'count': c} for k, c in counts]


def trends(points=defs.TREND_POINTS):
    """{check key: [(date, count), ...]} for the last `points` snapshot
    days, oldest first. Empty — never an error — when the snapshot table
    has not been created yet."""
    try:
        from app.models.data_quality import DataQualitySnapshot as Snap
        dates = [r[0] for r in db.session.query(Snap.snapshot_date)
                 .filter(Snap.scope == SNAPSHOT_SCOPE).distinct()
                 .order_by(Snap.snapshot_date.desc()).limit(points).all()]
        if not dates:
            return {}
        out = {}
        for r in Snap.query.filter(Snap.scope == SNAPSHOT_SCOPE,
                                   Snap.snapshot_date >= min(dates)) \
                .order_by(Snap.snapshot_date).all():
            out.setdefault(r.check_key, []).append((r.snapshot_date, r.count))
        return out
    except Exception:
        db.session.rollback()
        return {}


def sparkline(series, width=120, height=28):
    """SVG polyline points for a trend, or '' with fewer than two points."""
    values = [c for _d, c in series if c is not None]
    if len(values) < 2:
        return ''
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1
    step = width / (len(values) - 1)
    return ' '.join(
        f'{i * step:.1f},{height - 2 - (v - lo) / span * (height - 4):.1f}'
        for i, v in enumerate(values))
