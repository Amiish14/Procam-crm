"""
Competitor intelligence by vertical, service, geography and industry.

The CRM already records *that* a competitor did something, in
`competitor_intelligence`. What it could not answer is *where*: "who is
taking heavy-lift work in Gujarat?" or "which of them have moved into
air charter?" Those are the four axes the business actually competes on,
and none of them was a column.

This module adds the axes, not a second competitor master. Every entry
points at the competitor's `companies` row (§19 — a competitor is a
Company classification) and, where one exists, at the Procam account,
opportunity or project it bears on.

The vocabularies — vertical, service, industry, activity type and
confidence — are Master Data lists. None of them is written down here,
so adding "air charter" to the services list is an administrator's job
and takes effect in the capture form, the filters and the summary at the
same moment.

Honesty: every entry carries a source, a publication date, a captured
date, a confidence and a last-verified stamp. An entry with no source
can still be recorded — people do hear things — but it is recorded as
`unverified`, and it is shown as unverified wherever it appears.
"""
from __future__ import annotations

from datetime import datetime

from app import db
from app.intel import (LIST_ACTIVITY_TYPE, LIST_CONFIDENCE, master_items)
from app.services import audit
from app.services import sales_rules as rules


class CompetitorIntelRefused(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message, self.status = message, status


# ── vocabularies, all from Master Data ───────────────────────────────
def vocabularies():
    """Everything the capture form and the filters offer.

    An empty list here means nobody has configured that vocabulary yet.
    The screens say exactly that rather than substituting a built-in
    list, because a hidden fallback is how a vocabulary stops being
    maintained.
    """
    return {
        'vertical': [{'code': c, 'label': l}
                     for c, l in master_items('vertical')],
        'service': [{'code': c, 'label': l}
                    for c, l in master_items('service')],
        'industry': [{'code': c, 'label': l}
                     for c, l in master_items('industry')],
        'activity_type': [{'code': c, 'label': l}
                          for c, l in master_items(LIST_ACTIVITY_TYPE)],
        'confidence': [{'code': c, 'label': l}
                       for c, l in master_items(LIST_CONFIDENCE)],
    }


def _check(list_key, value, label):
    """A value must be one Master Data offers, or nothing at all."""
    value = (value or '').strip()
    if not value:
        return ''
    codes = {c for c, _l in master_items(list_key)}
    if codes and value not in codes:
        raise CompetitorIntelRefused(
            f'{label} "{value}" is not in Master Data. Add it there first '
            f'so every screen agrees on the vocabulary.')
    return value


def _competitor_company(name, company_id=None):
    """Resolve the competitor to a Company row where we can."""
    from app import Company

    if company_id:
        company = db.session.get(Company, int(company_id))
        if company is not None:
            return company
    name = (name or '').strip()
    if not name:
        return None
    return Company.query.filter(Company.name.ilike(name)).first()


# ── capture ──────────────────────────────────────────────────────────
def capture(*, competitor_name, summary, actor=None, company_id=None,
            vertical='', service='', industry='', geography='',
            activity_type='', event_date=None, confidence='reported',
            source='', source_url='', publication_date=None,
            related_account_id=None, related_opportunity_id=None,
            related_project_id=None, raw_item_id=None, commit=True):
    """Record one piece of competitor activity.

    Refuses rather than guesses: a vocabulary value Master Data does not
    hold is an error, not a silent free-text column.
    """
    from app.models.intel import IntelCompetitorActivity

    competitor_name = (competitor_name or '').strip()
    summary = (summary or '').strip()
    if not competitor_name:
        raise CompetitorIntelRefused('Name the competitor.')
    if not summary:
        raise CompetitorIntelRefused('Say what they were seen doing.')

    vertical = _check('vertical', vertical, 'Vertical')
    service = _check('service', service, 'Service')
    industry = _check('industry', industry, 'Industry')
    activity_type = _check(LIST_ACTIVITY_TYPE, activity_type,
                           'Activity type')
    confidence = _check(LIST_CONFIDENCE, confidence or 'reported',
                        'Confidence') or 'reported'
    if confidence != 'unverified' and not (source or '').strip():
        # Anything stronger than "we heard" has to say who said it.
        raise CompetitorIntelRefused(
            'Give the source, or record this as unverified.')

    company = _competitor_company(competitor_name, company_id)
    row = IntelCompetitorActivity(
        company_id=company.id if company is not None else None,
        competitor_name=competitor_name[:240],
        vertical=vertical or None, service=service or None,
        industry=industry or None,
        geography=(geography or '').strip()[:120] or None,
        activity_type=activity_type or None,
        summary=summary, event_date=event_date or rules.business_today(),
        confidence=confidence, source=(source or '').strip()[:240] or None,
        source_url=(source_url or '').strip()[:500] or None,
        publication_date=publication_date,
        captured_at=datetime.utcnow(),
        related_account_id=related_account_id or None,
        related_opportunity_id=related_opportunity_id or None,
        related_project_id=related_project_id or None,
        raw_item_id=raw_item_id or None,
        added_by=actor)
    if confidence == 'confirmed':
        row.last_verified_at = datetime.utcnow()
        row.last_verified_by = actor
    db.session.add(row)
    db.session.flush()
    audit.record('intel.competitor.capture', 'competitor_activity', row.id,
                 new={'competitor': competitor_name, 'vertical': vertical,
                      'service': service, 'geography': geography,
                      'confidence': confidence, 'source': source},
                 actor=actor)
    if commit:
        db.session.commit()
    return row


def verify(row_id, *, actor=None, confidence=None, note=''):
    """Re-check an entry: stamp who looked and when, and audit it."""
    from app.models.intel import IntelCompetitorActivity

    row = db.session.get(IntelCompetitorActivity, int(row_id))
    if row is None:
        raise CompetitorIntelRefused('No such entry.', 404)
    before = {'confidence': row.confidence,
              'last_verified_at': str(row.last_verified_at or '')}
    if confidence:
        row.confidence = _check(LIST_CONFIDENCE, confidence, 'Confidence')
    row.last_verified_at = datetime.utcnow()
    row.last_verified_by = actor
    audit.record_change('intel.competitor.verify', 'competitor_activity',
                        row.id, before,
                        {'confidence': row.confidence,
                         'last_verified_at': str(row.last_verified_at)},
                        reason=note or None, actor=actor)
    db.session.commit()
    return row


# ── query ────────────────────────────────────────────────────────────
def search(*, competitor='', vertical='', service='', industry='',
           geography='', activity_type='', confidence='', since=None,
           account_id=None, project_id=None, page=1, per_page=50):
    """Competitor activity on any combination of the four axes."""
    from app.models.intel import IntelCompetitorActivity as A

    q = A.query
    if competitor:
        q = q.filter(A.competitor_name.ilike(f'%{competitor}%'))
    for column, value in ((A.vertical, vertical), (A.service, service),
                          (A.industry, industry),
                          (A.activity_type, activity_type),
                          (A.confidence, confidence)):
        if value:
            q = q.filter(column == value)
    if geography:
        q = q.filter(A.geography.ilike(f'%{geography}%'))
    if since:
        q = q.filter(A.event_date >= since)
    if account_id:
        q = q.filter(A.related_account_id == int(account_id))
    if project_id:
        q = q.filter(A.related_project_id == int(project_id))

    try:
        page, per_page = max(1, int(page)), min(200, max(1, int(per_page)))
    except (TypeError, ValueError):
        page, per_page = 1, 50
    total = q.count()
    rows = (q.order_by(A.event_date.desc(), A.id.desc())
            .offset((page - 1) * per_page).limit(per_page).all())
    return {'items': [r.to_dict() for r in rows], 'total': total,
            'page': page, 'per_page': per_page,
            'pages': max(1, (total + per_page - 1) // per_page)}


def by_axis(axis='vertical', *, since=None):
    """A count of entries per value of one axis — the summary tiles.

    Only axes the table actually has are allowed; an unknown axis is a
    programming error and says so rather than quietly counting nothing.
    """
    from sqlalchemy import func
    from app.models.intel import IntelCompetitorActivity as A

    column = {'vertical': A.vertical, 'service': A.service,
              'industry': A.industry, 'geography': A.geography,
              'activity_type': A.activity_type,
              'confidence': A.confidence,
              'competitor': A.competitor_name}.get(axis)
    if column is None:
        raise CompetitorIntelRefused(f'{axis} is not an axis we hold.')
    q = db.session.query(column, func.count(A.id)).group_by(column)
    if since:
        q = q.filter(A.event_date >= since)
    return [{'value': value or '(not stated)', 'count': count}
            for value, count in q.all()]
