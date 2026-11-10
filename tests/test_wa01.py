"""The five issues the PFM team raised on WhatsApp.

CRM-WA-01. One test block per item, and in two of them the test is
really about a trap:

§2  the merge has to be *reversible*, and the old one recorded counts
    — "lead_activities.lead_id: 14". Fourteen of what? After the
    merge the survivor holds thirty activities and nothing says which
    fourteen came with the loser. So the tests here are about row ids,
    and about the survivor's own records staying put.

§3  "PIC" means two different people in this CRM: `leads.pic` is the
    customer's contact and `leads.assigned_to` is our employee. The
    instruction "backfill PIC email from the assigned user's record"
    is safe under one reading and corrupting under the other, so one
    test asserts that an employee's address is never written into the
    customer's email column.
"""
import importlib
import importlib.util
import os
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'wa01-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'Wa01-123456789')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'wa01.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app = _main.app
db = _main.db

from app import (Company, Contact, Employee, Lead,            # noqa: E402
                 LeadActivity, LeadAttachment)
from app.models.audit import AuditEvent                       # noqa: E402
from app.models.lead_merge import LeadMerge                    # noqa: E402
from app.services import lead_archive as la                    # noqa: E402
from app.services import lead_duplicates as ld                 # noqa: E402
from app.services import lead_merge as lm                      # noqa: E402
from app.services import signin_diagnosis as sd                # noqa: E402

TAG = 'ZWa01'
ADMIN = 'ZW_ADM'
OWNER = 'ZW_OWN'
#: A valid GSTIN by the CRM's own checker, so the GSTIN signal is
#: exercised rather than skipped.
GSTIN = '27AAPFU0939F1ZV'


@pytest.fixture(autouse=True)
def clean():
    with flask_app.app_context():
        db.create_all()
        _purge()
        yield
        _purge()


def _purge():
    ids = [l.id for l in Lead.query.filter(
        Lead.company.like(TAG + '%')).all()]
    if ids:
        LeadActivity.query.filter(
            LeadActivity.lead_id.in_(ids)).delete(
                synchronize_session=False)
        LeadAttachment.query.filter(
            LeadAttachment.lead_id.in_(ids)).delete(
                synchronize_session=False)
        AuditEvent.query.filter(
            AuditEvent.entity_type == 'lead',
            AuditEvent.entity_id.in_([str(i) for i in ids])).delete(
                synchronize_session=False)
        LeadMerge.query.filter(
            LeadMerge.lead_id.in_(ids)).delete(
                synchronize_session=False)
        Lead.query.filter(Lead.id.in_(ids)).delete(
            synchronize_session=False)
    Contact.query.filter(Contact.name.like(TAG + '%')).delete(
        synchronize_session=False)
    Company.query.filter(Company.name.like(TAG + '%')).delete(
        synchronize_session=False)
    Employee.query.filter(Employee.emp_code.like('ZW_%')).delete(
        synchronize_session=False)
    db.session.commit()


def _emp(code, name, **kw):
    kw.setdefault('role', 'user')
    kw.setdefault('is_active', True)
    kw.setdefault('email', f'{code.lower()}@procamgroup.in')
    # Explicit, because the column default is True: a fixture that
    # left it alone would make every account read as "holds a
    # temporary password", which is a real verdict and not the one
    # most of these tests are about.
    kw.setdefault('must_change_pw', False)
    emp = Employee(emp_code=code, name=name, password_hash='x', **kw)
    emp.set_password('Correct-horse-123')
    db.session.add(emp)
    db.session.commit()
    return emp


def _lead(**kw):
    kw.setdefault('company', f'{TAG} Heavy Lift Limited')
    kw.setdefault('stage', 'New')
    kw.setdefault('source', 'email')
    kw.setdefault('created_at', datetime.utcnow() - timedelta(days=3))
    lead = Lead(**kw)
    db.session.add(lead)
    db.session.commit()
    return lead


def _client(code, role='admin'):
    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess['emp_code'] = code
        sess['role'] = role
    return client


def _token(client):
    """The token a browser would read out of the cookie.

    Sent on every POST below. Without it CSRF answers 400 before the
    gate runs, so a test meaning to assert "403 for a non-admin"
    would pass on the wrong refusal.
    """
    r = client.get('/my-work')
    for header in r.headers.getlist('Set-Cookie'):
        if header.startswith('csrf_token='):
            return header.split('=', 1)[1].split(';')[0]
    return ''


# ═══════════════════════════════════════════════════════════════════
# §1 — archive, with one of four reasons
# ═══════════════════════════════════════════════════════════════════
def test_the_four_reasons_are_the_ones_asked_for():
    assert set(la.CATEGORY_CODES) == {
        'duplicate', 'junk', 'test', 'lost_before_qualifying'}


def test_a_category_is_required_and_not_defaulted():
    """A default category would be assigned to every archive nobody
    thought about, and the count it produces read as a finding."""
    with pytest.raises(la.Refused):
        la.compose_reason('')
    with pytest.raises(la.Refused):
        la.compose_reason('something else')


def test_the_category_and_the_note_are_stored_together():
    assert la.compose_reason('duplicate') == 'duplicate'
    assert la.compose_reason('duplicate', 'dupe of #10291') == \
        'duplicate: dupe of #10291'


def test_a_category_is_read_back_out():
    assert la.split_reason('duplicate: dupe of #10291') == \
        ('duplicate', 'dupe of #10291')
    assert la.split_reason('junk') == ('junk', '')


def test_an_archive_filed_before_the_categories_is_not_reclassified():
    """It has no category, and that is what it is. Guessing from the
    words would file somebody else's sentence under a code they never
    chose."""
    code, note = la.split_reason('duplicate of 10291, client confirmed')
    assert code == la.UNCATEGORISED
    assert note == 'duplicate of 10291, client confirmed'


def test_spelling_is_forgiven_but_invention_is_not():
    assert la.check_category('Lost-Before-Qualifying') == \
        'lost_before_qualifying'
    assert la.check_category('LOST BEFORE QUALIFYING') == \
        'lost_before_qualifying'
    with pytest.raises(la.Refused):
        la.check_category('lost')


def test_a_note_that_will_not_fit_is_refused_not_truncated():
    with pytest.raises(la.Refused):
        la.compose_reason('junk', 'x' * 400)


def test_archiving_with_a_category_stores_it(monkeypatch):
    with flask_app.app_context():
        _emp(ADMIN, 'Admin Person', role='admin')
        lead = _lead(assigned_to=ADMIN)
        la.archive(lead, actor=ADMIN, category='duplicate',
                   note='dupe of #10291')
        db.session.expire_all()
        fresh = Lead.query.get(lead.id)
        assert fresh.is_archived is True
        assert fresh.archive_reason == 'duplicate: dupe of #10291'
        state = la.state_for(fresh, ADMIN)
        assert state['category'] == 'duplicate'
        assert state['category_label'] == 'Duplicate'
        assert state['note'] == 'dupe of #10291'


def test_a_caller_written_before_this_still_works():
    """A bulk tool or a script that archives with a sentence should
    not start failing because the lead screen grew a dropdown."""
    with flask_app.app_context():
        _emp(ADMIN, 'Admin Person', role='admin')
        lead = _lead(assigned_to=ADMIN)
        la.archive(lead, actor=ADMIN, reason='client went quiet')
        db.session.expire_all()
        assert Lead.query.get(lead.id).archive_reason == \
            'client went quiet'


def test_the_screen_is_given_the_list_it_must_draw():
    """One list, enforced on the server. A copy in the template drifts
    from it, and the half that drifts is the one that lets a value
    through."""
    with flask_app.app_context():
        _emp(ADMIN, 'Admin Person', role='admin')
        lead = _lead(assigned_to=ADMIN)
        state = la.state_for(lead, ADMIN)
        assert [c['code'] for c in state['categories']] == \
            list(la.CATEGORY_CODES)
        for entry in state['categories']:
            assert entry['label'] and entry['what']


def test_the_route_takes_a_category():
    with flask_app.app_context():
        _emp(ADMIN, 'Admin Person', role='admin')
        lead = _lead(assigned_to=ADMIN)
        lid = lead.id
    client = _client(ADMIN)
    r = client.get(f'/api/leads/{lid}/archive-state')
    assert r.status_code == 200
    body = r.get_json()
    assert body['may'] is True
    assert len(body['categories']) == 4


# ═══════════════════════════════════════════════════════════════════
# §2 — duplicates: the four signals
# ═══════════════════════════════════════════════════════════════════
def test_the_four_signals_are_the_ones_asked_for():
    assert {k for k, _s, _w in ld.SIGNALS} == {
        'company_name', 'email_domain', 'phone', 'gstin'}


def test_a_gstin_outranks_a_domain():
    """A shared GSTIN is one legal entity exactly; a shared domain can
    be two. A warning that treats them alike is one people click
    through."""
    assert ld.STRENGTH['gstin'] > ld.STRENGTH['phone']
    assert ld.STRENGTH['phone'] > ld.STRENGTH['company_name']
    assert ld.STRENGTH['company_name'] > ld.STRENGTH['email_domain']


def test_a_domain_alone_is_not_worth_interrupting_somebody():
    assert ld.score(['email_domain']) < ld.WORTH_SHOWING
    assert ld.score(['gstin']) >= ld.WORTH_SHOWING
    assert ld.score(['phone']) >= ld.WORTH_SHOWING


def test_two_weak_signals_do_not_add_up_to_a_certainty():
    assert ld.score(['email_domain', 'company_name']) < \
        ld.score(['gstin'])


def test_free_mail_is_not_a_domain_match():
    """Two unrelated customers who both used Gmail are not related,
    and counting them would fire the warning on half the board."""
    assert ld.domain_of('somebody@gmail.com') == ''
    assert ld.domain_of('buyer@zzwa-client.example') == \
        'zzwa-client.example'


def test_a_phone_is_the_last_ten_digits():
    """+91 98765 43210, 098765 43210 and 9876543210 are one phone."""
    assert ld.norm_phone('+91 98765 43210') == '9876543210'
    assert ld.norm_phone('098765 43210') == '9876543210'
    assert ld.norm_phone('9876543210') == '9876543210'
    assert ld.norm_phone('1234') == ''


def test_a_half_typed_gstin_does_not_match_another_half_typed_one():
    assert ld.norm_gstin(GSTIN) == GSTIN
    assert ld.norm_gstin('27AAPF') == ''


def test_the_name_normaliser_is_the_account_matchers():
    """Three implementations of "the same name" would not agree."""
    from app.services import company_match

    for name in (f'{TAG} Heavy Lift Limited', 'BEML LIMITED',
                 'Skipper Seil Ltd'):
        assert ld.norm_name(name) == company_match.norm(name)


def test_the_same_phone_finds_the_other_lead():
    with flask_app.app_context():
        first = _lead(phone='+91 98765 43210')
        found = ld.check(company='Totally Different Name',
                         phone='098765 43210')
        assert [r['lead_id'] for r in found] == [first.id]
        assert 'phone' in found[0]['matched']


def test_the_same_gstin_finds_it_through_the_account():
    """`leads` has no GSTIN column — the GST master is on `companies`
    — so a lead's GSTIN is its account's."""
    with flask_app.app_context():
        account = Company(name=f'{TAG} Big Client', gstin=GSTIN)
        db.session.add(account)
        db.session.commit()
        first = _lead(company_id=account.id)
        found = ld.check(company='Nothing Alike', gstin=GSTIN)
        assert [r['lead_id'] for r in found] == [first.id]
        assert found[0]['matched'] == ['gstin']


def test_an_archived_or_merged_lead_is_not_offered_as_a_duplicate():
    with flask_app.app_context():
        gone = _lead(phone='9876543210', is_archived=True)
        folded = _lead(phone='9876543210', merged_into_id=gone.id)
        assert ld.check(company='x', phone='9876543210') == []


def test_a_lead_does_not_match_itself():
    with flask_app.app_context():
        lead = _lead(phone='9876543210')
        found = ld.check(company=lead.company, phone='9876543210',
                         exclude_id=lead.id)
        assert found == []


def test_the_warning_route_answers_for_anybody_signed_in():
    """The person keying a lead in is usually not an administrator."""
    with flask_app.app_context():
        _emp(OWNER, 'Owner Person', role='user')
        _lead(phone='9876543210')
    client = _client(OWNER, role='user')
    r = client.get('/api/leads/duplicate-check?phone=9876543210'
                   '&company=Anything')
    assert r.status_code == 200
    assert r.get_json()['count'] == 1


def test_the_warning_route_refuses_a_stranger():
    client = flask_app.test_client()
    assert client.get('/api/leads/duplicate-check?phone=1').status_code \
        == 401


# ═══════════════════════════════════════════════════════════════════
# §2 — the merge, and putting it back
# ═══════════════════════════════════════════════════════════════════
def _pair_with_history():
    """Two leads, the loser carrying rows of its own, the survivor
    carrying rows of its own. The second half is the point: a reversal
    must not take the survivor's records with it."""
    keep = _lead(company=f'{TAG} Survivor')
    drop = _lead(company=f'{TAG} Loser')
    mine = LeadActivity(lead_id=drop.id, kind='call',
                        subject=f'{TAG} the loser\'s call',
                        occurred_at=datetime.utcnow())
    theirs = LeadActivity(lead_id=keep.id, kind='call',
                          subject=f'{TAG} the survivor\'s call',
                          occurred_at=datetime.utcnow())
    db.session.add_all([mine, theirs])
    db.session.commit()
    return keep, drop, mine, theirs


def test_a_merge_moves_the_losers_rows():
    with flask_app.app_context():
        keep, drop, mine, theirs = _pair_with_history()
        ok, detail = lm.merge(drop, keep, actor=ADMIN, reason='same job')
        assert ok, detail
        db.session.expire_all()
        assert LeadActivity.query.get(mine.id).lead_id == keep.id
        assert Lead.query.get(drop.id).merged_into_id == keep.id
        assert Lead.query.get(drop.id).is_archived is True


def test_the_merge_records_row_ids_not_counts():
    """The whole of reversibility. "lead_activities.lead_id: 14" is
    auditable and useless: fourteen of what?"""
    with flask_app.app_context():
        keep, drop, mine, _theirs = _pair_with_history()
        lm.merge(drop, keep, actor=ADMIN)
        ledger = lm.last_merge(Lead.query.get(drop.id))
        assert ledger is not None
        assert ledger.partial is False
        moves = ledger.moves_json or {}
        assert 'lead_activities.lead_id' in moves
        assert mine.id in moves['lead_activities.lead_id']


def test_the_audit_row_records_the_stage_the_lead_really_had():
    """It used to read `old={'stage': lead.stage}` after the
    assignment, so the trail recorded the new stage as the old one —
    and a reversal had nothing to restore."""
    with flask_app.app_context():
        keep, drop, _m, _t = _pair_with_history()
        drop.stage = 'RFQ Generated'
        db.session.commit()
        lm.merge(drop, keep, actor=ADMIN)
        row = (AuditEvent.query
               .filter_by(action='lead.merged',
                          entity_id=str(drop.id)).first())
        assert row is not None
        assert row.old_value['stage'] == 'RFQ Generated'
        assert row.old_value['is_archived'] is False


def test_putting_it_back_returns_the_losers_rows_only():
    """The survivor's own call must stay on the survivor. That is why
    the ids are recorded rather than the counts."""
    with flask_app.app_context():
        keep, drop, mine, theirs = _pair_with_history()
        drop.stage = 'RFQ Generated'
        db.session.commit()
        lm.merge(drop, keep, actor=ADMIN)
        db.session.expire_all()

        ok, detail = lm.unmerge(Lead.query.get(drop.id), actor=ADMIN)
        assert ok, detail
        db.session.expire_all()

        assert LeadActivity.query.get(mine.id).lead_id == drop.id
        assert LeadActivity.query.get(theirs.id).lead_id == keep.id
        fresh = Lead.query.get(drop.id)
        assert fresh.merged_into_id is None
        assert fresh.stage == 'RFQ Generated'
        assert fresh.is_archived is False


def test_a_row_moved_by_a_person_since_the_merge_is_left_alone():
    """Their decision is newer than the merge."""
    with flask_app.app_context():
        keep, drop, mine, _t = _pair_with_history()
        third = _lead(company=f'{TAG} Third')
        lm.merge(drop, keep, actor=ADMIN)
        db.session.expire_all()

        moved = LeadActivity.query.get(mine.id)
        moved.lead_id = third.id
        db.session.commit()

        ok, _detail = lm.unmerge(Lead.query.get(drop.id), actor=ADMIN)
        assert ok
        db.session.expire_all()
        assert LeadActivity.query.get(mine.id).lead_id == third.id


def test_the_ledger_is_kept_and_marked_rather_than_deleted():
    """A merge that happened and was undone is two events. A deleted
    ledger row would make it none."""
    with flask_app.app_context():
        keep, drop, _m, _t = _pair_with_history()
        lm.merge(drop, keep, actor=ADMIN)
        db.session.expire_all()
        lm.unmerge(Lead.query.get(drop.id), actor=ADMIN)
        db.session.expire_all()
        rows = LeadMerge.query.filter_by(lead_id=drop.id).all()
        assert len(rows) == 1
        assert rows[0].reversed is True
        assert rows[0].reversed_by == ADMIN


def test_a_lead_that_was_never_merged_cannot_be_unmerged():
    with flask_app.app_context():
        lead = _lead()
        ok, why = lm.unmerge(lead, actor=ADMIN)
        assert ok is False
        assert 'not merged' in why


def test_a_merge_from_before_the_ledger_says_why_it_cannot_reverse():
    """Rather than reversing it wrongly, or claiming it did."""
    with flask_app.app_context():
        keep, drop, _m, _t = _pair_with_history()
        drop.merged_into_id = keep.id
        drop.is_archived = True
        db.session.commit()
        why = lm.unmerge_blockers(Lead.query.get(drop.id))
        assert 'no record of which rows' in why
        ok, _ = lm.unmerge(Lead.query.get(drop.id), actor=ADMIN)
        assert ok is False


def test_a_partial_merge_is_refused_rather_than_half_reversed():
    """A reversal that put some rows back and left the rest would be
    worse than no reversal at all."""
    with flask_app.app_context():
        keep, drop, _m, _t = _pair_with_history()
        lm.merge(drop, keep, actor=ADMIN)
        ledger = lm.last_merge(Lead.query.get(drop.id))
        ledger.partial = True
        db.session.commit()
        ok, why = lm.unmerge(Lead.query.get(drop.id), actor=ADMIN)
        assert ok is False
        assert 'could not record by id' in why


def test_a_won_lead_is_still_never_merged_away():
    """It is a reported figure. Unchanged by this release, and worth
    re-asserting beside the reversal: "it can be undone" is not a
    reason to loosen what may be done."""
    with flask_app.app_context():
        keep, drop, _m, _t = _pair_with_history()
        drop.stage = 'Won'
        db.session.commit()
        ok, why = lm.merge(drop, keep, actor=ADMIN)
        assert ok is False
        assert 'reported figure' in why


def test_the_admin_merge_routes_refuse_a_non_admin():
    with flask_app.app_context():
        _emp(OWNER, 'Owner Person', role='user')
        keep, drop, _m, _t = _pair_with_history()
        ids = (drop.id, keep.id)
    client = _client(OWNER, role='user')
    headers = {'X-CSRFToken': _token(client)}
    for path, body in (
            ('/api/admin/lead-duplicates/merge',
             {'lead_id': ids[0], 'into': ids[1]}),
            ('/api/admin/lead-duplicates/unmerge',
             {'lead_id': ids[0]})):
        r = client.post(path, json=body, headers=headers)
        assert r.status_code == 403, (path, r.status_code,
                                      r.get_json())


def test_the_admin_merge_routes_refuse_a_post_with_no_token():
    """A write with no token does nothing and says nothing — the
    failure this CRM has shipped six times."""
    with flask_app.app_context():
        _emp(ADMIN, 'Admin Person', role='admin')
        keep, drop, _m, _t = _pair_with_history()
        ids = (drop.id, keep.id)
    was = flask_app.config.get('WTF_CSRF_ENABLED', True)
    flask_app.config['WTF_CSRF_ENABLED'] = True
    try:
        r = _client(ADMIN).post('/api/admin/lead-duplicates/merge',
                                json={'lead_id': ids[0],
                                      'into': ids[1]})
    finally:
        flask_app.config['WTF_CSRF_ENABLED'] = was
    assert r.status_code == 400
    assert r.get_json()['code'] == 'csrf'
    with flask_app.app_context():
        assert Lead.query.get(ids[0]).merged_into_id is None


# ═══════════════════════════════════════════════════════════════════
# §2 — the report
# ═══════════════════════════════════════════════════════════════════
def test_the_report_pairs_each_set_once():
    """A list showing #1/#2 and #2/#1 would be read as two problems."""
    with flask_app.app_context():
        from app.services import lead_dupe_report as rep

        _lead(phone='9876543210', company=f'{TAG} One')
        _lead(phone='9876543210', company=f'{TAG} Two')
        pairs = rep.pairs()
        assert len(pairs) == 1
        assert pairs[0]['matched'] == ['phone']


def test_the_report_suggests_the_more_advanced_copy():
    """In this data the older copy is usually the empty one somebody
    marked Lost — QUESTIONS 71."""
    with flask_app.app_context():
        from app.services import lead_dupe_report as rep

        husk = _lead(phone='9876543210', stage='Lost',
                     company=f'{TAG} Husk',
                     created_at=datetime.utcnow() - timedelta(days=9))
        worked = _lead(phone='9876543210', stage='Under Negotiation',
                       company=f'{TAG} Worked')
        pair = rep.pairs()[0]
        assert pair['keep']['lead_id'] == worked.id
        assert pair['drop']['lead_id'] == husk.id


def test_the_report_script_can_never_merge():
    import ast

    path = os.path.join(_ROOT, 'scripts',
                        '2026_11_10_lead_duplicates.py')
    tree = ast.parse(open(path).read())
    options = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and getattr(node.func, 'attr', None) == 'add_argument'):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and \
                        isinstance(arg.value, str):
                    options.add(arg.value)
    assert '--check' in options
    assert '--apply' not in options
    assert not [o for o in options if 'merge' in o.lower()]

    called = {getattr(n.func, 'attr', None) for n in ast.walk(tree)
              if isinstance(n, ast.Call)}
    assert 'merge' not in called
    assert 'commit' not in called
    assert 'rollback' in called


# ═══════════════════════════════════════════════════════════════════
# §3 — which "PIC email", and the one that must not be written
# ═══════════════════════════════════════════════════════════════════
def test_the_employee_list_now_carries_the_address_for_everybody():
    """This single line was §3. The projection was narrowed for the
    assignment dropdowns, which need a code and a name, and that
    removed the one field anybody needed in order to contact the
    person in charge. Admins saw it; the sales team did not."""
    with flask_app.app_context():
        _emp(OWNER, 'Owner Person', role='user',
             email='zw.own@procamgroup.in')
    client = _client(OWNER, role='user')
    rows = client.get('/api/employees').get_json()
    mine = [r for r in rows if r['emp_code'] == OWNER]
    assert mine, 'the owner is not in the list at all'
    assert mine[0]['email'] == 'zw.own@procamgroup.in'


def test_the_mobile_number_is_still_not_in_the_bulk_list():
    """Nothing in the CRM shows that in bulk, and §3 asked for the
    email."""
    with flask_app.app_context():
        _emp(OWNER, 'Owner Person', role='user', mobile='9876543210')
    client = _client(OWNER, role='user')
    rows = client.get('/api/employees').get_json()
    mine = [r for r in rows if r['emp_code'] == OWNER][0]
    assert 'mobile' not in mine


def test_an_employee_address_is_never_written_into_the_customer_field():
    """The trap in §3. `leads.email` is the customer's. A Procam
    address in it means "Draft email" writes to us, the mail trail
    files our own message as the customer's reply, and the quotation
    goes to the wrong mailbox."""
    import ast

    path = os.path.join(_ROOT, 'scripts', '2026_11_10_pic_email.py')
    source = open(path).read()
    tree = ast.parse(source)

    # The only assignment to a lead attribute is `lead.email`, and the
    # value it takes comes from `_from_own_mail`, which refuses an
    # internal address.
    written = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute):
                    written.add(target.attr)
    assert written <= {'email'}, sorted(written)
    assert 'Employee' not in source.split('def _from_own_mail')[1] \
        .split('def ')[0]


def test_a_customer_address_is_taken_from_the_leads_own_mail():
    with flask_app.app_context():
        mod = _pic_script()
        _lead(pic=f'{TAG} Buyer', email=None,
              original_email_from='buyer@zzwa-client.example')
        theirs = mod.customer_side()
        assert len(theirs['fillable']) == 1
        _lead_row, address, where = theirs['fillable'][0]
        assert address == 'buyer@zzwa-client.example'
        assert 'arrived from' in where


def test_an_internal_address_is_not_a_candidate():
    """A lead forwarded in by a colleague: the outer sender is ours,
    and filling the customer field with it is the corruption."""
    with flask_app.app_context():
        mod = _pic_script()
        _lead(pic=f'{TAG} Buyer', email=None,
              original_email_from='colleague@procamlogistics.com')
        theirs = mod.customer_side()
        assert theirs['fillable'] == []
        assert len(theirs['named_no_email']) == 1


def test_a_procam_address_already_in_the_customer_field_is_reported():
    with flask_app.app_context():
        mod = _pic_script()
        _lead(pic=f'{TAG} Buyer', email='someone@procamgroup.in')
        theirs = mod.customer_side()
        assert len(theirs['email_is_ours']) == 1


def test_an_address_somebody_typed_is_never_overwritten():
    with flask_app.app_context():
        mod = _pic_script()
        _lead(pic=f'{TAG} Buyer', email='typed@zzwa-client.example',
              original_email_from='other@zzwa-client.example')
        theirs = mod.customer_side()
        assert theirs['fillable'] == []


def test_the_owner_side_reports_and_changes_nothing():
    with flask_app.app_context():
        mod = _pic_script()
        _emp(OWNER, 'No Address Person', role='user', email=None)
        _lead(assigned_to=OWNER)
        ours = mod.employee_side()
        assert [c for c, _n, _k in ours['no_address']] == [OWNER]


def _pic_script():
    path = os.path.join(_ROOT, 'scripts', '2026_11_10_pic_email.py')
    spec = importlib.util.spec_from_file_location('picmail', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ═══════════════════════════════════════════════════════════════════
# §4 — nothing dropped silently
# ═══════════════════════════════════════════════════════════════════
def test_the_status_script_only_reads():
    import ast

    path = os.path.join(_ROOT, 'scripts', 'mail_ingest_status.py')
    tree = ast.parse(open(path).read())
    options = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and getattr(node.func, 'attr', None) == 'add_argument'):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and \
                        isinstance(arg.value, str):
                    options.add(arg.value)
    assert '--check' in options
    assert '--apply' not in options

    called = {getattr(n.func, 'attr', None) for n in ast.walk(tree)
              if isinstance(n, ast.Call)}
    assert 'commit' not in called
    assert 'add' not in called
    assert 'delete' not in called
    assert 'rollback' in called


def test_the_status_script_knows_what_landing_means():
    mod = _status_script()
    from app.models.ingest_log import (ATTACHED, CREATED, OUTCOMES,
                                       REOPENED)
    assert set(mod.LANDED) == {CREATED, ATTACHED, REOPENED}
    for outcome in mod.LANDED:
        assert outcome in OUTCOMES


def _status_script():
    path = os.path.join(_ROOT, 'scripts', 'mail_ingest_status.py')
    spec = importlib.util.spec_from_file_location('ingstat', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ═══════════════════════════════════════════════════════════════════
# §5 — why one person cannot sign in
# ═══════════════════════════════════════════════════════════════════
def test_an_unknown_code_says_so():
    with flask_app.app_context():
        out = sd.diagnose('ZW_NOBODY')
        assert out['verdict'] == sd.NO_SUCH_CODE
        assert out['exists'] is False
        assert 'TMS code' in out['advice']


def test_the_code_is_read_the_way_the_login_form_reads_it():
    with flask_app.app_context():
        _emp(OWNER, 'Owner Person')
        out = sd.diagnose(f'  {OWNER.lower()}  ')
        assert out['exists'] is True
        assert out['code'] == OWNER


def test_a_deactivated_account_is_the_trap_this_exists_for():
    """`/login` filters on is_active before the password is checked,
    so a deactivated person gets the identical message to somebody
    typing a wrong password — and resetting the password will not
    help."""
    with flask_app.app_context():
        _emp(OWNER, 'Left Person', is_active=False)
        out = sd.diagnose(OWNER)
        assert out['verdict'] == sd.INACTIVE
        assert 'will not help' in out['advice']
        assert 'Reactivate' in out['advice']


def test_a_locked_account_reports_the_lock():
    with flask_app.app_context():
        emp = _emp(OWNER, 'Owner Person')
        emp.locked_until = datetime.utcnow() + timedelta(minutes=9)
        db.session.commit()
        out = sd.diagnose(OWNER)
        assert out['verdict'] == sd.LOCKED
        assert 'minute' in out['detail']


def test_an_expired_temporary_password_reports_itself():
    with flask_app.app_context():
        emp = _emp(OWNER, 'Owner Person')
        emp.must_change_pw = True
        emp.temp_password_expires_at = (datetime.utcnow()
                                        - timedelta(hours=1))
        db.session.commit()
        assert sd.diagnose(OWNER)['verdict'] == sd.TEMP_EXPIRED


def test_an_account_with_no_usable_hash_is_named():
    """A row created by an import or a seed: there is nothing they
    could type that would be accepted."""
    with flask_app.app_context():
        emp = _emp(OWNER, 'Owner Person')
        emp.password_hash = ''
        db.session.commit()
        assert sd.diagnose(OWNER)['verdict'] == sd.NO_PASSWORD

        emp.password_hash = 'plaintextpassword'
        db.session.commit()
        assert sd.diagnose(OWNER)['verdict'] == sd.NO_PASSWORD


def test_a_healthy_account_says_the_password_is_simply_wrong():
    with flask_app.app_context():
        _emp(OWNER, 'Owner Person')
        out = sd.diagnose(OWNER)
        assert out['verdict'] == sd.FINE
        assert 'Reset password' in out['advice']


def test_a_live_temporary_password_is_its_own_verdict():
    """Distinct from FINE: the password they were given may have been
    mistyped when it was passed on, and re-issuing is the fix."""
    with flask_app.app_context():
        emp = _emp(OWNER, 'Owner Person')
        emp.must_change_pw = True
        emp.temp_password_expires_at = (datetime.utcnow()
                                        + timedelta(hours=10))
        db.session.commit()
        assert sd.diagnose(OWNER)['verdict'] == sd.MUST_CHANGE


def test_it_never_returns_a_password_or_a_hash():
    with flask_app.app_context():
        _emp(OWNER, 'Owner Person')
        out = sd.diagnose(OWNER)
        blob = repr(out).lower()
        assert 'hash' not in blob
        assert 'password_hash' not in blob
        assert 'correct-horse' not in blob


def test_inactive_outranks_locked():
    """Reactivating is the thing that has to happen, and the lock will
    have expired by the time anybody gets to it."""
    with flask_app.app_context():
        emp = _emp(OWNER, 'Owner Person', is_active=False)
        emp.locked_until = datetime.utcnow() + timedelta(minutes=9)
        db.session.commit()
        assert sd.diagnose(OWNER)['verdict'] == sd.INACTIVE


def test_every_verdict_has_advice():
    for verdict in (sd.NO_SUCH_CODE, sd.INACTIVE, sd.NO_PASSWORD,
                    sd.LOCKED, sd.TEMP_EXPIRED, sd.MUST_CHANGE,
                    sd.FINE):
        assert sd.ADVICE[verdict].strip(), verdict


def test_the_admin_route_refuses_a_non_admin():
    with flask_app.app_context():
        _emp(OWNER, 'Owner Person', role='user')
    client = _client(OWNER, role='user')
    r = client.get(f'/api/admin/signin-check?code={OWNER}')
    assert r.status_code == 403


def test_the_admin_route_refuses_a_stranger():
    client = flask_app.test_client()
    assert client.get('/api/admin/signin-check?code=X').status_code == 401


def test_the_check_script_cannot_write():
    import ast

    path = os.path.join(_ROOT, 'scripts', 'signin_check.py')
    tree = ast.parse(open(path).read())
    options = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and getattr(node.func, 'attr', None) == 'add_argument'):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and \
                        isinstance(arg.value, str):
                    options.add(arg.value)
    assert '--apply' not in options
    assert not [o for o in options if 'reset' in o.lower()]

    called = {getattr(n.func, 'attr', None) for n in ast.walk(tree)
              if isinstance(n, ast.Call)}
    assert 'commit' not in called
    assert 'set_password' not in called
    assert 'rollback' in called
