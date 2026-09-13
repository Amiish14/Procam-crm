"""Duplicate detection — all seven signals, against a real database.

A duplicate is filed against an existing lead instead of creating one, so
a false duplicate loses an enquiry. Each signal is therefore tested both
ways: the classic duplicate it exists to catch, and the near miss it must
leave alone — a different route, a generic attachment name, a different
reference number.
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'DuplicateTestOnly12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'duplicates.db')

import importlib                                              # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Lead, LeadEmail, LeadAttachment = _main.Lead, _main.LeadEmail, \
    _main.LeadAttachment
Company, EmailClassification = _main.Company, _main.EmailClassification

from app.services import lead_intake as li                    # noqa: E402
from app.services import lead_intake_db as lidb               # noqa: E402

_W = li.DUPLICATE_WEIGHTS


def msg(subject='', body='', frm='buyer@dupsteel.example', attachments=(),
        conversation=None, mid=None, in_reply_to=None):
    m = {'subject': subject, 'body': {'content': body},
         'from': {'emailAddress': {'address': frm}},
         'toRecipients': [{'emailAddress': {'address': 'leads@procamgroup.in'}}],
         'ccRecipients': [],
         'attachments': [{'name': n} for n in attachments]}
    if attachments:
        m['hasAttachments'] = True
    if conversation:
        m['conversationId'] = conversation
    if mid:
        m['internetMessageId'] = mid
    if in_reply_to:
        m['internetMessageHeaders'] = [{'name': 'In-Reply-To',
                                        'value': in_reply_to}]
    return m


@pytest.fixture(scope='module')
def world():
    """Leads made here are neutralised at the end rather than deleted.

    Deleting a lead frees its id, and SQLite hands that id to the next
    insert — where another module's orphaned trail rows would reappear on
    a stranger's lead. So the trail rows and attachments go, and each
    lead row stays with nothing left that any lookup can match.
    """
    made = {'leads': [], 'companies': []}
    with flask_app.app_context():
        db.create_all()
        steel = Company(name='Dup Steel Ltd', is_active=True,
                        email_domains=['dupsteel.example',
                                       'dupsteel-group.example'])
        db.session.add(steel)
        db.session.commit()
        made['companies'].append(steel.id)
    made['steel'] = made['companies'][0]

    def lead(**kw):
        kw.setdefault('source', 'email')
        kw.setdefault('stage', 'New Opportunity')
        kw.setdefault('company', 'Dup Test')
        kw.setdefault('created_at', datetime.utcnow() - timedelta(days=2))
        files = kw.pop('files', ())
        extracted = kw.pop('extracted', None)
        if extracted is not None:
            kw['email_extracted_json'] = json.dumps(extracted)
        with flask_app.app_context():
            row = Lead(**kw)
            db.session.add(row)
            db.session.flush()
            for name in files:
                db.session.add(LeadAttachment(
                    lead_id=row.id, filename=name, source='email',
                    storage_path=f'/nonexistent/{name}'))
            db.session.commit()
            made['leads'].append(row.id)
            return row.id

    made['lead'] = lead
    yield made

    with flask_app.app_context():
        for lead_id in made['leads']:
            LeadAttachment.query.filter_by(lead_id=lead_id).delete()
            LeadEmail.query.filter_by(lead_id=lead_id).delete()
            row = db.session.get(Lead, lead_id)
            if row is not None:
                row.email = row.email2 = row.original_email_from = None
                row.conversation_id = row.email_message_id = None
                row.original_email_subject = row.original_email_body = None
                row.email_extracted_json = None
                row.company_id = None
                row.created_at = datetime.utcnow() - timedelta(days=3650)
        EmailClassification.query.filter(
            EmailClassification.from_domain.like('%.example')).filter(
            EmailClassification.message_id.like('<dup-%')).delete(
                synchronize_session=False)
        db.session.commit()
        for cid in made['companies']:
            c = db.session.get(Company, cid)
            if c is not None:
                db.session.delete(c)
        db.session.commit()


def _breakdown(m, **kw):
    with flask_app.app_context():
        return lidb.duplicate_breakdown(
            msg=m, subject=li.match_subject(m.get('subject') or ''),
            from_addr=li.effective_sender(m), **kw)


def _signals(d):
    return {r['signal'] for r in d['reasons']}


# ─── the readers, on plain text ──────────────────────────────────────────
@pytest.mark.parametrize('text, keys', [
    ('RFQ No. NTPC/2026/1234 for a transformer', ['NTPC20261234']),
    ('Tender ref: MEC-TN-0098', ['MECTN0098']),
    ('RFQ-DLI-26-0061 Breakbulk Airoli - Not Quoted', ['DLI260061']),
    ('Enquiry Ref No: ABC/123', ['ABC123']),
    ('RFQ from Mumbai to Chennai', []),
    ('enquiry regarding cranes', []),
    ('RFQ 2026', []),
    ('RFQ 12.09.2026', []),
    ('rfq heavy transport xlsx', []),
])
def test_enquiry_references_are_numbers_not_words(text, keys):
    assert [k for _raw, k in li.enquiry_references(text)] == keys


@pytest.mark.parametrize('name, meaningful', [
    ('image001.png', False), ('Outlook-2kx0w3dg.png', False),
    ('Scan_20260901.pdf', False), ('RFQ.xlsx', False), ('smime.p7s', False),
    ('logo.jpg', False), ('invite.ics', False),
    ('Transformer_Hazira_Dahej.xlsx', True), ('BOQ Kandla.xlsx', True),
])
def test_generic_filenames_are_not_evidence(name, meaningful):
    assert bool(li.meaningful_attachment(name)) is meaningful


def test_places_and_weights_are_compared_in_one_form():
    assert li.place_key('Vadodara, Gujarat') == li.place_key('vadodara plant')
    assert li.place_key('Bengaluru') == li.place_key('Bangalore')
    assert li.place_key('Mundra Port') == 'mundra'
    assert li.cargo_weights('180 MT, 45,000 kgs and 2.5 tonnes') == \
        {180.0, 45.0, 2.5}
    assert li.distinctive_cargo_words('Heavy Reactor Vessel (ODC)') == \
        ['reactor', 'vessel']


# ─── same_thread ─────────────────────────────────────────────────────────
def test_the_same_conversation_is_a_certain_duplicate(world):
    lead_id = world['lead'](email='a@dupthread.example',
                            conversation_id='AAQk-dup-thread-1')
    d = _breakdown(msg('Transport of boilers', 'details below',
                       frm='b@elsewhere-dup.example',
                       conversation='AAQk-dup-thread-1'))
    assert d['lead_id'] == lead_id
    assert d['score'] == 100
    assert 'same_thread' in _signals(d)
    detail = [r for r in d['reasons'] if r['signal'] == 'same_thread'][0]
    assert detail['weight'] == 100 and 'conversation' in detail['detail']


def test_the_same_email_seen_twice_matches_its_lead(world):
    lead_id = world['lead'](email='a@dupmid.example',
                            email_message_id='<dup-seen-twice@x>')
    d = _breakdown(msg('Anything', frm='a@dupmid.example',
                       mid='<dup-seen-twice@x>'))
    assert d['lead_id'] == lead_id and 'same_thread' in _signals(d)


def test_a_reply_to_an_email_on_the_trail_matches_even_outside_the_window(world):
    lead_id = world['lead'](email='a@dupold.example',
                            created_at=datetime.utcnow() - timedelta(days=90))
    with flask_app.app_context():
        db.session.add(LeadEmail(lead_id=lead_id, direction='outbound',
                                 message_id='<dup-trail-reply@procam>'))
        db.session.commit()
    d = _breakdown(msg('Our reply', frm='x@dupold.example',
                       in_reply_to='<dup-trail-reply@procam>'))
    assert d['lead_id'] == lead_id and 'same_thread' in _signals(d)


# ─── same_reference ──────────────────────────────────────────────────────
def test_the_same_tender_number_from_another_sender_is_a_duplicate(world):
    lead_id = world['lead'](
        email='projects@dupconsultant.example',
        original_email_subject='Tender for transformer movement',
        original_email_body='Please quote against Tender No. NTPC/2026/7781 '
                            'for 2 transformers.')
    d = _breakdown(msg('Transformer movement enquiry',
                       'Ref tender ref NTPC-2026-7781, kindly quote.',
                       frm='buyer@dupother.example'))
    assert d['lead_id'] == lead_id
    assert d['score'] >= 70
    assert 'same_reference' in _signals(d)


def test_a_different_reference_number_is_not_the_same_reference(world):
    world['lead'](email='projects@dupref2.example',
                  original_email_body='Tender No. BHEL/2026/5501 attached.')
    d = _breakdown(msg('Transformer enquiry', 'Tender No. BHEL/2026/5502',
                       frm='buyer@dupref2-other.example'))
    assert 'same_reference' not in _signals(d)
    assert d['score'] < 31


def test_a_short_reference_only_counts_within_one_account(world):
    lead_id = world['lead'](email='ops@dupshortref.example',
                            original_email_body='RFQ No. 4471 for cranes.')
    other = _breakdown(msg('Cranes', 'RFQ No. 4471', frm='x@dupstranger.example'))
    assert 'same_reference' not in _signals(other), \
        'two customers can both be on RFQ 4471'
    same = _breakdown(msg('Cranes', 'RFQ No. 4471',
                          frm='someone@dupshortref.example'))
    assert same['lead_id'] == lead_id and 'same_reference' in _signals(same)


def test_a_reference_in_a_trail_subject_is_found(world):
    lead_id = world['lead'](email='a@duptrailref.example',
                            original_email_body='Need rates.')
    with flask_app.app_context():
        db.session.add(LeadEmail(lead_id=lead_id, direction='inbound',
                                 subject='RE: Enquiry Ref ORD-88213-B'))
        db.session.commit()
    d = _breakdown(msg('Follow up', 'about enquiry ref ORD-88213-B please',
                       frm='z@duptrailref-other.example'))
    assert d['lead_id'] == lead_id and 'same_reference' in _signals(d)


# ─── same_sender_and_subject + same_account_in_window ────────────────────
def test_the_same_sender_resending_the_same_subject_is_a_duplicate(world):
    lead_id = world['lead'](email='buyer@dupresend.example',
                            original_email_subject='RFQ - crawler crane for Paradip')
    m = msg('RFQ - crawler crane for Paradip - Reminder',
            'Resending, please quote.', frm='buyer@dupresend.example')
    d = _breakdown(m)
    assert d['lead_id'] == lead_id
    assert _signals(d) >= {'same_sender_and_subject', 'same_account_in_window'}
    assert d['score'] == _W['same_sender_and_subject'] + _W['same_account_in_window']
    assert d['reasons'][0]['weight'] >= d['reasons'][-1]['weight'], \
        'strongest reason first'


def test_a_contact_address_on_the_lead_counts_as_the_sender(world):
    lead_id = world['lead'](email='desk@dupcontact.example',
                            email2='manager@dupcontact.example',
                            original_email_subject='Shifting of DG sets to Pune')
    d = _breakdown(msg('Shifting of DG sets to Pune', 'again',
                       frm='manager@dupcontact.example'))
    assert d['lead_id'] == lead_id and 'same_sender_and_subject' in _signals(d)


def test_a_generic_subject_is_not_evidence(world):
    world['lead'](email='buyer@dupgeneric.example', original_email_subject='RFQ')
    d = _breakdown(msg('RFQ', 'another one', frm='buyer@dupgeneric.example'))
    assert 'same_sender_and_subject' not in _signals(d)
    assert d['score'] < 31


def test_two_strangers_on_free_mail_are_not_one_account(world):
    world['lead'](email='someone.dup1@gmail.com',
                  original_email_subject='Need truck for machinery')
    d = _breakdown(msg('Need a trailer', 'from Pune to Chennai',
                       frm='another.dup2@gmail.com'))
    assert 'same_account_in_window' not in _signals(d)


def test_the_resolved_account_joins_two_domains(world):
    lead_id = world['lead'](email='buyer@dupsteel-group.example',
                            company_id=world['steel'])
    d = _breakdown(msg('New coils', 'details', frm='new.buyer@dupsteel.example'))
    assert d['lead_id'] == lead_id
    reason = [r for r in d['reasons'] if r['signal'] == 'same_account_in_window']
    assert reason and 'Dup Steel Ltd' in reason[0]['detail']


def test_a_lead_outside_the_window_is_not_a_duplicate(world):
    world['lead'](email='buyer@dupwindow.example',
                  original_email_subject='Movement of a 300 MT stator',
                  created_at=datetime.utcnow() - timedelta(days=45))
    m = msg('Movement of a 300 MT stator', 'again',
            frm='buyer@dupwindow.example')
    assert _breakdown(m)['score'] == 0
    assert _breakdown(m, window_days=60)['score'] >= 70, \
        'the window is configurable'


# ─── same_account_and_route ──────────────────────────────────────────────
def test_the_same_account_and_route_with_the_same_file_is_a_duplicate(world):
    lead_id = world['lead'](
        email='logistics@duproute.example',
        original_email_subject='Transformer shifting',
        extracted={'origin': 'Vadodara, Gujarat',
                   'destination': 'Mundra Port'},
        files=['Transformer_GA_Drawing_Rev2.pdf'])
    d = _breakdown(msg('Shifting requirement',
                       'Movement from Vadodara to Mundra. Details attached.',
                       frm='another.person@duproute.example',
                       attachments=['Transformer_GA_Drawing_Rev2.pdf']))
    assert d['lead_id'] == lead_id
    assert _signals(d) >= {'same_account_and_route', 'same_attachment_name',
                           'same_account_in_window'}
    assert d['score'] == 35 + 30 + 20


def test_a_different_route_for_the_same_account_is_not_a_duplicate(world):
    world['lead'](email='logistics@dupdiffroute.example',
                  extracted={'origin': 'Vadodara', 'destination': 'Mundra'})
    d = _breakdown(msg('Shifting requirement',
                       'Movement from Vadodara to Chennai.',
                       frm='logistics@dupdiffroute.example'))
    assert 'same_account_and_route' not in _signals(d)
    assert d['score'] < 31


def test_the_route_is_read_from_the_lead_email_when_nothing_was_extracted(world):
    lead_id = world['lead'](
        email='a@dupparsed.example',
        original_email_body='Please move 4 pieces from Hazira to Dahej.')
    d = _breakdown(msg('Movement', 'Cargo from Hazira to Dahej, 4 pieces.',
                       frm='b@dupparsed.example'))
    assert d['lead_id'] == lead_id and 'same_account_and_route' in _signals(d)


def test_the_same_route_for_a_different_customer_is_not_a_signal(world):
    """Half the mailbox moves cargo from Hazira to Dahej."""
    world['lead'](email='a@dupcommonroute.example',
                  extracted={'origin': 'Hazira', 'destination': 'Dahej'})
    d = _breakdown(msg('Movement', 'Cargo from Hazira to Dahej.',
                       frm='b@dupcommonroute-other.example'))
    assert 'same_account_and_route' not in _signals(d)


# ─── same_attachment_name ────────────────────────────────────────────────
def test_generic_attachment_names_are_ignored(world):
    world['lead'](email='a@dupfiles.example',
                  files=['image001.png', 'RFQ.xlsx', 'Outlook-2kx0w3dg.png',
                         'smime.p7s', 'Scan_20260901.pdf'])
    d = _breakdown(msg('Another enquiry', 'see attached',
                       frm='b@dupfiles-other.example',
                       attachments=['image001.png', 'RFQ.xlsx',
                                    'Outlook-2kx0w3dg.png', 'smime.p7s',
                                    'Scan_20260901.pdf']))
    assert 'same_attachment_name' not in _signals(d)
    assert d['score'] == 0


def test_a_specific_attachment_name_matches_across_senders(world):
    lead_id = world['lead'](email='a@dupspecific.example',
                            files=['Kandla_Jetty_Crane_Layout.xlsx'])
    d = _breakdown(msg('Crane layout', 'attached',
                       frm='b@dupspecific-other.example',
                       attachments=['kandla_jetty_crane_layout.XLSX']))
    assert d['lead_id'] == lead_id
    assert _signals(d) == {'same_attachment_name'}
    assert d['score'] == 30


# ─── same_cargo ──────────────────────────────────────────────────────────
def test_the_same_cargo_and_weight_is_a_signal(world):
    lead_id = world['lead'](email='a@dupcargo.example',
                            extracted={'cargo_type': 'Reactor Vessel',
                                       'cargo_weight_mt': 180})
    d = _breakdown(msg('Movement', 'Three reactor vessels of 180 MT each.',
                       frm='b@dupcargo.example'))
    assert d['lead_id'] == lead_id
    assert _signals(d) == {'same_cargo', 'same_account_in_window'}


def test_the_same_cargo_at_a_different_weight_is_not(world):
    world['lead'](email='a@dupcargo2.example',
                  extracted={'cargo_type': 'Reactor Vessel',
                             'cargo_weight_mt': 180})
    d = _breakdown(msg('Movement', 'One reactor vessel of 95 MT.',
                       frm='b@dupcargo2.example'))
    assert 'same_cargo' not in _signals(d)


def test_generic_cargo_words_are_not_a_description(world):
    world['lead'](email='a@dupcargo3.example',
                  extracted={'cargo_type': 'Heavy project cargo',
                             'cargo_weight_mt': 20})
    d = _breakdown(msg('Movement', 'Heavy project cargo, 20 MT.',
                       frm='b@dupcargo3.example'))
    assert 'same_cargo' not in _signals(d)


# ─── shape, bounds and failure ───────────────────────────────────────────
def test_the_old_signature_still_answers_score_and_lead(world):
    lead_id = world['lead'](email='buyer@dupcompat.example',
                            original_email_subject='Barge for ODC at Haldia')
    m = msg('Barge for ODC at Haldia', 'again', frm='buyer@dupcompat.example')
    with flask_app.app_context():
        pair = lidb.duplicate_score(msg=m, subject='Barge for ODC at Haldia',
                                    from_addr='buyer@dupcompat.example')
    assert pair == (75, lead_id)


def test_the_score_is_capped_at_one_hundred(world):
    world['lead'](email='buyer@dupcap.example', conversation_id='AAQk-dup-cap',
                  original_email_subject='Lifting of 400 MT column',
                  original_email_body='RFQ No. CAP/2026/0001')
    d = _breakdown(msg('Lifting of 400 MT column', 'RFQ No. CAP/2026/0001',
                       frm='buyer@dupcap.example', conversation='AAQk-dup-cap'))
    assert d['score'] == 100
    assert sum(r['weight'] for r in d['reasons']) > 100


def test_only_the_most_recent_candidates_are_read(world):
    older = world['lead'](email='buyer@duplimit.example',
                          original_email_subject='Hydra crane for Bhilai plant',
                          created_at=datetime.utcnow() - timedelta(hours=30))
    world['lead'](email='other@duplimit-unrelated.example',
                  created_at=datetime.utcnow() - timedelta(minutes=1))
    m = msg('Hydra crane for Bhilai plant', 'again',
            frm='buyer@duplimit.example')
    assert _breakdown(m)['lead_id'] == older
    assert _breakdown(m, limit=1)['score'] == 0, \
        'the candidate set is bounded by the limit'


def test_a_scoring_failure_answers_no_duplicate(world, monkeypatch):
    def broken(*a, **kw):
        raise RuntimeError('boom')
    monkeypatch.setattr(lidb, '_Probe', broken)
    d = _breakdown(msg('Anything', frm='a@dupbroken.example'))
    assert d == {'score': 0, 'lead_id': None, 'reasons': []}


# ─── wired through the classifier into the review payload ────────────────
_ENQUIRY = ('We need transport of 2 transformers, 120 MT each, from Hazira '
            'to Dahej. Tender No. GETCO/2026/3319. Please quote. '
            'Contact +91 90000 40004.')


def test_the_classifier_files_a_classic_duplicate_and_records_why(world):
    lead_id = world['lead'](email='tenders@dupgetco.example',
                            original_email_subject='Transformer movement',
                            original_email_body=_ENQUIRY)
    m = msg('Transformer transport enquiry', _ENQUIRY,
            frm='purchase@dupepc.example', mid='<dup-classic@x>')
    with flask_app.app_context():
        d = li.classify(m, lidb.build_context())
        assert d.klass == li.Klass.DUPLICATE, d
        assert d.lead_id == lead_id and d.duplicate_score >= 70
        row = lidb.record(d, m)
        db.session.commit()
        stored = db.session.get(EmailClassification, row.id).payload
    assert stored['duplicate']['lead_id'] == lead_id
    assert stored['duplicate']['score'] == d.duplicate_score
    signals = {r['signal'] for r in stored['duplicate']['reasons']}
    assert 'same_reference' in signals
    for r in stored['duplicate']['reasons']:
        assert set(r) == {'signal', 'weight', 'detail'} and r['detail']


def test_a_near_miss_is_not_held_as_a_duplicate(world):
    """Same customer, different route, different tender, generic files:
    only the account matches, which is not enough to hold anything."""
    world['lead'](email='tenders@dupnear.example',
                  original_email_subject='Transformer movement',
                  original_email_body='Tender No. GETCO/2026/1000 from '
                                      'Vadodara to Mundra.',
                  files=['image001.png'])
    body = ('We need transport of 2 transformers, 120 MT each, from Hazira '
            'to Dahej. Tender No. GETCO/2026/2000. Please quote. '
            'Contact +91 90000 40004.')
    m = msg('Transformer transport enquiry', body,
            frm='another@dupnear.example', attachments=['image001.png'],
            mid='<dup-near@x>')
    with flask_app.app_context():
        d = li.classify(m, lidb.build_context())
    assert d.klass not in (li.Klass.DUPLICATE,)
    assert d.duplicate_score < 31
    assert d.extra['duplicate']['reasons'][0]['signal'] == \
        'same_account_in_window'


def test_a_context_with_only_the_old_lookup_still_classifies():
    """Callers that built a Context before breakdowns existed pass only
    duplicate_score. Their answer must be honoured unchanged."""
    ctx = li.Context(duplicate_score=lambda **kw: (80, 4242))
    d = li.classify(msg('Crane hire at Vizag port', 'Need a 200 MT crane. '
                        'Please quote, +91 90000 50005.'), ctx)
    assert d.klass == li.Klass.DUPLICATE and d.lead_id == 4242
    assert d.extra['duplicate'] == {'score': 80, 'lead_id': 4242,
                                    'reasons': []}


def test_nothing_is_added_to_the_decision_when_nothing_fired():
    d = li.classify(msg('Crane hire at Vizag port', 'Need a 200 MT crane. '
                        'Please quote, +91 90000 50005.'), li.Context())
    assert 'duplicate' not in d.extra
