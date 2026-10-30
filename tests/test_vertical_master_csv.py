"""
The vertical review master, as one CSV.

FINISH-TRACKER-CRM §2. TKT-0078 asked for the 296-lead master file.
The five review workbooks already exist and are the right shape for
somebody filling one in; they are no use to anybody who wants to
read the whole list, diff it against last week, or load it
elsewhere. This is that list, once, as a CSV.

It is a report. Nothing it does touches a lead, and no file appears
unless asked for — the tests below hold both.
"""
import importlib
import importlib.util
import os
import sys
import tempfile
from datetime import datetime

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'vmcsv-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'VmCsvTest-123456')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'vmcsv.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app = _main.app
db = _main.db
Lead = _main.Lead


def _script():
    path = os.path.join(_ROOT, 'scripts', 'vertical_master_csv.py')
    spec = importlib.util.spec_from_file_location('vmcsv', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


vm = _script()
ROWS = [
    {'lead_id': 11, 'company': 'Praj Industries', 'title': 'Sea job',
     'owner': 'EMP1', 'now': 'Heavy Transport',
     'looks_like': 'Sea Freight', 'confidence': 'high',
     'evidence': 'the words say sea'},
    {'lead_id': 9, 'company': 'Hacklin', 'title': 'Customs job',
     'owner': 'EMP2', 'now': 'Heavy Transport',
     'looks_like': 'Customs', 'confidence': 'low',
     'evidence': 'one word'},
]


@pytest.fixture
def surveyed(monkeypatch):
    """The survey, stubbed. What is being tested is the file, not
    the classifier — which has its own tests."""
    from app.services import vertical_audit
    monkeypatch.setattr(vertical_audit, 'survey',
                        lambda **kw: {'review': list(ROWS),
                                      'recorded': [],
                                      'changed_since': []})
    return ROWS


def test_the_rows_are_the_review_list_in_lead_order(surveyed):
    with flask_app.app_context():
        found = vm.rows()
        assert [r['lead_id'] for r in found] == [9, 11]


def test_the_answer_column_is_left_blank(surveyed):
    """The file exists to collect it. Pre-filling it would be
    answering the question the reviewer is being asked."""
    with flask_app.app_context():
        for row in vm.rows():
            assert row['correct_vertical'] == ''


def test_the_evidence_and_the_suggestion_come_through(surveyed):
    with flask_app.app_context():
        row = [r for r in vm.rows() if r['lead_id'] == 11][0]
        assert row['looks_like'] == 'Sea Freight'
        assert row['confidence'] == 'high'
        assert row['evidence'] == 'the words say sea'
        assert row['current_vertical'] == 'Heavy Transport'


def test_the_counts_are_per_suggestion_and_per_confidence(surveyed):
    with flask_app.app_context():
        total, by_suggestion, by_confidence = vm.counts(vm.rows())
        assert total == 2
        assert by_suggestion['Sea Freight'] == 1
        assert by_confidence['low'] == 1


def test_the_report_writes_nothing_by_itself(surveyed, tmp_path):
    with flask_app.app_context():
        text = vm.report(vm.rows())
        assert 'Nothing was written' in text
        assert 'docs/reports/vertical_master_' in text


def test_the_file_is_named_for_the_day():
    from datetime import date
    assert vm.filename(date(2026, 10, 29)).endswith(
        'vertical_master_2026_10_29.csv')
    assert vm.filename(date(2026, 10, 29)).startswith('docs/reports')


def test_writing_produces_a_readable_csv(surveyed, tmp_path):
    import csv
    with flask_app.app_context():
        found = vm.rows()
        path = vm.write(found, 'out.csv', root=str(tmp_path))
        with open(path, newline='', encoding='utf-8') as handle:
            got = list(csv.DictReader(handle))
        assert [r['lead_id'] for r in got] == ['9', '11']
        assert list(got[0].keys()) == list(vm.COLUMNS)
        assert got[0]['correct_vertical'] == ''


def test_the_report_names_the_file_once_written(surveyed, tmp_path):
    with flask_app.app_context():
        path = vm.write(vm.rows(), 'named.csv', root=str(tmp_path))
        assert 'Written: ' in vm.report(vm.rows(), path=path)


def test_nothing_is_written_to_any_lead(surveyed, tmp_path):
    with flask_app.app_context():
        db.create_all()
        lead = Lead(company='VM Untouched', stage='New Opportunity',
                    source='manual', procam_vertical='Heavy Transport',
                    created_at=datetime.utcnow())
        db.session.add(lead)
        db.session.commit()
        vm.write(vm.rows(), 'nothing.csv', root=str(tmp_path))
        assert db.session.get(
            Lead, lead.id).procam_vertical == 'Heavy Transport'
        Lead.query.filter(Lead.company == 'VM Untouched').delete(
            synchronize_session=False)
        db.session.commit()


def test_an_empty_review_list_is_said_plainly(monkeypatch):
    from app.services import vertical_audit
    monkeypatch.setattr(vertical_audit, 'survey',
                        lambda **kw: {'review': [], 'recorded': [],
                                      'changed_since': []})
    with flask_app.app_context():
        assert 'Nothing awaiting' in vm.report(vm.rows())


def test_the_command_line_writes_nothing_without_write(surveyed,
                                                       tmp_path):
    import contextlib
    import io
    out = io.StringIO()
    old = sys.argv
    sys.argv = ['vertical_master_csv.py']
    try:
        with contextlib.redirect_stdout(out):
            code = vm.main()
    finally:
        sys.argv = old
    assert code == 0
    assert 'Nothing was written' in out.getvalue()
