"""Training Academy — §73-81.

§77 is the one that could do damage: "Training records must not affect
Production Dashboard, KPI, Funnel, Reports, My Work, Customer data."
That is tested by taking a full census of the production tables before and
after a learner completes every level.
"""
import os
import sys
import json
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'TrainTestOnly12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'train.db')

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db, Employee = _main.app, _main.db, _main.Employee
flask_app.config['WTF_CSRF_ENABLED'] = False

from app.models.training import (TrainingProgress,          # noqa: E402
                                 Certificate, TrainingStatus)
from app.training import service as tr                      # noqa: E402
from app.training.content import LEVELS, BY_KEY, PASS_MARK  # noqa: E402

PRODUCTION_TABLES = ['leads', 'companies', 'contacts', 'opportunities',
                     'task_instances', 'lead_activities',
                     'account_relationship_tags', 'notifications']


@pytest.fixture()
def learner():
    with flask_app.app_context():
        db.create_all()
        Certificate.query.delete()
        TrainingProgress.query.delete()
        for code, sup in (('TRADM', True), ('TRUSER', False)):
            e = Employee.query.filter_by(emp_code=code).first()
            if not e:
                e = Employee(emp_code=code, name=f'Learner {code}')
                db.session.add(e)
            e.role = 'admin' if sup else 'user'
            e.is_active, e.must_change_pw = True, False
            e.is_super_admin, e.vertical = sup, 'All'
        db.session.commit()
    return True


def _c(code='TRUSER'):
    c = flask_app.test_client()
    with flask_app.app_context():
        role = Employee.query.filter_by(emp_code=code).first().role
    with c.session_transaction() as s:
        s.update(emp_code=code, name=code, role=role, vertical='All')
    return c


def _census():
    from sqlalchemy import text
    out = {}
    with flask_app.app_context():
        names = set(db.inspect(db.engine).get_table_names())
        for table in PRODUCTION_TABLES:
            if table not in names:
                continue
            with db.engine.connect() as conn:
                out[table] = conn.execute(
                    text(f'SELECT COUNT(*) FROM "{table}"')).scalar()
    return out


def _complete(code, level):
    """Do a level properly: read, practise, pass."""
    tr.mark_learned(code, level['key'])
    practice = level['practice']
    answer = 0 if practice['kind'] == 'number' else practice['answer']
    tr.check_practice(code, level['key'], answer)
    return tr.check_quiz(code, level['key'],
                         [q[2] for q in level['quiz']])


# ── §75: ten levels ──────────────────────────────────────────────────
def test_there_are_ten_levels(learner):
    assert len(LEVELS) == 10
    for level in LEVELS:
        assert level['sections'] and level['practice'] and level['quiz']


# ── §74: learn → practise → validate, in that order ──────────────────
def test_a_level_cannot_be_passed_without_the_exercise(learner):
    """Answering the quiz alone must not complete a level."""
    with flask_app.app_context():
        level = LEVELS[0]
        result = tr.check_quiz('TRUSER', level['key'],
                               [q[2] for q in level['quiz']])
        assert result['score'] == 100
        assert result['passed'] is False
        assert result['needs_practice'] is True

        row = TrainingProgress.query.filter_by(
            emp_code='TRUSER', level_key=level['key']).first()
        assert row.status != TrainingStatus.COMPLETED


def test_a_wrong_quiz_does_not_pass(learner):
    with flask_app.app_context():
        level = BY_KEY['company_people']
        tr.mark_learned('TRUSER', level['key'])
        tr.check_practice('TRUSER', level['key'], level['practice']['answer'])
        wrong = [(q[2] + 1) % len(q[1]) for q in level['quiz']]
        result = tr.check_quiz('TRUSER', level['key'], wrong)
        assert result['score'] < PASS_MARK
        assert result['passed'] is False


def test_doing_it_properly_completes_the_level(learner):
    with flask_app.app_context():
        result = _complete('TRUSER', LEVELS[0])
        assert result['passed'] is True
        row = TrainingProgress.query.filter_by(
            emp_code='TRUSER', level_key=LEVELS[0]['key']).first()
        assert row.status == TrainingStatus.COMPLETED
        assert row.completed_at is not None


def test_the_best_score_is_kept_across_attempts(learner):
    with flask_app.app_context():
        level = BY_KEY['leads']
        tr.mark_learned('TRUSER', level['key'])
        tr.check_practice('TRUSER', level['key'], level['practice']['answer'])
        tr.check_quiz('TRUSER', level['key'],
                      [(q[2] + 1) % len(q[1]) for q in level['quiz']])
        tr.check_quiz('TRUSER', level['key'], [q[2] for q in level['quiz']])
        row = TrainingProgress.query.filter_by(
            emp_code='TRUSER', level_key=level['key']).first()
        assert row.quiz_score == 100
        assert row.attempts >= 3


# ── §77: production data is untouched ────────────────────────────────
def test_completing_every_level_writes_nothing_to_production(learner):
    """The guarantee that matters. A census before and after."""
    before = _census()
    with flask_app.app_context():
        for level in LEVELS:
            _complete('TRUSER', level)
        tr.issue_certificate('TRUSER', 'Learner TRUSER')
    after = _census()
    assert before == after, (
        'training changed production tables: '
        + str({k: (before[k], after[k]) for k in before
               if before[k] != after[k]}))


def test_training_rows_live_only_in_training_tables(learner):
    """Training writes to its own tables and nowhere else.

    Asserts no CHANGE rather than an empty database: every test module
    shares one application instance, so production tables may already hold
    rows from elsewhere. What must never happen is training adding to
    them.
    """
    from app import Lead, Company, Opportunity
    with flask_app.app_context():
        before = (Lead.query.count(), Company.query.count(),
                  Opportunity.query.count())
        progress_before = TrainingProgress.query.count()

        _complete('TRUSER', LEVELS[0])

        assert TrainingProgress.query.count() > progress_before
        after = (Lead.query.count(), Company.query.count(),
                 Opportunity.query.count())
        assert before == after, \
            f'training touched production tables: {before} → {after}'


# ── §79: certification ───────────────────────────────────────────────
def test_no_certificate_until_every_level_is_done(learner):
    with flask_app.app_context():
        _complete('TRUSER', LEVELS[0])
        cert, problem = tr.issue_certificate('TRUSER', 'Learner')
        assert cert is None
        assert 'still to complete' in problem


def test_certificate_is_issued_and_is_stable(learner):
    with flask_app.app_context():
        for level in LEVELS:
            _complete('TRUSER', level)
        cert, problem = tr.issue_certificate('TRUSER', 'Learner TRUSER')
        assert cert is not None and problem is None
        assert cert.levels_passed == len(LEVELS)
        assert cert.average_score == 100
        assert cert.certificate_id.startswith('PCM-')

        again, _p = tr.issue_certificate('TRUSER', 'Learner TRUSER')
        assert again.certificate_id == cert.certificate_id, \
            'a second visit must not mint a new certificate'
        assert Certificate.query.count() == 1


def test_the_certificate_page_prints(learner):
    with flask_app.app_context():
        for level in LEVELS:
            _complete('TRUSER', level)
    body = _c().get('/academy/certificate').get_data(as_text=True)
    assert 'PCM-' in body
    assert 'window.print()' in body


# ── §80: the management view ─────────────────────────────────────────
def test_management_dashboard_counts_the_team(learner):
    with flask_app.app_context():
        for level in LEVELS:
            _complete('TRUSER', level)
        tr.issue_certificate('TRUSER', 'Learner TRUSER')
        view = tr.management_view()
    assert view['total'] >= 2
    assert view['certified'] == 1
    me = [p for p in view['people'] if p['emp_code'] == 'TRUSER'][0]
    assert me['percent'] == 100 and me['certified'] is True


def test_management_view_needs_admin(learner):
    assert _c('TRUSER').get('/admin/training').status_code == 403
    assert _c('TRADM').get('/admin/training').status_code == 200


# ── §73: the pages render ────────────────────────────────────────────
def test_academy_pages_render(learner):
    c = _c()
    assert c.get('/academy').status_code == 200
    for level in LEVELS:
        assert c.get(f'/academy/{level["key"]}').status_code == 200


def test_anonymous_is_redirected(learner):
    assert flask_app.test_client().get('/academy').status_code == 302
