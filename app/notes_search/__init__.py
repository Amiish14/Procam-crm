"""Notes search: keyword ranking over the notes a viewer may see, with an
optional meaning-based pass through the Copilot index.

Permission first, always: candidates come from app.access.scope.notes, so
a note on a lead the viewer cannot open is never scored or returned.
"""
import math
import re
from datetime import datetime, timedelta

_WORD = re.compile(r"[a-z0-9][a-z0-9/'\-]*")
_STOP = {'the', 'and', 'for', 'with', 'that', 'this', 'from', 'was', 'are',
         'has', 'have', 'not', 'but', 'you', 'our', 'his', 'her', 'they',
         'will', 'can', 'all', 'any', 'about', 'into', 'out', 'who'}
#: Relevance halves every this many days, so a recent note about the same
#: thing outranks one from two years ago without hiding the old one.
RECENCY_HALF_LIFE_DAYS = 180
MAX_CANDIDATES = 3000


def tokens(text):
    return [t for t in _WORD.findall((text or '').lower())
            if len(t) > 2 and t not in _STOP]


def _parse_day(v, end=False):
    if not v:
        return None
    try:
        d = datetime.strptime(str(v)[:10], '%Y-%m-%d')
    except ValueError:
        return None
    return d + timedelta(days=1) if end else d


def _snippet(text, terms, width=220):
    low = text.lower()
    hits = [low.find(t) for t in terms if low.find(t) >= 0]
    start = max(0, (min(hits) if hits else 0) - 60)
    piece = text[start:start + width]
    return ('…' if start else '') + piece + ('…' if start + width < len(text)
                                             else '')


def search_notes(query, *, author=None, date_from=None, date_to=None,
                 lead_id=None, limit=50, sc=None, now=None):
    from sqlalchemy import or_
    from app import Employee, Lead, LeadNote
    from app.access import scope as scope_mod

    sc = sc or scope_mod.current()
    now = now or datetime.utcnow()
    terms = list(dict.fromkeys(tokens(query)))[:12]

    q = scope_mod.notes(LeadNote.query, sc=sc).filter(
        LeadNote.is_deleted.isnot(True))
    if author:
        q = q.filter(LeadNote.author == author.strip().upper())
    start, end = _parse_day(date_from), _parse_day(date_to, end=True)
    if start:
        q = q.filter(LeadNote.created_at >= start)
    if end:
        q = q.filter(LeadNote.created_at < end)
    if lead_id:
        try:
            q = q.filter(LeadNote.lead_id == int(lead_id))
        except ValueError:
            return {'results': [], 'backend': 'none', 'total': 0}
    if terms:
        q = q.filter(or_(*[LeadNote.note_text.ilike(f'%{t}%')
                           for t in terms]))
    rows = q.order_by(LeadNote.created_at.desc()).limit(MAX_CANDIDATES).all()

    scored = []
    for n in rows:
        counts = {}
        for t in tokens(n.note_text):
            counts[t] = counts.get(t, 0) + 1
        matched = [t for t in terms if counts.get(t)]
        if terms and not matched:
            continue
        base = (sum(1 + math.log(counts[t]) for t in matched)
                * (len(matched) / len(terms))) if terms else 1.0
        if terms and query.strip().lower() in (n.note_text or '').lower():
            base *= 1.5                              # the exact phrase
        age = max(0.0, (now - (n.created_at or now)).total_seconds() / 86400)
        score = base * 0.5 ** (age / RECENCY_HALF_LIFE_DAYS)
        scored.append((score, n))

    backend = 'keyword'
    semantic_ids = set()
    if terms:
        try:
            from app.copilot import retrieval
            if retrieval.embeddings_available():
                found = retrieval.search(sc, query, limit=20)
                if found.get('backend') == 'vector':
                    backend = 'keyword+semantic'
                    semantic_ids = {h['lead_id'] for h in found['hits']
                                    if (h.get('source') or '') == 'note'}
        except Exception:
            semantic_ids = set()
    if semantic_ids:
        have = {n.id for _s, n in scored}
        extra = scope_mod.notes(LeadNote.query, sc=sc).filter(
            LeadNote.lead_id.in_(semantic_ids),
            LeadNote.is_deleted.isnot(True)).limit(100).all()
        scored += [(0.25, n) for n in extra if n.id not in have]

    scored.sort(key=lambda s: (-s[0], -(s[1].id or 0)))
    top = scored[:limit]
    lead_ids = {n.lead_id for _s, n in top}
    leads = {l.id: l for l in Lead.query.filter(Lead.id.in_(lead_ids))
             .all()} if lead_ids else {}
    authors = {n.author for _s, n in top if n.author}
    names = {e.emp_code: e.name for e in Employee.query.filter(
        Employee.emp_code.in_(authors)).all()} if authors else {}
    return {
        'backend': backend, 'total': len(scored),
        'results': [{
            'note_id': n.id, 'lead_id': n.lead_id,
            'company': (leads.get(n.lead_id).company
                        if leads.get(n.lead_id) else ''),
            'author': n.author or '',
            'author_name': names.get(n.author, n.author_name or ''),
            'created_at': str(n.created_at)[:16] if n.created_at else '',
            'note_type': n.note_type or 'general',
            'snippet': _snippet(n.note_text or '', terms),
            'score': round(score, 4),
        } for score, n in top],
    }
