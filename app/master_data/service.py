"""Reading and changing master data — §59-62.

Everything that needs a vocabulary asks here, so a value added in the
admin screen is immediately available everywhere (§60): forms, filters,
reports, assignment, and the Excel template lookups (§45).
"""
from app import db
from app.models.master_data import MasterList, MasterItem, SYSTEM_LISTS


# Where each list is actually referenced, so §61 can tell whether a value
# is in use before anyone deletes it.  (model attribute, column) pairs
# resolved lazily — several live in blueprints that import late.
USAGE = {
    'vertical': [
        ('app:Employee', 'vertical'),
        ('app:Lead', 'procam_vertical'),
    ],
    'industry': [
        ('app:Company', 'industry'),
        ('app:Lead', 'industry'),
        ('app:Contact', 'industry'),
    ],
    'lost_reason':   [('app:Lead', 'lost_reason')],
    'source':        [('app:Lead', 'source')],
    'account_stage': [('app:Company', 'dev_stage')],
    'relationship':  [('presales.models:AccountRelationshipTag', 'tag')],
    'priority':      [('app:Company', 'priority')],
}


def _resolve(path):
    module_path, _, attr = path.partition(':')
    import importlib
    return getattr(importlib.import_module(module_path), attr)


def items(list_key, include_inactive=False):
    """Values in a list, ordered for display."""
    q = MasterItem.query.filter_by(list_key=list_key)
    if not include_inactive:
        q = q.filter_by(is_active=True)
    return q.order_by(MasterItem.sort_order, MasterItem.label).all()


def values(list_key):
    """Just the codes — for validating an Excel upload or a form post."""
    return [i.code for i in items(list_key)]


def labels(list_key):
    return {i.code: i.label for i in items(list_key, include_inactive=True)}


def usage_count(list_key, code):
    """How many live records reference this value.  §61: if any do, the
    value may be deactivated but never deleted."""
    total = 0
    for path, column in USAGE.get(list_key, []):
        try:
            model = _resolve(path)
            total += db.session.query(model).filter(
                getattr(model, column) == code).count()
        except Exception:
            continue
    return total


def add_item(list_key, code, label=None, description='', meta=None,
             sort_order=None, actor=None):
    code = (code or '').strip()
    if not code:
        raise ValueError('A code is required')
    existing = MasterItem.query.filter_by(list_key=list_key,
                                          code=code).first()
    if existing is not None:
        # Re-adding a previously retired value reactivates it rather than
        # colliding on the unique constraint.
        existing.is_active = True
        if label:
            existing.label = label
        db.session.commit()
        return existing

    if sort_order is None:
        last = (MasterItem.query.filter_by(list_key=list_key)
                .order_by(MasterItem.sort_order.desc()).first())
        sort_order = ((last.sort_order or 0) + 10) if last else 10

    item = MasterItem(list_key=list_key, code=code,
                      label=(label or code).strip(),
                      description=description or '', meta=meta or {},
                      sort_order=sort_order, is_active=True,
                      created_by=actor or '')
    db.session.add(item)
    db.session.commit()
    return item


def update_item(item_id, **fields):
    item = MasterItem.query.get(item_id)
    if item is None:
        raise ValueError('No such item')
    for key in ('label', 'description', 'sort_order', 'is_active', 'meta'):
        if key in fields and fields[key] is not None:
            setattr(item, key, fields[key])
    db.session.commit()
    return item


def delete_item(item_id):
    """Delete only if nothing references it — otherwise deactivate (§61).

    Returns (deleted: bool, used_by: int).
    """
    item = MasterItem.query.get(item_id)
    if item is None:
        raise ValueError('No such item')
    used = usage_count(item.list_key, item.code)
    if used:
        item.is_active = False
        db.session.commit()
        return False, used
    db.session.delete(item)
    db.session.commit()
    return True, 0


def all_lists():
    registry = {l.key: l for l in MasterList.query.order_by(
        MasterList.sort_order, MasterList.label).all()}
    grouped = {}
    for item in MasterItem.query.order_by(MasterItem.list_key,
                                          MasterItem.sort_order,
                                          MasterItem.label).all():
        grouped.setdefault(item.list_key, []).append(item)
    return [registry[k].to_dict(grouped.get(k, []))
            for k in registry]


def ensure_lists():
    """Create any missing list registrations.  Safe to call repeatedly."""
    known = {l.key for l in MasterList.query.all()}
    created = 0
    for order, (key, label, desc) in enumerate(SYSTEM_LISTS):
        if key in known:
            continue
        db.session.add(MasterList(key=key, label=label, description=desc,
                                  is_system=True, sort_order=order * 10))
        created += 1
    if created:
        db.session.commit()
    return created
