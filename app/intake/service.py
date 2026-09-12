"""
The three intake screens, behind one service.

    accounts_needing_owners()  the Account Master gap — §12, §15
    review_queue()             what the classifier could not decide — §21
    intelligence()             is any of this working — §22

Kept apart from the classifier itself: lead_intake.py decides, this
reports and lets a person disagree. A correction recorded here is what
Phase 3 will eventually learn from, so the pair — what was decided, and
what a human changed it to — is written together or not at all.
"""
from datetime import datetime, timedelta

from app import db
from app.services import lead_intake as li


# ─── Account Master — §12, §14, §15 ──────────────────────────────────────
def account_rows(*, missing_only=False, q=None, limit=300):
    """Accounts with their two owners, worst-configured first.

    Sorted so the gap is the first thing on screen: an account with no
    owner at all cannot auto-assign, and an account with a primary but no
    secondary delivers half the promise.
    """
    from app import Company, Employee

    query = Company.query.filter(Company.is_active.is_(True))
    if q:
        query = query.filter(Company.name.ilike(f'%{q.strip()}%'))
    rows = query.order_by(Company.name).limit(4000).all()

    names = {e.emp_code: e.name for e in Employee.query.all()}
    out = []
    for c in rows:
        domains = list(c.email_domains or [])
        gap = (0 if not c.pic_emp_code else
               1 if not c.secondary_pic_emp_code else 2)
        if missing_only and gap == 2:
            continue
        out.append({
            'id': c.id,
            'name': c.name,
            'vertical': c.vertical or '',
            'primary': c.pic_emp_code or '',
            'primary_name': names.get(c.pic_emp_code or '', ''),
            'secondary': c.secondary_pic_emp_code or '',
            'secondary_name': names.get(c.secondary_pic_emp_code or '', ''),
            'backup': c.backup_pic_emp_code or '',
            'domains': domains,
            'domain_text': ', '.join(domains),
            # 0 = no owner, 1 = primary only, 2 = both
            'ownership': gap,
            'can_auto_assign': bool(c.pic_emp_code and domains),
        })
    out.sort(key=lambda r: (r['ownership'], r['name'].lower()))
    return out[:limit]


def account_summary():
    from app import Company

    live = Company.query.filter(Company.is_active.is_(True))
    total = live.count()
    with_domain = live.filter(Company.email_domains.isnot(None),
                              Company.email_domains != '[]').count()
    with_primary = live.filter(Company.pic_emp_code.isnot(None),
                               Company.pic_emp_code != '').count()
    with_both = live.filter(Company.pic_emp_code.isnot(None),
                            Company.pic_emp_code != '',
                            Company.secondary_pic_emp_code.isnot(None),
                            Company.secondary_pic_emp_code != '').count()
    ready = live.filter(Company.pic_emp_code.isnot(None),
                        Company.pic_emp_code != '',
                        Company.email_domains.isnot(None),
                        Company.email_domains != '[]').count()
    return {
        'total': total,
        'with_domain': with_domain,
        'with_primary': with_primary,
        'with_both': with_both,
        # The number that actually matters: a lead from one of these
        # assigns itself with nobody touching it.
        'auto_assignable': ready,
        'no_owner': total - with_primary,
    }


def save_account_owners(company_id, *, primary=None, secondary=None,
                        backup=None, vertical=None, domains=None, actor=None):
    """(ok, error). Validates before writing, like lead assignment does."""
    from app import Company
    from app.services import lead_assignment

    company = db.session.get(Company, int(company_id))
    if company is None:
        return False, 'Account not found'

    primary = (primary or '').strip()
    secondary = (secondary or '').strip()
    backup = (backup or '').strip()

    err = lead_assignment.validate(primary, secondary)
    if err:
        return False, err
    if backup and backup in (primary, secondary):
        return False, ('The backup should be someone other than the primary '
                       'or secondary — otherwise there is no backup.')

    company.pic_emp_code = primary or None
    company.secondary_pic_emp_code = secondary or None
    company.backup_pic_emp_code = backup or None
    if vertical is not None:
        company.vertical = (vertical or '').strip() or None
    if domains is not None:
        cleaned = []
        for d in _split_domains(domains):
            if d not in cleaned:
                cleaned.append(d)
        company.email_domains = cleaned
    return True, None


def _split_domains(value):
    """Whatever someone typed → bare mail domains.

    People paste "https://www.tatasteel.com/about", "@tatasteel.co.in"
    and "buyer@tatasteel.com" interchangeably. li.domain_of only handles
    the address form and returns nothing for a URL, so a pasted URL was
    being stored whole and would never match a sender.
    """
    import re

    if isinstance(value, (list, tuple)):
        parts = list(value)
    else:
        parts = re.split(r'[,\n;\s]+', str(value or ''))

    out = []
    for raw in parts:
        d = (raw or '').strip().lower()
        if not d:
            continue
        if '@' in d:
            d = d.rsplit('@', 1)[1]
        d = re.sub(r'^[a-z]+://', '', d)
        d = d.split('/')[0].split('?')[0].strip().strip('.')
        if d.startswith('www.'):
            d = d[4:]
        # A bare label with no dot is not a domain, and neither is
        # anything carrying characters a hostname cannot hold.
        if d and '.' in d and re.fullmatch(r'[a-z0-9.\-]+', d):
            out.append(d)
    return out


def domain_conflicts(company_id, domains):
    """Other accounts already claiming any of these domains.

    A domain on two accounts means a lead from it is assigned by
    whichever the lookup happens to reach first, which is worse than not
    matching at all.
    """
    from app import Company

    wanted = set(_split_domains(domains))
    if not wanted:
        return []
    clashes = []
    for c in Company.query.filter(Company.email_domains.isnot(None)).all():
        if c.id == int(company_id):
            continue
        shared = wanted & {str(d).lower() for d in (c.email_domains or [])}
        if shared:
            clashes.append({'id': c.id, 'name': c.name,
                            'domains': sorted(shared)})
    return clashes


# ─── Review queue — §21 ──────────────────────────────────────────────────
def review_queue(limit=200):
    from app import EmailClassification, Company

    rows = (EmailClassification.query
            .filter(EmailClassification.review_state == 'pending')
            .order_by(EmailClassification.created_at.desc())
            .limit(limit).all())

    out = []
    for r in rows:
        from app.services import lead_intake_db as lidb
        company, how = (None, 'unmapped')
        try:
            company, how = lidb.resolve_account(r.from_addr)
        except Exception:
            pass
        primary, secondary = lidb.owners_for(company)
        d = r.to_dict()
        d.update({
            'account': company.name if company else '',
            'account_id': company.id if company else None,
            'account_found_by': how,
            'vertical': (company.vertical if company else '') or '',
            'primary': primary or '',
            'secondary': secondary or '',
        })
        out.append(d)
    return out


def review_counts():
    from app import EmailClassification
    q = EmailClassification.query
    return {
        'pending': q.filter_by(review_state='pending').count(),
        'accepted': q.filter_by(review_state='accepted').count(),
        'rejected': q.filter_by(review_state='rejected').count(),
        'reclassified': q.filter_by(review_state='reclassified').count(),
    }


def accept(classification_id, *, actor=None):
    """Create the lead this email should have produced.

    Uses the stored payload rather than re-fetching from Graph: the
    message may have been moved or deleted in the mailbox since, and a
    review queue that fails because someone tidied their inbox is not a
    review queue.
    """
    from app import EmailClassification, Lead
    from app.services import lead_intake_db as lidb
    from app.services import lead_assignment

    row = db.session.get(EmailClassification, int(classification_id))
    if row is None:
        return None, 'Not found'
    if row.created_lead_id:
        return row, None                    # already accepted

    payload = dict(row.payload or {})
    sender_addr = payload.get('resolved_sender') or row.from_addr or ''
    company, _how = lidb.resolve_account(sender_addr)

    lead = Lead(
        source='email',
        stage='New Opportunity',
        company=(company.name if company else '')
                or (row.from_domain or 'Unknown'),
        company_id=company.id if company else None,
        email=sender_addr or None,
        email_message_id=row.message_id,
        conversation_id=row.conversation_id,
        original_email_subject=(row.subject or '')[:500] or None,
        original_email_from=sender_addr or None,
        original_email_body=(payload.get('body') or '')[:8000] or None,
        original_email_source='recovered_from_review',
        classification=li.Klass.NEW_LEAD,
        lead_confidence=row.confidence,
        duplicate_score=row.duplicate_score,
        procam_vertical=(company.vertical if company else None),
    )
    db.session.add(lead)
    db.session.flush()

    primary, secondary = lidb.owners_for(company)
    if primary:
        lead_assignment.assign(lead, primary_code=primary,
                               secondary_code=secondary, actor=actor,
                               note='accepted from lead review')

    row.created_lead_id = lead.id
    row.review_state = 'accepted'
    lidb.correct(row, li.Klass.NEW_LEAD, reason='Accepted at review',
                 by=actor)
    return row, None


def reject(classification_id, *, reason, actor=None, note=None):
    """Reject with a structured reason — §10.

    Nothing is deleted. The row stays, the reason is recorded, and the
    pair (what the classifier said, what the human said) is the label
    Phase 3 will learn from.
    """
    from app import EmailClassification
    from app.services import lead_intake_db as lidb

    row = db.session.get(EmailClassification, int(classification_id))
    if row is None:
        return None, 'Not found'
    if not (reason or '').strip():
        return None, 'A reason is required'

    row.review_state = 'rejected'
    lidb.correct(row, row.classification, reason=reason[:80], by=actor)
    if note:
        row.reason = ((row.reason or '') + f' | reviewer: {note}')[:300]
    return row, None


def reclassify(classification_id, *, to_class, reason=None, actor=None):
    """The classifier was wrong in a specific way — the most useful
    correction there is, because it names the right answer."""
    from app import EmailClassification
    from app.services import lead_intake_db as lidb

    if to_class not in li.Klass.LABELS:
        return None, f'Unknown class {to_class}'
    row = db.session.get(EmailClassification, int(classification_id))
    if row is None:
        return None, 'Not found'

    row.review_state = 'reclassified'
    lidb.correct(row, to_class, reason=reason, by=actor)
    return row, None


# ─── Intelligence — §22 ──────────────────────────────────────────────────
def intelligence(days=30):
    from app import EmailClassification

    since = datetime.utcnow() - timedelta(days=days)
    rows = (EmailClassification.query
            .filter(EmailClassification.created_at >= since).all())
    total = len(rows)

    by_class, by_step, by_reason = {}, {}, {}
    corrected = wrong = 0
    for r in rows:
        by_class[r.classification] = by_class.get(r.classification, 0) + 1
        if r.decided_by:
            by_step[r.decided_by] = by_step.get(r.decided_by, 0) + 1
        if r.corrected_to:
            corrected += 1
            if r.was_wrong:
                wrong += 1
        if r.correction_reason:
            by_reason[r.correction_reason] = \
                by_reason.get(r.correction_reason, 0) + 1

    leads = by_class.get(li.Klass.NEW_LEAD, 0)
    noise = total - leads

    return {
        'days': days,
        'total': total,
        'leads_created': leads,
        'noise': noise,
        # The headline: how much of the intake did not need a lead.
        'noise_reduction': _pct(noise, total),
        # Accuracy is only meaningful over what a human actually
        # reviewed. Counting unreviewed decisions as correct would
        # report 100% for a classifier nobody had checked.
        'reviewed': corrected,
        'wrong': wrong,
        'accuracy': _pct(corrected - wrong, corrected) if corrected else None,
        'by_class': sorted(
            ({'klass': k, 'label': li.Klass.LABELS.get(k, k), 'count': v,
              'pct': _pct(v, total)} for k, v in by_class.items()),
            key=lambda d: -d['count']),
        'by_step': sorted(
            ({'step': k, 'count': v, 'pct': _pct(v, total)}
             for k, v in by_step.items()), key=lambda d: -d['count']),
        'by_reason': sorted(
            ({'reason': k, 'count': v} for k, v in by_reason.items()),
            key=lambda d: -d['count'])[:20],
        **review_counts(),
    }


def _pct(n, total):
    return round(100.0 * n / total, 1) if total else 0.0


def rejection_reasons():
    """From master data, so Admin can add one without a deployment."""
    try:
        from app.master_data import service as md
        return [i.label for i in md.items('lead_rejection_reason')]
    except Exception:
        return ['Other']
