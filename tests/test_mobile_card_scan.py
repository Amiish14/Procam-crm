"""Mobile CRM and business-card capture — §33-41.

§35: "Card Scan must NOT be hidden inside multiple menus."
§37: "Never silently save extracted information."
§39: duplicate detection before anything is created.
§40: a scan should become work, not a filed card.
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
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'CardTestOnly12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'card.db')

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Company, Contact, Lead, Employee = (_main.Company, _main.Contact,
                                    _main.Lead, _main.Employee)
flask_app.config['WTF_CSRF_ENABLED'] = False

from app.models.business_card import BusinessCardImport     # noqa: E402
from presales.models import AccountRelationshipTag          # noqa: E402
from app.master_data import service as md                   # noqa: E402
from app.services.business_card_ocr import find_duplicates   # noqa: E402

_CSS = os.path.join(_ROOT, 'static', 'css', 'crm.css')
_TEMPLATES = os.path.join(_ROOT, 'templates')


@pytest.fixture()
def world():
    with flask_app.app_context():
        db.create_all()
        md.ensure_lists()
        for label in ('Customer', 'Competitor', 'Vendor'):
            md.add_item('relationship', label, label)
        BusinessCardImport.query.delete()
        AccountRelationshipTag.query.delete()
        Lead.query.delete()
        Contact.query.delete()
        Company.query.delete()

        e = Employee.query.filter_by(emp_code='CDADM').first()
        if not e:
            e = Employee(emp_code='CDADM', name='Card Admin')
            db.session.add(e)
        e.role, e.is_active, e.must_change_pw = 'admin', True, False
        e.is_super_admin, e.vertical = True, 'All'

        db.session.add(Company(name='Siemens Limited', is_active=True,
                               website='siemens.com'))
        db.session.commit()
        return True


def _c():
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code='CDADM', name='Card Admin', role='admin',
                 vertical='All')
    return c


def _card(fields):
    with flask_app.app_context():
        row = BusinessCardImport(extracted_json=fields, status='Pending',
                                 uploaded_by_id='CDADM')
        db.session.add(row)
        db.session.commit()
        return row.id


# ── §35: the action is reachable in one tap ──────────────────────────
def test_scan_is_a_button_on_my_work_not_a_menu_item():
    html = open(os.path.join(_TEMPLATES, 'my_work', 'home.html')).read()
    assert 'crm-fab' in html, 'no thumb-reachable scan button'
    assert 'business-cards/scan' in html


def test_the_scan_button_is_mobile_only():
    """A floating button on a desktop is clutter; there it belongs in the
    page header instead."""
    css = open(_CSS).read()
    assert '.crm-fab{' in css
    block = css[css.index('.crm-fab{'):]
    assert 'display:none' in block[:400], \
        'the floating button must be hidden by default'
    assert '@media (max-width:820px)' in css


def test_inputs_are_sized_to_stop_ios_zooming():
    """Anything under 16px makes iOS Safari zoom on focus, which throws
    the layout out mid-entry."""
    css = open(_CSS).read()
    mobile = css[css.index('@media (max-width:820px)'):]
    assert 'font-size:16px' in mobile


# ── §39: duplicates found before anything is created ─────────────────
def test_duplicate_company_is_found_by_name(world):
    with flask_app.app_context():
        dup = find_duplicates({'company': 'Siemens'}, db)
    assert any(a['name'] == 'Siemens Limited' for a in dup['accounts'])


def test_duplicate_company_is_found_by_website(world):
    with flask_app.app_context():
        dup = find_duplicates({'company': 'Unrelated Name',
                               'website': 'www.siemens.com'}, db)
    assert any(a['name'] == 'Siemens Limited' for a in dup['accounts'])


# ── §51: a card never creates a second company ───────────────────────
def test_saving_a_card_matches_an_existing_company(world):
    """"Siemens Ltd" on a card must attach to "Siemens Limited"."""
    card_id = _card({'name': 'R Shah', 'company': 'Siemens Ltd',
                     'email': 'r.shah@siemens.com'})
    r = _c().post(f'/api/business-cards/{card_id}/save',
                  data=json.dumps({'choice': 'new',
                                   'fields': {'name': 'R Shah',
                                              'company': 'Siemens Ltd',
                                              'email': 'r.shah@siemens.com'}}),
                  content_type='application/json')
    assert r.status_code == 200, r.get_data(as_text=True)
    with flask_app.app_context():
        assert Company.query.filter(
            Company.name.ilike('%siemens%')).count() == 1, \
            'the card created a duplicate company'
        contact = Contact.query.filter_by(name='R Shah').first()
        assert contact is not None
        assert contact.company_id is not None, \
            'the contact must link to Company Master by id, not by name'


# ── §38: classification at the point of capture ──────────────────────
def test_classifications_are_applied_on_save(world):
    card_id = _card({'name': 'A Rival', 'company': 'Rival Freight'})
    r = _c().post(f'/api/business-cards/{card_id}/save',
                  data=json.dumps({
                      'choice': 'new',
                      'fields': {'name': 'A Rival',
                                 'company': 'Rival Freight'},
                      'classifications': ['Competitor', 'Vendor']}),
                  content_type='application/json')
    assert r.status_code == 200
    with flask_app.app_context():
        company = Company.query.filter_by(name='Rival Freight').first()
        tags = {t.tag for t in AccountRelationshipTag.query.filter_by(
            account_id=company.id).all()}
    assert tags == {'Competitor', 'Vendor'}


def test_an_invented_classification_is_refused(world):
    card_id = _card({'name': 'X', 'company': 'Some Co'})
    r = _c().post(f'/api/business-cards/{card_id}/save',
                  data=json.dumps({
                      'choice': 'new',
                      'fields': {'name': 'X', 'company': 'Some Co'},
                      'classifications': ['Frenemy']}),
                  content_type='application/json')
    assert r.status_code == 400
    assert 'not a known relationship' in r.get_json()['error'].lower()


# ── §40: the scan becomes work ───────────────────────────────────────
def test_a_follow_up_creates_a_lead(world):
    card_id = _card({'name': 'P Kumar', 'company': 'New Prospect Ltd'})
    r = _c().post(f'/api/business-cards/{card_id}/save',
                  data=json.dumps({
                      'choice': 'new',
                      'fields': {'name': 'P Kumar',
                                 'company': 'New Prospect Ltd'},
                      'classifications': ['Customer'],
                      'follow_up_date': '2026-10-01',
                      'note': 'Met at the Mumbai expo'}),
                  content_type='application/json')
    assert r.status_code == 200
    assert r.get_json()['lead_id'] is not None
    with flask_app.app_context():
        lead = Lead.query.filter_by(
            company='New Prospect Ltd').first()
        assert lead is not None
        assert lead.source == 'business_card'
        assert lead.company_id is not None
        assert lead.assigned_to == 'CDADM'
        assert str(lead.followup_date) == '2026-10-01'
        assert 'Mumbai expo' in (lead.notes or '')


def test_saving_without_a_follow_up_creates_no_lead(world):
    """Not every card is a lead — filing a contact must stay possible."""
    card_id = _card({'name': 'Q Person', 'company': 'Quiet Co'})
    r = _c().post(f'/api/business-cards/{card_id}/save',
                  data=json.dumps({'choice': 'new',
                                   'fields': {'name': 'Q Person',
                                              'company': 'Quiet Co'}}),
                  content_type='application/json')
    assert r.status_code == 200
    assert r.get_json()['lead_id'] is None
    with flask_app.app_context():
        assert Lead.query.filter_by(company='Quiet Co').count() == 0
