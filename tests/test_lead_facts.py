"""The lead's dates, its team, and the quotation attached to it.

CRM-FINAL-CLOSEOUT-01 §5, §6, §7.

§5 is the one with a trap in it. "Estimated" is easy to build and
easy to build wrongly: the earliest dated thing on a lead is usually
the first call, and reading that as the bound for the RFQ dates the
RFQ *before* the call — a lower bound printed as an upper one. So
most of the date tests are about which direction the bound points,
and one asserts the whole point of the release: that no answer ever
comes back without a label saying how it is known.
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
os.environ.setdefault('SECRET_KEY', 'facts-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'Facts-123456789')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'facts.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app = _main.app
db = _main.db

from app import (Company, Employee, Lead, LeadActivity,       # noqa: E402
                 LeadAttachment, LeadStageHistory)
from app.models.audit import AuditEvent                       # noqa: E402
from app.models.task_engine import (TaskInstance,              # noqa: E402
                                    TaskInstanceStatus)
from app.services import lead_dates as ld                      # noqa: E402
from app.services import lead_quote_file as lqf                # noqa: E402
from app.services import lead_team as lt                       # noqa: E402

#: Codes used by these tests only. Shaped so nothing real collides.
OWNER = 'ZF_OWN'
SECOND = 'ZF_SEC'
HEAD = 'ZF_HED'
ACCT = 'ZF_ACC'
TASKER = 'ZF_TSK'

TODAY = date(2026, 11, 8)


@pytest.fixture(autouse=True)
def clean(tmp_path):
    with flask_app.app_context():
        db.create_all()
        _purge()
        os.environ[lqf.STORAGE_ENV] = str(tmp_path / 'quotes')
        yield
        _purge()
        os.environ.pop(lqf.STORAGE_ENV, None)


def _purge():
    for model in (LeadAttachment, LeadActivity, LeadStageHistory,
                  TaskInstance, Lead):
        model.query.delete()
    AuditEvent.query.filter(
        AuditEvent.actor.like('ZF_%')).delete(synchronize_session=False)
    Company.query.filter(Company.name.like('ZFacts%')).delete(
        synchronize_session=False)
    Employee.query.filter(Employee.emp_code.like('ZF_%')).delete(
        synchronize_session=False)
    db.session.commit()


def _emp(code, name, *, vertical='Project Cargo', active=True,
         role='user'):
    emp = Employee(emp_code=code, name=name, password_hash='x',
                   role=role, email=f'{code.lower()}@procamgroup.in',
                   vertical=vertical, is_active=active)
    db.session.add(emp)
    db.session.commit()
    return emp


def _lead(**kw):
    kw.setdefault('company', 'ZFacts Client')
    kw.setdefault('stage', 'New')
    kw.setdefault('source', 'email')
    kw.setdefault('created_at', datetime(2026, 9, 1, 9, 0))
    lead = Lead(**kw)
    db.session.add(lead)
    db.session.commit()
    return lead


def _activity(lead, kind, when):
    db.session.add(LeadActivity(lead_id=lead.id, kind=kind,
                                subject=f'a {kind}', occurred_at=when))
    db.session.commit()


def _moved(lead, to_stage, when):
    db.session.add(LeadStageHistory(lead_id=lead.id, to_stage=to_stage,
                                    changed_at=when))
    db.session.commit()


def _row(lead, key):
    return {r['key']: r for r in ld.for_lead(lead)}[key]


# ═══════════════════════════════════════════════════════════════════
# §5 — the date ladder
# ═══════════════════════════════════════════════════════════════════
def test_every_answer_carries_a_label():
    """The whole release in one assertion. A date with no label is
    indistinguishable, on the screen and in a CSV, from one somebody
    typed."""
    with flask_app.app_context():
        lead = _lead(stage='Quoted', rfq_date=date(2026, 9, 20))
        _moved(lead, 'Quoted', datetime(2026, 10, 1, 9, 0))
        for row in ld.for_lead(lead):
            assert row['source'] in (ld.ENTERED, ld.AUDIT,
                                     ld.ESTIMATED, ld.UNKNOWN)
            assert row['source_label']
            assert row['detail'], row['key']


def test_the_column_wins():
    with flask_app.app_context():
        lead = _lead(rfq_date=date(2026, 9, 20))
        _activity(lead, 'rfq', datetime(2026, 9, 25, 9, 0))
        row = _row(lead, 'rfq')
        assert row['source'] == ld.ENTERED
        assert row['date'] == date(2026, 9, 20)


def test_an_activity_dates_it_when_the_column_is_empty():
    with flask_app.app_context():
        lead = _lead()
        _activity(lead, 'call', datetime(2026, 9, 5, 11, 0))
        row = _row(lead, 'first_call')
        assert row['source'] == ld.AUDIT
        assert row['date'] == date(2026, 9, 5)
        assert row['bound'] is False


def test_the_first_call_is_the_first_one_not_the_last():
    with flask_app.app_context():
        lead = _lead()
        _activity(lead, 'call', datetime(2026, 10, 2, 9, 0))
        _activity(lead, 'call', datetime(2026, 9, 5, 9, 0))
        assert _row(lead, 'first_call')['date'] == date(2026, 9, 5)


def test_a_stage_move_dates_the_step_it_implies():
    with flask_app.app_context():
        lead = _lead(stage='RFQ Generated')
        _moved(lead, 'RFQ Generated', datetime(2026, 9, 18, 9, 0))
        row = _row(lead, 'rfq')
        assert row['source'] == ld.AUDIT
        assert row['date'] == date(2026, 9, 18)


def test_an_activity_is_preferred_over_the_stage_move():
    """The activity dates the event; the stage move dates the
    paperwork that followed it."""
    with flask_app.app_context():
        lead = _lead(stage='RFQ Generated')
        _activity(lead, 'rfq', datetime(2026, 9, 14, 9, 0))
        _moved(lead, 'RFQ Generated', datetime(2026, 9, 18, 9, 0))
        assert _row(lead, 'rfq')['date'] == date(2026, 9, 14)


def test_an_old_lead_is_dated_from_the_audit_trail():
    """`lead_stage_history` was added after the fact. For the rows
    that predate it the audit events are the only record, which is
    the same source scripts/crm2_backfill_stage_entered.py reads."""
    with flask_app.app_context():
        lead = _lead(stage='Quoted')
        db.session.add(AuditEvent(
            occurred_at=datetime(2026, 9, 11, 9, 0), actor='ZF_OWN',
            action='lead.stage_change', entity_type='lead',
            entity_id=str(lead.id), new_value={'stage': 'RFQ Generated'}))
        db.session.commit()
        row = _row(lead, 'rfq')
        assert row['source'] == ld.AUDIT
        assert row['date'] == date(2026, 9, 11)
        assert 'audit trail' in row['detail']


def test_the_stage_history_beats_the_audit_event():
    """Two records of one move disagree by a day; the screen must not
    show whichever was read second."""
    with flask_app.app_context():
        lead = _lead(stage='Quoted')
        _moved(lead, 'RFQ Generated', datetime(2026, 9, 18, 9, 0))
        db.session.add(AuditEvent(
            occurred_at=datetime(2026, 9, 11, 9, 0), actor='ZF_OWN',
            action='lead.stage_change', entity_type='lead',
            entity_id=str(lead.id), new_value={'stage': 'RFQ Generated'}))
        db.session.commit()
        assert _row(lead, 'rfq')['date'] == date(2026, 9, 18)


# ── the bound, which is the part that can be wrong ───────────────────
def test_the_estimate_is_an_upper_bound_from_a_later_step():
    with flask_app.app_context():
        lead = _lead(stage='Quoted')
        _moved(lead, 'Quoted', datetime(2026, 10, 1, 9, 0))
        row = _row(lead, 'rfq')
        assert row['source'] == ld.ESTIMATED
        assert row['bound'] is True
        assert row['date'] == date(2026, 10, 1)
        assert 'on or before' in row['detail']


def test_an_earlier_step_is_never_read_as_the_bound():
    """The trap. The call is the earliest dated thing on the lead;
    using it would date the RFQ before the call happened."""
    with flask_app.app_context():
        lead = _lead(stage='Quoted')
        _activity(lead, 'call', datetime(2026, 9, 5, 9, 0))
        _moved(lead, 'Quoted', datetime(2026, 10, 1, 9, 0))
        row = _row(lead, 'rfq')
        assert row['source'] == ld.ESTIMATED
        assert row['date'] == date(2026, 10, 1), (
            'the RFQ was bounded by something that happened before it')
        assert row['date'] > _row(lead, 'first_call')['date']


def test_the_earliest_of_several_later_steps_is_the_tightest_bound():
    with flask_app.app_context():
        lead = _lead(stage='Under Negotiation')
        _moved(lead, 'Quoted', datetime(2026, 10, 1, 9, 0))
        _moved(lead, 'Under Negotiation', datetime(2026, 10, 20, 9, 0))
        assert _row(lead, 'rfq')['date'] == date(2026, 10, 1)


def test_a_lead_that_never_got_there_is_not_estimated():
    """A lead on New has not had an RFQ. Saying "on or before today"
    would invent an event as well as a date."""
    with flask_app.app_context():
        lead = _lead(stage='New')
        assert _row(lead, 'rfq')['source'] == ld.UNKNOWN
        assert _row(lead, 'rfq')['date'] is None


def test_past_the_stage_with_nothing_dated_says_so():
    with flask_app.app_context():
        lead = _lead(stage='Quoted')
        row = _row(lead, 'rfq')
        assert row['source'] == ld.UNKNOWN
        assert row['date'] is None
        assert 'did' in row['detail'] and 'bound' in row['detail']


def test_a_stage_reached_and_left_still_counts():
    """A lead moved back to Call Done still had its RFQ."""
    with flask_app.app_context():
        lead = _lead(stage='Call Done')
        _moved(lead, 'Quoted', datetime(2026, 10, 1, 9, 0))
        _moved(lead, 'Call Done', datetime(2026, 10, 9, 9, 0))
        assert _row(lead, 'rfq')['source'] == ld.ESTIMATED


def test_a_retired_stage_name_resolves():
    """`stage_map.ALIASES` folds Visit Done onto Business Discussion."""
    with flask_app.app_context():
        lead = _lead(stage='RFQ Generated')
        _moved(lead, 'Visit Done', datetime(2026, 9, 9, 9, 0))
        row = _row(lead, 'meeting')
        assert row['source'] == ld.AUDIT
        assert row['date'] == date(2026, 9, 9)


def test_the_enquiry_date_is_always_known():
    with flask_app.app_context():
        lead = _lead()
        row = _row(lead, 'created')
        assert row['source'] == ld.ENTERED
        assert row['date'] == date(2026, 9, 1)


# ── the rule about what an estimate may be used for ──────────────────
def test_firm_only_leaves_the_estimates_out():
    """An estimate is a bound for a reader, never an input to
    anything that decides whether somebody is late."""
    with flask_app.app_context():
        lead = _lead(stage='Quoted')
        _moved(lead, 'Quoted', datetime(2026, 10, 1, 9, 0))
        firm = ld.firm_only(lead)
        assert all(r['source'] in ld.FIRM for r in firm)
        assert not any(r['source'] == ld.ESTIMATED for r in firm)
        # And there is something estimated to have left out.
        assert any(r['source'] == ld.ESTIMATED for r in ld.for_lead(lead))


def test_no_estimate_comes_from_an_average():
    """The line this module draws. Every estimated date must equal a
    timestamp that is actually on this lead — never a figure derived
    from how long these things usually take."""
    with flask_app.app_context():
        lead = _lead(stage='Under Negotiation')
        _moved(lead, 'Quoted', datetime(2026, 10, 1, 9, 0))
        _moved(lead, 'Under Negotiation', datetime(2026, 10, 20, 9, 0))
        real = {date(2026, 9, 1), date(2026, 10, 1), date(2026, 10, 20)}
        for row in ld.for_lead(lead):
            if row['source'] == ld.ESTIMATED:
                assert row['date'] in real, (
                    f'{row["key"]} was estimated to {row["date"]}, '
                    f'which is not a date on this lead')


def test_the_summary_counts_the_rungs():
    with flask_app.app_context():
        lead = _lead(stage='Quoted', rfq_date=date(2026, 9, 20))
        _moved(lead, 'Quoted', datetime(2026, 10, 1, 9, 0))
        out = ld.summary(lead)
        assert out['total'] == len(ld.MILESTONES)
        assert sum(out['counts'].values()) == out['total']
        assert out['known'] == out['total'] - out['counts'][ld.UNKNOWN]


# ═══════════════════════════════════════════════════════════════════
# §6 — the team
# ═══════════════════════════════════════════════════════════════════
def test_no_employee_code_is_written_into_the_module():
    """§6: "no names in code". Asserted on the source rather than
    trusted: a code added later for one urgent case is exactly how
    this rule stops holding."""
    import re

    source = open(os.path.join(_ROOT, 'app', 'services',
                               'lead_team.py')).read()
    body = source.split('"""', 2)[-1]
    # Employee codes in this CRM are letters then digits, e.g. the
    # director's. Any such literal in the code is the thing banned.
    found = re.findall(r"'[A-Z]{3}[A-Z]*\d{3,}'", body)
    assert not found, f'employee codes in lead_team.py: {found}'


def test_the_owner_and_the_second_owner_come_off_the_lead():
    with flask_app.app_context():
        _emp(OWNER, 'Owner Person')
        _emp(SECOND, 'Second Person')
        lead = _lead(assigned_to=OWNER, secondary_owner=SECOND)
        team = lt.for_lead(lead)
        assert [m['emp_code'] for m in team][:2] == [OWNER, SECOND]
        assert team[0]['role_key'] == lt.OWNER
        assert team[1]['role_key'] == lt.SECOND


def test_one_person_holding_two_roles_is_one_row():
    """Two rows for one person reads as two people, and the first
    thing anybody does with this list is count it."""
    with flask_app.app_context():
        _emp(OWNER, 'Owner Person')
        company = Company(name='ZFacts Client', pic_emp_code=OWNER)
        db.session.add(company)
        db.session.commit()
        lead = _lead(assigned_to=OWNER, company_id=company.id)
        team = lt.for_lead(lead)
        assert [m['emp_code'] for m in team] == [OWNER]
        keys = [r['key'] for r in team[0]['roles']]
        assert lt.OWNER in keys and lt.ACCOUNT_OWNER in keys
        # The strongest role is the one shown.
        assert team[0]['role_key'] == lt.OWNER


def test_a_task_holder_is_on_the_team():
    with flask_app.app_context():
        _emp(TASKER, 'Task Person')
        lead = _lead()
        db.session.add(TaskInstance(
            task_key='rfq.register', entity_type='Lead',
            entity_id=lead.id, owner_user_id=TASKER,
            status=TaskInstanceStatus.OPEN[0]))
        db.session.commit()
        codes = [m['emp_code'] for m in lt.for_lead(lead)]
        assert TASKER in codes


def test_a_closed_task_does_not_put_somebody_on_the_team():
    """Being able to open a lead you once worked is different from
    being someone to ask about it today."""
    with flask_app.app_context():
        _emp(TASKER, 'Task Person')
        lead = _lead()
        db.session.add(TaskInstance(
            task_key='rfq.register', entity_type='Lead',
            entity_id=lead.id, owner_user_id=TASKER,
            status=TaskInstanceStatus.CANCELLED))
        db.session.commit()
        assert TASKER not in [m['emp_code'] for m in lt.for_lead(lead)]


def test_the_account_owner_is_named_with_the_account():
    with flask_app.app_context():
        _emp(ACCT, 'Account Person')
        company = Company(name='ZFacts Big Client', pic_emp_code=ACCT)
        db.session.add(company)
        db.session.commit()
        lead = _lead(company_id=company.id)
        member = [m for m in lt.for_lead(lead)
                  if m['emp_code'] == ACCT][0]
        assert member['role_key'] == lt.ACCOUNT_OWNER
        assert 'ZFacts Big Client' in member['roles'][0]['detail']


def test_an_unowned_lead_says_so_rather_than_showing_an_empty_row():
    """Half the book is unowned. That is a fact to state, not a gap
    to render as whitespace."""
    with flask_app.app_context():
        lead = _lead()
        out = lt.summary(lead)
        assert out['unowned'] is True
        assert out['accountable'] == 0


def test_an_inactive_member_is_listed_and_flagged():
    """Dropping a leaver would make the lead look owned when the
    person it names has gone."""
    with flask_app.app_context():
        _emp(OWNER, 'Left Person', active=False)
        lead = _lead(assigned_to=OWNER)
        out = lt.summary(lead)
        assert OWNER in out['inactive']
        assert out['team'][0]['is_active'] is False


def test_a_code_with_no_employee_record_is_not_hidden():
    with flask_app.app_context():
        lead = _lead(assigned_to='ZF_GONE')
        out = lt.summary(lead)
        assert 'ZF_GONE' in out['unknown_codes']
        assert 'no employee record' in out['team'][0]['name']


def test_membership_grants_nothing():
    """A directory, not a permission. Asserted on the imports,
    because the way this becomes a permission is somebody calling it
    from a gate."""
    for path in ('app/access/scope.py', 'app/access/service.py',
                 'app/access/records.py'):
        source = open(os.path.join(_ROOT, path)).read()
        assert 'lead_team' not in source, (
            f'{path} imports lead_team — the team is a directory, '
            f'and reading it in a gate makes it a permission')


def test_access_is_asked_of_scope_and_not_recomputed():
    with flask_app.app_context():
        _emp(OWNER, 'Owner Person')
        lead = _lead(assigned_to=OWNER)
        team = lt.for_lead(lead, check_access=True)
        assert 'can_open' in team[0]
        assert team[0]['can_open'] in (True, False, None)


# ═══════════════════════════════════════════════════════════════════
# §7 — the quotation file
# ═══════════════════════════════════════════════════════════════════
def test_a_quotation_is_attached_to_its_lead():
    with flask_app.app_context():
        lead = _lead()
        out = lqf.attach(lead, filename='Quotation.pdf',
                         blob=b'%PDF-1.4 fake', content_type='application/pdf',
                         actor=OWNER)
        assert out['attachment']['filename'] == 'Quotation.pdf'
        rows = LeadAttachment.query.filter_by(lead_id=lead.id).all()
        assert len(rows) == 1
        assert rows[0].source == lqf.SOURCE
        assert os.path.exists(rows[0].storage_path)


def test_two_files_of_the_same_name_do_not_overwrite():
    """Two revisions are routinely both called Quotation.pdf."""
    with flask_app.app_context():
        lead = _lead()
        lqf.attach(lead, filename='Quotation.pdf', blob=b'one', actor=OWNER)
        lqf.attach(lead, filename='Quotation.pdf', blob=b'two', actor=OWNER)
        rows = LeadAttachment.query.filter_by(lead_id=lead.id).all()
        assert len({r.storage_path for r in rows}) == 2
        bodies = set()
        for r in rows:
            with open(r.storage_path, 'rb') as fh:
                bodies.add(fh.read())
        assert bodies == {b'one', b'two'}


def test_the_number_and_date_fill_a_blank():
    with flask_app.app_context():
        lead = _lead()
        out = lqf.attach(lead, filename='q.pdf', blob=b'x',
                         quote_no='QUO-000123',
                         quote_date='2026-11-05', actor=OWNER)
        assert out['stamped']['quote_no'] == 'QUO-000123'
        assert lead.quote_no == 'QUO-000123'
        assert lead.quote_date == date(2026, 11, 5)


def test_an_existing_number_is_never_overwritten():
    """How the CRM comes to disagree with the quotation register."""
    with flask_app.app_context():
        lead = _lead(quote_no='QUO-000001', quote_date=date(2026, 10, 1))
        out = lqf.attach(lead, filename='q.pdf', blob=b'x',
                         quote_no='QUO-999999',
                         quote_date='2026-11-05', actor=OWNER)
        assert out['stamped'] == {}
        assert lead.quote_no == 'QUO-000001'
        assert lead.quote_date == date(2026, 10, 1)


def test_a_date_it_cannot_read_is_refused_not_guessed():
    with flask_app.app_context():
        lead = _lead()
        with pytest.raises(lqf.Refused):
            lqf.attach(lead, filename='q.pdf', blob=b'x',
                       quote_date='next Tuesday', actor=OWNER)
        assert LeadAttachment.query.filter_by(lead_id=lead.id).count() == 0


def test_the_upload_does_not_move_the_stage():
    """A stage change starts the quote-ageing clock. Inferring one
    from a file would move leads on the strength of a draft."""
    with flask_app.app_context():
        lead = _lead(stage='RFQ Generated')
        out = lqf.attach(lead, filename='q.pdf', blob=b'x', actor=OWNER)
        assert lead.stage == 'RFQ Generated'
        assert 'Quoted' in out['stage_hint']


def test_no_hint_when_the_lead_is_already_quoted():
    with flask_app.app_context():
        lead = _lead(stage='Quoted')
        out = lqf.attach(lead, filename='q.pdf', blob=b'x', actor=OWNER)
        assert out['stage_hint'] == ''


def test_the_value_columns_are_not_touched():
    """`quote_value_num` is governed by value_gate, with a margin
    floor and an approval limit behind it."""
    with flask_app.app_context():
        lead = _lead()
        lqf.attach(lead, filename='q.pdf', blob=b'x',
                   note='worth about 12 lakh', actor=OWNER)
        assert getattr(lead, 'quote_value_num', None) is None

    source = open(os.path.join(_ROOT, 'app', 'services',
                               'lead_quote_file.py')).read()
    body = source.split('"""', 2)[-1]
    for column in ('quote_value_num', 'quoted_amount_inr',
                   'estimated_value_inr'):
        assert f'.{column} =' not in body, column


def test_it_leaves_a_timeline_entry_and_an_audit_row():
    with flask_app.app_context():
        lead = _lead()
        lqf.attach(lead, filename='q.pdf', blob=b'x', actor=OWNER)
        acts = LeadActivity.query.filter_by(lead_id=lead.id).all()
        assert [a.kind for a in acts] == ['quote']
        rows = AuditEvent.query.filter_by(
            action='lead.quote_file_attached',
            entity_id=str(lead.id)).all()
        assert len(rows) == 1


def test_the_timeline_entry_dates_the_quote_milestone():
    """§5 and §7 meeting: the activity this writes is what makes the
    quotation date knowable on a lead nobody typed one into."""
    with flask_app.app_context():
        lead = _lead(stage='Quoted')
        lqf.attach(lead, filename='q.pdf', blob=b'x', actor=OWNER)
        assert _row(lead, 'quote')['source'] in (ld.AUDIT, ld.ENTERED)


def test_a_failed_write_leaves_no_file_behind():
    """A row pointing at no file is a broken download; a file with no
    row is litter. Neither is left."""
    with flask_app.app_context():
        lead = _lead()
        root = lqf.storage_root()
        import app.services.lead_quote_file as module

        def _boom(*a, **kw):
            raise RuntimeError('the database said no')

        real = module.audit.record if hasattr(module, 'audit') else None
        from app.services import audit as audit_module
        saved = audit_module.record
        audit_module.record = _boom
        try:
            with pytest.raises(RuntimeError):
                lqf.attach(lead, filename='q.pdf', blob=b'x', actor=OWNER)
        finally:
            audit_module.record = saved
        db.session.rollback()
        assert LeadAttachment.query.filter_by(lead_id=lead.id).count() == 0
        folder = os.path.join(root, str(lead.id))
        left = os.listdir(folder) if os.path.isdir(folder) else []
        assert left == [], f'left behind: {left}'


# ═══════════════════════════════════════════════════════════════════
# the routes
# ═══════════════════════════════════════════════════════════════════
def _client(code):
    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess['emp_code'] = code
        sess['role'] = 'admin'
    return client


def _token(client):
    """The token a browser would read out of the cookie.

    Spelled out rather than skipped: a POST test that does not send
    this passes for the wrong reason the day the gate breaks, and a
    write with no token is the failure this CRM has shipped six times.
    """
    r = client.get('/my-work')
    for header in r.headers.getlist('Set-Cookie'):
        if header.startswith('csrf_token='):
            return header.split('=', 1)[1].split(';')[0]
    jar = getattr(client, 'cookie_jar', None) or []
    for cookie in jar:
        if cookie.name == 'csrf_token':
            return cookie.value
    return ''


def test_the_three_routes_answer_for_a_signed_in_admin():
    with flask_app.app_context():
        _emp('ZF_ADM', 'Admin Person', role='admin')
        lead = _lead(assigned_to='ZF_ADM', stage='Quoted')
        lid = lead.id
    client = _client('ZF_ADM')
    for path in (f'/api/leads/{lid}/dates',
                 f'/api/leads/{lid}/team',
                 f'/api/leads/{lid}/quote-files'):
        r = client.get(path)
        assert r.status_code == 200, (path, r.status_code)
        assert r.get_json()['ok'] is True


def test_the_routes_refuse_a_stranger():
    with flask_app.app_context():
        lead = _lead()
        lid = lead.id
    client = flask_app.test_client()
    for path in (f'/api/leads/{lid}/dates', f'/api/leads/{lid}/team'):
        assert client.get(path).status_code == 401, path


def test_a_date_is_a_string_or_empty_never_the_word_none():
    with flask_app.app_context():
        _emp('ZF_ADM', 'Admin Person', role='admin')
        lead = _lead(assigned_to='ZF_ADM')
        lid = lead.id
    body = _client('ZF_ADM').get(f'/api/leads/{lid}/dates').get_json()
    for row in body['dates']:
        assert isinstance(row['date'], str)
        assert row['date'] != 'None'


def test_the_upload_route_refuses_a_file_it_cannot_store():
    import io

    with flask_app.app_context():
        _emp('ZF_ADM', 'Admin Person', role='admin')
        lead = _lead(assigned_to='ZF_ADM')
        lid = lead.id
    client = _client('ZF_ADM')
    r = client.post(f'/api/leads/{lid}/quote-files',
                    data={'file': (io.BytesIO(b'x'), 'quote.exe')},
                    content_type='multipart/form-data',
                    headers={'X-CSRFToken': _token(client)})
    assert r.status_code == 400
    assert '.exe' in r.get_json()['error']


def test_the_upload_route_stores_a_pdf():
    import io

    with flask_app.app_context():
        _emp('ZF_ADM', 'Admin Person', role='admin')
        lead = _lead(assigned_to='ZF_ADM')
        lid = lead.id
    client = _client('ZF_ADM')
    r = client.post(f'/api/leads/{lid}/quote-files',
                    data={'file': (io.BytesIO(b'%PDF-1.4'), 'Quotation.pdf'),
                          'quote_no': 'QUO-000777'},
                    content_type='multipart/form-data',
                    headers={'X-CSRFToken': _token(client)})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body['ok'] is True
    assert body['stamped']['quote_no'] == 'QUO-000777'
    assert len(body['files']) == 1


def test_the_upload_refuses_a_post_with_no_token():
    """The failure this CRM has shipped six times: a write with no
    token does nothing and says nothing.

    `WTF_CSRF_ENABLED` is forced on for the duration. Ten test
    modules switch it off on the shared app object and do not put it
    back, so whether this route is protected would otherwise depend
    on which of them pytest happened to run first — and a security
    assertion that passes for that reason is not an assertion.
    """
    import io

    with flask_app.app_context():
        _emp('ZF_ADM', 'Admin Person', role='admin')
        lead = _lead(assigned_to='ZF_ADM')
        lid = lead.id
    was = flask_app.config.get('WTF_CSRF_ENABLED', True)
    flask_app.config['WTF_CSRF_ENABLED'] = True
    try:
        r = _client('ZF_ADM').post(
            f'/api/leads/{lid}/quote-files',
            data={'file': (io.BytesIO(b'%PDF-1.4'), 'q.pdf')},
            content_type='multipart/form-data')
    finally:
        flask_app.config['WTF_CSRF_ENABLED'] = was
    assert r.status_code == 400, r.get_json()
    assert r.get_json()['code'] == 'csrf'
    with flask_app.app_context():
        assert LeadAttachment.query.filter_by(lead_id=lid).count() == 0


# ═══════════════════════════════════════════════════════════════════
# §8 — the prefix that 404ed
# ═══════════════════════════════════════════════════════════════════
def test_the_proxy_prefix_is_stripped_off_the_root():
    assert _main._strip_url_prefix('/CRM/', '/CRM') == '/'
    assert _main._strip_url_prefix('/CRM', '/CRM') == '/'


def test_a_deeper_path_keeps_its_rest():
    assert _main._strip_url_prefix('/CRM/my-work', '/CRM') == '/my-work'
    assert _main._strip_url_prefix('/CRM/api/leads', '/CRM') == '/api/leads'


def test_a_route_that_merely_starts_with_the_same_letters_survives():
    """`/crm2` is a real route. Stripping four characters off it
    would route the cockpit to nothing."""
    assert _main._strip_url_prefix('/crm2', '/CRM') == '/crm2'
    assert _main._strip_url_prefix('/CRM2', '/CRM') == '/CRM2'


def test_nothing_is_stripped_when_there_is_no_prefix():
    assert _main._strip_url_prefix('/CRM/', '') == '/CRM/'
    assert _main._strip_url_prefix('/my-work', '') == '/my-work'


def test_the_shim_uses_the_shared_function():
    """So the behaviour the tests above pin is the behaviour the
    deployed middleware has."""
    import inspect

    source = inspect.getsource(_main)
    index = source.index('def _force_script_name')
    assert '_strip_url_prefix' in source[index:index + 400]
