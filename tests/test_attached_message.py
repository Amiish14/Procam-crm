"""Forward-as-attachment: reading the enquiry instead of the covering note.

CRM-USER-ISSUES-02NOV §4, second pass. QUESTIONS.md 68 recorded this as
the one piece of §4 left unbuilt.

Outlook's "Forward" puts the original into the body, and the parser
unwraps that already. "Forward as attachment" — and dragging a message
onto a new one, which is the default on Outlook for Mac — makes the
enquiry an `itemAttachment` instead, and `graph_client.list_attachments`
filtered those out while its own docstring said they "need different
handling".

So a lead was still created, which is why this never looked like
missing mail: it was built from "FYI, please quote" with no customer,
no route and no cargo, and the enquiry sat in an attachment nobody
read.

The design under test is that the attached message is rendered into
the text shape a normal forward has and spliced onto the body, so the
parser, the sender promotion, the signals and the AI extraction all
work unchanged. The tests therefore check the outcome the ingest cares
about — who the lead is from and what it says — not the rendering.
"""
import importlib
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'attached-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'Attached-12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'attached.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app = _main.app

from email_ingest import attached_message as att           # noqa: E402
from email_ingest import parser as email_parser            # noqa: E402

CLIENT = 'r.deshpande@zzattach-client.example'
COLLEAGUE = 'operations@procamlogistics.com'
ENQUIRY = ('Dear Sir,\n\nWe need to move a 40 MVA transformer from '
           'Nashik to Bhopal. Gross weight 82 MT, dimensions '
           '6.2 x 3.1 x 3.4 m. Kindly quote your best rates.\n\n'
           'Regards,\nR Deshpande')


# ── a Graph that answers, without a network ──────────────────────────
class FakeGraph:
    """Only the two calls this path makes."""

    def __init__(self, items=None, raises=False):
        self.items = items or []
        self.raises = raises
        self.calls = 0

    def list_item_attachments(self, mailbox, message_id):
        self.calls += 1
        if self.raises:
            raise RuntimeError('Graph said no')
        return list(self.items)


def _attached_message(*, sender=CLIENT, name='R Deshpande',
                      subject='RFQ — 40 MVA transformer',
                      body=ENQUIRY, html=False):
    return {
        '@odata.type': '#microsoft.graph.message',
        'subject': subject,
        'from': {'emailAddress': {'address': sender, 'name': name}},
        'toRecipients': [{'emailAddress': {
            'address': 'sales@procamlogistics.com', 'name': 'Sales'}}],
        'sentDateTime': '2026-11-01T09:14:00Z',
        'body': ({'contentType': 'html',
                  'content': f'<html><body><p>{body}</p></body></html>'}
                 if html else
                 {'contentType': 'text', 'content': body}),
        '_attachment_name': 'RFQ 40 MVA transformer.msg',
    }


def _outer(*, note='FYI, please quote.', attachments=True,
           sender=COLLEAGUE, subject='FW: RFQ'):
    return {
        'id': 'AAMkAD-outer',
        'internetMessageId': '<outer@procamlogistics.com>',
        'subject': subject,
        'from': {'emailAddress': {'address': sender, 'name': 'Operations'}},
        'toRecipients': [{'emailAddress': {
            'address': 'leads@procamgroup.in'}}],
        'receivedDateTime': '2026-11-01T09:20:00Z',
        'hasAttachments': attachments,
        'body': {'contentType': 'text', 'content': note},
        'bodyPreview': note,
        'internetMessageHeaders': [],
    }


# ── the gate, before any Graph call ──────────────────────────────────
def test_no_attachments_means_no_graph_call():
    graph = FakeGraph([_attached_message()])
    report = att.unwrap(_outer(attachments=False), graph, 'leads@x')
    assert report['spliced'] is False
    assert graph.calls == 0
    assert 'no attachments' in report['skipped']


def test_an_ordinary_inline_forward_is_left_alone():
    """The enquiry is already in the body. Fetching is waste, and
    splicing would hand the parser the same message twice."""
    inline = _outer(note=(
        '---------- Forwarded message ----------\n'
        f'From: R Deshpande <{CLIENT}>\n'
        'Subject: RFQ\n\n' + ENQUIRY))
    graph = FakeGraph([_attached_message()])
    report = att.unwrap(inline, graph, 'leads@x')
    assert report['spliced'] is False
    assert graph.calls == 0
    assert 'already carries' in report['skipped']


def test_a_graph_failure_loses_nothing():
    msg = _outer()
    report = att.unwrap(msg, FakeGraph(raises=True), 'leads@x')
    assert report['spliced'] is False
    # The outer message is untouched and still becomes a lead, exactly
    # as it did before this module existed.
    assert msg['body']['content'] == 'FYI, please quote.'


def test_a_file_only_attachment_finds_nothing():
    report = att.unwrap(_outer(), FakeGraph([]), 'leads@x')
    assert report['spliced'] is False
    assert 'no attached messages' in report['skipped']


# ── the splice ───────────────────────────────────────────────────────
def test_the_attached_enquiry_reaches_the_body():
    msg = _outer()
    report = att.unwrap(msg, FakeGraph([_attached_message()]), 'leads@x')
    assert report['spliced'] is True
    body = email_parser._get_body_text(msg)
    assert '40 MVA transformer' in body
    assert 'Nashik' in body
    # The covering note is kept: who relayed it and what they said are
    # things the CRM records.
    assert 'FYI, please quote.' in body


def test_the_lead_is_attributed_to_the_customer_not_the_colleague():
    """The whole point. Before this, `email` was the colleague."""
    msg = _outer()
    att.unwrap(msg, FakeGraph([_attached_message()]), 'leads@x')
    extracted = email_parser.extract_lead(msg)
    assert extracted['email'] == CLIENT
    assert COLLEAGUE not in (extracted['email'] or '')


def test_the_subject_becomes_the_enquiry_subject():
    msg = _outer(subject='FW: RFQ')
    att.unwrap(msg, FakeGraph([_attached_message()]), 'leads@x')
    extracted = email_parser.extract_lead(msg)
    assert '40 MVA' in (extracted['subject'] or '')


def test_the_signals_are_read_from_the_attached_message():
    msg = _outer()
    att.unwrap(msg, FakeGraph([_attached_message()]), 'leads@x')
    extracted = email_parser.extract_lead(msg)
    signals = extracted['signals']
    assert signals['cargo_keywords'], 'no cargo found in the enquiry'
    assert (signals.get('origin') or '').lower().startswith('nashik') \
        or 'nashik' in (extracted['body_text'] or '').lower()


def test_without_the_unwrap_the_colleague_is_the_lead():
    """The bug, asserted, so the fix cannot be quietly reverted."""
    msg = _outer()
    extracted = email_parser.extract_lead(msg)
    assert extracted['email'] == COLLEAGUE
    assert '40 MVA' not in (extracted['body_text'] or '')


def test_an_html_attached_body_is_read_as_text():
    msg = _outer()
    att.unwrap(msg, FakeGraph([_attached_message(html=True)]), 'leads@x')
    body = email_parser._get_body_text(msg)
    assert '40 MVA transformer' in body
    assert '<p>' not in body


def test_an_empty_covering_note_still_works():
    msg = _outer(note='')
    report = att.unwrap(msg, FakeGraph([_attached_message()]), 'leads@x')
    assert report['spliced'] is True
    assert '40 MVA transformer' in email_parser._get_body_text(msg)


# ── the two traps found while building it ────────────────────────────
def test_the_forward_cache_is_invalidated():
    """`analyze_forward` caches its verdict on msg['_forward'] and
    returns it unconditionally next time. Anything that asked before
    the splice cached "not a forward" against the covering note, and
    that stale answer would outlive the body — the splice would land
    and change nothing."""
    msg = _outer()
    before = email_parser.analyze_forward(msg)
    assert before['promoted'] is False
    assert isinstance(msg.get('_forward'), dict)

    att.unwrap(msg, FakeGraph([_attached_message()]), 'leads@x')
    assert '_forward' not in msg

    after = email_parser.analyze_forward(msg)
    assert after['promoted'] is True
    assert after['original_sender'] == CLIENT


def test_an_html_outer_body_does_not_eat_the_rendered_headers():
    """Appending plain text to an HTML body would leave the body-text
    conversion running an HTML strip over the rendered headers, which
    eats anything in angle brackets — including every address."""
    msg = _outer()
    msg['body'] = {'contentType': 'html',
                   'content': '<html><body><p>FYI</p></body></html>'}
    att.unwrap(msg, FakeGraph([_attached_message()]), 'leads@x')
    body = email_parser._get_body_text(msg)
    assert CLIENT in body, 'the address was stripped as a tag'


# ── several attached messages ────────────────────────────────────────
def test_two_attached_messages_are_counted_and_only_the_first_read():
    """Guessing which of a batch the lead is about would be worse than
    saying so."""
    items = [_attached_message(),
             _attached_message(sender='other@zzattach-two.example',
                               subject='RFQ — crane')]
    msg = _outer()
    report = att.unwrap(msg, FakeGraph(items), 'leads@x')
    assert report['spliced'] is True
    assert report['found'] == 2
    assert 'only the first' in report['skipped']
    assert len(report['messages']) == 2


def test_a_non_message_item_is_not_treated_as_an_enquiry():
    """An attached contact card or appointment. The Graph client
    filters these, so the service never sees them — asserted on the
    client, where the filter is."""
    import inspect

    from email_ingest.graph_client import GraphClient
    source = inspect.getsource(GraphClient.list_item_attachments)
    assert 'endswith("message")' in source


# ── what the report says ─────────────────────────────────────────────
def test_describe_names_the_real_sender_and_subject():
    got = att.describe(_attached_message())
    assert got['from'] == CLIENT
    assert '40 MVA' in got['subject']
    assert got['body_chars'] > 50
    assert got['attachment_name'].endswith('.msg')


def test_both_intake_paths_unwrap():
    """Both or neither: a backfill that built leads out of covering
    notes would be a second, quieter version of the same bug."""
    for path in ('email_ingest/single_message.py',
                 'email_ingest/pipeline.py'):
        source = open(os.path.join(_ROOT, path)).read()
        assert 'attached_message' in source, path
        assert '.unwrap(' in source, path


def test_the_check_script_cannot_create_anything():
    """§4's --check script. There is no --apply and no write."""
    import ast

    path = os.path.join(_ROOT, 'scripts',
                        '2026_11_03_attached_forwards.py')
    tree = ast.parse(open(path).read())

    options = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and getattr(node.func, 'attr', None) == 'add_argument'):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and \
                        isinstance(arg.value, str):
                    options.add(arg.value)
    assert '--check' in options
    assert '--apply' not in options

    # Nothing is added to a session and nothing is committed.
    called = {getattr(n.func, 'attr', None) for n in ast.walk(tree)
              if isinstance(n, ast.Call)}
    assert 'commit' not in called
    assert 'add' not in called
    assert 'delete' not in called
    assert 'rollback' in called
