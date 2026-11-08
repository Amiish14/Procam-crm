"""The master on every quote, and no annexure still saying DRAFT.

CRM-FINAL-CLOSEOUT-01 §4.

The decision worth testing is the one about Sea Freight. "Nothing
should still say DRAFT" and "do not invent contract language" are
both instructions, and the only way to hold both is to retire the
placeholder so the vertical prints the Procam Standard T&C alone.
So these tests pin: the retirement supersedes rather than deletes,
the master itself can never be retired, and a quote in a retired
vertical still prints real terms.
"""
import importlib
import importlib.util
import os
import sys
import tempfile
from datetime import date

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'draft-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'Draft-123456789')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'draft.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app = _main.app
db = _main.db

from app.models.quote_terms import (ANNEXURE, MASTER,          # noqa: E402
                                    QuoteTermSet)
from app.services import quote_terms as qt                     # noqa: E402

SCRIPT = os.path.join(_ROOT, 'scripts', '2026_11_08_no_draft_terms.py')

PLACEHOLDER = ('DRAFT — the clause that says how much free time the '
               'client gets before storage is charged.')
REAL = ('Free time: 7 days from arrival at the port of discharge. '
        'Storage thereafter is charged at actuals.')
MASTER_BODY = ('Validity: 15 days from the quote date.\n'
               'Payment: 30 days from the date of invoice.')


def _script():
    spec = importlib.util.spec_from_file_location('nodraft', SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def clean():
    """Term sets *and* their audit rows.

    SQLite reuses row ids after a delete, so an audit row left behind
    by an earlier test is found by `entity_id` under a later test's
    term set — and the count, which is the point of that assertion,
    comes back as three.
    """
    with flask_app.app_context():
        db.create_all()
        _purge()
        yield
        _purge()


def _purge():
    from app.models.audit import AuditEvent

    QuoteTermSet.query.delete()
    AuditEvent.query.filter(
        AuditEvent.entity_type == 'quote_term_set').delete(
            synchronize_session=False)
    db.session.commit()


def _master():
    row = qt.publish_master(MASTER_BODY, actor='ZD_TEST',
                            effective_from=date(2026, 11, 2))
    db.session.commit()
    return row


def _annexure(vertical, body):
    row = qt.publish(vertical, body, actor='ZD_TEST')
    db.session.commit()
    return row


# ── finding the markers ──────────────────────────────────────────────
def test_a_placeholder_is_found_and_real_wording_is_not():
    with flask_app.app_context():
        mod = _script()
        _master()
        _annexure('Sea Freight', PLACEHOLDER)
        _annexure('Project Cargo', REAL)
        state = mod.survey()
        assert [r.vertical for r in state['drafts']] == ['Sea Freight']


def test_the_word_deep_inside_a_real_clause_is_not_a_placeholder():
    """"a draft bill of lading" is wording, not a marker."""
    with flask_app.app_context():
        mod = _script()
        _master()
        filler = 'Free time: 7 days from arrival. ' * 20
        _annexure('Sea Freight',
                  filler + 'A draft bill of lading is issued on '
                           'receipt of the shipping instructions.')
        assert mod.survey()['drafts'] == []


def test_the_master_saying_draft_is_reported_and_not_retired():
    with flask_app.app_context():
        mod = _script()
        qt.publish_master('DRAFT — the standard terms.', actor='ZD_TEST')
        db.session.commit()
        state = mod.survey()
        assert state['master_draft'] is True
        # It is not in the list of things to retire.
        assert state['drafts'] == []


# ── retiring ─────────────────────────────────────────────────────────
def test_retiring_supersedes_and_never_deletes():
    """A quote printed under version 1 must still be able to say what
    it printed under."""
    with flask_app.app_context():
        _master()
        row = _annexure('Sea Freight', PLACEHOLDER)
        rid = row.id
        qt.retire('Sea Freight', actor='ZD_TEST')
        db.session.commit()
        kept = QuoteTermSet.query.get(rid)
        assert kept is not None
        assert kept.is_current is False
        assert kept.superseded_at is not None
        assert kept.superseded_by_id is None, (
            'retired is not replaced — a successor id would point at '
            'terms that do not exist')


def test_a_retired_vertical_prints_the_master_alone():
    with flask_app.app_context():
        master = _master()
        _annexure('Sea Freight', PLACEHOLDER)
        qt.retire('Sea Freight', actor='ZD_TEST')
        db.session.commit()
        assert qt.current_for('Sea Freight') is None
        assert qt.master_current().id == master.id


def test_the_master_cannot_be_retired():
    """A quote with no master prints no standard terms at all."""
    with flask_app.app_context():
        _master()
        with pytest.raises(qt.TermsError):
            qt.retire('', actor='ZD_TEST')
        with pytest.raises(qt.TermsError):
            qt.retire(None, actor='ZD_TEST')
        assert qt.master_current() is not None


def test_retiring_a_vertical_with_no_annexure_is_refused():
    with flask_app.app_context():
        _master()
        with pytest.raises(qt.TermsError):
            qt.retire('Warehousing', actor='ZD_TEST')


def test_retiring_leaves_an_audit_row_saying_why():
    with flask_app.app_context():
        from app.models.audit import AuditEvent

        _master()
        row = _annexure('Sea Freight', PLACEHOLDER)
        qt.retire('Sea Freight', actor='ZD_TEST', reason='placeholder')
        db.session.commit()
        events = AuditEvent.query.filter_by(
            action='quote_terms.retired',
            entity_id=str(row.id)).all()
        assert len(events) == 1
        assert events[0].new_value['superseded_by'] is None


def test_a_new_annexure_can_still_be_published_afterwards():
    """Retirement is not a dead end — the day the commercial team
    writes the clauses, publishing works as it always did."""
    with flask_app.app_context():
        _master()
        _annexure('Sea Freight', PLACEHOLDER)
        qt.retire('Sea Freight', actor='ZD_TEST')
        db.session.commit()
        fresh = _annexure('Sea Freight', REAL)
        assert fresh.is_current is True
        assert qt.current_for('Sea Freight').body == REAL


# ── the script ───────────────────────────────────────────────────────
def test_the_dry_run_retires_nothing():
    with flask_app.app_context():
        mod = _script()
        _master()
        _annexure('Sea Freight', PLACEHOLDER)
        assert mod.main(['--check']) == 0
        db.session.expire_all()
        assert qt.current_for('Sea Freight') is not None


def test_the_apply_retires_it():
    """The apply path, not only the refusal."""
    with flask_app.app_context():
        mod = _script()
        _master()
        _annexure('Sea Freight', PLACEHOLDER)
        assert mod.main(['--apply']) == 0
        db.session.expire_all()
        assert qt.current_for('Sea Freight') is None
        assert qt.master_current() is not None


def test_the_apply_is_a_no_op_when_nothing_says_draft():
    with flask_app.app_context():
        mod = _script()
        _master()
        _annexure('Project Cargo', REAL)
        assert mod.main(['--apply']) == 0
        db.session.expire_all()
        assert qt.current_for('Project Cargo') is not None


def test_the_script_never_edits_a_body():
    """Stripping the word and leaving the prose is the one change
    that makes a placeholder look approved."""
    import ast

    tree = ast.parse(open(SCRIPT).read())
    written = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute):
                    written.add(target.attr)
    assert 'body' not in written
    assert 'title' not in written
    assert not written, f'the report writes attributes: {sorted(written)}'


# ── the half that was already built ──────────────────────────────────
def test_the_wiring_check_finds_the_four_places():
    mod = _script()
    wired = mod.wiring()
    assert set(wired) == {
        'the builder stamps the master',
        'the quote screen prints it',
        'the emailed quotation prints it',
        'the job handover carries it'}
    for label, ok in wired.items():
        assert ok is True, f'{label} is no longer wired'
