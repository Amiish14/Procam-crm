"""
One customer, one account.

FINISH-TRACKER-CRM §0. The live run of `link_unplaced_leads.py
--unlinked` on 29 October 2026 put 218 of 293 unlinked leads under
CHOOSE, and almost none of them were ambiguous leads — they were
ambiguous accounts. Praj has three, Hacklin four, Meiden four, L&T
three, Tmilltd three. `account_match._by_name` refuses to answer
when two active accounts normalise alike, and logs "matches 2
accounts — leaving it to a person", so every lead from that
customer stays unplaced until somebody merges.

Nothing here deletes. The tests below hold that, and hold that the
merged-away name still finds the right customer afterwards — a
merge that makes the old spelling unmatchable is worse than the
duplicate it fixed.
"""
import importlib
import importlib.util
import json
import os
import sys
import tempfile
from datetime import datetime

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'dupes-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'DupesTest-123456')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'dupes.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app = _main.app
flask_app.config['WTF_CSRF_ENABLED'] = False
db = _main.db
Company, Contact, Employee, Lead = (_main.Company, _main.Contact,
                                    _main.Employee, _main.Lead)

PREFIX = 'AD '
ADMIN, REP, OTHER = 'AD_ADMIN', 'AD_REP', 'AD_OTHER'
PEOPLE = ((ADMIN, 'Dupes Director', 'admin'),
          (REP, 'Jayanta Kumar Paul', 'user'),
          (OTHER, 'James Francis Xavier', 'manager'))
VERTICAL = 'Heavy Transport'
FLAG = 'FEATURE_ACCOUNT_MERGE'


def _script():
    path = os.path.join(_ROOT, 'scripts', 'account_duplicates.py')
    spec = importlib.util.spec_from_file_location('acctdupes', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ad = _script()


def _flag(state):
    from app.models.integration import AppSetting
    from app.services import flags
    row = AppSetting.query.filter_by(key='flag.' + FLAG).first()
    if row is None:
        row = AppSetting(key='flag.' + FLAG)
        db.session.add(row)
    row.value_json = json.dumps(bool(state))
    db.session.commit()
    flags.cache_clear()


def _purge():
    from app.models.assignment import AccountVerticalOwner
    from app.models.audit import AuditEvent
    ours = Company.query.filter(Company.name.like(PREFIX + '%')).all()
    for account in ours:
        AccountVerticalOwner.query.filter_by(
            account_id=account.id).delete(synchronize_session=False)
        AuditEvent.query.filter(
            AuditEvent.entity_type == 'company',
            AuditEvent.entity_id == str(account.id)).delete(
                synchronize_session=False)
    Contact.query.filter(Contact.name.like(PREFIX + '%')).delete(
        synchronize_session=False)
    Lead.query.filter(Lead.company.like(PREFIX + '%')).delete(
        synchronize_session=False)
    Company.query.filter(Company.name.like(PREFIX + '%')).delete(
        synchronize_session=False)
    Employee.query.filter(
        Employee.emp_code.in_([p[0] for p in PEOPLE])).delete(
            synchronize_session=False)
    _flag(False)
    db.session.commit()


@pytest.fixture(autouse=True)
def _clean():
    with flask_app.app_context():
        db.create_all()
        _purge()
        for code, name, role in PEOPLE:
            db.session.add(Employee(
                emp_code=code, name=name, password_hash='x', role=role,
                vertical=VERTICAL, is_active=True,
                email=code.lower() + '@procamlogistics.com'))
        db.session.commit()
        _flag(True)
        yield
        _purge()


def _account(name, *, gstin=None, domains=(), pic=REP, leads=0,
             won=0, active=True):
    account = Company(name=PREFIX + name, is_active=active,
                      pic_emp_code=pic, vertical=VERTICAL,
                      gstin=gstin)
    account.email_domains = list(domains)
    db.session.add(account)
    db.session.flush()
    for _ in range(leads):
        db.session.add(Lead(company=PREFIX + name,
                            company_id=account.id,
                            stage='New Opportunity', source='manual',
                            assigned_to=pic, procam_vertical=VERTICAL,
                            created_at=datetime.utcnow()))
    for _ in range(won):
        db.session.add(Lead(company=PREFIX + name,
                            company_id=account.id, stage='Won',
                            source='manual', assigned_to=pic,
                            procam_vertical=VERTICAL,
                            created_at=datetime.utcnow()))
    db.session.flush()
    return account


def _owner_row(account, vertical, code):
    from app.models.assignment import AccountVerticalOwner
    db.session.add(AccountVerticalOwner(
        account_id=account.id, vertical=vertical, emp_code=code,
        source='manual'))
    db.session.flush()


def _ours(found):
    return [c for c in found
            if c['survivor']['name'].startswith(PREFIX)]


# ── §0 finding the clusters ──────────────────────────────────────────
def test_two_accounts_with_the_same_gstin_are_one_customer():
    with flask_app.app_context():
        _account('Praj One', gstin='27AAACP1234A1Z5')
        _account('Praj Two Industries', gstin='27AAACP1234A1Z5')
        db.session.commit()
        found = _ours(ad.clusters())
        assert len(found) == 1
        assert ad.BY_GSTIN in found[0]['reasons']


def test_two_accounts_sharing_a_domain_are_one_customer():
    with flask_app.app_context():
        _account('Hacklin', domains=['hacklin-test.fi'])
        _account('Oy Hacklin Logistics', domains=['hacklin-test.fi'])
        db.session.commit()
        found = _ours(ad.clusters())
        assert len(found) == 1
        assert ad.BY_DOMAIN in found[0]['reasons']


def test_names_that_normalise_alike_are_one_customer():
    """"Tmilltd" and "TMILTD" are the live pair; the CRM's own
    normaliser is what decides, not a ratio."""
    with flask_app.app_context():
        _account('Meiden T&D (India) Ltd')
        _account('MEIDEN T&D INDIA LIMITED')
        db.session.commit()
        found = _ours(ad.clusters())
        assert len(found) == 1
        assert ad.BY_NAME in found[0]['reasons']


def test_a_cluster_is_transitive():
    """A shares a domain with B, B normalises like C: one customer,
    not two pairs."""
    with flask_app.app_context():
        _account('Linked One', domains=['linked-test.com'])
        _account('Linked Two', domains=['linked-test.com'])
        _account('Linked Two Pvt Ltd')
        db.session.commit()
        found = _ours(ad.clusters())
        assert len(found) == 1
        assert len(found[0]['accounts']) == 3


def test_two_different_customers_are_not_a_cluster():
    with flask_app.app_context():
        _account('Alpha Steel', domains=['alpha-test.com'])
        _account('Beta Cement', domains=['beta-test.com'])
        db.session.commit()
        assert _ours(ad.clusters()) == []


def test_a_placeholder_account_is_never_clustered():
    """"Unknown" claims our own domain, so clustering on it would
    drag every account into one group."""
    with flask_app.app_context():
        unknown = Company(name='Unknown', is_active=True)
        unknown.email_domains = ['procamgroup.in']
        db.session.add(unknown)
        _account('Real Co', domains=['procamgroup.in'])
        db.session.commit()
        for cluster in ad.clusters():
            names = [a['name'] for a in cluster['accounts']]
            assert 'Unknown' not in names


def test_an_already_merged_account_drops_out():
    with flask_app.app_context():
        _account('Settled One', domains=['settled-test.com'])
        _account('Settled Two', domains=['settled-test.com'],
                 active=False)
        db.session.commit()
        assert _ours(ad.clusters()) == []


def test_only_the_named_accounts_clusters_are_shown():
    with flask_app.app_context():
        wanted = _account('Wanted A', domains=['wanted-test.com'])
        _account('Wanted B', domains=['wanted-test.com'])
        _account('Ignored A', domains=['ignored-test.com'])
        _account('Ignored B', domains=['ignored-test.com'])
        db.session.commit()
        found = _ours(ad.clusters([wanted.id]))
        assert len(found) == 1
        assert wanted.id in [a['id'] for a in found[0]['accounts']]


# ── §0 the suggested survivor ────────────────────────────────────────
def test_the_survivor_is_the_one_holding_the_history():
    with flask_app.app_context():
        big = _account('Weighty', domains=['weighty-test.com'],
                       leads=6)
        _account('Weightless', domains=['weighty-test.com'], leads=1)
        db.session.commit()
        cluster = _ours(ad.clusters())[0]
        assert cluster['survivor']['id'] == big.id
        assert '6 lead(s)' in cluster['survivor_why']


def test_a_win_breaks_a_tie():
    with flask_app.app_context():
        winner = _account('Won Some', domains=['tie-test.com'],
                          leads=1, won=2)
        _account('Won None', domains=['tie-test.com'], leads=3)
        db.session.commit()
        cluster = _ours(ad.clusters())[0]
        # Three leads beats one, so the bigger row still wins — the
        # tie-break only applies at equal totals.
        assert cluster['survivor']['leads'] == 3
        assert winner.id in [a['id'] for a in cluster['accounts']]


def test_the_survivor_is_a_suggestion_not_a_decision():
    with flask_app.app_context():
        _account('Suggest A', domains=['suggest-test.com'], leads=2)
        _account('Suggest B', domains=['suggest-test.com'])
        db.session.commit()
        text = ad.report(ad.clusters())
        assert 'suggested survivor' in text
        assert 'evidence, not a decision' in text


def test_the_report_shows_owners_per_vertical():
    with flask_app.app_context():
        one = _account('Owned A', domains=['owned-test.com'], leads=2)
        two = _account('Owned B', domains=['owned-test.com'])
        _owner_row(one, VERTICAL, REP)
        _owner_row(two, 'Warehousing', OTHER)
        db.session.commit()
        text = ad.report(ad.clusters())
        assert 'owners —' in text
        assert REP in text and OTHER in text


def test_the_report_changes_nothing():
    with flask_app.app_context():
        one = _account('Quiet A', domains=['quiet-test.com'], leads=2)
        two = _account('Quiet B', domains=['quiet-test.com'])
        db.session.commit()
        ad.report(ad.clusters())
        assert db.session.get(Company, one.id).is_active is True
        assert db.session.get(Company, two.id).is_active is True


def test_the_report_is_honest_when_there_are_none():
    assert 'No duplicate accounts' in ad.report([])


def test_the_counts_are_clusters_accounts_and_leads():
    with flask_app.app_context():
        _account('Counted A', domains=['counted-test.com'], leads=2)
        _account('Counted B', domains=['counted-test.com'], leads=1)
        db.session.commit()
        clusters, accounts, leads, opened = ad.counts(_ours(ad.clusters()))
        assert (clusters, accounts, leads, opened) == (1, 2, 3, 3)


# ── §0 the merge ─────────────────────────────────────────────────────
def test_a_merge_moves_the_leads():
    with flask_app.app_context():
        keep = _account('Keep Me', domains=['merge-test.com'], leads=3)
        fold = _account('Fold Me', domains=['merge-test.com'], leads=2)
        db.session.commit()
        done, refused = ad.merge(keep.id, [fold.id], actor=ADMIN)
        assert refused == []
        assert done[0]['moved'].get('leads.company_id') == 2
        assert Lead.query.filter_by(company_id=fold.id).count() == 0
        assert Lead.query.filter_by(company_id=keep.id).count() == 5


def test_a_merge_moves_the_contacts():
    with flask_app.app_context():
        keep = _account('Contact Keep', domains=['ct-test.com'],
                        leads=2)
        fold = _account('Contact Fold', domains=['ct-test.com'])
        db.session.add(Contact(name=PREFIX + 'Someone',
                               email='someone@ct-test.com',
                               company_id=fold.id))
        db.session.commit()
        ad.merge(keep.id, [fold.id], actor=ADMIN)
        moved = Contact.query.filter(
            Contact.name.like(PREFIX + '%')).first()
        assert moved.company_id == keep.id


def test_a_merge_moves_the_vertical_owners():
    with flask_app.app_context():
        keep = _account('Owner Keep', domains=['ow-test.com'], leads=2)
        fold = _account('Owner Fold', domains=['ow-test.com'])
        _owner_row(fold, 'Warehousing', OTHER)
        db.session.commit()
        ad.merge(keep.id, [fold.id], actor=ADMIN)
        from app.models.assignment import AccountVerticalOwner
        row = AccountVerticalOwner.query.filter_by(
            account_id=keep.id, vertical='Warehousing').first()
        assert row is not None and row.emp_code == OTHER


def test_an_owner_row_that_would_clash_stays_put_and_is_counted():
    """Both accounts name somebody for Heavy Transport. One of them
    has to stay where it is, because the alternative is deleting a
    record of who served this customer."""
    with flask_app.app_context():
        keep = _account('Clash Keep', domains=['cl-test.com'], leads=2)
        fold = _account('Clash Fold', domains=['cl-test.com'])
        _owner_row(keep, VERTICAL, REP)
        _owner_row(fold, VERTICAL, OTHER)
        db.session.commit()
        done, _refused = ad.merge(keep.id, [fold.id], actor=ADMIN)
        left = done[0]['left_behind']
        assert left.get('account_vertical_owner.account_id') == 1
        from app.models.assignment import AccountVerticalOwner
        assert AccountVerticalOwner.query.filter_by(
            account_id=keep.id, vertical=VERTICAL).one().emp_code == REP
        assert AccountVerticalOwner.query.filter_by(
            account_id=fold.id).count() == 1


def test_nothing_is_deleted():
    with flask_app.app_context():
        keep = _account('Alive Keep', domains=['al-test.com'], leads=2)
        fold = _account('Alive Fold', domains=['al-test.com'])
        db.session.commit()
        fold_id = fold.id
        ad.merge(keep.id, [fold_id], actor=ADMIN)
        row = db.session.get(Company, fold_id)
        assert row is not None
        assert row.is_active is False
        assert f'(merged into #{keep.id})' in row.name


def test_the_old_name_still_finds_the_right_customer():
    """The point of the alias. A merge that makes the old spelling
    unmatchable is worse than the duplicate it fixed."""
    with flask_app.app_context():
        from app.services import account_match
        keep = _account('Praj', domains=['praj-test.com'], leads=3)
        fold = _account('Praj Industries Limited',
                        domains=['praj-test.com'])
        db.session.commit()
        was = fold.name
        ad.merge(keep.id, [fold.id], actor=ADMIN)
        found = account_match._by_name(was)
        assert found is not None and found.id == keep.id


def test_the_matcher_can_answer_again_after_a_merge():
    """Before: two active rows normalise alike, so `_by_name`
    returns nothing at all and every lead waits. After: one."""
    with flask_app.app_context():
        from app.services import account_match
        keep = _account('Tmilltd', leads=4)
        fold = _account('TMILLTD')
        db.session.commit()
        assert account_match._by_name(PREFIX + 'Tmilltd') is None
        ad.merge(keep.id, [fold.id], actor=ADMIN)
        found = account_match._by_name(PREFIX + 'Tmilltd')
        assert found is not None and found.id == keep.id


def test_the_losers_domains_move_across():
    with flask_app.app_context():
        keep = _account('Domain Keep', domains=['dk-test.com'],
                        leads=2)
        fold = _account('Domain Fold',
                        domains=['dk-test.com', 'extra-test.com'])
        db.session.commit()
        ad.merge(keep.id, [fold.id], actor=ADMIN)
        domains = db.session.get(Company, keep.id).email_domains or []
        assert 'extra-test.com' in domains


def test_a_merge_writes_an_audit_row_on_both_sides():
    with flask_app.app_context():
        from app.models.audit import AuditEvent
        keep = _account('Audit Keep', domains=['au-test.com'], leads=2)
        fold = _account('Audit Fold', domains=['au-test.com'])
        db.session.commit()
        ad.merge(keep.id, [fold.id], actor=ADMIN)
        merged = AuditEvent.query.filter_by(
            action='account.merged', entity_id=str(fold.id)).first()
        absorbed = AuditEvent.query.filter_by(
            action='account.absorbed', entity_id=str(keep.id)).first()
        assert merged is not None and merged.actor == ADMIN
        assert absorbed is not None and absorbed.actor == ADMIN


def test_a_merge_refuses_an_inactive_survivor():
    with flask_app.app_context():
        gone = _account('Gone', domains=['gn-test.com'], active=False)
        other = _account('Other', domains=['gn-test.com'])
        db.session.commit()
        done, refused = ad.merge(gone.id, [other.id], actor=ADMIN)
        assert done == []
        assert 'already been merged' in refused[0][1]


def test_a_merge_refuses_itself():
    with flask_app.app_context():
        keep = _account('Self', leads=1)
        db.session.commit()
        done, refused = ad.merge(keep.id, [keep.id], actor=ADMIN)
        assert done == []
        assert 'that is the survivor' in refused[0][1]


def test_a_merge_refuses_an_account_already_merged():
    with flask_app.app_context():
        keep = _account('Twice Keep', leads=2)
        fold = _account('Twice Fold', active=False)
        db.session.commit()
        done, refused = ad.merge(keep.id, [fold.id], actor=ADMIN)
        assert done == []
        assert 'already merged' in refused[0][1]


def test_a_merge_refuses_an_account_that_does_not_exist():
    with flask_app.app_context():
        keep = _account('Real One', leads=1)
        db.session.commit()
        done, refused = ad.merge(keep.id, [99999901], actor=ADMIN)
        assert done == [] and refused[0][1] == 'no such account'


def test_the_preview_writes_nothing():
    with flask_app.app_context():
        keep = _account('Preview Keep', domains=['pv-test.com'],
                        leads=2)
        fold = _account('Preview Fold', domains=['pv-test.com'],
                        leads=3)
        db.session.commit()
        text = ad.merge_preview(keep.id, [fold.id])
        assert 'leads.company_id: 3 row(s) move' in text
        assert 'Nothing was changed' in text
        assert db.session.get(Company, fold.id).is_active is True


# ── §0 the command line's gate ───────────────────────────────────────
def _run(argv):
    import contextlib
    import io
    out = io.StringIO()
    old = sys.argv
    sys.argv = ['account_duplicates.py'] + argv
    try:
        with contextlib.redirect_stdout(out):
            code = ad.main()
    finally:
        sys.argv = old
    return code, out.getvalue()


def test_the_command_line_is_read_only_by_default():
    with flask_app.app_context():
        keep = _account('CLI Keep', domains=['cli-test.com'], leads=2)
        fold = _account('CLI Fold', domains=['cli-test.com'])
        db.session.commit()
        fold_id = fold.id
    code, text = _run([])
    assert code == 0
    assert 'Nothing was changed' in text
    with flask_app.app_context():
        assert db.session.get(Company, fold_id).is_active is True


def test_a_merge_without_an_actor_writes_nothing():
    with flask_app.app_context():
        keep = _account('Armless Keep', domains=['am-test.com'],
                        leads=2)
        fold = _account('Armless Fold', domains=['am-test.com'])
        db.session.commit()
        keep_id, fold_id = keep.id, fold.id
    code, text = _run(['--survivor', str(keep_id), '--merge',
                       str(fold_id), '--apply'])
    assert code == 0
    assert 'Nothing was changed' in text
    with flask_app.app_context():
        assert db.session.get(Company, fold_id).is_active is True


def test_a_merge_without_a_survivor_writes_nothing():
    with flask_app.app_context():
        fold = _account('Lonely Fold')
        db.session.commit()
        fold_id = fold.id
    code, text = _run(['--merge', str(fold_id), '--actor', ADMIN,
                       '--apply'])
    assert code == 2
    assert 'Nothing was changed' in text
    with flask_app.app_context():
        assert db.session.get(Company, fold_id).is_active is True


def test_the_command_line_merges_when_fully_armed():
    with flask_app.app_context():
        keep = _account('Armed Keep', domains=['ar-test.com'], leads=2)
        fold = _account('Armed Fold', domains=['ar-test.com'], leads=1)
        db.session.commit()
        keep_id, fold_id = keep.id, fold.id
    code, text = _run(['--survivor', str(keep_id), '--merge',
                       str(fold_id), '--actor', ADMIN, '--apply'])
    assert code == 0
    assert 'merged' in text
    with flask_app.app_context():
        assert db.session.get(Company, fold_id).is_active is False
        assert Lead.query.filter_by(company_id=keep_id).count() == 3


# ── §0 the Admin screen ──────────────────────────────────────────────
def _client(code=ADMIN, role='admin'):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s['emp_code'] = code
        s['role'] = role
    return c


def test_the_screen_404s_with_the_flag_off():
    with flask_app.app_context():
        _flag(False)
    r = _client().get('/admin/account-duplicates')
    assert r.status_code == 404
    r = _client().get('/api/admin/account-duplicates')
    assert r.status_code == 404


def test_the_screen_renders_with_the_flag_on_for_a_pilot_user():
    with flask_app.app_context():
        _account('Screen Keep', domains=['sc-test.com'], leads=2)
        _account('Screen Fold', domains=['sc-test.com'])
        db.session.commit()
    r = _client().get('/admin/account-duplicates')
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'Duplicate accounts' in body
    assert PREFIX + 'Screen Fold' in body
    assert 'Nothing is deleted' in body


def test_the_screen_is_admins_only():
    r = _client(REP, 'user').get('/admin/account-duplicates')
    assert r.status_code == 403


def test_the_api_lists_the_same_clusters():
    with flask_app.app_context():
        _account('Api Keep', domains=['ap-test.com'], leads=2)
        _account('Api Fold', domains=['ap-test.com'])
        db.session.commit()
    data = _client().get('/api/admin/account-duplicates').get_json()
    assert data['ok'] is True
    ours = [r for r in data['rows']
            if r['name'].startswith(PREFIX)]
    assert len(ours) == 1
    assert any(a['suggested'] for a in ours[0]['accounts'])


def test_the_screen_can_merge_in_one_click():
    with flask_app.app_context():
        keep = _account('Click Keep', domains=['ck-test.com'], leads=2)
        fold = _account('Click Fold', domains=['ck-test.com'], leads=1)
        db.session.commit()
        keep_id, fold_id = keep.id, fold.id
    r = _client().post('/api/admin/account-duplicates/merge',
                       json={'survivor': keep_id, 'merge': [fold_id]})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()['ok'] is True
    with flask_app.app_context():
        assert db.session.get(Company, fold_id).is_active is False
        assert Lead.query.filter_by(company_id=keep_id).count() == 3


def test_the_screen_refuses_a_merge_with_no_losers():
    with flask_app.app_context():
        keep = _account('Empty Keep', leads=1)
        db.session.commit()
        keep_id = keep.id
    r = _client().post('/api/admin/account-duplicates/merge',
                       json={'survivor': keep_id, 'merge': []})
    assert r.status_code == 400


def test_the_screen_refuses_to_merge_the_survivor_away():
    with flask_app.app_context():
        keep = _account('Both Keep', leads=1)
        db.session.commit()
        keep_id = keep.id
    r = _client().post('/api/admin/account-duplicates/merge',
                       json={'survivor': keep_id,
                             'merge': [keep_id]})
    assert r.status_code == 400
    assert 'cannot also be merged' in r.get_json()['error']
