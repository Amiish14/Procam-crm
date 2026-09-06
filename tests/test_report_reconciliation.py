"""Reports must reconcile with the database — §63, §64, §67.

"If Dashboard says Won = 100 but report says Won = 94, do not declare
deployment complete."

The audit found exactly that: 1,348 Won opportunities, 835 reaching the
report. The report was not wrong about what it counted — it silently
dropped what it could not attribute. These tests pin both halves of the
fix: link what can be linked, and show what cannot.
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
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'ReconTestOnly12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'recon.db')

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Company, Lead, Opportunity, Employee = (_main.Company, _main.Lead,
                                        _main.Opportunity, _main.Employee)


@pytest.fixture()
def books():
    """Six Won deals worth 600,000 — two of them unattributed."""
    with flask_app.app_context():
        db.create_all()
        Opportunity.query.delete()
        Lead.query.delete()
        Company.query.delete()
        db.session.commit()

        e = Employee.query.filter_by(emp_code='RECADM').first()
        if not e:
            e = Employee(emp_code='RECADM', name='Recon Admin')
            db.session.add(e)
        e.role, e.is_active, e.must_change_pw = 'admin', True, False
        e.is_super_admin, e.vertical = True, 'All'

        acme = Company(name='Acme Corp', is_active=True)
        db.session.add(acme)
        db.session.flush()

        lead = Lead(company='Acme Corp', company_id=acme.id, stage='New')
        db.session.add(lead)
        db.session.flush()

        for n in range(4):                       # linked, 100k each
            db.session.add(Opportunity(opp_number=f'W-{n}',
                                       company_id=acme.id, stage='Won',
                                       value_inr=100000,
                                       owner_emp_code='RECADM'))
        # raised from a lead that HAS a company, but never linked itself
        db.session.add(Opportunity(opp_number='W-orphan-1',
                                   lead_id=lead.id, stage='Won',
                                   value_inr=100000,
                                   owner_emp_code='RECADM'))
        # no lead, no company — genuinely unattributable
        db.session.add(Opportunity(opp_number='W-orphan-2', stage='Won',
                                   value_inr=100000,
                                   owner_emp_code='RECADM'))
        db.session.commit()
        return {'acme': acme.id, 'lead': lead.id}


def _c():
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code='RECADM', name='Recon Admin', role='admin',
                 vertical='All')
    return c


def _report_total(html):
    """Sum the Won Value column out of the rendered report.

    The template renders raw floats (500000.0), not formatted currency, so
    the pattern must not assume thousands separators.
    """
    import re
    cells = re.findall(r'<td[^>]*>\s*([\d,]+(?:\.\d+)?)\s*</td>', html)
    return sum(float(v.replace(',', '')) for v in cells)


def test_the_database_holds_six_hundred_thousand(books):
    with flask_app.app_context():
        total = sum(float(o.value_inr or 0) for o in
                    Opportunity.query.filter_by(stage='Won').all())
    assert total == 600000


def test_unattributed_value_is_shown_not_dropped(books):
    """§67 — the report must not disagree with the database in silence."""
    html = _c().get('/reports/won-value-by-account').get_data(as_text=True)
    assert 'not linked to an account' in html, \
        'unattributed Won value vanished from the report'
    assert _report_total(html) == 600000, \
        'the report total must reconcile with the database'


def test_inheriting_a_company_from_the_parent_lead(books):
    """An opportunity belongs to the company of the lead it came from.
    That is inheritance, not a guess."""
    with flask_app.app_context():
        orphan = Opportunity.query.filter_by(
            opp_number='W-orphan-1').first()
        assert orphan.company_id is None
        lead = Lead.query.get(orphan.lead_id)
        orphan.company_id = lead.company_id
        db.session.commit()
        assert orphan.company_id == books['acme']

    html = _c().get('/reports/won-value-by-account').get_data(as_text=True)
    assert _report_total(html) == 600000, 'total must still reconcile'
    assert 'not linked to an account' in html, \
        'the genuinely unattributable deal must still be visible'


def test_a_fully_linked_book_shows_no_unattributed_line(books):
    with flask_app.app_context():
        for o in Opportunity.query.filter(
                Opportunity.company_id.is_(None)).all():
            o.company_id = books['acme']
        db.session.commit()
    html = _c().get('/reports/won-value-by-account').get_data(as_text=True)
    assert 'not linked to an account' not in html
    assert _report_total(html) == 600000


def test_shared_opp_number_links_only_when_unambiguous(books):
    """The 2023 import dropped lead_id but kept opp_number on both sides.

    That shared reference is a real join — but only where every lead
    carrying the number agrees on the company. Where two leads disagree,
    picking one would attach a Won deal to the wrong account, which is
    exactly the silent corruption §65 warns about.
    """
    with flask_app.app_context():
        other = Company(name='Beta Ltd', is_active=True)
        db.session.add(other)
        db.session.flush()

        # agreed: two leads, same number, same company
        for n in range(2):
            db.session.add(Lead(company='Acme Corp', company_id=books['acme'],
                                opp_number='OPP-AGREE', stage='New'))
        agreed = Opportunity(opp_number='OPP-AGREE', stage='Won',
                             value_inr=1000, owner_emp_code='RECADM')

        # conflicted: two leads, same number, different companies
        db.session.add(Lead(company='Acme Corp', company_id=books['acme'],
                            opp_number='OPP-CLASH', stage='New'))
        db.session.add(Lead(company='Beta Ltd', company_id=other.id,
                            opp_number='OPP-CLASH', stage='New'))
        clash = Opportunity(opp_number='OPP-CLASH', stage='Won',
                            value_inr=1000, owner_emp_code='RECADM')

        db.session.add_all([agreed, clash])
        db.session.commit()

        by_number = {}
        for lead_id, number, company_id in db.session.query(
                Lead.id, Lead.opp_number, Lead.company_id).filter(
                Lead.opp_number.isnot(None)).all():
            by_number.setdefault((number or '').strip(), set()).add(company_id)

        usable = {k: next(iter(v)) for k, v in by_number.items()
                  if len(v) == 1 and next(iter(v)) is not None}

        assert 'OPP-AGREE' in usable, 'an agreed number should link'
        assert usable['OPP-AGREE'] == books['acme']
        assert 'OPP-CLASH' not in usable, \
            'a number mapping to two companies must not be resolved'
