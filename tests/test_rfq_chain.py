"""
Twelve steps, and the timings are measured rather than typed.

CRM-RFQ-CHAIN-01 (R2).

The SCM team's spreadsheet says who was slow. Nobody can check
it, because the numbers on it were typed by the people it
measures. The point of running the chain on the task engine is
that every figure comes from a timestamp instead.

Three things these tests guard, in order of how much damage they
would do if they broke:

    Held time is never counted. An RFQ a director parked for a
    week is not a week of somebody being slow, and if it shows as
    one then the hold button becomes the thing people avoid and
    the whole chain goes back to being a spreadsheet.

    A customer's own clock is never coloured as a Procam delay.
    Step 11 waits on the customer deciding; painting that red
    would blame the sales team for the customer's board meeting.

    Steps close on events. If a step can simply be ticked, the
    chain records what people say happened rather than what did.
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
os.environ.setdefault('SECRET_KEY', 'rfqchain-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'RfqChain-123456')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'rfqchain.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
importlib.import_module('app.models.rfq')
importlib.import_module('app.models.task_engine')
flask_app = _main.app
flask_app.config['WTF_CSRF_ENABLED'] = False
db = _main.db
Employee = _main.Employee

from app.models.rfq import RFQ, RfqCondition                # noqa: E402
from app.models.task_engine import (TaskInstance,           # noqa: E402
                                    TaskInstanceStatus)
from app.services import rfq_chain as rc                    # noqa: E402

HEAD, SCM, SALES, DIRECTOR = 'RC_HEAD', 'RC_SCM', 'RC_SALES', 'RC_DIR'
VERTICAL = 'Heavy Transport'


def _purge():
    from app.models.quote import Quote
    from app.models.quote_terms import QuoteTermSet
    QuoteTermSet.query.filter(
        QuoteTermSet.title == 'Standard Terms').delete(
            synchronize_session=False)
    _main.Lead.query.filter(_main.Lead.company == 'RC Co').delete(
        synchronize_session=False)
    Quote.query.filter(Quote.quote_number.like('QUO-RC-%')).delete(
        synchronize_session=False)
    TaskInstance.query.filter(
        TaskInstance.entity_type == 'RFQ').delete(
            synchronize_session=False)
    RfqCondition.query.delete()
    RFQ.query.filter(RFQ.subject.like('RC %')).delete(
        synchronize_session=False)
    Employee.query.filter(Employee.emp_code.like('RC_%')).delete(
        synchronize_session=False)
    db.session.commit()


@pytest.fixture(autouse=True)
def _clean():
    with flask_app.app_context():
        db.create_all()
        _purge()
        for code, role in ((HEAD, 'user'), (SCM, 'user'),
                           (SALES, 'user'), (DIRECTOR, 'director')):
            db.session.add(Employee(
                emp_code=code, name=code.title(), password_hash='x',
                email=f'{code.lower()}@procamlogistics.com',
                role=role, vertical=VERTICAL))
        db.session.commit()
        yield
        _purge()


def _rfq(priority='Medium', **kw):
    # An RFQ belongs to somebody, or the Access Matrix correctly
    # hides it and the route tests 404 for the right reason.
    kw.setdefault('lead_driver', SCM)
    kw.setdefault('created_by_id', SCM)
    rfq = RFQ(rfq_number=f'RFQ-TEST-{datetime.utcnow().microsecond}',
              subject='RC chain test', priority=priority,
              needs_technical=kw.pop('needs_technical', True),
              needs_survey=kw.pop('needs_survey', True), **kw)
    db.session.add(rfq)
    db.session.commit()
    return rfq


def _open(rfq, step_no, *, started_hours_ago=0, owner=SCM):
    task = rc.open_step(rfq, step_no, owner=owner, actor='test')
    if started_hours_ago:
        task.started_at = datetime.utcnow() - timedelta(
            hours=started_hours_ago)
        task.created_at = task.started_at
    db.session.commit()
    return task


# ── the standard time comes from the priority ────────────────────────
@pytest.mark.parametrize('priority,days', [
    ('Critical', 2), ('High', 4), ('Medium', 7), ('Low', 10)])
def test_the_window_is_the_priority(priority, days):
    with flask_app.app_context():
        # Costing is 30% of the window.
        assert rc.standard_hours(6, priority) == pytest.approx(
            days * 24 * 0.30)


def test_a_customer_clock_has_no_standard_time():
    """Step 11 waits on the customer. Colouring that as our delay
    would blame sales for the customer's board meeting."""
    with flask_app.app_context():
        assert rc.standard_hours(11, 'Critical') is None
        rfq = _rfq('Critical')
        task = _open(rfq, 11, started_hours_ago=500)
        colour, words = rc.verdict(task, priority='Critical')
        assert colour == rc.BLUE
        assert 'customer' in words
        assert rc.result_for(task, priority='Critical') is None


def test_negotiation_runs_on_a_response_clock():
    with flask_app.app_context():
        assert rc.standard_hours(9, 'Critical') == 4
        assert rc.standard_hours(9, 'Medium') == 24


# ── colours ──────────────────────────────────────────────────────────
def test_a_step_past_its_time_is_red():
    with flask_app.app_context():
        rfq = _rfq('Medium')                       # costing = 50.4h
        task = _open(rfq, 6, started_hours_ago=60)
        colour, words = rc.verdict(task, priority='Medium')
        assert colour == rc.RED and 'overdue' in words


def test_a_short_critical_step_warns_before_it_is_due():
    """A flat 24-hour warning never fires on a 2.4-hour step."""
    with flask_app.app_context():
        rfq = _rfq('Critical')                     # registration=2.4h
        task = _open(rfq, 2, started_hours_ago=1.5)
        colour, _w = rc.verdict(task, priority='Critical')
        assert colour == rc.ORANGE


def test_a_fresh_step_is_green():
    with flask_app.app_context():
        rfq = _rfq('Low')
        task = _open(rfq, 6)
        assert rc.verdict(task, priority='Low')[0] == rc.GREEN


def test_a_held_step_is_purple():
    with flask_app.app_context():
        rfq = _rfq('Medium')
        task = _open(rfq, 6, started_hours_ago=100)
        task.held_at = datetime.utcnow()
        db.session.commit()
        assert rc.verdict(task, priority='Medium')[0] == rc.PURPLE


# ── held time is never counted ───────────────────────────────────────
def test_hold_pauses_the_clock_and_reactivation_pays_it_back():
    with flask_app.app_context():
        rfq = _rfq('Medium')
        task = _open(rfq, 6, started_hours_ago=10)
        due_before = task.due_at

        ok, _d = rc.hold(rfq, actor=DIRECTOR, reason='Postponed by customer')
        assert ok, _d
        db.session.commit()
        assert task.held_at is not None

        # Six hours of hold, then back.
        task.held_at = datetime.utcnow() - timedelta(hours=6)
        rfq.held_at = task.held_at
        db.session.commit()
        ok, detail = rc.reactivate(rfq, actor=DIRECTOR)
        assert ok, detail
        db.session.commit()

        assert task.paused_seconds >= 6 * 3600 - 60
        assert task.held_at is None
        # The due date moved by the held time, or the step comes
        # back already overdue for a pause nobody caused.
        assert task.due_at > due_before


def test_held_time_does_not_make_a_step_late():
    with flask_app.app_context():
        rfq = _rfq('Medium')                       # costing = 50.4h
        task = _open(rfq, 6, started_hours_ago=60)
        # 20 of those 60 hours were a hold.
        task.paused_seconds = 20 * 3600
        db.session.commit()
        assert rc.elapsed_hours(task) == pytest.approx(40, abs=0.1)
        assert rc.result_for(task, priority='Medium') == rc.ON_TIME


def test_only_a_head_or_director_may_hold():
    with flask_app.app_context():
        rfq = _rfq()
        ok, why = rc.hold(rfq, actor=SALES, reason='Postponed by customer')
        assert ok is False and 'head or a director' in why


def test_a_hold_always_needs_a_reason():
    with flask_app.app_context():
        ok, why = rc.hold(_rfq(), actor=DIRECTOR, reason='')
        assert ok is False and 'reason' in why


# ── results and reasons ──────────────────────────────────────────────
def test_a_late_close_is_refused_without_a_reason():
    with flask_app.app_context():
        rfq = _rfq('Medium')
        _open(rfq, 3, started_hours_ago=200)
        ok, why = rc.close_step(rfq, 3, actor=SCM, event='confirm')
        assert ok is False and 'delay reason' in why


def test_an_early_close_is_refused_without_a_reason():
    with flask_app.app_context():
        rfq = _rfq('Low')
        _open(rfq, 6)                              # barely started
        ok, why = rc.close_step(rfq, 6, actor=SCM,
                                event='cost_sheet_saved')
        assert ok is False and 'good practice' in why


def test_others_needs_a_remark():
    with flask_app.app_context():
        rfq = _rfq('Medium')
        _open(rfq, 3, started_hours_ago=200)
        ok, why = rc.close_step(rfq, 3, actor=SCM, event='confirm',
                                reason='Others')
        assert ok is False and 'remark' in why


def test_an_on_time_close_asks_for_nothing():
    with flask_app.app_context():
        rfq = _rfq('Medium')                       # screening = 8.4h
        _open(rfq, 3, started_hours_ago=6)
        ok, detail = rc.close_step(rfq, 3, actor=SCM, event='confirm')
        assert ok is True, detail
        assert detail['result'] == rc.ON_TIME


def test_the_result_is_frozen_at_close():
    """Recomputing later would rewrite history whenever a setting
    changed."""
    with flask_app.app_context():
        rfq = _rfq('Medium')
        _open(rfq, 3, started_hours_ago=6)
        rc.close_step(rfq, 3, actor=SCM, event='confirm')
        db.session.commit()
        task = rc.steps_for(rfq)[3]
        assert task.result == rc.ON_TIME
        assert task.completed_at is not None


# ── steps close on events ────────────────────────────────────────────
def test_a_step_cannot_be_closed_by_the_wrong_event():
    with flask_app.app_context():
        rfq = _rfq('Medium')
        _open(rfq, 6, started_hours_ago=20)
        ok, why = rc.close_step(rfq, 6, actor=SCM, event='confirm')
        assert ok is False and 'cost_sheet_saved' in why


def test_only_screening_technical_and_survey_are_confirmable():
    assert rc.CONFIRMABLE == (3, 4, 5)


def test_closing_a_step_opens_the_next_one():
    with flask_app.app_context():
        rfq = _rfq('Medium')
        _open(rfq, 3, started_hours_ago=6)
        ok, detail = rc.close_step(rfq, 3, actor=SCM, event='confirm')
        db.session.commit()
        assert detail['next'] == 4
        assert 4 in rc.steps_for(rfq)


def test_an_unticked_step_is_skipped_entirely():
    """A chain that always shows twelve boxes when two never happen
    reports a false bottleneck at the two that stay grey."""
    with flask_app.app_context():
        rfq = _rfq('Medium', needs_technical=False,
                   needs_survey=False)
        assert [s['no'] for s in rc.applicable(rfq)][:5] == \
            [1, 2, 3, 6, 7]
        _open(rfq, 3, started_hours_ago=6)
        _ok, detail = rc.close_step(rfq, 3, actor=SCM, event='confirm')
        assert detail['next'] == 6


def test_a_closed_step_is_never_reopened():
    with flask_app.app_context():
        rfq = _rfq('Medium')
        _open(rfq, 3, started_hours_ago=6)
        rc.close_step(rfq, 3, actor=SCM, event='confirm')
        db.session.commit()
        assert rc.open_step(rfq, 3, actor='test') is None


def test_a_held_step_cannot_be_closed():
    with flask_app.app_context():
        rfq = _rfq('Medium')
        _open(rfq, 3, started_hours_ago=6)
        rc.hold(rfq, actor=DIRECTOR, reason='Postponed by customer')
        db.session.commit()
        ok, why = rc.close_step(rfq, 3, actor=SCM, event='confirm')
        assert ok is False and 'hold' in why


# ── priority change ──────────────────────────────────────────────────
def test_changing_the_priority_moves_every_open_due_date():
    with flask_app.app_context():
        rfq = _rfq('Low')
        task = _open(rfq, 6)
        was = task.due_at
        ok, _d = rc.set_priority(rfq, 'Critical', actor=DIRECTOR)
        assert ok
        db.session.commit()
        assert task.due_at < was          # a tighter window


def test_a_salesperson_cannot_change_the_priority():
    with flask_app.app_context():
        ok, why = rc.set_priority(_rfq('Low'), 'Critical',
                                  actor=SALES)
        assert ok is False and 'head or a director' in why


# ── registration ─────────────────────────────────────────────────────
@pytest.mark.parametrize('days,expected', [
    (1, 'Critical'), (3, 'High'), (6, 'Medium'), (20, 'Low')])
def test_priority_is_suggested_from_the_deadline(days, expected):
    today = date(2026, 10, 26)
    assert rc.suggest_priority(today + timedelta(days=days),
                               today=today) == expected


# ── the board ────────────────────────────────────────────────────────
def test_the_board_shows_done_current_and_future():
    with flask_app.app_context():
        rfq = _rfq('Medium')
        _open(rfq, 3, started_hours_ago=6)
        rc.close_step(rfq, 3, actor=SCM, event='confirm')
        db.session.commit()
        rows = {r['step_no']: r for r in rc.board(rfq)}
        assert rows[3]['state'] == 'done'
        assert rows[4]['state'] == 'current'
        assert rows[8]['state'] == 'future'
        assert rows[1]['colour'] == rc.GREY


# ── the flag ─────────────────────────────────────────────────────────
def test_the_chain_is_off_by_default():
    from app.services import flags
    assert flags.DEFAULTS['FEATURE_RFQ_CHAIN'] is False
    with flask_app.app_context():
        assert rc.enabled('nobody') is False


# ── the routes ───────────────────────────────────────────────────────
def _client(emp=None):
    c = flask_app.test_client()
    if emp:
        with c.session_transaction() as s:
            s['emp_code'] = emp
    return c


def _flag(state=True):
    import json
    from app.models.integration import AppSetting
    from app.services import flags
    row = AppSetting.query.filter_by(
        key='flag.FEATURE_RFQ_CHAIN').first()
    if row is None:
        row = AppSetting(key='flag.FEATURE_RFQ_CHAIN')
        db.session.add(row)
    row.value_json = json.dumps(bool(state))
    db.session.commit()
    flags.cache_clear()


def test_every_route_404s_while_the_flag_is_off():
    with flask_app.app_context():
        _flag(False)
        rfq_id = _rfq().id
    c = _client(HEAD)
    for path in ('/api/chain/board', f'/api/chain/{rfq_id}',
                 '/rfqs/chain'):
        assert c.get(path).status_code == 404, path
    assert c.post(f'/api/chain/{rfq_id}/hold',
                  json={'reason': 'x'}).status_code == 404


def test_there_is_no_button_that_closes_an_event_step():
    """Steps 6, 7, 8, 11 and 12 close when the work happens. A
    button would let the chain record what people say happened."""
    with flask_app.app_context():
        _flag(True)
        rfq = _rfq('Medium')
        _open(rfq, 6, started_hours_ago=20)
        rfq_id = rfq.id
    try:
        r = _client(SCM).post(f'/api/chain/{rfq_id}/confirm',
                              json={'step_no': 6})
        assert r.status_code == 400
        assert 'close when the work happens' in r.get_json()['error']
    finally:
        with flask_app.app_context():
            _flag(False)


def test_registration_warns_when_the_deadline_beats_the_window():
    with flask_app.app_context():
        _flag(True)
        rfq = _rfq('Medium')
        _open(rfq, 2, started_hours_ago=1)
        rfq_id = rfq.id
        soon = str(date.today() + timedelta(days=2))
    try:
        r = _client(SCM).post(f'/api/chain/{rfq_id}/register',
                              json={'priority': 'Medium',
                                    'customer_deadline': soon})
        body = r.get_json()
        assert r.status_code == 200, body
        assert 'before the Medium window' in body['warning']
    finally:
        with flask_app.app_context():
            _flag(False)


def test_screening_that_is_missing_something_keeps_the_step_open():
    """Closing there would record a question as an answer."""
    with flask_app.app_context():
        _flag(True)
        rfq = _rfq('Medium')
        _open(rfq, 3, started_hours_ago=4)
        rfq_id = rfq.id
    try:
        r = _client(SCM).post(
            f'/api/chain/{rfq_id}/screening',
            json={'outcome': 'Missing: drawings / dimensions'})
        body = r.get_json()
        assert body['ok'] is True and body['closed'] is False
        with flask_app.app_context():
            task = rc.steps_for(db.session.get(RFQ, rfq_id))[3]
            assert task.waiting_for_customer is True
            assert task.status in TaskInstanceStatus.OPEN
    finally:
        with flask_app.app_context():
            _flag(False)


def test_a_confirm_records_its_conditions_with_visibility():
    with flask_app.app_context():
        _flag(True)
        rfq = _rfq('Medium')
        _open(rfq, 4, started_hours_ago=20)
        rfq_id = rfq.id
    try:
        r = _client(SCM).post(f'/api/chain/{rfq_id}/confirm', json={
            'step_no': 4,
            'conditions': [
                {'type': 'Exclusion', 'text': 'Crane not included',
                 'visibility': 'customer'},
                {'type': 'Assumption', 'text': 'Ask ops re axle load',
                 'visibility': 'internal'}]})
        assert r.status_code == 200, r.get_data(as_text=True)
        with flask_app.app_context():
            rows = RfqCondition.query.filter_by(rfq_id=rfq_id).all()
            assert len(rows) == 2
            facing = [c for c in rows if c.customer_facing()]
            assert len(facing) == 1
            assert facing[0].text == 'Crane not included'
    finally:
        with flask_app.app_context():
            _flag(False)


# ── step 8 closes when a quotation goes out ──────────────────────────
def _quote_for(rfq, **kw):
    from app.models.quote import Quote
    # Owned by somebody, or the Access Matrix correctly hides it
    # and the route 404s for a reason that is not the one under
    # test.
    kw.setdefault('prepared_by_id', SALES)
    kw.setdefault('created_by_id', SALES)
    q = Quote(quote_number=kw.pop('number', 'QUO-RC-0001'),
              rfq_id=rfq.id, subject='RC quote',
              total_amount=kw.pop('amount', 125000),
              currency='INR', status=kw.pop('status', 'Approved'),
              rev_no=kw.pop('rev_no', 0), **kw)
    db.session.add(q)
    db.session.commit()
    return q


def test_sending_a_quotation_closes_step_8_with_its_evidence():
    with flask_app.app_context():
        _flag(True)
        try:
            rfq = _rfq('Medium')
            _open(rfq, 8, started_hours_ago=20)
            quote = _quote_for(rfq, rev_no=1)
            ok, detail = rc.on_quote_sent(quote, actor=SALES)
            assert ok, detail
            db.session.commit()

            task = rc.steps_for(rfq)[8]
            assert task.status == TaskInstanceStatus.COMPLETED
            ev = task.closed_evidence or {}
            # The claim is "quote sent"; these are the evidence.
            assert ev['quote_number'] == 'QUO-RC-0001'
            assert ev['rev_no'] == 1
            assert ev['amount'] == 125000
            assert ev['sent_by'] == SALES
            assert ev['sent_at']
        finally:
            _flag(False)


def test_sending_the_same_quotation_twice_closes_one_step():
    """The CRM send path and the mail matcher can both see it."""
    with flask_app.app_context():
        _flag(True)
        try:
            rfq = _rfq('Medium')
            _open(rfq, 8, started_hours_ago=20)
            quote = _quote_for(rfq)
            assert rc.on_quote_sent(quote, actor=SALES)[0] is True
            db.session.commit()
            ok, why = rc.on_quote_sent(quote, actor=SALES)
            assert ok is False and 'already closed' in why
        finally:
            _flag(False)


def test_a_quote_with_no_rfq_closes_nothing_and_does_not_raise():
    with flask_app.app_context():
        _flag(True)
        try:
            from app.models.quote import Quote
            loose = Quote(quote_number='QUO-RC-LOOSE',
                          subject='no rfq', total_amount=1)
            db.session.add(loose)
            db.session.commit()
            ok, why = rc.on_quote_sent(loose, actor=SALES)
            assert ok is False and 'not against an RFQ' in why
            db.session.delete(loose)
            db.session.commit()
        finally:
            _flag(False)


def test_with_the_chain_off_sending_changes_no_step():
    with flask_app.app_context():
        _flag(False)
        rfq = _rfq('Medium')
        _open(rfq, 8, started_hours_ago=20)
        quote = _quote_for(rfq, number='QUO-RC-OFF')
        ok, why = rc.on_quote_sent(quote, actor=SALES)
        assert ok is False and 'off' in why
        assert rc.steps_for(rfq)[8].status in TaskInstanceStatus.OPEN


def test_the_send_route_closes_step_8():
    with flask_app.app_context():
        _flag(True)
        try:
            rfq = _rfq('Medium')
            _open(rfq, 8, started_hours_ago=20)
            quote = _quote_for(rfq, number='QUO-RC-ROUTE',
                               status='Approved')
            rfq_id, qid = rfq.id, quote.id
        except Exception:
            _flag(False)
            raise
    try:
        r = _client(SALES).post(f'/api/quotes/{qid}/submit-to-client',
                                json={})
        assert r.status_code == 200, r.get_data(as_text=True)
        with flask_app.app_context():
            from app.models.rfq import RFQ as _R
            task = rc.steps_for(db.session.get(_R, rfq_id))[8]
            assert task.status == TaskInstanceStatus.COMPLETED
            assert (task.closed_evidence or {})['quote_number'] == \
                'QUO-RC-ROUTE'
    finally:
        with flask_app.app_context():
            _flag(False)


# ── §4.6: the tick list comes from the terms library ────────────────
def _terms(vertical, body, version=1):
    from app.models.quote_terms import QuoteTermSet
    row = QuoteTermSet(vertical=vertical, version=version,
                       title='Standard Terms', body=body,
                       is_current=True)
    db.session.add(row)
    db.session.commit()
    return row


def _rfq_in(vertical):
    """An RFQ whose vertical resolves, via its lead."""
    lead = _main.Lead(company='RC Co', stage='RFQ Generated',
                      procam_vertical=vertical, source='manual')
    db.session.add(lead)
    db.session.flush()
    rfq = _rfq('Medium', lead_id=lead.id)
    return rfq


def test_clauses_are_proposed_from_the_vertical_terms():
    with flask_app.app_context():
        _terms(VERTICAL,
               'DRAFT — replace before issuing.\n'
               '1. Rates are valid for the period stated.\n'
               '2. Detention is chargeable beyond the free period.\n')
        rfq = _rfq_in(VERTICAL)
        rows, note = rc.proposed_conditions(rfq)
        assert [r['text'] for r in rows] == [
            'Rates are valid for the period stated.',
            'Detention is chargeable beyond the free period.']
        # The numbering is ours; the customer never sees "1."
        assert not any(r['text'][0].isdigit() for r in rows)
        assert 'DRAFT' in note


def test_nothing_is_pre_ticked():
    """Pre-ticking puts a page of conditions on a quotation nobody
    read, which is what a tick list is meant to prevent."""
    with flask_app.app_context():
        _terms(VERTICAL, '1. Rates exclude duties and taxes.\n')
        rows, _note = rc.proposed_conditions(_rfq_in(VERTICAL))
        assert rows and all(r['ticked'] is False for r in rows)


def test_a_vertical_with_no_terms_says_so():
    with flask_app.app_context():
        rfq = _rfq_in('Nowhere Vertical')
        rows, note = rc.proposed_conditions(rfq)
        assert rows == []
        assert 'No standard terms on file' in note


def test_headings_are_not_offered_as_clauses():
    with flask_app.app_context():
        _terms(VERTICAL,
               'Standard Terms and Conditions\n'
               'DRAFT — replace before issuing.\n'
               '1. A real clause that is long enough to keep.\n'
               '2. ok\n')
        rows, _n = rc.proposed_conditions(_rfq_in(VERTICAL))
        assert len(rows) == 1
        assert rows[0]['text'].startswith('A real clause')


def test_a_clause_already_recorded_is_not_proposed_again():
    with flask_app.app_context():
        _flag(True)
        try:
            _terms(VERTICAL,
                   '1. Rates exclude duties and taxes.\n'
                   '2. Detention is chargeable beyond free time.\n')
            rfq = _rfq_in(VERTICAL)
            db.session.add(RfqCondition(
                rfq_id=rfq.id, step_no=4,
                text='Rates exclude duties and taxes.',
                visibility=RfqCondition.CUSTOMER))
            db.session.commit()
            rfq_id = rfq.id
        except Exception:
            _flag(False)
            raise
    try:
        body = _client(SCM).get(
            f'/api/chain/{rfq_id}/conditions').get_json()
        assert body['ok'] is True
        texts = [r['text'] for r in body['proposed']]
        assert 'Rates exclude duties and taxes.' not in texts
        assert len(body['recorded']) == 1
        assert body['types']
    finally:
        with flask_app.app_context():
            _flag(False)


# ── §4.1: the twelve as admin-editable definitions ──────────────────
def test_all_twelve_steps_are_seeded_as_definitions():
    from app.models.task_engine import TaskDefinition

    with flask_app.app_context():
        added, _present = rc.seed_definitions(actor='test')
        db.session.commit()
        try:
            rows = TaskDefinition.query.filter_by(
                module='rfq_chain').all()
            assert len(rows) == 12
            assert {r.task_key for r in rows} == {
                s['key'] for s in rc.STEPS}
            # The catalogue reads in step order, not insertion order.
            first = rc.definition_for(1)
            assert first.title.startswith('1. RFQ Received')
        finally:
            TaskDefinition.query.filter_by(
                module='rfq_chain').delete(
                    synchronize_session=False)
            db.session.commit()


def test_a_definition_already_tuned_is_left_alone():
    """A seed that overwrites undoes an admin's change on every
    deploy."""
    from app.models.task_engine import TaskDefinition

    with flask_app.app_context():
        rc.seed_definitions(actor='test')
        db.session.commit()
        try:
            row = rc.definition_for(3)
            row.sla_hours = 99
            row.title = 'Renamed by an admin'
            db.session.commit()

            added, present = rc.seed_definitions(actor='test')
            db.session.commit()
            assert added == [] and present == 12
            again = rc.definition_for(3)
            assert again.sla_hours == 99
            assert again.title == 'Renamed by an admin'
        finally:
            TaskDefinition.query.filter_by(
                module='rfq_chain').delete(
                    synchronize_session=False)
            db.session.commit()


def test_each_definition_carries_the_rule_not_a_name():
    """§4.1 — who holds a step comes from a setting, so no
    person's code is written into the catalogue."""
    from app.models.task_engine import TaskDefinition

    with flask_app.app_context():
        rc.seed_definitions(actor='test')
        db.session.commit()
        try:
            rows = TaskDefinition.query.filter_by(
                module='rfq_chain').all()
            for row in rows:
                rule = row.next_owner_rule or {}
                assert rule, row.task_key
                blob = str(rule)
                # A real employee code would look like EMP123456.
                assert 'EMP' not in blob and 'DIR' not in blob
            assert rc.definition_for(2).next_owner_rule['key'] == \
                'rfq_coordinator'
        finally:
            TaskDefinition.query.filter_by(
                module='rfq_chain').delete(
                    synchronize_session=False)
            db.session.commit()


def test_the_chain_still_runs_without_the_definitions():
    """The rows are a catalogue, not a dependency — a chain must
    not stop because somebody deleted a definition."""
    from app.models.task_engine import TaskDefinition

    with flask_app.app_context():
        TaskDefinition.query.filter_by(module='rfq_chain').delete(
            synchronize_session=False)
        db.session.commit()
        rfq = _rfq('Medium')
        task = rc.open_step(rfq, 3, owner=SCM, actor='test')
        assert task is not None
        assert rc.standard_hours(3, 'Medium') > 0


def test_the_docstring_no_longer_claims_what_was_not_true():
    import inspect
    text = inspect.getdoc(rc) or ''
    assert 'seed_definitions' in text
    assert 'The steps are `TaskDefinition` rows' not in text
