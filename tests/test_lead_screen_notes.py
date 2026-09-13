"""
The lead screen: note history, who may edit a note, why the lead exists,
and searching notes.
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'LeadScreenTestOnly12345')
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'leadscreen.db'))

from app import (app as flask_app, db, Employee, Lead, LeadNote,   # noqa
                 EmailClassification)
from app.access.service import set_profile                          # noqa
from app.models.access import DataScope                             # noqa

flask_app.config['WTF_CSRF_ENABLED'] = False
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture()
def world():
    made = {}
    with flask_app.app_context():
        db.create_all()
        for code, role in (('LSONE', 'user'), ('LSTWO', 'user'),
                           ('LSADM', 'admin')):
            e = Employee.query.filter_by(emp_code=code).first()
            if e is None:
                e = Employee(emp_code=code, name=f'{code} Name')
                db.session.add(e)
            e.role, e.is_active, e.must_change_pw = role, True, False
            e.vertical, e.is_super_admin, e.session_version = 'All', False, 0
        db.session.commit()
        set_profile('LSONE', DataScope.OWN, [], actor='LSADM')
        set_profile('LSTWO', DataScope.OWN, [], actor='LSADM')
        set_profile('LSADM', DataScope.ALL, [], actor='LSADM')
        mine = Lead(company='Lead Screen One', source='email', stage='New',
                    assigned_to='LSONE', secondary_owner='LSTWO',
                    procam_vertical='Project Freight', vertical_confidence=82,
                    vertical_reason='transformer, ODC')
        theirs = Lead(company='Lead Screen Two', source='manual',
                      stage='New', assigned_to='LSTWO')
        db.session.add_all([mine, theirs])
        db.session.flush()
        old = datetime.utcnow() - timedelta(days=400)
        notes = [
            LeadNote(lead_id=mine.id, note_text='Crane availability at '
                     'Kandla confirmed for the transformer move',
                     author='LSONE', created_at=datetime.utcnow()),
            LeadNote(lead_id=mine.id, note_text='Kandla crane quote old',
                     author='LSONE', created_at=old),
            LeadNote(lead_id=mine.id, note_text='Kandla deleted note',
                     author='LSONE', is_deleted=True),
            LeadNote(lead_id=theirs.id, note_text='Kandla secret for two',
                     author='LSTWO'),
            LeadNote(lead_id=mine.id, note_text='Called about Kandla again',
                     author='LSTWO'),
        ]
        db.session.add_all(notes)
        cls = EmailClassification(message_id=f'<ls-{mine.id}@x>',
                                  classification='A_new_lead',
                                  decided_by='step_9', reason='RFQ wording',
                                  confidence=91, created_lead_id=mine.id,
                                  payload={'keywords': ['rfq', 'odc'],
                                           'score_parts': {'rfq': 40}})
        db.session.add(cls)
        db.session.commit()
        made = {'mine': mine.id, 'theirs': theirs.id,
                'note': notes[0].id, 'old': notes[1].id,
                'deleted': notes[2].id, 'secret': notes[3].id,
                'by_two': notes[4].id, 'cls': cls.id}
    yield made
    with flask_app.app_context():
        LeadNote.query.filter(LeadNote.lead_id.in_(
            [made['mine'], made['theirs']])).delete(synchronize_session=False)
        EmailClassification.query.filter_by(id=made['cls']).delete()
        Lead.query.filter(Lead.id.in_([made['mine'], made['theirs']])) \
            .delete(synchronize_session=False)
        db.session.commit()


def _c(code, role='user'):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code=code, name=code, role=role, vertical='All', sv=0)
    return c


# ── note history ─────────────────────────────────────────────────────
def test_versions_are_numbered_with_their_writers(world):
    c = _c('LSONE')
    base = f'/api/leads/{world["mine"]}/notes/{world["note"]}'
    assert c.put(base, json={'note_text': 'Second wording'}).status_code == 200
    assert _c('LSADM', 'admin').put(
        base, json={'note_text': 'Third wording'}).status_code == 200
    v = c.get(base + '/revisions').get_json()['versions']
    assert v[2]['written_by'] == 'LSADM'
    assert [x['version'] for x in v] == [1, 2, 3]
    assert v[0]['note_text'].startswith('Crane availability')
    assert v[2]['note_text'] == 'Third wording' and v[2]['current']
    assert v[1]['written_by'] == 'LSONE'
    assert v[1]['written_by_name'] == 'LSONE Name'


def test_only_the_author_or_wider_scope_may_edit(world):
    base = f'/api/leads/{world["mine"]}/notes/{world["note"]}'
    # LSTWO can open the lead (secondary owner) but did not write the note
    r = _c('LSTWO').put(base, json={'note_text': 'rewritten by someone else'})
    assert r.status_code == 403
    assert _c('LSADM', 'admin').put(
        base, json={'note_text': 'corrected by an administrator'}) \
        .status_code == 200


def test_history_of_a_lead_you_cannot_open_is_refused(world):
    r = _c('LSONE').get(f'/api/leads/{world["theirs"]}/notes/'
                        f'{world["secret"]}/revisions')
    assert r.status_code in (403, 404)


# ── why this is a lead ───────────────────────────────────────────────
def test_the_intake_decision_and_vertical_are_explained(world):
    body = _c('LSONE').get(
        f'/api/leads/{world["mine"]}/classification').get_json()
    assert body['decision']['label']
    assert body['decision']['confidence'] == 91
    assert body['decision']['keywords'] == ['rfq', 'odc']
    assert body['vertical'] == {'value': 'Project Freight', 'confidence': 82,
                                'reason': 'transformer, ODC'}
    assert _c('LSONE').get(f'/api/leads/{world["theirs"]}/classification') \
        .status_code in (403, 404)


# ── notes search ─────────────────────────────────────────────────────
def test_search_never_returns_a_note_the_viewer_cannot_see(world):
    rows = _c('LSONE').get('/api/notes/search?q=kandla').get_json()['results']
    ids = {r['note_id'] for r in rows}
    assert world['secret'] not in ids
    assert world['deleted'] not in ids
    assert world['note'] in ids


def test_recent_and_fuller_matches_rank_first(world):
    rows = _c('LSONE').get('/api/notes/search?q=kandla+crane') \
        .get_json()['results']
    ids = [r['note_id'] for r in rows]
    assert ids.index(world['note']) < ids.index(world['old'])


def test_author_and_date_filters(world):
    c = _c('LSONE')
    by_two = c.get('/api/notes/search?q=kandla&author=lstwo') \
        .get_json()['results']
    assert {r['note_id'] for r in by_two} == {world['by_two']}
    since = (datetime.utcnow() - timedelta(days=30)).strftime('%Y-%m-%d')
    recent = c.get(f'/api/notes/search?q=kandla&from={since}') \
        .get_json()['results']
    assert world['old'] not in {r['note_id'] for r in recent}


def test_the_lead_screen_offers_history_edit_and_search():
    src = open(os.path.join(_ROOT, 'templates', 'app.html')).read()
    for needle in ('function showNoteHistory', 'function editLeadNote',
                   'function noteDiff', 'function loadLeadClassification',
                   "'/api/notes/search?'", 'vertical_confidence'):
        assert needle in src, needle
    # every interpolated note value in the diff is escaped
    diff = src[src.index('function noteDiff'):
               src.index('async function showNoteHistory')]
    assert '${a[i]}' not in diff and '${b[j]}' not in diff
