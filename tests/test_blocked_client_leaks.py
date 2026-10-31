"""
scripts/blocked_client_leaks.py — leads the gate should have refused.

The register's own audit trail records refusals, not checks that
silently passed, so a gate that failed open leaves no trace. This
script is the only way to find out whether it let anything through,
which makes "does it actually find one" worth holding with tests.

Everything here is read-only, and one test says so by counting the
rows before and after.
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'leak-test-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'LeakTest12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'leaks.db'))

import importlib                                          # noqa: E402
import importlib.util                                     # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Company, Lead = _main.Company, _main.Lead

from app.models.restriction import (                      # noqa: E402
    BLOCKED, CAUTION, ClientRestriction, ClientRestrictionEvent,
    SCOPE_ENTITY, SCOPE_GROUP)
from app.services import client_restrictions as restrictions  # noqa: E402

# Loaded by path, not by putting scripts/ on sys.path: scripts/ holds
# an `email_ingest.py` that would shadow the `email_ingest` package the
# app imports, and the failure looks nothing like its cause.
_spec = importlib.util.spec_from_file_location(
    'blocked_client_leaks',
    os.path.join(_ROOT, 'scripts', 'blocked_client_leaks.py'))
leaks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(leaks)

WIL = 'Walchandnagar Industries Limited'
TAG = 'LEAK-'
#: The gate went live on the morning of 31 Oct 2026 IST.
SINCE = '2026-10-31'
#: 31 Oct 2026, 09:10 IST — inside the window, as stored (UTC).
INSIDE = datetime(2026, 10, 31, 3, 40)
#: 30 Oct 2026, 11:00 IST — the day before, outside it.
BEFORE = datetime(2026, 10, 30, 5, 30)


def _wipe():
    ids = [r.id for r in ClientRestriction.query.all()]
    if ids:
        ClientRestrictionEvent.query.filter(
            ClientRestrictionEvent.restriction_id.in_(ids)).delete(
                synchronize_session=False)
        ClientRestriction.query.filter(ClientRestriction.id.in_(ids)).delete(
            synchronize_session=False)
    Lead.query.filter(Lead.company.like('%Walchand%')).delete(
        synchronize_session=False)
    Lead.query.filter(Lead.company.like(TAG + '%')).delete(
        synchronize_session=False)
    Company.query.filter(Company.name.like(TAG + '%')).delete(
        synchronize_session=False)
    Company.query.filter(Company.name.like('%Walchand%')).delete(
        synchronize_session=False)
    db.session.commit()
    restrictions.cache_clear()


@pytest.fixture()
def world():
    with flask_app.app_context():
        db.create_all()
        _wipe()
        yield
        _wipe()


def _block(company=WIL, *, status=BLOCKED, aliases=('WIL',), scope=SCOPE_ENTITY,
           domains=(), emails=(), gstin=None, linked_account_id=None):
    row = ClientRestriction(
        status=status, company_name=company,
        name_key=restrictions.normalise(company),
        reason_category='Payment default / outstanding',
        reason_detail='Outstanding against invoices, unresolved.',
        gstin=gstin, linked_account_id=linked_account_id,
        recommended_by='LEAKADM', recommended_at=datetime.utcnow(),
        approved_by='LEAKADM', approved_at=datetime.utcnow(), scope=scope)
    row.aliases = list(aliases)
    row.domains = list(domains)
    row.emails = list(emails)
    db.session.add(row)
    db.session.commit()
    restrictions.cache_clear()
    return row


def _lead(company, *, when=INSIDE, email=None, company_id=None,
          lost_reason=None, source='manual', archived=False):
    lead = Lead(company=company, created_at=when, email=email,
                company_id=company_id, lost_reason=lost_reason,
                source=source, stage='New Opportunity',
                is_archived=archived)
    db.session.add(lead)
    db.session.commit()
    return lead


def _run(**kw):
    """The script's own reporting pass, as a list of rows."""
    rows = restrictions.live_rows()
    accounts = {}
    out = []
    # Only this module's own leads: every test module shares one
    # DATABASE_URL, so a sweep of the whole table would report rows
    # another module left behind.
    mine = Lead.query.filter(db.or_(Lead.company.like(TAG + '%'),
                                    Lead.company.like('%Walchand%')))
    for lead in mine.order_by(Lead.id).all():
        account = leaks._account(lead, accounts)
        row, matched_on, score = leaks._best_match(
            rows, leaks._parts(lead, account))
        if row is None:
            continue
        out.append((lead, row, matched_on, score,
                    leaks._verdict(row, matched_on, lead)))
    return out


# ── the window ───────────────────────────────────────────────────────
def test_the_ist_date_becomes_the_right_utc_window():
    start, end = leaks._window('2026-10-31', '2026-10-31')
    # 31 Oct 00:00 IST is 30 Oct 18:30 UTC, and the window is a whole
    # IST day, so it ends 24 hours later.
    assert start == datetime(2026, 10, 30, 18, 30)
    assert end == datetime(2026, 10, 31, 18, 30)


def test_no_until_runs_to_now():
    start, end = leaks._window('2026-10-31', None)
    assert start == datetime(2026, 10, 30, 18, 30)
    assert (datetime.utcnow() - end) < timedelta(seconds=5)


# ── finding the leak ─────────────────────────────────────────────────
def test_a_lead_on_a_blocked_client_is_reported_as_refusable(world):
    _block()
    lead = _lead('Walchandnagar Industries Ltd')
    found = _run()
    assert [f[0].id for f in found] == [lead.id]
    assert found[0][4] == leaks.REFUSED
    assert found[0][2] == 'name'


def test_an_unrelated_lead_is_not_reported(world):
    _block()
    _lead(TAG + 'Some Other Company Pvt Ltd')
    assert _run() == []


def test_a_lead_already_closed_as_blocked_is_not_a_leak(world):
    _block()
    _lead('Walchandnagar Industries Ltd',
          lost_reason=leaks.CLOSED_REASON)
    assert _run()[0][4] == leaks.CLOSED


def test_a_caution_entry_only_warns(world):
    _block(status=CAUTION)
    _lead('Walchandnagar Industries Ltd')
    assert _run()[0][4] == leaks.WARNED


def test_a_fuzzy_name_only_warns_because_it_would_never_refuse(world):
    # The register itself never returns `blocked` on a fuzzy score, so
    # a lead matched that way was not let through by a broken gate.
    _block(aliases=())
    _lead('Walchandnagar Industrial Limited')
    found = _run()
    assert found, 'the near miss should still be listed'
    assert found[0][2] in ('name-fuzzy', 'name-possible')
    assert found[0][4] == leaks.WARNED


def test_the_account_link_is_matched_even_when_the_name_is_different(world):
    account = Company(name=TAG + 'Renamed Trading Co')
    db.session.add(account)
    db.session.commit()
    _block(linked_account_id=account.id)
    _lead(TAG + 'Renamed Trading Co', company_id=account.id)
    found = _run()
    assert found[0][2] == 'account'
    assert found[0][4] == leaks.REFUSED


def test_a_group_domain_catches_the_lead_by_its_address(world):
    _block(company=TAG + 'Group Co', aliases=(), scope=SCOPE_GROUP,
           domains=('blockedgroup.example',))
    _lead(TAG + 'Nothing Like The Name',
          email='buyer@blockedgroup.example')
    found = _run()
    assert found[0][2] == 'domain'
    assert found[0][4] == leaks.REFUSED


def test_an_entity_scope_entry_does_not_catch_a_whole_domain(world):
    # The point of entity scope: a dispute with one company on a
    # shared domain must not block its sister company.
    _block(company=TAG + 'Group Co', aliases=(), scope=SCOPE_ENTITY,
           domains=('blockedgroup.example',))
    _lead(TAG + 'Nothing Like The Name',
          email='buyer@blockedgroup.example')
    assert _run() == []


def test_the_accounts_gstin_is_matched(world):
    account = Company(name=TAG + 'By GSTIN Ltd', gstin='27AAACW1234A1Z5')
    db.session.add(account)
    db.session.commit()
    _block(company=TAG + 'Quite Another Name', aliases=(),
           gstin='27AAACW1234A1Z5')
    _lead(TAG + 'By GSTIN Ltd', company_id=account.id)
    found = _run()
    assert found[0][2] == 'gstin'


def test_a_blocked_exact_match_outranks_a_caution_near_miss(world):
    _block(company=TAG + 'Near Miss Traders', status=CAUTION, aliases=())
    blocked = _block(company=TAG + 'Near Miss Trading', aliases=())
    _lead(TAG + 'Near Miss Trading')
    found = _run()
    assert found[0][1].id == blocked.id
    assert found[0][4] == leaks.REFUSED


# ── the parts a lead offers the register ─────────────────────────────
def test_the_parts_include_the_accounts_name_and_mail_domains(world):
    account = Company(name=TAG + 'Master Name Ltd',
                      email_domains=['master.example'])
    db.session.add(account)
    db.session.commit()
    lead = _lead(TAG + 'As Typed', company_id=account.id,
                 email='a@typed.example')
    parts = leaks._parts(lead, account)
    assert TAG + 'Master Name Ltd' in parts['names']
    assert TAG + 'As Typed' in parts['names']
    assert 'master.example' in parts['domains']
    assert 'typed.example' in parts['domains']


def test_the_window_excludes_the_day_before_the_gate(world):
    _block()
    lead = _lead('Walchandnagar Industries Ltd', when=BEFORE)
    start, end = leaks._window(SINCE, None)
    # Scoped to this lead: every test module shares one DATABASE_URL,
    # so a global count here would be counting other modules' rows.
    assert Lead.query.filter(Lead.id == lead.id,
                             Lead.created_at >= start,
                             Lead.created_at < end).count() == 0


# ── read-only ────────────────────────────────────────────────────────
def test_the_script_writes_nothing(world, capsys, monkeypatch):
    _block()
    _lead('Walchandnagar Industries Ltd')
    before = (Lead.query.count(), ClientRestriction.query.count(),
              ClientRestrictionEvent.query.count())
    monkeypatch.setattr(sys, 'argv',
                        ['blocked_client_leaks.py', '--since', SINCE])
    assert leaks.main() == 0
    out = capsys.readouterr().out
    assert 'READ ONLY' in out
    assert leaks.REFUSED in out
    assert (Lead.query.count(), ClientRestriction.query.count(),
            ClientRestrictionEvent.query.count()) == before


def test_the_script_never_commits(world, monkeypatch):
    # Belt and braces: a future edit that adds a write would have to
    # commit, and this makes that fail loudly rather than quietly
    # changing live data in what is documented as an audit.
    _block()
    _lead('Walchandnagar Industries Ltd')

    def _no(*a, **kw):
        raise AssertionError('this script must not commit')

    monkeypatch.setattr(db.session, 'commit', _no)
    monkeypatch.setattr(sys, 'argv',
                        ['blocked_client_leaks.py', '--since', SINCE])
    assert leaks.main() == 0


def test_an_empty_register_says_so_and_stops(world, capsys, monkeypatch):
    monkeypatch.setattr(sys, 'argv',
                        ['blocked_client_leaks.py', '--since', SINCE])
    assert leaks.main() == 0
    assert 'nothing can have leaked' in capsys.readouterr().out


def test_archived_leads_are_left_out_unless_asked_for(world, capsys,
                                                      monkeypatch):
    _block()
    lead = _lead('Walchandnagar Industries Ltd', archived=True)
    monkeypatch.setattr(sys, 'argv',
                        ['blocked_client_leaks.py', '--since', SINCE])
    leaks.main()
    # By lead number, not by the total: the modules share one
    # DATABASE_URL, so the total counts rows this test did not make.
    assert f'#{lead.id} ' not in capsys.readouterr().out
    monkeypatch.setattr(sys, 'argv', ['blocked_client_leaks.py', '--since',
                                      SINCE, '--include-archived'])
    leaks.main()
    assert f'#{lead.id} ' in capsys.readouterr().out


def test_the_csv_carries_every_column_a_decision_needs(world, monkeypatch,
                                                       tmp_path):
    _block()
    _lead('Walchandnagar Industries Ltd', email='buyer@wil.example')
    path = str(tmp_path / 'leaks.csv')
    monkeypatch.setattr(sys, 'argv', ['blocked_client_leaks.py', '--since',
                                      SINCE, '--csv', path])
    leaks.main()
    import csv as _csv
    with open(path) as fh:
        rows = list(_csv.DictReader(fh))
    assert len(rows) == 1
    for field in ('verdict', 'lead_id', 'created_at_ist', 'company',
                  'restricted_client', 'matched_on', 'owner',
                  'lead_closed_as_blocked',
                  'refusals_logged_for_this_entry'):
        assert field in rows[0], field
    assert rows[0]['verdict'] == leaks.REFUSED


# ── telling a leak apart from an ordinary sequence ───────────────────
def test_a_lead_created_before_its_block_is_not_a_gate_failure(world,
                                                               capsys,
                                                               monkeypatch):
    row = _block()
    row.approved_at = INSIDE + timedelta(hours=4)
    row.recommended_at = INSIDE + timedelta(hours=4)
    db.session.commit()
    restrictions.cache_clear()
    _lead('Walchandnagar Industries Ltd', when=INSIDE)
    monkeypatch.setattr(sys, 'argv',
                        ['blocked_client_leaks.py', '--since', SINCE])
    leaks.main()
    out = capsys.readouterr().out
    assert 'in force when the lead was created: no — blocked later' in out
    assert 'predate their block' in out
    assert '0 lead(s) the gate should have refused' in out


def test_a_lead_created_after_its_block_is_a_gate_failure(world, capsys,
                                                          monkeypatch):
    row = _block()
    row.approved_at = INSIDE - timedelta(hours=4)
    db.session.commit()
    restrictions.cache_clear()
    _lead('Walchandnagar Industries Ltd', when=INSIDE)
    monkeypatch.setattr(sys, 'argv',
                        ['blocked_client_leaks.py', '--since', SINCE])
    leaks.main()
    out = capsys.readouterr().out
    assert 'in force when the lead was created: yes' in out
    assert '1 lead(s) the gate should have refused' in out


def test_intake_refusals_for_the_same_client_are_counted(world, capsys,
                                                         monkeypatch):
    # The gate stopping the same client's mail an hour earlier is what
    # says the gate works and failed for a moment, rather than never
    # having run at all.
    from app import EmailEvent

    row = _block()
    row.approved_at = INSIDE - timedelta(days=2)
    db.session.commit()
    restrictions.cache_clear()
    db.session.add(EmailEvent(
        received_at=INSIDE - timedelta(minutes=15),
        status='blocked_client', classification='blocked_client',
        decided_by='restriction-register',
        reason=f'{WIL} is blocked — Payment default'))
    db.session.commit()
    _lead('Walchandnagar Industries Ltd', when=INSIDE)
    monkeypatch.setattr(sys, 'argv',
                        ['blocked_client_leaks.py', '--since', SINCE])
    leaks.main()
    assert 'stopped at intake 1 time(s)' in capsys.readouterr().out
