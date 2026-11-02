"""
The terms a quote is made under.

CRM 2.0 R5, rebuilt for PROCAM-DECISIONS-02NOV Module 1. Four
things, in the order they print:

  the master (STC)       one Procam Standard T&C, on every quote in
                         every vertical
  the annexure           optional, per vertical, for what genuinely
                         differs there
  the client's own T&C   a file they or the tender sent, either
                         replacing the master or added to it
  special conditions     typed on one quote because that job is
  (SCC)                  unusual; overrides the clause it names

A quote is never blocked for want of any of them (Module 1.5). If
there is no annexure the master goes alone; if there is no master
either, the quote still prints and `for_print()` says what was
missing. Nothing in this module raises on the issue path.

Versioned, never edited
    `publish()` supersedes the current version and writes a new one.
    Nothing is updated in place, because a quote printed in March
    was made under March's terms and editing the row in June
    silently rewrites what a client agreed to. A quote records the
    version it printed, so "what did they actually sign up to" is a
    question with an answer.

    The cost is that fixing a typo makes a version. That is the
    right cost: the alternative is a contract whose history is
    whatever is in somebody's sent items.
"""
from __future__ import annotations

import re
from datetime import date, datetime

from app import db


class TermsError(ValueError):
    """Something the caller asked for cannot be stored as given."""


def current_for(vertical):
    """The annexure a new quote in this vertical would print.

    Kept under its old name and shape because six callers read it —
    the RFQ chain's condition proposals, the readiness check, the
    print path. What changed is that it no longer returns the whole
    of a quote's terms: the master below is the rest.
    """
    from app.models.quote_terms import ANNEXURE, QuoteTermSet

    vertical = (vertical or '').strip()
    if not vertical:
        return None
    return (QuoteTermSet.query
            .filter_by(vertical=vertical, is_current=True)
            .filter(db.or_(QuoteTermSet.kind == ANNEXURE,
                           QuoteTermSet.kind.is_(None)))
            .order_by(QuoteTermSet.version.desc()).first())


def master_current():
    """The Procam Standard T&C every quote prints."""
    from app.models.quote_terms import MASTER, QuoteTermSet

    return (QuoteTermSet.query
            .filter_by(kind=MASTER, is_current=True)
            .order_by(QuoteTermSet.version.desc()).first())


def master_history():
    from app.models.quote_terms import MASTER, QuoteTermSet

    return (QuoteTermSet.query.filter_by(kind=MASTER)
            .order_by(QuoteTermSet.version.desc()).all())


def history_for(vertical):
    from app.models.quote_terms import ANNEXURE, QuoteTermSet

    return (QuoteTermSet.query
            .filter_by(vertical=(vertical or '').strip())
            .filter(db.or_(QuoteTermSet.kind == ANNEXURE,
                           QuoteTermSet.kind.is_(None)))
            .order_by(QuoteTermSet.version.desc()).all())


def publish(vertical, body, *, title=None, actor=None,
            effective_from=None, kind=None):
    """A new current version. The old one is superseded, not deleted."""
    from app.models.quote_terms import (ANNEXURE, MASTER,
                                        MASTER_VERTICAL, QuoteTermSet)
    from app.services import audit

    kind = (kind or ANNEXURE).strip()
    if kind not in (MASTER, ANNEXURE):
        raise TermsError(f'A term set is {MASTER} or {ANNEXURE}, '
                         f'not {kind!r}.')
    vertical = (vertical or '').strip()
    if kind == MASTER:
        # The master belongs to no vertical, and a caller passing one
        # has almost certainly meant to publish an annexure.
        if vertical:
            raise TermsError('The Procam Standard T&C is not a '
                             'vertical\'s terms. Publish it with no '
                             'vertical, or publish an annexure.')
        vertical = MASTER_VERTICAL
    elif not vertical:
        raise TermsError('An annexure belongs to a vertical; name one.')
    body = (body or '').strip()
    if not body:
        raise TermsError('Empty terms are not terms.')

    was = master_current() if kind == MASTER else current_for(vertical)
    version = (was.version + 1) if was else 1
    row = QuoteTermSet(
        vertical=vertical, kind=kind, version=version,
        title=(title or ('Procam Standard Terms and Conditions'
                         if kind == MASTER
                         else 'Standard Terms and Conditions'))[:200],
        body=body, is_current=True,
        effective_from=effective_from or date.today(),
        created_by=actor)
    db.session.add(row)
    db.session.flush()

    if was is not None:
        was.is_current = False
        was.superseded_at = datetime.utcnow()
        was.superseded_by_id = row.id

    what = ('the Procam Standard T&C' if kind == MASTER
            else f'the {vertical} annexure')
    audit.record('quote_terms.published', 'quote_term_set',
                 str(row.id),
                 old={'version': was.version if was else None},
                 new={'vertical': vertical, 'kind': kind,
                      'version': version},
                 actor=actor,
                 reason=f'version {version} of {what}; version '
                        f'{was.version} superseded'
                        if was else
                        f'first version of {what}')
    return row


def publish_master(body, *, title=None, actor=None,
                   effective_from=None):
    """A new version of the Procam Standard T&C."""
    from app.models.quote_terms import MASTER

    return publish(None, body, title=title, actor=actor,
                   effective_from=effective_from, kind=MASTER)


def attach_to(quote, *, actor=None):
    """Stamp the current versions onto a quote.

    Called when the quote is built and again if it is re-issued. A
    quote that has already printed keeps the versions it printed —
    each id is set once and not moved, because the client has the old
    wording in their hand.

    The master and the annexure are stamped independently. A quote
    built before the master existed has an annexure id and no master
    id; re-issuing it picks up today's master without disturbing the
    annexure it already printed. That is the point of two columns.
    """
    from app.services import audit

    vertical = vertical_of(quote)
    attached = []

    master = None
    if not getattr(quote, 'terms_master_version_id', None):
        master = master_current()
        if master is not None:
            quote.terms_master_version_id = master.id
            attached.append(('master', master))

    annexure = None
    if not getattr(quote, 'terms_version_id', None):
        annexure = current_for(vertical)
        if annexure is not None:
            quote.terms_version_id = annexure.id
            attached.append(('annexure', annexure))

    for what, row in attached:
        audit.record('quote.terms_attached', 'quote', str(quote.id),
                     new={'kind': what, 'term_set_id': row.id,
                          'vertical': row.vertical,
                          'version': row.version},
                     actor=actor,
                     reason=(f'printed under the Procam Standard T&C '
                             f'v{row.version}' if what == 'master'
                             else f'printed under the {vertical} '
                                  f'annexure v{row.version}'))
    # The annexure, for callers that used to get it from here. None
    # when the quote already had one, exactly as before.
    return annexure


def vertical_of(quote):
    """Which vertical's terms and charge groups this quote uses."""
    from app import Lead

    lead = None
    if getattr(quote, 'lead_id', None):
        lead = db.session.get(Lead, quote.lead_id)
    return ((getattr(lead, 'procam_vertical', None) or '').strip()
            or 'Freight Forwarding')


def terms_on(quote):
    """The annexure this quote printed, not today's version."""
    from app.models.quote_terms import QuoteTermSet

    vid = getattr(quote, 'terms_version_id', None)
    if vid:
        return db.session.get(QuoteTermSet, vid)
    return current_for(vertical_of(quote))


def master_on(quote):
    """The master this quote printed, not today's version."""
    from app.models.quote_terms import QuoteTermSet

    vid = getattr(quote, 'terms_master_version_id', None)
    if vid:
        return db.session.get(QuoteTermSet, vid)
    return master_current()


# ── special conditions ───────────────────────────────────────────────
def conditions_on(quote):
    from app.models.quote_terms import QuoteSpecialCondition

    return (QuoteSpecialCondition.query
            .filter_by(quote_id=quote.id)
            .order_by(QuoteSpecialCondition.seq.asc()).all())


def add_condition(quote, text, *, actor=None):
    from app.models.quote_terms import QuoteSpecialCondition
    from app.services import audit

    text = (text or '').strip()
    if not text:
        raise TermsError('An empty condition says nothing; write it '
                         'or leave it out.')
    rows = conditions_on(quote)
    row = QuoteSpecialCondition(
        quote_id=quote.id, seq=(rows[-1].seq + 1) if rows else 1,
        text=text, created_by=actor)
    db.session.add(row)
    db.session.flush()
    audit.record('quote.condition_added', 'quote', str(quote.id),
                 new={'seq': row.seq, 'text': text[:400]},
                 actor=actor,
                 reason='a condition that applies to this job only')
    return row


def remove_condition(row, *, actor=None):
    from app.services import audit

    audit.record('quote.condition_removed', 'quote',
                 str(row.quote_id),
                 old={'seq': row.seq, 'text': (row.text or '')[:400]},
                 actor=actor, reason='withdrawn before issue')
    db.session.delete(row)


# ── clauses, and what overrides what ────────────────────────────────
#: A clause is "Label: wording". The label is what an SCC names when
#: it overrides the clause, so it is part of the contract's structure
#: and not formatting: `Payment: 30 days from the invoice date` is
#: overridden by an SCC that begins `Payment:`.
#:
#: Prose with no label is still a clause. It simply cannot be
#: overridden by name, which is the honest outcome — there is nothing
#: for an SCC to point at.
_LABEL_MAX = 40


def clause_key(text):
    """The label an SCC would have to name to override this clause.

    None when the line carries no label. Deliberately strict, because
    the cost of being loose is an SCC silently deleting a clause it
    never meant to touch. A label is the words before the first
    colon, and it has to look like a heading rather than the start of
    a sentence: at most four words, at most 40 characters, no
    sentence punctuation inside it.

    Those rules are a filter, not a proof. "Note: ..." keys as
    `note`, and would be overridden by an SCC beginning "Note:" —
    which is the right outcome if the master has a Note clause, and a
    harmless one if it does not, since an SCC whose label matches
    nothing is printed as an extra condition rather than dropped.
    """
    raw = (text or '').strip()
    raw = re.sub(r'^\s*\(?\d{1,2}[.)]\s*', '', raw)
    if ':' not in raw:
        return None
    label = raw.split(':', 1)[0].strip()
    if not label or len(label) > _LABEL_MAX:
        return None
    if any(ch in label for ch in '.;,'):
        return None
    words = label.split()
    if not words or len(words) > 4:
        return None
    return ' '.join(w.lower() for w in words)


def clauses(body):
    """[{'key', 'label', 'text'}] for a term set's body."""
    out = []
    for raw in (body or '').splitlines():
        line = raw.strip()
        if not line:
            continue
        key = clause_key(line)
        label = (line.split(':', 1)[0].strip() if key else '')
        out.append({'key': key, 'label': label, 'text': line})
    return out


# ── the client's standing conditions ────────────────────────────────
def standing_for(account_id):
    from app.models.quote_terms import AccountStandingCondition

    if not account_id:
        return []
    return (AccountStandingCondition.query
            .filter_by(account_id=account_id)
            .filter(AccountStandingCondition.ended_at.is_(None))
            .order_by(AccountStandingCondition.seq.asc()).all())


def add_standing(account_id, text, *, source=None, actor=None):
    from app.models.quote_terms import AccountStandingCondition
    from app.services import audit

    text = (text or '').strip()
    if not text:
        raise TermsError('An empty condition says nothing; write it '
                         'or leave it out.')
    if not account_id:
        raise TermsError('A standing condition belongs to a client.')
    rows = standing_for(account_id)
    row = AccountStandingCondition(
        account_id=account_id, seq=(rows[-1].seq + 1) if rows else 1,
        text=text, source=(source or '')[:300] or None,
        created_by=actor)
    db.session.add(row)
    db.session.flush()
    audit.record('account.standing_condition_added', 'company',
                 str(account_id),
                 new={'seq': row.seq, 'text': text[:400]},
                 actor=actor,
                 reason='applies to every quote for this client')
    return row


def end_standing(row, *, actor=None):
    from datetime import datetime as _dt

    from app.services import audit

    row.ended_at, row.ended_by = _dt.utcnow(), actor
    audit.record('account.standing_condition_ended', 'company',
                 str(row.account_id),
                 old={'seq': row.seq, 'text': (row.text or '')[:400]},
                 actor=actor, reason='no longer agreed')
    return row


def conditions_for(quote):
    """Every condition on this quote: the client's, then this job's.

    The client's first because they are the older agreement, and a
    condition typed on one quote is the more recent decision — so
    where both name the same clause, the quote's wins. `overrides`
    below settles that; this only fixes the order.
    """
    account_id = getattr(quote, 'account_id', None)
    out = [{'scope': 'client', 'seq': c.seq, 'text': c.text,
            'key': clause_key(c.text), 'id': c.id}
           for c in standing_for(account_id)]
    out += [{'scope': 'quote', 'seq': c.seq, 'text': c.text,
             'key': clause_key(c.text), 'id': c.id}
            for c in conditions_on(quote)]
    return out


# ── the client's or the tender's own document ───────────────────────
def document_number(quote):
    """What an uploaded T&C is filed against: the QUO or RFQ number.

    The number, not the row id, so the file follows every revision
    (Module 1.3). A revision re-prices; it does not change what the
    client sent us.
    """
    number = (getattr(quote, 'quote_number', '') or '').strip()
    if number:
        return number
    rfq = getattr(quote, 'rfq', None)
    return ((getattr(rfq, 'rfq_number', '') or '').strip() or '')


def documents_on(quote):
    from app.models.quote_terms import QuoteTermsDocument

    number = document_number(quote)
    if not number:
        return []
    return (QuoteTermsDocument.query
            .filter_by(doc_number=number)
            .filter(QuoteTermsDocument.withdrawn_at.is_(None))
            .order_by(QuoteTermsDocument.id.asc()).all())


def attach_document(quote, *, filename, stored_path, content_type,
                    mode, note=None, actor=None):
    """File the client's or the tender's own T&C against the number."""
    from app.models.quote_terms import QuoteTermsDocument
    from app.services import audit

    mode = (mode or '').strip().lower()
    if mode not in (QuoteTermsDocument.REPLACE, QuoteTermsDocument.ADD):
        # No default. A file dropped on a quote with nobody saying
        # which of the two it is would be printed one way and argued
        # the other.
        raise TermsError('Say whether this replaces the Procam '
                         'standard terms or is added to them.')
    number = document_number(quote)
    if not number:
        raise TermsError('This quote has no number yet; save it '
                         'first, then attach the terms.')
    row = QuoteTermsDocument(
        doc_number=number, quote_id=getattr(quote, 'id', None),
        filename=(filename or '')[:240],
        stored_path=stored_path, content_type=(content_type or '')[:120],
        mode=mode, note=(note or '')[:300] or None, uploaded_by=actor)
    db.session.add(row)
    db.session.flush()
    audit.record('quote.terms_document_attached', 'quote',
                 str(getattr(quote, 'id', '') or number),
                 new={'doc_number': number, 'filename': row.filename,
                      'mode': mode},
                 actor=actor,
                 reason=(f'the client\'s own terms replace the Procam '
                         f'standard on {number}' if mode == 'replace'
                         else f'the client\'s own terms are added to '
                              f'the Procam standard on {number}'))
    return row


def withdraw_document(row, *, actor=None):
    from datetime import datetime as _dt

    from app.services import audit

    row.withdrawn_at, row.withdrawn_by = _dt.utcnow(), actor
    audit.record('quote.terms_document_withdrawn', 'quote',
                 str(row.quote_id or row.doc_number),
                 old={'filename': row.filename, 'mode': row.mode},
                 actor=actor, reason='withdrawn')
    return row


# ── what the PDF prints ─────────────────────────────────────────────
def for_print(quote):
    """Everything the PDF prints under the price table.

    Never raises and never refuses (Module 1.5): a quote is not
    blocked for want of terms. Where something is absent this says so
    in `notes`, and the quote goes out with whatever there is.
    """
    from app.models.quote_terms import QuoteTermsDocument

    vertical = vertical_of(quote)
    master = master_on(quote)
    annexure = terms_on(quote)
    docs = documents_on(quote)
    conditions = conditions_for(quote)

    replaced_by = next((d for d in docs
                        if d.mode == QuoteTermsDocument.REPLACE), None)

    # Which standard clauses a condition has overridden. The quote's
    # own conditions beat the client's standing ones, which beat the
    # standard — latest decision wins, and each losing clause is
    # marked rather than dropped so the quote can show what it
    # replaced.
    named = {}
    for cond in conditions:
        if cond['key']:
            named[cond['key']] = cond        # 'quote' comes last

    def _rendered(row):
        if row is None:
            return {'title': '', 'body': '', 'version': None,
                    'clauses': [], 'missing': True}
        out = []
        for clause in clauses(row.body):
            beaten = named.get(clause['key']) if clause['key'] else None
            out.append({**clause,
                        'overridden_by': (beaten['text']
                                          if beaten else ''),
                        'overridden_scope': (beaten['scope']
                                             if beaten else '')})
        return {'title': row.title or '', 'body': row.body or '',
                'version': row.version, 'clauses': out,
                'effective_from': (str(row.effective_from)
                                   if row.effective_from else ''),
                'missing': False}

    notes = []
    if master is None:
        notes.append('No Procam Standard T&C is published. The quote '
                     'prints without it.')
    if annexure is None and vertical:
        notes.append(f'No {vertical} annexure. The master goes alone.')
    if replaced_by is not None:
        notes.append(f'{replaced_by.filename} replaces the Procam '
                     f'standard terms on this quote.')

    return {
        'vertical': vertical,
        # `standard` keeps its old name and shape: the PDF and the
        # mail both read it, and under Module 1.1 the thing every
        # quote prints is the master. The annexure is beside it.
        'standard': _rendered(None if replaced_by else master),
        'annexure': _rendered(None if replaced_by else annexure),
        'documents': [d.to_dict() for d in docs],
        'replaced': replaced_by is not None,
        'special': [{'seq': c['seq'], 'text': c['text'],
                     'scope': c['scope']} for c in conditions],
        'notes': notes,
        # Module 1.5, as a value the caller can assert on rather than
        # a promise in a docstring.
        'blocks_issue': False,
    }
