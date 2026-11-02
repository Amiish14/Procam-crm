"""
The two TMS intake formats, tested against the document's own
worked examples.

Source: `docs/tms_import_formats.md`, Procam LR commit c8e6e85.
Both handover builders used to emit a CRM-shaped payload and carry
an `INVENTED` list of field names saying so. These tests exist so
that cannot come back: the field names are read out of the copied
document where it is machine-readable, and every filled example in
it is run through the builder.
"""
import json
import os
import re
import sys
import tempfile
from datetime import date

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'tms-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'TmsTest-12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'tms.db'))

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app = _main.app
db = _main.db
Company, Lead = _main.Company, _main.Lead

from app.services import (ff_handover, job_handover,       # noqa: E402
                          tms_intake)

DOC = os.path.join(_ROOT, 'docs', 'tms_import_formats.md')


def _doc():
    with open(DOC) as fh:
        return fh.read()


def _json_examples():
    """Every fenced JSON block in Part A."""
    part_a = _doc().split('# Part B', 1)[0]
    return [json.loads(m) for m in
            re.findall(r'```json\n(.*?)\n```', part_a, re.S)]


# ── the document is the source, not this file ────────────────────────
def test_the_document_is_in_the_repo():
    """Pinned here so a change to the format shows up as a diff
    rather than as a surprise at the far end."""
    assert os.path.exists(DOC)
    assert 'Part A — the FF Handover Pack' in _doc()


def test_every_field_name_we_send_is_one_the_reader_knows():
    """A name the reader does not know is dropped without a word.
    The field tables in the document are the list."""
    doc = _doc()
    for field in tms_intake.PART_A_FIELDS:
        assert f'`{field}`' in doc, \
            f'{field} is not in the document'


def test_the_six_gst_treatments_are_the_documents_six():
    doc = _doc()
    for value in tms_intake.GST_TREATMENTS:
        assert f'`{value}`' in doc
    assert len(tms_intake.GST_TREATMENTS) == 6


def test_the_four_required_fields_are_the_documented_four():
    assert tms_intake.PART_A_REQUIRED == (
        'reference', 'customer', 'mode', 'direction')


def test_neither_module_carries_an_invented_list_any_more():
    assert not hasattr(ff_handover, 'INVENTED')
    assert not hasattr(job_handover, 'INVENTED')


# ── Part A, example by example ───────────────────────────────────────
@pytest.fixture
def ff_world():
    from app.models.quote import Quote, QuoteLine
    from app.models.rfq import RFQ

    with flask_app.app_context():
        def purge():
            from app.models.ff import FfHandoverPack

            ids = [q.id for q in Quote.query.filter(
                Quote.subject.like('TMS~%')).all()]
            if ids:
                # Packs first: they hold a foreign key to the quote,
                # and leaving one behind keeps its quote alive, which
                # the next test then finds instead of its own.
                FfHandoverPack.query.filter(
                    FfHandoverPack.quote_id.in_(ids)).delete(
                        synchronize_session=False)
                QuoteLine.query.filter(
                    QuoteLine.quote_id.in_(ids)).delete(
                        synchronize_session=False)
                Quote.query.filter(Quote.id.in_(ids)).delete(
                    synchronize_session=False)
            RFQ.query.filter(RFQ.subject.like('TMS~%')).delete(
                synchronize_session=False)
            Lead.query.filter(Lead.company.like('TMS~%')).delete(
                synchronize_session=False)
            Company.query.filter(Company.name.like('TMS~%')).delete(
                synchronize_session=False)
            db.session.commit()

        purge()
        yield purge
        purge()


def _build_ff(example):
    """A CRM quote carrying what this example's shipment holds."""
    from app.models.quote import Quote, QuoteLine
    from app.models.rfq import RFQ

    acct = Company(name='TMS~' + example['customer'], is_active=True)
    db.session.add(acct)
    db.session.flush()
    lead = Lead(company=acct.name, company_id=acct.id, stage='Won',
                procam_vertical='Freight Forwarding',
                mode=example['mode'].title(), crn=example['reference'])
    db.session.add(lead)
    db.session.flush()
    rfq = RFQ(rfq_number='R-' + example['reference'],
              subject='TMS~' + example['reference'],
              lead_id=lead.id, account_id=acct.id,
              origin=example['pol_name'],
              destination=example['pod_name'],
              cargo=example.get('hs_code', ''))
    # The enquiry now has a column for each of these, which is the
    # point of the change: the pack reads the shipment instead of
    # leaving it blank for somebody at the far end.
    for field in ('direction', 'shipper_name', 'consignee_name',
                  'pol_code', 'pol_name', 'pod_code', 'pod_name',
                  'hs_code', 'package_count', 'package_kind',
                  'gross_weight_kg', 'volume_cbm',
                  'container_count'):
        if field in example:
            setattr(rfq, field, example[field])
    db.session.add(rfq)
    db.session.flush()
    q = Quote(quote_number='Q-' + example['reference'],
              subject='TMS~' + example['reference'],
              lead_id=lead.id, account_id=acct.id, rfq_id=rfq.id,
              crn=example['reference'], incoterm=example['incoterm'],
              incoterm_place=example.get('incoterm_place'),
              currency=example.get('currency', 'INR'),
              total_amount=example['sell_value'],
              quote_date=date.today())
    db.session.add(q)
    db.session.flush()
    # The carrier on a pack is whoever won the freight leg, so the
    # example's carrier has to have been chosen for it to appear.
    from app.models.ff import RateRequest
    from app.models.rfq import RateSourcingLine

    leg = RateSourcingLine(rfq_id=rfq.id, line_no=1,
                           service='Freight',
                           origin=example['pol_name'],
                           destination=example['pod_name'])
    db.session.add(leg)
    db.session.flush()
    req = RateRequest(rfq_id=rfq.id, line_id=leg.id,
                      vendor_name=example['carrier_name'],
                      status='Chosen', requested_by='T')
    db.session.add(req)
    db.session.flush()
    db.session.add(QuoteLine(quote_id=q.id, line_no=1,
                             service='Freight', charge_group='freight',
                             line_total=example['sell_value'],
                             source_rate_request_id=req.id))
    db.session.commit()
    return q, lead, acct, rfq


@pytest.mark.parametrize('example', _json_examples(),
                         ids=lambda e: f"{e['mode']}_{e['direction']}")
def test_each_part_a_example_round_trips(example, ff_world):
    """Everything in the example that the CRM holds must come back
    out of the builder, spelled the way the reader expects."""
    with flask_app.app_context():
        q, lead, acct, rfq = _build_ff(example)
        payload = ff_handover.build_payload(q)

        assert payload['reference'] == example['reference']
        assert payload['customer'] == acct.name
        assert payload['mode'] == example['mode']
        assert payload['direction'] == example['direction'], \
            f'{rfq.origin} -> {rfq.destination} read as ' \
            f'{payload.get("direction")}'
        assert payload['incoterm'] == example['incoterm']
        assert payload['pol_name'] == example['pol_name']
        assert payload['pod_name'] == example['pod_name']
        assert payload['sell_value'] == float(example['sell_value'])
        assert payload['currency'] == example['currency']

        # Every value in the document's example that the CRM now
        # holds must come back out, spelled its way. This is the
        # test the format was copied in for.
        for field, expected in example.items():
            if field in ('customer', 'sell_value'):
                continue            # checked above, or renamed
            got = payload.get(field)
            if isinstance(expected, (int, float)) and got is not None:
                assert float(got) == float(expected), field
            else:
                assert got == expected, \
                    f'{field}: sent {got!r}, example says {expected!r}'

        assert not tms_intake.unknown_fields(payload)
        assert not tms_intake.missing_required(payload)
        db.session.rollback()


def test_every_example_direction_is_derived_from_its_route():
    """The CRM does not record import or export, so it is read off
    the two free-text ends. Each documented example must come out
    the way the document says."""
    for e in _json_examples():
        assert tms_intake.direction_for(
            e['pol_name'], e['pod_name']) == e['direction'], \
            f"{e['reference']}: {e['pol_name']} -> {e['pod_name']}"


@pytest.mark.parametrize('origin,destination', [
    ('Chennai', ''), ('', 'Hamburg'), (None, None),
    ('Chennai', 'Mumbai'),
])
def test_a_direction_that_cannot_be_told_is_left_out(origin,
                                                     destination):
    """Absent is a required field missing, which the TMS refuses
    having created nothing. A guess is a job pointing the wrong way
    through customs."""
    assert tms_intake.direction_for(origin, destination) is None


def test_multimodal_has_no_tms_mode():
    assert tms_intake.mode_for('Sea') == 'SEA'
    assert tms_intake.mode_for('Air') == 'AIR'
    assert tms_intake.mode_for('Multimodal') is None


def test_a_pack_that_cannot_import_says_why_before_it_is_sent():
    problems = ff_handover.payload_problems(
        {'reference': 'X', 'customer': 'Y'})
    joined = ' '.join(problems)
    assert 'mode' in joined and 'direction' in joined
    assert 'EXPORT' in joined, 'it should say what to put there'


def test_is_dangerous_is_never_written_unless_true():
    """bool(value) at the far end: "No" means yes. The only safe
    way to say no is to leave the field out."""
    assert 'is_dangerous' not in tms_intake.prune(
        {'is_dangerous': False})
    assert 'is_dangerous' not in tms_intake.prune(
        {'is_dangerous': ''})
    assert tms_intake.prune({'is_dangerous': True})['is_dangerous']


def test_the_workbook_answers_each_field_on_exactly_one_sheet(
        ff_world, tmp_path):
    """The reader walks every sheet and the last one wins, so a
    second sheet naming a field in column A would override the
    first — and a blank value there would wipe it."""
    from openpyxl import load_workbook

    example = _json_examples()[0]
    with flask_app.app_context():
        q, lead, acct, rfq = _build_ff(example)
        ff_handover.capture_confirmation(
            q, email='buyer@example.com', subject='go ahead')
        db.session.commit()
        _p, payload, path = ff_handover.build_pack(
            q, out_dir=str(tmp_path))
        db.session.commit()

    wb = load_workbook(path)
    seen = {}
    for name in wb.sheetnames:
        for row in wb[name].iter_rows(values_only=True):
            key = row[0] if row else None
            if isinstance(key, str) and key in tms_intake.PART_A_FIELDS:
                seen.setdefault(key, []).append(name)
    for field, sheets in seen.items():
        assert len(sheets) == 1, \
            f'{field} is answered on {sheets} — the last would win'
    assert seen, 'no field names reached column A at all'
    # And the values are the ones we built.
    ws = wb['Shipment']
    values = {r[0]: r[1] for r in ws.iter_rows(values_only=True)}
    assert values['reference'] == example['reference']
    assert values['mode'] == example['mode']


# ── Part B ───────────────────────────────────────────────────────────
def _part_b_examples():
    """(vertical, gst_treatment) from the document's examples."""
    part_b = _doc().split('# Part B', 1)[1]
    out = []
    for block in re.findall(r'```\n(.*?)\n```', part_b, re.S):
        vert = re.search(r'vertical (\S+)', block)
        gst = re.search(r'gst_treatment (\w+)', block)
        if vert and gst:
            out.append((vert.group(1), gst.group(1)))
    return out


def test_the_documents_part_b_examples_were_found():
    found = _part_b_examples()
    assert len(found) >= 5, found


@pytest.mark.parametrize('tms_vertical,expected', _part_b_examples(),
                         ids=lambda v: str(v))
def test_each_part_b_example_gets_its_documented_gst_treatment(
        tms_vertical, expected):
    """PTM-H and BLADE are TRANSPORT_FCM; PFM, INSTALLATION and
    WAREHOUSE are GOODS_18. PTM-M is TRANSPORT_RCM in the document
    because that client pays the tax direct — an arrangement the
    CRM does not record, so it is the one that must be overridden
    by hand."""
    ours = {v: k for k, v in job_handover.VERTICAL_CODE.items()}
    crm_vertical = ours.get(tms_vertical)
    if crm_vertical is None:
        pytest.skip(f'{tms_vertical} is not a CRM vertical')
    got = tms_intake.gst_treatment_for(crm_vertical)
    if expected == 'TRANSPORT_RCM':
        assert got == 'TRANSPORT_FCM'
        assert tms_intake.gst_treatment_for(
            crm_vertical, override='TRANSPORT_RCM') == 'TRANSPORT_RCM'
    else:
        assert got == expected


def test_an_unknown_gst_treatment_is_refused():
    with pytest.raises(tms_intake.IntakeError):
        tms_intake.gst_treatment_for('Heavy Transport',
                                     override='VAT_20')


@pytest.mark.parametrize('crm,tms', [
    ('Heavy Transport', 'PTM-H'),
    ('General Transport', 'PTM-M'),
    ('Project Freight', 'PFM'),
    ('Installation', 'INSTALLATION'),
    ('Warehousing', 'WAREHOUSE'),
])
def test_the_vertical_is_spelled_the_way_their_examples_spell_it(
        crm, tms):
    assert job_handover.vertical_code_for(crm) == tms


def test_part_b_never_sends_a_number_the_tms_issues(ff_world,
                                                    tmp_path):
    """The project code and job number are typed by a person and
    the order number is assigned by their series. One minted here
    would be an identifier in somebody else's sequence."""
    from app.models.quote import Quote, QuoteLine

    with flask_app.app_context():
        acct = Company(name='TMS~Haulage Co', is_active=True)
        db.session.add(acct)
        db.session.flush()
        lead = Lead(company=acct.name, company_id=acct.id,
                    stage='Won', procam_vertical='Heavy Transport')
        db.session.add(lead)
        db.session.flush()
        q = Quote(quote_number='Q-HT-1', subject='TMS~ht',
                  lead_id=lead.id, account_id=acct.id,
                  currency='INR', total_amount=4850000,
                  quote_date=date.today())
        db.session.add(q)
        db.session.flush()
        db.session.add(QuoteLine(quote_id=q.id, line_no=1,
                                 service='Trailer',
                                 charge_group='line_haul',
                                 line_total=4850000))
        db.session.commit()

        payload = job_handover.build_payload(q)
        blocks = (payload['project'], payload['job'],
                  payload['client_order'])
        for block in blocks:
            for key in tms_intake.THEIRS_TO_NUMBER:
                assert key not in block, \
                    f'{key} is theirs to issue'

        assert payload['project']['vertical'] == 'PTM-H'
        assert payload['project']['client_name'] == acct.name
        assert payload['project']['linked_lead_number'] == str(lead.id)
        assert payload['job']['job_type'] == 'Road'
        assert payload['client_order']['gst_treatment'] == \
            'TRANSPORT_FCM'
        assert payload['client_order']['po_value'] == 4850000.0
        # `po_currency`, the TMS ClientPO column. It was
        # `currency`, which the far end has no place for, so the
        # value was dropped and the order read INR by fallback —
        # right by accident for INR and wrong for every other.
        assert payload['client_order']['po_currency'] == 'INR'
        assert 'currency' not in payload['client_order']
        db.session.rollback()


def test_the_customers_own_po_number_is_the_one_thing_we_send(
        ff_world, tmp_path):
    from app.models.ff import FfHandoverPack
    from app.models.quote import Quote

    with flask_app.app_context():
        acct = Company(name='TMS~PO Co', is_active=True)
        db.session.add(acct)
        db.session.flush()
        lead = Lead(company=acct.name, company_id=acct.id,
                    stage='Won', procam_vertical='Installation')
        db.session.add(lead)
        db.session.flush()
        q = Quote(quote_number='Q-PO-1', subject='TMS~po',
                  lead_id=lead.id, account_id=acct.id,
                  currency='INR', total_amount=100000,
                  quote_date=date.today())
        db.session.add(q)
        db.session.flush()
        db.session.add(FfHandoverPack(quote_id=q.id,
                                      client_po_number='PO-99881'))
        db.session.commit()

        payload = job_handover.build_payload(q)
        assert payload['client_order']['client_po_number'] == \
            'PO-99881'
        assert payload['client_order']['gst_treatment'] == 'GOODS_18'
        # And on a forwarding pack the same number is tally_po_number.
        ff = ff_handover.build_payload(q)
        assert ff['tally_po_number'] == 'PO-99881'
        db.session.rollback()


# ── the shipment on the enquiry ──────────────────────────────────────
from app.services import ff_shipment                      # noqa: E402


def _full():
    return {'direction': 'EXPORT', 'pol_name': 'Chennai',
            'pod_name': 'Hamburg', 'gross_weight_kg': '18400',
            'package_count': '12'}


def test_the_five_required_fields_are_required():
    assert ff_shipment.validate(_full()) == []
    for field in ('direction', 'pol_name', 'pod_name',
                  'gross_weight_kg', 'package_count'):
        data = _full()
        data[field] = ''
        assert ff_shipment.validate(data), f'{field} was not required'


def test_a_port_may_be_a_code_or_a_name():
    """Requiring both would stop work on the first enquiry whose
    agent gave a port name and no LOCODE, which is most of them."""
    data = _full()
    data['pol_name'] = ''
    data['pol_code'] = 'INMAA'
    assert ff_shipment.validate(data) == []


def test_every_problem_is_reported_at_once():
    """A form that reveals one problem per save is a form somebody
    saves six times."""
    problems = ff_shipment.validate({})
    assert len(problems) >= 5


def test_dangerous_goods_need_a_class_and_a_un_number():
    data = _full()
    data['is_dangerous'] = 'yes'
    fields = {f for f, _m in ff_shipment.validate(data)}
    assert fields == {'dg_class', 'un_number'}
    data.update({'dg_class': '3', 'un_number': 'UN1263'})
    assert ff_shipment.validate(data) == []


def test_not_dangerous_is_not_the_same_as_not_asked():
    """Null is "nobody has said"; False is a person saying no. Only
    a True ever reaches the pack, where any value means dangerous."""
    assert ff_shipment._clean('is_dangerous', None) is None
    assert ff_shipment._clean('is_dangerous', 'no') is False
    assert ff_shipment._clean('is_dangerous', 'yes') is True


@pytest.mark.parametrize('field,bad', [
    ('package_count', 'twelve'), ('gross_weight_kg', 'heavy'),
    ('package_count', '-3'), ('gross_weight_kg', '-1'),
])
def test_a_number_that_is_not_one_is_refused(field, bad):
    with pytest.raises(ff_shipment.ShipmentError):
        ff_shipment._clean(field, bad)


def test_the_direction_is_only_ever_a_suggestion():
    """It pre-fills. It is never stored on the strength of the
    guess: direction decides which customs regime a job goes
    through."""
    class _R:
        origin, destination = 'Chennai', 'Hamburg'
        pol_name = pod_name = None
    assert ff_shipment.suggest(_R())['direction'] == 'EXPORT'
    # And nothing is written by suggesting.
    with pytest.raises(ff_shipment.ShipmentError):
        ff_shipment.apply_to(_R(), {'pol_name': 'Chennai',
                                    'pod_name': 'Hamburg',
                                    'gross_weight_kg': 1,
                                    'package_count': 1})


def test_an_enquiry_is_not_ready_for_rates_until_it_is_complete(
        ff_world):
    from app.models.rfq import RFQ

    with flask_app.app_context():
        rfq = RFQ(rfq_number='R-ready', subject='TMS~ready',
                  origin='Chennai', destination='Hamburg')
        db.session.add(rfq)
        db.session.flush()
        assert not ff_shipment.ready_for_rates(rfq)
        ff_shipment.apply_to(rfq, _full(), actor='T')
        db.session.flush()
        assert ff_shipment.ready_for_rates(rfq)
        assert rfq.direction == 'EXPORT'
        assert float(rfq.gross_weight_kg) == 18400
        db.session.rollback()


# ── GST: quote, then account, then vertical ──────────────────────────
def test_the_account_default_beats_the_vertical():
    class _A:
        default_gst_treatment = 'TRANSPORT_RCM'
    assert tms_intake.gst_treatment_for(
        'Heavy Transport') == 'TRANSPORT_FCM'
    assert tms_intake.gst_treatment_for(
        'Heavy Transport', account=_A()) == 'TRANSPORT_RCM'


def test_the_quote_beats_the_account():
    class _A:
        default_gst_treatment = 'TRANSPORT_RCM'
    assert tms_intake.gst_treatment_for(
        'Heavy Transport', override='GOODS_18',
        account=_A()) == 'GOODS_18'


def test_rcm_is_still_never_reached_by_a_vertical_alone():
    """It means the client pays the tax direct — an arrangement
    with them, not a property of the work."""
    for vertical in tms_intake.BY_VERTICAL:
        assert tms_intake.gst_treatment_for(vertical) != \
            'TRANSPORT_RCM'


def test_an_empty_account_default_falls_through():
    class _A:
        default_gst_treatment = None
    assert tms_intake.gst_treatment_for(
        'Installation', account=_A()) == 'GOODS_18'


def test_a_rubbish_account_default_is_refused_not_sent():
    class _A:
        default_gst_treatment = 'MADE_UP'
    with pytest.raises(tms_intake.IntakeError):
        tms_intake.gst_treatment_for('Installation', account=_A())


# ── what is left for a person ────────────────────────────────────────
def test_cannot_supply_is_down_to_what_is_genuinely_unknown():
    assert set(tms_intake.CANNOT_SUPPLY) == {
        'etd', 'eta', 'notify_name', 'notify_address',
        'overseas_agent_name', 'customer_code'}
    for field in ('shipper_name', 'pol_code', 'hs_code',
                  'gross_weight_kg', 'is_dangerous', 'cha_mode',
                  'carrier_name', 'incoterm_place'):
        assert field not in tms_intake.CANNOT_SUPPLY


@pytest.mark.parametrize('direction,pol,pod,expected', [
    ('EXPORT', 'Chennai', 'Hamburg', 'OWN'),
    ('EXPORT', 'Kolkata', 'Hamburg', 'OUTSOURCED'),
    ('IMPORT', 'Shanghai', 'Nhava Sheva', 'OUTSOURCED'),
    ('IMPORT', 'Singapore', 'Chennai', 'OWN'),
    ('CROSS_TRADE', 'Shanghai', 'Jebel Ali', 'OUTSOURCED'),
    ('', 'Chennai', 'Hamburg', None),
])
def test_the_cha_mode_follows_the_end_that_clears_customs(
        direction, pol, pod, expected):
    """Getting this backwards decides whether we are selling our
    own licence or buying somebody else's."""
    class _R:
        pass
    r = _R()
    r.direction, r.pol_name, r.pod_name = direction, pol, pod
    r.origin = r.destination = None
    assert ff_handover._cha_mode(r) == expected


# ── PROCAM-DECISIONS-02NOV Module 1: the handover's real fields ─────
def test_the_cargo_dimensions_are_split_into_the_tms_columns():
    """The TMS keeps three sides and a unit; a person types one
    string. CRM-COCKPIT-02 §5 gave the lead that string and nothing
    carried it, so the job was typed from the enquiry mail twice."""
    assert tms_intake.split_dimensions('7.2 x 3.1 x 3.4 m') == \
        (7.2, 3.1, 3.4, 'm')
    assert tms_intake.split_dimensions('12.0 X 3.5 X 4.2') == \
        (12.0, 3.5, 4.2, 'm')
    assert tms_intake.split_dimensions('900 x 300 x 250 cm') == \
        (900.0, 300.0, 250.0, 'cm')
    assert tms_intake.split_dimensions('7.2 × 3.1 × 3.4 mtrs') == \
        (7.2, 3.1, 3.4, 'm')


def test_a_dimension_it_cannot_read_is_refused_not_guessed():
    """An ODC job is priced on its dimensions. A partial read is
    worse than a blank: a blank is obviously missing, and 7.2 in the
    wrong unit is not."""
    for text in ('about 7 metres long', '7.2 x 3.1', '',
                 '7.2 x 3.1 x 3.4 yards', '0 x 3 x 4 m',
                 'L 7.2 W 3.1 H 3.4', None):
        assert tms_intake.split_dimensions(text) is None, text


def test_the_dimension_unit_fits_the_tms_column():
    """`cargo_dimension_unit` is VARCHAR(4) at the far end."""
    for unit in tms_intake.DIMENSION_UNITS:
        assert len(unit) <= 4


def test_a_lookup_key_is_not_a_field():
    """`client_name` finds the client whose id the TMS stores. It is
    in the pack on purpose, and counting it as an invented field
    would make the fix look like deleting the column that makes the
    pack usable."""
    assert 'client_name' in tms_intake.LOOKUP_KEYS
    assert 'client_name' in tms_intake.PART_B_PROJECT
    assert 'client_name' in tms_intake.PART_B_ORDER


def test_the_order_pack_names_the_tms_currency_column():
    assert 'po_currency' in tms_intake.PART_B_ORDER
    assert 'currency' not in tms_intake.PART_B_ORDER


def test_nothing_the_tms_decides_is_in_a_pack():
    """Which Procam GST registration raises the invoice is a function
    of the billing entity and the place of supply, both settled over
    there. Naming one here would be guessing at somebody else's
    compliance."""
    packs = (tms_intake.PART_B_PROJECT, tms_intake.PART_B_JOB,
             tms_intake.PART_B_ORDER)
    for field in tms_intake.THEIRS_TO_DECIDE:
        for pack in packs:
            assert field not in pack, field
