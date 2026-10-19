"""The one way out of the building.

The CRM has never sent an email: Graph needs a Mail.Send grant the
token does not carry, so every send since the feature was built has
been a 403. These tests cover the transport that does not need it,
and the choice between the two.
"""
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'mailer-test-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'MailerTest12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'mailer.db'))

from app.services import mailer                            # noqa: E402


class FakeSMTP:
    """Enough of smtplib.SMTP to see what would go on the wire."""

    sent = []
    logins = []
    started_tls = False

    def __init__(self, host, port, timeout=None):
        FakeSMTP.host, FakeSMTP.port = host, port

    def starttls(self):
        FakeSMTP.started_tls = True

    def login(self, user, password):
        FakeSMTP.logins.append(user)

    def send_message(self, msg, to_addrs=None):
        FakeSMTP.sent.append((msg, to_addrs))

    def quit(self):
        pass


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    FakeSMTP.sent, FakeSMTP.logins, FakeSMTP.started_tls = [], [], False
    for name in ('CRM_MAIL_TRANSPORT', 'MAIL_SERVER', 'MAIL_USERNAME',
                 'MAIL_PASSWORD', 'MAIL_USE_SSL', 'CRM_MAIL_FROM',
                 'MAIL_DEFAULT_SENDER', 'CRM_INTERNAL_EMAIL_DOMAINS',
                 'CRM_EMAIL_ALLOW_EXTERNAL', 'NOTIFY_ENABLED'):
        monkeypatch.delenv(name, raising=False)
    import smtplib
    monkeypatch.setattr(smtplib, 'SMTP', FakeSMTP)
    monkeypatch.setattr(smtplib, 'SMTP_SSL', FakeSMTP)


def _smtp(monkeypatch):
    monkeypatch.setenv('MAIL_SERVER', 'smtp.office365.com')
    monkeypatch.setenv('MAIL_USERNAME', 'tms@procamgroup.in')
    monkeypatch.setenv('MAIL_PASSWORD', 'not-a-real-password')
    monkeypatch.setenv('CRM_MAIL_FROM', 'Procam CRM <tms@procamgroup.in>')


# ── choosing a transport ─────────────────────────────────────────────
def test_graph_is_used_only_while_no_smtp_server_is_configured():
    assert mailer.transport() == mailer.GRAPH


def test_configuring_a_mail_server_is_the_whole_switch(monkeypatch):
    _smtp(monkeypatch)
    assert mailer.transport() == mailer.SMTP


def test_the_transport_can_be_pinned_either_way(monkeypatch):
    _smtp(monkeypatch)
    monkeypatch.setenv('CRM_MAIL_TRANSPORT', 'graph')
    assert mailer.transport() == mailer.GRAPH


def test_it_says_plainly_when_it_cannot_send(monkeypatch):
    monkeypatch.setenv('CRM_MAIL_TRANSPORT', 'smtp')
    ok, why = mailer.ready()
    assert not ok and 'MAIL_SERVER' in why


# ── sending ──────────────────────────────────────────────────────────
def test_an_smtp_message_carries_both_a_text_and_an_html_part(monkeypatch):
    _smtp(monkeypatch)
    assert mailer.send('ops@procamgroup.in', 'Subject',
                       '<p>Hello <b>there</b></p>')
    msg, to_addrs = FakeSMTP.sent[0]
    assert to_addrs == ['ops@procamgroup.in']
    assert msg['From'] == 'Procam CRM <tms@procamgroup.in>'
    types = {part.get_content_type() for part in msg.walk()}
    assert 'text/plain' in types and 'text/html' in types
    body = msg.get_body(preferencelist=('plain',)).get_content()
    assert 'Hello there' in body, body


def test_it_logs_in_and_starts_tls(monkeypatch):
    _smtp(monkeypatch)
    mailer.send('ops@procamgroup.in', 'Subject', '<p>x</p>')
    assert FakeSMTP.started_tls
    assert FakeSMTP.logins == ['tms@procamgroup.in']


def test_an_attachment_rides_along(monkeypatch):
    _smtp(monkeypatch)
    mailer.send('ops@procamgroup.in', 'Subject', '<p>x</p>',
                attachments=[{'filename': 'RFQ.eml',
                              'content_type': 'message/rfc822',
                              'content': b'Subject: the original'}])
    msg, _ = FakeSMTP.sent[0]
    names = [p.get_filename() for p in msg.walk() if p.get_filename()]
    assert names == ['RFQ.eml']


def test_an_attachment_over_the_budget_is_left_out_and_the_mail_still_goes(
        monkeypatch):
    _smtp(monkeypatch)
    big = b'x' * (mailer.MAX_TOTAL_ATTACHMENT_BYTES + 1)
    assert mailer.send('ops@procamgroup.in', 'Subject', '<p>x</p>',
                       attachments=[{'filename': 'small.pdf',
                                     'content': b'tiny'},
                                    {'filename': 'huge.pdf',
                                     'content': big}])
    msg, _ = FakeSMTP.sent[0]
    names = [p.get_filename() for p in msg.walk() if p.get_filename()]
    assert names == ['small.pdf']


def test_cc_is_delivered_as_well_as_shown(monkeypatch):
    _smtp(monkeypatch)
    mailer.send('ops@procamgroup.in', 'Subject', '<p>x</p>',
                cc=['head@procamgroup.in'])
    msg, to_addrs = FakeSMTP.sent[0]
    assert msg['Cc'] == 'head@procamgroup.in'
    assert to_addrs == ['ops@procamgroup.in', 'head@procamgroup.in'], (
        'a Cc header with no envelope recipient is a header nobody receives')


# ── the rail in front of every transport ─────────────────────────────
def test_an_external_address_is_refused_over_smtp_too(monkeypatch):
    _smtp(monkeypatch)
    assert mailer.send('buyer@customer.com', 'Subject', '<p>x</p>') is False
    assert FakeSMTP.sent == []


def test_only_the_external_half_is_dropped(monkeypatch):
    _smtp(monkeypatch)
    assert mailer.send(['ops@procamgroup.in', 'buyer@customer.com'],
                       'Subject', '<p>x</p>')
    _msg, to_addrs = FakeSMTP.sent[0]
    assert to_addrs == ['ops@procamgroup.in']


def test_nothing_is_sent_when_notifications_are_switched_off(monkeypatch):
    _smtp(monkeypatch)
    monkeypatch.setenv('NOTIFY_ENABLED', 'false')
    assert mailer.send('ops@procamgroup.in', 'Subject', '<p>x</p>') is False
    assert FakeSMTP.sent == []


def test_a_transport_failure_is_reported_not_raised(monkeypatch):
    _smtp(monkeypatch)

    def _boom(*a, **kw):
        raise OSError('connection refused')

    import smtplib
    monkeypatch.setattr(smtplib, 'SMTP', _boom)
    assert mailer.send('ops@procamgroup.in', 'Subject', '<p>x</p>') is False


# ── the plain-text fallback ──────────────────────────────────────────
def test_html_becomes_readable_text():
    text = mailer._text_from(
        '<style>p{color:red}</style><h1>Lead assigned</h1>'
        '<p>Acme &amp; Co<br>Mumbai</p>')
    assert 'color:red' not in text
    assert 'Lead assigned' in text
    assert 'Acme & Co' in text
    assert 'Mumbai' in text
    assert '<' not in text


# ── message/rfc822: the 554 that stopped two notifications ───────────
def _built(attachments, **kw):
    """The assembled MIME message, as the SMTP path would send it."""
    from email.message import EmailMessage
    from email.utils import formataddr

    from app.services import mailer

    msg = EmailMessage()
    msg['Subject'] = kw.get('subject', 'Subject')
    msg['From'] = formataddr(('Procam CRM', 'tms@procamgroup.in'))
    msg['To'] = 'ops@procamgroup.in'
    msg.set_content('text part')
    msg.add_alternative('<p>html part</p>', subtype='html')
    mailer._attach(msg, attachments, kw.get('subject', 'Subject'))
    return msg


def _parts(msg):
    return [p for p in msg.walk()
            if p.get_content_disposition() == 'attachment']


RAW_EML = (b'From: buyer@client.com\r\n'
           b'To: leads@procamgroup.in\r\n'
           b'Subject: Enquiry DAP AE260904542\r\n'
           b'Message-ID: <abc@client.com>\r\n'
           b'\r\n'
           b'Please quote for Shanghai to Nhava Sheva.\r\n')


def test_an_eml_attachment_is_not_base64_encoded():
    """RFC 2046 §5.2.1: a message/rfc822 body may only be 7bit, 8bit
    or binary. Exchange enforces it and answers

        554 5.6.0 Invalid message content when handling MIME exception

    which is what two notifications carrying a captured original got
    on 16 October. The whole message is refused, so the colleague is
    told nothing at all.
    """
    msg = _built([{'filename': 'Enquiry.eml',
                   'content_type': 'message/rfc822',
                   'content': RAW_EML}])
    part = _parts(msg)[0]
    assert part.get_content_type() == 'message/rfc822'
    assert part.get('Content-Transfer-Encoding') in ('7bit', '8bit', 'binary')
    assert part.get('Content-Transfer-Encoding') != 'base64'
    bytes(msg)                      # must serialise without raising


def test_the_original_survives_being_attached():
    msg = _built([{'filename': 'Enquiry.eml',
                   'content_type': 'message/rfc822',
                   'content': RAW_EML}])
    raw = bytes(msg).decode('utf-8', 'replace')
    assert 'Enquiry DAP AE260904542' in raw
    assert 'Shanghai to Nhava Sheva' in raw


def test_bytes_that_are_not_a_message_go_as_a_file():
    """A truncated capture must still reach the reader, not take the
    whole notification down with it."""
    msg = _built([{'filename': 'Broken.eml',
                   'content_type': 'message/rfc822',
                   'content': b'\x00\x01 not a mail at all'}])
    part = _parts(msg)[0]
    assert part.get_content_type() == 'application/octet-stream'
    assert part.get_filename() == 'Broken.eml'
    bytes(msg)


def test_an_empty_eml_is_left_out_rather_than_sent_empty():
    msg = _built([{'filename': 'Nothing.eml',
                   'content_type': 'message/rfc822', 'content': b''}])
    assert _parts(msg) == []


def test_a_filename_cannot_carry_a_line_break():
    """It goes into a header. A newline there is a different message
    from the one intended."""
    msg = _built([{'filename': 'Quote\r\nBcc: outsider@example.com.pdf',
                   'content_type': 'application/pdf',
                   'content': b'%PDF-1.4 ...'}])
    part = _parts(msg)[0]
    name = part.get_filename() or ''
    assert '\n' not in name and '\r' not in name
    # The text survives inside the quoted filename parameter, which is
    # harmless. What must not exist is a line that *starts* a Bcc
    # header — that is what injection would have produced.
    raw = bytes(msg).decode('utf-8', 'replace')
    assert not any(line.lower().startswith('bcc:')
                   for line in raw.splitlines())


def test_an_ordinary_attachment_is_unchanged():
    msg = _built([{'filename': 'BOQ.pdf', 'content_type': 'application/pdf',
                   'content': b'%PDF-1.4 ...'}])
    part = _parts(msg)[0]
    assert part.get_content_type() == 'application/pdf'
    assert part.get_filename() == 'BOQ.pdf'


def test_an_eml_beside_an_ordinary_file_both_arrive():
    msg = _built([
        {'filename': 'Enquiry.eml', 'content_type': 'message/rfc822',
         'content': RAW_EML},
        {'filename': 'BOQ.pdf', 'content_type': 'application/pdf',
         'content': b'%PDF-1.4 ...'}])
    types = sorted(p.get_content_type() for p in _parts(msg))
    assert types == ['application/pdf', 'message/rfc822']
    bytes(msg)


def test_the_documents_arrive_and_the_bulky_original_is_what_gives():
    """Lead 12208: a 15.1 MB .eml consumed all but 565 KB of the
    budget, so seven of the ten real documents were dropped in favour
    of a second copy of each inside the original. An .eml carries the
    whole message including its own attachments, so everything
    travelled twice.
    """
    big_eml = RAW_EML + b'X' * (14 * 1024 * 1024)
    docs = [{'filename': f'Doc{i}.pdf', 'content_type': 'application/pdf',
             'content': b'%PDF-1.4 ' + b'd' * 200_000} for i in range(6)]
    msg = _built([{'filename': 'Original.eml',
                   'content_type': 'message/rfc822',
                   'content': big_eml}] + docs)

    names = [p.get_filename() for p in _parts(msg)]
    assert [n for n in names if n and n.startswith('Doc')] == \
        [f'Doc{i}.pdf' for i in range(6)], \
        'every real document should arrive'
    assert 'Original.eml' not in names, \
        'the original is what gives way, not the documents'


def test_the_original_still_goes_when_there_is_room():
    msg = _built([{'filename': 'Original.eml',
                   'content_type': 'message/rfc822', 'content': RAW_EML},
                  {'filename': 'BOQ.pdf', 'content_type': 'application/pdf',
                   'content': b'%PDF-1.4 ...'}])
    names = [p.get_filename() for p in _parts(msg)]
    assert set(names) == {'Original.eml', 'BOQ.pdf'}
    # and it is still last, so a future budget squeeze drops it first
    assert names[-1] == 'Original.eml'


def test_a_non_ascii_subject_leaves_as_rfc_2047():
    """An em dash in a subject is not a MIME fault. Python encodes
    non-ASCII headers on the way out; only the attribute is 8-bit."""
    msg = _built([], subject='Lead assigned to you — Alstom')
    head = bytes(msg).split(b'\r\n\r\n', 1)[0]
    head.decode('ascii')           # raises if anything 8-bit got out
    assert b'=?utf-8?' in head


# ── the two messages Exchange refused on 16 October ──────────────────
#
# Lead 12208, Alstom. A forwarded RFP captured as a 15.1 MB `.eml`
# plus the ten documents extracted from it. Both notifications came
# back
#
#     554 5.6.0 Invalid message content when handling MIME exception
#
# because `add_attachment(bytes, maintype='message', subtype='rfc822')`
# base64-encodes the part, and RFC 2046 §5.2.1 forbids any encoding on
# a message/rfc822 body but 7bit, 8bit or binary.
#
# They failed on 16 October at 04:31 and the fix landed on the 17th at
# 18:24, so they were never evidence of a hole in it — which is worth
# writing down, because a closeout check that used the wrong fix date
# reported them as a live failure three days later.
#
# These tests are built from that message's actual shape: the same
# eleven attachments, the same sizes, the same order.
ALSTOM_DOCS = (
    ('SCC financial template_Alstom.xlsx', 50677,
     'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
    ('Job description_CJB_WH.pdf', 209263, 'application/pdf'),
    ('Ethics and Sustainable Development Charter.pdf', 381260, 'application/pdf'),
    ('EHS Requirements for Critical Contractors.pdf', 585138, 'application/pdf'),
    ('About Alstom Appendix 11.1.pdf', 8569054, 'application/pdf'),
    ('Warehouse Operations Manpower_Service_Agreement.pdf', 291610, 'application/pdf'),
    ('Sricity Intercart Transport RFQ_C.pdf', 319588, 'application/pdf'),
    ('SOW Document Manpower_CJB.pdf', 254379, 'application/pdf'),
    ('SOW  Manpower Contract - Sri City.pdf', 348195, 'application/pdf'),
    ('image001.png', 22542, 'image/png'),
)
ALSTOM_EML_BYTES = 15163296
ALSTOM_EML_NAME = ('FW RFP - Warehouse Operations Manpower services - '
                   'Coimbatore  Sri City.eml')


def _alstom_attachments(eml_size=ALSTOM_EML_BYTES):
    """The eleven attachments of lead 12208, original first — which is
    the order the caller passed and the order `_attach` must not keep."""
    eml = (b'From: buyer@alstom.com\r\n'
           b'To: leads@procamgroup.in\r\n'
           b'Subject: FW RFP - Warehouse Operations Manpower\r\n'
           b'MIME-Version: 1.0\r\n'
           b'Content-Type: text/plain\r\n\r\n')
    eml += b'x' * max(0, eml_size - len(eml))
    out = [{'filename': ALSTOM_EML_NAME, 'content': eml,
            'content_type': 'message/rfc822'}]
    for name, size, ctype in ALSTOM_DOCS:
        out.append({'filename': name, 'content': b'\x01' * size,
                    'content_type': ctype})
    return out


def _build_alstom(attachments):
    from email.message import EmailMessage
    from app.services import mailer
    msg = EmailMessage()
    msg['Subject'] = 'Lead assigned to you — Alstom'
    msg['From'] = 'tms@procamgroup.in'
    msg['To'] = 'srinivas.m@procamgroup.in'
    msg.set_content('A lead has been assigned to you.')
    msg.add_alternative('<p>A lead has been assigned to you.</p>',
                        subtype='html')
    mailer._attach(msg, attachments, subject='Lead assigned to you — Alstom')
    return msg


def test_no_rfc822_part_is_ever_base64():
    """The 554 itself. RFC 2046 §5.2.1 allows only 7bit, 8bit or
    binary on a message/rfc822 body, and Exchange enforces it."""
    msg = _build_alstom(_alstom_attachments())
    for part in msg.walk():
        if part.get_content_type() == 'message/rfc822':
            cte = (part.get('Content-Transfer-Encoding') or '7bit').lower()
            assert cte in ('7bit', '8bit', 'binary'), (
                f'a message/rfc822 part is {cte}-encoded — this is the '
                f'exact shape Exchange refused with 554 5.6.0')


def test_the_real_documents_travel_and_the_bulky_original_does_not():
    """What the budget is for.

    The original `.eml` contains every one of these documents inside
    itself, so attaching both sends each twice — and at 15.1 MB the
    copy is larger than the ten originals together. Before the sort,
    it went first and seven of the ten real documents were dropped in
    its favour.
    """
    msg = _build_alstom(_alstom_attachments())
    names = [p.get_filename() for p in msg.walk() if p.get_filename()]
    for doc, _size, _ctype in ALSTOM_DOCS:
        assert doc in names, f'{doc} was dropped — it is what the lead is'
    assert ALSTOM_EML_NAME not in names, (
        'the 15.1 MB original consumed the budget again')


def test_a_small_original_still_travels():
    """The rule is the budget, not a ban. An `.eml` that fits is worth
    having: it is the customer's own words."""
    atts = _alstom_attachments(eml_size=40_000)
    msg = _build_alstom(atts)
    names = [p.get_filename() for p in msg.walk() if p.get_filename()]
    assert ALSTOM_EML_NAME in names
    for doc, _size, _ctype in ALSTOM_DOCS:
        assert doc in names


def test_the_built_message_stays_under_the_budget():
    msg = _build_alstom(_alstom_attachments())
    from app.services import mailer
    raw = msg.as_bytes()
    # Base64 adds about a third; the budget counts raw bytes, so the
    # wire size is larger. It must still be something Exchange takes.
    assert len(raw) < 20 * 1024 * 1024, (
        f'{len(raw)} bytes on the wire is too much to rely on')


def test_an_eml_that_will_not_parse_goes_as_a_file():
    """Truncated captures exist. They must not take the whole message
    down with them."""
    from email.message import EmailMessage
    from app.services import mailer
    msg = EmailMessage()
    msg.set_content('x')
    kind = mailer._attach_message(msg, b'not a message at all',
                                  'broken.eml')
    assert kind == 'application/octet-stream'
    names = [p.get_filename() for p in msg.walk() if p.get_filename()]
    assert 'broken.eml' in names
