"""Global CRM — searching, counting and reading the relationship database.

Everything in here starts from ``app.access.scope``. There is no
"unscoped" variant of any query: a search is ``scope.contacts()``
narrowed further, never widened, so a rep cannot reach another desk's
people by guessing a filter or an id.

Two rules decide what a reader is shown about an account, and both come
from code that already existed:

  * ``app.access.scope.contacts()`` decides which *people* are listed —
    the viewer's own, their accounts', and the accounts they are working
    a lead on.
  * ``app.access.records.company_access()`` decides how much of the
    *account* behind a person is shown. A viewer who reaches an account
    only because they have a lead on it gets ``partial``: their own
    leads, RFQs and quotes (which the scoped queries give them anyway)
    and this contact's own history — not the account's whole timeline.

"Last interaction" means this *person*, not their employer. It is
``app.services.contact``'s definition — an activity or an email, never
``updated_at`` — matched to the contact through the address the enquiry
came from, plus anything logged against them on the account timeline.
Rolling the account's whole history up onto every person at it would
report six colleagues as warm because one of them was rung, and "never
contacted" would read as nearly zero on a database where it is nearly
everybody. The account-level answer still exists, and who_handles uses
it, because there the question really is about the organisation.

Vocabularies are never written down here. Relationship types, verticals,
industries and services all come from ``app.master_data.service``, so a
value an administrator adds appears in the filters without a deploy.

Filtering and paging happen in SQL. The contact table is the largest in
the CRM and the one most often asked a vague question; a search that
loads it into Python to filter is a search that times out the week the
import lands.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import String, case, func, null, or_, select, union_all
from sqlalchemy.orm import aliased

from app.access import records as rec
from app.access import scope as scope_mod

#: Permission required to change who looks after a contact. Deliberately
#: the one that already governs account ownership rather than a new key:
#: a contact and its account are the same relationship seen from two
#: sides, and two permissions would eventually disagree.
ASSIGN_PERM = 'accounts.assign'

#: A page of search results. Larger pages are capped rather than refused
#: — the Copilot asks for "everything" and should get a sane first page.
DEFAULT_PER_PAGE = 50
MAX_PER_PAGE = 200

#: How long a contact may go untouched before Global CRM calls them cold.
STALE_DAYS = 90
#: "Recently added" on the dashboard.
RECENT_DAYS = 30
#: Rows behind each dashboard tile. The tile gives the count; the list is
#: a sample to act on, not the whole answer.
SAMPLE = 25

#: Master Data lists this module reads.
LIST_RELATIONSHIP = 'relationship'
LIST_VERTICAL = 'vertical'
LIST_INDUSTRY = 'industry'
LIST_SERVICE = 'service'

#: Sentinel for "the caller did not mention this field", so assign() can
#: tell "leave it alone" apart from "clear it".
UNCHANGED = object()


class DirectoryRefused(Exception):
    """A refusal with the HTTP status and the JSON the route should send."""

    def __init__(self, message, status=400, **extra):
        super().__init__(message)
        self.message = message
        self.status = status
        self.extra = extra


# ── schema safety ────────────────────────────────────────────────────
def ensure_tables():
    """Create this package's tables if the migration has not run yet.

    New tables only — nothing here alters a table another module reads,
    so a boot that lands before the migration costs a CREATE and no
    downtime.
    """
    from app import db
    from app.directory.models import TABLES
    try:
        db.metadata.create_all(bind=db.engine, tables=list(TABLES),
                               checkfirst=True)
        return True
    except Exception:                                   # pragma: no cover
        return False


def _has_relationship_table():
    from app import db
    try:
        return db.inspect(db.engine).has_table('contact_relationships')
    except Exception:                                   # pragma: no cover
        return False


# ── vocabularies ─────────────────────────────────────────────────────
def _master(list_key):
    try:
        from app.master_data import service as md
        return [{'code': i.code, 'label': i.label} for i in md.items(list_key)]
    except Exception:                                   # pragma: no cover
        return []


def vocabularies():
    """Every list the search form offers, straight from Master Data."""
    return {
        'relationship': _master(LIST_RELATIONSHIP),
        'vertical': _master(LIST_VERTICAL),
        'industry': _master(LIST_INDUSTRY),
        'service': _master(LIST_SERVICE),
    }


# ── the shared column definitions ────────────────────────────────────
#
# Search, the dashboard and the relationship view must agree about what a
# contact's vertical, PIC and relationship *are*, or the tile and the list
# behind it will disagree in front of a user. They agree because they all
# read this one builder.

def _blank_to_null(col):
    """'' and NULL both mean "not recorded"; the database thinks they are
    different, and a COUNT grouped on them reports two empty buckets."""
    return func.nullif(func.trim(func.coalesce(col, '')), '')


class _Cols:
    """The expressions a scoped contact query exposes."""

    __slots__ = ('account', 'rel', 'account_id', 'company_name', 'vertical',
                 'pic', 'secondary_pic', 'industry', 'country', 'city',
                 'relationship', 'last_ts', 'designation')


def base_query(sc):
    """A scoped Contact query with the account and relationship joined on.

    Returns ``(query, cols)``. The Company join is aliased on purpose:
    ``scope.contacts()`` already carries subqueries that select from
    ``companies``, and an unaliased join would let SQLAlchemy correlate
    them against the outer table and quietly answer a different question.
    """
    from app import Company, Contact

    acct = aliased(Company, name='dir_account')
    cols = _Cols()
    cols.account = acct
    # A contact may be linked through either column; account_id is the
    # §7 link, company_id the older one, and both are in live data.
    cols.account_id = func.coalesce(Contact.account_id, Contact.company_id)

    q = scope_mod.contacts(sc=sc).outerjoin(acct, acct.id == cols.account_id)

    if _has_relationship_table():
        from app.directory.models import ContactRelationship
        rel = aliased(ContactRelationship, name='dir_rel')
        q = q.outerjoin(rel, rel.contact_id == Contact.id)
        rel_type, rel_secondary, rel_vertical = (
            rel.relationship_type, rel.secondary_pic, rel.vertical)
    else:
        # The migration has not run. Everything still works; the three
        # facts it stores simply read as "not recorded".
        rel = None
        rel_type = rel_secondary = rel_vertical = null()
    cols.rel = rel

    cols.company_name = func.coalesce(_blank_to_null(acct.name),
                                      _blank_to_null(Contact.company))
    cols.designation = _blank_to_null(Contact.designation)
    cols.vertical = func.coalesce(_blank_to_null(rel_vertical),
                                  _blank_to_null(acct.vertical))
    cols.pic = func.coalesce(_blank_to_null(Contact.assigned_to),
                             _blank_to_null(acct.pic_emp_code))
    cols.secondary_pic = func.coalesce(_blank_to_null(rel_secondary),
                                       _blank_to_null(acct.secondary_pic_emp_code))
    # agent_type is where the relationship used to be typed by hand; it
    # is the fallback so nobody has to re-classify 4,000 people before
    # the filter becomes useful.
    cols.relationship = func.coalesce(_blank_to_null(rel_type),
                                      _blank_to_null(Contact.agent_type))
    cols.industry = func.coalesce(_blank_to_null(Contact.industry),
                                  _blank_to_null(acct.industry))
    cols.country = func.coalesce(_blank_to_null(Contact.country),
                                 _blank_to_null(acct.country))
    cols.city = func.coalesce(_blank_to_null(Contact.city),
                              _blank_to_null(acct.city))

    q, cols.last_ts = _join_last_interaction(q, cols)
    return q, cols


def _account_interaction_sq():
    """``account_id -> most recent interaction``, grouped in SQL.

    This is the *account's* answer, used by who_handles to say whether an
    organisation is live. It is deliberately not what a contact's last
    interaction means: a meeting with one buyer is not contact with the
    other six people at the same employer, and rolling it up would report
    a whole account as warm because one person in it was rung.
    """
    from app import Lead, LeadActivity, LeadEmail
    from presales.models import AccountActivity

    parts = [
        select(AccountActivity.account_id.label('account_id'),
               AccountActivity.occurred_at.label('ts'))
        .where(AccountActivity.occurred_at.isnot(None),
               AccountActivity.account_id.isnot(None)),
        select(Lead.company_id.label('account_id'),
               LeadActivity.occurred_at.label('ts'))
        .select_from(LeadActivity).join(Lead, Lead.id == LeadActivity.lead_id)
        .where(Lead.company_id.isnot(None),
               LeadActivity.occurred_at.isnot(None)),
        select(Lead.company_id.label('account_id'),
               LeadEmail.sent_or_received_at.label('ts'))
        .select_from(LeadEmail).join(Lead, Lead.id == LeadEmail.lead_id)
        .where(Lead.company_id.isnot(None),
               LeadEmail.sent_or_received_at.isnot(None)),
    ]
    u = union_all(*parts).subquery('dir_acct_touch')
    return (select(u.c.account_id.label('account_id'),
                   func.max(u.c.ts).label('ts'))
            .group_by(u.c.account_id).subquery('dir_acct_last'))


def _contact_interaction_sq():
    """``contact_id -> most recent interaction logged against that person``."""
    from presales.models import AccountActivity
    return (select(AccountActivity.contact_id.label('contact_id'),
                   func.max(AccountActivity.occurred_at).label('ts'))
            .where(AccountActivity.contact_id.isnot(None),
                   AccountActivity.occurred_at.isnot(None))
            .group_by(AccountActivity.contact_id)
            .subquery('dir_contact_last'))


def _email_interaction_sq():
    """``email address -> most recent interaction``, from the lead trail.

    Leads carry no contact id, so the only honest link between a person
    and the calls and emails on a lead is the address the enquiry came
    from. "Interaction" is the definition ``app.services.contact`` fixed
    for the whole CRM — an activity OR an email, never ``updated_at``,
    because a typo correction is not a conversation.
    """
    from app import Lead, LeadActivity, LeadEmail

    addr = func.lower(func.trim(Lead.email))
    live = (Lead.email.isnot(None), func.trim(Lead.email) != '')
    parts = [
        select(addr.label('email'), LeadActivity.occurred_at.label('ts'))
        .select_from(LeadActivity).join(Lead, Lead.id == LeadActivity.lead_id)
        .where(LeadActivity.occurred_at.isnot(None), *live),
        select(addr.label('email'),
               LeadEmail.sent_or_received_at.label('ts'))
        .select_from(LeadEmail).join(Lead, Lead.id == LeadEmail.lead_id)
        .where(LeadEmail.sent_or_received_at.isnot(None), *live),
    ]
    u = union_all(*parts).subquery('dir_mail_touch')
    return (select(u.c.email.label('email'), func.max(u.c.ts).label('ts'))
            .group_by(u.c.email).subquery('dir_mail_last'))


def _join_last_interaction(q, cols):
    """Attach the two per-person interaction subqueries and return the
    later of the two.

    A CASE rather than GREATEST/MAX-of-two: SQLite's two-argument ``max``
    answers NULL when either side is NULL, which would report everybody
    without an account-level note as never contacted.
    """
    from app import Contact

    cont_sq = _contact_interaction_sq()
    mail_sq = _email_interaction_sq()
    q = (q.outerjoin(cont_sq, cont_sq.c.contact_id == Contact.id)
          .outerjoin(mail_sq,
                     mail_sq.c.email == func.lower(func.trim(
                         func.coalesce(Contact.email, '')))))
    a, c = cont_sq.c.ts, mail_sq.c.ts
    return q, case((a.is_(None), c), (c.is_(None), a), (a > c, a), else_=c)


# ── search ───────────────────────────────────────────────────────────
def _like(value):
    return '%' + str(value).strip().replace('%', r'\%') + '%'


def _int(value, default=None):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _service_account_ids(service):
    """Accounts that have asked us for this service.

    A person does not have a service; the work their employer sends us
    does. The filter therefore reads the two places a service is actually
    written down — what a lead is for, and what an RFQ's scope says.
    """
    from app import Lead
    from app.models.rfq import RFQ

    like = _like(service)
    lead_ids = (select(Lead.company_id)
                .where(Lead.company_id.isnot(None),
                       Lead.products.ilike(like)))
    rfq_ids = (select(RFQ.account_id)
               .where(RFQ.account_id.isnot(None),
                      or_(RFQ.scope.ilike(like), RFQ.subject.ilike(like))))
    return union_all(lead_ids, rfq_ids)


def search_contacts(sc, q=None, company=None, designation=None, industry=None,
                    country=None, city=None, vertical=None, account_id=None,
                    pic=None, relationship=None, service=None,
                    last_interaction_days=None, page=1,
                    per_page=DEFAULT_PER_PAGE):
    """A page of contacts the viewer may see, filtered and counted in SQL.

    ``last_interaction_days`` reads both ways on purpose, because the two
    questions people ask of a relationship database are opposites:
    ``30`` means "spoken to in the last 30 days"; ``-90`` means "nothing
    for 90 days or more", which is the list somebody has to work through.
    """
    from app import Contact

    query, cols = base_query(sc)

    if q:
        term = _like(q)
        query = query.filter(or_(
            Contact.name.ilike(term), Contact.email.ilike(term),
            Contact.phone.ilike(term), Contact.mobile.ilike(term),
            Contact.designation.ilike(term), Contact.company.ilike(term),
            cols.account.name.ilike(term)))
    if company:
        term = _like(company)
        query = query.filter(or_(Contact.company.ilike(term),
                                 cols.account.name.ilike(term)))
    if designation:
        query = query.filter(Contact.designation.ilike(_like(designation)))
    if industry:
        query = query.filter(cols.industry.ilike(_like(industry)))
    if country:
        query = query.filter(cols.country.ilike(_like(country)))
    if city:
        query = query.filter(cols.city.ilike(_like(city)))
    if vertical:
        query = query.filter(cols.vertical.ilike(_like(vertical)))
    if account_id not in (None, '', 0):
        aid = _int(account_id)
        # An unparseable id is no account at all, not "every account".
        query = query.filter(cols.account_id == (aid if aid is not None else -1))
    if pic:
        who = str(pic).strip()
        query = query.filter(or_(cols.pic == who, cols.secondary_pic == who))
    if relationship:
        query = query.filter(cols.relationship.ilike(_like(relationship)))
    if service:
        query = query.filter(cols.account_id.in_(_service_account_ids(service)))
    days = _int(last_interaction_days)
    if days is not None:
        cutoff = datetime.utcnow() - timedelta(days=abs(days))
        if days >= 0:
            query = query.filter(cols.last_ts.isnot(None),
                                 cols.last_ts >= cutoff)
        else:
            query = query.filter(or_(cols.last_ts.is_(None),
                                     cols.last_ts < cutoff))

    page = max(1, _int(page, 1) or 1)
    per_page = min(MAX_PER_PAGE, max(1, _int(per_page, DEFAULT_PER_PAGE)
                                     or DEFAULT_PER_PAGE))

    rows_q = query.with_entities(
        Contact.id, Contact.name, Contact.email, Contact.phone,
        Contact.mobile, Contact.designation, Contact.created_at,
        cols.account_id.label('account_id'),
        cols.company_name.label('company_name'),
        cols.vertical.label('vertical'), cols.pic.label('pic'),
        cols.secondary_pic.label('secondary_pic'),
        cols.relationship.label('relationship'),
        cols.industry.label('industry'), cols.country.label('country'),
        cols.city.label('city'), cols.last_ts.label('last_interaction'))

    total = query.with_entities(func.count(func.distinct(Contact.id))).scalar() or 0
    rows = (rows_q
            # NULLS LAST without relying on the dialect's default, so the
            # people nobody has spoken to sit at the bottom everywhere.
            .order_by(case((cols.last_ts.is_(None), 1), else_=0),
                      cols.last_ts.desc(), Contact.name.asc())
            .limit(per_page).offset((page - 1) * per_page).all())

    names = _employee_names([r.pic for r in rows]
                            + [r.secondary_pic for r in rows])
    now = datetime.utcnow()
    return {
        'items': [_row(r, names, now) for r in rows],
        'total': total, 'page': page, 'per_page': per_page,
        'pages': max(1, -(-total // per_page)),
    }


def _row(r, names, now):
    last = r.last_interaction
    return {
        'id': r.id, 'name': r.name or '',
        'designation': r.designation or '',
        'email': r.email or '', 'phone': r.phone or r.mobile or '',
        'account_id': r.account_id, 'company': r.company_name or '',
        'vertical': r.vertical or '', 'relationship': r.relationship or '',
        'industry': r.industry or '', 'country': r.country or '',
        'city': r.city or '',
        'pic': r.pic or '', 'pic_name': names.get(r.pic or '', ''),
        'secondary_pic': r.secondary_pic or '',
        'secondary_pic_name': names.get(r.secondary_pic or '', ''),
        'created_at': str(r.created_at)[:10] if r.created_at else '',
        'last_interaction': str(last)[:16] if last else '',
        'last_interaction_days': (now - last).days if last else None,
    }


def _employee_names(codes):
    from app import Employee
    wanted = {c for c in codes if c}
    if not wanted:
        return {}
    return {e.emp_code: (e.name or e.emp_code) for e in
            Employee.query.filter(Employee.emp_code.in_(wanted)).all()}


# ── dashboard ────────────────────────────────────────────────────────
#: The tiles Release 4 names. The *values* behind them come from Master
#: Data; these are only the words used to recognise each family, so a
#: site that calls its prospects "Targets" still gets a full breakdown
#: under `by_relationship` even if the tile reads zero.
_TILES = (('customers', 'customer'), ('prospects', 'prospect'),
          ('vendors', 'vendor'), ('partners', 'partner'),
          ('competitor_contacts', 'competitor'))


def dashboard(sc):
    """The shape of the viewer's relationship database."""
    from app import Contact

    query, cols = base_query(sc)
    now = datetime.utcnow()
    stale_cutoff = now - timedelta(days=STALE_DAYS)
    recent_cutoff = now - timedelta(days=RECENT_DAYS)

    def count(*conditions):
        q = query
        for c in conditions:
            q = q.filter(c)
        return q.with_entities(func.count(func.distinct(Contact.id))).scalar() or 0

    def breakdown(expr, limit=25):
        rows = (query.with_entities(expr.label('k'),
                                    func.count(func.distinct(Contact.id)))
                .group_by(expr).order_by(func.count(func.distinct(Contact.id)).desc())
                .limit(limit).all())
        return [{'key': k or '—', 'count': n} for k, n in rows]

    total = count()
    by_relationship = breakdown(cols.relationship)
    tiles = {name: count(cols.relationship.ilike(f'%{word}%'))
             for name, word in _TILES}
    # A competitor is an account classification (§19), not only a word
    # typed against a person, so the accounts tagged as one count too.
    tiles['competitor_contacts'] = count(or_(
        cols.relationship.ilike('%competitor%'),
        cols.account_id.in_(_competitor_account_ids())))

    missing = {
        'no_email': count(_blank_to_null(Contact.email).is_(None)),
        'no_phone': count(_blank_to_null(Contact.phone).is_(None),
                          _blank_to_null(Contact.mobile).is_(None)),
        'no_designation': count(cols.designation.is_(None)),
        'no_account': count(cols.account_id.is_(None)),
        'no_pic': count(cols.pic.is_(None)),
        'no_vertical': count(cols.vertical.is_(None)),
        'no_relationship': count(cols.relationship.is_(None)),
    }
    incomplete = count(or_(
        _blank_to_null(Contact.email).is_(None),
        cols.account_id.is_(None), cols.pic.is_(None),
        cols.vertical.is_(None)))

    return {
        'total': total,
        **tiles,
        'by_relationship': by_relationship,
        'by_vertical': breakdown(cols.vertical),
        'by_country': breakdown(cols.country),
        'by_industry': breakdown(cols.industry),
        'by_pic': _named(breakdown(cols.pic)),
        'recently_added': {
            'count': count(Contact.created_at >= recent_cutoff),
            'rows': _sample(query, cols, Contact.created_at >= recent_cutoff,
                            order=Contact.created_at.desc()),
        },
        'never_contacted': {
            'count': count(cols.last_ts.is_(None)),
            'rows': _sample(query, cols, cols.last_ts.is_(None)),
        },
        'idle_90': {
            'days': STALE_DAYS,
            'count': count(cols.last_ts.isnot(None),
                           cols.last_ts < stale_cutoff),
            'rows': _sample(query, cols, cols.last_ts.isnot(None),
                            cols.last_ts < stale_cutoff,
                            order=cols.last_ts.asc()),
        },
        'missing_information': {'total': incomplete, 'breakdown': missing},
    }


def _competitor_account_ids():
    """Accounts classified as competitors, from the §5 relationship tags."""
    from app import Company
    try:
        from presales.models import AccountRelationshipTag as Tag
    except Exception:                                   # pragma: no cover
        return select(Company.id).where(False)
    return select(Tag.account_id).where(Tag.tag.ilike('%competitor%'))


def _named(rows):
    names = _employee_names([r['key'] for r in rows])
    for r in rows:
        r['label'] = names.get(r['key'], r['key'])
    return rows


def _sample(query, cols, *conditions, order=None):
    from app import Contact
    q = query
    for c in conditions:
        q = q.filter(c)
    rows = (q.with_entities(Contact.id, Contact.name, Contact.email,
                            Contact.phone, Contact.mobile,
                            Contact.designation, Contact.created_at,
                            cols.account_id.label('account_id'),
                            cols.company_name.label('company_name'),
                            cols.vertical.label('vertical'),
                            cols.pic.label('pic'),
                            cols.secondary_pic.label('secondary_pic'),
                            cols.relationship.label('relationship'),
                            cols.industry.label('industry'),
                            cols.country.label('country'),
                            cols.city.label('city'),
                            cols.last_ts.label('last_interaction'))
            .order_by(order if order is not None else Contact.name.asc())
            .limit(SAMPLE).all())
    names = _employee_names([r.pic for r in rows])
    now = datetime.utcnow()
    return [_row(r, names, now) for r in rows]


# ── one person ───────────────────────────────────────────────────────
def _contact_in_scope(sc, contact_id):
    from app import Contact
    cid = _int(contact_id)
    if cid is None:
        return None
    return scope_mod.contacts(sc=sc).filter(Contact.id == cid).first()


def relationship_view(sc, contact_id):
    """Everything Procam knows about one person, inside the boundary.

    Returns ``None`` when the contact does not exist *or* sits outside
    the viewer's scope. The two answer the same way on purpose: the rest
    of the CRM 404s rather than 403s on a record you may not see, because
    a 403 confirms the record exists, and the existence of a person at a
    named company is itself information.
    """
    from app import Company, Contact, Employee, Lead, LeadNote
    from app.models.quote import Quote
    from app.models.rfq import RFQ
    from app.services import lead_value
    from presales.models import AccountActivity

    contact = _contact_in_scope(sc, contact_id)
    if contact is None:
        return None

    account_id = contact.account_id or contact.company_id
    account = Company.query.get(account_id) if account_id else None
    access = rec.company_access(account, sc=sc) if account else rec.ROUTING
    full = access == rec.FULL

    rel = _relationship_row(contact.id)
    pic = (contact.assigned_to or
           (account.pic_emp_code if account else '') or '')
    secondary = ((rel.secondary_pic if rel else '') or
                 (account.secondary_pic_emp_code if account else '') or '')
    vertical = ((rel.vertical if rel else '') or
                (account.vertical if account else '') or '')
    names = _employee_names([pic, secondary])

    # Deals. Every one of these is a scoped query, so a partial viewer
    # sees their own work on this account and nothing of anybody else's.
    leads = (scope_mod.leads(sc=sc)
             .filter(_lead_match(contact, account_id))
             .order_by(Lead.updated_at.desc()).limit(50).all())
    lead_ids = [l.id for l in leads]
    rfqs = (scope_mod.rfqs(sc=sc).filter(RFQ.account_id == account_id)
            .order_by(RFQ.received_date.desc()).limit(50).all()
            if account_id else [])
    quotes = (scope_mod.quotes(sc=sc).filter(Quote.account_id == account_id)
              .order_by(Quote.quote_date.desc()).limit(50).all()
              if account_id else [])

    # The account timeline belongs to the account. A partial viewer gets
    # only what was logged against this person — company_access() says
    # they may see the contacts, not the account's whole history.
    acts = AccountActivity.query
    if full and account_id:
        acts = acts.filter(or_(AccountActivity.account_id == account_id,
                               AccountActivity.contact_id == contact.id))
    else:
        acts = acts.filter(AccountActivity.contact_id == contact.id)
    activities = acts.order_by(AccountActivity.occurred_at.desc()).limit(100).all()

    notes = []
    if lead_ids:
        notes = (LeadNote.query
                 .filter(LeadNote.lead_id.in_(lead_ids),
                         LeadNote.is_deleted.isnot(True))
                 .order_by(LeadNote.id.desc()).limit(50).all())

    interactions = _interaction_history(lead_ids, activities)
    last = interactions[0]['at'] if interactions else None

    return {
        'contact': {
            'id': contact.id, 'name': contact.name or '',
            'designation': contact.designation or '',
            'email': contact.email or '',
            'phone': contact.phone or '', 'mobile': contact.mobile or '',
            'linkedin': contact.linkedin or '',
            'city': contact.city or (account.city if account else ''),
            'country': contact.country or (account.country if account else ''),
            'industry': contact.industry or (account.industry if account else ''),
            'company': (account.name if account else '') or contact.company or '',
            'relationship': ((rel.relationship_type if rel else '')
                             or contact.agent_type or ''),
            'decision_role': contact.decision_role or '',
            'relationship_strength': contact.relationship_strength or '',
            'is_active': bool(contact.is_active),
            'notes': contact.notes or '',
        },
        'account': ({'id': account.id, 'name': account.name,
                     'access': access,
                     'industry': account.industry or '',
                     'city': account.city or '', 'state': account.state or '',
                     'country': account.country or '',
                     'stage': account.dev_stage or '' if full else '',
                     'priority': account.priority or '' if full else '',
                     'next_action_at': (str(account.next_action_at)
                                        if full and account.next_action_at
                                        else '')}
                    if account else None),
        'ownership': {
            'pic': pic, 'pic_name': names.get(pic, ''),
            'secondary_pic': secondary,
            'secondary_pic_name': names.get(secondary, ''),
            'vertical': vertical,
            'history': [h.to_dict() for h in _assignment_history(contact.id)],
        },
        'leads': [{'id': l.id, 'company': l.company or '',
                   'project': l.project or '', 'stage': l.stage or '',
                   'owner': l.assigned_to or '',
                   'value': lead_value.format_inr(lead_value.value_inr(l)),
                   'value_inr': float(lead_value.value_inr(l) or 0),
                   'next_action': l.next_action or '',
                   'followup_date': (str(l.followup_date)
                                     if l.followup_date else '')}
                  for l in leads],
        'rfqs': [{'id': r.id, 'number': r.rfq_number, 'subject': r.subject or '',
                  'status': r.status or '',
                  'received': str(r.received_date) if r.received_date else ''}
                 for r in rfqs],
        'quotes': [{'id': q.id, 'number': q.quote_number,
                    'status': q.status or '',
                    'total': lead_value.format_inr(q.total_amount),
                    'date': str(q.quote_date) if q.quote_date else ''}
                   for q in quotes],
        'meetings': [a.to_dict() for a in activities
                     if (a.kind or '') in ('Meeting', 'Visit')],
        'projects': _projects_for(account_id, contact.id),
        'notes': [{'id': n.id, 'text': (n.note_text or '')[:1000],
                   'type': n.note_type or '', 'author': n.author_name
                   or n.author or ''} for n in notes],
        'interactions': interactions[:100],
        'last_interaction': str(last)[:16] if last else '',
        'last_interaction_days': ((datetime.utcnow() - last).days
                                  if last else None),
        'next_action': _next_action(leads, activities, account if full else None),
    }


def _lead_match(contact, account_id):
    """Leads that are this person's: their account's, or carrying their
    email address (an enquiry that arrived before the account was linked)."""
    from app import Lead
    conds = []
    if account_id:
        conds.append(Lead.company_id == account_id)
    if contact.email:
        conds.append(func.lower(Lead.email) == contact.email.strip().lower())
    if not conds:
        return func.coalesce(Lead.id, 0) < 0      # matches nothing
    return or_(*conds)


def _relationship_row(contact_id):
    if not _has_relationship_table():
        return None
    from app.directory.models import ContactRelationship
    return ContactRelationship.query.filter_by(contact_id=contact_id).first()


def _assignment_history(contact_id):
    from app import db
    try:
        if not db.inspect(db.engine).has_table('contact_assignments'):
            return []
    except Exception:                                   # pragma: no cover
        return []
    from app.directory.models import ContactAssignment
    return (ContactAssignment.query.filter_by(contact_id=contact_id)
            .order_by(ContactAssignment.assigned_at.desc()).limit(50).all())


def _interaction_history(lead_ids, activities):
    """One timeline out of the account log, the lead activities and the
    email trail — the same three sources the last-interaction column is
    computed from, so the headline figure and the list below it agree."""
    from app import LeadActivity, LeadEmail

    out = [{'at': a.occurred_at, 'kind': a.kind or 'Activity',
            'subject': a.subject or '', 'by': a.performed_by or '',
            'source': 'account'}
           for a in activities if a.occurred_at]
    if lead_ids:
        for a in (LeadActivity.query.filter(LeadActivity.lead_id.in_(lead_ids))
                  .order_by(LeadActivity.occurred_at.desc()).limit(100)):
            if a.occurred_at:
                out.append({'at': a.occurred_at, 'kind': a.kind or 'activity',
                            'subject': a.subject or '',
                            'by': a.performed_by or '', 'source': 'lead'})
        for e in (LeadEmail.query.filter(LeadEmail.lead_id.in_(lead_ids))
                  .order_by(LeadEmail.sent_or_received_at.desc()).limit(100)):
            if e.sent_or_received_at:
                out.append({'at': e.sent_or_received_at,
                            'kind': f'email {e.direction or ""}'.strip(),
                            'subject': (e.subject or '')[:200],
                            'by': e.created_by or '', 'source': 'email'})
    out.sort(key=lambda r: r['at'], reverse=True)
    for r in out:
        r['at_display'] = str(r['at'])[:16]
    return out


def _projects_for(account_id, contact_id):
    """Project Intelligence rows this person or their account is on."""
    try:
        from presales.models_projects import (Project, ProjectAccount,
                                              ProjectContact)
    except Exception:                                   # pragma: no cover
        return []
    conds = [Project.id.in_(select(ProjectContact.project_id)
                            .where(ProjectContact.contact_id == contact_id))]
    if account_id:
        conds.append(Project.id.in_(select(ProjectAccount.project_id)
                                    .where(ProjectAccount.account_id == account_id)))
    try:
        rows = (Project.query.filter(or_(*conds))
                .order_by(Project.id.desc()).limit(25).all())
    except Exception:                                   # pragma: no cover
        return []
    return [{'id': p.id, 'name': p.name, 'stage': p.stage or '',
             'vertical': p.procam_vertical or '',
             'pic': p.pic_emp_code or ''} for p in rows]


def _next_action(leads, activities, account):
    """The next thing somebody has committed to doing, soonest first."""
    options = []
    for l in leads:
        if l.next_action or l.followup_date:
            options.append({'what': l.next_action or 'Follow up',
                            'when': str(l.followup_date) if l.followup_date else '',
                            'where': f'lead {l.id}'})
    for a in activities:
        if a.next_action:
            options.append({'what': a.next_action,
                            'when': str(a.next_action_at) if a.next_action_at else '',
                            'where': 'account activity'})
    if account is not None and account.next_action_at:
        options.append({'what': 'Account review',
                        'when': str(account.next_action_at),
                        'where': 'account'})
    dated = sorted([o for o in options if o['when']], key=lambda o: o['when'])
    return (dated or options or [None])[0]


# ── assignment ───────────────────────────────────────────────────────
def _active_employee(code, label):
    from app import Employee
    code = (code or '').strip()
    if not code:
        return ''
    emp = Employee.query.filter_by(emp_code=code, is_active=True).first()
    if emp is None:
        raise DirectoryRefused(f'{label} is not an active employee', 400)
    return emp.emp_code


def _master_value(value, list_key, label):
    """Validate against Master Data, never against a list written here."""
    value = (value or '').strip()
    if not value:
        return ''
    from app.master_data import service as md
    allowed = {v.lower() for v in md.values(list_key)}
    # An empty list means the administrator has not populated that
    # vocabulary yet; refusing everything would make the screen unusable.
    if allowed and value.lower() not in allowed:
        raise DirectoryRefused(f'{label} is not one of the configured '
                               f'{list_key} values', 400)
    return value


def assign(sc, contact_id, primary_pic=UNCHANGED, secondary_pic=UNCHANGED,
           vertical=UNCHANGED, account_id=UNCHANGED,
           relationship_type=UNCHANGED, reason=None, actor=None):
    """Change who looks after a contact, and record that it happened.

    Refuses without ``accounts.assign`` — the same permission that governs
    account ownership, because this is the same decision. Refuses without
    a reason: the reason is the only part of a reassignment that is still
    useful a year later.
    """
    from app import Company, Contact, db
    from app.services import audit

    if not sc.can(ASSIGN_PERM):
        raise DirectoryRefused(
            'You do not have permission to reassign contacts. Ask an '
            'administrator.', 403, need=ASSIGN_PERM)

    contact = _contact_in_scope(sc, contact_id)
    if contact is None:
        raise DirectoryRefused('No such contact', 404)

    reason = (reason or '').strip()
    if not reason:
        raise DirectoryRefused("A reason is required; it is recorded in the "
                               "contact's history", 400)

    ensure_tables()
    from app.directory.models import ContactAssignment, ContactRelationship

    rel = ContactRelationship.query.filter_by(contact_id=contact.id).first()
    if rel is None:
        rel = ContactRelationship(contact_id=contact.id)
        db.session.add(rel)

    before = {'primary_pic': contact.assigned_to or '',
              'account_id': contact.account_id or contact.company_id,
              'secondary_pic': rel.secondary_pic or '',
              'vertical': rel.vertical or '',
              'relationship_type': rel.relationship_type or ''}

    if primary_pic is not UNCHANGED:
        contact.assigned_to = _active_employee(primary_pic, 'The PIC') or None
    if secondary_pic is not UNCHANGED:
        rel.secondary_pic = _active_employee(secondary_pic,
                                             'The secondary PIC') or None
    if (contact.assigned_to and rel.secondary_pic
            and contact.assigned_to == rel.secondary_pic):
        raise DirectoryRefused('The secondary PIC must be a different person '
                               'from the PIC', 400)
    if vertical is not UNCHANGED:
        rel.vertical = _master_value(vertical, LIST_VERTICAL,
                                     'That vertical') or None
    if relationship_type is not UNCHANGED:
        rel.relationship_type = _master_value(
            relationship_type, LIST_RELATIONSHIP,
            'That relationship type') or None
    if account_id is not UNCHANGED:
        aid = _int(account_id)
        if account_id in (None, '', 0):
            contact.account_id = None
        else:
            if aid is None:
                raise DirectoryRefused('The account must be an id', 400)
            account = Company.query.filter_by(id=aid, is_active=True).first()
            if account is None:
                raise DirectoryRefused('No such active account', 404)
            contact.account_id = account.id
            if not contact.company:
                contact.company = account.name
    rel.updated_by = actor or sc.emp_code or ''

    after = {'primary_pic': contact.assigned_to or '',
             'account_id': contact.account_id or contact.company_id,
             'secondary_pic': rel.secondary_pic or '',
             'vertical': rel.vertical or '',
             'relationship_type': rel.relationship_type or ''}
    old, new = audit.changes(before, after)
    if not old and not new:
        db.session.rollback()
        return {'changed': False, 'contact_id': contact.id, **after}

    db.session.add(ContactAssignment(
        contact_id=contact.id,
        previous_pic_code=before['primary_pic'] or None,
        new_pic_code=after['primary_pic'] or None,
        assigned_by=(actor or sc.emp_code or 'system'),
        reason=reason,
        changes={'old': old, 'new': new}))
    audit.record_change('directory.contact.assign', 'contact', contact.id,
                        before, after, reason=reason,
                        actor=actor or sc.emp_code)
    db.session.commit()
    return {'changed': True, 'contact_id': contact.id, **after}


# ── Group D: who handles this account? ───────────────────────────────
def who_handles(sc, q, limit=20):
    """Answer "who looks after this customer?" from any way of naming it.

    Scope deliberately does *not* hide the answer. §5.1 and §6.6 both turn
    on anyone being able to find out that an organisation is already a
    Procam account and who owns it — that is the whole point of the
    directory, and withholding it is what produces two people calling the
    same customer. What scope decides is how much *beyond* the routing
    answer a reader gets, through ``company_access()``: a reader with no
    claim on the account sees the owners, whether it is live, and when it
    was last touched, and nothing else.
    """
    from app import Company, Contact, Opportunity

    term = (q or '').strip()
    if not term:
        return {'query': '', 'matches': []}
    like = _like(term)
    bare = term.lower().lstrip('@')

    # A free-text company name somebody typed on a contact or a lead is
    # the alias people actually use; it is how "BHEL Trichy" finds
    # "Bharat Heavy Electricals Limited".
    alias_ids = union_all(
        select(Contact.account_id).where(Contact.account_id.isnot(None),
                                         Contact.company.ilike(like)),
        select(Contact.company_id).where(Contact.company_id.isnot(None),
                                         Contact.company.ilike(like)))
    rows = (Company.query.filter(Company.is_active.isnot(False))
            .filter(or_(
                Company.name.ilike(like),
                Company.website.ilike(like),
                # email_domains is a JSON list; matched as text so this
                # works on SQLite as well as Postgres.
                func.lower(func.cast(Company.email_domains, String))
                .like(f'%{bare}%'),
                Company.industry.ilike(like),
                Company.vertical.ilike(like),
                Company.city.ilike(like),
                Company.state.ilike(like),
                Company.pic_emp_code.ilike(like),
                Company.secondary_pic_emp_code.ilike(like),
                Company.id.in_(alias_ids),
                Company.id.in_(_group_ids(like)),
                Company.id.in_(_pic_name_account_ids(like))))
            .order_by(Company.name.asc()).limit(max(1, _int(limit, 20)))
            .all())

    # One read each for the owners, the groups and the last interactions
    # of the whole result set: a directory lookup that fires three
    # queries per row is the one people stop using.
    owners = _owner_directory(rows)
    groups = _group_names(rows)
    touched = _last_interactions([c.id for c in rows])

    out = []
    for c in rows:
        access = rec.company_access(c, sc=sc)
        opps = (Opportunity.query
                .filter(Opportunity.company_id == c.id,
                        Opportunity.won_at.is_(None),
                        Opportunity.lost_at.is_(None)))
        row = {
            'account_id': c.id, 'name': c.name,
            'group': groups.get(c.parent_account_id, ''),
            'industry': c.industry or '', 'vertical': c.vertical or '',
            'city': c.city or '', 'state': c.state or '',
            'country': c.country or '',
            'access': access,
            'owners': [o for o in (
                _owner(c.pic_emp_code, 'PIC', owners),
                _owner(c.secondary_pic_emp_code, 'Secondary PIC', owners),
                _owner(c.backup_pic_emp_code, 'Backup PIC', owners)) if o],
            'active_opportunities': opps.count(),
            'last_interaction': touched.get(c.id, ''),
        }
        if access in (rec.FULL, rec.PARTIAL):
            # Only a reader with a claim on the account sees which deals
            # are open; everyone else gets the count and the owner to ring.
            row['opportunities'] = [
                {'id': o.id, 'number': o.opp_number, 'title': o.title or '',
                 'stage': o.stage or '', 'owner': o.owner_emp_code or ''}
                for o in opps.order_by(Opportunity.id.desc()).limit(20)]
        out.append(row)
    return {'query': term, 'matches': out}


def _owner(code, role, directory):
    """One owner line. An owner the employee master does not know still
    gets a line: "nobody can tell you who this is" is the answer somebody
    has to act on, and hiding it reads as "unowned"."""
    if not code:
        return None
    emp = directory.get(code)
    return {'emp_code': code, 'role': role,
            'name': (emp.name or code) if emp else code,
            'email': (emp.email or '') if emp else '',
            'vertical': (emp.vertical or '') if emp else '',
            'is_active': bool(emp.is_active) if emp else False}


def _owner_directory(companies):
    """Every owner of every matched account, in one read."""
    from app import Employee
    codes = {c for row in companies for c in
             (row.pic_emp_code, row.secondary_pic_emp_code,
              row.backup_pic_emp_code) if c}
    if not codes:
        return {}
    return {e.emp_code: e for e in
            Employee.query.filter(Employee.emp_code.in_(codes)).all()}


def _group_names(companies):
    """The parent (group) name of every matched account, in one read."""
    from app import Company
    ids = {c.parent_account_id for c in companies if c.parent_account_id}
    if not ids:
        return {}
    return {c.id: c.name for c in
            Company.query.filter(Company.id.in_(ids)).all()}


def _group_ids(like):
    """Accounts whose parent (the group) matches."""
    from app import Company
    parent = aliased(Company, name='dir_group')
    return (select(Company.id)
            .select_from(Company).join(parent, parent.id == Company.parent_account_id)
            .where(parent.name.ilike(like)))


def _pic_name_account_ids(like):
    """Accounts owned by somebody whose *name* matches, not only their code."""
    from app import Company, Employee
    codes = select(Employee.emp_code).where(Employee.name.ilike(like))
    return (select(Company.id)
            .where(or_(Company.pic_emp_code.in_(codes),
                       Company.secondary_pic_emp_code.in_(codes))))


def _last_interactions(account_ids):
    """``{account_id: 'YYYY-MM-DD HH:MM'}`` for a page of accounts.

    The account-level answer, not the per-contact one: here the question
    is whether the organisation is live, which anything logged against
    anybody there answers.
    """
    from app import db
    ids = [i for i in account_ids if i]
    if not ids:
        return {}
    sq = _account_interaction_sq()
    rows = db.session.execute(
        select(sq.c.account_id, sq.c.ts)
        .where(sq.c.account_id.in_(ids))).all()
    return {aid: str(ts)[:16] for aid, ts in rows if ts}
