"""
The terms a quote is made under.

CRM 2.0 R5. Two things kept apart on purpose:

  standard terms (STC)   one current version per vertical, the same
                         on every quote in that vertical
  special conditions     added to one quote because that job is
  (SCC)                  unusual

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

from datetime import date, datetime

from app import db


class TermsError(ValueError):
    """Something the caller asked for cannot be stored as given."""


def current_for(vertical):
    """The version a new quote in this vertical would print."""
    from app.models.quote_terms import QuoteTermSet

    return (QuoteTermSet.query
            .filter_by(vertical=(vertical or '').strip(),
                       is_current=True)
            .order_by(QuoteTermSet.version.desc()).first())


def history_for(vertical):
    from app.models.quote_terms import QuoteTermSet

    return (QuoteTermSet.query
            .filter_by(vertical=(vertical or '').strip())
            .order_by(QuoteTermSet.version.desc()).all())


def publish(vertical, body, *, title=None, actor=None,
            effective_from=None):
    """A new current version. The old one is superseded, not deleted."""
    from app.models.quote_terms import QuoteTermSet
    from app.services import audit

    vertical = (vertical or '').strip()
    if not vertical:
        raise TermsError('Terms belong to a vertical; name one.')
    body = (body or '').strip()
    if not body:
        raise TermsError('Empty terms are not terms.')

    was = current_for(vertical)
    version = (was.version + 1) if was else 1
    row = QuoteTermSet(
        vertical=vertical, version=version,
        title=(title or 'Standard Terms and Conditions')[:200],
        body=body, is_current=True,
        effective_from=effective_from or date.today(),
        created_by=actor)
    db.session.add(row)
    db.session.flush()

    if was is not None:
        was.is_current = False
        was.superseded_at = datetime.utcnow()
        was.superseded_by_id = row.id

    audit.record('quote_terms.published', 'quote_term_set',
                 str(row.id),
                 old={'version': was.version if was else None},
                 new={'vertical': vertical, 'version': version},
                 actor=actor,
                 reason=f'version {version} of the {vertical} standard '
                        f'terms; version {was.version} superseded'
                        if was else
                        f'first version of the {vertical} standard terms')
    return row


def attach_to(quote, *, actor=None):
    """Stamp the current version onto a quote.

    Called when the quote is built and again if it is re-issued. A
    quote that has already printed keeps the version it printed —
    `quote.terms_version_id` is set once and not moved, because the
    client has the old wording in their hand.
    """
    from app.services import audit

    if getattr(quote, 'terms_version_id', None):
        return None
    vertical = vertical_of(quote)
    row = current_for(vertical)
    if row is None:
        return None
    quote.terms_version_id = row.id
    audit.record('quote.terms_attached', 'quote', str(quote.id),
                 new={'terms_version_id': row.id,
                      'vertical': vertical, 'version': row.version},
                 actor=actor,
                 reason=f'printed under {vertical} standard terms '
                        f'v{row.version}')
    return row


def vertical_of(quote):
    """Which vertical's terms and charge groups this quote uses."""
    from app import Lead

    lead = None
    if getattr(quote, 'lead_id', None):
        lead = db.session.get(Lead, quote.lead_id)
    return ((getattr(lead, 'procam_vertical', None) or '').strip()
            or 'Freight Forwarding')


def terms_on(quote):
    """The exact wording this quote printed, not today's version."""
    from app.models.quote_terms import QuoteTermSet

    vid = getattr(quote, 'terms_version_id', None)
    if vid:
        return db.session.get(QuoteTermSet, vid)
    return current_for(vertical_of(quote))


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


def for_print(quote):
    """Everything the PDF prints under the price table."""
    terms = terms_on(quote)
    return {
        'vertical': vertical_of(quote),
        'standard': {
            'title': terms.title if terms else '',
            'body': terms.body if terms else '',
            'version': terms.version if terms else None,
            'missing': terms is None,
        },
        'special': [{'seq': c.seq, 'text': c.text}
                    for c in conditions_on(quote)],
    }
