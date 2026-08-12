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


def _company_from_domain(email: str) -> str:
    if not email or "@" not in email:
        return ""
    domain = email.split("@", 1)[1].strip().lower()
    if domain.startswith("www."):
        domain = domain[4:]
    if domain in PERSONAL_DOMAINS:
        return ""
    root = domain.split(".", 1)[0]
    if not root:
        return ""
    return root.title()


def _sender_info(msg: dict) -> Tuple[str, str]:
    """Return (from_email, from_name) — both possibly empty strings."""
    frm = (msg.get("from") or {}).get("emailAddress") or {}
    return (frm.get("address") or "").strip(), (frm.get("name") or "").strip()


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

    If `skip_reason` is set, the caller should not create a lead.
    """
    if not isinstance(msg, dict):
        return None

    subject = (msg.get("subject") or "").strip()
    from_email, from_name = _sender_info(msg)
    body_text = _get_body_text(msg)

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

    # Raised threshold: 0.4 (from 0.2). Filters out noise like single-cargo-
    # keyword ops emails from existing customers.
    if score < 0.4:
        payload["skip_reason"] = "low confidence"

    return payload
