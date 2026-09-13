"""
§4 — retrieval over the unstructured half of the CRM.

Notes, emails and the original enquiry are where the answer often is,
and none of it is a column you can filter on. This is the layer that
finds it.

The rule that shapes everything here
    Permission filtering happens BEFORE retrieval, not after. A chunk
    the viewer may not see is never a candidate, so it never enters a
    context window and cannot be surfaced by a cleverly worded question.
    Filtering afterwards would work right up until the day it didn't.

Two backends, one interface
    Lexical scoring runs today, against the database, with no GPU and no
    vector service. When PROCAM_AI_EMBED_URL points at a Procam-hosted
    embedding model, the same chunks carry vectors and search switches
    to cosine similarity. Callers see no difference — `search()` returns
    the same shape either way, and says which backend answered.

    Building the vector path first would have meant an empty index and
    a GPU nobody has yet. Building the lexical path first means the
    feature works now and gets better later, which is the right order.

Metadata travels with the chunk
    record id, account, opportunity, owner, vertical, date, and the
    owner code the scope filter matches on. Ownership changes invalidate
    the chunk — a lead reassigned to another vertical changes who may
    retrieve it, and an index that keeps yesterday's permissions is the
    failure mode most RAG-over-CRM builds ship with.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
from collections import Counter

from app.access import scope as sc_mod


#: Chunks are small enough to be a quotable answer and large enough to
#: carry context. Emails are chunked; a note usually is one chunk.
CHUNK_CHARS = 900
CHUNK_OVERLAP = 120

#: Never return more than this, whatever the caller asks for.
MAX_HITS = 20

_WORD = re.compile(r"[a-z0-9][a-z0-9'&/-]*")

#: Words that match everything and rank nothing.
_STOP = {
    'the', 'and', 'for', 'you', 'are', 'our', 'with', 'from', 'this',
    'that', 'have', 'has', 'was', 'were', 'will', 'would', 'can', 'not',
    'but', 'all', 'any', 'please', 'kindly', 'dear', 'sir', 'madam',
    'regards', 'thanks', 'thank', 'hi', 'hello', 'we', 'us', 'is', 'it',
    'to', 'of', 'in', 'on', 'at', 'as', 'be', 'by', 'or', 'a', 'an',
}


def tokens(text):
    return [t for t in _WORD.findall((text or '').lower())
            if len(t) > 2 and t not in _STOP]


# ── boilerplate ──────────────────────────────────────────────────────
#
# The mail gateway stamps a banner on every external email, and it is
# the first thing in the body. Indexed as-is, it became the visible
# opening of half the search results — a reader saw "CAUTION: This email
# originated from outside…" where the enquiry should have been. It is
# not content; it is furniture, and identical on thousands of messages.

_BOILERPLATE = (
    re.compile(r'^\s*CAUTION\s*:.*?content is safe\.?\s*', re.I | re.S),
    re.compile(r'^\s*\[?EXTERNAL(?:\s+EMAIL)?\]?\s*:?\s*', re.I),
    re.compile(r'^\s*(?:Confidential|RESTRICTED|Internal Use Only)\s*$',
               re.I | re.M),
    re.compile(r'\s*Sent from my (?:iPhone|iPad|Android|Samsung).*$',
               re.I | re.S),
)


def strip_boilerplate(text):
    """Drop the gateway banner and signature furniture from a body.

    Conservative on purpose: each pattern is anchored, and only removes
    text that is demonstrably not the customer's. Over-stripping would
    lose the enquiry, which is worse than showing a banner.
    """
    out = (text or '')
    for rx in _BOILERPLATE:
        out = rx.sub('', out, count=1)
    return out.strip()


# ── chunking ─────────────────────────────────────────────────────────
def split(text, *, size=CHUNK_CHARS, overlap=CHUNK_OVERLAP):
    """Overlapping windows, broken on a line where possible.

    The overlap matters: a route and its tonnage often straddle a line
    break, and a hard split would put them in different chunks and find
    neither.
    """
    body = (text or '').strip()
    if not body:
        return []
    if len(body) <= size:
        return [body]

    out, start = [], 0
    while start < len(body):
        end = min(start + size, len(body))
        if end < len(body):
            nl = body.rfind('\n', start + size // 2, end)
            if nl > start:
                end = nl
        out.append(body[start:end].strip())
        if end >= len(body):
            break
        start = max(end - overlap, start + 1)
    return [c for c in out if c]


# ── building the index ───────────────────────────────────────────────
def chunks_for_lead(lead):
    """Everything readable on one lead, as (source, subject, text).

    The forwarded covering note is deliberately not separated out here —
    it is already kept apart on the Lead, and this indexes what is
    stored rather than re-parsing it.
    """
    return [(source, subject, text)
            for source, subject, text, _when in _dated_chunks_for_lead(lead)]


def _dated_chunks_for_lead(lead):
    """chunks_for_lead with the date each passage was actually written.

    Every chunk used to carry the lead's updated_at, so a two-year-old
    email looked as fresh as this morning's note to the recency ranking
    and to the citation a reader sees. Each passage now carries its own
    date, falling back to the lead's only when the passage has none.
    """
    from app import LeadEmail, LeadNote

    out = []
    if (lead.original_email_body or '').strip():
        out.append(('enquiry', lead.original_email_subject or '',
                    lead.original_email_body,
                    lead.original_email_received_at or lead.created_at))
    # The column is note_text. This read `body` / `note`, which LeadNote
    # does not have, so no note was ever indexed. Deleted notes stay out.
    for note in (LeadNote.query.filter_by(lead_id=lead.id)
                 .filter(LeadNote.is_deleted.isnot(True)).limit(50).all()):
        text = note.note_text or ''
        if text.strip():
            out.append(('note', '', text, note.created_at))
    for mail in (LeadEmail.query.filter_by(lead_id=lead.id)
                 .order_by(LeadEmail.sent_or_received_at.desc())
                 .limit(50).all()):
        if (mail.body or '').strip():
            out.append((f'email:{mail.direction or "?"}',
                        mail.subject or '', mail.body,
                        mail.sent_or_received_at or mail.created_at))
    return out


def index_lead(lead, *, commit=True):
    """(re)build the chunks for one lead. Returns how many were written.

    Rebuilds rather than appends, because the owner and vertical on
    every chunk have to match the lead as it is now — that is the
    invalidation §4 asks for, and doing it any other way leaves stale
    permissions in the index.
    """
    from app import Company, db
    from app.models.copilot import CopilotChunk

    CopilotChunk.query.filter_by(lead_id=lead.id).delete(
        synchronize_session=False)

    account = (Company.query.get(lead.company_id)
               if lead.company_id else None)
    written = 0
    # The original enquiry and the first inbound email are usually the
    # same text — the trail seeds row 1 from the message that created
    # the lead. Indexed twice, every hit came back twice and duplicates
    # crowded other accounts out of the top ten. Deduplicated on the
    # normalised text, keeping whichever source is seen first.
    seen = set()
    for source, subject, text, written_at in _dated_chunks_for_lead(lead):
        cleaned = strip_boilerplate(text)
        if not cleaned:
            continue
        for n, piece in enumerate(
                split(f'{subject}\n{cleaned}'.strip())):
            fingerprint = hashlib.sha1(
                ' '.join(piece.lower().split()).encode()).hexdigest()
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            db.session.add(CopilotChunk(
                lead_id=lead.id,
                company_id=lead.company_id,
                source=source[:24],
                seq=n,
                owner_emp_code=lead.assigned_to or '',
                secondary_emp_code=lead.secondary_owner or '',
                vertical=(lead.procam_vertical
                          or (account.vertical if account else '') or ''),
                account_name=(account.name if account else lead.company) or '',
                occurred_at=(written_at or lead.updated_at
                             or lead.created_at),
                text=piece[:2000],
                embedding=None))
            written += 1
    if commit:
        db.session.commit()
    return written


def invalidate_lead(lead_id):
    """Called when ownership or vertical changes.

    A lead reassigned to another vertical changes who may retrieve its
    text. Dropping the chunks is safer than updating them: a miss costs
    one re-index, a stale row costs a disclosure.
    """
    from app import db
    from app.models.copilot import CopilotChunk

    CopilotChunk.query.filter_by(lead_id=lead_id).delete(
        synchronize_session=False)
    db.session.commit()


# ── searching ────────────────────────────────────────────────────────
#: The source types a caller may filter on, and how each maps onto the
#: `source` column (enquiry | note | email:inbound | email:outbound).
SOURCE_TYPES = ('enquiry', 'note', 'email')

# ── ranking weights ──────────────────────────────────────────────────
#
# Every number here is a documented, deterministic choice — the ranking
# has to be explainable to the person who asks "why was this first?".
#
#: Recency half-life. A passage this many days old keeps half the
#: recency credit of one written today; a year-old one keeps a quarter.
RECENCY_HALF_LIFE_DAYS = 180
#: The most recency can move a score. Relevance stays in charge: an old
#: email that says exactly what was asked must still beat a new one that
#: only mentions a word of it, so recency is a tie-breaker with teeth,
#: not a filter.
RECENCY_SHARE = 0.35
#: What kind of text a passage is. The customer's enquiry and a colleague's
#: note are written about the job; an outbound email is often our own
#: boilerplate quoted back.
SOURCE_WEIGHT = {'enquiry': 1.25, 'note': 1.15, 'email:inbound': 1.0,
                 'email:outbound': 0.9}
#: A passage that is mostly a signature block — names, phone numbers,
#: "Regards" — matches a company or a person's name on every message
#: they ever sent, and says nothing about the job.
SIGNATURE_WEIGHT = 0.5
#: A synonym or expanded abbreviation counts for this share of a word
#: the person typed. It is a hint, not evidence.
EXPANSION_WEIGHT = 0.6
#: The query's words appearing together, in order.
PHRASE_BONUS = 0.5
#: The query's words appearing close together (full bonus when adjacent,
#: shrinking as the window around them grows).
PROXIMITY_BONUS = 0.3
#: Hybrid mix when an internal embedder is configured: meaning first,
#: wording second, both on a 0–1 scale before the priors above apply.
HYBRID_VECTOR_SHARE = 0.65


def _as_datetime(value, *, end=False):
    """A date filter from a date, a datetime or an ISO string, or None.

    An end date given as a plain date means "through the end of that
    day", which is what "to 31 August" means to the person asking.
    """
    from datetime import date, datetime, timedelta

    if value in (None, ''):
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        dt = datetime(value.year, value.month, value.day)
        return dt + timedelta(days=1) if end else dt
    try:
        d = datetime.strptime(str(value)[:10], '%Y-%m-%d')
    except ValueError:
        return None
    return d + timedelta(days=1) if end else d


def _scoped_chunks(scope, *, limit=4000, lead_id=None, terms=None,
                   date_from=None, date_to=None, owner=None, source=None,
                   company_id=None, account=None):
    """Candidate chunks, already inside the viewer's boundary.

    This is the whole security model of the retrieval layer: the filter
    is on the query, so an out-of-scope chunk is never a candidate. Every
    optional filter below is ANDed onto that boundary — each can only
    narrow what the scope already allows, never reach past it.

    The cap used to be applied to the whole scoped table in id order, and
    the lead and the words were filtered afterwards in Python. With more
    than 4,000 chunks in scope — any admin or vertical head on production
    data — search only ever looked at the oldest 4,000, and "search this
    lead" found nothing for a lead indexed later. The lead and (for the
    lexical ranker) the words now narrow the query before the cap, and
    the newest passages win when it still applies.
    """
    from app.models.copilot import CopilotChunk
    from sqlalchemy import or_

    q = CopilotChunk.query
    if scope.codes is not None:
        if not scope.codes:
            return []
        q = q.filter(or_(
            CopilotChunk.owner_emp_code.in_(scope.codes),
            CopilotChunk.secondary_emp_code.in_(scope.codes)))
    if lead_id:
        q = q.filter(CopilotChunk.lead_id == int(lead_id))
    if company_id:
        q = q.filter(CopilotChunk.company_id == int(company_id))
    if account:
        q = q.filter(CopilotChunk.account_name.ilike(f'%{account}%'))
    if owner:
        # Chunks carry the lead's owners, not a note's author — the
        # author is not indexed, so "by" filters on who owns the lead.
        q = q.filter(or_(CopilotChunk.owner_emp_code == owner,
                         CopilotChunk.secondary_emp_code == owner))
    if source:
        kind = str(source).lower()
        if kind == 'email':
            q = q.filter(CopilotChunk.source.like('email:%'))
        elif kind in ('note', 'enquiry'):
            q = q.filter(CopilotChunk.source == kind)
    start = _as_datetime(date_from)
    stop = _as_datetime(date_to, end=True)
    if start is not None:
        q = q.filter(CopilotChunk.occurred_at >= start)
    if stop is not None:
        q = q.filter(CopilotChunk.occurred_at < stop)
    if terms:
        # A chunk with none of the words scores zero in the lexical
        # ranker anyway; leaving it out of the candidates changes no
        # result, only what fits under the cap. Tokens are lower-case
        # words, and LIKE is case-insensitive for them. Expanded
        # spellings are included, or "BL" could never find a passage
        # that only says "bill of lading".
        q = q.filter(or_(*[CopilotChunk.text.ilike(f'%{t}%')
                           for t in list(dict.fromkeys(terms))[:24]]))
    return (q.order_by(CopilotChunk.occurred_at.desc(),
                       CopilotChunk.id.desc())
            .limit(limit).all())


def search(scope, query, *, limit=8, lead_id=None, date_from=None,
           date_to=None, owner=None, source=None, company_id=None,
           account=None, now=None):
    """Ranked chunks the viewer may see. Never raises.

    Returns {'backend': 'lexical'|'hybrid'|..., 'hits': [...],
    'filters': {...}, 'expanded': [...]}. Each hit carries its lead,
    account, source, date and a citation, so the answer can say where it
    came from. `filters` lists what was actually applied, in words, so
    the answer can say that too.

    Optional filters — every one narrows inside the scope:
        date_from, date_to   a date, datetime or 'YYYY-MM-DD'
        owner                an emp_code: leads that person owns
        source               'note' | 'email' | 'enquiry'
        company_id, account  one account, by id or by name
        lead_id              one lead
    """
    filters = _describe_filters(lead_id=lead_id, date_from=date_from,
                                date_to=date_to, owner=owner, source=source,
                                company_id=company_id, account=account)
    try:
        from app.copilot import vocabulary

        terms = tokens(query)
        # tokens() drops anything under three letters, which is right for
        # "of" and wrong for "BL" or "HL". A short word the vocabulary
        # knows is kept, and matched on word boundaries like a phrase.
        typed = vocabulary.short_forms(query)
        if not terms and not typed:
            return {'backend': 'none', 'hits': [], 'filters': filters,
                    'expanded': []}
        groups = vocabulary.expansion_groups(query)
        expanded = [alt for g in groups for alt in g]

        vector = _backend() == 'vector'
        narrow = dict(lead_id=lead_id, date_from=date_from, date_to=date_to,
                      owner=owner, source=source, company_id=company_id,
                      account=account)
        # The vector ranker finds passages without the literal words, so
        # it is not narrowed by them.
        candidates = _scoped_chunks(
            scope, terms=None if vector else terms + typed + expanded,
            **narrow)
        if not candidates:
            return {'backend': _backend(), 'hits': [], 'filters': filters,
                    'expanded': expanded}

        cap = min(int(limit or 8), MAX_HITS)
        lexical = _lexical_scores(terms, candidates, groups=groups,
                                  typed=typed)
        if vector:
            cosine = _vector_rank(query, candidates)
            if cosine is not None:
                scored = _hybrid(cosine, lexical, now=now)
                return {'backend': 'hybrid', 'hits': _shape(scored[:cap]),
                        'filters': filters, 'expanded': expanded}

        scored = [(raw * _prior(chunk, now=now), chunk, matched)
                  for raw, chunk, matched in lexical]
        scored.sort(key=lambda s: (-s[0], -(s[1].id or 0)))
        return {'backend': 'lexical', 'hits': _shape(scored[:cap]),
                'filters': filters, 'expanded': expanded}
    except Exception:
        try:
            from app import app as flask_app
            flask_app.logger.exception('copilot retrieval failed')
        except Exception:
            pass
        return {'backend': 'error', 'hits': [], 'filters': filters,
                'expanded': []}


def _describe_filters(**f):
    """The filters in words, for the answer's "how I answered" line.
    Values the caller supplied, never the query that applied them."""
    out = {}
    for key in ('lead_id', 'company_id', 'account', 'owner', 'source'):
        if f.get(key):
            out[key] = str(f[key])
    for key in ('date_from', 'date_to'):
        if f.get(key):
            out[key] = str(f[key])[:10]
    return out


def looks_like_signature(text):
    """True when most of a passage is a signature block.

    Two or more signature-shaped lines, making up at least half of the
    non-empty lines. Both conditions: one "Regards" at the foot of a
    real enquiry must not halve that enquiry's score.
    """
    lines = [l for l in (text or '').splitlines() if l.strip()]
    if not lines:
        return False
    sig = sum(1 for l in lines if _SIGNATURE_LINE.search(l))
    return sig >= 2 and sig * 2 >= len(lines)


_SIGNATURE_LINE = re.compile(
    r'^\s*(?:(?:best|warm|kind)?\s*regards\b|thanks\s*(?:&|and)\s*regards|'
    r'sincerely|yours\s+(?:truly|faithfully|sincerely)|'
    r'(?:mob(?:ile)?|tel|ph(?:one)?|fax|cell|direct)\b\s*[:.]?|'
    r'[mte]\s*:|e-?mail\s*:|web\s*:|www\.|https?://|'
    r'\+?\d[\d\s()./-]{7,}\s*$|[\w.+-]+@[\w-]+\.[\w.]+\s*$)',
    re.IGNORECASE)


def _prior(chunk, *, now=None):
    """Source × signature × recency — everything that is not relevance.

    Undated passages get no recency credit rather than full credit: a
    date we cannot see is not evidence the text is new.
    """
    from datetime import datetime

    weight = SOURCE_WEIGHT.get((chunk.source or '').lower(), 1.0)
    if looks_like_signature(chunk.text):
        weight *= SIGNATURE_WEIGHT
    recency = 0.0
    when = chunk.occurred_at
    if when is not None:
        age = max(((now or datetime.utcnow()) - when).days, 0)
        recency = 0.5 ** (age / float(RECENCY_HALF_LIFE_DAYS))
    return weight * ((1.0 - RECENCY_SHARE) + RECENCY_SHARE * recency)


def _positions(seq, term):
    return [i for i, t in enumerate(seq) if t == term]


def _proximity(seq, present):
    """1.0 when the distinct query words sit side by side, less as the
    smallest window holding all of them widens. 0 for fewer than two."""
    if len(present) < 2:
        return 0.0
    points = sorted((i, t) for t in present for i in _positions(seq, t))
    need, best = len(present), None
    have, left = Counter(), 0
    for right, (pos, term) in enumerate(points):
        have[term] += 1
        while len(have) == need:
            width = pos - points[left][0] + 1
            best = width if best is None else min(best, width)
            lt = points[left][1]
            have[lt] -= 1
            if not have[lt]:
                del have[lt]
            left += 1
    if not best:
        return 0.0
    return need / float(best)


def _has_phrase_in(seq, phrase_tokens):
    n = len(phrase_tokens)
    if n < 2:
        return False
    return any(seq[i:i + n] == phrase_tokens for i in range(len(seq) - n + 1))


def _lexical_scores(terms, candidates, *, groups=(), typed=()):
    """[(relevance, chunk, matched_words)] — relevance only, no priors.

    TF-IDF over the candidate set, which is small by construction, plus:
      * short typed forms ("BL") and expanded spellings ("bill of
        lading"), matched on word boundaries so a two-letter
        abbreviation cannot match inside a longer word; an expansion
        counts EXPANSION_WEIGHT of a typed word;
      * a coverage multiplier — every concept present beats one word
        many times, because a question is a conjunction, not a bag;
      * PHRASE_BONUS when the words appear together in order, and
        PROXIMITY_BONUS scaled by how tightly they cluster.
    """
    from app.copilot import vocabulary

    wanted = list(dict.fromkeys(terms))
    # (weight, spellings) per concept: each typed short form is its own
    # concept at full weight; each synonym group one concept at less.
    concepts = ([(1.0, [t]) for t in dict.fromkeys(typed)]
                + [(EXPANSION_WEIGHT, list(g)) for g in groups])
    docs = []
    for c in candidates:
        seq = tokens(c.text)
        norm = ' ' + vocabulary._norm(c.text) + ' '
        docs.append((c, seq, Counter(seq), norm))
    n = len(docs)
    df = Counter()
    for _c, _seq, counts, norm in docs:
        for t in counts:
            df[t] += 1
        for _w, spellings in concepts:
            for alt in spellings:
                if vocabulary._has_phrase(norm, alt):
                    df['\x00' + alt] += 1

    denominator = float(len(wanted) + len(typed)) or 1.0
    out = []
    for chunk, seq, counts, norm in docs:
        total = float(len(seq)) or 1.0
        score, present = 0.0, []
        for t in wanted:
            tf = counts.get(t, 0)
            if not tf:
                continue
            present.append(t)
            idf = math.log((n + 1) / (df[t] + 1)) + 1.0
            score += (tf / total) * idf
        concepts_hit = []
        for weight, spellings in concepts:
            for alt in spellings:
                occ = len(re.findall(r'(?<![a-z0-9])' + re.escape(alt)
                                     + r'(?![a-z0-9])', norm))
                if not occ:
                    continue
                idf = math.log((n + 1) / (df['\x00' + alt] + 1)) + 1.0
                score += weight * (occ / total) * idf
                concepts_hit.append(alt)
                break
        if score <= 0:
            continue
        coverage = min(1.0, (len(present) + len(concepts_hit)) / denominator)
        score *= 1.0 + coverage
        if wanted and len(present) == len(wanted) and \
                _has_phrase_in(seq, [t for t in terms if t in counts]):
            score *= 1.0 + PHRASE_BONUS
        score *= 1.0 + PROXIMITY_BONUS * _proximity(seq, present)
        out.append((score, chunk, present + concepts_hit))
    return out


def _lexical_rank(terms, candidates, *, groups=(), typed=(), now=None):
    """Ranked [(score, chunk)] — the lexical backend end to end.

    Kept with its original shape for callers and tests that rank a
    candidate list directly."""
    scored = [(raw * _prior(c, now=now), c)
              for raw, c, _m in _lexical_scores(terms, candidates,
                                                groups=groups, typed=typed)]
    scored.sort(key=lambda s: (-s[0], -(s[1].id or 0)))
    return scored


def _hybrid(cosine, lexical, *, now=None):
    """Meaning and wording together, when an embedder is configured.

    Both scores go onto 0–1 (cosine clipped at 0, lexical divided by the
    best lexical score) and mix HYBRID_VECTOR_SHARE : the rest, then the
    same source/recency priors apply. A passage the embedder skipped for
    having no vector still ranks on its wording rather than vanishing.
    """
    lex = {c.id: (raw, matched) for raw, c, matched in lexical}
    top = max([raw for raw, _m in lex.values()] or [0.0]) or 1.0
    seen, out = set(), []
    for cos, chunk in cosine:
        raw, matched = lex.get(chunk.id, (0.0, []))
        mix = (HYBRID_VECTOR_SHARE * max(float(cos), 0.0)
               + (1.0 - HYBRID_VECTOR_SHARE) * (raw / top))
        out.append((mix * _prior(chunk, now=now), chunk, matched))
        seen.add(chunk.id)
    for raw, chunk, matched in lexical:
        if chunk.id in seen:
            continue
        mix = (1.0 - HYBRID_VECTOR_SHARE) * (raw / top)
        out.append((mix * _prior(chunk, now=now), chunk, matched))
    out.sort(key=lambda s: (-s[0], -(s[1].id or 0)))
    return out


def source_type(source):
    """'email:inbound' → 'email'; the filterable kind of a chunk."""
    s = (source or '').lower()
    return 'email' if s.startswith('email') else s


def _shape(scored):
    out = []
    for item in scored:
        score, c = item[0], item[1]
        matched = item[2] if len(item) > 2 else []
        when = str(c.occurred_at or '')[:10]
        out.append({
            'lead_id': c.lead_id,
            'account': c.account_name or '',
            'source': c.source,
            'vertical': c.vertical or '',
            'when': when,
            'text': c.text,
            'score': round(float(score), 4),
            'chunk_id': c.id,
            'company_id': c.company_id,
            'source_type': source_type(c.source),
            'matched': list(matched),
            # §3.5 — what a reader clicks to check the passage: the lead
            # it sits on, the kind of text, and the date it was written.
            'citation': {'type': 'lead', 'id': c.lead_id,
                         'label': c.account_name or f'Lead {c.lead_id}',
                         'source': source_type(c.source) or 'text',
                         'date': when},
        })
    return out


# ── the embedding path, for when there is a host ─────────────────────
def _backend():
    return 'vector' if embeddings_available() else 'lexical'


def embeddings_available():
    """Only a Procam-hosted embedder, on the same §3.1 terms as the
    language model."""
    from app.copilot import model as model_mod

    url = (os.environ.get('PROCAM_AI_EMBED_URL') or '').strip()
    if not url:
        return False
    host = re.sub(r'^https?://', '', url).split('/')[0].lower()
    return not any(host == bad or host.endswith('.' + bad)
                   for bad in model_mod._PUBLIC)


def embed(texts):
    """Vectors from the internal embedder, or None. Never raises."""
    if not embeddings_available():
        return None
    try:
        from openai import OpenAI

        client = OpenAI(api_key=os.environ.get('PROCAM_AI_KEY')
                        or 'not-needed',
                        base_url=os.environ['PROCAM_AI_EMBED_URL'].rstrip('/'),
                        timeout=float(os.environ.get(
                            'PROCAM_AI_TIMEOUT', '20')))
        resp = client.embeddings.create(
            model=(os.environ.get('PROCAM_AI_EMBED_MODEL')
                   or 'nomic-embed-text'),
            input=list(texts))
        return [d.embedding for d in resp.data]
    except Exception:
        try:
            from app import app as flask_app
            flask_app.logger.info('copilot embedder unavailable, '
                                  'falling back to lexical')
        except Exception:
            pass
        return None


def _vector_rank(query, candidates):
    """Cosine similarity, or None to fall back.

    Chunks with no stored vector are skipped rather than scored badly:
    an index that is half-built should degrade to lexical, not quietly
    return only the half that happens to be embedded.
    """
    import json

    vectors = embed([query])
    if not vectors:
        return None
    q = vectors[0]
    embedded = [c for c in candidates if c.embedding]
    if len(embedded) < len(candidates) * 0.9:
        return None

    def cosine(a, b):
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a)) or 1.0
        nb = math.sqrt(sum(y * y for y in b)) or 1.0
        return dot / (na * nb)

    scored = []
    for c in embedded:
        try:
            v = json.loads(c.embedding)
        except Exception:
            continue
        scored.append((cosine(q, v), c))
    scored.sort(key=lambda s: -s[0])
    return scored


def stats():
    """What the admin screen shows about the index."""
    from app.models.copilot import CopilotChunk

    try:
        total = CopilotChunk.query.count()
        vectored = CopilotChunk.query.filter(
            CopilotChunk.embedding.isnot(None)).count()
    except Exception:
        return {'chunks': 0, 'embedded': 0, 'backend': _backend(),
                'error': 'index table not present'}
    return {'chunks': total, 'embedded': vectored, 'backend': _backend(),
            'embedder': (os.environ.get('PROCAM_AI_EMBED_MODEL')
                         if embeddings_available() else None)}
