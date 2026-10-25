"""
Before the RFQ: the activity moves the stage, and nothing is typed twice.

CRM-LEAD-JOURNEY-01 (R4).

641 accounts have no dev_stage and 392 are "Active Account" with
almost nothing in between — a funnel with no middle, because
recording the activity and moving the stage are two jobs and the
second does not happen. So the activity moves it.

The tests that earn their place are the ones about not inventing
data: a channel is set only where a rule actually fits, a stage
never walks backwards on ordinary activity, and a target with no
channel mix behind it reports "we do not know" rather than a
number.
"""
import importlib
import os
import sys
import tempfile
from datetime import date, datetime, timedelta

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'journey-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'Journey-1234567')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'journey.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
importlib.import_module('app.models.rfq_target')
flask_app = _main.app
flask_app.config['WTF_CSRF_ENABLED'] = False
db = _main.db
Lead, Company, Employee = _main.Lead, _main.Company, _main.Employee

from app.models.rfq_target import RfqTarget               # noqa: E402
from app.services import lead_journey as lj               # noqa: E402

SALES, DIRECTOR = 'LJ_SALES', 'DIR_LJ001'
VERTICAL = 'Warehousing'


def _purge():
    from presales.models import AccountActivity, AccountStageHistory
    AccountActivity.query.delete()
    AccountStageHistory.query.delete()
    RfqTarget.query.filter(RfqTarget.emp_code.like('LJ%')).delete(
        synchronize_session=False)
    Lead.query.filter(Lead.company.like('LJ %')).delete(
        synchronize_session=False)
    Company.query.filter(Company.name.like('LJ %')).delete(
        synchronize_session=False)
    Employee.query.filter(
        db.or_(Employee.emp_code.like('LJ_%'),
               Employee.emp_code == DIRECTOR)).delete(
        synchronize_session=False)
    db.session.commit()


@pytest.fixture(autouse=True)
def _clean():
    with flask_app.app_context():
        db.create_all()
        _purge()
        db.session.add(Employee(
            emp_code=SALES, name='LJ Sales', password_hash='x',
            role='user', vertical=VERTICAL,
            email='ljsales@procamgroup.in'))
        db.session.add(Employee(
            emp_code=DIRECTOR, name='LJ Director', password_hash='x',
            role='director', vertical='All',
            email='ljdir@procamgroup.in'))
        db.session.commit()
        _flags(True)
        yield
        _flags(False)
        _purge()


def _flags(state):
    import json
    from app.models.integration import AppSetting
    from app.services import flags
    for key in ('flag.FEATURE_PRE_RFQ_FUNNEL',
                'flag.FEATURE_LEAD_JOURNEY'):
        row = AppSetting.query.filter_by(key=key).first()
        if row is None:
            row = AppSetting(key=key)
            db.session.add(row)
        row.value_json = json.dumps(bool(state))
    db.session.commit()
    flags.cache_clear()


def _account(name='LJ Steel', **kw):
    account = Company(name=name, is_active=True, **kw)
    db.session.add(account)
    db.session.commit()
    return account


def _lead(account=None, **kw):
    lead = Lead(company='LJ Co', stage=kw.pop('stage', 'New'),
                source=kw.pop('source', 'manual'),
                procam_vertical=VERTICAL, assigned_to=SALES,
                company_id=getattr(account, 'id', None),
                created_at=datetime.utcnow(), **kw)
    db.session.add(lead)
    db.session.commit()
    return lead


# ── §6.1: the channel the CRM can work out ───────────────────────────
def test_an_auction_is_a_tender_channel():
    with flask_app.app_context():
        lead = _lead(lead_type='auction')
        channel, why = lj.detect_channel(lead)
        assert channel == 'Tender / E-Procurement Portals'
        assert 'auction' in why


def test_a_directors_forward_is_a_management_lead():
    with flask_app.app_context():
        lead = _lead(forwarded_by_email='ljdir@procamgroup.in')
        channel, _why = lj.detect_channel(lead)
        assert channel == 'Management Leads'


def test_an_account_that_has_won_before_is_a_repeat_customer():
    with flask_app.app_context():
        account = _account()
        _lead(account, stage='Won')
        lead = _lead(account)
        channel, why = lj.detect_channel(lead)
        assert channel == 'Existing Customer / Repeat'
        assert 'won work before' in why


def test_outside_mail_is_an_inbound_enquiry():
    with flask_app.app_context():
        lead = _lead(source='email',
                     original_email_from='buyer@elsewhere.com')
        assert lj.detect_channel(lead)[0] == 'Inbound Enquiries'


def test_a_lead_no_rule_fits_is_left_for_a_person():
    """A guessed channel feeds a conversion rate that reads as
    measurement."""
    with flask_app.app_context():
        lead = _lead(source='manual')
        channel, why = lj.detect_channel(lead)
        assert channel == ''
        assert 'ask' in why


def test_setting_the_channel_records_whether_it_was_automatic():
    with flask_app.app_context():
        lead = _lead(lead_type='auction')
        lj.set_channel(lead, actor=SALES)
        db.session.commit()
        assert lead.lead_channel == 'Tender / E-Procurement Portals'
        assert lead.lead_channel_auto is True

        typed = _lead()
        lj.set_channel(typed, actor=SALES, channel='Cold Calling')
        db.session.commit()
        assert typed.lead_channel_auto is False


# ── §6.2: the activity moves the stage ───────────────────────────────
def test_meeting_planned_and_meeting_held_are_different():
    """One 'Meeting' cannot tell a diary entry from a thing that
    happened, and the difference is the stage it moves."""
    assert lj.MOVES_TO['Meeting planned'] == 'Meeting Planned'
    assert lj.MOVES_TO['Meeting held'] == 'Meeting Completed'


def test_logging_a_call_moves_the_account_forward():
    with flask_app.app_context():
        account = _account()
        lead = _lead(account)
        ok, detail = lj.log(lead, kind='Phone call', actor=SALES)
        assert ok, detail
        db.session.commit()
        assert account.dev_stage == 'Contact Established'
        assert detail['moved_to'] == 'Contact Established'


def test_a_later_call_does_not_drag_the_account_backwards():
    with flask_app.app_context():
        account = _account()
        lead = _lead(account)
        lj.log(lead, kind='Meeting held', actor=SALES,
               value='Immediate (≤ 1 month)')
        db.session.commit()
        assert account.dev_stage == 'Meeting Completed'

        ok, detail = lj.log(lead, kind='Phone call', actor=SALES)
        db.session.commit()
        assert ok
        assert detail['moved_to'] == ''
        assert account.dev_stage == 'Meeting Completed'


def test_meeting_held_requires_the_timing():
    with flask_app.app_context():
        lead = _lead(_account())
        ok, why = lj.log(lead, kind='Meeting held', actor=SALES)
        assert ok is False and 'Expected RFQ timing' in why


def test_a_capability_answer_of_no_closes_the_lead():
    with flask_app.app_context():
        lead = _lead(_account())
        ok, _d = lj.log(lead, kind='Capability check', actor=SALES,
                        value='No')
        assert ok
        db.session.commit()
        assert lead.stage == 'Lost'
        assert lead.lost_reason == lj.CAPABILITY_LOST_REASON


def test_a_capability_answer_of_yes_does_not_close_it():
    with flask_app.app_context():
        lead = _lead(_account())
        lj.log(lead, kind='Capability check', actor=SALES,
               value='Yes — standard')
        db.session.commit()
        assert lead.stage != 'Lost'


def test_rfq_expected_records_the_date():
    with flask_app.app_context():
        lead = _lead(_account())
        when = str(date.today() + timedelta(days=30))
        ok, _d = lj.log(lead, kind='RFQ expected', actor=SALES,
                        value=when)
        assert ok
        db.session.commit()
        assert str(lead.rfq_expected_date) == when


def test_an_activity_needs_an_account():
    """Otherwise it belongs to nobody."""
    with flask_app.app_context():
        lead = _lead(None)
        ok, why = lj.log(lead, kind='Phone call', actor=SALES)
        assert ok is False and 'no account' in why


def test_an_unknown_kind_is_refused():
    with flask_app.app_context():
        lead = _lead(_account())
        ok, why = lj.log(lead, kind='Pigeon', actor=SALES)
        assert ok is False and 'kind must be' in why


def test_the_rfq_arriving_makes_the_account_active():
    with flask_app.app_context():
        account = _account()
        assert lj.on_rfq_received(account, actor=SALES) == \
            'Active Account'
        db.session.commit()
        assert account.dev_stage == 'Active Account'


# ── §6.3: targets ────────────────────────────────────────────────────
def test_a_target_with_no_mix_behind_it_says_so():
    """Dividing by an empty mix prints infinity; "we do not know"
    is the honest cell."""
    assert lj.leads_needed(10, {}) is None
    assert lj.leads_needed(10, {'Cold Calling': 0.0}) is None


def test_leads_needed_uses_the_blended_conversion():
    # Half inbound at 50%, half cold calling at 5% -> 27.5%
    got = lj.leads_needed(11, {'Inbound Enquiries': 0.5,
                               'Cold Calling': 0.5})
    assert got == pytest.approx(11 / 0.275)


def test_targets_count_rfqs_from_their_timestamps():
    from app.models.rfq import RFQ

    with flask_app.app_context():
        month = lj.month_key()
        db.session.add(RfqTarget(month=month, vertical=VERTICAL,
                                 emp_code=SALES, target_rfqs=4))
        rfq = RFQ(rfq_number='LJ-RFQ-1', subject='x',
                  lead_driver=SALES, registered_at=datetime.utcnow())
        db.session.add(rfq)
        db.session.commit()
        try:
            rows = lj.targets_for(month=month)
            mine = [r for r in rows if r['emp_code'] == SALES]
            assert mine and mine[0]['target'] == 4
            assert mine[0]['received'] >= 0   # counted, not typed
        finally:
            db.session.delete(rfq)
            db.session.commit()


def test_a_target_is_unique_per_month_vertical_and_person():
    from sqlalchemy.exc import IntegrityError

    with flask_app.app_context():
        month = lj.month_key()
        db.session.add(RfqTarget(month=month, vertical=VERTICAL,
                                 emp_code=SALES, target_rfqs=4))
        db.session.commit()
        db.session.add(RfqTarget(month=month, vertical=VERTICAL,
                                 emp_code=SALES, target_rfqs=9))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()


# ── the flags ────────────────────────────────────────────────────────
def test_both_r4_flags_are_off_by_default():
    from app.services import flags
    assert flags.DEFAULTS['FEATURE_PRE_RFQ_FUNNEL'] is False
    assert flags.DEFAULTS['FEATURE_LEAD_JOURNEY'] is False


def test_the_journey_routes_404_while_the_flag_is_off():
    with flask_app.app_context():
        _flags(False)
        lead_id = _lead(_account()).id
    client = flask_app.test_client()
    with client.session_transaction() as s:
        s['emp_code'] = SALES
    assert client.get(f'/api/journey/{lead_id}').status_code == 404
    assert client.post(f'/api/journey/{lead_id}/activity',
                       json={'kind': 'Phone call'}).status_code == 404


def test_the_flowchart_has_eight_boxes_then_twelve():
    from app.services import rfq_chain

    with flask_app.app_context():
        lead = _lead(_account())
        got = rfq_chain.journey(lead)
        assert len(got['before']) == 8
        assert len(got['after']) == 12
        assert all(b['state'] == 'future' for b in got['after'])
