"""Every open CRM item, in one release.

CRM-FINAL-13NOV. Section A is verification of what shipped on 11 and
12 November, with one thing that had not: the placeholder save. B and
C are new.

THE ONE IDEA RUNNING THROUGH ALL OF IT

A mail that was never an enquiry must not become a lead, and a rule
about a lead must never cost somebody the typing they just did. Both
faults in A1 and in items 10 and 13 are the same shape: a rule that
was right about one thing being applied to something else.
"""
import importlib
import os
import sys
import tempfile
from datetime import datetime

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'final13-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'Final13-123456789')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'final13.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
importlib.import_module('app.models.assignment')
flask_app = _main.app
db = _main.db
Lead, Company, Employee = _main.Lead, _main.Company, _main.Employee

from app.models.assignment import AccountVerticalOwner       # noqa: E402
from app.models.email_admin import VerticalHead              # noqa: E402
from app.services import (account_gate, bulk_mail, flags,     # noqa: E402
                          internal_mail, lead_account,
                          lead_people, lead_vertical,
                          vertical_heads)

VERT = 'Heavy Cargo'
OWNER = 'ZF_OWNER'
HEAD = 'ZF_HEAD'
DESK = 'ZF_DESK'


def _purge():
    from app.models.audit import AuditEvent

    ids = [l.id for l in Lead.query.filter(Lead.company.like('ZF%')).all()]
    ids += [l.id for l in Lead.query.filter(
        Lead.company.in_(('To review', 'Unknown', 'New Lead'))).all()]
    if ids:
        AuditEvent.query.filter(
            AuditEvent.entity_type == 'lead',
            AuditEvent.entity_id.in_([str(i) for i in ids])).delete(
                synchronize_session=False)
    Lead.query.filter(db.or_(
        Lead.company.like('ZF%'),
        Lead.company.in_(('To review', 'Unknown', 'New Lead')))).delete(
            synchronize_session=False)
    account_ids = [c.id for c in
                   Company.query.filter(Company.name.like('ZF%')).all()]
    if account_ids:
        AccountVerticalOwner.query.filter(
            AccountVerticalOwner.account_id.in_(account_ids)).delete(
                synchronize_session=False)
    Company.query.filter(db.or_(
        Company.name.like('ZF%'),
        # item 12's tests need accounts that are literally short, so
        # they cannot carry the ZF prefix. Left behind, 'Bhel' and
        # 'BHEL' both normalise to 'bhel' and `_by_name` then
        # correctly refuses to choose between two accounts -- which
        # made the exact-match test fail for the right reason in the
        # wrong place.
        Company.name.in_(('Bhel', 'BHEL', 'ZF Bhel')))).delete(
            synchronize_session=False)
    Employee.query.filter(Employee.emp_code.like('ZF_%')).delete(
        synchronize_session=False)
    VerticalHead.query.filter_by(vertical=VERT).delete(
        synchronize_session=False)
    from app.models.integration import AppSetting
    AppSetting.query.filter(AppSetting.key.in_((
        'flag.FEATURE_ACCOUNT_FIRST', 'flag.FEATURE_LEAD_ACCOUNT',
        'rfq_coordinator', 'internal_notice_senders'))).delete(
            synchronize_session=False)
    db.session.commit()
    flags.cache_clear()
    vertical_heads.cache_clear()


@pytest.fixture(autouse=True)
def _clean():
    with flask_app.app_context():
        db.create_all()
        _purge()
        for code, vertical in ((OWNER, VERT), (HEAD, VERT), (DESK, VERT)):
            db.session.add(Employee(
                emp_code=code, name=code.title(), password_hash='x',
                email=f'{code.lower()}@procamlogistics.com', role='user',
                vertical=vertical, is_active=True, must_change_pw=False))
        db.session.add(VerticalHead(vertical=VERT, head_emp_code=HEAD,
                                    active=True))
        db.session.commit()
        vertical_heads.cache_clear()
        _flag('FEATURE_LEAD_ACCOUNT', True)
        yield
        _purge()


def _flag(name, state=True):
    import json

    from app.models.integration import AppSetting
    key = f'flag.{name}'
    row = AppSetting.query.filter_by(key=key).first()
    if row is None:
        row = AppSetting(key=key)
        db.session.add(row)
    row.value_json = json.dumps(bool(state))
    db.session.commit()
    flags.cache_clear()


def _setting(key, value):
    from app.services import admin_settings
    admin_settings.save(key, value, actor='test') if hasattr(
        admin_settings, 'save') else None


def _lead(**kw):
    kw.setdefault('company', 'To review')
    kw.setdefault('stage', 'New Opportunity')
    kw.setdefault('procam_vertical', VERT)
    kw.setdefault('assigned_to', OWNER)
    kw.setdefault('created_at', datetime.utcnow())
    lead = Lead(**kw)
    db.session.add(lead)
    db.session.commit()
    _orphans(lead.id)
    return lead


def _orphans(lead_id):
    """Rows another module left behind on this id.

    The suite shares one SQLite database and SQLite reuses row ids
    after a delete, so a fresh lead inherits the quotes, RFQs, mails
    and activities of a deleted lead with the same id. The sweep asks
    `junk_delete.blockers` whether anybody has worked a lead before it
    will archive it -- correctly -- so an inherited RFQ row put every
    lead this module creates into "somebody has worked this" and the
    survey came back empty. Cleared for this id only.
    """
    from app.models.audit import AuditEvent

    models = []
    for module_name, names in (
            ('app.models.rfq', ('RFQ',)),
            ('app.models.quote', ('Quote',)),
            ('app.models.cost_sheet', ('CostSheet',)),
    ):
        try:
            module = importlib.import_module(module_name)
        except Exception:                                 # noqa: BLE001
            continue
        for name in names:
            model = getattr(module, name, None)
            if model is not None and hasattr(model, 'lead_id'):
                models.append(model)
    for name in ('LeadEmail', 'LeadActivity', 'LeadNote', 'LeadAttachment',
                 'Opportunity'):
        model = getattr(_main, name, None)
        if model is not None and hasattr(model, 'lead_id'):
            models.append(model)
    for model in models:
        try:
            model.query.filter(model.lead_id == lead_id).delete(
                synchronize_session=False)
        except Exception:                                 # noqa: BLE001
            db.session.rollback()
    AuditEvent.query.filter(
        AuditEvent.entity_type == 'lead',
        AuditEvent.entity_id == str(lead_id)).delete(
            synchronize_session=False)
    db.session.commit()


def _client(code=OWNER, role='user'):
    client = flask_app.test_client()
    with client.session_transaction() as s:
        s['emp_code'] = code
        s['role'] = role
    return client


def _token(client):
    r = client.get('/my-work')
    for header in r.headers.getlist('Set-Cookie'):
        if header.startswith('csrf_token='):
            return header.split('=', 1)[1].split(';')[0]
    return ''


# ═══════════════════════════════════════════════════════════════════
# A1 — saving is never gated, placeholder leads included
# ═══════════════════════════════════════════════════════════════════
def test_a_placeholder_lead_saves(app=None):
    """The live report of 13 November: "Link or create the account
    before saving." on a "To review" lead, with nothing saved."""
    with flask_app.app_context():
        lead_id = _lead(company='To review').id
    client = _client()
    r = client.put(f'/api/leads/{lead_id}',
                   json={'pic': 'Nicole', 'phone': '+91 98200 00000',
                         'rfq_date': '2026-11-12'},
                   headers={'X-CSRFToken': _token(client)})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body['ok'] is True
    with flask_app.app_context():
        lead = db.session.get(Lead, lead_id)
        assert lead.pic == 'Nicole'
        assert lead.phone == '+91 98200 00000'
        assert str(lead.rfq_date) == '2026-11-12'


@pytest.mark.parametrize('name', ['To review', 'Unknown', 'New Lead'])
def test_every_placeholder_name_saves(name):
    with flask_app.app_context():
        lead_id = _lead(company=name).id
    client = _client()
    r = client.put(f'/api/leads/{lead_id}', json={'pic': 'Someone'},
                   headers={'X-CSRFToken': _token(client)})
    assert r.status_code == 200, r.get_data(as_text=True)
    with flask_app.app_context():
        assert db.session.get(Lead, lead_id).pic == 'Someone'


def test_the_customer_is_asked_for_rather_than_demanded():
    """Saved, and the one gap is carried back so the screen can open
    the picker instead of refusing."""
    with flask_app.app_context():
        lead_id = _lead(company='To review').id
    client = _client()
    body = client.put(f'/api/leads/{lead_id}', json={'pic': 'Nicole'},
                      headers={'X-CSRFToken': _token(client)}).get_json()
    assert body['needs'] == 'account'
    prompt = body['account_unnamed']
    assert prompt['saved'] is True
    assert prompt['company'] == 'To review'
    assert 'Account check' in prompt['resolver']


def test_save_refusal_is_now_always_empty():
    """Kept as a name the route calls, and as the place the decision
    is written down. A test, so deleting the reasoning is deliberate."""
    with flask_app.app_context():
        lead = _lead(company='To review')
        assert lead_account.save_refusal(lead) == ''
        assert lead_account.save_refusal(lead, {'company': 'To review'}) == ''


def test_a_real_name_is_not_asked_about():
    with flask_app.app_context():
        lead = _lead(company='ZF Real Customer Pvt Ltd')
        assert lead_account.unnamed_prompt(lead) is None


def test_the_owner_and_vertical_save_on_a_placeholder_lead():
    """A1 names them: owner, PIC and vertical, on no-account AND
    placeholder leads."""
    with flask_app.app_context():
        lead_id = _lead(company='Unknown', assigned_to=None).id
    client = _client(HEAD)
    r = client.put(f'/api/leads/{lead_id}',
                   json={'assigned_to': OWNER, 'pic': 'Nicole',
                         'procam_vertical': VERT,
                         'procam_vertical_was': VERT},
                   headers={'X-CSRFToken': _token(client)})
    assert r.status_code == 200, r.get_data(as_text=True)
    with flask_app.app_context():
        lead = db.session.get(Lead, lead_id)
        assert lead.assigned_to == OWNER
        assert lead.pic == 'Nicole'


# ═══════════════════════════════════════════════════════════════════
# item 10 — our own mail is not a lead
# ═══════════════════════════════════════════════════════════════════
DIGEST = ('Procam Logistics owner: somebody\n'
          'Current bid status: under evaluation\n'
          'Mr Somebody, please create / confirm the CRM lead for this '
          'tender.\n\nProcam Tech team')


def _msg(subject, body, sender):
    return {'subject': subject,
            'from': {'emailAddress': {'address': sender}},
            'body': {'content': body},
            'internetMessageHeaders': []}


def test_the_tender_digest_is_recognised_as_ours():
    with flask_app.app_context():
        assert internal_mail.from_us('tms@procamgroup.in')
        assert internal_mail.from_us('somebody@procamlogistics.com')
        assert not internal_mail.from_us('buyer@customer.example')
        assert internal_mail.reads_as_a_notice('Tender digest', DIGEST)


def test_the_digest_does_not_become_a_lead():
    with flask_app.app_context():
        msg = _msg('Daily tender digest', DIGEST, 'tms@procamgroup.in')
        what, lead_id, why = internal_mail.decide(msg, DIGEST)
        assert what == internal_mail.LOG
        assert lead_id is None
        assert internal_mail.REASON in why


def test_the_digest_joins_the_tender_lead_when_it_names_one():
    with flask_app.app_context():
        _lead(company='ZF Hydro Project Owner Ltd',
              original_email_subject='RFQ for the Hydro tender')
        body = DIGEST + '\nCustomer: ZF Hydro Project Owner Ltd'
        msg = _msg('Daily tender digest', body, 'tms@procamgroup.in')
        what, lead_id, why = internal_mail.decide(msg, body)
        assert what == internal_mail.ATTACH
        assert lead_id
        assert 'customer named in it' in why


def test_a_colleagues_forward_of_a_real_enquiry_is_untouched():
    """The rescue exists for this and must keep running: from us,
    worded like a notice, and a customer's own address in the body."""
    with flask_app.app_context():
        body = ('Please create the CRM lead for this.\n\n'
                'From: buyer@realcustomer.example\n'
                'We need 2x40HQ from Mundra to Nanjing.')
        msg = _msg('FW: RFQ', body, 'somebody@procamlogistics.com')
        what, _lead_id, _why = internal_mail.decide(msg, body)
        assert what == internal_mail.NOTHING


def test_a_customers_own_mail_is_never_a_notice():
    with flask_app.app_context():
        msg = _msg('Please create the CRM lead', DIGEST,
                   'buyer@customer.example')
        what, _lead_id, _why = internal_mail.decide(msg, DIGEST)
        assert what == internal_mail.NOTHING


def test_an_administrator_can_name_another_notice_sender():
    with flask_app.app_context():
        from app.services import admin_settings
        admin_settings.save('internal_notice_senders',
                            'robot@reports.example', actor='test')
        assert internal_mail.from_us('robot@reports.example')


# ═══════════════════════════════════════════════════════════════════
# item 11 — where the vertical came from
# ═══════════════════════════════════════════════════════════════════
def test_the_vertical_source_is_recorded_as_a_word():
    assert lead_vertical.source_for('from the Account Master') == \
        lead_vertical.ACCOUNT
    assert lead_vertical.source_for('matched "warehouse", "storage"') == \
        lead_vertical.GUESSED
    assert lead_vertical.source_for('nothing in the text points anywhere') \
        == lead_vertical.NONE
    assert lead_vertical.source_for('whatever', from_attachment=True) == \
        lead_vertical.ATTACHMENT


def test_a_guess_is_not_firm_until_it_is_confident():
    assert lead_vertical.is_firm(95, lead_vertical.GUESSED) is True
    assert lead_vertical.is_firm(40, lead_vertical.GUESSED) is False
    # Somebody said so: firm at any confidence.
    assert lead_vertical.is_firm(0, lead_vertical.ACCOUNT) is True
    assert lead_vertical.is_firm(None, lead_vertical.STATED) is True


def test_a_review_lead_with_a_guessed_vertical_goes_to_the_desk():
    from email_ingest.single_message import _desk_for_a_guessed_vertical

    with flask_app.app_context():
        from app.services import admin_settings
        admin_settings.save('rfq_coordinator', DESK, actor='test')
        lead = _lead(needs_review=True, vertical_confidence=40,
                     vertical_source=lead_vertical.GUESSED)
        assert _desk_for_a_guessed_vertical(lead) == DESK


def test_a_confident_vertical_routes_normally():
    from email_ingest.single_message import _desk_for_a_guessed_vertical

    with flask_app.app_context():
        from app.services import admin_settings
        admin_settings.save('rfq_coordinator', DESK, actor='test')
        lead = _lead(needs_review=True, vertical_confidence=95,
                     vertical_source=lead_vertical.GUESSED)
        assert _desk_for_a_guessed_vertical(lead) == ''


def test_a_lead_nobody_flagged_routes_normally():
    """Most leads get their vertical from a guess, and routing those
    normally is right. Both halves have to hold."""
    from email_ingest.single_message import _desk_for_a_guessed_vertical

    with flask_app.app_context():
        from app.services import admin_settings
        admin_settings.save('rfq_coordinator', DESK, actor='test')
        lead = _lead(needs_review=False, vertical_confidence=40,
                     vertical_source=lead_vertical.GUESSED)
        assert _desk_for_a_guessed_vertical(lead) == ''


def test_with_no_desk_named_the_lead_still_gets_an_owner():
    """A lead with nobody at all would be worse than one with an owner
    to correct."""
    from email_ingest.single_message import _desk_for_a_guessed_vertical

    with flask_app.app_context():
        lead = _lead(needs_review=True, vertical_confidence=10,
                     vertical_source=lead_vertical.GUESSED)
        assert _desk_for_a_guessed_vertical(lead) == ''


def test_the_lead_carries_the_source_to_the_screen():
    with flask_app.app_context():
        lead = _lead(vertical_source=lead_vertical.GUESSED,
                     vertical_confidence=55)
        out = lead.to_dict()
        assert out['vertical_source'] == lead_vertical.GUESSED


# ═══════════════════════════════════════════════════════════════════
# item 12 — BHEL is not BEML
# ═══════════════════════════════════════════════════════════════════
def test_an_acronym_is_not_resembled_but_is_still_contained():
    """Item 12, and the rule it actually needs.

    A length floor alone was wrong and the first attempt at this
    proved it by breaking the BEML case this module exists for. What
    separates the two is containment:

        'beml' inside 'la beml'  -> yes, it is the name, whole
        'beml' against 'bhel'    -> no, two different words that
                                    happen to look alike
    """
    from app.services import account_match

    with flask_app.app_context():
        db.session.add(Company(name='ZF Bhel', is_active=True,
                               email_domains=[]))
        db.session.add(Company(name='ZF Beml Limited', is_active=True,
                               email_domains=[]))
        db.session.commit()
        offered = [c.name for c, _r in
                   account_match._similar_names('BEML Limited')]
        assert 'ZF Bhel' not in offered
        assert 'ZF Beml Limited' in offered


def test_a_name_still_matches_itself():
    """The near-duplicate warning is built on this, and refusing all
    short names took it away for "LA BEML Limited" against
    "LA BEML LIMITED" -- seven normalised characters."""
    from app.services import account_match

    with flask_app.app_context():
        db.session.add(Company(name='ZF Bhel', is_active=True,
                               email_domains=[]))
        db.session.commit()
        offered = [c.name for c, _r in
                   account_match._similar_names('ZF BHEL')]
        assert 'ZF Bhel' in offered


def test_containment_is_on_word_boundaries():
    """'beml' is in 'la beml limited' and is NOT in 'bemlington'."""
    from app.services import account_match as am

    assert am._contains_as_a_word('beml', 'la beml limited')
    assert am._contains_as_a_word('la beml', 'la beml')
    assert not am._contains_as_a_word('beml', 'bemlington')
    assert not am._contains_as_a_word('beml', 'bhel')


def test_a_short_account_name_is_not_offered_for_a_long_one():
    """The reported sentence was "Bhel — (read as 'BEML Limited')":
    the short side was the ACCOUNT's."""
    from app.services import account_match

    with flask_app.app_context():
        db.session.add(Company(name='Bhel', is_active=True,
                               email_domains=[]))
        db.session.commit()
        rows = account_match.candidates(
            'someone@unrelated.example',
            company_name='BEML Limited')
        assert 'Bhel' not in [r['name'] for r in rows]


def test_a_long_name_is_still_resembled():
    """The rule is about short names, and taking resemblance away
    altogether would lose the matches it is there for."""
    from app.services import account_match

    with flask_app.app_context():
        db.session.add(Company(name='ZF Andritz Hydro Private Limited',
                               is_active=True, email_domains=[]))
        db.session.commit()
        rows = account_match._similar_names('ZF Andritz Hydro Pvt Ltd')
        assert rows, 'a long name must still resemble'


def test_an_exact_short_name_still_matches():
    """Exact or alias, which is what item 12 asks to keep."""
    from app.services import account_match

    with flask_app.app_context():
        db.session.add(Company(name='BHEL', is_active=True,
                               email_domains=[]))
        db.session.commit()
        found = account_match._by_name('BHEL')
        assert found is not None and found.name == 'BHEL'


# ═══════════════════════════════════════════════════════════════════
# item 13 — circulars are not enquiries
# ═══════════════════════════════════════════════════════════════════
def _bulk_msg(subject, body, sender='news@liner.example', headers=None):
    msg = _msg(subject, body, sender)
    msg['internetMessageHeaders'] = [
        {'name': k, 'value': v} for k, v in (headers or {}).items()]
    return msg


def test_a_newsletter_is_not_a_lead():
    with flask_app.app_context():
        msg = _bulk_msg('Weekly vessel positions',
                        'Our schedule for next week. Unsubscribe below.',
                        headers={'List-Unsubscribe': '<mailto:x@y.z>'})
        promo, why = bulk_mail.classify(msg, 'Our schedule for next week.')
        assert promo is True
        assert bulk_mail.REASON in why


@pytest.mark.parametrize('headers', [
    {'List-Unsubscribe': '<mailto:x@y.z>'},
    {'Precedence': 'bulk'},
    {'List-Id': '<news.liner.example>'},
    {'Auto-Submitted': 'auto-generated'},
    {'X-Campaign-Id': '12345'},
])
def test_every_bulk_header_is_read(headers):
    with flask_app.app_context():
        msg = _bulk_msg('Market update', 'Rates are firming.',
                        headers=headers)
        promo, _why = bulk_mail.classify(msg, 'Rates are firming.')
        assert promo is True


def test_a_quotation_from_a_mailing_system_is_still_business():
    """The half that matters more. A liner's circular and its
    quotation come from the same address through the same marketing
    system, and only one of them is business."""
    with flask_app.app_context():
        body = ('Please find our best rate for your RFQ: 2x40HQ '
                'Mundra to Nanjing, USD 1,250 per container.')
        msg = _bulk_msg('Re: RFQ 2x40HQ Mundra', body,
                        headers={'List-Unsubscribe': '<mailto:x@y.z>'})
        promo, why = bulk_mail.classify(msg, body)
        assert promo is False
        assert 'kept' in why


def test_a_tender_attachment_keeps_a_bulk_looking_mail():
    with flask_app.app_context():
        msg = _bulk_msg('Notice', 'See attached.',
                        headers={'Precedence': 'bulk'})
        msg['attachments'] = [{'name': 'NIT-tender-document.pdf'}]
        promo, _why = bulk_mail.classify(msg, 'See attached.')
        assert promo is False


def test_a_plain_enquiry_with_no_headers_is_untouched():
    with flask_app.app_context():
        body = 'We need a quote for 2x40HQ Mundra to Nanjing.'
        msg = _bulk_msg('RFQ', body, sender='buyer@customer.example')
        promo, why = bulk_mail.classify(msg, body)
        assert promo is False
        assert why == ''


# ═══════════════════════════════════════════════════════════════════
# item 14 — a head may change the PICs on their vertical's leads
# ═══════════════════════════════════════════════════════════════════
def test_a_head_may_change_the_pics_on_an_owned_lead():
    """The case that is easy to get wrong: a rule written as "the
    owner, or an admin" quietly excludes the head."""
    with flask_app.app_context():
        lead = _lead(assigned_to=OWNER)
        allowed, why = lead_people.may_set_people(lead, HEAD)
        assert allowed, why
        assert why == lead_people.AS_HEAD


def test_a_head_may_change_the_pics_on_an_unowned_lead():
    with flask_app.app_context():
        lead = _lead(assigned_to=None)
        allowed, _why = lead_people.may_set_people(lead, HEAD)
        assert allowed


def test_a_head_of_another_vertical_may_not():
    with flask_app.app_context():
        db.session.add(Employee(
            emp_code='ZF_OTHERHEAD', name='Other', password_hash='x',
            role='user', vertical='Warehousing', is_active=True))
        db.session.add(VerticalHead(vertical='Warehousing',
                                    head_emp_code='ZF_OTHERHEAD',
                                    active=True))
        db.session.commit()
        vertical_heads.cache_clear()
        lead = _lead(assigned_to=OWNER)
        allowed, _why = lead_people.may_set_people(lead, 'ZF_OTHERHEAD')
        assert not allowed
        VerticalHead.query.filter_by(vertical='Warehousing').delete(
            synchronize_session=False)
        db.session.commit()
        vertical_heads.cache_clear()


def test_the_check_script_asks_the_real_rule():
    import importlib.util

    path = os.path.join(_ROOT, 'scripts',
                        '2026_11_13_pic_rights_check.py')
    spec = importlib.util.spec_from_file_location('zf_rights', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    source = open(path).read()
    assert 'may_set_people' in source
    assert "add_argument('--apply'" not in source
    with flask_app.app_context():
        _lead(assigned_to=OWNER)
        assert (HEAD, VERT) in mod.heads()
        leads, total = mod.sample_leads(VERT, limit=3)
        assert total >= 1 and leads
        mod.report(limit=3)               # must not raise


# ═══════════════════════════════════════════════════════════════════
# item 15 — accounts with open leads and no owner
# ═══════════════════════════════════════════════════════════════════
def _owners_script():
    import importlib.util

    path = os.path.join(_ROOT, 'scripts', '2026_11_13_account_owners.py')
    spec = importlib.util.spec_from_file_location('zf_owners', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_an_account_with_no_owner_is_proposed_the_busiest_person():
    mod = _owners_script()
    with flask_app.app_context():
        account = Company(name='ZF Fox Brasil', is_active=True)
        db.session.add(account)
        db.session.flush()
        for _ in range(3):
            _lead(company='ZF Fox Brasil', company_id=account.id,
                  assigned_to=OWNER)
        _lead(company='ZF Fox Brasil', company_id=account.id,
              assigned_to=HEAD)
        found = mod.survey()
        mine = [row for row in found if row[0].id == account.id]
        assert mine, 'the account was not reported'
        _account, vertical, leads, proposal, why = mine[0]
        assert vertical == VERT
        assert len(leads) == 4
        assert proposal == OWNER
        assert '3 of 4' in why


def test_an_account_that_already_has_an_owner_is_not_reported():
    mod = _owners_script()
    with flask_app.app_context():
        account = Company(name='ZF Already Owned', is_active=True)
        db.session.add(account)
        db.session.flush()
        db.session.add(AccountVerticalOwner(
            account_id=account.id, vertical=VERT, emp_code=OWNER,
            source='manual'))
        _lead(company='ZF Already Owned', company_id=account.id)
        db.session.commit()
        assert not [row for row in mod.survey() if row[0].id == account.id]


def test_a_deactivated_person_is_not_proposed():
    mod = _owners_script()
    with flask_app.app_context():
        gone = Employee(emp_code='ZF_GONE', name='Gone',
                        password_hash='x', role='user', vertical=VERT,
                        is_active=False)
        db.session.add(gone)
        account = Company(name='ZF Stale Owner', is_active=True)
        db.session.add(account)
        db.session.flush()
        _lead(company='ZF Stale Owner', company_id=account.id,
              assigned_to='ZF_GONE')
        found = [row for row in mod.survey() if row[0].id == account.id]
        assert found
        _a, _v, _l, proposal, why = found[0]
        assert proposal == ''
        assert 'deactivated' in why


def test_only_ticked_owner_rows_are_written(tmp_path):
    mod = _owners_script()
    with flask_app.app_context():
        account = Company(name='ZF Tickable', is_active=True)
        other = Company(name='ZF Untouched', is_active=True)
        db.session.add_all([account, other])
        db.session.flush()
        _lead(company='ZF Tickable', company_id=account.id,
              assigned_to=OWNER)
        _lead(company='ZF Untouched', company_id=other.id,
              assigned_to=OWNER)
        rows = [
            {**mod.row_for(account, VERT, [1], OWNER, 'owns 1 of 1'),
             'apply': 'y'},
            mod.row_for(other, VERT, [1], OWNER, 'owns 1 of 1'),
        ]
        path = str(tmp_path / 'ticked.csv')
        mod._write_one(path, rows)

        wanted, bad = mod.read_ticks(path)
        assert [w[0] for w in wanted] == [account.id]
        assert not bad
        doable, refused = mod.plan(wanted)
        assert len(doable) == 1 and not refused
        batch = mod.next_batch_id()
        assert mod.apply(doable, batch=batch) == 1

        db.session.expire_all()
        assert account_gate.owner_for(account, VERT) == OWNER
        assert account_gate.owner_for(other, VERT) == ''

        assert mod.undo(batch) == 1
        db.session.expire_all()
        assert account_gate.owner_for(account, VERT) == ''


def test_a_row_named_since_the_csv_is_refused():
    mod = _owners_script()
    with flask_app.app_context():
        account = Company(name='ZF Raced', is_active=True)
        db.session.add(account)
        db.session.flush()
        db.session.add(AccountVerticalOwner(
            account_id=account.id, vertical=VERT, emp_code=HEAD,
            source='manual'))
        db.session.commit()
        doable, refused = mod.plan([(account.id, VERT, OWNER)])
        assert not doable
        assert refused and 'already owned' in refused[0][1]


# ═══════════════════════════════════════════════════════════════════
# items 10 + 13 — the archive script
# ═══════════════════════════════════════════════════════════════════
def _sweep_script():
    import importlib.util

    path = os.path.join(_ROOT, 'scripts',
                        '2026_11_13_internal_and_promo_leads.py')
    spec = importlib.util.spec_from_file_location('zf_sweep', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_sweep_finds_a_digest_lead_and_leaves_an_enquiry():
    mod = _sweep_script()
    with flask_app.app_context():
        digest_id = _lead(
            company='To review',
            original_email_from='tms@procamgroup.in',
            original_email_subject='Daily tender digest',
            original_email_body=DIGEST).id
        enquiry_id = _lead(
            company='ZF Real Enquiry Ltd',
            original_email_from='buyer@customer.example',
            original_email_subject='RFQ 2x40HQ Mundra to Nanjing',
            original_email_body='Please quote for two containers.').id
        found = mod.survey()
        # Asserted by MEMBERSHIP, not by count. `survey` reads the
        # whole book -- which is right, it is a sweep -- and the suite
        # shares one database, so another module's "To review" lead
        # from an internal sender is correctly in this list and has
        # nothing to do with what is being tested here.
        internal = {l.id for l, _k, _w in found['internal']}
        promotional = {l.id for l, _k, _w in found['promotional']}
        subjects = {l.original_email_subject
                    for l, _k, _w in found['internal']}
        assert 'Daily tender digest' in subjects
        assert digest_id in internal
        assert enquiry_id not in internal
        assert enquiry_id not in promotional


def test_the_sweep_archives_reversibly_and_never_as_lost():
    mod = _sweep_script()
    with flask_app.app_context():
        lead_id = _lead(company='To review',
                        original_email_from='tms@procamgroup.in',
                        original_email_subject='Tender digest',
                        original_email_body=DIGEST).id
        found = mod.survey()
        # Narrowed to this module's own lead before applying. The
        # sweep reads the whole book by design and the suite shares a
        # database, so applying everything it finds would archive
        # another module's fixtures and fail somewhere unrelated.
        mine = {'internal': [row for row in found['internal']
                             if row[0].id == lead_id],
                'promotional': [], 'left': []}
        assert mine['internal'], 'the digest lead was not found'
        batch = mod.next_batch_id()
        # As an administrator: the archive asks the same permission the
        # button asks, which is the point of passing an actor at all.
        db.session.add(Employee(
            emp_code='ZF_ADMIN', name='Adm', password_hash='x',
            role='admin', vertical=VERT, is_active=True))
        db.session.commit()
        mod.apply(mine, batch=batch, actor='ZF_ADMIN')
        db.session.expire_all()
        lead = db.session.get(Lead, lead_id)
        assert lead.is_archived is True
        assert lead.stage != 'Lost'
        # and it comes back
        assert mod.undo(batch, actor='ZF_ADMIN') == 1
        db.session.expire_all()
        assert db.session.get(Lead, lead_id).is_archived is not True


def test_the_sweep_leaves_a_lead_somebody_has_worked():
    mod = _sweep_script()
    with flask_app.app_context():
        lead = _lead(company='To review',
                     stage='Quoted',
                     original_email_from='tms@procamgroup.in',
                     original_email_subject='Tender digest',
                     original_email_body=DIGEST)
        found = mod.survey()
        archived = [l.id for l, _k, _w in found['internal']]
        left = [l.id for l, _k, _w in found['left']]
        assert lead.id not in archived or lead.id in left


def test_the_sweep_has_no_hard_delete():
    path = os.path.join(_ROOT, 'scripts',
                        '2026_11_13_internal_and_promo_leads.py')
    source = open(path).read()
    for forbidden in ('session.delete(', 'query.delete(', 'DROP ',
                      'DELETE FROM'):
        assert forbidden not in source, forbidden


def test_the_sweep_will_not_archive_without_an_actor():
    """A batch that could archive with nobody's rights would be a way
    round the one control on this path."""
    mod = _sweep_script()
    with flask_app.app_context():
        _lead(company='To review',
              original_email_from='tms@procamgroup.in',
              original_email_subject='Tender digest',
              original_email_body=DIGEST)
    assert mod.main(['--apply']) == 2


def test_the_sweep_reports_what_the_permission_refused():
    """Named and left, never forced."""
    mod = _sweep_script()
    with flask_app.app_context():
        lead_id = _lead(company='To review', assigned_to=None,
                        original_email_from='tms@procamgroup.in',
                        original_email_subject='Tender digest',
                        original_email_body=DIGEST).id
        db.session.add(Employee(
            emp_code='ZF_NOBODY', name='Nobody', password_hash='x',
            role='user', vertical='Warehousing', is_active=True))
        db.session.commit()
        found = mod.survey()
        mod.apply(found, batch=mod.next_batch_id(), actor='ZF_NOBODY')
        db.session.expire_all()
        assert db.session.get(Lead, lead_id).is_archived is not True
