"""Business card scanning — CRM-SEARCH-SCAN-01 Part 2.

The spec, item by item, with one test per promise:

    photo / scan / upload / PDF, front and back, any orientation
    auto-rotate, deskew, crop, contrast, THEN read, THEN parse
    every field, phones in E.164, confidence per field
    ALWAYS an editable review form, never auto-save
    the company matched to an account THROUGH ITS ALIASES
    a duplicate check before anything is written
    the card image kept with the contact

The specimens are `app/services/card_samples.py` — eleven drawn
cards, because there are none on file and the decision of 15 Nov was
to build against my own and re-measure on ten real ones after this
ships.
"""
import io
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'cardtest16')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'CardScan-123456789')
os.environ.pop('ANTHROPIC_API_KEY', None)       # the CRM's actual state
os.environ.pop('CARD_TEXT_READER', None)
_TMP = tempfile.mkdtemp()
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(_TMP, 'cards16.db')
os.environ['UPLOAD_ROOT'] = os.path.join(_TMP, 'uploads')

import importlib                                            # noqa: E402

_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Company, Contact, Employee, Lead = (_main.Company, _main.Contact,
                                    _main.Employee, _main.Lead)
flask_app.config['WTF_CSRF_ENABLED'] = False
flask_app.config['UPLOAD_ROOT'] = os.environ['UPLOAD_ROOT']

from app.models.business_card import BusinessCardImport     # noqa: E402
from app.services import (business_card_ocr, card_confidence,  # noqa: E402
                          card_fields, card_image, card_read,
                          card_samples)

ADMIN = 'ZC_ADM'


@pytest.fixture(scope='module')
def client():
    with flask_app.app_context():
        db.create_all()
        row = Employee.query.filter_by(emp_code=ADMIN).first()
        if row is None:
            row = Employee(emp_code=ADMIN, name='Card Admin',
                           password_hash='x')
            db.session.add(row)
        row.role, row.is_active, row.must_change_pw = 'admin', True, False
        row.vertical, row.is_super_admin = 'All', True
        db.session.commit()
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code=ADMIN, name='Card Admin', role='admin',
                 vertical='All')
    return c


@pytest.fixture(autouse=True)
def _clean():
    with flask_app.app_context():
        yield
        Contact.query.filter(Contact.name.like('ZC%')).delete(
            synchronize_session=False)
        Company.query.filter(Company.name.like('ZC%')).delete(
            synchronize_session=False)
        Lead.query.filter(Lead.company.like('ZC%')).delete(
            synchronize_session=False)
        db.session.commit()


def _upload(client, specimen, *, with_back=True):
    front, front_mime = card_samples.as_bytes(specimen)
    data = {'file': (io.BytesIO(front),
                     'card.pdf' if front_mime.endswith('pdf') else 'card.png')}
    if with_back and specimen.get('back'):
        back, _m = card_samples.as_bytes(specimen, side='back')
        data['file_back'] = (io.BytesIO(back), 'back.png')
    # No reader on a test box, so the card's printed text stands in for
    # what one would return — the same substitution the accuracy report
    # names at the top of its output.
    data['card_text'] = card_samples.both_sides_text(specimen)
    answer = client.post('/api/business-cards/upload', data=data,
                         content_type='multipart/form-data')
    assert answer.status_code == 200, answer.get_data(as_text=True)
    return answer.get_json()['card']


# ═══ preparing the picture ═════════════════════════════════════════
def test_the_skew_is_taken_out():
    """A card photographed at an angle is straightened. Measured, not
    asserted: the projection profile of straight text swings more than
    the same text at an angle, which is what the deskew maximises."""
    specimen = card_samples.by_key('skewed')
    data, mime = card_samples.as_bytes(specimen)
    ready = card_image.prepare(data, mime)
    assert ready.pages
    assert any('straightened' in n for n in ready.notes), ready.notes


def test_the_desk_is_cropped_away():
    specimen = card_samples.by_key('skewed')       # 90px of desk round it
    data, mime = card_samples.as_bytes(specimen)
    ready = card_image.prepare(data, mime)
    assert any('cropped' in n for n in ready.notes), ready.notes


def test_a_straight_card_is_not_turned():
    """The guard that matters more than the deskew itself: on a washed
    out photograph the profile wanders, and an unguarded search
    answers with the limit angle every time — a straight card rotated
    eight degrees, which is worse than leaving it alone."""
    specimen = card_samples.by_key('low_contrast')      # no skew in it
    data, mime = card_samples.as_bytes(specimen)
    ready = card_image.prepare(data, mime)
    assert not any('straightened' in n for n in ready.notes), ready.notes


def test_the_skew_found_is_the_skew_put_there():
    specimen = card_samples.by_key('skewed')            # 4.5° of it
    data, mime = card_samples.as_bytes(specimen)
    ready = card_image.prepare(data, mime)
    turned = [n for n in ready.notes if 'straightened' in n]
    assert turned and '-4' in turned[0], ready.notes


def test_preparing_a_card_is_fast_enough_for_a_phone():
    """A scan is taken standing up, in front of somebody. Measured
    rather than assumed: the angle search runs on a thumbnail, which
    is what keeps this off the critical path."""
    import time

    specimen = card_samples.by_key('skewed')
    data, mime = card_samples.as_bytes(specimen)
    started = time.time()
    card_image.prepare(data, mime)
    assert (time.time() - started) < 2.0


def test_a_card_that_fills_the_frame_is_not_cropped_to_pieces():
    """The crop is refused when what it found is not a card. A wrong
    crop loses the half with the phone number on it."""
    from PIL import Image

    img = Image.new('RGB', (600, 360), 'white')
    notes = []
    out = card_image.crop_to_card(img, notes)
    assert out.size == img.size
    assert not notes


def test_a_pdf_is_turned_into_a_picture():
    specimen = card_samples.by_key('pdf')
    data, mime = card_samples.as_bytes(specimen)
    assert mime == 'application/pdf'
    ready = card_image.prepare(data, mime)
    assert ready.pages, ready.notes


def test_front_and_back_are_one_card():
    specimen = card_samples.by_key('back_has_address')
    front, mime = card_samples.as_bytes(specimen)
    back, back_mime = card_samples.as_bytes(specimen, side='back')
    ready = card_image.prepare(front, mime, sides=[(back, back_mime)])
    assert len(ready.pages) == 2
    assert any(n.startswith('back:') for n in ready.notes), ready.notes


def test_any_orientation_is_offered_to_the_reader():
    from PIL import Image

    turns = card_image.quarter_turns(Image.new('RGB', (40, 20), 'white'))
    assert [angle for angle, _img in turns] == [0, 90, 180, 270]


def test_nothing_the_preparation_does_can_lose_the_card():
    """Every step is a try: a stage that fails passes the picture on
    with a note. A scanner that refuses the upload is a scanner down."""
    broken = b'this is not a picture'
    ready = card_image.prepare(broken, 'image/png')
    assert ready.pages == []
    assert ready.notes and 'not a picture' in ready.notes[0]


# ═══ the fields ════════════════════════════════════════════════════
def test_every_number_in_e164():
    for printed, want in (('+91 98200 11111', '+919820011111'),
                          ('9820011111', '+919820011111'),
                          ('022-6789 1234', '+912267891234'),
                          ('+971 50 123 4567', '+971501234567')):
        got, _kind, _why = card_fields.to_e164(printed)
        assert got == want, printed


def test_a_number_that_cannot_be_read_is_kept_and_marked():
    """Dropping it would lose the only way of reaching somebody
    because a library disagreed with a card."""
    found = card_fields.all_phones(['12345', '+91 98200 11111'])
    assert len(found) == 2
    bad = [p for p in found if not p['usable']][0]
    assert bad['printed'] == '12345' and bad['why']


def test_the_name_is_split_and_the_title_taken_off():
    assert card_fields.split_name('Mr. Rajesh Kumar Sharma') == {
        'full': 'Rajesh Kumar Sharma', 'first': 'Rajesh',
        'last': 'Sharma'}
    assert card_fields.split_name('Priya')['last'] == ''


def test_the_address_is_split_into_the_columns_the_crm_has():
    found = card_fields.split_address(
        '301, Tower B, Andheri East, Mumbai 400069, Maharashtra, India')
    assert found['city'] == 'Mumbai'
    assert found['state'] == 'Maharashtra'
    assert found['country'] == 'India'
    assert found['postal_code'] == '400069'


def test_a_state_it_does_not_recognise_is_left_for_the_reviewer():
    """The form is always editable, so a blank field is one keystroke
    and a wrong one is a correction nobody makes."""
    found = card_fields.split_address('12 Hillside, Zzyzx 99999')
    assert found['state'] == '' and found['city'] == ''


def test_the_department_comes_off_the_title():
    assert card_fields.split_designation(
        'Senior Manager - Logistics')['department'] == 'Logistics'
    assert card_fields.split_designation(
        'General Manager (Projects)')['department'] == 'Projects'


def test_the_linkedin_profile_however_it_is_printed():
    for printed in ('linkedin.com/in/ritu-banerjee-8812',
                    'https://www.linkedin.com/in/ritu-banerjee-8812/',
                    'See me on linkedin.com/in/ritu-banerjee-8812.'):
        assert card_fields.linkedin_of(printed).endswith(
            'ritu-banerjee-8812'), printed


# ═══ confidence ════════════════════════════════════════════════════
def test_two_passes_that_agree_are_the_strongest_thing_it_can_say():
    one = {'company': 'Zenith Steel Works Limited'}
    scores = card_confidence.score(one, readings=[one, dict(one)])
    assert scores['company']['score'] == card_confidence.AGREED
    assert not scores['company']['low']


def test_a_disagreement_is_shown_not_resolved():
    scores = card_confidence.score(
        {'company': 'Zenith Steel'},
        readings=[{'company': 'Zenith Steel'},
                  {'company': 'Zenith Steelworks'}],
        conflicts=['company'])
    assert scores['company']['low'] is True
    assert 'Zenith Steelworks' in scores['company']['why']


def test_something_worked_out_rather_than_read_says_so():
    scores = card_confidence.score({'company': 'Deccanports'},
                                   derived=['company'])
    assert scores['company']['low'] is True
    assert 'worked out' in scores['company']['why']
    assert 'company' in card_confidence.low_fields(scores)


def test_the_summary_names_what_to_look_at():
    scores = card_confidence.score({'company': 'X Ltd', 'city': 'Mumbai'},
                                   derived=['city'])
    assert 'city' in card_confidence.summary(scores)


def test_a_state_implied_by_a_city_is_marked_and_one_printed_is_not():
    """The honest distinction: "Tamil Nadu" worked out from "Chennai"
    is a guess worth a glance; "Maharashtra" printed on the card is
    not."""
    printed = business_card_ocr.extract_business_card(
        b'', 'image/png', card_text=card_samples.text_of(
            card_samples.by_key('plain')))['extracted']
    assert printed['confidence']['state']['low'] is False

    implied = business_card_ocr.extract_business_card(
        b'', 'image/png', card_text='Priya Nair\nGM\nKonkan Heavy\n'
                                    '+91 99400 55555\np@konkanheavy.co.in\n'
                                    'SIPCOT, Chennai 600032')['extracted']
    assert implied['state'] == 'Tamil Nadu'
    assert implied['confidence']['state']['low'] is True


# ═══ the reader, and the switch ════════════════════════════════════
def test_the_on_server_reader_is_off_until_it_is_switched_on():
    reader, why = card_read.text_reader()
    assert reader is None
    assert card_read.SWITCH in why


def test_the_switch_says_what_is_missing_rather_than_failing():
    os.environ[card_read.SWITCH] = 'on'
    try:
        reader, why = card_read.text_reader()
        assert reader is None or why == ''
        if reader is None:
            assert 'not installed' in why or 'cannot run' in why
    finally:
        os.environ.pop(card_read.SWITCH, None)


def test_which_way_up_is_decided_by_reading_not_guessing():
    """A card's text has an @, digits and a domain in it; a desk does
    not. That is the whole measure, and it is why the choice survives
    a card photographed upside down."""
    upright = card_read._card_likeness(
        'Rajesh Sharma\nrajesh@zenithsteel.com\n+91 98200 11111')
    noise = card_read._card_likeness('|| ~~ ** ||')
    assert upright > noise


# ═══ the whole scan ════════════════════════════════════════════════
def test_a_scan_creates_a_review_form_and_nothing_else(client):
    """"ALWAYS an editable review form, never auto-save."""
    with flask_app.app_context():
        before = Contact.query.count(), Company.query.count()
    card = _upload(client, card_samples.by_key('plain'))
    with flask_app.app_context():
        assert (Contact.query.count(), Company.query.count()) == before
        row = db.session.get(BusinessCardImport, card['id'])
        assert row.status == 'Extracted'
    assert card['extracted']['name'] == 'Rajesh Kumar Sharma'
    assert card['confidence']


def test_the_back_of_the_card_is_read_too(client):
    """The address is on the back of this one, and it must reach the
    form — scanning the back separately is the duplicate this screen
    exists to avoid."""
    card = _upload(client, card_samples.by_key('back_has_address'))
    found = card['extracted']
    assert card['has_back'] is True
    assert found['city'] == 'Kochi'
    assert found['postal_code'] == '682030'
    assert card['back_image_url']


def test_the_review_payload_says_what_was_done_to_the_picture(client):
    card = _upload(client, card_samples.by_key('skewed'))
    assert card['prep_notes'], card
    assert any('straightened' in n or 'cropped' in n
               for n in card['prep_notes'])


def test_the_pdf_card_reaches_the_same_form(client):
    card = _upload(client, card_samples.by_key('pdf'))
    assert card['extracted']['email'] == \
        'harpreet.singh@punjabagromovers.com'


def test_every_field_is_editable_on_the_screen(client):
    html = client.get('/business-cards/scan').get_data(as_text=True)
    for field in ('first_name', 'last_name', 'department', 'postal_code',
                  'industry', 'linkedin_url'):
        assert f"['{field}'," in html, field
    assert 'nothing is saved until' in html.lower()


def test_no_vendor_name_is_on_the_screen(client):
    """CRM-SEARCH-SCAN-01: "No AI/vendor names on any screen."""
    html = client.get('/business-cards/scan').get_data(as_text=True).lower()
    for word in ('anthropic', 'claude', 'openai', 'gpt', 'tesseract',
                 'groq', 'llama', 'gemini'):
        assert word not in html, word


# ═══ what happens on Save ══════════════════════════════════════════
def _save(client, card_id, fields, **extra):
    body = {'fields': fields, 'choice': 'new'}
    body.update(extra)
    return client.post(f'/api/business-cards/{card_id}/save', json=body)


def test_the_company_is_matched_through_its_alias(client):
    """The fault of 14 Nov, which cost a hand-merge on live: a card
    printed with the full name must find the account filed under the
    short one, not make a second account for it."""
    with flask_app.app_context():
        account = Company(name='ZC Amns', is_active=True,
                          aliases=['ZC ArcelorMittal Nippon Steel India'])
        db.session.add(account)
        db.session.commit()
        account_id, count_before = account.id, Company.query.count()

    card = _upload(client, card_samples.by_key('plain'))
    fields = dict(card['extracted'])
    fields['company'] = 'ZC ArcelorMittal Nippon Steel India'
    fields['name'] = 'ZC Test Person'
    fields['email'] = 'zc.person@amns.example'
    fields['mobile'] = '+919000000111'
    answer = _save(client, card['id'], fields)
    assert answer.status_code == 200, answer.get_data(as_text=True)
    with flask_app.app_context():
        assert Company.query.count() == count_before    # none created
        made = Contact.query.filter_by(name='ZC Test Person').first()
        assert made is not None
        assert made.account_id == account_id


def test_the_card_image_stays_with_the_contact(client):
    card = _upload(client, card_samples.by_key('linkedin'))
    fields = dict(card['extracted'])
    fields['name'] = 'ZC Kept Image'
    fields['company'] = 'ZC Hooghly Marine'
    fields['email'] = 'zc.kept@hooghlymarine.example'
    assert _save(client, card['id'], fields).status_code == 200
    with flask_app.app_context():
        made = Contact.query.filter_by(name='ZC Kept Image').first()
        assert made.card_image_path
        assert os.path.isfile(made.card_image_path)


def test_the_new_columns_are_written(client):
    card = _upload(client, card_samples.by_key('plain'))
    fields = dict(card['extracted'])
    fields.update({'name': 'ZC Full Row', 'company': 'ZC Zenith Steel',
                   'email': 'zc.full@zenithsteel.example',
                   'department': 'Logistics', 'postal_code': '400069'})
    assert _save(client, card['id'], fields).status_code == 200
    with flask_app.app_context():
        made = Contact.query.filter_by(name='ZC Full Row').first()
        assert made.department == 'Logistics'
        assert made.postal_code == '400069'
        assert made.city == 'Mumbai' and made.state == 'Maharashtra'


def test_a_person_who_is_only_on_a_lead_is_still_a_duplicate(client):
    """The Part 1 search reads the LEADS as well. Most people in this
    CRM are on a lead and nowhere else, so a check that reads only
    `contacts` calls somebody we quoted last week a new person."""
    with flask_app.app_context():
        db.session.add(Lead(pic='ZC Lead Person', company='ZC Lead Co',
                            email='zc.lead@leadco.example',
                            stage='New Opportunity', assigned_to=ADMIN))
        db.session.commit()
    card = _upload(client, card_samples.by_key('plain'))
    fields = dict(card['extracted'])
    fields.update({'name': 'ZC Lead Person', 'company': 'ZC Lead Co',
                   'email': 'zc.lead@leadco.example'})
    answer = _save(client, card['id'], fields)
    assert answer.status_code == 409, answer.get_data(as_text=True)
    body = answer.get_json()
    assert body['duplicate'] is True
    assert body['people'] and body['people'][0]['person'] == 'ZC Lead Person'
    with flask_app.app_context():
        assert Contact.query.filter_by(name='ZC Lead Person').first() is None

    # …and it can still be created, on purpose, with the matches shown.
    again = _save(client, card['id'], fields, confirm_duplicate=True)
    assert again.status_code == 200, again.get_data(as_text=True)
    with flask_app.app_context():
        assert Contact.query.filter_by(name='ZC Lead Person').first()


# ═══ the measurement ═══════════════════════════════════════════════
def test_the_accuracy_report_runs_and_says_what_it_did_not_measure():
    """The report must never claim a reading accuracy it did not
    measure: there is no reader on this box, and it says so."""
    import importlib.util

    path = os.path.join(_ROOT, 'scripts', '2026_11_16_card_accuracy.py')
    spec = importlib.util.spec_from_file_location('card_accuracy', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with flask_app.app_context():
        rows, totals = module.run()
    assert len(rows) >= 10
    assert totals['after'] >= totals['before']
    assert totals['fields'] > 0


def test_there_are_at_least_ten_varied_specimens():
    assert len(card_samples.SPECIMENS) >= 10
    shots = [tuple(sorted((s.get('shot') or {}).keys()))
             for s in card_samples.SPECIMENS]
    assert len(set(shots)) >= 4, shots
    assert any(s.get('back') for s in card_samples.SPECIMENS)
    assert any((s.get('shot') or {}).get('as_pdf')
               for s in card_samples.SPECIMENS)


def test_the_industry_comes_from_the_account_and_says_so(client):
    """A card does not print what the company does. The account knows;
    the screen marks it as worked out, not read."""
    with flask_app.app_context():
        db.session.add(Company(name='ZC Zenith Steel Works Limited',
                               is_active=True, industry='Steel'))
        db.session.commit()
    card = _upload(client, card_samples.by_key('plain'))
    fields = dict(card['extracted'])
    assert fields.get('industry') in ('', None) or fields['industry']
    # Asked directly, so the test does not depend on the fuzzy match
    # of a specimen name against a seeded one.
    from app.business_card.routes import _industry_from_account
    with flask_app.app_context():
        found, conf = {'company': 'ZC Zenith Steel Works Limited'}, {}
        _industry_from_account(found, conf)
        assert found['industry'] == 'Steel'
        assert conf['industry']['low'] is True
        assert 'not' in conf['industry']['why']


def test_the_card_can_be_looked_at_again_from_the_contact(client):
    """Keeping the image is only half of it: somebody has to be able
    to open it. A wrong reading is found by looking at the card."""
    card = _upload(client, card_samples.by_key('three_numbers'))
    fields = dict(card['extracted'])
    fields.update({'name': 'ZC Lookable', 'company': 'ZC Vindhya Power',
                   'email': 'zc.look@vindhyapower.example'})
    assert _save(client, card['id'], fields).status_code == 200
    with flask_app.app_context():
        made = Contact.query.filter_by(name='ZC Lookable').first()
        contact_id = made.id
        assert made.to_dict()['has_card_image'] is True
    answer = client.get(f'/api/contacts/{contact_id}/card-image')
    assert answer.status_code == 200
    assert answer.headers['Content-Type'].startswith('image/')
    assert answer.headers['X-Content-Type-Options'] == 'nosniff'


def test_a_contact_with_no_card_offers_no_image(client):
    with flask_app.app_context():
        row = Contact(name='ZC No Card', assigned_to=ADMIN)
        db.session.add(row)
        db.session.commit()
        contact_id = row.id
        assert row.to_dict()['has_card_image'] is False
    assert client.get(f'/api/contacts/{contact_id}/card-image').status_code \
        == 404


# ═══ the fault of 15 Nov on live: a reader that failed in silence ═══
# Every specimen read back empty, the report said READING IS MEASURED,
# and journalctl had not one line about it. Three faults, one test
# each, plus the command that would have found it in a minute.

def _fake_service(answer=None, raise_with=None):
    """A stand-in for the client library, recording what it was sent."""
    import types

    sent = {}

    class _Message:
        def __init__(self, text):
            self.content = [types.SimpleNamespace(text=text)]

    class _Messages:
        def create(self, **kw):
            sent.update(kw)
            if raise_with is not None:
                raise raise_with
            return _Message(answer or '{}')

    class _Client:
        def __init__(self, **kw):
            self.messages = _Messages()

    module = types.ModuleType('anthropic')
    module.Anthropic = _Client
    module.__version__ = 'test'
    return module, sent


def test_the_prepared_picture_is_what_gets_sent(monkeypatch):
    """The other half of the 15 Nov fault: the straightening, the
    crop and the contrast were computed and then thrown away — the
    reader was handed the ORIGINAL bytes and the original type."""
    module, sent = _fake_service(
        '{"name": "Rajesh Kumar Sharma", "company": "Zenith"}')
    monkeypatch.setitem(sys.modules, 'anthropic', module)
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-only-not-a-key')

    specimen = card_samples.by_key('skewed')
    original, mime = card_samples.as_bytes(specimen)
    found = business_card_ocr.extract_business_card(original, mime=mime)
    assert found['error'] is None, found['error']
    blocks = [b for b in sent['messages'][0]['content']
              if b.get('type') == 'image']
    assert blocks, sent
    import base64
    was_sent = base64.b64decode(blocks[0]['source']['data'])
    assert blocks[0]['source']['media_type'] == 'image/png'
    assert was_sent != original          # prepared, not the upload


def test_a_pdf_is_never_sent_as_a_pdf(monkeypatch):
    """A PDF inside an image block is refused by the service, and on
    live that refusal was swallowed — which is why the PDF specimen
    read back empty with no reason."""
    module, sent = _fake_service('{"name": "Harpreet Singh"}')
    monkeypatch.setitem(sys.modules, 'anthropic', module)
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-only-not-a-key')

    data, mime = card_samples.as_bytes(card_samples.by_key('pdf'))
    assert mime == 'application/pdf'
    business_card_ocr.extract_business_card(data, mime=mime)
    for block in sent['messages'][0]['content']:
        if block.get('type') == 'image':
            assert block['source']['media_type'] in \
                business_card_ocr.IMAGE_TYPES


def test_a_type_it_cannot_send_is_said_so_not_swallowed(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-only-not-a-key')
    found = business_card_ocr.read_with_service(
        [(b'%PDF-1.4 whatever', 'application/pdf')])
    assert found['fields'] is None
    assert 'not a picture it accepts' in (found['error'] + ''.join(
        found['notes']))


def test_a_reader_failure_is_logged_and_returned(monkeypatch, caplog):
    """NEVER SWALLOWED. The status, the exception type and the first
    of the message, at WARNING, where journalctl will show it."""
    import logging

    class _Boom(Exception):
        status_code = 404

    module, _sent = _fake_service(
        raise_with=_Boom('model: claude-nope not_found_error'))
    monkeypatch.setitem(sys.modules, 'anthropic', module)
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-only-not-a-key')

    data, mime = card_samples.as_bytes(card_samples.by_key('plain'))
    with caplog.at_level(logging.WARNING):
        found = business_card_ocr.extract_business_card(data, mime=mime)
    assert found['error'] and 'HTTP 404' in found['error']
    assert 'not_found_error' in found['error']
    assert any('image service failed' in r.getMessage()
               for r in caplog.records), caplog.records


def test_an_answer_without_json_is_a_reason_not_an_empty_form(monkeypatch):
    module, _sent = _fake_service('I cannot read this image.')
    monkeypatch.setitem(sys.modules, 'anthropic', module)
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-only-not-a-key')
    data, mime = card_samples.as_bytes(card_samples.by_key('plain'))
    found = business_card_ocr.extract_business_card(data, mime=mime)
    assert 'without any JSON' in (found['error'] or '')


def test_the_key_is_never_printed_or_returned(monkeypatch):
    """The debug command answers yes or no. Not a prefix, not a
    length, not the key."""
    secret = 'sk-ant-secret-value-do-not-print-0123456789'
    monkeypatch.setenv('ANTHROPIC_API_KEY', secret)
    ready, why = business_card_ocr.service_ready()
    assert ready is True and why == ''
    found = business_card_ocr.read_with_service([])
    assert secret not in (found['error'] + found['raw'])


def test_the_scan_page_says_the_reader_failed(client):
    html = client.get('/business-cards/scan').get_data(as_text=True)
    assert 'The image reader could not read this card' in html
    assert 'Nothing below was read off it' in html


def test_the_debug_command_reports_without_leaking(monkeypatch, capsys):
    import importlib.util

    secret = 'sk-ant-another-secret-0123456789abcdef'
    monkeypatch.setenv('ANTHROPIC_API_KEY', secret)
    monkeypatch.setenv('BUSINESS_CARD_MODEL', 'test-model-name')
    module, _sent = _fake_service('{"name": "Rajesh Kumar Sharma"}')
    monkeypatch.setitem(sys.modules, 'anthropic', module)

    path = os.path.join(_ROOT, 'scripts', '2026_11_16_card_debug.py')
    spec = importlib.util.spec_from_file_location('card_debug', path)
    debug = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(debug)
    with flask_app.app_context():
        assert debug.main(['--no-socket']) == 0
    printed = capsys.readouterr().out
    assert 'key visible to this process: yes' in printed
    assert 'test-model-name' in printed
    assert 'HTTP status:   200' in printed
    assert secret not in printed
    assert 'sk-ant' not in printed


def test_the_debug_command_says_when_no_key_is_visible(monkeypatch, capsys):
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    path = os.path.join(_ROOT, 'scripts', '2026_11_16_card_debug.py')
    import importlib.util
    spec = importlib.util.spec_from_file_location('card_debug2', path)
    debug = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(debug)
    with flask_app.app_context():
        assert debug.main(['--no-socket']) == 2      # stopped, nothing sent
    printed = capsys.readouterr().out
    assert 'key visible to this process: NO' in printed
    assert 'EnvironmentFile' in printed              # where to look next


def test_the_accuracy_report_refuses_to_claim_a_reading_it_did_not_get(
        monkeypatch, capsys):
    """The 15 Nov report said READING IS MEASURED at the top and
    printed zeros underneath."""
    import importlib.util

    class _Boom(Exception):
        status_code = 401

    module, _sent = _fake_service(raise_with=_Boom('invalid x-api-key'))
    monkeypatch.setitem(sys.modules, 'anthropic', module)
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-only-not-a-key')

    path = os.path.join(_ROOT, 'scripts', '2026_11_16_card_accuracy.py')
    spec = importlib.util.spec_from_file_location('card_acc2', path)
    report = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(report)
    with flask_app.app_context():
        report.run()
    printed = capsys.readouterr().out
    assert 'READING IS MEASURED' not in printed
    assert 'THE READER FAILED ON 11 OF 11' in printed
    assert 'NOT a measurement of the reader' in printed


# ═══ the on-server reader, live 15 Nov ═════════════════════════════
# The key was commented out and CARD_TEXT_READER=on. The card was
# read correctly — and the extractor then reported the ABSENT image
# service as a failure, so the report called a good reading "not a
# measurement" and fell back to the printed text, and the scan page
# put an error banner over a form that was correctly filled.

def _fake_tesseract(text='Rajesh Kumar Sharma\nZENITH STEEL WORKS LIMITED\n'
                         '+91 98200 11111\nrajesh.sharma@zenithsteel.com',
                    osd=0, raise_with=None, record=None):
    import types

    module = types.ModuleType('pytesseract')
    module.get_tesseract_version = lambda: '5.3.4'
    module.get_languages = lambda config='': ['eng', 'osd']

    def image_to_string(img, lang='eng', config=''):
        if record is not None:
            record.append({'mode': img.mode, 'size': img.size,
                           'lang': lang, 'config': config})
        if raise_with is not None:
            raise raise_with
        return text

    def image_to_osd(img):
        return f'Page number: 0\nRotate: {osd}\n'

    module.image_to_string = image_to_string
    module.image_to_osd = image_to_osd
    module.pytesseract = types.SimpleNamespace(
        tesseract_cmd='/usr/bin/tesseract')
    return module


def test_no_key_is_not_a_failure_when_the_on_server_reader_read_it(
        monkeypatch):
    """THE FAULT: the card was read, and the extraction still reported
    'no key is visible' as the error."""
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    monkeypatch.setenv(card_read.SWITCH, 'on')
    monkeypatch.setitem(sys.modules, 'pytesseract', _fake_tesseract())

    data, mime = card_samples.as_bytes(card_samples.by_key('plain'))
    found = business_card_ocr.extract_business_card(data, mime=mime)
    assert found['error'] is None, found['error']
    assert found['extracted']['name'] == 'Rajesh Kumar Sharma'
    assert found['extracted']['mobile'] == '+919820011111'


def test_the_scan_page_shows_no_banner_when_it_was_read(client,
                                                        monkeypatch):
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    monkeypatch.setenv(card_read.SWITCH, 'on')
    monkeypatch.setitem(sys.modules, 'pytesseract', _fake_tesseract())

    specimen = card_samples.by_key('plain')
    front, mime = card_samples.as_bytes(specimen)
    answer = client.post('/api/business-cards/upload',
                         data={'file': (io.BytesIO(front), 'card.png')},
                         content_type='multipart/form-data')
    card = answer.get_json()['card']
    assert card['ocr_error'] in (None, '')      # nothing for the banner
    assert card['extracted']['name'] == 'Rajesh Kumar Sharma'


def test_nothing_read_at_all_is_still_said(monkeypatch):
    """Silent only when it WORKED. A reader that runs and finds no
    text still has to say so."""
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    monkeypatch.setenv(card_read.SWITCH, 'on')
    monkeypatch.setitem(sys.modules, 'pytesseract', _fake_tesseract(text=''))

    data, mime = card_samples.as_bytes(card_samples.by_key('plain'))
    found = business_card_ocr.extract_business_card(data, mime=mime)
    assert found['error'] and 'nothing read this card' in found['error']


def test_the_reader_is_given_a_greyscale_picture_big_enough_to_read(
        monkeypatch):
    """Mode and size are the first suspects when a reader answers
    nothing, so they are pinned."""
    seen = []
    monkeypatch.setenv(card_read.SWITCH, 'on')
    monkeypatch.setitem(sys.modules, 'pytesseract',
                        _fake_tesseract(record=seen))

    data, mime = card_samples.as_bytes(card_samples.by_key('plain'))
    ready = card_image.prepare(data, mime)
    card_read.read_image(ready.pages[0])
    assert seen, 'the reader was never called'
    assert seen[0]['mode'] == 'L'
    assert seen[0]['size'][0] >= 1000
    assert seen[0]['lang'] == 'eng'
    assert '--psm' in seen[0]['config']


def test_a_card_is_not_read_as_a_page_of_running_text(monkeypatch):
    """psm 3, the default, assumes a page. A card is a few short
    lines scattered over a small area — psm 6, then 11 when that
    reads poorly."""
    seen = []
    monkeypatch.setenv(card_read.SWITCH, 'on')
    monkeypatch.setitem(sys.modules, 'pytesseract',
                        _fake_tesseract(text='x', record=seen))
    data, mime = card_samples.as_bytes(card_samples.by_key('plain'))
    ready = card_image.prepare(data, mime)
    card_read.read_image(ready.pages[0])
    used = {s['config'] for s in seen}
    assert '--oem 3 --psm 6' in used
    assert card_read.SPARSE_CONFIG in used      # a poor read tried both


def test_which_way_up_is_one_question_not_four(monkeypatch):
    """The orientation model answers it in one call. Four reads of
    every card is four times the wait on a phone."""
    seen = []
    monkeypatch.setenv(card_read.SWITCH, 'on')
    monkeypatch.setitem(sys.modules, 'pytesseract',
                        _fake_tesseract(osd=90, record=seen))
    data, mime = card_samples.as_bytes(card_samples.by_key('upside_down'))
    ready = card_image.prepare(data, mime)
    found = card_read.read_image(ready.pages[0])
    assert found['rotation'] == 90
    assert len(seen) <= 2                        # not four turns


def test_the_reader_failing_is_reported_not_swallowed(monkeypatch):
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    monkeypatch.setenv(card_read.SWITCH, 'on')
    monkeypatch.setitem(sys.modules, 'pytesseract',
                        _fake_tesseract(raise_with=RuntimeError('boom')))
    data, mime = card_samples.as_bytes(card_samples.by_key('plain'))
    found = card_read.read_image(card_image.prepare(data, mime).pages[0])
    assert 'boom' in found['error']


def test_the_binary_can_be_pointed_at(monkeypatch):
    """systemd has its own PATH, which is the commonest reason this
    works in a shell and not in the service."""
    module = _fake_tesseract()
    monkeypatch.setitem(sys.modules, 'pytesseract', module)
    monkeypatch.setenv(card_read.SWITCH, 'on')
    monkeypatch.setenv(card_read.CMD, '/opt/bin/tesseract')
    reader, why = card_read.text_reader()
    assert reader is not None and why == ''
    assert module.pytesseract.tesseract_cmd == '/opt/bin/tesseract'


def test_the_debug_command_tests_the_on_server_reader(monkeypatch, capsys):
    """It stopped at "no key" and never tried the reader that was
    actually configured."""
    import importlib.util

    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    monkeypatch.setenv(card_read.SWITCH, 'on')
    monkeypatch.setitem(sys.modules, 'pytesseract', _fake_tesseract())

    path = os.path.join(_ROOT, 'scripts', '2026_11_16_card_debug.py')
    spec = importlib.util.spec_from_file_location('card_debug3', path)
    debug = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(debug)
    with flask_app.app_context():
        assert debug.main(['--no-socket']) == 0
    printed = capsys.readouterr().out
    assert 'the on-server reader call' in printed
    assert '/usr/bin/tesseract' in printed       # the binary
    assert '5.3.4' in printed                    # the version
    assert 'eng' in printed                      # the languages
    assert '--psm' in printed                    # the segmentation
    assert 'mode L' in printed                   # the picture it got
    assert 'ms' in printed                       # elapsed
    assert 'Rajesh Kumar Sharma' in printed      # the first 300 chars


def test_the_debug_command_says_a_reader_that_found_nothing(
        monkeypatch, capsys):
    import importlib.util

    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    monkeypatch.setenv(card_read.SWITCH, 'on')
    monkeypatch.setitem(sys.modules, 'pytesseract',
                        _fake_tesseract(text=''))
    path = os.path.join(_ROOT, 'scripts', '2026_11_16_card_debug.py')
    spec = importlib.util.spec_from_file_location('card_debug4', path)
    debug = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(debug)
    with flask_app.app_context():
        assert debug.main(['--no-socket']) == 1
    printed = capsys.readouterr().out
    assert 'found NO TEXT' in printed
    assert 'psm 11' in printed                   # what to try next


def test_the_report_does_not_call_an_unconfigured_service_a_failure(
        monkeypatch, capsys):
    import importlib.util

    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    monkeypatch.setenv(card_read.SWITCH, 'on')
    monkeypatch.setitem(sys.modules, 'pytesseract', _fake_tesseract())

    path = os.path.join(_ROOT, 'scripts', '2026_11_16_card_accuracy.py')
    spec = importlib.util.spec_from_file_location('card_acc3', path)
    report = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(report)
    with flask_app.app_context():
        report.run()
    printed = capsys.readouterr().out
    assert 'THE READER FAILED' not in printed
    assert 'the on-server reader: 5.3.4' in printed


def test_the_pdf_reader_is_asked_for_by_its_name():
    """pymupdf, not fitz — the old name warns on import."""
    source = open(os.path.join(_ROOT, 'app', 'services',
                               'card_image.py')).read()
    assert 'import pymupdf' in source
    assert source.index('import pymupdf') < source.index('import fitz')


def test_the_screen_says_reader_not_service(client):
    """UAT 17 Nov §3, and the no-vendor-names rule: the person
    scanning a card does not know or care which reader it is."""
    html = client.get('/business-cards/scan').get_data(as_text=True)
    assert 'the image reader is unavailable' in html
    assert 'image service' not in html
    assert 'vision' not in html.lower()


# ═══ CRM-UI-01 Stage 2 — the shell ═════════════════════════════════
def test_one_hamburger_only(client):
    """Stage 0 found two: #sbHamb in the markup and #mobile-burger
    injected beside it, toggling a selector that fell back to `nav`."""
    html = client.get('/app').get_data(as_text=True)
    assert 'id="sbHamb"' in html
    assert "b.id = 'mobile-burger'" not in html


def test_the_phone_gets_a_bottom_bar_with_five(client):
    html = client.get('/app').get_data(as_text=True)
    assert 'id="crmTabs"' in html
    for tab in ('tabHome', 'tabLeads', 'tabScan', 'tabFind', 'tabMore'):
        assert f'id="{tab}"' in html, tab


def test_sign_out_is_reachable_from_more(client):
    """It ran off the right edge of a 360px top bar."""
    html = client.get('/app').get_data(as_text=True)
    assert 'id="crmMore"' in html
    assert 'crmSignOut()' in html
    assert 'more-out' in html


def test_the_scan_and_chat_buttons_cannot_share_a_corner():
    css = open(os.path.join(_ROOT, 'static', 'css', 'shell.css')).read()
    # On a phone the floating scan button is gone (Scan is a tab) and
    # the chat button sits above the bar.
    assert '#crm-fab { display: none !important; }' in css
    assert '.pai-fab' in css and 'bottom: calc(76px' in css
    # And in the 768-899 band, where there is no bar, it clears the FAB.
    assert '@media (min-width: 768px) and (max-width: 899px)' in css


def test_the_tap_minimum_is_a_token_not_a_number():
    tokens = open(os.path.join(_ROOT, 'static', 'css', 'tokens.css')).read()
    assert '--tap-min: 44px' in tokens
    css = open(os.path.join(_ROOT, 'static', 'css', 'shell.css')).read()
    assert 'var(--tap-min)' in css


def test_there_is_exactly_one_palette():
    """Three `:root` palettes disagreed about the brand red."""
    import glob

    roots = []
    for path in (glob.glob(os.path.join(_ROOT, 'static', 'css', '*.css'))
                 + [os.path.join(_ROOT, 'templates', 'app.html')]):
        body = open(path).read()
        if ':root{' in body or ':root {' in body:
            roots.append(os.path.basename(path))
    assert roots == ['tokens.css'], roots


def test_every_stylesheet_carries_a_version(client):
    """Live, 17 Nov: Stage 1 and 2 were deployed and phones kept
    serving the old CSS out of cache. A changed URL is the only thing
    a browser reliably treats as a different file."""
    import re

    for page in ('/app', '/contacts/search', '/business-cards/scan'):
        html = client.get(page).get_data(as_text=True)
        links = re.findall(r'href="[^"]*?/static/css/([a-z0-9_.-]+\.css)'
                           r'(\?v=[^"]*)?"', html)
        assert links, page
        for name, version in links:
            assert version, f'{name} on {page} carries no ?v='


def test_the_token_file_is_linked_not_imported():
    """An @import carries no `?v=`, so the palette would stay cached
    while the rest of the deploy arrived."""
    css = open(os.path.join(_ROOT, 'static', 'css', 'crm.css')).read()
    # The word appears in the comment that explains why there is no
    # import; what must not be there is the rule.
    assert '@import url' not in css
    page = open(os.path.join(_ROOT, 'templates',
                             'contact_search.html')).read()
    assert 'tokens.css?v=' in page
    assert page.index('tokens.css') < page.index('crm.css')


# ═══ CRM-UI-01 Stage 3 — lists ═════════════════════════════════════
def test_a_table_becomes_a_card_list_on_a_phone(client):
    html = client.get('/app').get_data(as_text=True)
    assert 'css/lists.css' in html
    assert 'js/lists.js' in html
    css = open(os.path.join(_ROOT, 'static', 'css', 'lists.css')).read()
    assert 'content: attr(data-label)' in css
    assert '@media (max-width: 767px)' in css


def test_the_labels_come_from_the_table_not_from_each_renderer():
    """Forty render functions would each have to be edited, and the
    forty-first would be forgotten."""
    js = open(os.path.join(_ROOT, 'static', 'js', 'lists.js')).read()
    assert "setAttribute('data-label'" in js
    assert 'MutationObserver' in js          # rows are replaced wholesale


def test_the_filters_are_moved_into_the_sheet_not_copied():
    """A copy would have the same ids as the originals, and every
    `g('lSt').value` in the app reads by id."""
    js = open(os.path.join(_ROOT, 'static', 'js', 'lists.js')).read()
    assert 'appendChild(el)' in js
    assert 'cloneNode' not in js


def test_find_a_person_does_not_scroll_the_page_sideways():
    """Stage 0's only horizontal scroll: ten columns, 1043px of table
    in a 768px screen, and part of every row unreachable."""
    page = open(os.path.join(_ROOT, 'templates',
                             'contact_search.html')).read()
    assert 'fs-tw' in page and 'overflow-x:auto' in page
    assert "data-label=" in page             # and a card list below 768


def test_a_dialog_is_never_taller_than_the_screen():
    """My own regression from the phone type bump: the lead sheet was
    849px tall in an 800px screen with `overflow:hidden`, so its
    buttons could not be reached."""
    css = open(os.path.join(_ROOT, 'static', 'css', 'shell.css')).read()
    assert '100dvh' in css
    assert 'body.modal-open #crmTabs' in css


def test_the_bottom_bar_steps_aside_for_a_dialog(client):
    html = client.get('/app').get_data(as_text=True)
    assert "classList.toggle('modal-open'" in html
