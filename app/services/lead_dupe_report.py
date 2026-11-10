"""Every pair of live leads that looks like one enquiry.

CRM-WA-01 §2: "A one-off report of current likely duplicates (dry
run, CSV) for the PFM team to review before any merge."

PAIRS, NOT CLUSTERS

`lead_merge.groups()` already finds clusters by thread key, which is
the right shape for mail threads. This is the other question: two
leads that share a GSTIN, a phone, a normalised name or a domain,
whatever arrived them. A pair is what a person can decide about —
"are #10291 and #10433 the same job" has an answer, and "are these
six leads one thing" usually does not.

WHICH ONE IT SUGGESTS KEEPING

The same rule as the thread-duplicate report (QUESTIONS 71): the most
advanced live status, then the newest. In this data the older copy is
usually the empty one somebody marked Lost, so suggesting the oldest
would hand somebody a list that discards the work and keeps the husk.

IT SUGGESTS AND NOTHING ELSE

No merge, no archive, no flag. §2 says the PFM team reviews it before
any merge, and a report that acted on its own findings would make
that review a formality.
"""
from __future__ import annotations

from datetime import datetime


def _rank(lead):
    """Sort key for "which of these would you keep". Highest wins.

    The same ladder the thread-duplicate report uses, read off
    `stage_map.JOURNEY` so the two reports cannot disagree about
    which copy of a lead is the real one.
    """
    from app.services import stage_map

    stage = stage_map.canonical(getattr(lead, 'stage', None))
    if stage in stage_map.JOURNEY:
        advancement = 100 + stage_map.JOURNEY.index(stage)
    elif stage in ('Lost', 'Not Interested', 'On Hold'):
        advancement = ('Lost', 'Not Interested',
                       'On Hold').index(stage)
    else:
        advancement = 3
    return (advancement, lead.created_at or datetime.min, lead.id)


def pairs(*, limit=100, within_days=None):
    """Live duplicate pairs, strongest signal first.

    One row per pair rather than per lead, and each pair appears once
    — a list that showed #10291/#10433 and #10433/#10291 would be
    read as two problems.
    """
    from app import Lead
    from app.services import lead_duplicates as ld

    rows = ld._live_leads(limit_days=within_days)
    prints = {lead.id: (lead, ld.fingerprint(lead=lead))
              for lead in rows}

    seen, out = set(), []
    for lead_id, (lead, mine) in prints.items():
        for other_id, (other, theirs) in prints.items():
            if other_id <= lead_id:
                continue
            matched = ld.compare(mine, theirs)
            if not matched:
                continue
            score = ld.score(matched)
            if score < ld.WORTH_SHOWING:
                continue
            key = (lead_id, other_id)
            if key in seen:
                continue
            seen.add(key)

            keep = max((lead, other), key=_rank)
            drop = other if keep is lead else lead
            out.append({
                'score': score,
                'matched': matched,
                'why': '; '.join(ld.MEANING[k] for k in matched),
                'keep': _row(keep),
                'drop': _row(drop),
            })
    out.sort(key=lambda p: (-p['score'], p['keep']['lead_id']))
    return out[:limit]


def _row(lead):
    return {
        'lead_id': lead.id,
        'company': (lead.company or '')[:60],
        'contact': (lead.pic or '')[:40],
        'email': (lead.email or '')[:60],
        'phone': (lead.phone or '')[:24],
        'stage': lead.stage or '',
        'owner': lead.assigned_to or '',
        'created': str(lead.created_at or '')[:10],
    }
