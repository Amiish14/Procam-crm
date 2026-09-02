"""Pure regex/heuristic parser for Microsoft Graph message dicts → Lead payloads.

No DB, no network, no side effects — trivial to unit test in isolation.

Public surface:
    extract_lead(msg) -> dict | None    # main entry
"""
from __future__ import annotations

import os
import re
from html.parser import HTMLParser
from typing import Optional, Tuple, List

# ─── Constants ─────────────────────────────────────────────────────────
PERSONAL_DOMAINS = {
    "gmail.com", "yahoo.com", "yahoo.co.in", "hotmail.com", "outlook.com",
    "live.com", "rediffmail.com", "aol.com", "icloud.com", "protonmail.com",
    "yahoo.in",
}

# Common Indian cities + states. Case-insensitive comparison used.
INDIAN_PLACES = {
    # Metros / major cities
    "mumbai", "delhi", "new delhi", "kolkata", "chennai", "bangalore",
    "bengaluru", "hyderabad", "pune", "ahmedabad", "surat", "jaipur",
    "lucknow", "kanpur", "nagpur", "indore", "thane", "bhopal",
    "visakhapatnam", "vizag", "patna", "vadodara", "ludhiana", "agra",
    "nashik", "faridabad", "meerut", "rajkot", "kalyan", "varanasi",
    "srinagar", "aurangabad", "dhanbad", "amritsar", "navi mumbai",
    "allahabad", "prayagraj", "ranchi", "howrah", "coimbatore", "jabalpur",
    "gwalior", "vijayawada", "jodhpur", "madurai", "raipur", "kota",
    "chandigarh", "guwahati", "solapur", "mysore", "mysuru", "gurgaon",
    "gurugram", "noida", "greater noida", "ghaziabad", "kochi", "cochin",
    "trivandrum", "thiruvananthapuram", "mangalore", "mangaluru", "goa",
    "panaji", "haldia", "kandla", "mundra", "paradip", "tuticorin",
    "ennore", "jnpt", "dahej", "hazira", "pipavav", "kattupalli",
    "krishnapatnam", "gopalpur", "sikka", "porbandar", "okha",
    "jamnagar", "bhubaneswar", "cuttack", "silvassa", "daman",
    # States / UTs
    "gujarat", "maharashtra", "karnataka", "tamil nadu", "kerala",
    "andhra pradesh", "telangana", "odisha", "orissa", "west bengal",
    "bihar", "jharkhand", "chhattisgarh", "madhya pradesh", "rajasthan",
    "uttar pradesh", "uttarakhand", "punjab", "haryana", "himachal pradesh",
    "jammu and kashmir", "ladakh", "assam", "arunachal pradesh",
    "manipur", "meghalaya", "mizoram", "nagaland", "sikkim", "tripura",
    "goa", "delhi ncr", "ncr",
}

CARGO_KEYWORDS = [
    "ODC", "over-dimensional", "over dimensional", "project cargo",
    "heavy lift", "container", "containers", "MT", "tons", "tonnes",
    "truck", "trailer", "hydraulic", "freight", "transport", "shipment",
    "consignment", "cargo", "load",
]

RFQ_KEYWORDS = [
    "rfq", "quote", "quotation", "rate", "pricing", "requirement",
    "enquiry", "inquiry", "tender",
]

URGENCY_HIGH = ["urgent", "asap", "immediate", "priority"]
URGENCY_MED = ["soon", "quickly", "expedite"]

_PHONE_RE = re.compile(r"\b(?:\+?91[-\s]?|0)?([6-9]\d{9})\b")

_NOREPLY_RE = re.compile(r"no-?reply|donotreply|noreply|mailer-daemon|postmaster", re.I)

# Newsletter/marketing local-parts — treat as bulk regardless of subject line.
_BULK_LOCAL_RE = re.compile(
    r"^(newsletter|newsletters|updates|alerts|notifications?|marketing|news|"
    r"digest|events?|announce|announcements?|campaign|mailer|do[-_]?not[-_]?reply|"
    r"broadcasts?|bulk|promo|promotions?)(\d*|_.*|\..*)?$",
    re.I,
)

# Sender domains that never produce logistics leads. Anything ending in
# these gets skipped. Categorised for maintenance clarity.
_BULK_DOMAIN_SUFFIXES = (
    # ── Email marketing / aggregator infra ──
    "substack.com", "mailchimpapp.com", "mailchimp.com",
    "em.linkedin.com", "linkedin.com",
    "sendgrid.net", "mailgun.org", "amazonses.com",
    "constantcontact.com", "hubspotemail.net", "mktomail.com",
    "eloqua.com", "marketo.com", "salesforce.com",
    "notifications.google.com", "accounts.google.com",
    "microsoftonline.com", "office.com", "sharepointonline.com",

    # ── Banks + payment / statement notifications ──
    "bank.in", "hdfc.bank.in", "hdfcbank.bank.in", "sbi.bank.in",
    "alerts.sbi.bank.in", "icici.bank.in", "axis.bank.in",
    "kotak.bank.in", "kotakalert.bank.in", "mail.kotakalert.bank.in",
    "deutsche.bank.in", "idfcfirst.bank.in", "emailer.idfcfirst.bank.in",
    "sbicard.com", "americanexpress.com", "hdfcergo.com", "onlinesbi.com",
    "paytm.com", "razorpay.com", "phonepe.com",

    # ── Travel & hospitality (irrelevant) ──
    "makemytrip.com", "booking.com", "property.booking.com", "cleartrip.com",
    "agoda.com", "goibibo.com", "oyorooms.com", "expedia.com",
    "airbnb.com", "trip.com", "ixigo.com", "yatra.com", "myvacationaffair.in",

    # ── Job boards / HR consulting / staffing spam ──
    "naukri.com", "workindia.in", "hirect.com", "instahyre.com",
    "indeed.com", "monster.com", "timesjobs.com", "foundit.in",
    "apna.co", "mafoistrategy.com", "hr.mafoistrategy.com",
    "silveroakhealth.com", "workforcemanagerhub.com", "splendin.com",
    "workflowbizpro.com", "nistglobal.com", "infiflex.com",
    "inventiconnect.com", "sspu.ac.in",

    # ── News / newsletters / research feeds ──
    "economist.com", "b.economist.com", "moodys.com",
    "economictimesnews.com", "ettech.com", "timesofindianewsletters.in",
    "hindustantimes.com", "nbmcw.in", "pv-magazine.com", "saurenergy.com",
    "breakbulk.news", "freightweek.org", "joc.com", "heavyliftpfi.com",
    "projectcargonetwork.com", "railanalysisindia.com", "metrorailnews.in",
    "hktdc.com", "core-shipping.com", "projectstoday.net",
    "logisyn.com", "supplychaincatalyst", "vccircle.co", "ibef.org",
    "sagarsandesh.in", "sagarsandesh01", "sagarsandesh04",
    "shisl.com", "trstexpo.com", "cwiemeevents.com",
    "combinedlogisticsnetworks.com", "forwarderfocusdirectory.com",
    "ffconnex1.com", "conference.cii.in", "messages.cii.in",
    "email.engage.here.com", "engage.here.com",
    "comms.dell.com", "emails.woodmac.com", "email.lawrbit.com",
    "events.trstexpo.com", "events.messefrankfurtindia.in",
    "mailers.hdfcbank.bank.in", "read.directtoconsumer.co",
    "newsletter.theneurondaily.com", "updates.combinedlogisticsnetworks.com",
    "mnmreports.com", "metalogicpms.com",
    "quarantine@messaging.microsoft.com", "messaging.microsoft.com",
    "conference.cii.in", "messages.cii.in", "ficci.com",
    "assocham.co.in", "careedge.in", "powerlineonline.in",
    "indiashippingnews.com", "royalmedia.com", "tracxn.com",
    "immensitylogistics.com", "ezeeshipping.com",

    # ── Government / tender aggregators / random alerts ──
    "nic.in", "gov.in", "etender-nic@nic.in",
    "tenderwizardhelpdesk1.in", "hal-india.co.in",
    "procuretiger.com", "procuretigers.com", "gem.gov.in",
    "odexservices.com", "one-line.com", "cma-cgm.com",
    "customer.cmacgm-group.com", "emailer.idfcfirst.bank.in",

    # ── Random consulting / SaaS pings / spam ──
    "mismosystems.com", "aparajitha.com", "hansinfomatic.in",
    "silveroakhealth.com", "giftveda.in", "kecrpg.com",
    "hansinfomatic.in", "ptandco.com", "info.thenavalarch.com",
    "129122150.mailchimpapp.com",
)

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

_INVOICE_RE = re.compile(r"\b(invoice|payment reminder|statement|receipt)\b", re.I)
_BOUNCE_RE = re.compile(r"undeliver|mail delivery|returned mail|failure notice", re.I)

_AUTOREPLY_SUBJECT_MARKERS = [
    "out of office", "automatic reply", "auto-reply", "auto reply",
    "delivery status notification",
]

_NEWSLETTER_PREFIXES = ["[newsletter]", "newsletter:"]

# City-pair patterns
_FROM_TO_RE = re.compile(
    r"\bfrom\s+([A-Za-z][A-Za-z\s]{1,40}?)\s+to\s+([A-Za-z][A-Za-z\s]{1,40}?)"
    r"(?=[.,;\n\r\?\!]|$)",
    re.I,
)
_ORIGIN_RE = re.compile(r"origin\s*[:\-]\s*([A-Za-z][A-Za-z\s]{1,40}?)(?=[.,;\n\r\?\!]|$)", re.I)
_DESTINATION_RE = re.compile(
    r"destination\s*[:\-]\s*([A-Za-z][A-Za-z\s]{1,40}?)(?=[.,;\n\r\?\!]|$)",
    re.I,
)


# ─── HTML → text (stdlib only) ─────────────────────────────────────────
class _HTMLStripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: List[str] = []
        self._skip = 0  # nested count of script/style tags

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("br", "p", "div", "tr", "li"):
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip > 0:
            self._skip -= 1
        elif tag in ("p", "div", "tr", "li"):
            self._parts.append("\n")

    def handle_data(self, data):
        if self._skip == 0:
            self._parts.append(data)

    def get_text(self) -> str:
        return "".join(self._parts)


def _html_to_text(html: str) -> str:
    """Strip HTML tags / scripts / styles and collapse whitespace. stdlib only."""
    if not html:
        return ""
    try:
        p = _HTMLStripper()
        p.feed(html)
        p.close()
        text = p.get_text()
    except Exception:
        # Fall back to a crude regex strip if the parser chokes
        text = re.sub(r"<[^>]+>", " ", html)
    # Collapse whitespace
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ─── Field extractors ──────────────────────────────────────────────────
def _extract_phone(text: str) -> Optional[str]:
    if not text:
        return None
    m = _PHONE_RE.search(text)
    if not m:
        return None
    return "+91-" + m.group(1)


def _match_place(candidate: str) -> Optional[str]:
    """Return a normalized place name if candidate matches an Indian city/state."""
    if not candidate:
        return None
    c = candidate.strip().lower()
    # Trim trailing filler words
    for suffix in (" is", " for", " and", " on"):
        if c.endswith(suffix):
            c = c[: -len(suffix)].strip()
    if c in INDIAN_PLACES:
        return c.title()
    # Try progressively shorter prefixes for phrases like "Mumbai port"
    tokens = c.split()
    for n in range(min(len(tokens), 3), 0, -1):
        head = " ".join(tokens[:n])
        if head in INDIAN_PLACES:
            return head.title()
    return None


def _extract_origin_destination(text: str) -> Tuple[Optional[str], Optional[str]]:
    if not text:
        return (None, None)

    origin: Optional[str] = None
    destination: Optional[str] = None

    m = _FROM_TO_RE.search(text)
    if m:
        origin = _match_place(m.group(1))
        destination = _match_place(m.group(2))

    if not origin:
        m2 = _ORIGIN_RE.search(text)
        if m2:
            origin = _match_place(m2.group(1))

    if not destination:
        m3 = _DESTINATION_RE.search(text)
        if m3:
            destination = _match_place(m3.group(1))

    return (origin, destination)


def _extract_cargo_keywords(text: str) -> List[str]:
    if not text:
        return []
    lower = text.lower()
    found: List[str] = []
    seen = set()
    for kw in CARGO_KEYWORDS:
        kw_l = kw.lower()
        # Word-boundary match for short tokens (MT), substring OK for phrases
        if len(kw_l) <= 3:
            pattern = r"\b" + re.escape(kw_l) + r"\b"
            if re.search(pattern, lower) and kw_l not in seen:
                found.append(kw)
                seen.add(kw_l)
        else:
            if kw_l in lower and kw_l not in seen:
                found.append(kw)
                seen.add(kw_l)
    return found


def _is_rfq(subject: str, text: str) -> bool:
    hay = ((subject or "") + " " + (text or "")).lower()
    return any(re.search(r"\b" + re.escape(kw) + r"\b", hay) for kw in RFQ_KEYWORDS)


def _urgency(subject: str, text: str) -> str:
    hay = ((subject or "") + " " + (text or "")).lower()
    if any(w in hay for w in URGENCY_HIGH):
        return "high"
    if any(w in hay for w in URGENCY_MED):
        return "medium"
    return "low"


# Subdomains that bulk/transactional mail is sent from. They are never the
# company name — "email.mckinsey.com" is McKinsey, not "Email".
_SENDING_SUBDOMAINS = {
    "mail", "email", "emails", "e", "em", "mailer", "mailers", "smtp",
    "mx", "news", "newsletter", "newsletters", "info", "reply", "replies",
    "noreply", "no-reply", "notify", "notifications", "alerts", "updates",
    "marketing", "campaign", "campaigns", "go", "click", "clicks", "links",
    "link", "track", "tracking", "send", "sender", "sg", "mkt", "cp",
    "comms", "connect", "engage", "messages", "messaging", "events",
    "read", "t", "m", "n", "u", "s", "www",
}

# Second-level labels that are part of a public suffix rather than a name:
# savas.co.in, example.ac.uk, foo.com.au → the name is the label BEFORE these.
_PUBLIC_SUFFIX_SLD = {
    "co", "com", "net", "org", "edu", "gov", "ac", "gob", "or", "ne", "go",
}


def _domain_root(email_or_domain: str) -> str:
    """The registrable name out of an address or domain, lowercased.

    Strips bulk-mail sending subdomains and multi-part public suffixes, so
    the answer is the organisation rather than whatever host relayed the
    message::

        email.mckinsey.com   → mckinsey
        mail.exed.hbs.edu    → hbs
        e.shrm.org           → shrm
        raibin@savas.co.in   → savas
        gaurav@jakson.com    → jakson

    Returns "" for personal-mail domains and anything unparseable.
    """
    if not email_or_domain:
        return ""
    domain = email_or_domain.strip().lower()
    if "@" in domain:
        domain = domain.split("@", 1)[1]
    domain = domain.strip().strip(".")
    if not domain or domain in PERSONAL_DOMAINS:
        return ""

    labels = [l for l in domain.split(".") if l]
    # Drop leading sending subdomains (mail.email.foo.com → foo.com), but
    # never strip away the whole name.
    while len(labels) > 2 and labels[0] in _SENDING_SUBDOMAINS:
        labels.pop(0)
    if len(labels) > 2 and labels[0] in _SENDING_SUBDOMAINS:
        labels.pop(0)
    if len(labels) < 2:
        return labels[0] if labels else ""

    # foo.co.in / foo.ac.uk → the name sits one label further left.
    if len(labels) >= 3 and labels[-2] in _PUBLIC_SUFFIX_SLD:
        return labels[-3]
    return labels[-2]


def _company_from_domain(email: str) -> str:
    if not email or "@" not in email:
        return ""
    domain = email.split("@", 1)[1].strip().lower()
    if domain.startswith("www."):
        domain = domain[4:]
    if domain in PERSONAL_DOMAINS:
        return ""
    root = _domain_root(domain)
    if not root:
        return ""
    return root.title()


def _sender_info(msg: dict) -> Tuple[str, str]:
    """Return (from_email, from_name) — both possibly empty strings."""
    frm = (msg.get("from") or {}).get("emailAddress") or {}
    return (frm.get("address") or "").strip(), (frm.get("name") or "").strip()


# ─── v2026-09 — Forwarded-lead detection, splitting and promotion ──────
# Policy (see docs/LEAD_INGESTION_POLICY.md):
#   leads@procamgroup.in is the ONLY lead source, and the CRM imports ONLY
#   emails that a Procam employee forwarded into it. A forward is a
#   *container*: the lead is the ORIGINAL message inside it, never the
#   employee who pressed Forward.
#
# This section provides:
#   split_forwarded_body()   — split "employee note | original headers |
#                              original body" out of a forwarded message
#   analyze_forward()        — full analysis + in-place promotion of
#                              msg['from'] to the original external sender
#   _promote_forwarded_sender() — back-compat thin wrapper over the above

# "---------- Forwarded message ----------", "-----Original Message-----",
# "Begin forwarded message:" and the bare "Forwarded message" variants that
# Outlook/Gmail/Apple Mail emit once HTML has been flattened to text.
_FORWARD_BLOCK_RE = re.compile(
    r"(?i)^[\s>*_\-]*(?:"
    r"-{2,}\s*forwarded message\s*-{2,}"
    r"|-{2,}\s*original message\s*-{2,}"
    r"|begin forwarded message\s*:?"
    r"|forwarded message\s*:?"
    r")[\s>*_\-]*$"
)

# A single RFC-822-ish header line inside a forwarded block.
_HEADER_LINE_RE = re.compile(
    r"(?i)^[\s>]*(from|sent|date|to|cc|bcc|subject|reply-to|importance)"
    r"\s*:\s*(.*)$"
)

# "Name" <addr@example.com>  /  Name <addr@example.com>  /  addr@example.com
_ADDR_RE = re.compile(
    r"^\s*(?:\"?([^<\"]{0,120}?)\"?\s*)?<\s*([^>\s]+@[^>\s]+?)\s*>\s*$"
)

# Matches typical Outlook / Gmail / plain-text forward header blocks:
#   From: John Doe <john@customer.com>
#   From: john@customer.com
# Case-insensitive, line-anchored (after any leading > or whitespace).
_FORWARD_FROM_RE = re.compile(
    r"(?im)^[\s>]*from\s*:\s*"
    r"(?:\"?([^<\"\n\r]{1,120})\"?\s*<\s*([^>\s]+@[^>\s]+)\s*>"   # "Name" <email>
    r"|([^\s<>()\"@]+@[^\s<>()\",;]+))"                             # bare email
    r"\s*$"
)

# Fallback for mobile/iOS/Apple Mail style:
#   On Wed, Aug 27, 2026 at 3:45 PM, John Doe <john@customer.com> wrote:
_FORWARD_WROTE_RE = re.compile(
    r"(?is)\bon\b[^<\n\r]{5,140}?"
    r"<\s*([^>\s]+@[^>\s]+)\s*>"
    r"\s*wrote:"
)

# Generic e-mail address (used only as a last-resort fallback).
_ANY_EMAIL_RE = re.compile(
    r"([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})"
)

# Subject prefixes that mark a forward. English + common client variants.
_FORWARD_SUBJECT_RE = re.compile(r"^\s*(?:(?:fwd?|fw|f/w)\s*[:.]\s*)+", re.I)

# Subject prefixes stripped when recovering the ORIGINAL subject.
_SUBJECT_PREFIX_RE = re.compile(r"^\s*(?:(?:re|fwd?|fw|f/w)\s*[:.]\s*)+", re.I)


def _parse_addr(raw: str) -> Tuple[str, str]:
    """Parse a From:-style value into (email, display_name)."""
    raw = (raw or "").strip().rstrip(";,")
    if not raw:
        return "", ""
    m = _ADDR_RE.match(raw)
    if m:
        return (m.group(2) or "").strip().lower(), (m.group(1) or "").strip()
    m2 = _ANY_EMAIL_RE.search(raw)
    if m2:
        email = m2.group(1).strip().lower()
        # Anything before the address (minus brackets/quotes) is the name.
        name = raw[: m2.start()].strip(" \"'<>(),;-")
        return email, name
    return "", raw.strip(" \"'")


def split_forwarded_body(body_text: str) -> dict:
    """Split a forwarded email body into its three parts.

    Returns::

        {'is_forward':    bool,
         'forward_note':  str,   # what the employee typed above the forward
         'original_body': str,   # the ORIGINAL message content
         'headers':       {'from': ..., 'subject': ..., 'to': ..., ...},
         'trigger':       'marker' | 'header' | 'wrote' | None}

    `trigger` says what the split keyed off. Only 'marker' — an explicit
    "Forwarded message" / "Original Message" block — proves a forward on
    its own; a bare `From:` header ('header') also appears in ordinary
    quoted reply chains, so callers must corroborate it.

    When the body is not a forward, `is_forward` is False and
    `original_body` is the untouched input.
    """
    out = {"is_forward": False, "trigger": None, "forward_note": "",
           "original_body": body_text or "", "headers": {}}
    if not body_text:
        return out

    lines = body_text.splitlines()

    marker_idx = None
    for i, line in enumerate(lines):
        if _FORWARD_BLOCK_RE.match(line):
            marker_idx = i
            break

    from_idx = None
    for i, line in enumerate(lines):
        m = _HEADER_LINE_RE.match(line)
        if m and m.group(1).lower() == "from" and "@" in (m.group(2) or ""):
            from_idx = i
            break

    # Where the original message's header block starts.
    header_start = None
    if marker_idx is not None:
        if from_idx is not None and from_idx >= marker_idx:
            header_start = from_idx
        else:
            header_start = marker_idx + 1
        note_end = marker_idx
        out["trigger"] = "marker"
    elif from_idx is not None:
        header_start = from_idx
        note_end = from_idx
        out["trigger"] = "header"
    else:
        # Last resort — Apple/iOS "On <date>, X <a@b.c> wrote:" style.
        m = _FORWARD_WROTE_RE.search(body_text)
        if m:
            out["is_forward"] = True
            out["trigger"] = "wrote"
            out["forward_note"] = body_text[: m.start()].strip()
            out["original_body"] = body_text[m.end():].strip() or body_text
            out["headers"] = {"from": m.group(0)}
            return out
        return out

    out["is_forward"] = True
    out["forward_note"] = "\n".join(lines[:note_end]).strip()

    # Consume the consecutive header lines (tolerating blank lines that the
    # HTML→text flattener leaves between them).
    headers: dict = {}
    i = header_start
    blanks = 0
    last_header = header_start - 1
    while i < len(lines) and i < header_start + 16:
        line = lines[i]
        m = _HEADER_LINE_RE.match(line)
        if m:
            key = m.group(1).lower()
            val = (m.group(2) or "").strip()
            # Only the first occurrence of each header belongs to this level
            # of the forward; deeper nesting is left inside the body.
            if key not in headers:
                headers[key] = val
            last_header = i
            blanks = 0
        elif not line.strip():
            blanks += 1
            if blanks > 2:
                break
        else:
            break
        i += 1

    out["headers"] = headers
    original = "\n".join(lines[last_header + 1:]).strip()
    out["original_body"] = original or body_text
    return out


def _extract_forwarded_sender(subject: str, body_text: str) -> Optional[Tuple[str, str]]:
    """If the message looks like a forward, return (email, name) of the
    original external sender.

    Tries, in order:
      1. The `From:` header of the split forwarded block.
      2. Strict "From: ..." header line anywhere in the body.
      3. "On <date>, <name> <email> wrote:" (iOS Mail / mobile Outlook).
      4. Fallback: first non-internal, non-noreply address in the body.
    """
    if not body_text:
        return None
    lowered = body_text.lower()
    split = split_forwarded_body(body_text)
    looks_forwarded = bool(_FORWARD_SUBJECT_RE.match(subject or "")) or split["is_forward"] or (
        "forwarded message" in lowered
        or "begin forwarded" in lowered
        or "-----original message-----" in lowered
        or " wrote:" in lowered                # iOS / mobile reply-forward
    )
    if not looks_forwarded:
        return None

    # Strategy 1 — From: header of the split block
    hdr_from = (split.get("headers") or {}).get("from")
    if hdr_from:
        email, name = _parse_addr(hdr_from)
        if email and "@" in email:
            return email, name

    # Strategy 2 — strict From: header anywhere
    m = _FORWARD_FROM_RE.search(body_text)
    if m:
        name  = (m.group(1) or "").strip()
        email = (m.group(2) or m.group(3) or "").strip().lower()
        if email and "@" in email:
            return email, name

    # Strategy 3 — "On <date>, <name> <email> wrote:"
    m = _FORWARD_WROTE_RE.search(body_text)
    if m:
        email = (m.group(1) or "").strip().lower()
        if email and "@" in email:
            return email, ""

    # Strategy 4 — first non-internal email in the body
    skip = _skip_domains()
    for match in _ANY_EMAIL_RE.finditer(body_text):
        email = match.group(1).lower()
        if "@" not in email:
            continue
        domain = email.split("@", 1)[1]
        if domain in skip:
            continue    # internal — skip
        if _NOREPLY_RE.search(email):
            continue
        return email, ""

    return None


def analyze_forward(msg: dict) -> dict:
    """Detect + unwrap a forwarded lead email.

    A forwarded email is a *container*. This function pulls the original
    message out of it and, when the original sender is external, rewrites
    ``msg['from']`` in place so every downstream step (skip check, AI
    extraction, dedup, Lead attribution) sees the CUSTOMER — never the
    Procam employee who forwarded it.

    The result is also cached on ``msg['_forward']``. Returns::

        {'is_forward', 'promoted', 'forwarded_by', 'forwarder_is_internal',
         'original_sender', 'original_name', 'original_subject',
         'forward_note', 'original_body', 'reason'}

    `reason` is set only when the message looks forwarded but the original
    sender could not be resolved — the caller uses it to reject rather than
    fall back to attributing the lead to the forwarder.
    """
    cached = msg.get("_forward")
    if isinstance(cached, dict):
        return cached

    frm_email, frm_name = _sender_info(msg)
    subject   = (msg.get("subject") or "").strip()
    body_text = _get_body_text(msg)
    skip      = _skip_domains()
    frm_domain = frm_email.split("@", 1)[1].lower() if "@" in frm_email else ""
    forwarder_is_internal = bool(frm_domain and frm_domain in skip)

    split = split_forwarded_body(body_text)
    subject_marks_forward = bool(_FORWARD_SUBJECT_RE.match(subject))
    # An explicit "Forwarded message" / "Original Message" block or a
    # Fw:/Fwd: subject proves a forward on its own. A bare "From:" header
    # does NOT — an ordinary quoted reply chain looks identical — so for an
    # external sender it is not enough. An INTERNAL sender writing into the
    # leads inbox is by definition relaying someone else's mail, so any
    # split signal (or none at all) counts as a forward for them.
    explicit_forward = bool(subject_marks_forward or split["trigger"] == "marker")
    is_forward = bool(explicit_forward or forwarder_is_internal)

    info = {
        "is_forward":            is_forward,
        "promoted":              False,
        "forwarded_by":          ({"email": frm_email, "name": frm_name}
                                  if is_forward and frm_email else None),
        "forwarder_is_internal": forwarder_is_internal,
        "original_sender":       None,
        "original_name":         "",
        "original_subject":      "",
        "forward_note":          split["forward_note"],
        "original_body":         split["original_body"],
        "reason":                None,
    }

    if not is_forward:
        msg["_forward"] = info
        return info

    # ── Resolve the ORIGINAL sender ───────────────────────────────────
    orig_email, orig_name = "", ""
    hdr_from = (split.get("headers") or {}).get("from")
    if hdr_from:
        orig_email, orig_name = _parse_addr(hdr_from)
    if not orig_email and (explicit_forward or split["trigger"]):
        # The looser strategies (bare From: line, "… wrote:", first external
        # address in the body) only run when the message actually carries a
        # forward signal. For an internal sender with no forward structure
        # at all, scanning the body for "some external address" would pick
        # up whatever happens to sit in a signature — better to reject.
        found = _extract_forwarded_sender(subject, body_text)
        if found:
            orig_email, orig_name = found

    if not orig_email or "@" not in orig_email:
        info["reason"] = "forwarded email: original sender not identifiable"
        msg["_forward"] = info
        return info

    orig_domain = orig_email.split("@", 1)[1].lower()
    if orig_domain in skip:
        # The "original" is another internal address — an internal thread,
        # not a customer lead. Never attribute it to the forwarder.
        info["reason"] = ("forwarded email: original sender is also internal "
                          f"({orig_domain})")
        msg["_forward"] = info
        return info

    if frm_email and orig_email == frm_email.lower():
        info["reason"] = ("forwarded email: original sender is the forwarder "
                          "themselves")
        msg["_forward"] = info
        return info

    # Original subject — prefer the forwarded Subject: header, else strip
    # the Fwd:/Re: prefixes off the outer subject.
    hdr_subject = (split.get("headers") or {}).get("subject") or ""
    info["original_subject"] = (hdr_subject.strip()
                                or _SUBJECT_PREFIX_RE.sub("", subject).strip())
    info["original_sender"] = orig_email
    info["original_name"]   = orig_name
    info["promoted"]        = True

    # Rewrite in place so downstream sees the customer, not the employee.
    msg["from"] = {"emailAddress": {"address": orig_email,
                                    "name": orig_name or ""}}
    if info["forwarded_by"]:
        msg["_forwarded_by"] = info["forwarded_by"]
    msg["_forward"] = info
    return info


def _promote_forwarded_sender(msg: dict) -> Optional[dict]:
    """Back-compat wrapper. Returns a dict describing the promotion, or
    None when nothing was rewritten."""
    info = analyze_forward(msg)
    if not info.get("promoted"):
        return None
    return {"forwarded_by": (info.get("forwarded_by") or {}).get("email"),
            "original_sender": info.get("original_sender")}


def _get_body_text(msg: dict) -> str:
    body = msg.get("body") or {}
    content = body.get("content") or ""
    content_type = (body.get("contentType") or "").lower()
    if content_type == "html":
        return _html_to_text(content)
    return (content or "").strip()


def _skip_domains() -> set:
    raw = os.environ.get("EMAIL_INGEST_SKIP_DOMAINS", "procamlogistics.com,procamgroup.in")
    return {d.strip().lower() for d in raw.split(",") if d.strip()}


def _should_skip(msg: dict) -> Optional[str]:
    """Return the first skip reason, or None if the message should be processed."""
    subject = (msg.get("subject") or "").strip()
    subject_l = subject.lower()
    body_preview = (msg.get("bodyPreview") or "").strip()
    from_email, _ = _sender_info(msg)

    if not subject and not body_preview:
        return "empty message"

    # Auto-responders / delivery status
    for marker in _AUTOREPLY_SUBJECT_MARKERS:
        if marker in subject_l:
            return f"auto-reply: {marker}"

    # Bounces
    if _BOUNCE_RE.search(subject):
        return "bounce / delivery failure"

    # No-reply / mailer-daemon senders
    if from_email and _NOREPLY_RE.search(from_email):
        return "no-reply sender"

    # Internal domains AND bulk-mail infra domains
    if from_email and "@" in from_email:
        sender_domain = from_email.split("@", 1)[1].lower()
        if sender_domain in _skip_domains():
            return f"internal domain: {sender_domain}"
        # Bulk marketing / SaaS-email infra (Substack, Mailchimp, LinkedIn, etc.)
        for suffix in _BULK_DOMAIN_SUFFIXES:
            if sender_domain == suffix or sender_domain.endswith("." + suffix):
                return f"bulk marketing infra: {sender_domain}"

        # Newsletter-style local part (newsletter@, updates@, alerts@, ...)
        local = from_email.split("@", 1)[0]
        if _BULK_LOCAL_RE.match(local):
            return f"bulk sender: {local}@"

    # Newsletters
    for pfx in _NEWSLETTER_PREFIXES:
        if subject_l.startswith(pfx):
            return "newsletter"

    body_content = (msg.get("body") or {}).get("content") or ""
    if body_content:
        unsub_count = len(re.findall(r"unsubscribe", body_content, re.I))
        if unsub_count > 2:
            return "newsletter / bulk (multiple unsubscribe links)"

    # Invoices / statements / receipts
    if _INVOICE_RE.search(subject):
        return "invoice / statement"

    return None


# ─── Main entry ────────────────────────────────────────────────────────
def extract_lead(msg: dict) -> Optional[dict]:
    """Convert a Graph message dict into a lead-payload dict.

    Returns a dict with keys:
        company, contact_name, email, phone, subject, body_text,
        signals (rfq, cargo_keywords, origin, destination, urgency),
        confidence, skip_reason

    For a forwarded email the payload describes the ORIGINAL message
    inside the forward — `subject` and `body_text` are the original
    subject/content, and `email` / `contact_name` are the original
    external sender. The forwarding employee is recorded separately under
    `forwarded_by` / `forward_note` and never becomes the contact.

    If `skip_reason` is set, the caller should not create a lead.
    """
    if not isinstance(msg, dict):
        return None

    # v2026-09 — the leads inbox receives forwards. Unwrap the container:
    # rewrite msg["from"] to the ORIGINAL external sender so every step
    # downstream (skip check, dedup, AI extract, Lead attribution) treats
    # the customer as the source, not the employee who forwarded it.
    fwd = analyze_forward(msg)

    subject = (msg.get("subject") or "").strip()
    from_email, from_name = _sender_info(msg)
    body_text = _get_body_text(msg)

    # For a resolved forward, parse the ORIGINAL message only — the
    # employee's covering note is kept aside so it never pollutes the
    # signals, the AI extraction or the lead notes.
    if fwd.get("promoted"):
        subject   = fwd.get("original_subject") or _SUBJECT_PREFIX_RE.sub("", subject).strip()
        body_text = fwd.get("original_body") or body_text

    payload = {
        "company": "",
        "contact_name": from_name or (from_email.split("@")[0] if from_email else ""),
        "email": from_email,
        "phone": None,
        "subject": subject,
        "body_text": body_text,
        "signals": {
            "rfq": False,
            "cargo_keywords": [],
            "origin": None,
            "destination": None,
            "urgency": "low",
        },
        "confidence": 0.0,
        "skip_reason": None,
        # ── Forward metadata (consumed by the enricher) ────────────────
        "is_forward": bool(fwd.get("is_forward")),
        "forward_resolved": bool(fwd.get("promoted")),
        "forward_reason": fwd.get("reason"),
        "forwarded_by": fwd.get("forwarded_by"),
        "forward_note": fwd.get("forward_note") or "",
        "outer_subject": (msg.get("subject") or "").strip(),
    }

    reason = _should_skip(msg)
    if reason:
        payload["skip_reason"] = reason
        return payload

    # Signals
    combined = f"{subject}\n{body_text}"
    origin, destination = _extract_origin_destination(combined)
    cargo = _extract_cargo_keywords(combined)
    rfq = _is_rfq(subject, body_text)
    urgency = _urgency(subject, body_text)
    phone = _extract_phone(combined)

    payload["phone"] = phone
    payload["signals"] = {
        "rfq": rfq,
        "cargo_keywords": cargo,
        "origin": origin,
        "destination": destination,
        "urgency": urgency,
    }

    # Company: prefer domain-derived (skips personal domains); else empty
    company = _company_from_domain(from_email)
    payload["company"] = company

    # Confidence — weighted to favour real inquiries with actionable signals.
    score = 0.0
    if phone:
        score += 0.30
    if rfq:
        score += 0.30            # RFQ is the strongest single signal
    if origin or destination:
        score += 0.20
    if len(cargo) >= 3:
        score += 0.20            # 3+ cargo keywords implies genuine logistics content
    elif len(cargo) >= 2:
        score += 0.10
    if company:
        score += 0.10
    # Penalise pure "personal domain" senders unless they have RFQ signals
    if from_email and "@" in from_email:
        domain = from_email.split("@", 1)[1].lower()
        if domain in PERSONAL_DOMAINS and not rfq:
            score -= 0.10
    if score < 0.0:
        score = 0.0
    elif score > 1.0:
        score = 1.0
    payload["confidence"] = round(score, 3)

    # Hard requirement: must contain some real logistics content.
    # An email with NO cargo keywords, NO RFQ signal, NO route reference,
    # and NO phone is not a logistics lead — probably admin/tech/HR chatter
    # that slipped through the domain blocklist. Skip it outright.
    if not cargo and not rfq and not (origin or destination) and not phone:
        payload["confidence"] = round(score, 3)
        payload["skip_reason"] = "not logistics-related (no cargo/RFQ/route/phone signals)"
        return payload

    # Threshold 0.5 (from 0.4): emails below this don't reach AI (cost gate).
    # Anything with a real logistics signal (RFQ + cargo, or phone + route, etc.)
    # clears 0.5 easily; weak-signal emails get filtered here for free.
    # v2026-08 — forwarded-by-internal emails are already vetted by a
    # Procam employee, so don't second-guess them with confidence scores.
    is_internal_forward = bool(payload.get("forward_resolved"))
    if not is_internal_forward and score < 0.5:
        payload["skip_reason"] = "low confidence"

    return payload
