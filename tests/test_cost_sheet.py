"""
What a job costs us, beside what we are charging for it.

CRM-QUOTE-REV-02 §5.3.

Two asymmetries here are deliberate and easy to "tidy away":

    Freight forwarding prefills the cost from rate tables and
    every other vertical types it. A prefilled zero where there
    are no rate tables would look like a priced job at no cost,
    and somebody would approve it.

    A quote with no price has margin None, not 0%. Zero is a real
    number and would sit in a report as though somebody had
    decided it.
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
os.environ.setdefault('SECRET_KEY', 'cost-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'CostSheet-12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'cost.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
importlib.import_module('app.models.quote')
importlib.import_module('app.models.rfq')
flask_app = _main.app
flask_app.config['WTF_CSRF_ENABLED'] = False
db = _main.db
Lead, Company, Employee = _main.Lead, _main.Company, _main.Employee

from app.models.quote import Quote                         # noqa: E402
from app.models.rfq import RFQ                              # noqa: E402
from app.models.task_engine import TaskInstanceStatus       # noqa: E402
from app.services import cost_sheet as cs                   # noqa: E402
from app.services import rfq_chain as rc                    # noqa: E402

SCM = 'CS_SCM'


def _purge():
    # SQLite reuses rfq ids after a purge, so last test's finished
    # step 7 becomes this test's step 7 unless the instances go too.
    from app.models.quote_terms import QuoteSpecialCondition
    from app.models.task_engine import TaskInstance
    TaskInstance.query.filter(
        TaskInstance.entity_type == 'RFQ').delete(
            synchronize_session=False)
    QuoteSpecialCondition.query.delete()
    Quote.query.filter(Quote.quote_number.like('CS-%')).delete(
        synchronize_session=False)
    RFQ.query.filter(RFQ.rfq_number.like('CS-%')).delete(
        synchronize_session=False)
    Lead.query.filter(Lead.company.like('CS %')).delete(
        synchronize_session=False)
    Employee.query.filter(Employee.emp_code.like('CS_%')).delete(
        synchronize_session=False)
    db.session.commit()


@pytest.fixture(autouse=True)
def _clean():
    with flask_app.app_context():
        db.create_all()
        _purge()
        db.session.add(Employee(emp_code=SCM, name='CS Scm',
                                password_hash='x', role='admin',
                                email='cs@procamlogistics.com'))
        db.session.commit()
        yield
        _flags(False)
        _purge()


def _flags(state):
    import json
    from app.models.integration import AppSetting
    from app.services import flags
    for key in ('flag.FEATURE_RFQ_CHAIN', 'flag.FEATURE_QUOTE_REV_V2'):
        row = AppSetting.query.filter_by(key=key).first()
        if row is None:
            row = AppSetting(key=key)
            db.session.add(row)
        row.value_json = json.dumps(bool(state))
    db.session.commit()
    flags.cache_clear()


_N = [0]


def _world(vertical='Heavy Transport'):
    _N[0] += 1
    lead = Lead(company='CS Co', stage='RFQ Generated', source='manual',
                procam_vertical=vertical)
    db.session.add(lead)
    db.session.flush()
    rfq = RFQ(rfq_number=f'CS-RFQ-{_N[0]}', subject='CS rfq',
              lead_id=lead.id, priority='Medium')
    db.session.add(rfq)
    db.session.flush()
    quote = Quote(quote_number=f'CS-{_N[0]:04d}', rfq_id=rfq.id,
                  lead_id=lead.id, subject='CS quote',
                  currency='INR', total_amount=0)
    db.session.add(quote)
    db.session.commit()
    return lead, rfq, quote


# ── the margin floors (D10) ──────────────────────────────────────────
@pytest.mark.parametrize('vertical,floor', [
    ('Heavy Transport', 0.38), ('General Transport', 0.14),
    ('Freight Forwarding', 0.10), ('Warehousing', 0.18),
    ('Installation', 0.25)])
def test_each_vertical_has_its_floor(vertical, floor):
    with flask_app.app_context():
        assert cs.floor_for(vertical) == floor


def test_an_unknown_vertical_gets_the_lowest_floor_not_none():
    with flask_app.app_context():
        assert cs.floor_for('Something New') == cs.DEFAULT_FLOOR


def test_a_quote_with_no_price_has_no_margin():
    """0% would sit in a report as though somebody decided it."""
    assert cs.margin(1000, 0) is None
    assert cs.margin(1000, None) is None


def test_margin_is_a_fraction_of_the_sell_price():
    assert cs.margin(620, 1000) == pytest.approx(0.38)


def test_below_the_floor_needs_a_director():
    with flask_app.app_context():
        _l, _r, quote = _world('Heavy Transport')
        got = cs.verdict(quote, 700, 1000)      # 30% vs 38%
        assert got['needs_director'] is True
        assert 'below the 38% floor' in got['words']


def test_above_twenty_five_lakh_needs_a_director_whatever_the_margin():
    with flask_app.app_context():
        _l, _r, quote = _world('Freight Forwarding')
        got = cs.verdict(quote, 1_000_000, 3_000_000)   # 67%
        assert got['needs_director'] is True
        assert 'lakh' in got['words']


def test_a_healthy_quote_needs_no_director():
    with flask_app.app_context():
        _l, _r, quote = _world('Freight Forwarding')
        got = cs.verdict(quote, 800, 1000)
        assert got['needs_director'] is False


# ── prefill, and its absence ─────────────────────────────────────────
def test_a_vertical_with_no_rate_tables_is_told_to_type_the_cost():
    """A prefilled zero would look like a priced job at no cost."""
    with flask_app.app_context():
        _l, _r, quote = _world('Heavy Transport')
        cost, why = cs.prefill_cost(quote)
        assert cost is None
        assert 'no rate tables' in why


def test_freight_with_no_vendor_replies_prefills_nothing():
    with flask_app.app_context():
        _l, _r, quote = _world('Freight Forwarding')
        cost, why = cs.prefill_cost(quote)
        assert cost is None
        assert 'vendor' in why or 'rate tables' in why


# ── saving (§5.3's submit gate) ──────────────────────────────────────
def test_saving_refuses_a_quote_with_no_price():
    with flask_app.app_context():
        _flags(True)
        _l, _r, quote = _world()
        ok, why = cs.save(quote, cost=500, sell=0, currency='INR',
                          actor=SCM)
        assert ok is False and 'quoted price is required' in why


def test_saving_refuses_a_missing_currency():
    with flask_app.app_context():
        _flags(True)
        _l, _r, quote = _world()
        ok, why = cs.save(quote, cost=500, sell=1000, currency='',
                          actor=SCM)
        assert ok is False and 'currency' in why


def test_saving_refuses_words_where_numbers_belong():
    with flask_app.app_context():
        _flags(True)
        _l, _r, quote = _world()
        ok, why = cs.save(quote, cost='about 500', sell=1000,
                          currency='INR', actor=SCM)
        assert ok is False and 'numbers' in why


def test_saving_the_cost_sheet_closes_step_6():
    with flask_app.app_context():
        _flags(True)
        _l, rfq, quote = _world('Freight Forwarding')
        task = rc.open_step(rfq, 6, owner=SCM, actor='test')
        task.started_at = datetime.utcnow() - timedelta(hours=30)
        db.session.commit()

        ok, detail = cs.save(quote, cost=800, sell=1000,
                             currency='INR', actor=SCM)
        assert ok is True, detail
        db.session.commit()

        closed = rc.steps_for(rfq)[6]
        assert closed.status == TaskInstanceStatus.COMPLETED
        ev = closed.closed_evidence or {}
        assert ev['cost'] == 800 and ev['sell'] == 1000
        assert ev['margin'] == pytest.approx(0.2)
        assert float(quote.cost_amount) == 800
        assert quote.cost_saved_by == SCM


def test_with_the_chain_off_saving_still_records_the_cost():
    with flask_app.app_context():
        _flags(False)
        _l, _r, quote = _world()
        ok, detail = cs.save(quote, cost=800, sell=1000,
                             currency='INR', actor=SCM)
        assert ok is True
        db.session.commit()
        assert float(quote.cost_amount) == 800
        assert detail['step6'] is None


# ── approval closes step 7 ───────────────────────────────────────────
def test_approval_closes_step_7():
    with flask_app.app_context():
        _flags(True)
        _l, rfq, quote = _world('Freight Forwarding')
        task = rc.open_step(rfq, 7, owner=SCM, actor='test')
        task.started_at = datetime.utcnow() - timedelta(hours=10)
        db.session.commit()
        ok, detail = cs.on_approved(quote, actor=SCM)
        assert ok is True, detail
        db.session.commit()
        assert rc.steps_for(rfq)[7].status == \
            TaskInstanceStatus.COMPLETED


def test_a_condition_conflict_stops_approval_closing_step_7():
    from app.models.quote_terms import QuoteSpecialCondition

    with flask_app.app_context():
        _flags(True)
        _l, rfq, quote = _world('Freight Forwarding')
        rc.open_step(rfq, 7, owner=SCM, actor='test')
        for seq, text in enumerate(('Validity 30 days',
                                    'Offer validity 45 days'),
                                   start=1):
            db.session.add(QuoteSpecialCondition(
                quote_id=quote.id, seq=seq, text=text))
        db.session.commit()
        ok, why = cs.on_approved(quote, actor=SCM)
        assert ok is False
        assert 'Conflict to resolve before approval' in why
        assert rc.steps_for(rfq)[7].status in TaskInstanceStatus.OPEN
