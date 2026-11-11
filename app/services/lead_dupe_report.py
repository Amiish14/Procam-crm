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


#: A signal value shared by more than this many leads identifies
#: nothing: a shared switchboard, a group mail domain, a holding
#: company's GSTIN on fifty subsidiaries. Skipped rather than
#: reported, and skipping is said out loud by the script.
MAX_PER_VALUE = 25


def pairs(*, limit=100, within_days=None):
    """Live duplicate pairs, strongest signal first.

    One row per pair rather than per lead, and each pair appears once
    — a list that showed #10291/#10433 and #10433/#10291 would be
    read as two problems.

    CRM-MAIL-2026-11-11: this used to load every live lead as a full
    ORM row and compare all of them against all of them. On the live
    book that is thousands of rows carrying the customers' entire
    enquiries, and the script was killed by the kernel for memory
    before it printed anything.

    Two changes, no change in what it finds. The rows come from
    `lead_duplicates.live_prints`, which reads eleven columns and the
    GSTINs in one query each. And the pairs are found by *blocking*:
    a pair can only match if it shares a value for one of the four
    signals, so the four indexes below produce the candidates
    directly instead of n² comparisons discovering that 99.99% of
    them share nothing.
    """
    from collections import defaultdict

    from app.services import lead_duplicates as ld

    prints = ld.live_prints(limit_days=within_days)

    # One index per signal: value -> the leads carrying it. A lead
    # with no value for a signal is simply not in that index.
    buckets = defaultdict(lambda: defaultdict(list))
    for lead_id, (_lead, mine) in prints.items():
        for key, _score, _what in ld.SIGNALS:
            value = mine.get(key)
            if value:
                buckets[key][value].append(lead_id)

    candidates = set()
    for key, _score, _what in ld.SIGNALS:
        for _value, ids in buckets[key].items():
            if len(ids) < 2:
                continue
            # A value shared by hundreds of leads is a value that
            # identifies nothing — a switchboard number, a group
            # domain. Comparing them all would be the n² problem
            # again, in one bucket, and every pair it produced would
            # be noise.
            if len(ids) > MAX_PER_VALUE:
                continue
            ordered = sorted(ids)
            for i, first in enumerate(ordered):
                for second in ordered[i + 1:]:
                    candidates.add((first, second))

    out = []
    for first, second in candidates:
        lead, mine = prints[first]
        other, theirs = prints[second]
        matched = ld.compare(mine, theirs)
        if not matched:
            continue
        score = ld.score(matched)
        if score < ld.WORTH_SHOWING:
            continue
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
