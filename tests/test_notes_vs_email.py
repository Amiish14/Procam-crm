"""The Notes box must never destroy the customer's enquiry again.

email_ingest wrote the inbound email into Lead.notes, and the Notes /
call summary box read, edited and saved that same field — so the first
note anyone wrote replaced the enquiry. These are the acceptance
criteria for the split, as executable checks.
"""
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'NotesTestOnly12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'notes.db')

import importlib                                              # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Employee, Lead = _main.Employee, _main.Lead
LeadNote, LeadEmail = _main.LeadNote, _main.LeadEmail
flask_app.config['WTF_CSRF_ENABLED'] = False

ENQUIRY = """Dear Procam Team,

We have an RFQ for the Toshiba T&D transformer movement from JNPT to
Vadodara. 220 MT, over-dimensional. Payment terms 45 days from delivery.
Please quote by Friday.

Regards,
S. Krishnan
Toshiba T&D India"""


@pytest.fixture(scope='module')
def client():
    with flask_app.app_context():
        db.create_all()
        e = Employee.query.filter_by(emp_code='NOTEADM').first()
        if not e:
            e = Employee(emp_code='NOTEADM', name='Notes Admin')
            db.session.add(e)
        e.role, e.is_active, e.must_change_pw = 'admin', True, False
        e.vertical, e.is_super_admin = 'All', True
        db.session.commit()
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code='NOTEADM', name='Notes Admin', role='admin',
                 vertical='All')
    return c


def _email_lead(company='Toshiba T&D India'):
    """A lead as the ingest now creates one."""
    with flask_app.app_context():
        lead = Lead(company=company, source='email',
                    stage='New Opportunity',
                    email_message_id=f'<{company}@example.com>',
                    original_email_body=ENQUIRY,
                    original_email_subject='RFQ — transformer movement',
                    original_email_from='s.krishnan@toshiba-tnd.co.in',
                    original_email_source='ingested')
        db.session.add(lead)
        db.session.commit()
        return lead.id


# ─── the bug ─────────────────────────────────────────────────────────────
def test_saving_a_note_leaves_the_enquiry_intact(client):
    """The original defect: this used to wipe the email."""
    lid = _email_lead()
    r = client.post(f'/api/leads/{lid}/notes',
                    json={'note_text': 'Called Krishnan, quote by Thursday.',
                          'note_type': 'call'})
    assert r.status_code == 200

    with flask_app.app_context():
        lead = db.session.get(Lead, lid)
        assert lead.original_email_body == ENQUIRY
        assert 'Payment terms 45 days' in lead.original_email_body


def test_the_lead_put_cannot_write_the_notes_field_at_all(client):
    """The exact call the old Save button made.

    It must not reach `notes`, and it must not reach the email — that
    single shared field is the whole bug.
    """
    lid = _email_lead('Put Test Ltd')
    with flask_app.app_context():
        before = db.session.get(Lead, lid).notes

    client.put(f'/api/leads/{lid}',
               json={'notes': 'a note typed into the old box'})

    with flask_app.app_context():
        lead = db.session.get(Lead, lid)
        assert lead.notes == before, 'PUT still writes the legacy field'
        assert lead.original_email_body == ENQUIRY


def test_a_note_sent_the_old_way_is_kept_as_a_note(client):
    """Ignoring it would lose what the user typed."""
    lid = _email_lead('Legacy Client Ltd')
    client.put(f'/api/leads/{lid}',
               json={'notes': 'spoke to the buyer, wants a revised rate'})
    rows = client.get(f'/api/leads/{lid}/notes').get_json()
    assert any('revised rate' in n['note_text'] for n in rows)


def test_the_api_exposes_the_email_separately_from_notes(client):
    lid = _email_lead('Payload Ltd')
    client.post(f'/api/leads/{lid}/notes', json={'note_text': 'a note'})
    d = client.get(f'/api/leads/{lid}').get_json()
    assert d['original_email']['body'] == ENQUIRY
    assert 'a note' not in (d.get('notes') or '')


# ─── notes accumulate ────────────────────────────────────────────────────
def test_a_second_note_does_not_replace_the_first(client):
    lid = _email_lead('Two Notes Ltd')
    client.post(f'/api/leads/{lid}/notes', json={'note_text': 'first call'})
    client.post(f'/api/leads/{lid}/notes', json={'note_text': 'second call'})
    rows = client.get(f'/api/leads/{lid}/notes').get_json()
    texts = [n['note_text'] for n in rows]
    assert 'first call' in texts and 'second call' in texts


def test_every_note_carries_an_author_and_a_timestamp(client):
    lid = _email_lead('Author Ltd')
    client.post(f'/api/leads/{lid}/notes', json={'note_text': 'who said this'})
    note = client.get(f'/api/leads/{lid}/notes').get_json()[0]
    assert note['author'] == 'NOTEADM'
    assert note['author_name'] == 'Notes Admin'
    assert note['created_at']


def test_an_empty_note_is_refused(client):
    lid = _email_lead('Empty Ltd')
    assert client.post(f'/api/leads/{lid}/notes',
                       json={'note_text': '   '}).status_code == 400


def test_the_note_type_is_recorded(client):
    lid = _email_lead('Typed Ltd')
    client.post(f'/api/leads/{lid}/notes',
                json={'note_text': 'met at site', 'note_type': 'meeting'})
    assert client.get(f'/api/leads/{lid}/notes').get_json()[0]['note_type'] \
        == 'meeting'


# ─── delete is scoped to one note ────────────────────────────────────────
def test_deleting_a_note_removes_only_that_note(client):
    lid = _email_lead('Delete Ltd')
    client.post(f'/api/leads/{lid}/notes', json={'note_text': 'keep me'})
    client.post(f'/api/leads/{lid}/notes', json={'note_text': 'delete me'})
    rows = client.get(f'/api/leads/{lid}/notes').get_json()
    target = next(n for n in rows if n['note_text'] == 'delete me')

    assert client.delete(
        f'/api/leads/{lid}/notes/{target["id"]}').status_code == 200

    left = [n['note_text'] for n in
            client.get(f'/api/leads/{lid}/notes').get_json()]
    assert left == ['keep me']
    with flask_app.app_context():
        assert db.session.get(Lead, lid).original_email_body == ENQUIRY


def test_a_note_cannot_be_deleted_through_another_lead(client):
    lid_a = _email_lead('Scope A Ltd')
    lid_b = _email_lead('Scope B Ltd')
    client.post(f'/api/leads/{lid_a}/notes', json={'note_text': 'A note'})
    note_id = client.get(f'/api/leads/{lid_a}/notes').get_json()[0]['id']

    assert client.delete(
        f'/api/leads/{lid_b}/notes/{note_id}').status_code == 404
    assert len(client.get(f'/api/leads/{lid_a}/notes').get_json()) == 1


def test_there_is_no_endpoint_that_deletes_an_email():
    """The enquiry must not be deletable from this screen at all."""
    rules = [str(r) for r in flask_app.url_map.iter_rules()
             if 'emails' in str(r)]
    for rule in rules:
        methods = next(r.methods for r in flask_app.url_map.iter_rules()
                       if str(r) == rule)
        assert 'DELETE' not in methods, f'{rule} exposes DELETE'
        assert 'PUT' not in methods, f'{rule} exposes PUT'


# ─── the email trail ─────────────────────────────────────────────────────
def test_the_trail_shows_the_enquiry_even_with_no_trail_rows(client):
    """Leads that predate the trail still show their email."""
    lid = _email_lead('Fallback Ltd')
    rows = client.get(f'/api/leads/{lid}/emails').get_json()
    assert len(rows) == 1
    assert rows[0]['direction'] == 'inbound'
    assert rows[0]['body'] == ENQUIRY


def test_a_reply_is_appended_not_substituted(client):
    lid = _email_lead('Reply Ltd')
    with flask_app.app_context():
        db.session.add(LeadEmail(
            lead_id=lid, direction='inbound', body=ENQUIRY,
            subject='RFQ', source='ingested', status='received',
            sent_or_received_at=db.func.now()))
        db.session.commit()

    r = client.post(f'/api/leads/{lid}/emails',
                    json={'subject': 'Re: RFQ', 'body': 'Our quote attached.',
                          'to_addr': 's.krishnan@toshiba-tnd.co.in',
                          'status': 'sent'})
    assert r.status_code == 200

    rows = client.get(f'/api/leads/{lid}/emails').get_json()
    assert len(rows) == 2
    bodies = [m['body'] for m in rows]
    assert ENQUIRY in bodies and 'Our quote attached.' in bodies
    assert [m['direction'] for m in rows] == ['inbound', 'outbound']

    with flask_app.app_context():
        assert db.session.get(Lead, lid).original_email_body == ENQUIRY


def test_a_reply_does_not_become_a_note(client):
    lid = _email_lead('Separation Ltd')
    client.post(f'/api/leads/{lid}/emails',
                json={'body': 'a drafted reply', 'status': 'draft'})
    notes = client.get(f'/api/leads/{lid}/notes').get_json()
    assert not any('drafted reply' in n['note_text'] for n in notes)


def test_a_note_does_not_become_an_email(client):
    lid = _email_lead('Separation Two Ltd')
    client.post(f'/api/leads/{lid}/notes', json={'note_text': 'a call note'})
    trail = client.get(f'/api/leads/{lid}/emails').get_json()
    assert not any('a call note' in m['body'] for m in trail)


def test_the_trail_is_chronological(client):
    lid = _email_lead('Order Ltd')
    for n in range(3):
        client.post(f'/api/leads/{lid}/emails',
                    json={'body': f'reply {n}', 'status': 'sent'})
    rows = client.get(f'/api/leads/{lid}/emails').get_json()
    stamps = [m['at'] for m in rows if m['at']]
    assert stamps == sorted(stamps), 'oldest first'


def test_an_empty_email_is_refused(client):
    lid = _email_lead('Empty Mail Ltd')
    assert client.post(f'/api/leads/{lid}/emails',
                       json={'body': '  '}).status_code == 400


# ─── nothing else regressed ──────────────────────────────────────────────
def test_a_lead_with_no_email_still_saves(client):
    """A manually created lead has no enquiry and must be unaffected."""
    with flask_app.app_context():
        lead = Lead(company='Manual Ltd', source='manual',
                    stage='New Opportunity')
        db.session.add(lead)
        db.session.commit()
        lid = lead.id
    r = client.put(f'/api/leads/{lid}', json={'pic': 'Someone',
                                              'phone': '9820011223'})
    assert r.status_code == 200
    d = client.get(f'/api/leads/{lid}').get_json()
    assert d['pic'] == 'Someone'
    assert d['original_email'] is None


def test_the_ingest_no_longer_writes_the_body_into_notes():
    """Static guard on both ingest paths — this is the actual bug."""
    for path in ('email_ingest/pipeline.py', 'email_ingest/enrich.py'):
        src = open(os.path.join(_ROOT, path)).read()
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith('#'):
                continue
            assert not (stripped.startswith('notes=')
                        or stripped.startswith('"notes":')), \
                f'{path} still writes the email body into notes: {stripped}'


def test_the_save_handler_does_not_send_notes():
    """The lead form must not post the field that caused the loss."""
    html = open(os.path.join(_ROOT, 'templates', 'app.html')).read()
    save = html.split('async function saveLd()')[1][:900]
    assert "notes:g('lNotes')" not in save, \
        'Save still posts the notes field over the email'


# ─── legacy text is labelled for what it is ──────────────────────────────
def test_the_lead_payload_says_whether_it_came_from_email(client):
    """The screen decides from this whether legacy `notes` is an enquiry
    or a note — on 9,500-odd records, guessing wrong mislabels them."""
    lid = _email_lead('Provenance Ltd')
    assert client.get(f'/api/leads/{lid}').get_json()['email_message_id']

    with flask_app.app_context():
        manual = Lead(company='Hand Typed Ltd', source='manual',
                      notes='met them at the expo')
        db.session.add(manual)
        db.session.commit()
        mid = manual.id
    d = client.get(f'/api/leads/{mid}').get_json()
    assert d['email_message_id'] == ''
    assert d['source'] == 'manual'


def test_a_manual_lead_is_not_shown_as_having_an_email():
    html = open(os.path.join(_ROOT, 'templates', 'app.html')).read()
    assert 'const legacyIsEmail' in html
    assert 'Earlier note (before the notes split)' in html, \
        'legacy text on a non-email lead must not be called an email'
