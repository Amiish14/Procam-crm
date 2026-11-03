"""Archiving one lead, and the four things that were missing.

CRM-USER-ISSUES-02NOV §1: "There's no delete/archive option, even for
super admin."

The columns, the writer and the audit row all existed. Three things
did not, and there is a test here for each:

    * a way in for a person — the routes;
    * a reason that is actually required, in the service rather than
      in the browser, so every caller is held to it;
    * the exclusion from lists, which lived in thirty-three modules
      and not in `app/access/scope.leads` — the one function every
      board, count and report goes through. That is why an archived
      lead still appeared in the main lead list, and it is the half of
      this issue that no button would have fixed.

And one that must stay true: CRM-NO-DELETE-01. An archived lead is
still a row, still queryable, still restorable.
"""
import importlib
import json
import os
import sys
import tempfile
from datetime import datetime

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'archive-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'LeadArchive-12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'archive.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app = _main.app
flask_app.config['WTF_CSRF_ENABLED'] = False
db = _main.db
Lead = _main.Lead
Employee = _main.Employee

from app.access import scope as access_scope                # noqa: E402
from app.models.audit import AuditEvent, DeletionAudit      # noqa: E402
from app.models.email_admin import VerticalHead             # noqa: E402
from app.services import lead_archive as la                 # noqa: E402
from app.services import vertical_heads                     # noqa: E402

VERTICAL = 'Heavy Transport'
OWNER, HEAD, OTHER, ADMIN = 'ZA_OWN', 'ZA_HEAD', 'ZA_OTH', 'ZA_ADM'
REASON = 'client went quiet after the site visit'


def _purge():
    Lead.query.delete()
    DeletionAudit.query.filter(
        DeletionAudit.entity_type == 'Lead').delete(
            synchronize_session=False)
    AuditEvent.query.filter(AuditEvent.action.in_(
        ('bulk.archive', 'bulk.restore'))).delete(
            synchronize_session=False)
    Employee.query.filter(Employee.emp_code.in_(
        (OWNER, HEAD, OTHER, ADMIN))).delete(synchronize_session=False)
    VerticalHead.query.filter_by(vertical=VERTICAL).delete(
        synchronize_session=False)
    db.session.commit()


@pytest.fixture(autouse=True)
def _clean():
    with flask_app.app_context():
        db.create_all()
        _purge()
        for code, name, role in ((OWNER, 'Owner Person', 'user'),
                                 (HEAD, 'Head Person', 'user'),
                                 (OTHER, 'Other Person', 'user'),
                                 (ADMIN, 'Admin Person', 'admin')):
            db.session.add(Employee(
                emp_code=code, name=name, password_hash='x',
                email=f'{code.lower()}@procamlogistics.com',
                role=role, vertical=VERTICAL))
        db.session.add(VerticalHead(vertical=VERTICAL,
                                    head_emp_code=HEAD, active=True))
        db.session.commit()
        vertical_heads.cache_clear()
        yield
        _purge()
        vertical_heads.cache_clear()


def _lead(owner=OWNER, company='Drewes Group'):
    lead = Lead(company=company, stage='New', procam_vertical=VERTICAL,
                source='email', assigned_to=owner,
                created_at=datetime.utcnow())
    db.session.add(lead)
    db.session.commit()
    return lead


def _client(code=OWNER):
    client = flask_app.test_client()
    with client.session_transaction() as s:
        s['emp_code'] = code
        s['role'] = 'admin' if code == ADMIN else 'user'
    return client


# ── who may ──────────────────────────────────────────────────────────
@pytest.mark.parametrize('code', [OWNER, HEAD, ADMIN])
def test_the_owner_the_head_and_an_admin_may_archive(code):
    with flask_app.app_context():
        lead = _lead()
        assert la.may_archive(lead, code) is True


def test_somebody_else_may_not():
    with flask_app.app_context():
        lead = _lead()
        assert la.may_archive(lead, OTHER) is False
        assert 'owner' in la.why_not(lead, OTHER).lower()


def test_a_stranger_gets_403_and_the_lead_is_untouched():
    with flask_app.app_context():
        lid = _lead().id
    r = _client(OTHER).post(f'/api/leads/{lid}/archive',
                            json={'reason': REASON})
    assert r.status_code == 403
    with flask_app.app_context():
        assert Lead.query.get(lid).is_archived is not True


# ── the reason is mandatory, in the service ──────────────────────────
@pytest.mark.parametrize('reason', ['', '   ', None, 'x', '.'])
def test_a_reason_that_is_not_one_is_refused(reason):
    with flask_app.app_context():
        lead = _lead()
        with pytest.raises(la.Refused):
            la.archive(lead, reason=reason, actor=OWNER)
        assert Lead.query.get(lead.id).is_archived is not True


def test_a_reason_longer_than_the_column_is_refused_not_truncated():
    with flask_app.app_context():
        lead = _lead()
        with pytest.raises(la.Refused):
            la.archive(lead, reason='n' * (la.MAX_REASON + 1), actor=OWNER)


def test_the_route_says_400_when_the_reason_was_the_problem():
    """Not 403. The person may archive; they just said nothing."""
    with flask_app.app_context():
        lid = _lead().id
    r = _client(OWNER).post(f'/api/leads/{lid}/archive', json={'reason': ''})
    assert r.status_code == 400
    assert 'why' in r.get_json()['error'].lower()


# ── the archive itself ───────────────────────────────────────────────
def test_archiving_records_who_when_and_why():
    with flask_app.app_context():
        lead = _lead()
        la.archive(lead, reason=REASON, actor=OWNER)
        again = Lead.query.get(lead.id)
        assert again.is_archived is True
        assert again.archive_reason == REASON
        assert again.archived_by == OWNER
        assert again.archived_at is not None


def test_both_actions_leave_an_audit_row():
    with flask_app.app_context():
        lead = _lead()
        la.archive(lead, reason=REASON, actor=OWNER)
        la.restore(lead, actor=HEAD)
        actions = [r.action for r in DeletionAudit.query.filter_by(
            entity_type='Lead', entity_id=lead.id).all()]
        assert 'archive' in actions
        assert 'restore' in actions


def test_the_reason_reaches_the_audit_row():
    with flask_app.app_context():
        lead = _lead()
        la.archive(lead, reason=REASON, actor=OWNER)
        row = DeletionAudit.query.filter_by(
            entity_type='Lead', entity_id=lead.id, action='archive').first()
        assert row is not None and REASON in (row.reason or '')


def test_archiving_an_archived_lead_is_refused():
    """Otherwise the second reason overwrites the first, and why it
    actually went is lost."""
    with flask_app.app_context():
        lead = _lead()
        la.archive(lead, reason=REASON, actor=OWNER)
        with pytest.raises(la.Refused):
            la.archive(lead, reason='a different reason', actor=ADMIN)
        assert Lead.query.get(lead.id).archive_reason == REASON


def test_restoring_a_lead_that_is_not_archived_is_refused():
    with flask_app.app_context():
        lead = _lead()
        with pytest.raises(la.Refused):
            la.restore(lead, actor=OWNER)


def test_nothing_is_deleted():
    """CRM-NO-DELETE-01. The row is still there and still readable."""
    with flask_app.app_context():
        lead = _lead()
        lid = lead.id
        la.archive(lead, reason=REASON, actor=OWNER)
        assert Lead.query.get(lid) is not None
        assert Lead.query.filter_by(id=lid).count() == 1


def test_restore_clears_the_four_columns():
    with flask_app.app_context():
        lead = _lead()
        la.archive(lead, reason=REASON, actor=OWNER)
        la.restore(lead, actor=OWNER)
        again = Lead.query.get(lead.id)
        assert again.is_archived is False
        assert again.archived_at is None
        assert again.archived_by is None
        assert again.archive_reason is None


# ── it drops out of the lists ────────────────────────────────────────
def test_an_archived_lead_leaves_the_scope_every_board_goes_through():
    """The half of §1 that no button would have fixed."""
    with flask_app.app_context():
        lead = _lead()
        lid = lead.id
        with flask_app.test_request_context('/'):
            from flask import session as flask_session
            flask_session['emp_code'] = ADMIN
            flask_session['role'] = 'admin'
            before = {row.id for row in access_scope.leads().all()}
            assert lid in before
            la.archive(Lead.query.get(lid), reason=REASON, actor=ADMIN)
            after = {row.id for row in access_scope.leads().all()}
            assert lid not in after
            asked = {row.id for row in access_scope.leads(
                include_archived=True).all()}
            assert lid in asked


def test_the_main_lead_list_hides_it_and_the_filter_shows_it():
    with flask_app.app_context():
        lid = _lead().id
    client = _client(ADMIN)

    plain = client.get('/api/leads')
    assert lid in [row['id'] for row in plain.get_json()]

    with flask_app.app_context():
        la.archive(Lead.query.get(lid), reason=REASON, actor=ADMIN)

    hidden = client.get('/api/leads')
    assert lid not in [row['id'] for row in hidden.get_json()]

    shown = client.get('/api/leads?archived=only')
    assert lid in [row['id'] for row in shown.get_json()]

    both = client.get('/api/leads?archived=all')
    assert lid in [row['id'] for row in both.get_json()]


def test_the_number_is_kept():
    """§1 — "keeps its number". Archiving must not renumber anything."""
    with flask_app.app_context():
        lead = _lead()
        lid = lead.id
        la.archive(lead, reason=REASON, actor=OWNER)
        la.restore(Lead.query.get(lid), actor=OWNER)
        assert Lead.query.get(lid).id == lid


# ── the round trip over HTTP ─────────────────────────────────────────
def test_archive_and_restore_over_the_api():
    with flask_app.app_context():
        lid = _lead().id
    client = _client(OWNER)

    r = client.post(f'/api/leads/{lid}/archive', json={'reason': REASON})
    assert r.status_code == 200 and r.get_json()['ok'] is True
    assert r.get_json()['archived'] is True

    r = client.post(f'/api/leads/{lid}/archive-restore', json={})
    assert r.status_code == 200 and r.get_json()['ok'] is True
    assert r.get_json()['archived'] is False


def test_the_state_route_tells_a_screen_what_to_draw():
    with flask_app.app_context():
        lid = _lead().id
    body = _client(OWNER).get(f'/api/leads/{lid}/archive-state').get_json()
    assert body['may'] is True
    assert body['archived'] is False
    assert body['min_reason'] == la.MIN_REASON

    body = _client(OTHER).get(f'/api/leads/{lid}/archive-state').get_json()
    assert body['may'] is False
    assert body['why_not']
