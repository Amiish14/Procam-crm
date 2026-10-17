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
