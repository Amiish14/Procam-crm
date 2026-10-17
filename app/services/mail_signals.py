"""
What an inbound mail is telling you about a lead.

Three questions the ingest could not previously answer, all of which
cost somebody something in September:

    is this shipment activity?   Pre-alerts arrived for lead #11807 on
                                 12 October. The job was live. The
                                 lead still said Lost.
    which stage was it before?   A reopened lead was pushed to a fixed
                                 stage, losing where it actually was.
    is this a forwarded quote?   Our own offer, forwarded back by a
                                 colleague, matched no rule at all and
                                 was dropped as a reply.

Keywords, not a model. Each list is short, specific to freight, and
readable by the person who will be asked why a mail was classified
the way it was.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

#: Stages that mean the lead is closed as far as the pipeline cares.
CLOSED_STAGES = ('Lost', 'Not Interested', 'On Hold', 'Won')

#: A document that only exists once a shipment is actually moving.
#: Seeing one of these on a lead that is not Won is the strongest
#: signal the CRM gets that the stage is wrong.
SHIPMENT_PATTERNS = (
    r'\bpre[\s-]?alert\b', r'\bHBL\b', r'\bMBL\b', r'\bB\s*/\s*L\b',
    r'\bbill of lading\b', r'\bbooking confirmation\b',
    r'\bshipping order\b', r'\bdelivery order\b',
    r'\bcontainer (?:no|number)\b', r'\bvessel\b.*\bETA\b',
    r'\bpurchase order\b', r'\bP\.?O\.? no\b',
)

#: A quote going out, or a client answering one.
QUOTE_PATTERNS = (
    r'\boffer\b', r'\bquotation\b', r'\bquote\b', r'\brevised offer\b',
    r'\bour (?:best )?rates?\b', r'\bproposal\b',
)

#: Procam's own domains. A message FROM one of these TO an outsider,
#: inside a forwarded chain, is us quoting somebody.
OUR_DOMAINS = ('procamgroup.in', 'procamlogistics.com')

#: Vertical hints for a lead created from a forwarded quote, where
#: there is nothing else to go on. Order matters: the first match
#: wins, and customs beats sea because a customs job usually mentions
#: both.
VERTICAL_HINTS = (
    ('Customs', (r'\bcustoms?\b', r'\bclearance\b', r'\bbill of entry\b',
                 r'\bBOE\b', r'\bICEGATE\b')),
    ('Air Freight', (r'\bair\s?freight\b', r'\bAWB\b', r'\bairway ?bill\b')),
    ('Sea Freight', (r'\bDAP\b', r'\bFCL\b', r'\bLCL\b', r'\bCBM\b',
                     r'\bsea\s?freight\b', r'\bnhava sheva\b',
                     r'\bport of loading\b')),
    ('Heavy Transport', (r'\bODC\b', r'\btrailer\b', r'\blow ?bed\b',
                         r'\bcrane\b', r'\bSPMT\b', r'\bhydraulic axle\b')),
    ('Warehousing', (r'\bwarehous', r'\bgodown\b', r'\bpallet')),
)


def _blob(*parts):
    return ' \n '.join(str(p) for p in parts if p)[:20000]


def _any(patterns, text):
    return any(re.search(p, text, re.I) for p in patterns)


def _matched(patterns, text):
    return [p for p in patterns if re.search(p, text, re.I)]


# ── shipment activity ────────────────────────────────────────────────
def looks_like_shipment(subject='', body='', filenames=()):
    """Is this mail about a shipment that is already moving?"""
    text = _blob(subject, body, ' '.join(filenames or ()))
    return _any(SHIPMENT_PATTERNS, text)


def shipment_reason(subject='', body='', filenames=()):
    """The words that said so, for the banner and the audit trail."""
    text = _blob(subject, body, ' '.join(filenames or ()))
    hits = _matched(SHIPMENT_PATTERNS, text)
    pretty = [h.replace(r'\b', '').replace(r'\s*', ' ').replace('\\', '')
              for h in hits[:3]]
    return ', '.join(pretty)


# ── which stage to restore ───────────────────────────────────────────
def last_open_stage(lead, fallback=''):
    """The stage this lead was in before somebody closed it.

    Read from the stage history, newest backwards, for the first
    stage that is not a closed one. A reopened lead belongs where it
    was, not in whatever stage the configuration nominates — a deal
    that was in Under Negotiation when it was lost is not a fresh
    RFQ, and treating it as one loses the pipeline position and the
    forecast with it.

    Returns `fallback` when the history says nothing, which is the
    behaviour that existed before.
    """
    try:
        from app import LeadStageHistory
        rows = (LeadStageHistory.query
                .filter(LeadStageHistory.lead_id == lead.id)
                .order_by(LeadStageHistory.changed_at.desc())
                .limit(40).all())
    except Exception:
        try:
            from app import db
            db.session.rollback()
        except Exception:
            pass
        return fallback

    for row in rows:
        for stage in (row.from_stage, row.to_stage):
            stage = (stage or '').strip()
            if stage and stage not in CLOSED_STAGES:
                return stage
    return fallback


# ── forwarded quotes and client replies ──────────────────────────────
def is_internal(address):
    addr = (address or '').strip().lower()
    if '<' in addr and '>' in addr:
        addr = addr[addr.rfind('<') + 1:addr.rfind('>')].strip()
    if '@' not in addr:
        return False
    domain = addr.rsplit('@', 1)[1]
    return any(domain == d or domain.endswith('.' + d) for d in OUR_DOMAINS)


def looks_like_quote(subject='', body='', filenames=(), chain_froms=()):
    """Is this a Procam offer, or a client answering one?

    Three independent signals, any one of which is enough:

      * somewhere in the forwarded chain, a Procam address wrote to
        somebody outside — that is us quoting;
      * the subject says offer, quote or revised offer;
      * an attachment is named offer or quote.

    The BrahMos mail had all three and was dropped as a reply,
    because the top message was the client's two-word answer and
    nothing looked below it.
    """
    text = _blob(subject, body)
    if _any(QUOTE_PATTERNS, text):
        return True
    for name in (filenames or ()):
        if re.search(r'\b(offer|quote|quotation)\b', str(name), re.I):
            return True
    return any(is_internal(a) for a in (chain_froms or ()))


def quote_signals(subject='', body='', filenames=(), chain_froms=()):
    """Why it was classified that way, in a sentence."""
    reasons = []
    text = _blob(subject, body)
    if _any(QUOTE_PATTERNS, text):
        reasons.append('the subject or body mentions an offer or quote')
    if any(re.search(r'\b(offer|quote|quotation)\b', str(n), re.I)
           for n in (filenames or ())):
        reasons.append('an attachment is named offer or quote')
    inside = [a for a in (chain_froms or ()) if is_internal(a)]
    if inside:
        reasons.append(f'the chain contains a message from {inside[0]}')
    return '; '.join(reasons)


def vertical_for(subject='', body='', filenames=()):
    """A vertical for a lead created from a forwarded quote, or ''."""
    text = _blob(subject, body, ' '.join(filenames or ()))
    for vertical, patterns in VERTICAL_HINTS:
        if _any(patterns, text):
            return vertical
    return ''


def external_parties(addresses):
    """The addresses in a chain that are not ours.

    The contact on a lead created from a forwarded quote is the
    client, never the colleague who forwarded it — a lead whose
    contact is your own sales manager is worse than no contact.
    """
    out = []
    for raw in (addresses or []):
        addr = (raw or '').strip()
        if '<' in addr and '>' in addr:
            addr = addr[addr.rfind('<') + 1:addr.rfind('>')].strip()
        if '@' not in addr or is_internal(addr):
            continue
        if addr.lower() not in [a.lower() for a in out]:
            out.append(addr)
    return out
