"""Every message the ingest saw, including the ones it dropped.

CRM-USER-ISSUES-02NOV §4: "Mail forwarded to leads@procamgroup.in never
reaches the CRM … Add an 'unprocessed mail' log: every message the
poller saw and didn't turn into a lead or a timeline entry, with the
reason. Admin can view it and re-process one."

The log, the screen and the re-process button all already existed —
`mail_ingest_log`, /admin/mail-ingest, and `create-lead`. What did not
hold was the promise `_record_ingest` makes in its own docstring: "one
row per message, whatever happened". Four paths in
`process_single_message` returned without writing one, and the whole
of `pipeline.py` never wrote one at all.

The worst of the four was the sender denylist. `blocklist.check` drops
by domain from data/blocked_senders.txt and by local part on *any*
domain — `_BULK_LOCAL_RE` matches mailer@, bulk@, broadcast@, digest@,
campaign@, offers@ and deals@. A real enquiry relayed from an address
shaped like that was dropped with no trace anywhere: no lead, no
timeline entry, no log row. From the outside that is indistinguishable
from mail that never arrived, which is exactly how §4 was reported.
"""
import ast
import importlib
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'ingestlog-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'IngestLog-12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'ingestlog.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app = _main.app
db = _main.db

from email_ingest import blocklist                          # noqa: E402

SINGLE = os.path.join(_ROOT, 'email_ingest', 'single_message.py')
POLL = os.path.join(_ROOT, 'email_ingest', 'pipeline.py')


# ── the structural guarantee ─────────────────────────────────────────
#
# The real test would drive Microsoft Graph, which these tests cannot
# reach. So the promise is checked where it is actually made: every
# early `return` in the ingest must have a _record_ingest call in the
# same branch. That is the shape of the bug — four returns that did
# not — and it is the shape a future edit would reintroduce.
def _functions(path):
    tree = ast.parse(open(path).read())
    return {n.name: n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef)}


def _logs(node):
    """Whether this subtree calls _record_ingest."""
    return any(
        isinstance(n, ast.Call)
        and (getattr(n.func, 'id', None) == '_record_ingest'
             or getattr(n.func, 'attr', None) == '_record_ingest')
        for n in ast.walk(node))


def _returns_without_logging(path, func_name):
    """Early returns in `func_name` whose branch logs nothing.

    A branch that returns the result of a helper which logs for itself
    counts as logged — `_attach_outbound` is the real case — and the
    helper is checked rather than assumed, so this cannot be used to
    wave a branch through.
    """
    funcs = _functions(path)
    target = funcs.get(func_name)
    assert target is not None, f'{func_name} not found in {path}'

    bad = []
    for node in ast.walk(target):
        # Only branches: an `if` whose body returns is the shape that
        # dropped mail silently.
        if not isinstance(node, ast.If):
            continue
        returns = [n for n in node.body if isinstance(n, ast.Return)]
        if not returns:
            continue
        if _logs(ast.Module(body=node.body, type_ignores=[])):
            continue
        # `return _helper(...)` where _helper writes the row itself.
        delegated = False
        for ret in returns:
            value = ret.value
            if isinstance(value, ast.Call):
                name = (getattr(value.func, 'id', None)
                        or getattr(value.func, 'attr', None))
                helper = funcs.get(name or '')
                if helper is not None and _logs(helper):
                    delegated = True
        if delegated:
            continue
        bad.append(returns[0].lineno)
    return bad


def test_the_webhook_logs_every_branch_it_returns_from():
    bad = _returns_without_logging(SINGLE, 'process_single_message')
    assert not bad, (
        f'lines {bad} return without writing a mail_ingest_log row. '
        f'That is §4 exactly: a message the CRM dropped looks the '
        f'same as one that never arrived.')


def test_the_poll_path_writes_the_log_at_all():
    """It never wrote a single row. A backfill run processed mail and
    left no trace of what it decided."""
    source = open(POLL).read()
    assert 'def _record_ingest(' in source
    # Not merely defined — called, and more than once.
    tree = ast.parse(source)
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and getattr(n.func, 'id', None) == '_record_ingest']
    assert len(calls) >= 6, (
        f'only {len(calls)} call(s); the poll path skips for at least '
        f'six distinct reasons and each needs its own row')


def test_the_created_leads_are_logged_too_not_only_the_refusals():
    """A screen listing only refusals cannot answer "did our enquiry
    reach the CRM?", which is the first question the log exists for."""
    source = open(POLL).read()
    assert "_record_ingest('created'" in source


# ── the denylist, which is where the silence was ─────────────────────
def test_the_bulk_local_rule_matches_shapes_a_real_sender_can_have():
    """Not a complaint about the rule — a note on what it costs. These
    all drop, on any domain, so the log row is the only way anybody
    finds out."""
    for local in ('mailer', 'bulk', 'broadcast', 'digest', 'campaign',
                  'offers', 'deals'):
        assert blocklist.check(f'{local}@a-real-client.example'), local


def test_noreply_is_deliberately_not_blocked():
    """Procurement portals send genuine RFQs from no-reply@, and
    junk_suggest recorded two open opportunities among them."""
    assert blocklist.check('no-reply@tenders.example') is None
    assert blocklist.check('noreply@tenders.example') is None


def test_a_blocked_sender_is_logged_as_blocked_not_skipped():
    """Its own outcome, so the denylist's work is readable without
    reading past every "already ingested".

    Asked of the BRANCH rather than of a slice of characters after the
    denylist call. It was a 1400-character window, which is a proxy
    for "nearby" and broke the day a comment was added between the
    two -- reporting a silent drop that was not one, which is the one
    failure mode a guard like this must not have. The AST finds the
    `if blocked:` branch wherever it is and asks what it calls.
    """
    target = _functions(SINGLE).get('process_single_message')
    assert target is not None

    found = False
    for node in ast.walk(target):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if getattr(test, 'id', None) != 'blocked':
            continue
        for call in ast.walk(ast.Module(body=node.body, type_ignores=[])):
            if not isinstance(call, ast.Call):
                continue
            if getattr(call.func, 'id', None) != '_record_ingest':
                continue
            first = call.args[0] if call.args else None
            if isinstance(first, ast.Constant) and first.value == 'blocked':
                found = True
    assert found, (
        "the `if blocked:` branch does not call _record_ingest('blocked') "
        "— the denylist's work would be unreadable among the skips")


def test_the_row_carries_the_pattern_that_matched():
    """So the rule can be corrected rather than guessed at."""
    assert 'blocked sender domain: ' in (
        blocklist.check('somebody@' + _a_blocked_domain()) or '')


def _a_blocked_domain():
    names = sorted(blocklist.blocked_domains())
    if not names:
        pytest.skip('no blocked domains configured in this checkout')
    return names[0]


# ── re-processing has to actually work ───────────────────────────────
def test_force_overrides_the_denylist():
    """§4 asks for "re-process one". Without this the rows this change
    adds to the screen could be read and never acted on: the button
    would come straight back to the denylist and skip again."""
    source = open(SINGLE).read()
    assert 'blocklist.check(sender_email) if not force else None' in source


def test_force_still_overrides_the_system_sender_block():
    """The sibling rule this one was made to match. Guarding the
    regression in both directions."""
    source = open(SINGLE).read()
    assert 'if not force:' in source
    assert 'system_senders' in source


# ── the outcomes the screen can show ─────────────────────────────────
def test_blocked_is_a_known_outcome():
    from app.models.ingest_log import BLOCKED, OUTCOMES, SKIPPED
    assert BLOCKED in OUTCOMES and SKIPPED in OUTCOMES


def test_the_screen_is_reachable_for_an_admin():
    from app import Employee

    with flask_app.app_context():
        db.create_all()
        Employee.query.filter_by(emp_code='ZI_ADM').delete()
        db.session.add(Employee(
            emp_code='ZI_ADM', name='Admin Person', password_hash='x',
            role='admin', email='zi.adm@procamlogistics.com',
            is_active=True))
        db.session.commit()
    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess['emp_code'] = 'ZI_ADM'
        sess['role'] = 'admin'
    r = client.get('/admin/mail-ingest')
    assert r.status_code == 200, r.status_code
    with flask_app.app_context():
        Employee.query.filter_by(emp_code='ZI_ADM').delete()
        db.session.commit()


# ── the things §4 asked me to check that are NOT faults ──────────────
def test_an_html_only_body_is_read_as_text():
    """§4 lists HTML-only bodies as a candidate. They are handled."""
    from email_ingest import parser

    msg = {'body': {'contentType': 'html',
                    'content': '<html><body><p>40 MVA transformer, '
                               'Nashik to Bhopal</p></body></html>'}}
    text = parser._get_body_text(msg)
    assert '40 MVA transformer' in text
    assert '<p>' not in text


def test_the_graph_query_asks_for_the_threading_headers():
    """§4 lists the filters as a candidate. The headers are requested;
    without them In-Reply-To and References are always empty and a
    reply can only be recognised by its subject line."""
    from email_ingest.graph_client import DEFAULT_SELECT

    assert 'internetMessageHeaders' in DEFAULT_SELECT
    assert 'conversationId' in DEFAULT_SELECT


def test_the_not_relevant_rules_are_not_the_ingest_denylist():
    """§4 asks about "the 14 rules added this week". Those went to the
    Not-relevant classes, which classify a lead that has already been
    created. They cannot drop mail, and the module refuses a pattern
    matching a Procam domain precisely because a forward is how most
    real enquiries arrive."""
    from app.services import not_relevant_senders as nrs

    assert nrs.LIST_KEY == 'not_relevant_sender'
    assert nrs.why_not_a_pattern('*@procamlogistics.com')
    assert nrs.why_not_a_pattern('procamgroup.in')
