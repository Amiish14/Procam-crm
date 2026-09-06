"""Data Quality dashboard — §66.

A dashboard that reports the wrong number is worse than none, so each
check is tested against a known database state.
"""
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'DQTestOnly12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'dq.db')

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Company, Lead, Opportunity, Employee = (_main.Company, _main.Lead,
                                        _main.Opportunity, _main.Employee)

from app.data_quality import service as dq                  # noqa: E402


@pytest.fixture()
def world():
    with flask_app.app_context():
        db.create_all()
        Opportunity.query.delete()
        Lead.query.delete()
        Company.query.delete()
        Employee.query.filter(Employee.emp_code.like('DQ%')).delete(
            synchronize_session=False)
        db.session.commit()

        here = Employee(emp_code='DQHERE', name='Still Here', role='admin',
                        is_active=True, must_change_pw=False,
                        is_super_admin=True, vertical='All')
        gone = Employee(emp_code='DQGONE', name='Left Us', role='user',
                        is_active=False, must_change_pw=False)
        db.session.add_all([here, gone])

        acme = Company(name='Acme', is_active=True, pic_emp_code='DQHERE')
        orphan_co = Company(name='No Owner Ltd', is_active=True)
        db.session.add_all([acme, orphan_co])
        db.session.flush()

        db.session.add(Lead(company='Acme', company_id=acme.id,
                            assigned_to='DQHERE', stage='New'))
        db.session.add(Lead(company='Acme', company_id=acme.id,
                            assigned_to='', stage='New'))
        db.session.add(Lead(company='Acme', company_id=acme.id,
                            assigned_to='DQGONE', stage='New'))
        db.session.add(Lead(company='Unlinked Co', stage='New',
                            assigned_to='DQHERE'))

        db.session.add(Opportunity(opp_number='DQ-1', company_id=acme.id,
                                   stage='Won', value_inr=100,
                                   owner_emp_code='DQHERE'))
        db.session.add(Opportunity(opp_number='DQ-2', company_id=acme.id,
                                   stage='Won', owner_emp_code='DQHERE'))
        db.session.add(Opportunity(opp_number='DQ-3', stage='Lost',
                                   owner_emp_code=''))
        db.session.commit()
        return {'acme': acme.id}


def test_unowned_leads_counted(world):
    with flask_app.app_context():
        assert dq.check_unowned_leads()[0] == 1


def test_leads_of_leavers_counted(world):
    with flask_app.app_context():
        assert dq.check_leads_of_leavers()[0] == 1


def test_unowned_opportunities_counted(world):
    with flask_app.app_context():
        assert dq.check_unowned_opportunities()[0] == 1


def test_unlinked_records_counted(world):
    with flask_app.app_context():
        assert dq.check_unlinked_leads()[0] == 1
        assert dq.check_unlinked_opportunities()[0] == 1


def test_won_without_value_counted(world):
    """A Won deal with no value counts as zero in every report."""
    with flask_app.app_context():
        assert dq.check_won_without_value()[0] == 1


def test_companies_without_owner_counted(world):
    with flask_app.app_context():
        assert dq.check_companies_without_pic()[0] == 1


def test_duplicate_companies_detected(world):
    with flask_app.app_context():
        assert dq.check_duplicate_companies()[0] == 0
        db.session.add(Company(name='Acme Ltd.', is_active=True))
        db.session.commit()
        assert dq.check_duplicate_companies()[0] == 1, \
            'Acme and Acme Ltd. normalise the same and should be flagged'


def test_summary_reports_every_check(world):
    with flask_app.app_context():
        rows = dq.summary()
    assert len(rows) == len(dq.CHECKS)
    for row in rows:
        assert row['label'] and row['why'] and row['severity']
        assert 'count' in row


def test_a_broken_check_does_not_break_the_page(world):
    """One failing check must not take the dashboard down with it."""
    original = dq.CHECKS[:]
    dq.CHECKS.append(('boom', 'Deliberately broken', 'testing', 'info', '/',
                      lambda: (_ for _ in ()).throw(RuntimeError('boom'))))
    try:
        with flask_app.app_context():
            rows = dq.summary()
        broken = [r for r in rows if r['key'] == 'boom'][0]
        assert broken['count'] is None and broken['error']
    finally:
        dq.CHECKS[:] = original


def test_page_requires_admin(world):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code='DQHERE', name='Still Here', role='admin',
                 vertical='All')
    assert c.get('/admin/data-quality').status_code == 200

    anon = flask_app.test_client()
    assert anon.get('/admin/data-quality').status_code in (302, 401, 403)
