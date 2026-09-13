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
    """Everything readable on one lead, as (source, text) pairs.

    The forwarded covering note is deliberately not separated out here —
    it is already kept apart on the Lead, and this indexes what is
    stored rather than re-parsing it.
    """
    from app import LeadEmail, LeadNote

    out = []
    if (lead.original_email_body or '').strip():
        out.append(('enquiry', lead.original_email_subject or '',
                    lead.original_email_body))
    # The column is note_text. This read `body` / `note`, which LeadNote
    # does not have, so no note was ever indexed. Deleted notes stay out.
    for note in (LeadNote.query.filter_by(lead_id=lead.id)
                 .filter(LeadNote.is_deleted.isnot(True)).limit(50).all()):
        text = note.note_text or ''
        if text.strip():
            out.append(('note', '', text))
    for mail in (LeadEmail.query.filter_by(lead_id=lead.id)
                 .order_by(LeadEmail.sent_or_received_at.desc())
                 .limit(50).all()):
        if (mail.body or '').strip():
            out.append((f'email:{mail.direction or "?"}',
                        mail.subject or '', mail.body))
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
    for source, subject, text in chunks_for_lead(lead):
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
                occurred_at=lead.updated_at or lead.created_at,
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
def _scoped_chunks(scope, *, limit=4000, lead_id=None, terms=None):
    """Candidate chunks, already inside the viewer's boundary.

    This is the whole security model of the retrieval layer: the filter
    is on the query, so an out-of-scope chunk is never a candidate.

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
    if terms:
        # A chunk with none of the words scores zero in the lexical
        # ranker anyway; leaving it out of the candidates changes no
        # result, only what fits under the cap. Tokens are lower-case
        # words, and LIKE is case-insensitive for them.
        q = q.filter(or_(*[CopilotChunk.text.ilike(f'%{t}%')
                           for t in list(dict.fromkeys(terms))[:12]]))
    return (q.order_by(CopilotChunk.occurred_at.desc(),
                       CopilotChunk.id.desc())
            .limit(limit).all())


def search(scope, query, *, limit=8, lead_id=None):
    """Ranked chunks the viewer may see. Never raises.

    Returns {'backend': 'lexical'|'vector', 'hits': [...]}, each hit
    carrying its lead, account, source and the text itself, so the
    answer can cite where it came from.
    """
    try:
        terms = tokens(query)
        if not terms:
            return {'backend': 'none', 'hits': []}

        vector = _backend() == 'vector'
        # The vector ranker finds passages without the literal words, so
        # it is not narrowed by them.
        candidates = _scoped_chunks(scope, lead_id=lead_id,
                                    terms=None if vector else terms)
        if not candidates:
            return {'backend': _backend(), 'hits': []}

        if vector:
            scored = _vector_rank(query, candidates)
            if scored is not None:
                return {'backend': 'vector',
                        'hits': _shape(scored[:min(limit, MAX_HITS)])}

        return {'backend': 'lexical',
                'hits': _shape(_lexical_rank(terms, candidates)
                               [:min(limit, MAX_HITS)])}
    except Exception:
        try:
            from app import app as flask_app
            flask_app.logger.exception('copilot retrieval failed')
        except Exception:
            pass
        return {'backend': 'error', 'hits': []}


def _lexical_rank(terms, candidates):
    """TF-IDF over the candidate set, which is small by construction.

    Not a general search engine — it ranks a few thousand chunks the
    viewer may already see. That is enough to find "the Airoli
    transformer job" in a year of email, and it needs no GPU.
    """
    docs = [(c, Counter(tokens(c.text))) for c in candidates]
    n = len(docs)
    df = Counter()
    for _c, counts in docs:
        for t in counts:
            df[t] += 1

    scored = []
    for chunk, counts in docs:
        if not counts:
            continue
        total = sum(counts.values())
        score = 0.0
        matched = 0
        for t in terms:
            tf = counts.get(t, 0)
            if not tf:
                continue
            matched += 1
            idf = math.log((n + 1) / (df[t] + 1)) + 1.0
            score += (tf / total) * idf
        if not matched:
            continue
        # Every term present beats one term many times — a question is
        # a conjunction, not a bag.
        score *= 1.0 + (matched / len(terms))
        scored.append((score, chunk))
    scored.sort(key=lambda s: -s[0])
    return scored


def _shape(scored):
    out = []
    for score, c in scored:
        out.append({
            'lead_id': c.lead_id,
            'account': c.account_name or '',
            'source': c.source,
            'vertical': c.vertical or '',
            'when': str(c.occurred_at or '')[:10],
            'text': c.text,
            'score': round(float(score), 4),
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
