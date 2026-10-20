"""
FF-COMMS-01 R1 — ask several carriers, compare, choose, price it.

What R1 adds that was not there: `RateSourcingLine` carried exactly
one rate and a free-text vendor name, which records the rate you
went with and nothing about the three you did not. A forwarder asks
four agents, two reply, one is cheaper but slower, and that choice
has to be explainable months later.

The Incoterm matrix is the other half. A sell line tagged with a
charge group can be asked "is this ours to bill?" — but only once
somebody has said which side of the shipment our client is on,
because the same term bills the opposite half depending on the
answer.
"""
import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'ffrates-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'FfRatesTest12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'ffrates.db'))

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Lead, Employee = _main.Lead, _main.Employee
flask_app.config['WTF_CSRF_ENABLED'] = False

from app.models.access import AccessProfile, DataScope    # noqa: E402
from app.models.ff import (CHA_OWN, CHA_PARTNER,          # noqa: E402
                           NO_RESPONSE, RateRequest, REPLIED)
from app.models.quote import Quote, QuoteLine             # noqa: E402
from app.models.rfq import RFQ, RateSourcingLine          # noqa: E402
from app.services import ff_charges, ff_rates             # noqa: E402

ADMIN = 'FFADMIN'
TAG = 'FF '


def _emp(code, name):
    e = (Employee.query.filter_by(emp_code=code).first()
         or Employee(emp_code=code))
    e.name, e.is_active, e.must_change_pw = name, True, False
    e.role, e.is_super_admin, e.session_version = 'admin', True, 0
    e.email = f'{code.lower()}@procamgroup.in'
    db.session.add(e)
    db.session.flush()
    AccessProfile.query.filter_by(emp_code=code).delete()
    db.session.add(AccessProfile(emp_code=code, data_scope=DataScope.ALL,
                                 perms=[]))
    return e


@pytest.fixture()
def world():
    with flask_app.app_context():
        db.create_all()
        RateRequest.query.delete()
        QuoteLine.query.filter(QuoteLine.service.like(TAG + '%')).delete(
            synchronize_session=False)
        RateSourcingLine.query.filter(
            RateSourcingLine.service.like(TAG + '%')).delete(
                synchronize_session=False)
        RFQ.query.filter(RFQ.subject.like(TAG + '%')).delete(
            synchronize_session=False)
        Quote.query.filter(Quote.subject.like(TAG + '%')).delete(
            synchronize_session=False)
        _emp(ADMIN, 'FF Admin')
        db.session.flush()

        rfq = RFQ(rfq_number=f'FFR-{datetime.utcnow():%H%M%S%f}',
                  subject=TAG + 'Chennai to Hamburg', status='Received')
        db.session.add(rfq)
        db.session.flush()
        ocean = RateSourcingLine(rfq_id=rfq.id, line_no=1,
                                 service=TAG + 'Ocean freight',
                                 charge_group='freight',
                                 origin='Chennai', destination='Hamburg')
        customs = RateSourcingLine(rfq_id=rfq.id, line_no=2,
                                   service=TAG + 'Export customs',
                                   charge_group='export_customs',
                                   origin='Chennai')
        db.session.add_all([ocean, customs])
        quote = Quote(quote_number=f'FFQ-{datetime.utcnow():%H%M%S%f}',
                      subject=TAG + 'Chennai to Hamburg', rfq_id=rfq.id,
                      status='Draft', incoterm='FOB',
                      client_party='Shipper')
        db.session.add(quote)
        db.session.commit()
        yield {'rfq': rfq.id, 'ocean': ocean.id, 'customs': customs.id,
               'quote': quote.id}


def _line(world_, key='ocean'):
    return db.session.get(RateSourcingLine, world_[key])


# ── the Incoterm matrix ──────────────────────────────────────────────
def test_every_incoterm_2020_is_known():
    assert set(ff_charges.INCOTERMS) == {
        'EXW', 'FCA', 'FAS', 'FOB', 'CFR', 'CIF', 'CPT', 'CIP',
        'DAP', 'DPU', 'DDP'}


def test_exw_puts_everything_on_the_buyer():
    assert ff_charges.billable_groups('EXW', 'Shipper') == set()
    buyer = ff_charges.billable_groups('EXW', 'Consignee')
    assert 'freight' in buyer and 'origin' in buyer and 'duties' in buyer


def test_ddp_puts_everything_on_the_seller():
    seller = ff_charges.billable_groups('DDP', 'Shipper')
    for g in ('origin', 'freight', 'destination', 'duties',
              'import_customs'):
        assert g in seller, g


def test_fob_splits_at_the_rail():
    seller = ff_charges.billable_groups('FOB', 'Shipper')
    assert seller == {'origin', 'export_customs'}
    assert not ff_charges.is_billable('freight', 'FOB', 'Shipper')
    assert ff_charges.is_billable('freight', 'FOB', 'Consignee')


def test_cif_adds_freight_and_insurance_to_fob():
    fob = ff_charges.billable_groups('FOB', 'Shipper')
    cif = ff_charges.billable_groups('CIF', 'Shipper')
    assert cif - fob == {'freight', 'insurance'}


def test_duties_sit_with_the_buyer_everywhere_but_ddp():
    for term in ff_charges.INCOTERMS:
        seller_pays_duty = 'duties' in ff_charges.SELLER_BEARS[term]
        assert seller_pays_duty == (term == 'DDP'), term


def test_the_same_term_bills_the_opposite_half_by_party():
    """The trap this guards: an Incoterm allocates cost between
    shipper and consignee, not between us and our client."""
    a = ff_charges.billable_groups('CIF', 'Shipper')
    b = ff_charges.billable_groups('CIF', 'Consignee')
    assert a & b == set() or a != b
    assert 'freight' in a and 'freight' not in b


def test_an_unknown_term_or_party_is_refused_not_guessed():
    with pytest.raises(ff_charges.ChargeError):
        ff_charges.billable_groups('FOBB', 'Shipper')
    with pytest.raises(ff_charges.ChargeError):
        ff_charges.billable_groups('FOB', 'Somebody')


def test_other_is_always_billable():
    """It exists for charges no term anticipated. Dropping them
    because the matrix has no row is how a quote loses money."""
    for term in ff_charges.INCOTERMS:
        for party in ff_charges.PARTIES:
            assert ff_charges.is_billable('other', term, party)


# ── the CHA rule ─────────────────────────────────────────────────────
def test_own_cha_is_chennai_and_mumbai_only():
    assert ff_rates.cha_mode_for('Chennai Port') == CHA_OWN
    assert ff_rates.cha_mode_for('Nhava Sheva, Mumbai') == CHA_OWN
    assert ff_rates.cha_mode_for('Kolkata') == CHA_PARTNER
    assert ff_rates.cha_mode_for('') == CHA_PARTNER


def test_claiming_own_cha_where_we_have_none_is_refused():
    with pytest.raises(ff_rates.RateError):
        ff_rates.check_cha('Kolkata', CHA_OWN)
    assert ff_rates.check_cha('Kolkata', CHA_PARTNER) == CHA_PARTNER
    assert ff_rates.check_cha('Chennai', CHA_OWN) == CHA_OWN


# ── asking ───────────────────────────────────────────────────────────
def test_asking_several_agents_makes_one_request_each(world):
    with flask_app.app_context():
        made, skipped = ff_rates.request_rates(
            _line(world), [{'name': 'Maersk'}, {'name': 'MSC'},
                           {'name': 'Hapag'}], actor=ADMIN)
        db.session.commit()
    assert len(made) == 3 and skipped == []


def test_the_same_agent_is_not_asked_twice(world):
    with flask_app.app_context():
        ff_rates.request_rates(_line(world), [{'name': 'Maersk'}],
                               actor=ADMIN)
        db.session.commit()
        made, skipped = ff_rates.request_rates(
            _line(world), [{'name': 'maersk'}, {'name': 'MSC'}],
            actor=ADMIN)
        db.session.commit()
        # Read inside the context: the objects detach when it closes.
        names = [r.vendor_name for r in made]
    assert names == ['MSC']
    assert skipped == ['maersk']


def test_asking_nobody_is_refused(world):
    with flask_app.app_context():
        with pytest.raises(ff_rates.RateError):
            ff_rates.request_rates(_line(world), [], actor=ADMIN)


# ── capturing ────────────────────────────────────────────────────────
def _ask(world_, names, key='ocean'):
    made, _ = ff_rates.request_rates(
        _line(world_, key), [{'name': n} for n in names], actor=ADMIN)
    db.session.commit()
    return made


def test_a_reply_is_stored_with_its_rupee_value(world):
    with flask_app.app_context():
        r = _ask(world, ['Maersk'])[0]
        ff_rates.record_reply(r, amount='125000', currency='INR',
                              transit_days=24, actor=ADMIN)
        db.session.commit()
        assert r.status == REPLIED
        assert float(r.amount_inr) == 125000.0
        assert r.replied_at is not None


def test_a_rate_of_zero_is_not_a_rate(world):
    with flask_app.app_context():
        r = _ask(world, ['Maersk'])[0]
        with pytest.raises(ff_rates.RateError):
            ff_rates.record_reply(r, amount='0', actor=ADMIN)


def test_silence_and_not_yet_asked_are_different(world):
    """`No Response` is set by a person. Inferring it from an empty
    column would make the two indistinguishable."""
    with flask_app.app_context():
        r = _ask(world, ['Quiet Agent'])[0]
        assert r.status == 'Requested'
        ff_rates.mark(r, NO_RESPONSE)
        db.session.commit()
        assert r.status == NO_RESPONSE


# ── comparing ────────────────────────────────────────────────────────
def test_replies_are_ranked_cheapest_first(world):
    with flask_app.app_context():
        a, b, c = _ask(world, ['Maersk', 'MSC', 'Hapag'])
        ff_rates.record_reply(a, amount='130000', transit_days=24,
                              actor=ADMIN)
        ff_rates.record_reply(b, amount='118000', transit_days=31,
                              actor=ADMIN)
        ff_rates.record_reply(c, amount='125000', transit_days=22,
                              actor=ADMIN)
        db.session.commit()
        out = ff_rates.compare(_line(world))
    names = [r['request'].vendor_name for r in out['ranked']]
    assert names == ['MSC', 'Hapag', 'Maersk']
    assert out['ranked'][0]['is_cheapest']
    assert out['replied'] == 3 and out['waiting'] == 0


def test_the_cheapest_is_not_always_the_fastest(world):
    with flask_app.app_context():
        a, b = _ask(world, ['Slow Cheap', 'Quick Dear'])
        ff_rates.record_reply(a, amount='100000', transit_days=40,
                              actor=ADMIN)
        ff_rates.record_reply(b, amount='150000', transit_days=18,
                              actor=ADMIN)
        db.session.commit()
        out = ff_rates.compare(_line(world))
    cheapest = [r for r in out['ranked'] if r['is_cheapest']][0]
    fastest = [r for r in out['ranked'] if r['is_fastest']][0]
    assert cheapest['request'].vendor_name == 'Slow Cheap'
    assert fastest['request'].vendor_name == 'Quick Dear'


def test_an_expired_rate_is_flagged_not_hidden(world):
    with flask_app.app_context():
        r = _ask(world, ['Lapsed Lines'])[0]
        ff_rates.record_reply(
            r, amount='99000',
            validity_until=date.today() - timedelta(days=2), actor=ADMIN)
        db.session.commit()
        out = ff_rates.compare(_line(world))
    flags = out['ranked'][0]['flags']
    assert any('expired' in f for f in flags), flags


def test_a_missing_transit_time_is_flagged(world):
    with flask_app.app_context():
        r = _ask(world, ['No Transit'])[0]
        ff_rates.record_reply(r, amount='99000', actor=ADMIN)
        db.session.commit()
        out = ff_rates.compare(_line(world))
    assert any('transit' in f for f in out['ranked'][0]['flags'])


def test_waiting_vendors_are_counted_not_forgotten(world):
    with flask_app.app_context():
        a, b = _ask(world, ['Replied Co', 'Silent Co'])
        ff_rates.record_reply(a, amount='100000', actor=ADMIN)
        db.session.commit()
        out = ff_rates.compare(_line(world))
    assert out['replied'] == 1 and out['waiting'] == 1


# ── choosing ─────────────────────────────────────────────────────────
def test_choosing_the_cheapest_needs_no_explanation(world):
    with flask_app.app_context():
        a, b = _ask(world, ['Cheap', 'Dear'])
        ff_rates.record_reply(a, amount='100000', actor=ADMIN)
        ff_rates.record_reply(b, amount='200000', actor=ADMIN)
        db.session.commit()
        ff_rates.choose(a, actor=ADMIN)
        db.session.commit()
        assert a.chosen is True


def test_choosing_a_dearer_one_must_say_why(world):
    with flask_app.app_context():
        a, b = _ask(world, ['Cheap', 'Dear'])
        ff_rates.record_reply(a, amount='100000', actor=ADMIN)
        ff_rates.record_reply(b, amount='200000', actor=ADMIN)
        db.session.commit()
        with pytest.raises(ff_rates.RateError):
            ff_rates.choose(b, actor=ADMIN)
        ff_rates.choose(b, actor=ADMIN,
                        reason='Only one with reefer capacity')
        db.session.commit()
        assert b.chosen and 'reefer' in b.chosen_reason


def test_choosing_again_moves_the_choice(world):
    with flask_app.app_context():
        a, b = _ask(world, ['First', 'Second'])
        ff_rates.record_reply(a, amount='100000', actor=ADMIN)
        ff_rates.record_reply(b, amount='110000', actor=ADMIN)
        db.session.commit()
        ff_rates.choose(a, actor=ADMIN)
        db.session.commit()
        ff_rates.choose(b, actor=ADMIN, reason='Better schedule')
        db.session.commit()
        db.session.refresh(a)
        assert b.chosen is True and a.chosen is False


def test_an_expired_rate_cannot_be_chosen(world):
    with flask_app.app_context():
        r = _ask(world, ['Lapsed'])[0]
        ff_rates.record_reply(
            r, amount='90000',
            validity_until=date.today() - timedelta(days=1), actor=ADMIN)
        db.session.commit()
        with pytest.raises(ff_rates.RateError) as exc:
            ff_rates.choose(r, actor=ADMIN)
        assert 'expired' in str(exc.value)


def test_a_vendor_who_never_replied_cannot_be_chosen(world):
    with flask_app.app_context():
        r = _ask(world, ['Silent'])[0]
        with pytest.raises(ff_rates.RateError):
            ff_rates.choose(r, actor=ADMIN)


# ── pricing ──────────────────────────────────────────────────────────
def _chosen_ocean(world_, cost='100000'):
    r = _ask(world_, ['Maersk'])[0]
    ff_rates.record_reply(r, amount=cost, transit_days=24, actor=ADMIN)
    db.session.commit()
    ff_rates.choose(r, actor=ADMIN)
    db.session.commit()
    return r


def test_a_sell_line_carries_the_cost_and_the_margin(world):
    with flask_app.app_context():
        _chosen_ocean(world)
        quote = db.session.get(Quote, world['quote'])
        ql = ff_rates.quote_line_from(_line(world), quote=quote,
                                      sell_amount='130000')
        db.session.commit()
        assert float(ql.cost_inr) == 100000.0
        assert float(ql.line_total) == 130000.0
        assert float(ql.margin_inr) == 30000.0
        assert round(ql.margin_pct, 2) == round(30000 / 130000 * 100, 2)
        assert ql.charge_group == 'freight'
        assert ql.source_rate_request_id is not None


def test_a_markup_prices_the_line_from_the_cost(world):
    with flask_app.app_context():
        _chosen_ocean(world, cost='100000')
        quote = db.session.get(Quote, world['quote'])
        ql = ff_rates.quote_line_from(_line(world), quote=quote,
                                      markup_pct='15')
        db.session.commit()
        assert float(ql.line_total) == 115000.0


def test_a_price_and_a_markup_together_are_refused(world):
    with flask_app.app_context():
        _chosen_ocean(world)
        quote = db.session.get(Quote, world['quote'])
        with pytest.raises(ff_rates.RateError):
            ff_rates.quote_line_from(_line(world), quote=quote,
                                     sell_amount='1', markup_pct='1')


def test_pricing_a_leg_with_no_chosen_rate_is_refused(world):
    with flask_app.app_context():
        quote = db.session.get(Quote, world['quote'])
        with pytest.raises(ff_rates.RateError) as exc:
            ff_rates.quote_line_from(_line(world), quote=quote,
                                     sell_amount='1000')
        assert 'choose' in str(exc.value).lower()


def test_an_uncosted_line_is_not_margin_of_one_hundred_percent(world):
    """The number that would make a quote look profitable while it
    sinks: a line nobody has costed is not pure profit."""
    with flask_app.app_context():
        _chosen_ocean(world)
        quote = db.session.get(Quote, world['quote'])
        ff_rates.quote_line_from(_line(world), quote=quote,
                                 sell_amount='130000')
        db.session.add(QuoteLine(quote_id=quote.id, line_no=99,
                                 service=TAG + 'Uncosted extra',
                                 quantity=1, unit_rate=50000,
                                 line_total=50000,
                                 charge_group='other'))
        db.session.commit()
        summary = ff_rates.margin_summary(quote)
    assert summary['uncosted_lines'] == 1
    assert summary['reliable'] is False
    assert 'overstated' in summary['note']


def test_the_margin_adds_up(world):
    with flask_app.app_context():
        _chosen_ocean(world, cost='100000')
        quote = db.session.get(Quote, world['quote'])
        ff_rates.quote_line_from(_line(world), quote=quote,
                                 sell_amount='130000')
        db.session.commit()
        s = ff_rates.margin_summary(quote)
    assert s['sell_inr'] == 130000.0
    assert s['cost_inr'] == 100000.0
    assert s['margin_inr'] == 30000.0
    assert s['reliable'] is True


def test_lines_the_incoterm_does_not_bill_are_kept_and_marked(world):
    """A quote that silently omits what the other party pays is how a
    scope argument starts three months later."""
    with flask_app.app_context():
        quote = db.session.get(Quote, world['quote'])   # FOB / Shipper
        freight = QuoteLine(quote_id=quote.id, line_no=1,
                            service=TAG + 'Ocean', quantity=1,
                            unit_rate=100, line_total=100,
                            charge_group='freight')
        origin = QuoteLine(quote_id=quote.id, line_no=2,
                           service=TAG + 'Origin', quantity=1,
                           unit_rate=50, line_total=50,
                           charge_group='origin')
        db.session.add_all([freight, origin])
        db.session.commit()
        billable, not_billable = ff_charges.split_lines(
            [freight, origin], quote.incoterm, quote.client_party)
    assert origin in billable          # FOB seller bears origin
    assert freight in not_billable     # the buyer bears the freight
    assert len(billable) + len(not_billable) == 2


# ── the flag ─────────────────────────────────────────────────────────
def _client(code):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code=code, name=code, role='admin', sv=0)
    return c


def test_every_route_is_404_while_the_flag_is_off(world):
    """An unbuilt feature should look unbuilt from outside — not a
    500, and not a half-working screen."""
    c = _client(ADMIN)
    for path, method in (('/api/ff/charge-groups', 'get'),
                         (f'/api/ff/lines/{world["ocean"]}/rates', 'get'),
                         (f'/api/ff/lines/{world["ocean"]}/compare', 'get'),
                         (f'/api/ff/quotes/{world["quote"]}/margin', 'get')):
        r = getattr(c, method)(path)
        assert r.status_code == 404, f'{path} answered {r.status_code}'


def test_the_routes_work_when_the_flag_is_on(world, monkeypatch):
    from app.services import flags
    monkeypatch.setattr(flags, 'on',
                        lambda name, default=None:
                        True if name == 'FEATURE_FF_CRM' else False)
    body = _client(ADMIN).get(
        '/api/ff/charge-groups?incoterm=FOB&party=Shipper').get_json()
    assert body['ok']
    assert set(body['billable']) == {'origin', 'export_customs'}
    assert 'freight' in body['not_billable']
