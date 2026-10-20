"""
FF-COMMS-01 R4 and R5 — the confirmation, the pack, the calculator.

On a forwarding job the customer rarely issues a purchase order;
they reply "go ahead" to the quotation and that email is the
instruction. So it is captured as evidence — sender, time, subject,
Message-Id — because "the client confirmed" without those is
hearsay, and hearsay is what gets argued about when a shipment goes
wrong three weeks later.

Nothing here calls the TMS. The pack format was derived from CRM
data because FF-COMMS-02 was not available, and a release that
started posting to production the moment a flag flipped is the worst
possible way to discover a mistake in a format.
"""
import json
import os
import sys
import tempfile
from datetime import datetime
from decimal import Decimal

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'ffhand-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'FfHandTest12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'ffhand.db'))

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Company, Employee, Lead = _main.Company, _main.Employee, _main.Lead
flask_app.config['WTF_CSRF_ENABLED'] = False

from app.models.access import AccessProfile, DataScope    # noqa: E402
from app.models.ff import FfHandoverPack, RateRequest     # noqa: E402
from app.models.quote import Quote, QuoteLine             # noqa: E402
from app.models.rfq import RFQ, RateSourcingLine          # noqa: E402
from app.services import ff_handover, ff_weight           # noqa: E402

ADMIN = 'FHADMIN'
TAG = 'FH '


@pytest.fixture()
def world():
    with flask_app.app_context():
        db.create_all()

        def purge():
            FfHandoverPack.query.delete()
            RateRequest.query.delete()
            QuoteLine.query.filter(
                QuoteLine.service.like(TAG + '%')).delete(
                    synchronize_session=False)
            Quote.query.filter(Quote.subject.like(TAG + '%')).delete(
                synchronize_session=False)
            RateSourcingLine.query.filter(
                RateSourcingLine.service.like(TAG + '%')).delete(
                    synchronize_session=False)
            RFQ.query.filter(RFQ.subject.like(TAG + '%')).delete(
                synchronize_session=False)
            Lead.query.filter(Lead.company.like(TAG + '%')).delete(
                synchronize_session=False)
            Company.query.filter(Company.name.like(TAG + '%')).delete(
                synchronize_session=False)
            db.session.commit()

        purge()
        e = (Employee.query.filter_by(emp_code=ADMIN).first()
             or Employee(emp_code=ADMIN))
        e.name, e.is_active, e.must_change_pw = 'FH Admin', True, False
        e.role, e.is_super_admin, e.session_version = 'admin', True, 0
        e.email = 'fhadmin@procamgroup.in'
        db.session.add(e)
        db.session.flush()
        AccessProfile.query.filter_by(emp_code=ADMIN).delete()
        db.session.add(AccessProfile(emp_code=ADMIN,
                                     data_scope=DataScope.ALL, perms=[]))

        acct = Company(name=TAG + 'Northstar Mills', is_active=True)
        db.session.add(acct)
        db.session.flush()
        lead = Lead(company=TAG + 'Northstar Mills', company_id=acct.id,
                    stage='Quoted', procam_vertical='Freight Forwarding',
                    mode='Sea', assigned_to=ADMIN, crn='PWL-C26-000009')
        db.session.add(lead)
        db.session.flush()
        rfq = RFQ(rfq_number=f'FH-{datetime.utcnow():%H%M%S%f}',
                  subject=TAG + 'Chennai to Rotterdam', lead_id=lead.id,
                  account_id=acct.id, status='Quoted',
                  origin='Chennai', destination='Rotterdam',
                  cargo='Textile machinery', crn='PWL-C26-000009',
                  vendor_crn='PWL-T26-000009')
        db.session.add(rfq)
        db.session.flush()
        quote = Quote(quote_number=f'FHQ-{datetime.utcnow():%H%M%S%f}',
                      subject=TAG + 'Chennai to Rotterdam',
                      rfq_id=rfq.id, lead_id=lead.id, account_id=acct.id,
                      status='Submitted', incoterm='FOB',
                      client_party='Shipper', crn='PWL-C26-000009')
        db.session.add(quote)
        db.session.flush()
        db.session.add_all([
            QuoteLine(quote_id=quote.id, line_no=1,
                      service=TAG + 'Ocean freight', quantity=1,
                      unit_rate=130000, line_total=130000,
                      charge_group='freight', cost_inr=100000),
            QuoteLine(quote_id=quote.id, line_no=2,
                      service=TAG + 'Origin handling', quantity=1,
                      unit_rate=20000, line_total=20000,
                      charge_group='origin', cost_inr=15000)])
        db.session.commit()
        yield {'account': acct.id, 'lead': lead.id, 'rfq': rfq.id,
               'quote': quote.id}
        purge()


def _quote(world_):
    return db.session.get(Quote, world_['quote'])


# ── R5: chargeable weight ────────────────────────────────────────────
def test_sea_is_billed_on_whichever_is_greater():
    light = ff_weight.sea_chargeable(cbm='8', weight_kg='3000')
    assert light['chargeable'] == 8.0 and light['basis'] == 'volume'
    heavy = ff_weight.sea_chargeable(cbm='2', weight_kg='9000')
    assert heavy['chargeable'] == 9.0 and heavy['basis'] == 'weight'


def test_sea_at_exactly_equal_bills_on_volume():
    out = ff_weight.sea_chargeable(cbm='5', weight_kg='5000')
    assert out['chargeable'] == 5.0


def test_air_volumetric_is_length_by_width_by_height_over_6000():
    out = ff_weight.air_chargeable(gross_kg='40', length_cm='100',
                                   width_cm='80', height_cm='60')
    # 100*80*60 = 480000 / 6000 = 80 kg
    assert out['volumetric_kg'] == 80.0
    assert out['chargeable'] == 80.0 and out['basis'] == 'volume'


def test_air_bills_the_gross_when_the_cargo_is_dense():
    out = ff_weight.air_chargeable(gross_kg='500', length_cm='50',
                                   width_cm='50', height_cm='50')
    assert out['chargeable'] == 500.0 and out['basis'] == 'weight'


def test_air_counts_every_piece():
    one = ff_weight.air_chargeable(gross_kg='10', length_cm='60',
                                   width_cm='40', height_cm='30')
    four = ff_weight.air_chargeable(gross_kg='40', length_cm='60',
                                    width_cm='40', height_cm='30',
                                    pieces=4)
    assert four['volumetric_kg'] == one['volumetric_kg'] * 4


def test_air_rounds_up_to_the_next_half_kilo():
    """IATA bills to the next half kilo. Rounding down a few hundred
    grammes on every line is a small loss taken a thousand times."""
    out = ff_weight.air_chargeable(gross_kg='10', length_cm='30',
                                   width_cm='30', height_cm='31')
    # 27900/6000 = 4.65 -> 5.0
    assert out['volumetric_kg'] == 5.0


def test_air_accepts_a_volume_instead_of_dimensions():
    out = ff_weight.air_chargeable(gross_kg='100', volume_cbm='1')
    # 1 CBM = 1,000,000 cm3 / 6000 = 166.67 -> 167.0
    assert out['volumetric_kg'] == 167.0


def test_a_different_divisor_changes_the_answer():
    six = ff_weight.air_chargeable(gross_kg='1', volume_cbm='1')
    five = ff_weight.air_chargeable(gross_kg='1', volume_cbm='1',
                                    divisor=5000)
    assert five['volumetric_kg'] > six['volumetric_kg']


def test_air_without_dimensions_or_volume_is_refused():
    with pytest.raises(ff_weight.WeightError) as exc:
        ff_weight.air_chargeable(gross_kg='100')
    assert 'cannot be worked out' in str(exc.value)


def test_multimodal_has_no_single_chargeable_weight():
    """Returning one number would invent a figure no carrier
    recognises: the sea leg is W/M and the air leg volumetric."""
    with pytest.raises(ff_weight.WeightError) as exc:
        ff_weight.chargeable('Multimodal', cbm='5', gross_kg='1000')
    assert 'per leg' in str(exc.value)


def test_negative_and_rubbish_measurements_are_refused():
    with pytest.raises(ff_weight.WeightError):
        ff_weight.sea_chargeable(cbm='-1', weight_kg='100')
    with pytest.raises(ff_weight.WeightError):
        ff_weight.sea_chargeable(cbm='eight', weight_kg='100')


def test_the_mode_dispatches_to_the_right_formula():
    sea = ff_weight.chargeable('Sea', cbm='8', gross_kg='3000')
    air = ff_weight.chargeable('Air', gross_kg='40', volume_cbm='1')
    assert sea['unit'] == 'W/M (revenue tonne)'
    assert air['unit'] == 'kg'


# ── R4: the confirmation ─────────────────────────────────────────────
def test_the_confirmation_is_captured_as_evidence(world):
    with flask_app.app_context():
        pack = ff_handover.capture_confirmation(
            _quote(world), email='buyer@northstarmills.com',
            subject='RE: Quotation PWL-C26-000009 — go ahead',
            message_id='<abc123@northstarmills.com>', actor=ADMIN)
        db.session.commit()
        assert pack.confirmed_by_email == 'buyer@northstarmills.com'
        assert pack.confirmation_message_id == '<abc123@northstarmills.com>'
        assert pack.confirmed_at is not None


def test_a_confirmation_with_no_sender_is_refused(world):
    """An instruction nobody can be shown to have given is not
    evidence of anything."""
    with flask_app.app_context():
        with pytest.raises(ff_handover.HandoverError) as exc:
            ff_handover.capture_confirmation(
                _quote(world), email='', subject='go ahead')
        assert 'who authorised' in str(exc.value)


def test_a_confirmation_on_a_dead_quote_is_refused(world):
    with flask_app.app_context():
        q = _quote(world)
        q.status = 'Superseded'
        db.session.commit()
        with pytest.raises(ff_handover.HandoverError):
            ff_handover.capture_confirmation(
                q, email='buyer@x.com', subject='go ahead')


def test_won_needs_the_confirmation_first(world):
    with flask_app.app_context():
        with pytest.raises(ff_handover.HandoverError) as exc:
            ff_handover.mark_won(_quote(world), actor=ADMIN)
        assert 'purchase order' in str(exc.value)


def test_won_after_the_confirmation(world):
    with flask_app.app_context():
        q = _quote(world)
        ff_handover.capture_confirmation(
            q, email='buyer@northstarmills.com', subject='go ahead')
        ff_handover.mark_won(q, actor=ADMIN)
        db.session.commit()
        assert q.status == 'Won' and q.won_at is not None


def test_capturing_twice_updates_rather_than_duplicates(world):
    with flask_app.app_context():
        q = _quote(world)
        ff_handover.capture_confirmation(q, email='a@x.com',
                                         subject='first')
        db.session.commit()
        ff_handover.capture_confirmation(q, email='b@x.com',
                                         subject='corrected')
        db.session.commit()
        packs = FfHandoverPack.query.filter_by(quote_id=q.id).all()
    assert len(packs) == 1
    assert packs[0].confirmed_by_email == 'b@x.com'


# ── R4: the pack ─────────────────────────────────────────────────────
def test_the_pack_cannot_be_built_without_a_confirmation(world):
    with flask_app.app_context():
        with pytest.raises(ff_handover.HandoverError):
            ff_handover.build_pack(_quote(world), actor=ADMIN)


def test_the_pack_carries_the_charges_and_the_margin(world, tmp_path):
    with flask_app.app_context():
        q = _quote(world)
        ff_handover.capture_confirmation(
            q, email='buyer@northstarmills.com', subject='go ahead',
            message_id='<m1@x>')
        db.session.commit()
        pack, payload, path = ff_handover.build_pack(
            q, actor=ADMIN, lead=db.session.get(Lead, world['lead']),
            account=db.session.get(Company, world['account']),
            rfq=db.session.get(RFQ, world['rfq']),
            out_dir=str(tmp_path))
        db.session.commit()
    # Part A is flat and carries the shipment, not the charges —
    # the TMS prices the job from its own vendor orders, and the
    # priced lines ride along on a sheet its reader ignores.
    assert payload['reference'] == 'PWL-C26-000009'
    assert payload['incoterm'] == 'FOB'
    assert payload['mode'] == 'SEA', 'the TMS takes SEA, not Sea'
    assert payload['sell_value'] == 150000.0
    assert payload['currency'] == 'INR'
    assert os.path.exists(path)


def test_the_pack_records_the_message_id(world, tmp_path):
    with flask_app.app_context():
        q = _quote(world)
        ff_handover.capture_confirmation(
            q, email='buyer@northstarmills.com', subject='go ahead',
            message_id='<proof@northstarmills.com>')
        db.session.commit()
        _p, payload, _f = ff_handover.build_pack(
            q, actor=ADMIN, out_dir=str(tmp_path))
    # The confirmation is evidence on our side; Part A has no field
    # for it, so it stays on the pack row rather than being invented
    # into the payload.
    assert 'confirmation' not in payload
    with flask_app.app_context():
        from app.models.ff import FfHandoverPack
        row = FfHandoverPack.query.filter_by(quote_id=_p.quote_id
                                             ).first()
        assert row.confirmation_message_id == \
            '<proof@northstarmills.com>'


def test_an_unreliable_margin_is_declared_in_the_pack(world, tmp_path):
    with flask_app.app_context():
        q = _quote(world)
        db.session.add(QuoteLine(quote_id=q.id, line_no=3,
                                 service=TAG + 'Uncosted', quantity=1,
                                 unit_rate=5000, line_total=5000,
                                 charge_group='other'))
        ff_handover.capture_confirmation(q, email='b@x.com',
                                         subject='go')
        db.session.commit()
        _p, payload, path = ff_handover.build_pack(
            q, actor=ADMIN, out_dir=str(tmp_path))
    # The margin is not a Part A field. It is on the charges sheet,
    # which the reader ignores and a person reads.
    assert 'totals' not in payload
    from openpyxl import load_workbook
    wb = load_workbook(path)
    assert 'Charges (not imported)' in wb.sheetnames


def test_nothing_is_sent_to_the_tms(world, tmp_path, monkeypatch):
    """The format was derived, not given. A release that posted to
    production on a flag flip is the worst way to find a mistake."""
    called = []
    from app.services import integration as integ
    monkeypatch.setattr(integ, 'notify_tms',
                        lambda *a, **k: called.append(a))
    with flask_app.app_context():
        q = _quote(world)
        ff_handover.capture_confirmation(q, email='b@x.com',
                                         subject='go')
        db.session.commit()
        ff_handover.build_pack(q, actor=ADMIN, out_dir=str(tmp_path))
        db.session.commit()
    assert called == [], 'the handover reached the TMS'


def test_the_handover_module_does_not_import_notify_tms():
    """Belt as well as braces: it cannot call what it cannot see."""
    with open(os.path.join(_ROOT, 'app', 'services',
                           'ff_handover.py')) as fh:
        src = fh.read()
    assert 'notify_tms' not in src.split('"""', 2)[-1], (
        'ff_handover can reach the TMS')


def test_nothing_is_invented_any_more(world):
    """The real format arrived. What replaces the INVENTED list is
    its opposite: fields the format defines that the CRM has no
    column for, so a person fills them rather than discovering them
    at the far end."""
    assert not hasattr(ff_handover, 'INVENTED')
    assert 'shipper_name' in ff_handover.CANNOT_SUPPLY
    assert 'pol_code' in ff_handover.CANNOT_SUPPLY
    # And nothing in CANNOT_SUPPLY is a name we made up.
    from app.services import tms_intake
    for field in ff_handover.CANNOT_SUPPLY:
        assert field in tms_intake.PART_A_FIELDS


def test_the_payload_says_where_the_format_came_from(world, tmp_path):
    with flask_app.app_context():
        q = _quote(world)
        ff_handover.capture_confirmation(q, email='b@x.com',
                                         subject='go')
        db.session.commit()
        _p, payload, _f = ff_handover.build_pack(
            q, actor=ADMIN, out_dir=str(tmp_path))
    # There is no format_note any more: every key in the payload is
    # a field the reader knows, and a note would be one it does not.
    from app.services import tms_intake
    assert not tms_intake.unknown_fields(payload)


def test_the_payload_is_stored_so_it_can_be_reviewed(world, tmp_path):
    with flask_app.app_context():
        q = _quote(world)
        ff_handover.capture_confirmation(q, email='b@x.com',
                                         subject='go')
        db.session.commit()
        pack, _pl, _f = ff_handover.build_pack(
            q, actor=ADMIN, out_dir=str(tmp_path))
        db.session.commit()
        stored = json.loads(pack.payload_json)
    assert stored['reference'] == 'PWL-C26-000009'
    assert stored['customer']


# ── the flag ─────────────────────────────────────────────────────────
def test_the_r4_and_r5_routes_are_dark(world):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code=ADMIN, name=ADMIN, role='admin', sv=0)
    assert c.post('/api/ff/chargeable-weight',
                  json={'mode': 'Sea'}).status_code == 404
    assert c.post(f'/api/ff/quotes/{world["quote"]}/handover',
                  json={}).status_code == 404


# ── R5: the page ─────────────────────────────────────────────────────
def test_the_ff_page_is_dark_while_the_flag_is_off(world):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code=ADMIN, name=ADMIN, role='admin', sv=0)
    assert c.get('/ff').status_code == 404


def test_the_ff_page_renders_when_the_flag_is_on(world, monkeypatch):
    from app.services import flags
    monkeypatch.setattr(flags, 'on',
                        lambda name, default=None:
                        name == 'FEATURE_FF_CRM')
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code=ADMIN, name=ADMIN, role='admin', sv=0)
    r = c.get('/ff')
    assert r.status_code == 200
    page = r.get_data(as_text=True)
    for expected in ('Chargeable weight', 'Rate grid', 'Quote builder',
                     'CRN thread'):
        assert expected in page, expected


def test_the_ff_page_carries_the_csrf_wrapper():
    """It is served outside the app shell, so its buttons are dead
    without it — six pages shipped that way already."""
    with open(os.path.join(_ROOT, 'templates', 'ff',
                           'home.html')) as fh:
        page = fh.read()
    assert '_csrf_fetch.html' in page


def test_the_quote_builder_warns_rather_than_hiding_a_bad_margin():
    with open(os.path.join(_ROOT, 'templates', 'ff',
                           'home.html')) as fh:
        page = fh.read()
    assert 'This margin is overstated' in page
    assert 'at or below cost' in page
    assert 'fall to the other' in page, (
        'lines the Incoterm does not bill must be shown, not dropped')
