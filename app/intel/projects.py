"""
Project intelligence — one project, one timeline.

The failure this module exists to prevent: three trade bulletins report
the same refinery expansion in the same week, and the CRM ends up with
three projects, each with one update, none of which looks important. A
project is a thing in the world; the articles are sightings of it.

How de-duplication works
------------------------
Every sighting is reduced to a key of three normalised parts —

    name     lower-cased, punctuation stripped, and the words that carry
             no identity removed ("project", "limited", "pvt", "phase")
    owner    the owning company or group, through the same normaliser
             with the company suffixes removed
    location the place, normalised, with "dist"/"district"/"taluka"
             dropped

An incoming item matches an existing project when the full key matches,
or when the names match and neither the owner nor the location
*contradicts* — a bulletin that omits the owner is still about the same
project. On a match the item becomes a `ProjectUpdate` on the existing
project; it never becomes a second project.

What is never done: no sighting overwrites a confirmed fact with an
unverified one, and no field is filled in from a source that did not
state it. A blank on the screen means nobody has said, which is
information; a guess would not be.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime

from app import db
# The stage vocabulary lives in the package __init__ so the migration
# can seed the Master Data list from it without importing the Flask app.
from app.intel import INTEL_STAGES
from app.services import audit
from app.services import sales_rules as rules

log = logging.getLogger(__name__)

# ── the external lifecycle, mapped onto the CRM's project stages ─────
STAGE_CODES = tuple(code for code, _l, _p, _k in INTEL_STAGES)
STAGE_LABELS = {code: label for code, label, _p, _k in INTEL_STAGES}
#: intel stage → the presales PROJECT_STAGES value
STAGE_TO_PROJECT = {code: project for code, _l, project, _k in INTEL_STAGES}
STAGE_ORDER = {code: i for i, code in enumerate(STAGE_CODES)}

#: Confidence, strongest first — used to decide whether a new sighting
#: may overwrite an existing fact.
CONFIDENCE_RANK = {'confirmed': 3, 'reported': 2, 'unverified': 1, '': 0}

OPPORTUNITY_STATUSES = ('not_started', 'lead_created', 'opportunity',
                        'not_relevant')


# ─────────────────────────────────────────────────────────────────────
# Normalisation
# ─────────────────────────────────────────────────────────────────────
_PUNCT = re.compile(r'[^a-z0-9 ]+')
_WS = re.compile(r'\s+')

#: Words that appear in a project's name without identifying it. Removed
#: so "Dahej Phase-II Expansion Project" and "Dahej expansion phase 2"
#: reduce to the same key.
_NAME_NOISE = {
    'project', 'projects', 'plant', 'the', 'a', 'an', 'of', 'at', 'in',
    'for', 'and', 'phase', 'ph', 'unit', 'new', 'proposed', 'expansion',
    'limited', 'ltd', 'pvt', 'private', 'company', 'co', 'corporation',
    'corp', 'inc', 'llp', 'india', 'indias',
}
_OWNER_NOISE = {
    'limited', 'ltd', 'pvt', 'private', 'company', 'co', 'corporation',
    'corp', 'inc', 'llp', 'plc', 'group', 'holdings', 'industries',
    'enterprises', 'the', 'and', 'of', 'india', 'indian',
}
_PLACE_NOISE = {
    'dist', 'district', 'taluka', 'tehsil', 'village', 'near', 'the',
    'city', 'town', 'state', 'of', 'in', 'at',
}
#: Roman numerals seen in phase names, so "phase-II" and "phase 2" agree.
_ROMAN = {'i': '1', 'ii': '2', 'iii': '3', 'iv': '4', 'v': '5',
          'vi': '6', 'vii': '7', 'viii': '8', 'ix': '9', 'x': '10'}


def _singular(word):
    """"Petrochemicals" and "petrochemical" are the same word here.

    A crude rule on purpose: a stemmer would also collapse words that
    are genuinely different, and a wrong merge is worse than a missed
    one — a missed merge shows two projects, which somebody can fix,
    while a wrong merge hides one project inside another.
    """
    if len(word) > 4 and word.endswith('s') and not word.endswith(('ss',
                                                                   'us',
                                                                   'is')):
        return word[:-1]
    return word


def _tokens(text, noise):
    text = _PUNCT.sub(' ', (text or '').lower())
    out = []
    for word in _WS.sub(' ', text).strip().split():
        word = _ROMAN.get(word, word)
        if word in noise:
            continue
        word = _singular(word)
        if word and word not in noise:
            out.append(word)
    return out


def name_key(name):
    """A project name reduced to the words that identify it."""
    return ' '.join(sorted(set(_tokens(name, _NAME_NOISE))))


def owner_key(owner):
    return ' '.join(sorted(set(_tokens(owner, _OWNER_NOISE))))


def location_key(location):
    return ' '.join(sorted(set(_tokens(location, _PLACE_NOISE))))


def match_key(name, owner='', location=''):
    return f'{name_key(name)}|{owner_key(owner)}|{location_key(location)}'


def _compatible(a, b):
    """Two values of the same key part do not contradict each other.

    Blank never contradicts: a bulletin that did not name the owner is
    not evidence of a different owner. Otherwise one must contain the
    other, so "Jamnagar" and "Jamnagar Gujarat" agree.
    """
    if not a or not b:
        return True
    if a == b:
        return True
    sa, sb = set(a.split()), set(b.split())
    return sa <= sb or sb <= sa


# ─────────────────────────────────────────────────────────────────────
# Reading what a sighting says
# ─────────────────────────────────────────────────────────────────────
def detect_stage(*texts):
    """The furthest stage the text gives evidence for, or ''.

    Keyword evidence, not inference: if no phrase matches, the answer is
    "we cannot tell", and the project keeps the stage it had.
    """
    blob = ' '.join(t for t in texts if t).lower()
    if not blob:
        return ''
    best = ''
    for code, _label, _project, keywords in INTEL_STAGES:
        if any(k in blob for k in keywords):
            if not best or STAGE_ORDER[code] > STAGE_ORDER[best]:
                best = code
    return best


def extracted_entities(item, stage=''):
    """What was pulled out of the item, and where from.

    Only values the source actually carried: the structured payload
    fields, and the stage phrase that matched. Nothing is inferred from
    the absence of a field.
    """
    payload = dict(getattr(item, 'payload', None) or {})
    organisations = [payload.get(k) for k in
                     ('owner_group', 'epc_contractor', 'pmc',
                      'technology_provider')]
    suppliers = payload.get('equipment_suppliers') or []
    if isinstance(suppliers, str):
        suppliers = [s.strip() for s in suppliers.split(',') if s.strip()]
    out = {
        'organisations': [o for o in organisations if o],
        'equipment_suppliers': list(suppliers),
        'places': [p for p in (payload.get('location'),
                               payload.get('state')) if p],
        'industry': payload.get('industry') or '',
        'stage_evidence': stage or '',
        'title': (getattr(item, 'title', '') or '')[:300],
    }
    return {k: v for k, v in out.items() if v}


def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    for fmt in ('%Y-%m-%d', '%d-%m-%Y', '%d/%m/%Y'):
        try:
            return datetime.strptime(str(value or '').strip(), fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def _clean(value, limit=240):
    return (str(value).strip()[:limit] if value not in (None, '') else '')


# ─────────────────────────────────────────────────────────────────────
# Matching
# ─────────────────────────────────────────────────────────────────────
def find_match(name, owner='', location=''):
    """The existing project this sighting is about, or None."""
    from app.models.intel import IntelProjectFact

    nkey, okey, lkey = name_key(name), owner_key(owner), location_key(location)
    if not nkey:
        return None

    exact = IntelProjectFact.query.filter_by(
        match_key=f'{nkey}|{okey}|{lkey}').first()
    if exact is not None:
        return _project(exact.project_id)

    for fact in IntelProjectFact.query.filter_by(name_key=nkey).all():
        if (_compatible(okey, fact.owner_key or '')
                and _compatible(lkey, fact.location_key or '')):
            return _project(fact.project_id)
    return None


def _project(project_id):
    from presales.models_projects import Project
    return db.session.get(Project, project_id)


def fact_for(project_id):
    from app.models.intel import IntelProjectFact
    return IntelProjectFact.query.filter_by(project_id=project_id).first()


# ─────────────────────────────────────────────────────────────────────
# Ingest
# ─────────────────────────────────────────────────────────────────────
def ingest(item, *, actor=None, source_id=None, commit=True):
    """Fold one RawItem into the project timeline.

    Returns {'action': 'created'|'appended'|'duplicate'|'rejected',
             'project_id', 'update_id', 'raw_item_id', 'reason'}.

    `duplicate` means this exact item has been seen before — the second
    sighting of one article is not news. `appended` means a different
    article about a project we already have.
    """
    from app.models.intel import IntelProjectFact, IntelRawItem
    from presales.models_projects import (Project, ProjectStageHistory,
                                          ProjectUpdate)

    title = (getattr(item, 'title', '') or '').strip()
    if not title:
        return {'action': 'rejected', 'reason': 'the item has no title',
                'project_id': None, 'update_id': None, 'raw_item_id': None}

    fingerprint = item.fingerprint()
    seen = IntelRawItem.query.filter_by(fingerprint=fingerprint).first()
    if seen is not None:
        return {'action': 'duplicate',
                'reason': 'this item has already been captured',
                'project_id': seen.project_id, 'update_id': None,
                'raw_item_id': seen.id}

    payload = dict(getattr(item, 'payload', None) or {})
    name = _clean(payload.get('project_name') or title, 250)
    owner = _clean(payload.get('owner_group'))
    location = _clean(payload.get('location'))
    stage = _clean(payload.get('intel_stage')) or detect_stage(
        title, getattr(item, 'body', ''))
    if stage and stage not in STAGE_ORDER:
        stage = ''
    confidence = _clean(getattr(item, 'confidence', '') or 'reported', 20)
    published = _as_date(getattr(item, 'publication_date', None)
                         or payload.get('published'))

    raw = IntelRawItem(
        source_id=source_id or getattr(item, 'source_id', None),
        adapter_key=getattr(item, 'source_key', '') or 'manual',
        external_id=_clean(getattr(item, 'external_id', ''), 300),
        fingerprint=fingerprint,
        url=_clean(getattr(item, 'url', ''), 500),
        title=title[:500], body=getattr(item, 'body', '') or '',
        publication_date=published, payload=payload,
        confidence=confidence, captured_by=actor)
    db.session.add(raw)

    project = find_match(name, owner, location)
    created = project is None
    if created:
        project = Project(
            name=name,
            industry=_clean(payload.get('industry'), 120) or None,
            state=_clean(payload.get('state'), 80) or None,
            location=location or None,
            country=_clean(payload.get('country'), 60) or 'India',
            announcement_date=_as_date(payload.get('announcement_date'))
            or (published if stage == 'announced' else None),
            stage=STAGE_TO_PROJECT.get(stage, 'Project Identified'),
            source='Project Intelligence',
            source_url=_clean(getattr(item, 'url', ''), 500) or None,
            source_publication=_clean(
                getattr(item, 'source_label', ''), 200) or None,
            source_date=published,
            description=(getattr(item, 'body', '') or '')[:4000] or None,
            procam_vertical=_clean(payload.get('vertical'), 60) or None,
            pic_emp_code=_clean(payload.get('bd_owner'), 20) or None,
            created_by=actor)
        db.session.add(project)
        db.session.flush()

    fact = fact_for(project.id)
    if fact is None:
        fact = IntelProjectFact(project_id=project.id, created_by=actor,
                                opportunity_status='not_started',
                                update_count=0)
        db.session.add(fact)

    before_stage = fact.intel_stage or ''
    _apply(fact, project, item, payload, name=name, owner=owner,
           location=location, stage=stage, confidence=confidence,
           published=published, actor=actor)

    update = ProjectUpdate(
        project_id=project.id,
        update_date=published or rules.business_today(),
        update_type=_update_type(stage),
        source=_clean(getattr(item, 'source_label', '')
                      or getattr(item, 'source_key', ''), 200) or None,
        source_url=_clean(getattr(item, 'url', ''), 500) or None,
        # The timeline entry keeps the claim and its confidence together,
        # so nobody reads a rumour as a fact six months later.
        summary=f'{title}\n\n{(getattr(item, "body", "") or "")[:2000]}'
                f'\n\n[confidence: {confidence}]'.strip(),
        updated_by=actor or 'system')
    db.session.add(update)
    db.session.flush()

    fact.update_count = (fact.update_count or 0) + 1
    project.last_update_at = datetime.utcnow()

    if stage and stage != before_stage:
        new_project_stage = STAGE_TO_PROJECT.get(stage)
        if new_project_stage and new_project_stage != project.stage:
            db.session.add(ProjectStageHistory(
                project_id=project.id, from_stage=project.stage,
                to_stage=new_project_stage, changed_by=actor or 'system',
                note=f'From project intelligence: {title[:200]}'))
            project.stage = new_project_stage

    raw.project_id = project.id
    raw.project_update_id = update.id
    raw.status = 'created' if created else 'matched'
    raw.match_note = ('No existing project matched this name, owner and '
                      'location.' if created else
                      f'Matched project #{project.id} on '
                      f'{fact.match_key or match_key(name, owner, location)}')

    audit.record('intel.project.ingest', 'project', project.id,
                 new={'action': 'created' if created else 'appended',
                      'adapter': getattr(item, 'source_key', ''),
                      'source_url': getattr(item, 'url', ''),
                      'confidence': confidence},
                 actor=actor or 'system')
    if commit:
        db.session.commit()
    return {'action': 'created' if created else 'appended',
            'project_id': project.id, 'update_id': update.id,
            'raw_item_id': raw.id, 'reason': ''}


def _update_type(stage):
    """The existing PROJECT_UPDATE_TYPES value for an intel stage."""
    return {
        'announced': 'Announcement', 'land': 'Status Change',
        'clearance': 'Approval', 'epc': 'Contract Award',
        'equipment_order': 'Order Placed', 'supplier': 'Supplier Identified',
        'manufacturing': 'Procurement', 'shipment': 'Status Change',
        'commissioning': 'Status Change',
    }.get(stage, 'Research')


def _better(new_confidence, old_confidence):
    """Whether a new sighting may overwrite a value already recorded."""
    return (CONFIDENCE_RANK.get(new_confidence, 0)
            >= CONFIDENCE_RANK.get(old_confidence or '', 0))


def _apply(fact, project, item, payload, *, name, owner, location, stage,
           confidence, published, actor):
    """Write what this sighting adds, without losing what we knew.

    A blank field is filled in. A field already held is replaced only by
    a sighting of equal or greater confidence. The keys are rewritten
    only when they gain information, so a later article that omits the
    owner cannot erase it and split the timeline.
    """
    fact.owner_group = fact.owner_group or owner or None
    fact.location = fact.location or location or None
    fact.industry = fact.industry or _clean(payload.get('industry'), 120) or None
    fact.vertical = fact.vertical or _clean(payload.get('vertical'), 60) or None

    fact.name_key = fact.name_key or name_key(name)
    if owner and not fact.owner_key:
        fact.owner_key = owner_key(owner)
    if location and not fact.location_key:
        fact.location_key = location_key(location)
    fact.match_key = (f'{fact.name_key or ""}|{fact.owner_key or ""}'
                      f'|{fact.location_key or ""}')

    for column, key in (('land_status', 'land_status'),
                        ('clearance_status', 'clearance_status'),
                        ('epc_contractor', 'epc_contractor'),
                        ('pmc', 'pmc'),
                        ('technology_provider', 'technology_provider'),
                        ('logistics_needs', 'logistics_needs'),
                        ('timeline_note', 'timeline')):
        value = _clean(payload.get(key), 2000 if column in
                       ('logistics_needs', 'timeline_note') else 240)
        if value and (not getattr(fact, column)
                      or _better(confidence, fact.confidence)):
            setattr(fact, column, value)

    suppliers = payload.get('equipment_suppliers') or []
    if isinstance(suppliers, str):
        suppliers = [s.strip() for s in suppliers.split(',') if s.strip()]
    if suppliers:
        # Suppliers accumulate: a second article naming one more supplier
        # is an addition, not a correction.
        have = list(fact.equipment_suppliers or [])
        fact.equipment_suppliers = have + [s for s in suppliers if s not in have]

    value_inr = payload.get('value_inr')
    if value_inr and (fact.value_inr is None or _better(confidence,
                                                        fact.confidence)):
        try:
            fact.value_inr = float(value_inr)
            project.estimated_value_inr = fact.value_inr
        except (TypeError, ValueError):
            pass

    announced = _as_date(payload.get('announcement_date'))
    if announced and not fact.announcement_date:
        fact.announcement_date = announced
        project.announcement_date = project.announcement_date or announced

    if stage and (not fact.intel_stage
                  or STAGE_ORDER.get(stage, -1)
                  > STAGE_ORDER.get(fact.intel_stage, -1)):
        fact.intel_stage = stage

    # Source provenance always describes the LATEST sighting; the older
    # ones remain on the timeline and in intel_raw_items.
    fact.source_ref = _clean(getattr(item, 'source_label', '')
                             or getattr(item, 'source_key', ''), 240) or \
        fact.source_ref
    fact.source_url = _clean(getattr(item, 'url', ''), 500) or fact.source_url
    fact.publication_date = published or fact.publication_date
    fact.captured_at = datetime.utcnow()
    if _better(confidence, fact.confidence):
        fact.confidence = confidence
    if confidence == 'confirmed':
        fact.last_verified_at = datetime.utcnow()
        fact.last_verified_by = actor or 'system'

    merged = dict(fact.extracted_entities or {})
    for key, value in extracted_entities(item, stage).items():
        if isinstance(value, list):
            have = list(merged.get(key) or [])
            merged[key] = have + [v for v in value if v not in have]
        else:
            merged[key] = value
    fact.extracted_entities = merged


def ingest_many(items, *, actor=None, source_id=None):
    """Ingest a batch in one transaction. Returns a count per outcome."""
    counts = {'created': 0, 'appended': 0, 'duplicate': 0, 'rejected': 0}
    project_ids = []
    for item in items:
        out = ingest(item, actor=actor, source_id=source_id, commit=False)
        counts[out['action']] = counts.get(out['action'], 0) + 1
        if out.get('project_id'):
            project_ids.append(out['project_id'])
    db.session.commit()
    counts['project_ids'] = project_ids
    return counts


# ─────────────────────────────────────────────────────────────────────
# Running a configured source
# ─────────────────────────────────────────────────────────────────────
def run_source(cfg, adapter, *, since=None, actor=None):
    """Collect from one source and record the attempt, success or not.

    An unavailable source still writes a run row — status 'unavailable'
    and the adapter's reason — because "nothing came in" and "nothing
    could come in" look identical on a dashboard otherwise.
    """
    from app.models.intel import IntelSourceRun

    run = IntelSourceRun(source_id=getattr(cfg, 'source_id', None),
                         adapter_key=adapter.key, run_by=actor,
                         started_at=datetime.utcnow())
    db.session.add(run)

    ok, reason = adapter.available()
    if not ok:
        run.status, run.reason = 'unavailable', reason
        run.finished_at = datetime.utcnow()
        db.session.commit()
        return run

    items = adapter.fetch(since)
    run.items_fetched = len(items)
    if not items:
        run.status = 'ok'
        run.reason = adapter.last_reason or 'No items were returned.'
    else:
        counts = {'created': 0, 'appended': 0, 'duplicate': 0, 'rejected': 0}
        for item in items:
            out = ingest(item, actor=actor,
                         source_id=getattr(cfg, 'source_id', None),
                         commit=False)
            counts[out['action']] = counts.get(out['action'], 0) + 1
        run.status = 'ok'
        run.items_new = counts['created']
        run.items_matched = counts['appended']
        run.reason = (f"{counts['created']} new, {counts['appended']} added "
                      f"to existing timelines, {counts['duplicate']} already "
                      f"seen.")
    run.finished_at = datetime.utcnow()
    db.session.commit()
    return run


def run_all(*, since=None, actor=None, purpose='project'):
    """Every enabled source for a purpose. Returns the run rows."""
    from app.intel import adapters as ad

    runs = []
    for cfg, adapter in ad.configured():
        if purpose and (cfg.purpose or 'project') != purpose:
            continue
        runs.append(run_source(cfg, adapter, since=since, actor=actor))
    return runs


# ─────────────────────────────────────────────────────────────────────
# Actions on a project — each audited
# ─────────────────────────────────────────────────────────────────────
class IntelRefused(Exception):
    """An action the caller may not take, with a reason worth showing."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.message, self.status = message, status


def timeline(project_id, limit=200):
    """The project's updates, newest first, with their raw items."""
    from app.models.intel import IntelRawItem
    from presales.models_projects import ProjectUpdate

    updates = (ProjectUpdate.query.filter_by(project_id=project_id)
               .order_by(ProjectUpdate.update_date.desc(),
                         ProjectUpdate.id.desc()).limit(limit).all())
    raws = {r.project_update_id: r for r in IntelRawItem.query.filter_by(
        project_id=project_id).all() if r.project_update_id}
    out = []
    for u in updates:
        row = u.to_dict()
        raw = raws.get(u.id)
        row['confidence'] = (raw.confidence if raw else '')
        row['adapter'] = (raw.adapter_key if raw else '')
        row['publication_date'] = (str(raw.publication_date)
                                   if raw and raw.publication_date else '')
        row['captured_at'] = (str(raw.captured_at)[:19]
                              if raw and raw.captured_at else '')
        out.append(row)
    return out


def assign_owner(project, emp_code, *, actor=None, reason=''):
    """Give a project to a BD owner. Recorded on the project and audited."""
    from app import Employee

    code = (emp_code or '').strip().upper()
    if code:
        emp = Employee.query.filter_by(emp_code=code).first()
        if emp is None or not emp.is_active:
            raise IntelRefused(f'{code} is not an active employee.')
    before = project.pic_emp_code or ''
    project.pic_emp_code = code or None
    fact = fact_for(project.id)
    if fact is not None:
        fact.bd_owner_emp_code = code or None
    audit.record('intel.project.assign', 'project', project.id,
                 old={'bd_owner': before}, new={'bd_owner': code},
                 reason=reason or None, actor=actor)
    db.session.commit()
    return code


def follow(project, emp_code, *, actor=None, on=True):
    """Follow or unfollow a project. Audited both ways."""
    from app.models.intel import IntelProjectFollow

    code = (emp_code or '').strip().upper()
    if not code:
        raise IntelRefused('Nobody to follow with.')
    row = IntelProjectFollow.query.filter_by(project_id=project.id,
                                             emp_code=code).first()
    if on and row is None:
        db.session.add(IntelProjectFollow(project_id=project.id,
                                          emp_code=code))
    elif not on and row is not None:
        db.session.delete(row)
    audit.record('intel.project.follow', 'project', project.id,
                 new={'emp_code': code, 'following': bool(on)},
                 actor=actor or code)
    db.session.commit()
    return bool(on)


def followers(project_id):
    from app.models.intel import IntelProjectFollow
    return [r.emp_code for r in IntelProjectFollow.query.filter_by(
        project_id=project_id).all()]


def add_account(project, account_id, role='Other', *, actor=None, note=''):
    """Link an organisation to the project, through the presales service."""
    from app import Company
    from presales import services_projects as psvc
    from presales.services import PreSalesError

    account = db.session.get(Company, int(account_id))
    if account is None:
        raise IntelRefused('No such account.', 404)
    try:
        link = psvc.link_account_to_project(
            project=project, account=account, role=role,
            added_by_code=actor or 'system', note=note or None)
    except PreSalesError as exc:
        raise IntelRefused(str(exc))
    audit.record('intel.project.add_account', 'project', project.id,
                 new={'account_id': account.id, 'role': role}, actor=actor)
    db.session.commit()
    return link


def add_contact(project, contact_id, role_on_project='', *, actor=None):
    from app import Contact
    from presales import services_projects as psvc
    from presales.services import PreSalesError

    contact = db.session.get(Contact, int(contact_id))
    if contact is None:
        raise IntelRefused('No such contact.', 404)
    try:
        link = psvc.link_contact_to_project(
            project=project, contact=contact,
            role_on_project=role_on_project or None,
            added_by_code=actor or 'system')
    except PreSalesError as exc:
        raise IntelRefused(str(exc))
    audit.record('intel.project.add_contact', 'project', project.id,
                 new={'contact_id': contact.id,
                      'role': role_on_project}, actor=actor)
    db.session.commit()
    return link


def mark_not_relevant(project, reason, *, actor=None):
    """Take a project off the board, with the reason kept.

    Archived rather than deleted: the next bulletin about it will match
    the same key, and "we already decided this is not for us" is the
    answer, not a second project.
    """
    reason = (reason or '').strip()
    if not reason:
        raise IntelRefused('Say why it is not relevant — the reason is '
                           'what stops it being raised again.')
    fact = fact_for(project.id)
    if fact is None:
        raise IntelRefused('This project has no intelligence record.', 404)
    fact.not_relevant = True
    fact.not_relevant_reason = reason[:300]
    fact.opportunity_status = 'not_relevant'
    project.is_archived = True
    audit.record('intel.project.not_relevant', 'project', project.id,
                 new={'not_relevant': True}, reason=reason[:400], actor=actor)
    db.session.commit()
    return fact


def create_lead(project, *, actor=None, owner=None, note=''):
    """Turn a project into a lead, through the one assignment path.

    The owner is set by `app.services.lead_assignment.assign`, so the
    assignment history row and the notifications happen here exactly as
    they do everywhere else. A project that already made a lead returns
    that lead rather than a second one.
    """
    from app import Lead
    from app.services import lead_assignment

    fact = fact_for(project.id)
    if fact is not None and fact.lead_id:
        existing = db.session.get(Lead, fact.lead_id)
        if existing is not None:
            raise IntelRefused(
                f'This project already became lead #{existing.id}.')

    company = ((fact.owner_group if fact else '') or project.name)[:200]
    lead = Lead(
        company=company,
        project=project.name[:300],
        industry=(project.industry or (fact.industry if fact else '') or
                  None),
        state=project.state or None,
        country=project.country or 'India',
        source='Project Intelligence',
        procam_vertical=(project.procam_vertical
                         or (fact.vertical if fact else '') or None),
        notes=(f'Created from project intelligence #{project.id}.\n'
               f'Source: {(fact.source_ref if fact else "") or "—"} '
               f'{(fact.source_url if fact else "") or ""}\n'
               f'Confidence: {(fact.confidence if fact else "") or "—"}'),
        stage='New Opportunity')
    if project.estimated_value_inr:
        lead.estimated_value_inr = project.estimated_value_inr
    db.session.add(lead)
    db.session.flush()

    owner_code = (owner or project.pic_emp_code or actor or '').strip().upper()
    if owner_code:
        ok, err = lead_assignment.assign(
            lead, primary_code=owner_code, actor=actor,
            note=note or f'Created from project intelligence #{project.id}')
        if not ok:
            db.session.rollback()
            raise IntelRefused(err or 'The lead could not be assigned.')

    if fact is not None:
        fact.lead_id = lead.id
        fact.opportunity_status = 'lead_created'
    audit.record('intel.project.create_lead', 'lead', lead.id,
                 new={'project_id': project.id, 'owner': owner_code,
                      'source': 'Project Intelligence'},
                 reason=note or 'Created from project intelligence',
                 actor=actor)
    db.session.commit()
    return lead


# ─────────────────────────────────────────────────────────────────────
# Reading, within the viewer's access
# ─────────────────────────────────────────────────────────────────────
def visible(sc=None, *, include_not_relevant=False):
    """A Project query the viewer may see.

    A project with no BD owner is visible to anyone signed in: it is
    public-source intelligence about somebody else's plant, not a
    customer record, and hiding unowned projects is how they stay
    unowned. Once a project HAS an owner, the Access Matrix decides,
    exactly as it does for a lead.
    """
    from sqlalchemy import or_
    from app.access import scope as sc_mod
    from presales.models_projects import Project

    sc = sc or sc_mod.current()
    q = Project.query
    if not include_not_relevant:
        q = q.filter(Project.is_archived.is_(False))
    if sc.codes is None:
        return q
    if not sc.codes:
        # Signed out. Unowned intelligence is still not for them.
        return q.filter(False)
    return q.filter(or_(Project.pic_emp_code.is_(None),
                        Project.pic_emp_code == '',
                        Project.pic_emp_code.in_(sc.codes)))


def may_view(project, sc=None):
    from app.access import scope as sc_mod
    sc = sc or sc_mod.current()
    if project is None:
        return False
    if sc.codes is None:
        return True
    if not sc.codes:
        return False
    return (not (project.pic_emp_code or '')
            or sc.reaches(project.pic_emp_code))


def listing(sc=None, *, q='', stage='', vertical='', industry='', state='',
            owner='', confidence='', page=1, per_page=50):
    """A page of project intelligence, with its structured facts."""
    from app.models.intel import IntelProjectFact
    from presales.models_projects import Project

    query = visible(sc)
    if q:
        query = query.filter(Project.name.ilike(f'%{q}%'))
    if vertical:
        query = query.filter(Project.procam_vertical == vertical)
    if industry:
        query = query.filter(Project.industry == industry)
    if state:
        query = query.filter(Project.state == state)
    if owner:
        query = query.filter(Project.pic_emp_code == owner.upper())

    ids_by_fact = None
    if stage or confidence:
        fq = IntelProjectFact.query
        if stage:
            fq = fq.filter(IntelProjectFact.intel_stage == stage)
        if confidence:
            fq = fq.filter(IntelProjectFact.confidence == confidence)
        ids_by_fact = [f.project_id for f in fq.all()] or [0]
        query = query.filter(Project.id.in_(ids_by_fact))

    try:
        page, per_page = max(1, int(page)), min(200, max(1, int(per_page)))
    except (TypeError, ValueError):
        page, per_page = 1, 50
    total = query.count()
    rows = (query.order_by(Project.last_update_at.desc().nullslast(),
                           Project.id.desc())
            .offset((page - 1) * per_page).limit(per_page).all())
    facts = {f.project_id: f for f in IntelProjectFact.query.filter(
        IntelProjectFact.project_id.in_([r.id for r in rows] or [0])).all()}

    items = []
    for project in rows:
        row = project.to_dict()
        fact = facts.get(project.id)
        row['intel'] = fact.to_dict() if fact else {}
        row['stage_label'] = STAGE_LABELS.get(
            fact.intel_stage if fact else '', '')
        items.append(row)
    return {'items': items, 'total': total, 'page': page,
            'per_page': per_page,
            'pages': max(1, (total + per_page - 1) // per_page)}
