"""
Which quotation a mail is, and what to do when we cannot tell.

CRM-QUOTE-REV-02 §5.4.

Three rules in order, surest first:

    the quotation number and its revision, which only we issue;
    the RFQ number, which is also ours;
    the thread headers, which are the mail system's own idea of
    what belongs together.

Only a verified match closes step 8. Everything else goes in the
queue, because attaching a quotation to the wrong lead changes
that lead's stage, its amount and its reported pipeline, and
nobody looks again until the figures are wrong.

A suggestion is recorded with the unmatched row — the lead from
the same customer domain — so the person deciding starts from
what the CRM knows rather than from a subject line.
"""
from __future__ import annotations

import re

#: QUO-2026-0045, with or without a revision.
RX_QUOTE = re.compile(
    r'\b(QUO[-/ ]?\d{4}[-/ ]?\d{3,6})\b', re.I)
RX_REV = re.compile(r'\bRev[-\s]?(\d{1,2})\b', re.I)
RX_RFQ = re.compile(r'\b(RFQ[-/ ]?\d{4}[-/ ]?\d{3,6})\b', re.I)


def _norm_ref(raw):
    """"quo/2026/0045" -> "QUO-2026-0045"."""
    bits = re.split(r'[-/ ]', (raw or '').strip())
    bits = [b for b in bits if b]
    if len(bits) != 3:
        return (raw or '').strip().upper()
    return f'{bits[0].upper()}-{bits[1]}-{bits[2]}'


def refs_in(text):
    """(quote_number, rev_no, rfq_number) — any of them None."""
    blob = text or ''
    quote = RX_QUOTE.search(blob)
    rfq = RX_RFQ.search(blob)
    rev = RX_REV.search(blob)
    return (_norm_ref(quote.group(1)) if quote else None,
            int(rev.group(1)) if rev else None,
            _norm_ref(rfq.group(1)) if rfq else None)


def match(subject='', body='', *, headers=None):
    """(quote, lead, how) or (None, None, why-it-is-unmatched)."""
    from app import db
    from app.models.quote import Quote
    from app.models.rfq import RFQ

    blob = f'{subject or ""}\n{(body or "")[:4000]}'
    quote_no, rev_no, rfq_no = refs_in(blob)

    # 1. the quotation number, and the revision when it is given
    if quote_no:
        # The number a customer quotes back is the root's, and a
        # revision carries its own internal number — so this
        # finds the root, and the revision below picks the row.
        rows = Quote.query.filter(
            db.func.upper(Quote.quote_number) == quote_no).all()
        if rows:
            quote = rows[0]
            if rev_no is not None:
                from app.services import quote_rev
                for row in quote_rev.chain(quote):
                    if (row.rev_no if row.rev_no is not None
                            else 0) == rev_no:
                        quote = row
                        break
            return quote, _lead_of(quote), (
                f'quotation {quote_no}'
                + (f' Rev-{rev_no:02d}' if rev_no is not None else ''))

    # 2. the RFQ number
    if rfq_no:
        rfq = RFQ.query.filter(
            db.func.upper(RFQ.rfq_number) == rfq_no).first()
        if rfq is not None:
            quote = (Quote.query.filter_by(rfq_id=rfq.id)
                     .order_by(Quote.id.desc()).first())
            lead = None
            if rfq.lead_id:
                from app import Lead
                lead = db.session.get(Lead, rfq.lead_id)
            return quote, lead, f'RFQ {rfq_no}'

    # 3. the thread, which the mail system already grouped
    thread = _by_thread(headers or {})
    if thread is not None:
        return None, thread, 'the mail thread'

    return None, None, _why_not(quote_no, rfq_no)


def _why_not(quote_no, rfq_no):
    if quote_no:
        return f'{quote_no} is not a quotation this CRM issued'
    if rfq_no:
        return f'{rfq_no} is not an RFQ this CRM issued'
    return 'no quotation or RFQ number, and no thread we hold'


def _lead_of(quote):
    from app import Lead, db
    if quote is None or not quote.lead_id:
        return None
    return db.session.get(Lead, quote.lead_id)


def _by_thread(headers):
    from app import Lead, LeadEmail, db

    keys = [headers.get('conversation_id'),
            headers.get('in_reply_to')]
    keys += [k for k in (headers.get('references') or '').split()
             if k]
    for key in [k for k in keys if k]:
        row = (LeadEmail.query
               .filter(db.or_(LeadEmail.conversation_id == key,
                              LeadEmail.message_id == key))
               .order_by(LeadEmail.id.desc()).first())
        if row is not None and row.lead_id:
            return db.session.get(Lead, row.lead_id)
    return None


def suggest_lead(from_addr, to_addr=''):
    """A lead from the same customer domain, for the queue row."""
    from app import Lead, db
    from app.services import account_match, sales_rules

    for addr in (a for a in [from_addr] + (to_addr or '').split(',')
                 if a and a.strip()):
        account, _rule = account_match.resolve(addr.strip(),
                                               count_hit=False)
        if account is None or account_match.not_a_customer(account):
            continue
        lead = (Lead.query
                .filter(Lead.company_id == account.id,
                        Lead.is_archived.isnot(True),
                        Lead.stage.in_(sales_rules.open_stages()))
                .order_by(Lead.updated_at.desc()).first())
        if lead is not None:
            return lead, account
        return None, account
    return None, None


def park(*, message_id, subject, from_addr, to_addr='', body='',
         sent_at=None, headers=None, why=''):
    """Put an unplaceable quotation mail in the queue. Idempotent."""
    from app import db
    from app.models.quote import UnmatchedQuoteMail

    existing = UnmatchedQuoteMail.query.filter_by(
        message_id=message_id).first()
    if existing is not None:
        return existing

    lead, account = suggest_lead(from_addr, to_addr)
    row = UnmatchedQuoteMail(
        message_id=message_id,
        conversation_id=(headers or {}).get('conversation_id'),
        subject=(subject or '')[:500], from_addr=(from_addr or '')[:320],
        to_addr=(to_addr or '')[:600], sent_at=sent_at,
        body_excerpt=(body or '')[:2000],
        suggested_lead_id=getattr(lead, 'id', None),
        suggested_account_id=getattr(account, 'id', None),
        why=(why or '')[:300])
    db.session.add(row)
    return row


def on_quote_mail(*, message_id, subject, from_addr, to_addr='',
                  body='', sent_at=None, headers=None, actor='system'):
    """The whole §5.4 decision for one mail. (outcome, detail).

    'closed' — matched and step 8 closed;
    'matched' — matched, but there was no open step 8 to close;
    'parked' — in the queue for a person.
    """
    from app import db
    from app.services import rfq_chain

    quote, lead, how = match(subject=subject, body=body,
                             headers=headers)
    if quote is None and lead is None:
        park(message_id=message_id, subject=subject,
             from_addr=from_addr, to_addr=to_addr, body=body,
             sent_at=sent_at, headers=headers, why=how)
        return 'parked', how

    if quote is None:
        # A thread match places the mail but not the quotation, and
        # step 8 asks which quotation went out. That is a question
        # for a person, so it is parked with the lead suggested.
        park(message_id=message_id, subject=subject,
             from_addr=from_addr, to_addr=to_addr, body=body,
             sent_at=sent_at, headers=headers,
             why=f'matched {how}, but no quotation number — '
                 f'which revision went out?')
        return 'parked', how

    ok, detail = rfq_chain.on_quote_sent(
        quote, actor=actor,
        evidence={'matched_by': how, 'mail_reference': message_id,
                  'recipient': to_addr, 'sent_by': actor})
    db.session.flush()
    return ('closed' if ok else 'matched'), {'how': how,
                                             'quote_id': quote.id,
                                             'detail': detail}
