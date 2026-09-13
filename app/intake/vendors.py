"""
Vendor Master — the domains that supply us rather than buy from us.

    CATEGORIES            the controlled list an admin picks from
    category_of()         any stored vendor_type → one of those
    normalise_domain()    whatever was typed → a bare domain, or a reason
    vendor_rows()         the list, filtered
    create_vendor() / update_vendor() / set_active()

Mail from a registered domain (or any subdomain of it) is filed as rate
sourcing at step 6 of the classifier instead of creating a lead. That is
the whole effect, and it is a strong one: a customer's domain added here
by mistake stops their enquiries becoming leads. So nothing is ever
hard-deleted — a row is deactivated, which stops it matching and keeps
the record of who added it and why.

Changes to domain, category and active state are audited by the session
listener (VendorDomain is in its WATCH list); nothing here needs to call
the audit service for those.
"""
import re
from datetime import datetime

from app import db


#: (stored value, label). The stored values are the lowercase words the
#: table already held — 'shipping line', 'transporter', 'airline',
#: 'other' — so rows seeded or learned before this screen existed are
#: already in a category and need no data migration.
CATEGORIES = (
    ('supplier', 'Supplier'),
    ('shipping line', 'Shipping Line'),
    ('transporter', 'Transporter'),
    ('cha', 'CHA (customs house agent)'),
    ('warehouse partner', 'Warehouse Partner'),
    ('airline', 'Airline'),
    ('overseas agent', 'Overseas Agent'),
    ('other', 'Other'),
)
CATEGORY_KEYS = tuple(k for k, _ in CATEGORIES)
CATEGORY_LABELS = dict(CATEGORIES)
DEFAULT_CATEGORY = 'other'

#: Free text the column accepted before it was controlled. Anything not
#: here and not a category is shown as Other rather than rejected: the
#: row still matches mail, and refusing to display it would hide a rule
#: that is quietly in force.
_ALIASES = {
    'vendor': 'supplier', 'suppliers': 'supplier',
    'shipping': 'shipping line', 'shipping lines': 'shipping line',
    'liner': 'shipping line', 'line': 'shipping line', 'carrier': 'shipping line',
    'transport': 'transporter', 'transporters': 'transporter',
    'trucker': 'transporter', 'fleet owner': 'transporter',
    'customs': 'cha', 'customs broker': 'cha', 'customs house agent': 'cha',
    'warehouse': 'warehouse partner', 'warehousing': 'warehouse partner',
    'airlines': 'airline', 'air cargo': 'airline',
    'agent': 'overseas agent', 'overseas': 'overseas agent',
    'overseas agents': 'overseas agent',
}


def category_of(value):
    """Any stored or submitted vendor_type → a category key.

    Case, underscores and hyphens are ignored, so 'Shipping_Line',
    'shipping-line' and 'shipping line' are one category.
    """
    v = re.sub(r'[\s_\-]+', ' ', str(value or '')).strip().lower()
    if v in CATEGORY_KEYS:
        return v
    return _ALIASES.get(v, DEFAULT_CATEGORY)


def known_category(value):
    """The key for a submitted category, or None when it is not one.

    Stricter than category_of(): reading an old row may fall back to
    Other, but an admin typing a category that does not exist should be
    told, not silently filed under Other.
    """
    v = re.sub(r'[\s_\-]+', ' ', str(value or '')).strip().lower()
    if not v:
        return None
    if v in CATEGORY_KEYS:
        return v
    for key, label in CATEGORIES:
        if v == label.lower():
            return key
    return _ALIASES.get(v)


# ─── the domain itself ───────────────────────────────────────────────────
_HOST = re.compile(r'[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?')


def normalise_domain(raw):
    """(domain, error). Exactly one is None.

    Lenient about the harmless things people paste — capitals, a leading
    @, a leading www. — and strict about everything else. An email
    address or a URL is refused rather than cut down, unlike the Account
    Master's domain box: there the worst case is a lead routed to the
    wrong owner, here it is a customer's mail silently filed as a
    supplier's, so the admin should see exactly what is being registered.
    """
    d = str(raw or '').strip().lower()
    if not d:
        return None, 'Enter a domain, e.g. maersk.com'
    if re.search(r'\s', d):
        return None, 'A domain cannot contain spaces'
    d = d.lstrip('@')
    if '@' in d:
        return None, ('That looks like an email address. Enter only the '
                      'part after the @')
    if '://' in d or '/' in d or '?' in d or '#' in d or ':' in d:
        return None, 'That looks like a web address. Enter only the domain'
    d = d.strip('.')
    if d.startswith('www.'):
        d = d[4:]
    if '.' not in d:
        return None, 'A domain must contain a dot, e.g. maersk.com'
    labels = d.split('.')
    if len(d) > 200 or not all(_HOST.fullmatch(l) for l in labels):
        return None, 'That is not a valid domain name'
    if labels[-1].isdigit():
        return None, 'An IP address is not a domain'

    from email_ingest.parser import PERSONAL_DOMAINS, _PUBLIC_SUFFIX_SLD
    # Subdomains match, so registering a suffix registers everything
    # under it: "co.in" would file every Indian company's mail as a
    # supplier's. The parser's own suffix and free-mail lists are used so
    # the two cannot disagree about what a suffix is.
    if len(labels) == 2 and labels[0] in _PUBLIC_SUFFIX_SLD:
        return None, f'{d} is a public suffix, not a company domain'
    if d in PERSONAL_DOMAINS:
        return None, (f'{d} is free mail — customers write from it too. '
                      f'Registering it would stop their enquiries '
                      f'becoming leads')
    return d, None


# ─── reading ─────────────────────────────────────────────────────────────
def to_row(v, names=None):
    names = names or {}
    return {
        'id': v.id,
        'domain': v.domain,
        'name': v.name or '',
        'notes': v.notes or '',
        'category': category_of(v.vendor_type),
        'category_label': CATEGORY_LABELS[category_of(v.vendor_type)],
        # What is actually stored, so an admin can see a legacy value
        # that is being displayed as Other.
        'vendor_type': v.vendor_type or '',
        'learned_from': v.learned_from or 'manual',
        'learned_label': _learned_label(v.learned_from),
        'rejection_count': v.rejection_count or 0,
        'is_active': bool(v.is_active),
        'added_by': v.added_by or '',
        'added_by_name': names.get(v.added_by or '', ''),
        'created_at': str(v.created_at)[:16] if v.created_at else '',
        'updated_by': v.updated_by or '',
        'updated_by_name': names.get(v.updated_by or '', ''),
        'updated_at': str(v.updated_at)[:16] if v.updated_at else '',
    }


def _learned_label(value):
    return {
        'manual': 'Added by hand',
        'learned_from_rejections': 'Learned from review rejections',
    }.get(value or 'manual', value or 'Added by hand')


def vendor_rows(*, category=None, active=None, q=None, limit=2000):
    """The Vendor Master, newest first.

    The category filter runs over category_of() rather than the column,
    so a legacy 'agent' row appears under Overseas Agent where the screen
    shows it. The table is small — hundreds, not hundreds of thousands —
    so filtering after the query costs nothing worth an index.
    """
    from app import Employee, VendorDomain

    query = VendorDomain.query
    if active in (True, False):
        query = query.filter(VendorDomain.is_active.is_(active))
    if q:
        like = f'%{q.strip().lower()}%'
        query = query.filter(db.or_(VendorDomain.domain.ilike(like),
                                    VendorDomain.name.ilike(like),
                                    VendorDomain.notes.ilike(like)))
    rows = query.order_by(VendorDomain.created_at.desc(),
                          VendorDomain.id.desc()).limit(limit).all()
    want = known_category(category) if category else None
    if category and want is None:
        return []
    names = {e.emp_code: e.name for e in Employee.query.with_entities(
        Employee.emp_code, Employee.name).all()}
    return [to_row(v, names) for v in rows
            if want is None or category_of(v.vendor_type) == want]


def vendor_summary():
    from app import VendorDomain
    counts = {k: 0 for k in CATEGORY_KEYS}
    active = inactive = 0
    for vendor_type, is_active in VendorDomain.query.with_entities(
            VendorDomain.vendor_type, VendorDomain.is_active).all():
        if is_active:
            active += 1
            counts[category_of(vendor_type)] += 1
        else:
            inactive += 1
    return {'active': active, 'inactive': inactive,
            'by_category': [{'key': k, 'label': CATEGORY_LABELS[k],
                             'count': counts[k]} for k in CATEGORY_KEYS]}


# ─── writing ─────────────────────────────────────────────────────────────
#: (row, error, http status). The route turns this into a response; the
#: status lives here because "duplicate" is a fact about the data.
def create_vendor(*, domain, category=None, name=None, notes=None,
                  actor=None, learned_from='manual'):
    from app import VendorDomain

    clean, err = normalise_domain(domain)
    if err:
        return None, err, 400
    key = known_category(category) if category else DEFAULT_CATEGORY
    if key is None:
        return None, f'Unknown category {category!r}', 400

    existing = VendorDomain.query.filter(
        db.func.lower(VendorDomain.domain) == clean).first()
    if existing is not None:
        state = 'active' if existing.is_active else 'deactivated'
        return existing, (f'{clean} is already in the Vendor Master '
                          f'({state}). Edit or reactivate that row '
                          f'instead.'), 409

    row = VendorDomain(domain=clean, vendor_type=key,
                       name=_text(name, 200), notes=_text(notes, 2000),
                       learned_from=learned_from, rejection_count=0,
                       is_active=True, added_by=actor,
                       created_at=datetime.utcnow())
    db.session.add(row)
    db.session.flush()
    return row, None, 201


def update_vendor(vendor_id, *, category=None, name=None, notes=None,
                  actor=None):
    """Category, name and notes. The domain is not editable: a changed
    domain is a different rule, and should be added as one so the old
    row's history stays attached to what it actually matched."""
    from app import VendorDomain

    row = _get(vendor_id)
    if row is None:
        return None, 'Vendor not found', 404
    if category is not None:
        key = known_category(category)
        if key is None:
            return None, f'Unknown category {category!r}', 400
        row.vendor_type = key
    if name is not None:
        row.name = _text(name, 200)
    if notes is not None:
        row.notes = _text(notes, 2000)
    row.updated_at = datetime.utcnow()
    row.updated_by = actor
    return row, None, 200


def set_active(vendor_id, active, *, actor=None):
    row = _get(vendor_id)
    if row is None:
        return None, 'Vendor not found', 404
    row.is_active = bool(active)
    row.updated_at = datetime.utcnow()
    row.updated_by = actor
    return row, None, 200


def _get(vendor_id):
    from app import VendorDomain
    try:
        return db.session.get(VendorDomain, int(vendor_id))
    except (TypeError, ValueError):
        return None


def _text(value, cap):
    v = str(value or '').strip()
    return v[:cap] or None
