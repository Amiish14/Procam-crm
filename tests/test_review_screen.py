"""The Lead Review screen — every action, the duplicate override, and the
panels that explain a decision.

Each action is a labelled correction the learning engine reads, so these
tests check what was recorded as well as what happened.
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'ReviewTestOnly12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'review.db')

import importlib                                              # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Employee, Lead, LeadEmail = _main.Employee, _main.Lead, _main.LeadEmail
EmailClassification = _main.EmailClassification
flask_app.config['WTF_CSRF_ENABLED'] = False

from app.intake import learning                               # noqa: E402
from app.intake import service as svc                         # noqa: E402
from app.models.audit import AuditEvent                       # noqa: E402
from app.services import lead_intake as li                    # noqa: E402

K = li.Klass
_DOMAIN = 'rvtest.example'
_counter = [0]


def _client(code, role):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code=code, name=code, role=role, vertical='All')
    return c


@pytest.fixture(scope='module')
def world():
    with flask_app.app_context():
        db.create_all()
        for code, role, sup in (('RVADM', 'admin', True),
                                ('RVREP', 'user', False)):
            e = Employee.query.filter_by(emp_code=code).first()
            if e is None:
                e = Employee(emp_code=code, name=f'Review {code}')
                db.session.add(e)
            e.role, e.is_active, e.must_change_pw = role, True, False
            e.is_super_admin, e.vertical = sup, 'All'
        target = Lead(company='Review Target', source='email',
                      stage='New Opportunity', email=f'first@{_DOMAIN}')
        db.session.add(target)
        db.session.commit()
        target_id = target.id
    yield {'admin': _client('RVADM', 'admin'),
           'rep': _client('RVREP', 'user'), 'target': target_id}

    # Rows are cleaned up; lead rows are neutralised rather than deleted,
    # so their ids are not handed to another module's orphaned trail rows.
    with flask_app.app_context():
        rows = EmailClassification.query.filter(
            EmailClassification.message_id.like('<rv-%')).all()
        lead_ids = {r.created_lead_id for r in rows if r.created_lead_id}
        lead_ids.add(target_id)
        for r in rows:
            db.session.delete(r)
        for lead_id in lead_ids:
            LeadEmail.query.filter_by(lead_id=lead_id).delete()
            lead = db.session.get(Lead, lead_id)
            if lead is not None:
                lead.email = lead.original_email_from = None
                lead.email_message_id = lead.conversation_id = None
                lead.created_at = datetime.utcnow() - timedelta(days=3650)
        db.session.commit()


def _row(klass=K.REVIEW, frm=None, conversation=None, decided_by='step_10',
         review_state='pending', created_at=None, duplicate=None,
         duplicate_score=0, matched_lead_id=None, **kw):
    _counter[0] += 1
    n = _counter[0]
    frm = frm or f'buyer@{_DOMAIN}'
    payload = {'body': 'Please quote for 40 MT from Hazira to Dahej.',
               'resolved_sender': frm, 'attachments': [],
               'keywords': ['quote', 'tonnes'],
               'score_parts': [['base', 40], ['asks for a price', 25]]}
    if duplicate:
        payload['duplicate'] = duplicate
    payload.update(kw.pop('payload', {}))
    with flask_app.app_context():
        row = EmailClassification(
            message_id=f'<rv-{n}@x>', subject=f'Review item {n}',
            from_addr=frm, from_domain=frm.split('@')[1],
            conversation_id=conversation, classification=klass,
            decided_by=decided_by, reason='test decision', confidence=65,
            duplicate_score=duplicate_score, matched_lead_id=matched_lead_id,
            review_state=review_state, payload=payload,
            created_at=created_at or datetime.utcnow(), **kw)
        db.session.add(row)
        db.session.commit()
        return row.id


def _get(cid):
    with flask_app.app_context():
        return db.session.get(EmailClassification, cid)


# ─── the one-click actions ───────────────────────────────────────────────
@pytest.mark.parametrize('to_class', [K.RATE_SOURCING, K.QUOTE, K.INTERNAL,
                                      K.DUPLICATE])
def test_each_mark_action_records_a_correction(world, to_class):
    cid = _row()
    r = world['admin'].post(f'/api/intake/review/{cid}/reclassify',
                            json={'to_class': to_class})
    assert r.status_code == 200, r.get_data(as_text=True)
    row = _get(cid)
    assert row.review_state == 'reclassified'
    assert row.corrected_to == to_class and row.corrected_by == 'RVADM'
    assert row.classification == K.REVIEW


def test_accept_merge_and_reject_work_through_the_api(world):
    c = world['admin']
    accepted = _row()
    r = c.post(f'/api/intake/review/{accepted}/accept', json={})
    assert r.status_code == 200 and r.get_json()['lead_id']
    assert _get(accepted).correction_reason == 'Accepted at review'

    merged = _row()
    r = c.post(f'/api/intake/review/{merged}/merge',
               json={'lead_id': world['target']})
    assert r.status_code == 200 and r.get_json()['lead_id'] == world['target']
    assert _get(merged).review_state == 'merged'

    rejected = _row()
    assert c.post(f'/api/intake/review/{rejected}/reject',
                  json={}).status_code == 400
    r = c.post(f'/api/intake/review/{rejected}/reject',
               json={'reason': 'Spam / Marketing'})
    assert r.status_code == 200
    assert _get(rejected).review_state == 'rejected'


def test_the_page_offers_every_action_and_panel(world):
    html = world['admin'].get('/lead-review').get_data(as_text=True)
    for text in ('Accept as lead', 'Merge', 'Reject', 'Mark duplicate',
                 'Mark internal', 'Mark quote', 'Mark rate sourcing',
                 'Not a duplicate', 'Why this score', 'Classifier explanation',
                 'History', 'Held as duplicates'):
        assert text in html, text
    assert "u.indexOf('{{ url_prefix }}') !== 0" in open(os.path.join(
        _ROOT, 'templates', 'intake', 'review.html')).read()
    assert 'application/json' in html and 'X-CSRFToken' in html


# ─── not a duplicate ─────────────────────────────────────────────────────
_DUP = {'score': 85, 'lead_id': None, 'reasons': [
    {'signal': 'same_account_and_route', 'weight': 35,
     'detail': 'same account and the same route (hazira to dahej)'},
    {'signal': 'same_attachment_name', 'weight': 30,
     'detail': 'attachment Drawing_Rev2.pdf is on the lead too'},
    {'signal': 'same_account_in_window', 'weight': 20,
     'detail': f'same sender domain {_DOMAIN}'}]}


def _held_duplicate(world, **kw):
    dup = dict(_DUP, lead_id=world['target'])
    return _row(klass=K.DUPLICATE, decided_by='step_9',
                review_state='accepted', duplicate=dup, duplicate_score=85,
                matched_lead_id=world['target'], **kw)


def test_held_duplicates_have_their_own_view(world):
    held = _held_duplicate(world)
    old = _held_duplicate(world,
                          created_at=datetime.utcnow() - timedelta(days=45))
    checked = _held_duplicate(world)
    with flask_app.app_context():
        row = db.session.get(EmailClassification, checked)
        row.corrected_to = K.DUPLICATE
        db.session.commit()

    r = world['admin'].get('/api/intake/review?view=duplicates')
    assert r.status_code == 200
    body = r.get_json()
    ids = {i['id'] for i in body['items']}
    assert held in ids
    assert old not in ids, 'older than the review window'
    assert checked not in ids, 'already confirmed by a person'
    item = [i for i in body['items'] if i['id'] == held][0]
    assert item['duplicate_suspected'] is True
    assert item['payload']['duplicate']['reasons'][0]['signal'] == \
        'same_account_and_route'
    assert body['counts']['duplicates'] >= 1

    pending = {i['id'] for i in world['admin'].get('/api/intake/review')
               .get_json()['items']}
    assert held not in pending, 'the default queue is unchanged'


def test_not_a_duplicate_creates_the_lead_and_records_the_correction(world):
    cid = _held_duplicate(world)
    r = world['admin'].post(f'/api/intake/review/{cid}/accept',
                            json={'reason': 'Not a duplicate'})
    assert r.status_code == 200, r.get_data(as_text=True)
    lead_id = r.get_json()['lead_id']
    assert lead_id and lead_id != world['target']

    row = _get(cid)
    assert row.classification == K.DUPLICATE, 'the original decision stays'
    assert row.corrected_to == K.NEW_LEAD
    assert row.correction_reason == 'Not a duplicate'
    assert row.matched_lead_id == world['target'], \
        'which lead it was wrongly held against is kept'
    assert svc._verdict(row) == 'false_negative'
    with flask_app.app_context():
        ev = (AuditEvent.query.filter_by(action='classifier.correction',
                                         entity_id=str(cid))
              .order_by(AuditEvent.id.desc()).first())
        assert ev is not None and ev.reason == 'Not a duplicate'

    ids = {i['id'] for i in world['admin']
           .get('/api/intake/review?view=duplicates').get_json()['items']}
    assert cid not in ids


def test_not_a_duplicate_is_refused_for_mail_never_held_as_one(world):
    cid = _row()
    r = world['admin'].post(f'/api/intake/review/{cid}/accept',
                            json={'reason': 'Not a duplicate'})
    assert r.status_code == 400
    assert _get(cid).created_lead_id is None


def test_an_unknown_acceptance_reason_is_refused(world):
    cid = _row()
    r = world['admin'].post(f'/api/intake/review/{cid}/accept',
                            json={'reason': 'because I said so'})
    assert r.status_code == 400
    assert _get(cid).created_lead_id is None


def test_repeated_overrides_reach_the_learning_engine(world):
    """Three duplicates overruled the same way is a rule worth looking
    at; the proposal names the step that keeps getting it wrong."""
    for _ in range(3):
        cid = _held_duplicate(world)
        assert world['admin'].post(f'/api/intake/review/{cid}/accept',
                                   json={'reason': 'Not a duplicate'}) \
            .status_code == 200
    with flask_app.app_context():
        obs = [p for p in learning.proposals()
               if p['key'] == f'misclass:step_9:{K.DUPLICATE}:{K.NEW_LEAD}']
    assert obs and obs[0]['evidence'] >= 3


# ─── history ─────────────────────────────────────────────────────────────
def test_history_shows_the_same_sender_and_conversation_newest_first(world):
    now = datetime.utcnow()
    sender = f'history@{_DOMAIN}'
    older = _row(frm=sender, created_at=now - timedelta(days=5),
                 review_state='rejected', corrected_to=K.REVIEW,
                 correction_reason='Spam / Marketing')
    newer = _row(frm=sender, created_at=now - timedelta(days=1))
    same_thread = _row(frm=f'colleague@{_DOMAIN}', conversation='AAQk-rv-1',
                       created_at=now - timedelta(days=3))
    unrelated = _row(frm=f'someone.else@{_DOMAIN}')
    current = _row(frm=sender, conversation='AAQk-rv-1')

    r = world['admin'].get(f'/api/intake/review/{current}/history')
    assert r.status_code == 200
    hist = r.get_json()['history']
    ids = [h['id'] for h in hist]
    assert ids == [newer, same_thread, older]
    assert current not in ids and unrelated not in ids
    by_id = {h['id']: h for h in hist}
    assert by_id[same_thread]['match'] == ['same conversation']
    assert by_id[older]['match'] == ['same sender']
    assert by_id[older]['correction_reason'] == 'Spam / Marketing'


def test_history_matches_the_customer_behind_a_forward(world):
    customer = f'customer@{_DOMAIN}'
    before = _row(frm=customer)
    relayed = _row(frm='colleague@procamgroup.in',
                   payload={'resolved_sender': customer,
                            'forward_resolved': True})
    hist = world['admin'].get(f'/api/intake/review/{relayed}/history') \
        .get_json()['history']
    assert before in [h['id'] for h in hist]


def test_history_is_capped(world):
    sender = f'busy@{_DOMAIN}'
    for _ in range(23):
        _row(frm=sender)
    current = _row(frm=sender)
    with flask_app.app_context():
        assert len(svc.classification_history(current)) == 20
        assert len(svc.classification_history(current, limit=5)) == 5


def test_history_is_permission_checked_and_404s(world):
    cid = _row()
    assert world['rep'].get(f'/api/intake/review/{cid}/history') \
        .status_code in (302, 403)
    assert world['rep'].get('/api/intake/review?view=duplicates') \
        .status_code in (302, 403)
    assert world['admin'].get('/api/intake/review/99999999/history') \
        .status_code == 404
