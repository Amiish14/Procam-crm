"""Bulk lead administration — §52-58.

This is the only code in the CRM that destroys records, so the tests are
mostly about what it REFUSES to do: delete history, delete without a
reason, or let anyone but the super admin delete at all.
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
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'BulkTestOnly12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'bulk.db')

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Company, Lead, Opportunity = _main.Company, _main.Lead, _main.Opportunity
LeadActivity, Employee = _main.LeadActivity, _main.Employee
flask_app.config['WTF_CSRF_ENABLED'] = False

from app.bulk_admin import service as bulk                # noqa: E402
from app.models.audit import DeletionAudit                # noqa: E402
from app.master_data import service as md                 # noqa: E402


@pytest.fixture()
def leads():
    with flask_app.app_context():
        db.create_all()
        md.ensure_lists()
        md.add_item('vertical', 'Heavy Transport', 'Heavy Transport')
        DeletionAudit.query.delete()
        LeadActivity.query.delete()
        Opportunity.query.delete()
        Lead.query.delete()
        Company.query.delete()

        for code, sup in (('BADMIN', True), ('BSTAFF', False)):
            e = Employee.query.filter_by(emp_code=code).first()
            if not e:
                e = Employee(emp_code=code, name=code)
                db.session.add(e)
            e.role = 'admin' if sup else 'user'
            e.is_active, e.must_change_pw = True, False
            e.is_super_admin, e.vertical = sup, 'All'
        db.session.commit()

        acme = Company(name='Acme', is_active=True)
        db.session.add(acme)
        db.session.flush()

        plain = Lead(company='Plain Co', stage='New')
        withhist = Lead(company='History Co', stage='New')
        withwon = Lead(company='Won Co', stage='New')
        db.session.add_all([plain, withhist, withwon])
        db.session.flush()

        db.session.add(LeadActivity(lead_id=withhist.id, kind='call',
                                    subject='talked'))
        db.session.add(Opportunity(opp_number='B-1', lead_id=withwon.id,
                                   company_id=acme.id, stage='Won',
                                   value_inr=1000))
        db.session.commit()
        return {'plain': plain.id, 'hist': withhist.id, 'won': withwon.id}


def _c(code):
    c = flask_app.test_client()
    with flask_app.app_context():
        e = Employee.query.filter_by(emp_code=code).first()
        role = e.role
    with c.session_transaction() as s:
        s.update(emp_code=code, name=code, role=role, vertical='All')
    return c


def _post(client, path, payload):
    return client.post(path, data=json.dumps(payload),
                       content_type='application/json')


# ── §54: archiving is the normal action ──────────────────────────────
def test_archive_hides_without_destroying(leads):
    with flask_app.app_context():
        bulk.archive([leads['plain']], 'no longer relevant', 'BADMIN')
        lead = Lead.query.get(leads['plain'])
        assert lead is not None, 'archiving must never delete'
        assert lead.is_archived is True
        assert lead.archive_reason == 'no longer relevant'
        assert lead.archived_by == 'BADMIN'
        assert lead.archived_at is not None


def test_archived_leads_leave_the_active_list(leads):
    with flask_app.app_context():
        bulk.archive([leads['plain']], 'tidy up', 'BADMIN')
    body = _c('BADMIN').get('/admin/leads').get_data(as_text=True)
    assert 'Plain Co' not in body
    archived = _c('BADMIN').get('/admin/leads?show=archived'
                                ).get_data(as_text=True)
    assert 'Plain Co' in archived


def test_archiving_is_reversible(leads):
    with flask_app.app_context():
        bulk.archive([leads['plain']], 'oops', 'BADMIN')
        bulk.unarchive([leads['plain']], 'BADMIN')
        assert Lead.query.get(leads['plain']).is_archived is False


# ── §56: deletion is refused where history exists ────────────────────
def test_deletion_is_refused_when_a_won_deal_is_linked(leads):
    """The safeguard that matters most — this is business Procam has done."""
    with flask_app.app_context():
        with pytest.raises(PermissionError) as exc:
            bulk.delete([leads['won']], 'cleanup', 'BADMIN')
        assert 'Won deal' in str(exc.value)
        assert Lead.query.get(leads['won']) is not None


def test_impact_is_reported_before_anything_happens(leads):
    r = _post(_c('BADMIN'), '/api/bulk/leads/impact',
              {'ids': [leads['won'], leads['hist']]})
    body = r.get_json()
    assert body['ok']
    assert body['linked']['opportunities'] == 1
    assert body['linked']['activities'] == 1
    assert body['delete_blocked'] is True
    assert body['reasons']


def test_a_lead_with_no_history_can_be_deleted(leads):
    with flask_app.app_context():
        out = bulk.delete([leads['plain']], 'duplicate entry', 'BADMIN')
        assert out['deleted'] == 1
        assert Lead.query.get(leads['plain']) is None


def test_deletion_requires_a_reason(leads):
    with flask_app.app_context():
        with pytest.raises(ValueError):
            bulk.delete([leads['plain']], '   ', 'BADMIN')
        assert Lead.query.get(leads['plain']) is not None


# ── §57: the audit survives the record ───────────────────────────────
def test_deletion_is_audited_with_a_snapshot(leads):
    with flask_app.app_context():
        bulk.delete([leads['plain']], 'test deletion', 'BADMIN')
        row = DeletionAudit.query.filter_by(action='delete').first()
        assert row is not None
        assert row.reason == 'test deletion'
        assert row.performed_by == 'BADMIN'
        assert row.performed_at is not None
        # after a permanent delete the snapshot is all that remains
        assert row.snapshot.get('company') == 'Plain Co'


def test_archiving_is_audited_too(leads):
    with flask_app.app_context():
        bulk.archive([leads['plain']], 'stale', 'BADMIN')
        assert DeletionAudit.query.filter_by(action='archive').count() == 1


# ── §55: only the super admin may delete ─────────────────────────────
def test_a_plain_admin_cannot_delete(leads):
    r = _post(_c('BSTAFF'), '/api/bulk/leads/delete',
              {'ids': [leads['plain']], 'reason': 'trying it on'})
    assert r.status_code == 403
    with flask_app.app_context():
        assert Lead.query.get(leads['plain']) is not None


def test_the_api_refuses_a_blocked_delete_with_409(leads):
    r = _post(_c('BADMIN'), '/api/bulk/leads/delete',
              {'ids': [leads['won']], 'reason': 'cleanup'})
    assert r.status_code == 409
    assert r.get_json()['blocked'] is True


# ── §53: the ordinary bulk actions ───────────────────────────────────
def test_bulk_assign(leads):
    with flask_app.app_context():
        out = bulk.assign([leads['plain'], leads['hist']], 'BSTAFF', 'BADMIN')
        assert out['assigned'] == 2
        assert Lead.query.get(leads['plain']).assigned_to == 'BSTAFF'


def test_bulk_assign_refuses_an_inactive_employee(leads):
    with flask_app.app_context():
        gone = Employee(emp_code='BGONE', name='Gone', is_active=False)
        db.session.add(gone)
        db.session.commit()
        with pytest.raises(ValueError):
            bulk.assign([leads['plain']], 'BGONE', 'BADMIN')


def test_bulk_vertical_is_validated_against_master_data(leads):
    with flask_app.app_context():
        bulk.set_field([leads['plain']], 'procam_vertical',
                       'Heavy Transport', 'BADMIN')
        assert Lead.query.get(leads['plain']).procam_vertical == \
            'Heavy Transport'
        with pytest.raises(ValueError):
            bulk.set_field([leads['plain']], 'procam_vertical',
                           'Invented Vertical', 'BADMIN')
