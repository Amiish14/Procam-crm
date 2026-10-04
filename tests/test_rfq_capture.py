"""Capturing the client's request: the files, and the original message.

The regression this file exists for: the webhook path — the one
production runs — saved attachment files to disk and never wrote the
`LeadAttachment` rows, so nothing could serve them. The first test is
that bug, stated as a property.
"""
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'capture-test-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'CaptureTest12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'capture.db'))

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Lead, LeadAttachment = _main.Lead, _main.LeadAttachment

from app.models.mailops import LeadRawEmail               # noqa: E402
from app.services import rfq_capture                      # noqa: E402

TAG = 'CAP-'
RAW = (b'Message-ID: <cap-1@customer.test>\r\n'
       b'Subject: Shipment of 3 transformers\r\n'
       b'From: buyer@customer.test\r\n\r\nPlease quote.\r\n')


class FakeGraph:
    """Enough of GraphClient for the capture path."""

    def __init__(self, attachments=None, raw=RAW, missing=False):
        self._atts = attachments or []
        self._raw = raw
        self._missing = missing
        self.calls = []

    def list_attachments(self, mailbox, message_id):
        self.calls.append(('attachments', message_id))
        return self._atts

    def _request(self, method, path, **kw):
        self.calls.append((method, path))

        class R:
            text = ''
        r = R()
        r.status_code = 404 if self._missing else 200
        r.content = b'' if self._missing else self._raw
        return r


def _att(name, data=b'%PDF-1.4 fake', aid='AAA'):
    import base64
    return {'@odata.type': '#microsoft.graph.fileAttachment', 'id': aid,
            'name': name, 'contentType': 'application/pdf',
            'size': len(data), 'isInline': False,
            'contentBytes': base64.b64encode(data).decode()}


def _wipe():
    ids = [l.id for l in Lead.query.filter(Lead.company.like(TAG + '%')).all()]
    if ids:
        LeadAttachment.query.filter(LeadAttachment.lead_id.in_(ids)).delete(
            synchronize_session=False)
        LeadRawEmail.query.filter(LeadRawEmail.lead_id.in_(ids)).delete(
            synchronize_session=False)
        Lead.query.filter(Lead.id.in_(ids)).delete(synchronize_session=False)
    db.session.commit()


@pytest.fixture()
def lead(tmp_path, monkeypatch):
    from email_ingest import attachments as att_mod
    monkeypatch.setattr(att_mod, 'STORAGE_ROOT', str(tmp_path))
    with flask_app.app_context():
        db.create_all()
        _wipe()
        row = Lead(company=TAG + 'Customer Ltd', stage='New Opportunity',
                   source='email', email_message_id='<cap-1@customer.test>')
        db.session.add(row)
        db.session.commit()
        yield row
        _wipe()


def _msg(**kw):
    base = {'id': 'GRAPH-ID-1',
            'internetMessageId': '<cap-1@customer.test>',
            'subject': 'Shipment of 3 transformers',
            'from': {'emailAddress': {'address': 'buyer@customer.test'}},
            'hasAttachments': True}
    base.update(kw)
    return base


# ── the regression ───────────────────────────────────────────────────
def test_a_saved_file_gets_a_row_so_it_can_be_downloaded(lead, monkeypatch):
    monkeypatch.delenv('FEATURE_RFQ_CAPTURE', raising=False)
    graph = FakeGraph(attachments=[_att('BOQ.pdf')])
    out = rfq_capture.capture_for_lead(lead, graph=graph,
                                       mailbox='leads@procamgroup.in',
                                       msg=_msg(), commit=True)
    assert out['attachments'] == 1
    rows = LeadAttachment.query.filter_by(lead_id=lead.id).all()
    assert [r.filename for r in rows] == ['BOQ.pdf']
    assert os.path.exists(rows[0].storage_path)


def test_capturing_the_same_message_twice_adds_nothing(lead, monkeypatch):
    graph = FakeGraph(attachments=[_att('BOQ.pdf')])
    for _ in range(2):
        rfq_capture.capture_for_lead(lead, graph=graph,
                                     mailbox='leads@procamgroup.in',
                                     msg=_msg(), commit=True)
    assert LeadAttachment.query.filter_by(lead_id=lead.id).count() == 1


def test_a_file_already_on_disk_is_reused_not_copied(lead, monkeypatch, tmp_path):
    """The back-fill case. The webhook saved the file and wrote no row;
    re-fetching must adopt the file that is there, not write a second
    copy beside it and orphan the first — across ten thousand leads
    that is the attachment directory twice over."""
    import os as _os
    from email_ingest import attachments as att_mod

    lead_dir = _os.path.join(str(tmp_path), str(lead.id))
    _os.makedirs(lead_dir, exist_ok=True)
    data = b'%PDF-1.4 fake'
    already = _os.path.join(lead_dir, 'BOQ.pdf')
    with open(already, 'wb') as fh:
        fh.write(data)

    graph = FakeGraph(attachments=[_att('BOQ.pdf', data)])
    out = rfq_capture.capture_for_lead(lead, graph=graph,
                                       mailbox='leads@procamgroup.in',
                                       msg=_msg(), commit=True)
    assert out['attachments'] == 1
    assert sorted(_os.listdir(lead_dir)) == ['BOQ.pdf'], \
        'a second copy of the same file was written'
    row = LeadAttachment.query.filter_by(lead_id=lead.id).one()
    assert row.storage_path == already


def test_a_different_file_with_the_same_name_still_gets_its_own_copy(
        lead, tmp_path):
    """The other half: same name, different bytes, is a different file
    and must not be swallowed by the reuse."""
    import os as _os

    lead_dir = _os.path.join(str(tmp_path), str(lead.id))
    _os.makedirs(lead_dir, exist_ok=True)
    with open(_os.path.join(lead_dir, 'BOQ.pdf'), 'wb') as fh:
        fh.write(b'an entirely different document')

    graph = FakeGraph(attachments=[_att('BOQ.pdf', b'%PDF-1.4 fake')])
    rfq_capture.capture_for_lead(lead, graph=graph,
                                 mailbox='leads@procamgroup.in',
                                 msg=_msg(), commit=True)
    assert sorted(_os.listdir(lead_dir)) == ['BOQ-1.pdf', 'BOQ.pdf']


def test_a_message_with_no_attachments_fetches_nothing(lead):
    graph = FakeGraph(attachments=[_att('BOQ.pdf')])
    rfq_capture.capture_for_lead(lead, graph=graph,
                                 mailbox='leads@procamgroup.in',
                                 msg=_msg(hasAttachments=False), commit=True)
    assert graph.calls == [] or all(c[0] != 'attachments'
                                    for c in graph.calls)


# ── the original message ─────────────────────────────────────────────
def test_the_original_is_not_kept_unless_the_flag_is_on(lead, monkeypatch):
    monkeypatch.delenv('FEATURE_RFQ_CAPTURE', raising=False)
    graph = FakeGraph()
    out = rfq_capture.capture_for_lead(lead, graph=graph,
                                       mailbox='leads@procamgroup.in',
                                       msg=_msg(), commit=True)
    assert out['raw'] == 'off'
    assert LeadRawEmail.query.filter_by(lead_id=lead.id).count() == 0


def test_the_original_is_written_as_an_eml(lead, monkeypatch):
    monkeypatch.setenv('FEATURE_RFQ_CAPTURE', 'true')
    graph = FakeGraph()
    out = rfq_capture.capture_for_lead(lead, graph=graph,
                                       mailbox='leads@procamgroup.in',
                                       msg=_msg(), commit=True)
    assert out['raw'] == 'stored'
    row = LeadRawEmail.query.filter_by(lead_id=lead.id).one()
    assert row.storage_path.endswith('.eml')
    with open(row.storage_path, 'rb') as fh:
        assert fh.read() == RAW
    assert row.size_bytes == len(RAW)
    assert len(row.sha256) == 64


def test_capturing_the_original_twice_keeps_one_row(lead, monkeypatch):
    monkeypatch.setenv('FEATURE_RFQ_CAPTURE', 'true')
    graph = FakeGraph()
    for _ in range(2):
        rfq_capture.capture_for_lead(lead, graph=graph,
                                     mailbox='leads@procamgroup.in',
                                     msg=_msg(), commit=True)
    assert LeadRawEmail.query.filter_by(lead_id=lead.id).count() == 1


def test_a_message_the_mailbox_no_longer_has_is_recorded_not_retried(
        lead, monkeypatch):
    monkeypatch.setenv('FEATURE_RFQ_CAPTURE', 'true')
    graph = FakeGraph(missing=True)
    out = rfq_capture.capture_for_lead(lead, graph=graph,
                                       mailbox='leads@procamgroup.in',
                                       msg=_msg(), commit=True)
    assert out['raw'] == 'missing'
    row = LeadRawEmail.query.filter_by(lead_id=lead.id).one()
    assert row.status == 'missing' and row.error


def test_a_lookup_that_raises_is_a_missing_message_not_a_failure(
        lead, monkeypatch):
    """`_get_message_by_internet_id` raises when the message has left
    the Inbox. For the back-fill that is the ordinary outcome for an
    old lead, and letting it raise meant three hundred leads were
    scored as failed, recorded nowhere, and re-tried on every run."""
    from email_ingest import raw_mime, webhook

    def _boom(*a, **kw):
        raise RuntimeError('Graph could not find message with '
                           'internetMessageId ... it may have been deleted')

    monkeypatch.setattr(webhook, '_get_message_by_internet_id', _boom)
    assert raw_mime.graph_id_for(FakeGraph(), 'leads@procamgroup.in',
                                 '<gone@customer.test>') is None


def test_a_message_known_to_be_gone_is_recorded_without_asking_graph(
        lead, monkeypatch):
    """So the next run skips it instead of asking again."""
    monkeypatch.setenv('FEATURE_RFQ_CAPTURE', 'true')
    from email_ingest import raw_mime

    graph = FakeGraph()
    row = raw_mime.capture(
        lead.id, graph=graph, mailbox='leads@procamgroup.in',
        internet_message_id='<gone@customer.test>',
        subject='An old enquiry', known_missing=True, commit=True)
    assert row.status == 'missing' and row.error
    assert graph.calls == [], 'it asked Graph about a message it knew was gone'
    # And it is now settled, so a later pass has something to skip on.
    assert LeadRawEmail.query.filter_by(
        internet_message_id='<gone@customer.test>').count() == 1


def test_the_summary_is_what_the_drawer_and_the_email_both_read(
        lead, monkeypatch):
    monkeypatch.setenv('FEATURE_RFQ_CAPTURE', 'true')
    graph = FakeGraph(attachments=[_att('BOQ.pdf'), _att('Drawing.pdf',
                                                         aid='BBB')])
    rfq_capture.capture_for_lead(lead, graph=graph,
                                 mailbox='leads@procamgroup.in',
                                 msg=_msg(), commit=True)
    found = rfq_capture.summary_for_lead(lead.id)
    assert found['attachment_count'] == 2
    assert found['original_id'] is not None
    assert found['original']['status'] == 'stored'


def test_nothing_is_claimed_when_nothing_was_captured(lead):
    found = rfq_capture.summary_for_lead(lead.id)
    assert found['attachment_count'] == 0 and found['original'] is None
