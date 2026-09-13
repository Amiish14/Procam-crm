"""
Records reachable by id are confined to the people who may hold them.

Each route below loaded a record by id with no ownership check, and each
test is the probe that used to work: a signed-in user asking for an id
that is not theirs. Alongside, the paths that changed a lead's owner or
deleted it without updating the Copilot index, which filters search by
the owner stamped on each chunk — so a stale chunk is a disclosure.
"""
import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'RecAccessTestOnly12345')
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'recaccess.db'))

from app import (app as flask_app, db, Employee, Lead, LeadNote,    # noqa
                 LeadEmail, LeadAssignmentHistory, Opportunity,
                 ImportBatch)
from app.models.audit import DeletionAudit                   # noqa: E402,F401
from app.access.service import set_profile                  # noqa: E402
from app.models.access import DataScope                     # noqa: E402
from app.models.business_card import BusinessCardImport     # noqa: E402
from app.models.copilot import CopilotChunk                 # noqa: E402

flask_app.config['WTF_CSRF_ENABLED'] = False


@pytest.fixture()
def world():
    with flask_app.app_context():
        db.create_all()
        for code, role, sup in (('RAADM', 'admin', True),
                                ('RAONE', 'user', False),
                                ('RATWO', 'user', False)):
            e = Employee.query.filter_by(emp_code=code).first()
            if e is None:
                e = Employee(emp_code=code, name=code)
                db.session.add(e)
            e.role, e.is_active, e.must_change_pw = role, True, False
            e.vertical, e.is_super_admin = 'All', sup
        db.session.commit()
        set_profile('RAONE', DataScope.OWN, ['module.business_cards'],
                    actor='RAADM')
        set_profile('RATWO', DataScope.OWN, ['module.business_cards'],
                    actor='RAADM')

        lead = Lead(company='RA Two Co', source='manual',
                    assigned_to='RATWO', stage='New')
        db.session.add(lead)
        db.session.flush()
        opp = Opportunity(opp_number=f'RA-{lead.id}', lead_id=lead.id,
                          owner_emp_code='RATWO', stage='Won',
                          title='secret deal', value_inr=9_900_000)
        batch = ImportBatch(kind='leads', filename='two.csv', total_rows=1,
                            preview_data=json.dumps(
                                [{'data': {'company': 'Injected Co'}}]),
                            created_by='RATWO')
        card = BusinessCardImport(uploaded_by_id='RATWO',
                                  extracted_json={'name': 'A Contact'})
        db.session.add_all([opp, batch, card])
        db.session.commit()
        return {'lead': lead.id, 'opp': opp.id, 'batch': batch.id,
                'card': card.id}


def _c(code, role='user'):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code=code, name=code, role=role, vertical='All')
    return c


# ── reading by id ────────────────────────────────────────────────────
def test_another_owners_opportunity_is_not_readable(world):
    r = _c('RAONE').get(f'/api/opportunities/{world["opp"]}')
    assert r.status_code == 404
    assert b'9900000' not in r.data and b'secret deal' not in r.data
    assert _c('RATWO').get(
        f'/api/opportunities/{world["opp"]}').status_code == 200


def test_another_owners_deal_cannot_be_stamped_as_a_project(world):
    r = _c('RAONE').post(f'/api/opportunities/{world["opp"]}'
                         '/convert-to-project', json={'project_ref': 'X'})
    assert r.status_code == 403
    with flask_app.app_context():
        assert db.session.get(Opportunity, world['opp']).won_project_ref \
            is None


def test_outreach_cannot_send_another_owners_lead_out(world, monkeypatch):
    """Refused before any model is contacted — the key is fake."""
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'not-a-real-key')
    r = _c('RAONE').post('/api/outreach/generate',
                         json={'lead_id': world['lead']})
    assert r.status_code == 404


def test_another_persons_import_cannot_be_committed(world):
    r = _c('RAONE').post('/api/leads/import/commit',
                         json={'batch_id': world['batch']})
    assert r.status_code == 404
    with flask_app.app_context():
        assert db.session.get(ImportBatch, world['batch']).committed \
            in (False, None)
        assert Lead.query.filter_by(company='Injected Co').count() == 0


@pytest.mark.parametrize('method,path', [
    ('get', '/api/business-cards/{id}'),
    ('get', '/business-cards/{id}/image'),
    ('post', '/api/business-cards/{id}/save'),
    ('post', '/api/business-cards/{id}/discard'),
])
def test_another_persons_business_card_is_out_of_reach(world, method, path):
    r = getattr(_c('RAONE'), method)(path.format(id=world['card']), json={})
    assert r.status_code == 404
    with flask_app.app_context():
        assert db.session.get(BusinessCardImport, world['card']).status \
            != 'Discarded'


def test_the_card_list_shows_only_your_own(world):
    ids = {c['id'] for c in _c('RAONE').get(
        '/api/business-cards').get_json()['cards']}
    assert world['card'] not in ids
    ids = {c['id'] for c in _c('RATWO').get(
        '/api/business-cards').get_json()['cards']}
    assert world['card'] in ids


def test_the_uploader_still_reaches_their_card(world):
    r = _c('RATWO').get(f'/api/business-cards/{world["card"]}')
    assert r.status_code == 200


# ── the Copilot index follows the lead ───────────────────────────────
def _chunk(lead_id, owner):
    db.session.add(CopilotChunk(lead_id=lead_id, owner_emp_code=owner,
                                source='note', text='confidential terms'))
    db.session.commit()


def test_bulk_reassignment_records_history_and_drops_chunks(world):
    from app.bulk_admin import service as bulk
    with flask_app.app_context():
        _chunk(world['lead'], 'RATWO')
        bulk.assign([world['lead']], 'RAONE', 'RAADM')
        assert CopilotChunk.query.filter_by(lead_id=world['lead']).count() == 0
        h = (LeadAssignmentHistory.query.filter_by(lead_id=world['lead'])
             .order_by(LeadAssignmentHistory.id.desc()).first())
        assert (h.from_primary, h.to_primary) == ('RATWO', 'RAONE')
        assert h.changed_by == 'RAADM'


def test_bulk_stage_must_be_a_real_stage(world):
    from app.bulk_admin import service as bulk
    with flask_app.app_context():
        with pytest.raises(ValueError):
            bulk.set_field([world['lead']], 'stage', 'Definitely Won!!', 'X')
        assert db.session.get(Lead, world['lead']).stage == 'New'


def test_bulk_delete_leaves_nothing_to_reattach(world):
    from app.bulk_admin import service as bulk
    with flask_app.app_context():
        lid = world['lead']
        Opportunity.query.filter_by(lead_id=lid).delete()
        db.session.add(LeadNote(lead_id=lid, note_text='n', author='RATWO'))
        db.session.add(LeadEmail(lead_id=lid, direction='inbound', body='b'))
        _chunk(lid, 'RATWO')
        bulk.delete([lid], 'test cleanup', 'RAADM')
        for model in (LeadNote, LeadEmail, CopilotChunk):
            assert model.query.filter_by(lead_id=lid).count() == 0, model


def test_single_delete_drops_the_chunks(world):
    with flask_app.app_context():
        lid = world['lead']
        Opportunity.query.filter_by(lead_id=lid).delete()
        db.session.commit()
        _chunk(lid, 'RATWO')
    r = _c('RAADM', 'admin').delete(f'/api/leads/{lid}',
                                    json={'reason': 'test'})
    assert r.status_code == 200, r.get_data(as_text=True)
    with flask_app.app_context():
        assert CopilotChunk.query.filter_by(lead_id=lid).count() == 0


def test_notes_are_indexed_and_deleted_notes_are_not(world):
    from app.copilot import retrieval
    with flask_app.app_context():
        lid = world['lead']
        db.session.add(LeadNote(lead_id=lid, note_text='visible note text',
                                author='RATWO'))
        db.session.add(LeadNote(lead_id=lid, note_text='removed note text',
                                author='RATWO', is_deleted=True))
        db.session.commit()
        texts = [t for _src, _subj, t in
                 retrieval.chunks_for_lead(db.session.get(Lead, lid))]
        assert 'visible note text' in texts
        assert 'removed note text' not in texts
