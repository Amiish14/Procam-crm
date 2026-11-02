"""
When does our offer have to be in?

CRM-COCKPIT-02 §2. The CRM had no field for it and no way to read it,
so 137 leads sat at RFQ Generated or later with 28 quote dates between
them and nothing at all saying when the client stops accepting
offers. The number everybody actually works to was in the subject line
of the mail the whole time:

    RA- 57103: Indo Tech ... 29 Oct 2026 15:15:00
    eRFQ- 41688: Sterlite ... 04 Nov 2026 18:00:00

What this module does NOT do is reuse a date the CRM already had, and
that is the point of it. Indo Tech RA-57103 closed on 29 October with
an `email_extracted.target_date` of 13 November — the date the cargo
has to arrive, not the date the bid has to be in. Reusing it would
have set every auction deadline two weeks late and the tile would have
said green on the morning the auction closed.

Three readings, strongest first
    1. the portal subject — a stated minute, from a machine;
    2. a GeM or tender "bid end date";
    3. a phrase in the mail body: last date, due date, submission
       by/before, closing date, bid end, offer validity required by.

Anything read here is `quote_due_confirmed = False` until its owner
taps Confirm: a date a regular expression found is a proposal, and the
screen says so.

Times
    Mails state IST. Every datetime in this database is naive UTC, and
    a column that held IST would be compared against `utcnow()` by the
    first reader who did not know — five and a half hours of silent
    wrongness, in the direction that says a closed auction is still
    open. So this module parses IST and returns UTC, and
    `to_ist()` is what the screens render with.
"""
from __future__ import annotations

import re
from datetime import datetime, time, timedelta

#: India Standard Time. No DST, so a fixed offset is not a shortcut.
IST = timedelta(hours=5, minutes=30)

#: Where a date came from. `quote_due_source` holds one of these.
PORTAL_SUBJECT = 'portal_subject'
MAIL_BODY = 'mail_body'
GEM = 'gem'
TENDER = 'tender'
USER = 'user'
EXTENSION = 'extension'
SOURCES = (PORTAL_SUBJECT, MAIL_BODY, GEM, TENDER, USER, EXTENSION)

#: A date with no time on it. A portal states the minute; a sentence
#: in a body often does not, and something has to be assumed.
#: 18:00 IST — close of business — rather than 23:59, deliberately:
#: assuming the later time would paint a deadline green through the
#: afternoon it actually expired, and the cost of being early is a
#: tile that turns red a few hours sooner than it had to.
ASSUMED_HOUR = time(18, 0)

#: How far from the mail a due date can plausibly sit. A "valid until
#: 31 Dec 2027" in a signature block is not a submission deadline, and
#: a date two years out that lands in this field would hide the lead
#: from every tile for ever.
WINDOW_BACK = timedelta(days=3)
WINDOW_FORWARD = timedelta(days=400)

MONTHS = {
    'jan': 1, 'january': 1, 'feb': 2, 'february': 2, 'mar': 3,
    'march': 3, 'apr': 4, 'april': 4, 'may': 5, 'jun': 6, 'june': 6,
    'jul': 7, 'july': 7, 'aug': 8, 'august': 8, 'sep': 9, 'sept': 9,
    'september': 9, 'oct': 10, 'october': 10, 'nov': 11,
    'november': 11, 'dec': 12, 'december': 12,
}

# ── the portal number ────────────────────────────────────────────────
#: RA- 57103 / RA-57103 / RA 57103 — Indo Tech reverse auctions.
RX_RA = re.compile(r'\bRA[\s\-_]*(\d{3,8})\b', re.I)
#: eRFQ- 41688 — Procurengine / Sterlite.
RX_ERFQ = re.compile(r'\be[\s\-_]*RFQ[\s\-_]*(\d{3,8})\b', re.I)
#: GEM/2026/B/1234567 — the Government e-Marketplace bid number.
RX_GEM = re.compile(r'\bGEM[\s/\-]*(?:20\d\d[\s/\-]*)?B?[\s/\-]*(\d{4,10})\b',
                    re.I)
#: A tender or NIT reference. Deliberately last and deliberately
#: narrow: "tender" appears in half the mail in this mailbox.
RX_TENDER = re.compile(
    r'\b(?:tender|NIT)[\s:#\-]*(?:no\.?|number)?[\s:#\-]*'
    r'([A-Z0-9][A-Z0-9/\-]{5,28})\b', re.I)


def portal_key(subject='', body=''):
    """'RA-57103' | 'ERFQ-41688' | 'GEM-1234567' | 'TENDER-...' | ''.

    §5.6 — the key that holds a portal's three mails together. The
    alert, the reminder and the corrigendum arrive on separate threads
    with separate message ids, so `thread_key` cannot group them. The
    number can, and all three carry it.

    The subject is tried first and on its own: a quotation that quotes
    an earlier mail carries that mail's number in the body, and keying
    on it would attach our own offer to somebody else's auction.
    """
    for text in (subject or '', body or ''):
        if not text:
            continue
        m = RX_RA.search(text)
        if m:
            return f'RA-{int(m.group(1))}'
        m = RX_ERFQ.search(text)
        if m:
            return f'ERFQ-{int(m.group(1))}'
        m = RX_GEM.search(text)
        if m:
            return f'GEM-{m.group(1)}'
        if text is (subject or ''):
            m = RX_TENDER.search(text)
            if m:
                ref = re.sub(r'[^A-Z0-9/\-]', '', m.group(1).upper())
                return f'TENDER-{ref}'[:60]
    return ''


# ── dates in text ────────────────────────────────────────────────────
#: 29 Oct 2026 / 29-Oct-2026 / 29 October 26
RX_DMY_NAME = re.compile(
    r'\b(\d{1,2})[\s\-/]*([A-Za-z]{3,9})[\s\-/,]*(\d{2,4})\b')
#: 29-10-2026 / 29/10/2026 / 29.10.2026
RX_DMY_NUM = re.compile(r'\b(\d{1,2})[\-/.](\d{1,2})[\-/.](\d{2,4})\b')
#: 2026-10-29
RX_YMD = re.compile(r'\b(20\d\d)[\-/.](\d{1,2})[\-/.](\d{1,2})\b')
#: 15:15:00 / 15:15 / 3:15 PM / 3 PM / 1700 hrs. The last one has no
#: separator at all and is how most of this mailbox writes a time:
#: "by 1700 hrs" is a 17:00 deadline, and reading it as no time at all
#: assumed 18:00 and made an expired bid look open for an hour.
RX_TIME = re.compile(
    r'\b(\d{1,2})[:.](\d{2})(?::(\d{2}))?\s*(am|pm)?\b|'
    r'\b(\d{1,2})\s*(am|pm)\b|'
    r'\b([01]?\d|2[0-3])([0-5]\d)\s*(?:hrs?|hours)\b', re.I)


def _year(raw):
    value = int(raw)
    return value + 2000 if value < 100 else value


def _date_at(text, pos_from=0):
    """(date, end offset) for the first date at or after pos_from."""
    best = None
    for rx, order in ((RX_YMD, 'ymd'), (RX_DMY_NAME, 'dmy_name'),
                      (RX_DMY_NUM, 'dmy_num')):
        m = rx.search(text, pos_from)
        if not m:
            continue
        if best is not None and m.start() >= best[2]:
            continue
        try:
            if order == 'ymd':
                value = datetime(_year(m.group(1)), int(m.group(2)),
                                 int(m.group(3))).date()
            elif order == 'dmy_name':
                month = MONTHS.get(m.group(2).lower())
                if month is None:
                    continue
                value = datetime(_year(m.group(3)), month,
                                 int(m.group(1))).date()
            else:
                value = datetime(_year(m.group(3)), int(m.group(2)),
                                 int(m.group(1))).date()
        except ValueError:
            continue        # 31 February and friends
        best = (value, m.end(), m.start())
    if best is None:
        return None, pos_from
    return best[0], best[1]


def _time_near(text, pos, *, span=28):
    """The time immediately after a date, if there is one.

    Only just after: a subject can carry two times and a body a
    dozen, and the one that belongs to this date is the one beside
    it. 28 characters covers "29 Oct 2026 15:15:00" and "29 Oct
    2026 at 3:15 PM" without reaching the next sentence.
    """
    window = text[pos:pos + span]
    m = RX_TIME.search(window)
    if not m:
        return None
    if m.group(1):
        hour, minute = int(m.group(1)), int(m.group(2))
        meridiem = (m.group(4) or '').lower()
    elif m.group(5):
        hour, minute = int(m.group(5)), 0
        meridiem = (m.group(6) or '').lower()
    else:
        hour, minute = int(m.group(7)), int(m.group(8))
        meridiem = ''
    if meridiem == 'pm' and hour < 12:
        hour += 12
    if meridiem == 'am' and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return time(hour, minute)


def _ist_to_utc(day, clock):
    return datetime.combine(day, clock or ASSUMED_HOUR) - IST


# ── the phrases a person writes ──────────────────────────────────────
#: §2.3. Each is matched case-insensitively; the date is looked for
#: after the phrase, in the same sentence-ish window.
BODY_PHRASES = (
    'last date of submission', 'last date for submission',
    'last date', 'due date', 'submission by', 'submission before',
    'submission deadline', 'to be submitted by', 'submit by',
    'submit before', 'closing date', 'closing on', 'bid end date',
    'bid end', 'bid closing', 'offer validity required by',
    # §2.5 — the reminder mail on an auction already in the CRM. Its
    # subject carries the number but no date; the date is in this
    # sentence, and without these three the reminder proposed nothing.
    'auction closes', 'auction will close', 'closes on', 'closes at',
    'auction end', 'event ends', 'bid submission ends',
    # An adverb between the words is normal English and was enough to
    # miss a corrigendum: "the auction now closes 12 Nov". These two
    # are last in the list and still need a date immediately after
    # them, so "the window closes eventually" reads nothing.
    'now closes', 'closes', 'closing',
    'quotation required by', 'quote required by',
    'offers are to reach us by', 'deadline',
)

#: Phrases that look like a deadline and are not one. Checked first,
#: because "target date" and "delivery by" are the two that filled
#: this field with the wrong number when it was tried by hand.
NOT_A_DEADLINE = (
    'target date', 'delivery date', 'delivery by', 'required at site',
    'expected delivery', 'validity of our offer', 'our offer is valid',
    'payment due', 'invoice due', 'eta', 'etd',
)

#: §2.3 — a GeM or tender bid end, which states its own label.
GEM_PHRASES = ('bid end date', 'bid end date/time', 'bid closing date',
               'end date of bid submission')


class Reading:
    """A due date somebody or something proposed."""

    __slots__ = ('at', 'source', 'evidence')

    def __init__(self, at, source, evidence=''):
        self.at = at
        self.source = source
        self.evidence = (evidence or '')[:200]

    def __repr__(self):                                   # pragma: no cover
        return (f'Reading({self.at!r}, {self.source!r}, '
                f'{self.evidence!r})')

    def __eq__(self, other):
        return (isinstance(other, Reading) and self.at == other.at
                and self.source == other.source)


def _plausible(at, received_at):
    if at is None:
        return False
    if received_at is None:
        return True
    return (received_at - WINDOW_BACK) <= at <= (received_at
                                                 + WINDOW_FORWARD)


def from_subject(subject, *, received_at=None):
    """The date a portal states in its own subject line.

    The strongest reading there is: a machine wrote it, it carries a
    minute, and the subject of a portal mail is nothing but the
    reference and the deadline.
    """
    subject = subject or ''
    if not portal_key(subject):
        return None
    day, end = _date_at(subject)
    if day is None:
        return None
    at = _ist_to_utc(day, _time_near(subject, end))
    if not _plausible(at, received_at):
        return None
    return Reading(at, PORTAL_SUBJECT, subject.strip())


def from_body(body, *, received_at=None):
    """The date a sentence in the mail states."""
    text = ' '.join((body or '').split())
    if not text:
        return None
    low = text.lower()
    best = None
    for phrase in GEM_PHRASES + BODY_PHRASES:
        start = 0
        while True:
            at_pos = low.find(phrase, start)
            if at_pos < 0:
                break
            start = at_pos + len(phrase)
            # A phrase that is part of a disclaimed one does not
            # count: "target date" contains no deadline, and "our
            # offer is valid" is about our price, not their clock.
            head = low[max(0, at_pos - 24):at_pos + len(phrase)]
            if any(bad in head for bad in NOT_A_DEADLINE):
                continue
            day, end = _date_at(text, at_pos + len(phrase))
            if day is None or end - at_pos > 90:
                continue
            at = _ist_to_utc(day, _time_near(text, end))
            if not _plausible(at, received_at):
                continue
            source = GEM if phrase in GEM_PHRASES else MAIL_BODY
            found = Reading(at, source,
                            text[at_pos:min(len(text), end + 12)])
            # The earliest plausible deadline wins. A body that
            # states two is usually stating the bid end and a later
            # opening or delivery date, and being early is the safe
            # direction.
            if best is None or found.at < best.at:
                best = found
    return best


def read(subject='', body='', *, received_at=None):
    """The best reading for one message, or None.

    Subject before body, always: a portal's own line is a fact and a
    sentence in a body is an interpretation.
    """
    found = (from_subject(subject, received_at=received_at)
             or from_body(body, received_at=received_at))
    if found is not None and found.source == GEM \
            and portal_key(subject, body).startswith('TENDER-'):
        # "Bid end date" is the label GeM and a state tender both
        # use. The number says which this is, and `quote_due_source`
        # is read back as provenance, so it has to be the right word.
        found = Reading(found.at, TENDER, found.evidence)
    return found


def read_lead(lead):
    """The best reading for a lead, from the mail it came from."""
    return read(getattr(lead, 'original_email_subject', '') or '',
                getattr(lead, 'original_email_body', '') or '',
                received_at=(getattr(lead, 'received_at', None)
                             or getattr(lead, 'created_at', None)))


# ── rendering ────────────────────────────────────────────────────────
def to_ist(at):
    """The stored UTC moment as the IST one a person reads."""
    return (at + IST) if at else None


def today_ist():
    """The date it is where the users are.

    `date.today()` is the server's date, and the server keeps naive
    UTC. Between 18:30 and midnight UTC it is already tomorrow in
    IST, so a call logged at 01:00 IST on Tuesday is stamped Monday,
    and a follow-up proposed "today + 2" lands a day early. Every
    date a person reads or is held to comes from here.
    """
    return (datetime.utcnow() + IST).date()


def shown(at):
    """'04 Nov 2026 18:00' — the one way this is written on screen."""
    local = to_ist(at)
    return f'{local:%d %b %Y %H:%M}' if local else ''


#: §2.6 — one colour rule, read by the cockpit, the stage strip and
#: every list, so the three cannot drift apart.
RED, ORANGE, GREEN, GREY = 'red', 'orange', 'green', 'grey'


def colour(at, *, now=None):
    """'red' under a day, 'orange' under two, 'green' beyond, 'grey'
    for a lead with no date at all."""
    if at is None:
        return GREY
    now = now or datetime.utcnow()
    left = at - now
    if left < timedelta(days=1):
        return RED
    if left < timedelta(days=2):
        return ORANGE
    return GREEN


def hours_left(at, *, now=None):
    if at is None:
        return None
    return (at - (now or datetime.utcnow())).total_seconds() / 3600.0


def is_missed(lead, *, now=None):
    """Due date passed and no quote recorded (§2.7).

    A quote recorded at any revision counts: the question is whether
    an offer went in before the clock, not whether the number has
    since been revised.
    """
    at = getattr(lead, 'quote_due_at', None)
    if at is None:
        return False
    if (at > (now or datetime.utcnow())):
        return False
    if getattr(lead, 'quote_date', None) or \
            getattr(lead, 'quote_no', None) or \
            (getattr(lead, 'quote_value_num', None) or 0):
        return False
    # An outcome already recorded is the owner having answered the
    # question; the tile is for the ones nobody has answered.
    return not (getattr(lead, 'bid_outcome', '') or '').strip()
