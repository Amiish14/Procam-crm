"""The scanner end to end — WP2.

Covers the three things that were structurally impossible before: an
extraction with no Vision service, a duplicate caught on a field the
reviewer edited, and a number matched despite being written differently.
"""
import io
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, 'tests', 'fixtures'))

os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'CardTestOnly12345')
os.environ.pop('ANTHROPIC_API_KEY', None)      # the state the CRM is in
_TMP = tempfile.mkdtemp()
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(_TMP, 'cards.db')
os.environ['UPLOAD_ROOT'] = os.path.join(_TMP, 'uploads')

import importlib                                              # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db, Employee = _main.app, _main.db, _main.Employee
flask_app.config['WTF_CSRF_ENABLED'] = False
flask_app.config['UPLOAD_ROOT'] = os.environ['UPLOAD_ROOT']

from business_cards import CARDS                              # noqa: E402
from app.services.business_card_ocr import extract_business_card  # noqa: E402

# A 1x1 PNG — the image is stored and shown, never parsed here.
_PNG = (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00'
        b'\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc'
        b'\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82')


@pytest.fixture(scope='module')
def client():
    with flask_app.app_context():
        db.create_all()
        e = Employee.query.filter_by(emp_code='CARDADM').first()
        if not e:
            e = Employee(emp_code='CARDADM', name='Card Admin')
            db.session.add(e)
        e.role, e.is_active, e.must_change_pw = 'admin', True, False
        e.vertical, e.is_super_admin = 'All', True
        db.session.commit()
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code='CARDADM', name='Card Admin', role='admin',
                 vertical='All')
    return c


def _upload(client, text=None):
    data = {'file': (io.BytesIO(_PNG), 'card.png')}
    if text:
        data['card_text'] = text
    r = client.post('/api/business-cards/upload', data=data,
                    content_type='multipart/form-data')
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()['card']


# ─── extraction without the Vision service ───────────────────────────────
def test_the_scanner_works_with_no_vision_service(client):
    """ANTHROPIC_API_KEY is unset here, which is the CRM's actual state.

    Before, this produced an empty review form and the scan was wasted.
    """
    card = _upload(client, CARDS['name_first'])
    ex = card['extracted']
    assert ex['name'] == 'Rajesh Kumar Sharma'
    assert ex['company'] == 'AMBUJA CEMENT LIMITED'
    assert ex['email'] == 'rajesh.sharma@ambujacement.com'
    assert ex['mobile'] == '+91 98200 11223'


def test_an_upload_with_no_text_and_no_service_still_opens_a_review(client):
    """It must degrade to a manual form, never to an error."""
    card = _upload(client)
    assert card['status'] == 'Extracted'
    assert card['ocr_error']          # the reason is surfaced, not hidden


def test_nothing_is_created_before_the_user_confirms(client):
    from app import Contact
    with flask_app.app_context():
        before = Contact.query.count()
    _upload(client, CARDS['minimal'])
    with flask_app.app_context():
        assert Contact.query.count() == before, \
            'a scan must never create a contact on its own'


def test_the_review_payload_carries_the_card_image(client):
    card = _upload(client, CARDS['minimal'])
    assert card['image_url'].endswith(f'/business-cards/{card["id"]}/image')
    r = client.get(card['image_url'])
    assert r.status_code == 200
    assert r.headers['Content-Type'].startswith('image/')
    assert r.headers['X-Content-Type-Options'] == 'nosniff'


def test_the_image_route_refuses_a_path_outside_the_upload_root(client):
    """image_path is a column; a tampered row must not read /etc/passwd."""
    from app.models.business_card import BusinessCardImport
    card = _upload(client, CARDS['minimal'])
    with flask_app.app_context():
        row = db.session.get(BusinessCardImport, card['id'])
        row.image_path = '/etc/passwd'
        db.session.commit()
    assert client.get(f'/business-cards/{card["id"]}/image').status_code == 404


# ─── saving through the normal CRM path ──────────────────────────────────
def test_a_confirmed_card_creates_a_contact_and_an_account(client):
    from app import Contact, Company
    card = _upload(client, CARDS['company_first'])
    r = client.post(f'/api/business-cards/{card["id"]}/save',
                    json={'fields': card['extracted'], 'choice': 'new'})
    assert r.status_code == 200, r.get_data(as_text=True)
    with flask_app.app_context():
        c = Contact.query.filter_by(name='Priya Menon').first()
        assert c is not None
        assert c.email == 'priya.menon@bhel.in'
        assert c.account_id, 'the contact must be linked to its company'
        assert Company.query.get(c.account_id).name == \
            'BHARAT HEAVY ELECTRICALS LTD'


def test_the_saved_contact_appears_in_people(client):
    """People is the list WP1 made clickable, so this closes the loop."""
    rows = client.get('/api/contacts?type=person').get_json()
    assert any(r['name'] == 'Priya Menon' for r in rows)


# ─── duplicates ──────────────────────────────────────────────────────────
def test_a_duplicate_is_refused_until_it_is_confirmed(client):
    """Priya Menon was saved above; scanning her card again must stop."""
    card = _upload(client, CARDS['company_first'])
    r = client.post(f'/api/business-cards/{card["id"]}/save',
                    json={'fields': card['extracted'], 'choice': 'new'})
    assert r.status_code == 409
    body = r.get_json()
    assert body['duplicate'] is True
    assert body['matches']['contacts']


def test_a_duplicate_edited_in_at_review_time_is_still_caught(client):
    """The upload-time check cleared this card; the reviewer then typed
    an email that belongs to someone already in the CRM."""
    card = _upload(client, CARDS['minimal'])
    assert not (card['dup_matches'] or {}).get('contacts')
    fields = dict(card['extracted'], email='priya.menon@bhel.in')
    r = client.post(f'/api/business-cards/{card["id"]}/save',
                    json={'fields': fields, 'choice': 'new'})
    assert r.status_code == 409, 'the edited email was never re-checked'


def test_confirming_creates_the_contact_anyway(client):
    from app import Contact
    card = _upload(client, CARDS['company_first'])
    r = client.post(f'/api/business-cards/{card["id"]}/save',
                    json={'fields': card['extracted'], 'choice': 'new',
                          'confirm_duplicate': True})
    assert r.status_code == 200
    with flask_app.app_context():
        assert Contact.query.filter_by(name='Priya Menon').count() >= 2


def test_a_number_written_differently_is_still_a_duplicate(client):
    """The existing check compared phone strings exactly, so the same
    mobile written +91 98… and 098… created two contacts."""
    from app import Contact
    with flask_app.app_context():
        db.session.add(Contact(contact_type='person', name='Existing Person',
                               mobile='+91 98110 22334'))
        db.session.commit()
    card = _upload(client, 'Neha Kapoor\nneha2@kapoorexports.com\n09811022334')
    names = [m['name'] for m in (card['dup_matches'] or {}).get('contacts', [])]
    assert 'Existing Person' in names, \
        f'the same number in another format was not matched: {names}'


# ─── model output never bypasses validation ──────────────────────────────
def test_a_malformed_model_response_falls_back_to_the_parser(monkeypatch):
    """The model is not trusted to return what it promised."""
    import app.services.business_card_ocr as ocr
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-key')

    class _Boom:
        def __init__(self, **kw):
            raise RuntimeError('no credit')
    monkeypatch.setattr(ocr, '_MODEL', 'x', raising=False)
    import anthropic
    monkeypatch.setattr(anthropic, 'Anthropic', _Boom)

    out = extract_business_card(_PNG, 'image/png',
                                card_text=CARDS['all_caps'])
    assert out['error'], 'the failure must still be reported'
    assert out['extracted']['name'] == 'SURESH IYER', \
        'the deterministic parse must carry the scan'
