"""
Which leads the vertical dropdown defect moved, and what to do.

The lead edit form offered four verticals against the eleven leads
carry. A `<select>` with no option matching the record selects option
zero, so saving anything at all rewrote the vertical to Heavy
Transport. The form is fixed; this is about the damage already done.

Three populations, and the distinction is the whole point:

    recorded       the audit trail holds the previous value. The
                   restore is known, not inferred.
    review         keyword evidence only. The CRM knows the lead's
                   words disagree with Heavy Transport and does not
                   know what the right answer is.
    changed_since  somebody set the vertical again after the rewrite.
                   Restoring would undo a decision taken since.

Nothing here writes. `restore` in `scripts/` does that, from the
first population only, and only on an explicit instruction.
"""
from __future__ import annotations

import re
from datetime import datetime

TARGET = 'Heavy Transport'

#: Words that say which vertical a lead really belongs to. Narrow on
#: purpose: a false positive costs somebody a careful read of a lead
#: that was fine.
HINTS = (
    ('Customs', (r'\bcustoms?\b', r'\bclearance\b', r'\bbill of entry\b',
                 r'\bBOE\b', r'\bshipping bill\b', r'\bICEGATE\b')),
    ('Freight Forwarding', (r'\bfreight forward', r'\bforwarder\b',
                            r'\bCHA\b', r'\bconsolidat')),
    ('Sea Freight', (r'\bFCL\b', r'\bLCL\b', r'\bCBM\b',
                     r'\bbill of lading\b', r'\bB/?L\b', r'\bHBL\b',
                     r'\bMBL\b', r'\bnhava sheva\b',
                     r'\bport of loading\b', r'\bPOL\b', r'\bPOD\b',
                     r'\bDAP\b', r'\bCIF\b', r'\bFOB\b')),
    ('Air Freight', (r'\bair freight\b', r'\bairfreight\b', r'\bAWB\b',
                     r'\bairway ?bill\b')),
    ('Warehousing', (r'\bwarehous', r'\bgodown\b', r'\bstorage\b',
                     r'\bpallet')),
)

#: Words that genuinely mean Heavy Transport. They do not clear a
#: lead — a job can be both — but they lower the confidence.
HEAVY = (r'\bODC\b', r'\btrailer\b', r'\blow ?bed\b', r'\bcrane\b',
         r'\bhydraulic axle\b', r'\bSPMT\b', r'\bover ?dimension')

FIELDS = ('project', 'original_email_subject', 'original_email_body',
          'notes', 'products', 'opp_notes')


def text_of(lead):
    parts = [str(getattr(lead, f)) for f in FIELDS
             if getattr(lead, f, None)]
    return ' \n '.join(parts)[:20000]


def evidence(lead):
    """({vertical: hits}, heavy_transport_hits, the words that matched)."""
    text = text_of(lead)
    found, words = {}, []
    for vertical, patterns in HINTS:
        hit = [p for p in patterns if re.search(p, text, re.I)]
        if hit:
            found[vertical] = len(hit)
            words += [_pretty(p) for p in hit[:2]]
    heavy = sum(1 for p in HEAVY if re.search(p, text, re.I))
    return found, heavy, ', '.join(dict.fromkeys(words))[:200]


def _pretty(pattern):
    return (pattern.replace(r'\b', '').replace(r'\s*', ' ')
            .replace(r'\s?', ' ').replace('\\', '').replace('?', ''))


def rewrite_events(lead_id, since):
    """Every recorded change of this lead's vertical, newest first."""
    try:
        from app.models.audit import AuditEvent
        rows = (AuditEvent.query
                .filter(AuditEvent.entity_type.in_(('Lead', 'lead')),
                        AuditEvent.entity_id == str(lead_id),
                        AuditEvent.occurred_at >= since)
                .order_by(AuditEvent.occurred_at.desc()).limit(60).all())
    except Exception:
        from app import db
        db.session.rollback()
        return []
    out = []
    for r in rows:
        # A creation is not a change. `lead.create` snapshots the whole
        # row into new_value, `procam_vertical` among it, so reading
        # any row that merely mentions the field turns every lead's
        # birth into a vertical change — and then a lead created after
        # its own rewrite looks like somebody having corrected it
        # since.
        if (r.action or '').endswith(('.create', '.delete')):
            continue
        new = r.new_value if isinstance(r.new_value, dict) else {}
        old = r.old_value if isinstance(r.old_value, dict) else {}
        if 'procam_vertical' not in new or 'procam_vertical' not in old:
            continue
        was, now = (old.get('procam_vertical') or ''), \
            (new.get('procam_vertical') or '')
        if was == now:
            continue
        out.append({'at': r.occurred_at, 'by': r.actor or '',
                    'from': was, 'to': now})
    return out


def classify(lead, since):
    """Which population this lead belongs to, and why.

    Returns None when there is nothing to say about it.
    """
    events = rewrite_events(lead.id, since)
    rewrite = next((e for e in events
                    if e['to'] == TARGET and e['from']
                    and e['from'] != TARGET), None)

    # Anything done to the vertical AFTER the rewrite is a decision
    # taken since, and restoring would undo it.
    later = [e for e in events
             if rewrite is not None and e['at'] > rewrite['at']
             and e['to'] and e['to'] != TARGET]

    found, heavy, words = evidence(lead)
    best = max(found.items(), key=lambda kv: kv[1])[0] if found else ''
    score = sum(found.values())

    if rewrite is not None and later:
        return 'changed_since', {
            'was': rewrite['from'], 'rewritten_by': rewrite['by'],
            'rewritten_at': str(rewrite['at'])[:16],
            'changed_to': later[0]['to'], 'changed_by': later[0]['by'],
            'changed_at': str(later[0]['at'])[:16],
            'looks_like': best, 'evidence': words}

    if rewrite is not None:
        return 'recorded', {
            'restore_to': rewrite['from'], 'rewritten_by': rewrite['by'],
            'rewritten_at': str(rewrite['at'])[:16],
            'looks_like': best, 'evidence': words,
            'agrees': 'yes' if best == rewrite['from'] else
                      ('no evidence' if not best else 'differs')}

    if score and not (heavy and score < 2):
        return 'review', {
            'looks_like': best, 'evidence': words,
            'keyword_hits': score, 'heavy_hits': heavy,
            'confidence': 'high' if score >= 3 and not heavy
                          else 'medium' if score >= 2 or not heavy
                          else 'low'}
    return None


def survey(since='2026-09-01', limit=20000):
    """{'recorded': [...], 'review': [...], 'changed_since': [...]}."""
    from app import Lead

    # A date as text or as a datetime: callers have both to hand and
    # making each one convert is how a script dies on its first run.
    when = (since if isinstance(since, datetime)
            else datetime.fromisoformat(str(since)))
    out = {'recorded': [], 'review': [], 'changed_since': []}
    leads = (Lead.query
             .filter(Lead.procam_vertical == TARGET,
                     Lead.is_archived.isnot(True),
                     Lead.updated_at >= when)
             .order_by(Lead.id).limit(limit).all())
    for lead in leads:
        verdict = classify(lead, when)
        if verdict is None:
            continue
        bucket, detail = verdict
        out[bucket].append({
            'lead_id': lead.id,
            'company': (lead.company or '')[:80],
            'title': (lead.project or '')[:100],
            'stage': lead.stage or '',
            'pic': lead.assigned_to or '',
            'updated_at': str(lead.updated_at)[:16],
            'now': TARGET,
            **detail})
    out['examined'] = len(leads)
    return out
