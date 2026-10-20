"""
FF-COMMS-01 R2 and R3 — references, confidentiality, replies, caps.

Two things here are not ordinary features and are tested as such.

The CRN is gap-free, which only means anything if a rolled-back
transaction gives its number back. And a vendor must never learn who
the client is: there is a redaction, and behind it a leak check that
reads the finished message and refuses to send. The second is the
one that matters, because the first will miss something eventually.
"""
import os
import sys
import tempfile
from datetime import date, datetime, timedelta

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'ffcomms-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'FfCommsTest12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'ffcomms.db'))

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Company, Employee, Lead = _main.Company, _main.Employee, _main.Lead
flask_app.config['WTF_CSRF_ENABLED'] = False

from app.models.access import AccessProfile, DataScope    # noqa: E402
from app.models.ff import (CrnSequence, FfEmailTemplate,  # noqa: E402
                           RateRequest)
from app.models.rfq import RFQ, RateSourcingLine          # noqa: E402
from app.services import (crn as crn_svc, ff_inbound,     # noqa: E402
                          ff_privacy, ff_rates, ff_templates,
                          outbox)

ADMIN = 'FCADMIN'
TAG = 'FC '


def _purge():
    """Remove everything this module made.

    Run before *and* after. The house style here is clean-at-start,
    which leaves the last module's rows in a database every other
    module shares — and these rows are leads in a live vertical, so
    they turned up inside the sales-funnel counts two files later.
    """
    from app import Contact
    CrnSequence.query.delete()
    RateRequest.query.delete()
    FfEmailTemplate.query.delete()
    RateSourcingLine.query.filter(
        RateSourcingLine.service.like(TAG + '%')).delete(
            synchronize_session=False)
    RFQ.query.filter(RFQ.subject.like(TAG + '%')).delete(
        synchronize_session=False)
    Lead.query.filter(Lead.company.like(TAG + '%')).delete(
        synchronize_session=False)
    Contact.query.filter(Contact.company.like(TAG + '%')).delete(
        synchronize_session=False)
    accounts = [c.id for c in Company.query.filter(
        Company.name.like(TAG + '%')).all()]
    if accounts:
        Contact.query.filter(
            Contact.company_id.in_(accounts)).delete(
                synchronize_session=False)
        Company.query.filter(Company.id.in_(accounts)).delete(
            synchronize_session=False)
    db.session.commit()


@pytest.fixture()
def world():
    with flask_app.app_context():
        db.create_all()
        _purge()
        CrnSequence.query.delete()
        RateRequest.query.delete()
        RateSourcingLine.query.filter(
            RateSourcingLine.service.like(TAG + '%')).delete(
                synchronize_session=False)
        RFQ.query.filter(RFQ.subject.like(TAG + '%')).delete(
            synchronize_session=False)
        Lead.query.filter(Lead.company.like(TAG + '%')).delete(
            synchronize_session=False)
        Company.query.filter(Company.name.like(TAG + '%')).delete(
            synchronize_session=False)

        e = (Employee.query.filter_by(emp_code=ADMIN).first()
             or Employee(emp_code=ADMIN))
        e.name, e.is_active, e.must_change_pw = 'FF Comms Admin', True, False
        e.role, e.is_super_admin, e.session_version = 'admin', True, 0
        e.email = 'fcadmin@procamgroup.in'
        db.session.add(e)
        db.session.flush()
        AccessProfile.query.filter_by(emp_code=ADMIN).delete()
        db.session.add(AccessProfile(emp_code=ADMIN,
                                     data_scope=DataScope.ALL, perms=[]))

        acct = Company(name=TAG + 'Tata Steel Limited', is_active=True,
                       email_domains=['tatasteel.com'])
        db.session.add(acct)
        db.session.flush()
        lead = Lead(company=TAG + 'Tata Steel Limited',
                    company_id=acct.id, stage='New',
                    procam_vertical='Freight Forwarding',
                    assigned_to=ADMIN, email='buyer@tatasteel.com',
                    pic='Ramesh Iyer')
        db.session.add(lead)
        db.session.flush()
        rfq = RFQ(rfq_number=f'FC-{datetime.utcnow():%H%M%S%f}',
                  subject=TAG + 'Chennai to Hamburg', lead_id=lead.id,
                  account_id=acct.id, status='Received')
        db.session.add(rfq)
        db.session.flush()
        line = RateSourcingLine(rfq_id=rfq.id, line_no=1,
                                service=TAG + 'Ocean freight',
                                charge_group='freight',
                                origin='Chennai', destination='Hamburg',
                                scope='40ft HC for Tata Steel Limited')
        db.session.add(line)
        db.session.commit()
        yield {'account': acct.id, 'lead': lead.id, 'rfq': rfq.id,
               'line': line.id}
        _purge()


def _client(code=ADMIN):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code=code, name=code, role='admin', sv=0)
    return c


# ── R2: the reference ────────────────────────────────────────────────
def test_the_shape_is_bu_kind_fy_serial():
    assert crn_svc.format_crn('PWL', 'C', '26', 1) == 'PWL-C26-000001'
    assert crn_svc.format_crn('PWL', 'T', '26', 42) == 'PWL-T26-000042'


def test_the_financial_year_starts_in_april():
    assert crn_svc.financial_year(date(2026, 10, 20)) == '26'
    assert crn_svc.financial_year(date(2027, 3, 31)) == '26'
    assert crn_svc.financial_year(date(2027, 4, 1)) == '27'
    assert crn_svc.financial_year(date(2026, 1, 15)) == '25'


def test_a_reference_reads_back_as_it_was_written():
    d = crn_svc.parse('PWL-T26-000042')
    assert d == {'bu': 'PWL', 'kind': 'T', 'fy': '26', 'serial': 42}


def test_rubbish_is_refused_not_coerced():
    for bad in ('PWL-X26-000001', 'PWL-C6-000001', 'PWLC26000001',
                'PWL-C26-1', ''):
        with pytest.raises(crn_svc.CrnError):
            crn_svc.parse(bad)


def test_numbers_run_in_order_and_are_never_reused(world):
    with flask_app.app_context():
        got = [crn_svc.allocate('PWL', 'C') for _ in range(5)]
        db.session.commit()
    assert got == [f'PWL-C{crn_svc.financial_year()}-{n:06d}'
                   for n in range(1, 6)]
    assert len(set(got)) == 5


def test_a_rolled_back_transaction_burns_no_number(world):
    """What gap-free has to mean in practice."""
    with flask_app.app_context():
        first = crn_svc.allocate('PWL', 'C')
        db.session.commit()
        crn_svc.allocate('PWL', 'C')      # taken...
        db.session.rollback()             # ...and given back
        after = crn_svc.allocate('PWL', 'C')
        db.session.commit()
    assert crn_svc.parse(first)['serial'] == 1
    assert crn_svc.parse(after)['serial'] == 2, (
        'the rolled-back allocation left a hole in the series')


def test_the_two_series_are_separate_counters(world):
    with flask_app.app_context():
        c = crn_svc.allocate('PWL', 'C')
        t = crn_svc.allocate('PWL', 'T')
        db.session.commit()
    assert crn_svc.parse(c)['serial'] == 1
    assert crn_svc.parse(t)['serial'] == 1, (
        'the vendor series must not share the customer counter — if it '
        'did, a vendor could infer how many jobs exist')


def test_each_business_unit_counts_on_its_own(world):
    with flask_app.app_context():
        a = crn_svc.allocate('PWL', 'C')
        b = crn_svc.allocate('PLPL', 'C')
        db.session.commit()
    assert crn_svc.parse(a)['serial'] == 1
    assert crn_svc.parse(b)['serial'] == 1


def test_an_enquiry_is_stamped_once(world):
    with flask_app.app_context():
        lead = db.session.get(Lead, world['lead'])
        first = crn_svc.ensure_for_lead(lead)
        db.session.commit()
        again = crn_svc.ensure_for_lead(lead)
        db.session.commit()
    assert first == again, 'a second stamp would orphan the first thread'


def test_an_rfq_carries_the_customer_reference_and_gets_a_vendor_one(world):
    with flask_app.app_context():
        lead = db.session.get(Lead, world['lead'])
        rfq = db.session.get(RFQ, world['rfq'])
        crn_svc.ensure_for_lead(lead)
        c, t = crn_svc.ensure_for_rfq(rfq, lead=lead)
        db.session.commit()
        lead_crn = lead.crn          # read before the context closes
    assert c == lead_crn
    assert crn_svc.parse(t)['kind'] == 'T'
    assert t != c


def test_a_reference_is_found_in_a_messy_subject():
    found = crn_svc.find_in('Re: Fwd: [PWL-T26-000014] rate for Chennai')
    assert found == ['PWL-T26-000014']


def test_two_references_in_one_subject_are_both_returned():
    """Picking one silently is how mail is filed against the wrong job."""
    found = crn_svc.find_in('RE: PWL-T26-000014 and PWL-T26-000015')
    assert found == ['PWL-T26-000014', 'PWL-T26-000015']


# ── R3: the vendor never learns who the client is ────────────────────
def _secrets(world_, keep=('Chennai', 'Hamburg')):
    acct = db.session.get(Company, world_['account'])
    lead = db.session.get(Lead, world_['lead'])
    return ff_privacy.secrets_for(account=acct, lead=lead,
                                  consignee='Hamburg Stahlwerke',
                                  keep=list(keep))


def test_the_client_name_is_taken_out(world):
    with flask_app.app_context():
        names, domains = _secrets(world)
        out = ff_privacy.redact(
            'Please quote for FC Tata Steel Limited, contact '
            'buyer@tatasteel.com', names, domains)
    assert 'Tata Steel' not in out
    assert 'tatasteel.com' not in out
    assert ff_privacy.MASK in out


def test_the_consignee_is_taken_out_too(world):
    with flask_app.app_context():
        names, domains = _secrets(world)
        out = ff_privacy.redact('Deliver to Hamburg Stahlwerke',
                                names, domains)
    assert 'Stahlwerke' not in out


def test_the_port_survives_even_when_the_consignee_is_named_for_it(world):
    """A consignee called "Hamburg Stahlwerke" must not make Hamburg
    a secret: a rate request that will not say where the cargo is
    going cannot be answered."""
    with flask_app.app_context():
        names, domains = _secrets(world)
        out = ff_privacy.redact('Chennai to Hamburg, 40ft HC',
                                names, domains)
    assert 'Hamburg' in out and 'Chennai' in out
    # The full phrase is still a secret.
    assert 'Stahlwerke' not in ff_privacy.redact(
        'consignee Hamburg Stahlwerke', names, domains)


def test_our_own_domain_survives_so_a_vendor_can_reply(world):
    with flask_app.app_context():
        names, domains = _secrets(world)
        out = ff_privacy.redact('Reply to fcadmin@procamgroup.in',
                                names, domains)
    assert 'procamgroup.in' in out


def test_a_short_name_is_not_redacted_into_nonsense():
    """A client called "SP" would otherwise blank those letters
    everywhere and produce a message nobody can read."""
    names, domains = ff_privacy.secrets_for(extra_names=['SP'])
    assert names == set()


def test_the_longest_name_goes_first(world):
    with flask_app.app_context():
        names, domains = ff_privacy.secrets_for(
            extra_names=['Tata', 'Tata Steel Limited'])
        out = ff_privacy.redact('for Tata Steel Limited today',
                                names, domains)
    assert 'Steel Limited' not in out, (
        'a short alias matched first and left the rest behind')


def test_a_leak_refuses_the_send(world):
    """The gate that matters. Redaction will miss something one day."""
    with flask_app.app_context():
        names, domains = _secrets(world)
        with pytest.raises(ff_privacy.LeakError) as exc:
            ff_privacy.assert_clean(
                ['Rate request', 'Cargo for Tata Steel Limited',
                 'quote.pdf'], names, domains)
        assert 'nothing was sent' in str(exc.value)


def test_a_clean_message_passes_the_gate(world):
    with flask_app.app_context():
        names, domains = _secrets(world)
        assert ff_privacy.assert_clean(
            ['Rate request PWL-T26-000001',
             '40ft HC Chennai to Hamburg'], names, domains) is True


def test_an_attachment_name_is_checked_as_well(world):
    """The leak that redaction misses: the body is clean and the file
    is called TataSteel_RFQ.pdf."""
    with flask_app.app_context():
        names, domains = _secrets(world)
        with pytest.raises(ff_privacy.LeakError):
            ff_privacy.assert_clean(
                ['Rate request', 'Clean body',
                 'Tata Steel Limited RFQ.pdf'], names, domains)


def test_the_vendor_view_shows_the_t_number_only(world):
    with flask_app.app_context():
        lead = db.session.get(Lead, world['lead'])
        rfq = db.session.get(RFQ, world['rfq'])
        crn_svc.ensure_for_lead(lead)
        crn_svc.ensure_for_rfq(rfq, lead=lead)
        line = db.session.get(RateSourcingLine, world['line'])
        made, _ = ff_rates.request_rates(line, [{'name': 'Maersk'}],
                                         actor=ADMIN)
        db.session.commit()
        acct = db.session.get(Company, world['account'])
        view = ff_privacy.vendor_view(made[0], line=line, rfq=rfq,
                                      account=acct, lead=lead)
    assert view['reference'] == rfq.vendor_crn
    assert view['reference'] != rfq.crn
    assert 'Tata' not in str(view), str(view)


# ── R3: replies ──────────────────────────────────────────────────────
def _asked(world_, email='rates@maersk.com'):
    lead = db.session.get(Lead, world_['lead'])
    rfq = db.session.get(RFQ, world_['rfq'])
    crn_svc.ensure_for_lead(lead)
    crn_svc.ensure_for_rfq(rfq, lead=lead)
    line = db.session.get(RateSourcingLine, world_['line'])
    made, _ = ff_rates.request_rates(
        line, [{'name': 'Maersk', 'email': email}], actor=ADMIN)
    db.session.commit()
    return rfq, made[0]


def test_a_reply_is_filed_by_reference_and_sender(world):
    with flask_app.app_context():
        rfq, req = _asked(world)
        r = ff_inbound.link(f'RE: [{rfq.vendor_crn}] rate',
                            'rates@maersk.com')
        assert r.matched and r.confidence == 'exact'
        assert r.request.id == req.id


def test_a_reply_with_no_reference_is_left_for_a_person(world):
    with flask_app.app_context():
        _asked(world)
        r = ff_inbound.link('Re: your enquiry', 'rates@maersk.com')
    assert not r.matched and 'cannot place' in r.reason


def test_a_customer_reference_does_not_file_a_vendor_reply(world):
    """A C-number should never be in vendor mail at all; if one turns
    up, that is a leak to investigate, not a match to accept."""
    with flask_app.app_context():
        lead = db.session.get(Lead, world['lead'])
        crn_svc.ensure_for_lead(lead)
        db.session.commit()
        r = ff_inbound.link(f'RE: [{lead.crn}]', 'rates@maersk.com')
    assert not r.matched


def test_an_unknown_sender_is_not_guessed_at(world):
    with flask_app.app_context():
        rfq, _ = _asked(world)
        ff_rates.request_rates(
            db.session.get(RateSourcingLine, world['line']),
            [{'name': 'MSC', 'email': 'rates@msc.com'}], actor=ADMIN)
        db.session.commit()
        r = ff_inbound.link(f'RE: [{rfq.vendor_crn}]',
                            'someone@elsewhere.com')
    assert not r.matched
    assert 'not one of the' in r.reason


def test_one_vendor_asked_means_the_reference_is_enough(world):
    with flask_app.app_context():
        rfq, req = _asked(world)
        r = ff_inbound.link(f'RE: [{rfq.vendor_crn}]',
                            'someone.else@maersk.com')
    assert r.matched and r.confidence == 'likely'


# ── R3: reading a rate out of prose ──────────────────────────────────
@pytest.mark.parametrize('body,amount,currency', [
    ('Our rate is USD 1,450.00 all in', 1450.0, 'USD'),
    ('INR 125000 per container', 125000.0, 'INR'),
    ('We can do 2,300 EUR', 2300.0, 'EUR'),
    ('$1450 door to port', 1450.0, 'USD'),
])
def test_an_amount_is_read_out_of_the_sentence(body, amount, currency):
    got = ff_inbound.extract(body)
    assert got['amount'] == amount
    assert got['currency'] == currency


def test_a_small_number_is_not_mistaken_for_a_rate():
    """"2 x 40ft, 28 days" has no rate in it."""
    got = ff_inbound.extract('2 x 40ft containers, 28 days transit')
    assert got['amount'] is None
    assert 'No amount recognised' in ' '.join(got['notes'])


def test_a_transit_range_is_read_as_its_longer_end():
    """Promising the optimistic number is how a schedule slips on
    day one."""
    got = ff_inbound.extract('USD 1500, transit 28-32 days')
    assert got['transit_days'] == 32


def test_a_validity_in_the_past_is_flagged():
    got = ff_inbound.extract('USD 1500 valid till 01/01/2020')
    assert got['validity_until'] == date(2020, 1, 1)
    assert any('in the past' in n for n in got['notes'])


def test_extraction_never_says_it_is_confirmed():
    got = ff_inbound.extract('USD 1500, 30 days, valid till 31/12/2026')
    assert got['confirmed'] is False
    assert got['amount_text'], 'the person confirming must see the words'


def test_extraction_writes_nothing(world):
    with flask_app.app_context():
        _rfq, req = _asked(world)
        before = req.status
        ff_inbound.extract('USD 1500 all in, 30 days')
        db.session.commit()
        db.session.refresh(req)
    assert req.status == before and req.amount is None


# ── R3: templates ────────────────────────────────────────────────────
def test_all_six_templates_seed(world):
    with flask_app.app_context():
        ff_templates.seed()
        db.session.commit()
        codes = {t.code for t in FfEmailTemplate.query.all()}
    assert codes >= set(ff_templates.CODES)


def test_seeding_twice_does_not_overwrite_an_edit(world):
    with flask_app.app_context():
        ff_templates.seed()
        db.session.commit()
        tpl = FfEmailTemplate.query.filter_by(
            code=ff_templates.RFQ).first()
        tpl.subject = 'Our own wording {reference}'
        db.session.commit()
        ff_templates.seed()
        db.session.commit()
        again = FfEmailTemplate.query.filter_by(
            code=ff_templates.RFQ).first()
    assert again.subject == 'Our own wording {reference}', (
        "a redeploy reset somebody's careful rewrite")


def test_the_rate_request_template_is_vendor_facing(world):
    with flask_app.app_context():
        ff_templates.seed()
        db.session.commit()
        assert ff_templates.is_vendor_facing(ff_templates.RFQ)
        assert not ff_templates.is_vendor_facing(ff_templates.QUOTE)


def test_a_missing_placeholder_renders_blank_not_an_error(world):
    with flask_app.app_context():
        ff_templates.seed()
        db.session.commit()
        subject, html = ff_templates.render(
            ff_templates.RFQ, {'reference': 'PWL-T26-000001'})
    assert 'PWL-T26-000001' in subject
    assert '{service}' not in html


def test_an_unseeded_template_raises_rather_than_inventing_one(world):
    with flask_app.app_context():
        FfEmailTemplate.query.delete()
        db.session.commit()
        with pytest.raises(LookupError):
            ff_templates.render(ff_templates.RFQ, {})


# ── R3: the caps ─────────────────────────────────────────────────────
def test_the_caps_are_the_numbers_the_business_set():
    assert outbox.DEFAULT_MAX_PER_MINUTE == 20
    assert outbox.DEFAULT_MAX_RECIPIENTS_PER_DAY == 3000
    assert outbox.DEFAULT_MAX_BD_PER_DAY == 100


def test_a_cc_list_counts_as_many_recipients(world):
    from app.models.mailops import EmailOutbox
    row = EmailOutbox(to_addr='a@x.com',
                      cc_addr='b@x.com, c@x.com; d@x.com',
                      subject='s', html='<p></p>')
    assert outbox._recipients_of(row) == 4, (
        'a digest to twelve people on CC is twelve recipients to '
        'Microsoft, and a cap that counts it as one does not hold')


def test_business_development_mail_is_recognised():
    assert outbox.is_bd('ff.bd.intro')
    assert outbox.is_bd('campaign.october')
    assert not outbox.is_bd('lead.assigned')
    assert not outbox.is_bd(None)


def test_over_the_cap_is_held_not_failed(world, monkeypatch):
    """A held message arrives tomorrow. A failed one waits for
    somebody to notice, and nobody ever does."""
    from app.models.mailops import EmailOutbox
    monkeypatch.setattr(outbox, 'max_recipients_per_day', lambda: 1)
    monkeypatch.setattr(outbox, 'recipients_today',
                        lambda now=None, bd_only=False: 1)
    row = EmailOutbox(to_addr='a@x.com', subject='s', html='<p></p>')
    allowed, why = outbox.may_send(row)
    assert allowed is False
    assert 'holding' in why


def test_a_capped_message_waits_for_tomorrow_not_two_minutes():
    now = datetime(2026, 10, 20, 17, 30)
    assert outbox.next_day(now) == datetime(2026, 10, 21)


def test_the_bd_cap_is_separate_from_the_overall_one(world, monkeypatch):
    from app.models.mailops import EmailOutbox
    monkeypatch.setattr(outbox, 'max_recipients_per_day', lambda: 3000)
    monkeypatch.setattr(outbox, 'max_bd_per_day', lambda: 100)
    monkeypatch.setattr(
        outbox, 'recipients_today',
        lambda now=None, bd_only=False: 100 if bd_only else 200)
    bd = EmailOutbox(to_addr='a@x.com', subject='s', html='<p></p>',
                     event_key='ff.bd.intro')
    ordinary = EmailOutbox(to_addr='a@x.com', subject='s',
                           html='<p></p>', event_key='lead.assigned')
    assert outbox.may_send(bd)[0] is False
    assert outbox.may_send(ordinary)[0] is True


def test_the_worker_actually_calls_the_cap():
    """The control that exists and is never reached — the bug shipped
    twice already this week."""
    with open(os.path.join(_ROOT, 'app', 'services',
                           'outbox.py')) as fh:
        src = fh.read()
    body = src.split('def run_once', 1)[-1]
    assert 'may_send(' in body, 'the daily cap is never consulted'
    assert 'next_day(' in body


# ── the flag ─────────────────────────────────────────────────────────
def test_the_new_routes_are_dark_while_the_flag_is_off(world):
    c = _client()
    for path in ('/api/ff/crn/next', '/api/ff/templates',
                 '/api/ff/send-caps'):
        assert c.get(path).status_code == 404, path


# ── R3: the client's people, not just their company ──────────────────
def _with_contact(world_, name='Ramesh Iyer', phone='+91 98765 43210',
                  email='ramesh.iyer@tatasteel.com'):
    from app import Contact
    c = Contact(name=name, company_id=world_['account'],
                account_id=world_['account'], email=email, phone=phone,
                mobile='9123456780', is_active=True)
    db.session.add(c)
    db.session.commit()
    return c


def test_a_contact_name_is_a_secret(world):
    """A vendor who learns the buyer's name can go round us — the
    company name is only the most obvious giveaway."""
    with flask_app.app_context():
        _with_contact(world)
        s = ff_privacy.secrets_for(
            account=db.session.get(Company, world['account']),
            lead=db.session.get(Lead, world['lead']))
        out = ff_privacy.redact('Contact Ramesh Iyer on arrival',
                                s.names, s.domains, s.phones)
    assert 'Ramesh' not in out and 'Iyer' not in out


def test_a_contact_phone_is_a_secret_however_it_is_typed(world):
    with flask_app.app_context():
        _with_contact(world)
        s = ff_privacy.secrets_for(
            account=db.session.get(Company, world['account']),
            lead=db.session.get(Lead, world['lead']))
        for written in ('+91 98765 43210', '098765-43210',
                        '9876543210', '+91-98765-43210',
                        '91 98765 43210'):
            out = ff_privacy.redact(f'Call {written} on arrival',
                                    s.names, s.domains, s.phones)
            assert '43210' not in out, written
            assert ff_privacy.MASK_PHONE in out, written


def test_a_contact_email_domain_is_a_secret(world):
    with flask_app.app_context():
        _with_contact(world, email='buyer@steelbuyer-india.com')
        s = ff_privacy.secrets_for(
            account=db.session.get(Company, world['account']),
            lead=db.session.get(Lead, world['lead']))
        out = ff_privacy.redact('write to buyer@steelbuyer-india.com',
                                s.names, s.domains, s.phones)
    assert 'steelbuyer-india.com' not in out


def test_a_phone_in_an_attachment_name_fails_the_send(world):
    with flask_app.app_context():
        _with_contact(world)
        s = ff_privacy.secrets_for(
            account=db.session.get(Company, world['account']),
            lead=db.session.get(Lead, world['lead']))
        with pytest.raises(ff_privacy.LeakError):
            ff_privacy.assert_clean(
                ['Rate request', 'clean body',
                 'RFQ contact 9876543210.pdf'],
                s.names, s.domains, s.phones)


def test_a_contact_leak_refuses_the_send(world):
    with flask_app.app_context():
        _with_contact(world)
        s = ff_privacy.secrets_for(
            account=db.session.get(Company, world['account']),
            lead=db.session.get(Lead, world['lead']))
        with pytest.raises(ff_privacy.LeakError):
            ff_privacy.assert_clean(['Ask Ramesh Iyer for the packing list'],
                                    s.names, s.domains, s.phones)


def test_a_short_number_is_not_treated_as_a_phone(world):
    """"40ft x 2" and "ETA 1200" are not telephone numbers; redacting
    them would make the request unreadable."""
    with flask_app.app_context():
        assert ff_privacy.phone_digits('1200') == ''
        assert ff_privacy.phone_digits('40') == ''
        assert ff_privacy.phone_digits('9876543210') == '9876543210'


def test_the_vendor_view_carries_no_contact_detail(world):
    with flask_app.app_context():
        _with_contact(world)
        lead = db.session.get(Lead, world['lead'])
        rfq = db.session.get(RFQ, world['rfq'])
        crn_svc.ensure_for_lead(lead)
        crn_svc.ensure_for_rfq(rfq, lead=lead)
        line = db.session.get(RateSourcingLine, world['line'])
        line.scope = ('40ft HC for Tata Steel, call Ramesh Iyer on '
                      '+91 98765 43210, ramesh.iyer@tatasteel.com')
        made, _ = ff_rates.request_rates(line, [{'name': 'Maersk'}],
                                         actor=ADMIN)
        db.session.commit()
        view = ff_privacy.vendor_view(
            made[0], line=line, rfq=rfq,
            account=db.session.get(Company, world['account']), lead=lead)
        blob = str(view)
    for secret in ('Tata', 'Ramesh', 'Iyer', '43210', 'tatasteel.com'):
        assert secret not in blob, f'{secret} reached the vendor: {blob}'
    # And the route still reads.
    assert 'Chennai' in blob and 'Hamburg' in blob


def test_the_old_two_value_unpack_still_works(world):
    """Existing callers wrote `names, domains = secrets_for(...)`."""
    with flask_app.app_context():
        names, domains = ff_privacy.secrets_for(extra_names=['Acme Trading Co'])
    assert isinstance(names, set) and isinstance(domains, set)
