"""The eleven things the Outlook thread asked for.

CRM-MAIL-2026-11-11. One test file, in the order of the prompt, so
"which item is this" has an answer when one of them fails.

Two things these tests exist to hold in place above all others.

A save must never be lost to a rule about something else. Item 1 is
the account gate refusing a *save* and rolling back the RFQ details
that were typed in the same form. The lead is still not quotable;
the enquiry is still written down.

And a subject is not a thread. Items 3+5 are a mail gateway's "EXT:"
and a colleague's "DSR //" sitting where a reply prefix would be, so
every anchored test for one failed and a chase became a new lead.
"""
import importlib
import os
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'mail11-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'Mail11-123456789')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'mail11.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
importlib.import_module('app.models.assignment')
flask_app = _main.app
db = _main.db
Lead, Company, Employee = _main.Lead, _main.Company, _main.Employee
LeadEmail = _main.LeadEmail

from app.models.assignment import AccountVerticalOwner       # noqa: E402
from app.models.email_admin import VerticalHead              # noqa: E402
from app.services import (account_gate, flags, junk_delete,  # noqa: E402
                          lead_intake, lead_merge, mail_chase,
                          mail_filing, vertical_heads)

VERT = 'Freight Forwarding'
OWNER = 'ZM_OWNER'


def _purge():
    from app.models.audit import AuditEvent

    ids = [l.id for l in Lead.query.filter(Lead.company.like('ZM%')).all()]
    if ids:
        LeadEmail.query.filter(LeadEmail.lead_id.in_(ids)).delete(
            synchronize_session=False)
        AuditEvent.query.filter(
            AuditEvent.entity_type == 'lead',
            AuditEvent.entity_id.in_([str(i) for i in ids])).delete(
                synchronize_session=False)
    Lead.query.filter(Lead.company.like('ZM%')).delete(
        synchronize_session=False)
    account_ids = [c.id for c in
                   Company.query.filter(Company.name.like('ZM%')).all()]
    if account_ids:
        AccountVerticalOwner.query.filter(
            AccountVerticalOwner.account_id.in_(account_ids)).delete(
                synchronize_session=False)
    Company.query.filter(Company.name.like('ZM%')).delete(
        synchronize_session=False)
    Employee.query.filter(Employee.emp_code.like('ZM_%')).delete(
        synchronize_session=False)
    VerticalHead.query.filter(VerticalHead.vertical == VERT).delete(
        synchronize_session=False)
    from app.models.integration import AppSetting
    AppSetting.query.filter(AppSetting.key.in_((
        'flag.FEATURE_ACCOUNT_FIRST',
        'flag.FEATURE_JUNK_DELETE'))).delete(synchronize_session=False)
    db.session.commit()
    flags.cache_clear()
    # The vertical master caches the heads table for thirty seconds,
    # so a module that writes VerticalHead rows and does not clear it
    # leaves the next module resolving verticals against its rows.
    vertical_heads.cache_clear()


@pytest.fixture(autouse=True)
def _clean():
    with flask_app.app_context():
        db.create_all()
        _purge()
        db.session.add(Employee(
            emp_code=OWNER, name='Owner', password_hash='x', role='user',
            email='zm_owner@procamlogistics.com', vertical=VERT,
            is_active=True, must_change_pw=False))
        db.session.add(VerticalHead(vertical=VERT, head_emp_code=OWNER,
                                    active=True))
        db.session.commit()
        vertical_heads.cache_clear()
        _flag(True)
        yield
        _purge()


def _set_flag(name, state=True):
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


def _flag(state=True):
    _set_flag('FEATURE_ACCOUNT_FIRST', state)


def _lead(**kw):
    kw.setdefault('company', 'ZM Stsinfracon')
    kw.setdefault('stage', 'New Opportunity')
    kw.setdefault('procam_vertical', VERT)
    kw.setdefault('assigned_to', OWNER)
    kw.setdefault('email', 'procurement3@zmstsinfracon.example')
    kw.setdefault('created_at', datetime.utcnow())
    lead = Lead(**kw)
    db.session.add(lead)
    db.session.commit()
    _orphans(lead.id)
    return lead


def _orphans(lead_id):
    """Rows another module left behind on this id.

    The whole suite shares one SQLite database and SQLite reuses row
    ids after a delete, so a fresh lead inherits the RFQs, mails and
    activities of a deleted lead with the same id — which made
    `junk-check` answer "an RFQ was raised on it" about a lead
    created two lines earlier. Cleared for this id only.
    """
    from app.models.audit import AuditEvent

    for model, column in ((LeadEmail, 'lead_id'),):
        model.query.filter(getattr(model, column) == lead_id).delete(
            synchronize_session=False)
    for name in ('app.models.rfq', 'app.models.quote'):
        try:
            module = importlib.import_module(name)
        except Exception:                                 # noqa: BLE001
            continue
        for attr in ('RFQ', 'Quote', 'CostSheet'):
            model = getattr(module, attr, None)
            if model is None or not hasattr(model, 'lead_id'):
                continue
            try:
                model.query.filter(model.lead_id == lead_id).delete(
                    synchronize_session=False)
            except Exception:                             # noqa: BLE001
                db.session.rollback()
    for model_name in ('LeadActivity', 'LeadNote', 'LeadAttachment'):
        model = getattr(_main, model_name, None)
        if model is None:
            continue
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


def _client(code=OWNER):
    client = flask_app.test_client()
    with client.session_transaction() as s:
        s['emp_code'] = code
        s['role'] = 'user'
    return client


def _token(client):
    r = client.get('/my-work')
    for header in r.headers.getlist('Set-Cookie'):
        if header.startswith('csrf_token='):
            return header.split('=', 1)[1].split(';')[0]
    return ''


# ═══════════════════════════════════════════════════════════════════
# 1 — the gate blocked saving, not just the move
# ═══════════════════════════════════════════════════════════════════
def test_a_no_account_lead_still_cannot_be_quoted():
    """The line the rule exists for has not moved."""
    with flask_app.app_context():
        lead = _lead()
        assert account_gate.check(lead, 'Quoted', emp_code=OWNER) \
            is not None


def test_the_rfq_details_are_saved_even_though_the_move_is_refused():
    with flask_app.app_context():
        lead_id = _lead().id
    client = _client()
    r = client.put(f'/api/leads/{lead_id}',
                   json={'stage': 'RFQ Generated',
                         'rfq_date': '2026-10-26',
                         'origin': 'Dhar Railway Station',
                         'destination': 'Pithampur',
                         'estimated_value': 500000},
                   headers={'X-CSRFToken': _token(client)})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body['ok'] is True
    assert body['needs'] == 'account'
    with flask_app.app_context():
        lead = db.session.get(Lead, lead_id)
        # The details are on the lead …
        assert str(lead.rfq_date) == '2026-10-26'
        assert lead.origin == 'Dhar Railway Station'
        assert lead.destination == 'Pithampur'
        # … and the stage did not move.
        assert lead.stage == 'New Opportunity'


def test_the_refused_move_is_named_in_the_response():
    """So the screen can offer the picker instead of a dead end."""
    with flask_app.app_context():
        lead_id = _lead().id
    client = _client()
    r = client.put(f'/api/leads/{lead_id}',
                   json={'stage': 'RFQ Generated', 'pic': 'Someone',
                         'estimated_value': 500000},
                   headers={'X-CSRFToken': _token(client)})
    help_ = r.get_json()['stage_refused']
    assert help_['wanted'] == 'RFQ Generated'
    assert help_['from'] == 'New Opportunity'
    assert help_['reason'] == account_gate.WHY_NO_ACCOUNT
    assert 'Account check' in help_['resolver']
    assert account_gate.RULE in help_['error']


def test_a_save_with_no_stage_in_it_is_untouched_by_the_gate():
    with flask_app.app_context():
        lead_id = _lead().id
    client = _client()
    r = client.put(f'/api/leads/{lead_id}', json={'pic': 'Nicole'},
                   headers={'X-CSRFToken': _token(client)})
    assert r.status_code == 200
    assert 'needs' not in (r.get_json() or {})


def test_the_move_goes_through_once_the_account_is_named():
    with flask_app.app_context():
        account = Company(name='ZM STS Infracon Ltd', is_active=True,
                          email_domains=['zmstsinfracon.example'])
        db.session.add(account)
        db.session.flush()
        db.session.add(AccountVerticalOwner(
            account_id=account.id, vertical=VERT, emp_code=OWNER,
            source='manual'))
        lead = _lead(company_id=account.id)
        db.session.commit()
        lead_id, account_id = lead.id, account.id
    client = _client()
    # The quote due date is CRM-COCKPIT-02's rule, not this one, and
    # it still applies from RFQ Generated onwards. It is supplied here
    # for the same reason the estimated value is: this test is about
    # the account gate letting go, not about the other two.
    r = client.put(f'/api/leads/{lead_id}',
                   json={'stage': 'RFQ Generated',
                         'estimated_value': 500000,
                         'quote_due_at': '2026-11-20',
                         'followup': '2026-11-14'},
                   headers={'X-CSRFToken': _token(client)})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert 'stage_refused' not in (r.get_json() or {})
    with flask_app.app_context():
        assert db.session.get(Lead, lead_id).stage == 'RFQ Generated'
        assert db.session.get(Lead, lead_id).company_id == account_id


# ═══════════════════════════════════════════════════════════════════
# 2 — informational, not a lead
# ═══════════════════════════════════════════════════════════════════
def test_informational_is_a_reason_of_its_own():
    assert 'informational' in junk_delete.CODES
    assert 'not a lead' in junk_delete.LABELS['informational'].lower()


def test_informational_never_blocks_the_sender():
    """The address that sends a vessel list also sends real quotes."""
    assert 'informational' not in junk_delete.BLOCKS_BY_DEFAULT


def test_informational_needs_no_sentence():
    """A dropdown reason that is self-explanatory should not demand
    one: an extra box is how people type "junk" 2,312 times."""
    assert 'informational' not in junk_delete.NEEDS_NOTE


def test_an_informational_lead_leaves_the_board_and_is_not_lost():
    with flask_app.app_context():
        _set_flag('FEATURE_JUNK_DELETE', True)
        lead_id = _lead(company='ZM Vessel Positions Weekly').id
    client = _client()
    r = client.post(f'/api/leads/{lead_id}/junk-delete',
                    json={'reason': 'informational', 'note': '',
                          'block_sender': False},
                    headers={'X-CSRFToken': _token(client)})
    assert r.status_code == 200, r.get_data(as_text=True)
    with flask_app.app_context():
        lead = db.session.get(Lead, lead_id)
        assert lead.junk_deleted is True
        assert lead.is_archived is True
        # Never Lost: that is the whole point of the item.
        assert lead.stage != 'Lost'


# ═══════════════════════════════════════════════════════════════════
# 3 + 5 — a reply on an existing thread
# ═══════════════════════════════════════════════════════════════════
def test_the_gateway_tag_no_longer_hides_the_reply():
    subject = ('DSR // EXT: Re: RFQ for Sea Freight - FCA Aussafer, '
               'Italy to CFR Kolkata Sea Port')
    assert lead_intake.subject_kind(subject) == 'reply'


@pytest.mark.parametrize('subject', [
    '[EXT] Re: Sea freight enquiry',
    'EXT: Re: Sea freight enquiry',
    '[EXTERNAL] FW: Sea freight enquiry',
    'DSR // Re: Sea freight enquiry',
    '[SUSPECTED SPAM] Re: Sea freight enquiry',
    'CAUTION: Re: Sea freight enquiry',
    '[EXT] DSR // Re: FW: Sea freight enquiry',
])
def test_every_tag_shape_is_taken_off(subject):
    assert lead_intake.subject_kind(subject) in ('reply', 'forward')
    assert 'Sea freight enquiry' in lead_intake.strip_prefixes(subject)


def test_a_subject_that_merely_starts_with_the_word_is_left_alone():
    """"External affairs tender" is a subject, not a tag. Stripping it
    would change what the thread is called."""
    assert lead_intake.strip_lead_tags('External affairs tender') \
        == 'External affairs tender'
    assert lead_intake.subject_kind('External affairs tender') == 'fresh'


def test_the_double_pipe_in_a_real_subject_survives():
    """Procam's own subjects use || and // as separators. A tag
    stripper that ate those would rewrite half the mailbox."""
    subject = ('RFQ for FTL Transportation || Dhar Railway Station to '
               'STS Infracon, Pithampur || Tihi-Dhar Project ||')
    assert lead_intake.strip_prefixes(subject) == subject


def test_the_reminder_keys_to_the_same_thread_as_the_enquiry():
    """The fix, in the one form that matters: one key, one lead."""
    first = lead_merge.thread_key(
        subject='RFQ for Sea Freight - FCA Aussafer, Italy to CFR Kolkata',
        domain='gevernova.example')
    chase = lead_merge.thread_key(
        subject=('DSR // EXT: Re: RFQ for Sea Freight - FCA Aussafer, '
                 'Italy to CFR Kolkata'),
        domain='gevernova.example')
    assert first and first == chase


def test_two_different_enquiries_still_key_apart():
    a = lead_merge.thread_key(subject='[EXT] Re: Sea freight to Kolkata',
                              domain='gevernova.example')
    b = lead_merge.thread_key(subject='[EXT] Re: Air freight to Chennai',
                              domain='gevernova.example')
    assert a != b


def test_a_chase_is_recognised_only_from_the_customer():
    with flask_app.app_context():
        lead = _lead()
        assert mail_chase.is_a_chase(
            lead, subject='Gentle reminder: D&D estimate',
            from_addr='procurement3@zmstsinfracon.example')
        # Our own wording is us chasing them.
        assert not mail_chase.is_a_chase(
            lead, subject='Gentle reminder: D&D estimate',
            from_addr='someone@procamlogistics.com')
        # Somebody else's customer is not this lead's chase.
        assert not mail_chase.is_a_chase(
            lead, subject='Gentle reminder',
            from_addr='buyer@another.example')


def test_an_ordinary_reply_is_not_a_chase():
    """A revised BOQ is not chasing, and marking it as one would make
    the flag noise — which is the failure that makes people ignore
    a flag."""
    with flask_app.app_context():
        lead = _lead()
        assert not mail_chase.is_a_chase(
            lead, subject='Re: RFQ — revised BOQ attached',
            body='Please find the revised bill of quantities.',
            from_addr='procurement3@zmstsinfracon.example')


def test_the_chase_is_counted_and_dated():
    with flask_app.app_context():
        lead = _lead()
        assert mail_chase.flag(lead, phrase='gentle reminder',
                               message_id='<a@x>')
        assert lead.chase_count == 1
        assert lead.chasing_since is not None
        first = lead.chasing_since
        assert mail_chase.flag(lead, phrase='second reminder',
                               message_id='<b@x>')
        assert lead.chase_count == 2
        # Still the first time they asked: that is the age that matters.
        assert lead.chasing_since == first


def test_the_same_message_is_not_counted_twice():
    """Graph re-delivers a notification whenever the webhook answers
    slowly, and a chase counted three times reads as three chases."""
    with flask_app.app_context():
        lead = _lead()
        assert mail_chase.flag(lead, phrase='reminder',
                               message_id='<same@x>')
        assert not mail_chase.flag(lead, phrase='reminder',
                                   message_id='<same@x>')
        assert lead.chase_count == 1


def test_writing_to_the_customer_clears_the_chase():
    with flask_app.app_context():
        lead = _lead()
        mail_chase.flag(lead, phrase='reminder', message_id='<c@x>')
        db.session.commit()
        lead_id = lead.id
    client = _client()
    r = client.post(f'/api/leads/{lead_id}/emails',
                    json={'body': 'Our estimate is attached.',
                          'status': 'sent',
                          'to_addr': 'procurement3@zmstsinfracon.example'},
                    headers={'X-CSRFToken': _token(client)})
    assert r.status_code == 200, r.get_data(as_text=True)
    with flask_app.app_context():
        lead = db.session.get(Lead, lead_id)
        assert lead.chase_count == 0
        assert lead.chasing_since is None


def test_a_draft_does_not_clear_the_chase():
    """Writing it is not sending it."""
    with flask_app.app_context():
        lead = _lead()
        mail_chase.flag(lead, phrase='reminder', message_id='<d@x>')
        db.session.commit()
        lead_id = lead.id
    client = _client()
    client.post(f'/api/leads/{lead_id}/emails',
                json={'body': 'draft', 'status': 'draft'},
                headers={'X-CSRFToken': _token(client)})
    with flask_app.app_context():
        assert db.session.get(Lead, lead_id).chase_count == 1


def test_the_lead_carries_the_chase_to_the_screen():
    with flask_app.app_context():
        lead = _lead()
        mail_chase.flag(lead, phrase='reminder', message_id='<e@x>')
        db.session.commit()
        out = lead.to_dict()
        assert out['chase_count'] == 1
        assert out['chasing_since']


# ═══════════════════════════════════════════════════════════════════
# 4 — the duplicate picker needs a searchable number
# ═══════════════════════════════════════════════════════════════════
def test_a_lead_can_be_found_by_its_number():
    with flask_app.app_context():
        lead_id = _lead(company='ZM Sinofreight').id
    client = _client()
    r = client.get(f'/api/leads?q={lead_id}')
    body = r.get_json()
    rows = body if isinstance(body, list) else (
        body.get('items') or body.get('leads') or body.get('rows') or [])
    assert lead_id in [row.get('id') for row in rows]


def test_searching_by_name_still_works():
    with flask_app.app_context():
        lead_id = _lead(company='ZM Sinofreight').id
    client = _client()
    r = client.get('/api/leads?q=sinofreight')
    body = r.get_json()
    rows = body if isinstance(body, list) else (
        body.get('items') or body.get('leads') or body.get('rows') or [])
    assert lead_id in [row.get('id') for row in rows]


def test_a_number_search_does_not_require_it_in_the_name_too():
    """The clause is one OR with the rest. ANDed, "10291" would have
    to appear in the company name as well."""
    with flask_app.app_context():
        lead_id = _lead(company='ZM Nothing Numeric Here').id
    client = _client()
    r = client.get(f'/api/leads?q={lead_id}')
    body = r.get_json()
    rows = body if isinstance(body, list) else (
        body.get('items') or body.get('leads') or body.get('rows') or [])
    assert lead_id in [row.get('id') for row in rows]


# ═══════════════════════════════════════════════════════════════════
# 6 / 8 / 9 — pick the account where the refusal happened
# ═══════════════════════════════════════════════════════════════════
def test_the_refusal_carries_the_candidates_with_it():
    with flask_app.app_context():
        db.session.add(Company(name='ZM Sparkon Engineers',
                               is_active=True,
                               email_domains=['zmsparkon.example']))
        db.session.commit()
        lead = _lead(company='ZM Sparkon Engineers',
                     email='mishra@zmsparkon.example')
        help_ = account_gate.help_for(lead, emp_code=OWNER)
    assert help_['reason'] == account_gate.WHY_NO_ACCOUNT
    assert 'Account check' in help_['resolver']
    if help_['can_pick']:
        names = [s['name'] for s in help_['suggestions']]
        assert 'ZM Sparkon Engineers' in names


def test_the_advance_route_carries_the_candidates_too():
    with flask_app.app_context():
        lead_id = _lead().id
    client = _client()
    r = client.post(f'/api/leads/{lead_id}/advance',
                    json={'to_stage': 'Quoted', 'estimated_value': 100000},
                    headers={'X-CSRFToken': _token(client)})
    assert r.status_code == 400
    body = r.get_json()
    assert body['needs'] == 'account'
    assert 'account_help' in body
    assert body['account_help']['reason'] == account_gate.WHY_NO_ACCOUNT


def test_help_for_says_nothing_when_there_is_nothing_to_say():
    with flask_app.app_context():
        account = Company(name='ZM Praj Industries Ltd', is_active=True)
        db.session.add(account)
        db.session.flush()
        db.session.add(AccountVerticalOwner(
            account_id=account.id, vertical=VERT, emp_code=OWNER,
            source='manual'))
        lead = _lead(company_id=account.id)
        db.session.commit()
        help_ = account_gate.help_for(lead, emp_code=OWNER)
    assert help_['reason'] == ''
    assert help_['suggestions'] == []


# ═══════════════════════════════════════════════════════════════════
# 7 — the customer existed and intake did not link it
# ═══════════════════════════════════════════════════════════════════
def test_an_exact_name_links_when_it_is_the_only_candidate():
    """ANDRITZ HYDRO: the account existed under a name that
    normalises identically, and no exact rule fired because the
    account did not carry their domain."""
    from app.services import account_match

    with flask_app.app_context():
        db.session.add(Company(name='ZM Andritz Hydro Pvt Ltd',
                               is_active=True, email_domains=[]))
        db.session.commit()
        account, rule = account_match.auto_link_by_name(
            'someone@zmandritz.example',
            company_name='ZM ANDRITZ HYDRO PRIVATE LIMITED')
        assert account is not None, rule
        assert account.name == 'ZM Andritz Hydro Pvt Ltd'


def test_a_resemblance_never_links_on_its_own():
    from app.services import account_match

    with flask_app.app_context():
        db.session.add(Company(name='ZM Andritz Hydro Pvt Ltd',
                               is_active=True, email_domains=[]))
        db.session.commit()
        account, rule = account_match.auto_link_by_name(
            'someone@zmandritz.example',
            company_name='ZM Andritz Hydra Powerr')
        assert account is None


def test_two_candidates_mean_a_person_decides():
    from app.services import account_match

    with flask_app.app_context():
        db.session.add(Company(name='ZM Andritz Hydro Pvt Ltd',
                               is_active=True,
                               email_domains=['zmandritz.example']))
        db.session.add(Company(name='ZM Andritz Separation Ltd',
                               is_active=True, email_domains=[]))
        db.session.commit()
        account, rule = account_match.auto_link_by_name(
            'someone@zmandritz.example',
            company_name='ZM Andritz Separation Ltd')
        assert account is None


def test_the_intake_no_longer_asks_the_gate_before_linking():
    """It linked only while FEATURE_ACCOUNT_FIRST was on, so every
    lead ingested before 10 November landed on no account however
    obvious the customer was."""
    import inspect

    from email_ingest import single_message

    source = inspect.getsource(single_message)
    marker = source.index('auto_link(')
    before = source[max(0, marker - 900):marker]
    assert 'account_gate.enabled() and not getattr(lead' not in before


# ═══════════════════════════════════════════════════════════════════
# 11 — customer mail in the supplier panel
# ═══════════════════════════════════════════════════════════════════
def _mail(lead, **kw):
    kw.setdefault('direction', 'inbound')
    kw.setdefault('from_addr', lead.email)
    kw.setdefault('subject', 'RFQ for FTL Transportation')
    kw.setdefault('body', 'Please quote.')
    kw.setdefault('intake_class', 'G_rate_sourcing')
    kw.setdefault('sent_or_received_at', datetime.utcnow())
    row = LeadEmail(lead_id=lead.id, **kw)
    db.session.add(row)
    db.session.commit()
    return row


def test_the_customers_own_mail_is_not_rate_sourcing():
    with flask_app.app_context():
        lead = _lead()
        row = _mail(lead)
        assert mail_filing.from_the_customer(lead, row)
        assert not mail_filing.is_supplier_facing(lead, row)
        assert mail_filing.misfiled(lead, row)


def test_a_suppliers_mail_stays_in_the_supplier_panel():
    with flask_app.app_context():
        lead = _lead()
        row = _mail(lead, from_addr='rates@zmshippingline.example')
        assert mail_filing.is_supplier_facing(lead, row)
        assert not mail_filing.misfiled(lead, row)


def test_our_own_rate_request_stays_there_too():
    with flask_app.app_context():
        lead = _lead()
        row = _mail(lead, direction='outbound',
                    from_addr='someone@procamlogistics.com')
        assert mail_filing.is_supplier_facing(lead, row)


def test_free_mail_is_not_a_customer_mark():
    """Two customers on gmail.com are not one customer."""
    with flask_app.app_context():
        lead = _lead(email='buyer@gmail.com')
        row = _mail(lead, from_addr='someone.else@gmail.com')
        assert not mail_filing.from_the_customer(lead, row)


def test_the_rate_sourcing_panel_shows_only_supplier_mail():
    with flask_app.app_context():
        lead = _lead()
        _mail(lead)                                   # the customer's own
        _mail(lead, from_addr='rates@zmshippingline.example',
              subject='Our rates')
        lead_id = lead.id
    client = _client()
    rows = client.get(
        f'/api/leads/{lead_id}/emails?kind=rate_sourcing').get_json()
    assert [r['from_addr'] for r in rows] == ['rates@zmshippingline.example']
    customer = client.get(
        f'/api/leads/{lead_id}/emails?kind=customer').get_json()
    assert 'procurement3@zmstsinfracon.example' in [
        r['from_addr'] for r in customer]


def test_the_enquiry_fallback_never_lands_in_the_supplier_panel():
    """The first fault, and the one that explains all three reported
    leads: a lead with no trail rows had its own enquiry synthesised
    into every list, the supplier one included."""
    with flask_app.app_context():
        lead = _lead(original_email_body='Please quote FTL Dhar-Pithampur.',
                     original_email_from='procurement3@zmstsinfracon.example',
                     original_email_subject='RFQ for FTL Transportation')
        db.session.commit()
        lead_id = lead.id
    client = _client()
    assert client.get(
        f'/api/leads/{lead_id}/emails?kind=rate_sourcing').get_json() == []
    trail = client.get(
        f'/api/leads/{lead_id}/emails?kind=customer').get_json()
    assert len(trail) == 1
    assert trail[0]['subject'] == 'RFQ for FTL Transportation'


# ═══════════════════════════════════════════════════════════════════
# 10 — the bulk linking tool
# ═══════════════════════════════════════════════════════════════════
def _script():
    import importlib.util

    path = os.path.join(_ROOT, 'scripts', '2026_11_11_link_accounts.py')
    spec = importlib.util.spec_from_file_location('zm_link', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_tool_proposes_the_obvious_account():
    mod = _script()
    with flask_app.app_context():
        db.session.add(Company(name='ZM STS Infracon Ltd', is_active=True,
                               email_domains=['zmstsinfracon.example']))
        db.session.commit()
        lead = _lead()
        confidence, best, others = mod.propose(lead)
        assert confidence == mod.CERTAIN
        assert best['account'].name == 'ZM STS Infracon Ltd'


def test_the_tool_says_none_when_nothing_matches():
    mod = _script()
    with flask_app.app_context():
        lead = _lead(company='ZM Unknown', email='x@zmnowhere.example')
        confidence, best, _others = mod.propose(lead)
        assert confidence == mod.NONE
        assert best is None


def test_only_ticked_rows_are_applied(tmp_path):
    mod = _script()
    with flask_app.app_context():
        account = Company(name='ZM STS Infracon Ltd', is_active=True,
                          email_domains=['zmstsinfracon.example'])
        db.session.add(account)
        db.session.commit()
        ticked = _lead()
        untouched = _lead(company='ZM Second Lead')
        rows = [
            {**mod.row_for(ticked, 'no_account', mod.CERTAIN,
                           {'account': account, 'rule': 'domain'}, []),
             'apply': 'y'},
            mod.row_for(untouched, 'no_account', mod.CERTAIN,
                        {'account': account, 'rule': 'domain'}, []),
        ]
        path = str(tmp_path / 'ticked.csv')
        mod._write_one(path, rows)

        to_link, to_archive, bad = mod.read_ticks(path)
        assert [t[0] for t in to_link] == [ticked.id]
        assert not to_archive and not bad

        linkable, archivable, refused = mod.plan(to_link, to_archive)
        assert len(linkable) == 1
        batch = mod.next_batch_id()
        mod.apply(linkable, archivable, batch=batch)

        db.session.expire_all()
        assert db.session.get(Lead, ticked.id).company_id == account.id
        assert db.session.get(Lead, untouched.id).company_id is None

        # and it reverses
        mod.undo(batch)
        db.session.expire_all()
        assert db.session.get(Lead, ticked.id).company_id is None


def test_a_row_placed_since_the_csv_was_written_is_refused():
    mod = _script()
    with flask_app.app_context():
        account = Company(name='ZM STS Infracon Ltd', is_active=True)
        other = Company(name='ZM Someone Else', is_active=True)
        db.session.add_all([account, other])
        db.session.commit()
        lead = _lead(company_id=other.id)
        linkable, archivable, refused = mod.plan(
            [(lead.id, account.id, 'domain')], [])
        assert not linkable
        assert refused and 'already on account' in refused[0][1]


def test_the_tool_does_not_teach_the_matcher_in_bulk():
    """A tick is a decision about one lead. A learned sender is a
    decision about every future mail from that address."""
    import inspect

    mod = _script()
    assert 'learn=False' in inspect.getsource(mod.apply)


def test_the_csv_carries_the_tick_and_the_archive_columns():
    mod = _script()
    assert mod.FIELDS[0] == 'apply'
    assert 'archive' in mod.FIELDS
    assert 'account_id' in mod.FIELDS


def test_an_unknown_archive_word_is_refused_not_guessed():
    mod = _script()
    with flask_app.app_context():
        lead = _lead()
        path = os.path.join(tempfile.mkdtemp(), 'bad.csv')
        mod._write_one(path, [
            {**mod.row_for(lead, 'placeholder_account', mod.NONE, None, []),
             'archive': 'delete it'}])
        to_link, to_archive, bad = mod.read_ticks(path)
        assert not to_link and not to_archive
        assert bad and 'not one of' in bad[0][1]


# ═══════════════════════════════════════════════════════════════════
# the duplicate report that ran out of memory
# ═══════════════════════════════════════════════════════════════════
def test_the_report_reads_columns_not_whole_leads():
    """It was killed by the kernel on the live book: every live lead
    as a full ORM row, each carrying the customer's entire enquiry."""
    from app.services import lead_duplicates as ld

    assert 'original_email_body' not in ld.PRINT_COLUMNS
    with flask_app.app_context():
        _lead()
        prints = ld.live_prints()
        assert prints
        row, mine = next(iter(prints.values()))
        assert not hasattr(row, 'original_email_body')
        assert set(mine) == {ld.NAME, ld.DOMAIN, ld.PHONE, ld.GSTIN}


def test_the_report_still_finds_a_real_pair():
    from app.services import lead_dupe_report as report

    with flask_app.app_context():
        _lead(phone='+91 98765 43210')
        _lead(company='ZM Stsinfracon Projects', phone='098765 43210',
              email='other@zmelsewhere.example')
        pairs = report.pairs(limit=20)
        assert pairs
        assert 'phone' in pairs[0]['matched']


def test_a_value_shared_by_everybody_is_skipped():
    """A switchboard number on fifty leads identifies nothing, and
    comparing them all is the n-squared problem in one bucket."""
    from app.services import lead_dupe_report as report

    with flask_app.app_context():
        for i in range(report.MAX_PER_VALUE + 2):
            _lead(company=f'ZM Shared Board {i}', phone='+91 22 1234 5678',
                  email=f'x{i}@zmshared{i}.example')
        pairs = report.pairs(limit=50)
        assert not [p for p in pairs if 'phone' in p['matched']]


# ═══════════════════════════════════════════════════════════════════
# 10 — the admin screen
# ═══════════════════════════════════════════════════════════════════
def test_the_link_screen_refuses_a_salesperson():
    with flask_app.app_context():
        _lead()
    client = _client()
    assert client.get('/admin/account-link').status_code == 403
    assert client.get('/api/admin/account-link').status_code == 403


def test_the_link_screen_refuses_a_stranger():
    assert flask_app.test_client().get(
        '/admin/account-link').status_code == 401


def test_the_link_screen_renders_for_an_admin():
    with flask_app.app_context():
        db.session.add(Employee(
            emp_code='ZM_ADMIN', name='Adm', password_hash='x',
            role='admin', vertical=VERT, is_active=True,
            must_change_pw=False))
        db.session.commit()
        _lead()
    client = _client('ZM_ADMIN')
    with client.session_transaction() as s:
        s['role'] = 'admin'
    r = client.get('/admin/account-link')
    if r.status_code == 403:
        pytest.skip('the Access Matrix does not give this role '
                    'admin.triage in this database')
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    # The include that makes every POST on the page carry its token.
    assert 'csrf' in body.lower()
    r = client.get('/api/admin/account-link?limit=5')
    assert r.status_code == 200
    assert r.get_json()['ok'] is True


def test_rejecting_a_proposal_changes_nothing_about_the_lead():
    with flask_app.app_context():
        db.session.add(Employee(
            emp_code='ZM_ADMIN2', name='Adm', password_hash='x',
            role='admin', vertical=VERT, is_active=True,
            must_change_pw=False))
        account = Company(name='ZM Not The Customer', is_active=True)
        db.session.add(account)
        db.session.commit()
        lead_id, account_id = _lead().id, account.id
    client = _client('ZM_ADMIN2')
    with client.session_transaction() as s:
        s['role'] = 'admin'
    r = client.post('/api/admin/account-link/reject',
                    json={'lead_id': lead_id, 'account_id': account_id,
                          'why': 'different company'},
                    headers={'X-CSRFToken': _token(client)})
    if r.status_code == 403:
        pytest.skip('the Access Matrix does not give this role '
                    'admin.triage in this database')
    assert r.status_code == 200, r.get_data(as_text=True)
    with flask_app.app_context():
        assert db.session.get(Lead, lead_id).company_id is None
        from app.models.audit import AuditEvent
        assert AuditEvent.query.filter(
            AuditEvent.action == 'lead.account_proposal_rejected',
            AuditEvent.entity_id == str(lead_id)).count() == 1
