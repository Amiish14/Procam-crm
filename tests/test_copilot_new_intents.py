"""
Release 6 (Group L) — the new Copilot intents, under the §16 rules.

The same four things are asked of every intent added here, because they
are the four ways a Copilot answer goes wrong in production:

  1. it answers the right person with the right rows;
  2. it refuses, helpfully, when the Access Matrix says no;
  3. it never shows one person another person's records;
  4. it says "that source is not configured" rather than producing a
     plausible list, when the sibling stream it reads has not landed.

And one thing is asked of the module as a whole: that no handler
resolves its own scope. That is checked textually rather than by
behaviour, because a handler that calls `scope.current()` passes every
behavioural test in this file — the signed-in user and the scope the
test passes in are the same person for most of a test run, and the bug
only appears when they differ, which is exactly the case nobody writes
a test for.
"""
import os
import re
import sys
import tempfile
from datetime import date, datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'GroupLTestOnly12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'copilot_group_l.db')
# No model host. These intents must answer with the rules alone.
os.environ.pop('PROCAM_AI_BASE_URL', None)

from app import app as flask_app, db, Company, Employee, Lead   # noqa: E402
from app.access import scope as scope_mod                       # noqa: E402
from app.access.service import set_profile                      # noqa: E402
from app.copilot import intents as catalogue                    # noqa: E402
from app.copilot import service as svc                          # noqa: E402
from app.copilot import workbench_intents as gl                 # noqa: E402
from app.models.access import DataScope                         # noqa: E402

flask_app.config['WTF_CSRF_ENABLED'] = False

#: Everything the matrix can grant, for the "entitled" scopes.
ALL_PERMS = ['module.rfq', 'module.quotes', 'module.handovers',
             'module.funnels', 'module.competitors', 'reports.action',
             'reports.accounts', 'reports.competitor', 'admin.master',
             'admin.access']

#: The intents this release adds, with the permission each declares.
GROUP_L = {
    'what_to_update_today': None,
    'quotes_overdue': 'module.quotes',
    'rfqs_older_than': 'module.rfq',
    'accounts_not_contacted': None,
    'who_handles_account': None,
    'my_weekly_review': None,
    'projects_with_epc': 'module.competitors',
    'competitors_recent_projects': 'module.competitors',
    'vessel_operators_calling': None,
    'contacts_at_company': None,
}


@pytest.fixture(scope='module')
def world():
    """One salesperson, one colleague in another vertical, and an admin.

    MINE / THEIRS again: every row below exists in two copies so that
    "sees fewer rows" and "sees exactly their own rows" can be told
    apart. A suite that returns nothing to everybody passes the first
    and fails the second.
    """
    with flask_app.app_context():
        # The review and directory streams ship their own tables with
        # their own migration, and a deployed server has run it — so the
        # fixture imports their models before create_all(). The INTEL
        # tables are deliberately left out: that absence is what the
        # "source not configured" tests below are measuring, and a
        # mocked absence would stop measuring it the day the migration
        # lands.
        from app.models import review as _review_models      # noqa: F401
        from app.directory import models as _directory_models  # noqa: F401
        db.create_all()

        for code, role, vertical, head in (
                ('GLADM', 'admin', 'All', False),
                ('GLREP', 'user', 'Heavy Transport', False),
                ('GLOUT', 'user', 'Warehousing', False)):
            emp = Employee.query.filter_by(emp_code=code).first()
            if emp is None:
                emp = Employee(emp_code=code, name=f'{code} Person')
                db.session.add(emp)
            emp.role, emp.vertical, emp.is_active = role, vertical, True
            emp.is_vertical_head, emp.must_change_pw = head, False
            emp.is_super_admin = False
        db.session.commit()

        long_ago = datetime.utcnow() - timedelta(days=200)
        today = date.today()
        ids = {}
        for code, tag in (('GLREP', 'mine'), ('GLOUT', 'theirs')):
            acct = Company.query.filter_by(name=f'GL Acct {code}').first()
            if acct is None:
                acct = Company(name=f'GL Acct {code}')
                db.session.add(acct)
            acct.is_active = True
            acct.pic_emp_code = code
            acct.vertical = 'Heavy Transport'
            # Quiet for two hundred days, so both show up in the
            # 90-day question and the scoping is what decides.
            acct.last_activity_at = long_ago
            db.session.flush()

            lead = Lead.query.filter_by(company=f'GL Acct {code}').first()
            if lead is None:
                lead = Lead(company=f'GL Acct {code}', source='manual')
                db.session.add(lead)
            lead.assigned_to = code
            lead.assigned_name = f'{code} Person'
            lead.stage = 'Quoted'
            lead.company_id = acct.id
            lead.project = f'GL Project {code}'
            # No follow-up date, so the Workbench has something to say
            # about it whatever else is true of the fixture.
            lead.followup_date = None
            lead.is_archived = False
            db.session.flush()

            from app import Contact
            person = Contact.query.filter_by(
                name=f'GL Contact {code}').first()
            if person is None:
                person = Contact(name=f'GL Contact {code}')
                db.session.add(person)
            person.account_id = acct.id
            person.company_id = acct.id
            person.company = acct.name
            person.assigned_to = code
            person.designation = 'Logistics Head'
            person.email = f'{code.lower()}@example.invalid'
            db.session.flush()

            ids[tag] = {'account': acct.id, 'account_name': acct.name,
                        'lead': lead.id, 'code': code,
                        'contact': person.id,
                        'contact_name': person.name}

        from app.models.rfq import RFQ
        for code, tag, age, due in (('GLREP', 'mine', 30, today -
                                     timedelta(days=20)),
                                    ('GLOUT', 'theirs', 30, today -
                                     timedelta(days=20))):
            number = f'R-GL-{code}'
            rfq = RFQ.query.filter_by(rfq_number=number).first()
            if rfq is None:
                rfq = RFQ(rfq_number=number, subject=f'GL RFQ {code}')
                db.session.add(rfq)
            rfq.subject = f'GL RFQ {code}'
            rfq.account_id = ids[tag]['account']
            rfq.lead_id = ids[tag]['lead']
            rfq.lead_driver = code
            rfq.status = 'Received'
            rfq.received_date = today - timedelta(days=age)
            rfq.quote_by_date = due
            db.session.flush()
            ids[tag]['rfq_number'] = number

        # A young RFQ with a deadline still ahead: the control for both
        # ageing questions. Without it, "more than N days old" could be
        # returning everything and still pass.
        fresh = RFQ.query.filter_by(rfq_number='R-GL-FRESH').first()
        if fresh is None:
            fresh = RFQ(rfq_number='R-GL-FRESH', subject='GL fresh RFQ')
            db.session.add(fresh)
        fresh.account_id = ids['mine']['account']
        fresh.lead_id = ids['mine']['lead']
        fresh.lead_driver = 'GLREP'
        fresh.status = 'Received'
        fresh.received_date = today
        fresh.quote_by_date = today + timedelta(days=10)

        db.session.commit()

    yield ids

    # Quotes and RFQs survive the lead/company wipes other modules do in
    # their own fixtures, and turn up as orphans in their counts. Clean
    # up after ourselves — tests/test_copilot_rbac.py learnt this the
    # hard way.
    with flask_app.app_context():
        from app import Contact
        from app.models.rfq import RFQ
        RFQ.query.filter(RFQ.rfq_number.like('R-GL-%')).delete(
            synchronize_session=False)
        Contact.query.filter(Contact.name.like('GL Contact %')).delete(
            synchronize_session=False)
        Lead.query.filter(Lead.company.like('GL Acct %')).delete(
            synchronize_session=False)
        Company.query.filter(Company.name.like('GL Acct %')).delete(
            synchronize_session=False)
        db.session.commit()


def _scope_for(code, data_scope=DataScope.OWN, perms=ALL_PERMS):
    set_profile(code, data_scope, list(perms), actor='GLADM')
    return scope_mod.for_employee(code)


def _text(result):
    """Everything a person would read in one Result, flattened."""
    parts = [result.headline or '']
    parts += [str(v) for row in (result.rows or []) for v in row.values()]
    parts += [str(v) for v in (result.figures or {}).values()]
    parts += list(result.notes or [])
    return ' | '.join(parts)


# ══════════════════════════════════════════════════════════════════
#  0 · the contract the whole module is held to
# ══════════════════════════════════════════════════════════════════
def test_no_handler_resolves_its_own_scope():
    """A handler that calls scope.current() reads the session instead of
    the scope it was handed, and the RBAC sweep stops meaning anything.

    Checked on the source text, because a handler with this bug behaves
    correctly in every test where the two happen to agree.
    """
    source = open(gl.__file__, encoding='utf-8').read()
    # Prose about the rule is fine; a call is not.
    calls = re.findall(r'(?<!`)\bscope\s*\.\s*current\s*\(', source)
    calls += re.findall(r'\bsc_mod\s*\.\s*current\s*\(', source)
    calls += re.findall(r'\bscope_mod\s*\.\s*current\s*\(', source)
    assert not calls, f'{len(calls)} call(s) to current() in {gl.__file__}'


def test_every_new_intent_is_registered_with_the_permission_it_declares():
    for key, permission in GROUP_L.items():
        intent = catalogue.get(key)
        assert intent is not None, f'{key} is not in the catalogue'
        assert intent.permission == permission, key
        assert intent.label, key
        assert intent.examples, f'{key} has no example phrasings'


def test_every_new_intent_answers_with_a_headline(world):
    """§12 — no model configured, and still an answer, never a blank."""
    with flask_app.app_context():
        sc = _scope_for('GLADM', DataScope.ALL)
        for key in GROUP_L:
            result = catalogue.get(key).handler(
                sc, {'account': world['mine']['account_name'],
                     'days': 5, 'limit': 5})
            assert result.headline, f'{key} produced no headline'


def test_the_routing_rules_are_installed_once():
    """install_patterns() runs on import; running it again must not
    double the rules, or a reloaded module would shadow the catalogue
    with duplicates."""
    before = list(svc._PATTERNS)
    assert gl.install_patterns() is False
    assert svc._PATTERNS == before
    keys = [key for _rx, key, _spec in svc._PATTERNS]
    for key in GROUP_L:
        assert keys.count(key) >= 1, f'{key} has no routing rule'


# ══════════════════════════════════════════════════════════════════
#  1 · the right rows for the right person
# ══════════════════════════════════════════════════════════════════
def test_the_workbench_question_shows_the_viewers_own_work(world):
    with flask_app.app_context():
        sc = _scope_for('GLREP')
        result = catalogue.get('what_to_update_today').handler(sc, {})
        assert result.rows, 'the rep has open work and should see it'
        accounts = {r['Account'] for r in result.rows}
        assert world['mine']['account_name'] in accounts
        assert world['theirs']['account_name'] not in accounts


def test_overdue_quotations_are_the_ones_past_the_deadline(world):
    with flask_app.app_context():
        sc = _scope_for('GLREP')
        result = catalogue.get('quotes_overdue').handler(sc, {})
        numbers = {r['RFQ'] for r in result.rows}
        assert numbers == {world['mine']['rfq_number']}
        assert 'R-GL-FRESH' not in numbers        # its deadline is ahead
        assert result.figures['overdue'] == 1
        assert result.rows[0]['Days late'] > 0


def test_rfq_ageing_honours_the_number_of_days_asked_for(world):
    with flask_app.app_context():
        sc = _scope_for('GLREP')
        wide = catalogue.get('rfqs_older_than').handler(sc, {'days': 5})
        assert {r['RFQ'] for r in wide.rows} == {world['mine']['rfq_number']}
        assert wide.filters['days'] == 5

        # The same RFQ is thirty days old, so a higher threshold must
        # exclude it — a filter that is read but not applied is the
        # commonest way one of these answers goes quietly wrong.
        narrow = catalogue.get('rfqs_older_than').handler(sc, {'days': 90})
        assert narrow.empty is True
        assert not narrow.rows


def test_accounts_not_contacted_lists_the_viewers_own_book(world):
    with flask_app.app_context():
        sc = _scope_for('GLREP')
        result = catalogue.get('accounts_not_contacted').handler(
            sc, {'days': 90})
        names = {r['Account'] for r in result.rows}
        assert names == {world['mine']['account_name']}
        assert result.figures['count'] == 1
        assert result.rows[0]['Quiet (days)'] >= 90


def test_who_handles_an_account_answers_with_routing(world):
    with flask_app.app_context():
        sc = _scope_for('GLREP')
        result = catalogue.get('who_handles_account').handler(
            sc, {'account': world['mine']['account_name']})
        assert result.empty is False
        assert world['mine']['account_name'] in result.headline
        assert result.figures['primary'] == 'GLREP'
        # Answered through the directory's own who_handles, not a
        # second reading of companies.pic_emp_code.
        assert result.sources == ['Contact directory']


def test_who_handles_asks_back_when_no_account_is_named():
    with flask_app.app_context():
        sc = _scope_for('GLREP')
        result = catalogue.get('who_handles_account').handler(sc, {})
        assert result.empty is True
        assert 'which account' in result.headline.lower()


def test_contacts_at_a_company_come_from_the_directory(world):
    """And only the ones this viewer may see. The directory's own
    `base_query` decides that; this asserts the Copilot did not reach
    round it."""
    with flask_app.app_context():
        sc = _scope_for('GLREP')
        result = catalogue.get('contacts_at_company').handler(
            sc, {'account': world['mine']['account_name']})
        assert result.sources == ['Contact directory']
        assert {r['Name'] for r in result.rows} == {
            world['mine']['contact_name']}

        other = catalogue.get('contacts_at_company').handler(
            sc, {'account': world['theirs']['account_name']})
        assert world['theirs']['contact_name'] not in _text(other)


def test_the_weekly_review_comes_from_the_review_service(world):
    """The review screen's own agenda, not a second one assembled here:
    the two must never present different weeks to the same person."""
    with flask_app.app_context():
        sc = _scope_for('GLREP')
        result = catalogue.get('my_weekly_review').handler(sc, {})
        assert result.headline
        assert result.sources == ['Weekly sales review']
        assert world['theirs']['account_name'] not in _text(result)


def test_the_weekly_review_falls_back_and_says_so(world, monkeypatch):
    """On a server without the review service the answer is still
    useful — and admits which figures it is not showing, rather than
    passing the Workbench summary off as the review."""
    real = gl._module

    def only_workbench(dotted, *names):
        return None if dotted.startswith('app.review') else real(dotted,
                                                                 *names)

    monkeypatch.setattr(gl, '_module', only_workbench)
    with flask_app.app_context():
        sc = _scope_for('GLREP')
        result = catalogue.get('my_weekly_review').handler(sc, {})
        assert result.headline
        assert any('not installed' in n for n in result.notes)


# ══════════════════════════════════════════════════════════════════
#  2 · entitlement, separately from scope
# ══════════════════════════════════════════════════════════════════
@pytest.mark.parametrize('key,permission', [
    (k, p) for k, p in GROUP_L.items() if p])
def test_an_intent_without_its_permission_is_refused_helpfully(
        world, key, permission):
    with flask_app.app_context():
        sc = _scope_for('GLREP', DataScope.OWN, perms=[])
        assert sc.can(permission) is False
        answer = svc._answer(
            'group l permission probe',
            svc.Plan(key, {}), sc, 0.0)
        assert answer.result.rows == []
        assert answer.result.restricted is True
        assert 'access' in (answer.prose or '').lower()


def test_the_unpermissioned_intents_still_answer_an_unentitled_viewer(
        world):
    """The other half of §6.2. Routing and a person's own book are safe
    at every scope; gating them teaches people the panel is broken."""
    with flask_app.app_context():
        sc = _scope_for('GLREP', DataScope.OWN, perms=[])
        for key in (k for k, p in GROUP_L.items() if p is None):
            intent = catalogue.get(key)
            assert intent.permission is None
            result = intent.handler(
                sc, {'account': world['mine']['account_name']})
            assert result.headline, key


# ══════════════════════════════════════════════════════════════════
#  3 · scope isolation — never another person's records
# ══════════════════════════════════════════════════════════════════
def test_no_new_intent_shows_one_person_anothers_records(world):
    """The sweep. Only the account NAME may surface, and only from the
    routing intents — §5.1 says existence and the owner are not secret.
    An RFQ number, a project name or a count that includes the other
    vertical is a leak."""
    with flask_app.app_context():
        sc = _scope_for('GLREP')
        leaked = []
        for key in GROUP_L:
            intent = catalogue.get(key)
            if intent.permission and not sc.can(intent.permission):
                continue
            result = intent.handler(
                sc, {'account': world['mine']['account_name'],
                     'days': 1, 'limit': 50})
            blob = _text(result)
            for secret in (world['theirs']['rfq_number'],
                           'GL Project GLOUT',
                           world['theirs']['account_name']):
                if secret in blob:
                    leaked.append(f'{key}: disclosed {secret!r}')
        assert not leaked, '; '.join(leaked)


def test_the_colleague_sees_their_own_rows_and_the_admin_sees_both(world):
    """The control. Every isolation assertion above is worthless if the
    rows are simply never returned to anybody."""
    with flask_app.app_context():
        other = _scope_for('GLOUT')
        result = catalogue.get('quotes_overdue').handler(other, {})
        assert {r['RFQ'] for r in result.rows} == {
            world['theirs']['rfq_number']}

        admin = _scope_for('GLADM', DataScope.ALL)
        whole = catalogue.get('quotes_overdue').handler(admin, {})
        assert {r['RFQ'] for r in whole.rows} == {
            world['mine']['rfq_number'], world['theirs']['rfq_number']}


def test_naming_an_out_of_scope_account_yields_routing_and_nothing_else(
        world):
    with flask_app.app_context():
        sc = _scope_for('GLREP')
        result = catalogue.get('who_handles_account').handler(
            sc, {'account': world['theirs']['account_name']})
        # Existence and the owner: yes, that is the point of the intent.
        assert world['theirs']['account_name'] in result.headline
        assert result.figures['primary'] == 'GLOUT'
        # Anything about the account's business: no.
        assert result.rows == []
        assert any('outside your data scope' in n for n in result.notes)


# ══════════════════════════════════════════════════════════════════
#  4 · an absent sibling module, said out loud
# ══════════════════════════════════════════════════════════════════
#: The intents that read a stream which may not have landed, with the
#: module each of them wants.
ABSENT_SOURCES = [
    ('projects_with_epc', 'app.intel.projects'),
    ('competitors_recent_projects', 'app.intel.competitors'),
    ('vessel_operators_calling', 'app.intel.vendors'),
    ('contacts_at_company', 'app.directory.service'),
]


@pytest.mark.parametrize('key,module', ABSENT_SOURCES)
def test_an_unconfigured_source_is_empty_with_a_reason(world, key, module,
                                                       monkeypatch):
    """Never an invented row. When the stream is missing the answer says
    which source is missing, in a note, and returns nothing."""
    # Force the absence, so the test means the same thing before and
    # after the sibling stream merges.
    monkeypatch.setattr(gl, '_module', lambda dotted, *n: None)
    monkeypatch.setattr(gl, '_table', lambda name: False)

    with flask_app.app_context():
        sc = _scope_for('GLADM', DataScope.ALL)
        result = catalogue.get(key).handler(
            sc, {'account': world['mine']['account_name'], 'port': 'Chennai'})
        assert result.empty is True
        assert result.rows == []
        assert result.notes, f'{key} gave no reason'
        assert 'not configured' in ' '.join(result.notes).lower()


def test_an_absent_workbench_does_not_break_the_panel(world, monkeypatch):
    """The Workbench is a sibling stream too. If it goes, the Copilot
    says so; it does not raise into the panel."""
    monkeypatch.setattr(gl, '_module', lambda dotted, *n: None)
    with flask_app.app_context():
        sc = _scope_for('GLREP')
        for key in ('what_to_update_today', 'my_weekly_review'):
            result = catalogue.get(key).handler(sc, {})
            assert result.empty is True
            assert result.headline
            assert any('not configured' in n.lower() for n in result.notes)


def test_a_missing_table_is_reported_not_raised(world):
    """The intel tables arrive with their own migration. Until it has
    run the question must answer, not 500 — and `_table()` must be what
    notices, so nothing imports a model whose foreign key points at a
    table that does not exist yet.

    Whether the migration has run is not this test's business: run on
    its own the intel tables are absent, run with the whole suite
    another module may have created them. Both are valid states of a
    deployment and the intent has to behave in each, so the assertion
    is on the behaviour and not on the state.
    """
    with flask_app.app_context():
        assert gl._table('companies') is True
        assert gl._table('a_table_no_stream_will_ever_add') is False

        sc = _scope_for('GLADM', DataScope.ALL)
        result = catalogue.get('projects_with_epc').handler(sc, {})
        assert result.headline                       # never a blank panel
        if result.empty and not gl._table('intel_project_facts'):
            said = ' '.join([result.headline] + list(result.notes))
            assert 'app/intel' in said or 'not configured' in said


# ══════════════════════════════════════════════════════════════════
#  5 · end to end, through ask()
# ══════════════════════════════════════════════════════════════════
def test_the_questions_in_the_brief_reach_these_intents_and_answer(world):
    """Routing and answering together: a rule that classifies correctly
    into a handler that raises is still a broken feature."""
    asked = {
        'what do I need to update today': 'what_to_update_today',
        'show overdue quotations': 'quotes_overdue',
        'which RFQs are more than 5 days old': 'rfqs_older_than',
        'which accounts have I not contacted in 90 days':
            'accounts_not_contacted',
        'who handles this account': 'who_handles_account',
        'prepare my weekly sales review': 'my_weekly_review',
        'which tracked projects appointed an EPC': 'projects_with_epc',
        'which competitors recently executed warehouse projects':
            'competitors_recent_projects',
        'which MPV operators call Chennai': 'vessel_operators_calling',
        'which contacts do we know at this company': 'contacts_at_company',
    }
    with flask_app.app_context():
        sc = _scope_for('GLREP')
        for question, key in asked.items():
            answer = svc.ask(question, sc=sc)
            assert answer.intent_key == key, f'{question!r} → ' \
                                             f'{answer.intent_key!r}'
            assert answer.prose, question
            assert 'went wrong' not in (answer.result.headline or '')
            assert world['theirs']['account_name'] not in _text(
                answer.result)


def test_the_port_a_question_names_reaches_the_handler():
    """"Which MPV operators call Chennai" must not quietly become
    "which MPV operators call anywhere" — the port is the question."""
    key, params = svc._match_patterns('which MPV operators call Chennai')
    assert key == 'vessel_operators_calling'
    assert params.get('vessel_type') == 'mpv'
    # The classifier has one name extractor and it is called `account`;
    # the handler reads the port from there. See the module docstring.
    assert (params.get('port') or params.get('account')) == 'Chennai'
