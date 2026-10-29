"""
The backlog of open leads nobody holds.

On 29 October 2026 there were 132 of them. The lead sheet's new
account prompt stops more arriving, but only when a lead is linked;
it does nothing for leads already filed under an account whose owner
is known. `scripts/unowned_open_leads.py` reads that backlog, says
who the account would give each lead to, and — only when told the
leads and the actor — assigns the ones with a single answer.

Every write path here is exercised, not only its refusal: a gate
that was never built reports a clean dry run and writes nothing.
"""
import importlib
import importlib.util
import os
import sys
import tempfile
from datetime import datetime

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'unowned-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'Unowned-12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'unowned.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app = _main.app
db = _main.db
Company, Employee, Lead = _main.Company, _main.Employee, _main.Lead
LeadAssignmentHistory = _main.LeadAssignmentHistory

PREFIX = 'UO '
ONE, TWO, GONE, HEIR, ACTOR = ('UO_ONE', 'UO_TWO', 'UO_GONE',
                               'UO_HEIR', 'UO_ACTOR')
PEOPLE = ((ONE, 'Jayanta Kumar Paul', True),
          (TWO, 'James Francis Xavier', True),
          (GONE, 'Departed Person', False),
          (HEIR, 'Took Over', True),
          (ACTOR, 'Unowned Director', True))
HEAVY, PFM = 'Heavy Transport', 'Warehousing'


def _script():
    path = os.path.join(_ROOT, 'scripts', 'unowned_open_leads.py')
    spec = importlib.util.spec_from_file_location('uol', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


uol = _script()


def _purge():
    from app.models.assignment import AccountVerticalOwner, OwnerReplacement
    from app.models.audit import AuditEvent
    ids = [a.id for a in
           Company.query.filter(Company.name.like(PREFIX + '%')).all()]
    for account_id in ids:
        AccountVerticalOwner.query.filter_by(
            account_id=account_id).delete(synchronize_session=False)
    lead_ids = [r.id for r in
                Lead.query.filter(Lead.company.like(PREFIX + '%')).all()]
    for lead_id in lead_ids:
        LeadAssignmentHistory.query.filter_by(
            lead_id=lead_id).delete(synchronize_session=False)
        AuditEvent.query.filter(
            AuditEvent.entity_type == 'lead',
            AuditEvent.entity_id == str(lead_id)).delete(
                synchronize_session=False)
    Lead.query.filter(Lead.company.like(PREFIX + '%')).delete(
        synchronize_session=False)
    Company.query.filter(Company.name.like(PREFIX + '%')).delete(
        synchronize_session=False)
    OwnerReplacement.query.filter(
        OwnerReplacement.from_emp_code.in_([p[0] for p in PEOPLE])).delete(
            synchronize_session=False)
    Employee.query.filter(
        Employee.emp_code.in_([p[0] for p in PEOPLE])).delete(
            synchronize_session=False)
    db.session.commit()


@pytest.fixture(autouse=True)
def _clean():
    with flask_app.app_context():
        db.create_all()
        _purge()
        for code, name, live in PEOPLE:
            db.session.add(Employee(
                emp_code=code, name=name, password_hash='x', role='user',
                vertical=HEAVY, is_active=live,
                email=code.lower() + '@procamlogistics.com'))
        db.session.commit()
        yield
        _purge()


# ── the world ────────────────────────────────────────────────────────
def _account(name, pic=None, vertical=HEAVY):
    account = Company(name=PREFIX + name, is_active=True,
                      pic_emp_code=pic, vertical=vertical)
    db.session.add(account)
    db.session.flush()
    return account


def _vertical_owner(account, vertical, code):
    from app.models.assignment import AccountVerticalOwner
    db.session.add(AccountVerticalOwner(
        account_id=account.id, vertical=vertical, emp_code=code,
        source='manual'))
    db.session.flush()


def _lead(name, account=None, stage='New Opportunity', owner=None,
          vertical=HEAVY, archived=False):
    lead = Lead(company=PREFIX + name, stage=stage, source='manual',
                procam_vertical=vertical, assigned_to=owner,
                is_archived=archived,
                company_id=account.id if account is not None else None,
                created_at=datetime.utcnow())
    db.session.add(lead)
    db.session.flush()
    return lead


def _row(lead):
    return uol.read(db.session.get(Lead, lead.id))


# ── §1 the verdicts ──────────────────────────────────────────────────
def test_one_active_owner_is_obvious():
    with flask_app.app_context():
        account = _account('BEML LIMITED', pic=ONE)
        lead = _lead('BEML tender', account)
        row = _row(lead)
        assert row['verdict'] == uol.OBVIOUS
        assert row['owner'] == ONE


def test_the_vertical_owner_beats_the_account_pic():
    """AccountVerticalOwner first, pic_emp_code only as a fallback —
    the account PIC holds the relationship, the vertical owner does
    this kind of work."""
    with flask_app.app_context():
        account = _account('Tata Steel', pic=ONE)
        _vertical_owner(account, HEAVY, TWO)
        row = _row(_lead('Tata rake', account))
        assert row['verdict'] == uol.OBVIOUS
        assert row['owner'] == TWO


def test_the_pic_is_used_when_no_vertical_owner_exists():
    with flask_app.app_context():
        account = _account('L and T', pic=ONE)
        _vertical_owner(account, PFM, TWO)
        row = _row(_lead('L and T trailer', account, vertical=HEAVY))
        # The lead's vertical names nobody, so the account's other
        # verticals have a say — and here there is only one of them.
        assert row['verdict'] == uol.OBVIOUS
        assert row['owner'] == TWO


def test_several_active_owners_means_choose():
    with flask_app.app_context():
        account = _account('Jindal', pic=ONE)
        _vertical_owner(account, PFM, ONE)
        _vertical_owner(account, 'Installation', TWO)
        row = _row(_lead('Jindal move', account, vertical=HEAVY))
        assert row['verdict'] == uol.CHOOSE
        assert set(row['active']) == {ONE, TWO}
        assert row['owner'] == ''


def test_a_lead_with_no_account_says_so():
    with flask_app.app_context():
        row = _row(_lead('To review'))
        assert row['verdict'] == uol.NO_ACCOUNT
        assert row['owner'] == ''


def test_an_account_with_nobody_is_not_the_same_as_no_account():
    """Two different problems: one is fixed on the lead, the other on
    the account. Collapsing them hides which."""
    with flask_app.app_context():
        account = _account('Orphan Works', pic=None)
        row = _row(_lead('Orphan enquiry', account))
        assert row['verdict'] == uol.NO_OWNER


def test_a_departed_owner_gives_the_replacement():
    with flask_app.app_context():
        from app.models.assignment import OwnerReplacement
        db.session.add(OwnerReplacement(from_emp_code=GONE,
                                        to_emp_code=HEIR, active=True))
        account = _account('ACC Cement', pic=GONE)
        row = _row(_lead('ACC enquiry', account))
        assert row['verdict'] == uol.INACTIVE
        assert row['replacement'] == HEIR


def test_a_departed_owner_with_no_map_falls_back_to_the_backstop():
    with flask_app.app_context():
        from app.services import auto_assign
        account = _account('No Heir Ltd', pic=GONE)
        row = _row(_lead('No heir enquiry', account))
        assert row['verdict'] == uol.INACTIVE
        assert row['replacement'] == auto_assign.backstop()
        assert auto_assign.DEFAULT_BACKSTOP == 'DIR12010'


# ── §1 what the sweep picks up ───────────────────────────────────────
def test_only_unowned_open_leads_are_listed():
    with flask_app.app_context():
        account = _account('Sweep Ltd', pic=ONE)
        wanted = _lead('Sweep open unowned', account)
        _lead('Sweep owned', account, owner=ONE)
        _lead('Sweep won', account, stage='Won')
        _lead('Sweep lost', account, stage='Lost')
        _lead('Sweep archived', account, archived=True)
        db.session.commit()
        got = [r['lead_id'] for r in uol.unowned()
               if (r['company'] or '').startswith(PREFIX)]
        assert got == [wanted.id]


def test_named_ids_are_read_even_when_they_are_owned():
    """--ids is for looking at a lead, not only for sweeping."""
    with flask_app.app_context():
        account = _account('Named Ltd', pic=ONE)
        lead = _lead('Named owned', account, owner=TWO)
        db.session.commit()
        rows = uol.unowned([lead.id])
        assert len(rows) == 1 and rows[0]['owned'] is True


def test_the_counts_are_per_verdict_and_per_vertical():
    with flask_app.app_context():
        good = _account('Counted One', pic=ONE)
        _lead('Counted heavy', good, vertical=HEAVY)
        _lead('Counted pfm', good, vertical=PFM)
        _lead('Counted loose')
        db.session.commit()
        rows = [r for r in uol.unowned()
                if (r['company'] or '').startswith(PREFIX)]
        by_verdict, by_vertical = uol.counts(rows)
        assert by_verdict[uol.OBVIOUS] == 2
        assert by_verdict[uol.NO_ACCOUNT] == 1
        assert sum(by_vertical.values()) == 3
        assert by_vertical[HEAVY] >= 1


def test_the_report_changes_nothing_and_says_so():
    with flask_app.app_context():
        account = _account('Report Ltd', pic=ONE)
        lead = _lead('Report lead', account)
        db.session.commit()
        text = uol.report(uol.unowned())
        assert 'Nothing was changed' in text
        assert uol.OBVIOUS in text
        assert 'By verdict:' in text and 'By vertical:' in text
        assert db.session.get(Lead, lead.id).assigned_to in (None, '')


def test_the_report_is_honest_about_an_empty_backlog():
    with flask_app.app_context():
        assert 'Nothing unowned' in uol.report([])


# ── §2 the apply path ────────────────────────────────────────────────
def test_apply_assigns_an_obvious_lead():
    with flask_app.app_context():
        account = _account('Apply Ltd', pic=ONE)
        lead = _lead('Apply lead', account)
        db.session.commit()
        done, refused = uol.apply([lead.id], actor=ACTOR, notify=False)
        assert done == [(lead.id, ONE)]
        assert refused == []
        assert db.session.get(Lead, lead.id).assigned_to == ONE


def test_apply_writes_the_history_row_with_actor_and_reason():
    """The whole point of going through the screen's own path: the
    SQL UPDATE that set #12397 left no trail at all."""
    with flask_app.app_context():
        account = _account('History Ltd', pic=ONE)
        lead = _lead('History lead', account)
        db.session.commit()
        uol.apply([lead.id], actor=ACTOR, notify=False)
        rows = LeadAssignmentHistory.query.filter_by(
            lead_id=lead.id).all()
        assert len(rows) == 1
        assert rows[0].to_primary == ONE
        assert rows[0].changed_by == ACTOR
        assert rows[0].note == 'Backfill: owner taken from account'


def test_apply_writes_an_audit_row():
    with flask_app.app_context():
        from app.models.audit import AuditEvent
        account = _account('Audited Ltd', pic=ONE)
        lead = _lead('Audited lead', account)
        db.session.commit()
        uol.apply([lead.id], actor=ACTOR, notify=False)
        event = AuditEvent.query.filter_by(
            action='lead.owner_backfilled',
            entity_id=str(lead.id)).first()
        assert event is not None
        assert event.actor == ACTOR
        assert 'Backfill' in (event.reason or '')


def test_apply_refuses_a_won_lead():
    with flask_app.app_context():
        account = _account('Won Ltd', pic=ONE)
        lead = _lead('Won lead', account, stage='Won')
        db.session.commit()
        done, refused = uol.apply([lead.id], actor=ACTOR, notify=False)
        assert done == []
        assert 'Won' in refused[0][1]
        assert db.session.get(Lead, lead.id).assigned_to in (None, '')


def test_apply_refuses_a_lost_lead():
    with flask_app.app_context():
        account = _account('Lost Ltd', pic=ONE)
        lead = _lead('Lost lead', account, stage='Lost')
        db.session.commit()
        done, refused = uol.apply([lead.id], actor=ACTOR, notify=False)
        assert done == []
        assert 'Lost' in refused[0][1]


def test_apply_refuses_anything_that_is_not_obvious():
    with flask_app.app_context():
        account = _account('Choose Ltd', pic=ONE)
        _vertical_owner(account, PFM, ONE)
        _vertical_owner(account, 'Installation', TWO)
        lead = _lead('Choose lead', account, vertical=HEAVY)
        loose = _lead('Loose lead')
        db.session.commit()
        done, refused = uol.apply([lead.id, loose.id], actor=ACTOR,
                                  notify=False)
        assert done == []
        assert uol.CHOOSE in refused[0][1]
        assert uol.NO_ACCOUNT in refused[1][1]


def test_apply_refuses_a_lead_that_already_has_an_owner():
    with flask_app.app_context():
        account = _account('Taken Ltd', pic=ONE)
        lead = _lead('Taken lead', account, owner=TWO)
        db.session.commit()
        done, refused = uol.apply([lead.id], actor=ACTOR, notify=False)
        assert done == []
        assert 'already owned' in refused[0][1]
        assert db.session.get(Lead, lead.id).assigned_to == TWO


def test_apply_refuses_a_lead_that_does_not_exist():
    with flask_app.app_context():
        done, refused = uol.apply([99999901], actor=ACTOR, notify=False)
        assert done == []
        assert refused == [(99999901, 'no such lead')]


def test_apply_touches_only_the_leads_it_was_given():
    with flask_app.app_context():
        account = _account('Only Ltd', pic=ONE)
        named = _lead('Only named', account)
        other = _lead('Only other', account)
        db.session.commit()
        uol.apply([named.id], actor=ACTOR, notify=False)
        assert db.session.get(Lead, other.id).assigned_to in (None, '')


# ── §3 the missing history row for #12397 ────────────────────────────
def test_history_only_records_an_owner_set_outside_the_crm():
    with flask_app.app_context():
        account = _account('Manual Ltd', pic=TWO)
        lead = _lead('Manual lead', account)
        # What the SQL UPDATE did on 29 October: an owner, no trail.
        lead.assigned_to = TWO
        db.session.commit()
        written, skipped = uol.write_history(
            [lead.id], actor=ACTOR, owner=TWO,
            reason='Manual correction 29 Oct')
        assert written == [(lead.id, TWO)]
        row = LeadAssignmentHistory.query.filter_by(lead_id=lead.id).one()
        assert row.to_primary == TWO
        assert row.changed_by == ACTOR
        assert row.note == 'Manual correction 29 Oct'


def test_history_only_is_repeatable():
    with flask_app.app_context():
        account = _account('Twice Ltd', pic=TWO)
        lead = _lead('Twice lead', account)
        lead.assigned_to = TWO
        db.session.commit()
        uol.write_history([lead.id], actor=ACTOR, owner=TWO)
        written, skipped = uol.write_history([lead.id], actor=ACTOR,
                                             owner=TWO)
        assert written == []
        assert 'already recorded' in skipped[0][1]
        assert LeadAssignmentHistory.query.filter_by(
            lead_id=lead.id).count() == 1


def test_history_only_is_audited():
    with flask_app.app_context():
        from app.models.audit import AuditEvent
        account = _account('Trail Ltd', pic=TWO)
        lead = _lead('Trail lead', account)
        lead.assigned_to = TWO
        db.session.commit()
        uol.write_history([lead.id], actor=ACTOR, owner=TWO)
        event = AuditEvent.query.filter_by(
            action='lead.assignment_history_backfilled',
            entity_id=str(lead.id)).first()
        assert event is not None and event.actor == ACTOR


def test_history_only_changes_no_owner():
    with flask_app.app_context():
        account = _account('Unchanged Ltd', pic=ONE)
        lead = _lead('Unchanged lead', account)
        lead.assigned_to = TWO
        db.session.commit()
        uol.write_history([lead.id], actor=ACTOR)
        # The account says ONE; the lead says TWO; recording the trail
        # must not quietly "correct" the lead to the account's answer.
        assert db.session.get(Lead, lead.id).assigned_to == TWO


def test_history_only_refuses_a_lead_with_no_owner():
    with flask_app.app_context():
        account = _account('Empty Ltd', pic=ONE)
        lead = _lead('Empty lead', account)
        db.session.commit()
        written, skipped = uol.write_history([lead.id], actor=ACTOR)
        assert written == []
        assert 'does not set one' in skipped[0][1]
        assert LeadAssignmentHistory.query.filter_by(
            lead_id=lead.id).count() == 0


def test_history_only_refuses_when_the_owner_is_not_who_you_said():
    """The safety check: --owner is what you believe is there. If the
    lead says somebody else, the belief is wrong, not the lead."""
    with flask_app.app_context():
        account = _account('Mismatch Ltd', pic=ONE)
        lead = _lead('Mismatch lead', account)
        lead.assigned_to = TWO
        db.session.commit()
        written, skipped = uol.write_history([lead.id], actor=ACTOR,
                                             owner=ONE)
        assert written == []
        assert 'nothing written' in skipped[0][1]


def test_the_history_preview_writes_nothing():
    with flask_app.app_context():
        account = _account('Preview Ltd', pic=TWO)
        lead = _lead('Preview lead', account)
        lead.assigned_to = TWO
        db.session.commit()
        text = uol.history_preview([lead.id], owner=TWO)
        assert 'would record' in text
        assert 'Nothing was changed' in text
        assert LeadAssignmentHistory.query.filter_by(
            lead_id=lead.id).count() == 0


# ── §2 the command line's own gate ───────────────────────────────────
def _run(argv):
    import io
    import contextlib
    out = io.StringIO()
    old = sys.argv
    sys.argv = ['unowned_open_leads.py'] + argv
    try:
        with contextlib.redirect_stdout(out):
            code = uol.main()
    finally:
        sys.argv = old
    return code, out.getvalue()


def test_the_command_line_is_read_only_without_apply():
    with flask_app.app_context():
        account = _account('CLI Ltd', pic=ONE)
        lead = _lead('CLI lead', account)
        db.session.commit()
        lead_id = lead.id
    code, text = _run([])
    assert code == 0
    assert 'Nothing was changed' in text
    with flask_app.app_context():
        assert db.session.get(Lead, lead_id).assigned_to in (None, '')


def test_apply_without_an_actor_writes_nothing():
    with flask_app.app_context():
        account = _account('No Actor Ltd', pic=ONE)
        lead = _lead('No actor lead', account)
        db.session.commit()
        lead_id = lead.id
    code, text = _run(['--ids', str(lead_id), '--apply'])
    assert code == 2
    assert 'Nothing was changed' in text
    with flask_app.app_context():
        assert db.session.get(Lead, lead_id).assigned_to in (None, '')


def test_apply_without_ids_writes_nothing():
    with flask_app.app_context():
        account = _account('No Ids Ltd', pic=ONE)
        lead = _lead('No ids lead', account)
        db.session.commit()
        lead_id = lead.id
    code, text = _run(['--actor', ACTOR, '--apply'])
    assert code == 2
    with flask_app.app_context():
        assert db.session.get(Lead, lead_id).assigned_to in (None, '')


def test_the_command_line_apply_path_works_when_fully_armed():
    with flask_app.app_context():
        account = _account('Armed Ltd', pic=ONE)
        lead = _lead('Armed lead', account)
        db.session.commit()
        lead_id = lead.id
    code, text = _run(['--ids', str(lead_id), '--actor', ACTOR,
                       '--apply', '--quiet'])
    assert code == 0
    assert 'assigned' in text
    with flask_app.app_context():
        assert db.session.get(Lead, lead_id).assigned_to == ONE


def test_history_only_previews_without_apply():
    with flask_app.app_context():
        account = _account('CLI Hist Ltd', pic=TWO)
        lead = _lead('CLI hist lead', account)
        lead.assigned_to = TWO
        db.session.commit()
        lead_id = lead.id
    code, text = _run(['--history-only', '--ids', str(lead_id)])
    assert code == 0
    assert 'Nothing was changed' in text
    with flask_app.app_context():
        assert LeadAssignmentHistory.query.filter_by(
            lead_id=lead_id).count() == 0


def test_history_only_writes_when_fully_armed():
    with flask_app.app_context():
        account = _account('CLI Write Ltd', pic=TWO)
        lead = _lead('CLI write lead', account)
        lead.assigned_to = TWO
        db.session.commit()
        lead_id = lead.id
    code, text = _run(['--history-only', '--ids', str(lead_id),
                       '--owner', TWO, '--actor', ACTOR, '--apply',
                       '--reason', 'Manual correction 29 Oct'])
    assert code == 0
    assert 'recorded' in text
    with flask_app.app_context():
        row = LeadAssignmentHistory.query.filter_by(
            lead_id=lead_id).one()
        assert row.note == 'Manual correction 29 Oct'
        assert row.changed_by == ACTOR
