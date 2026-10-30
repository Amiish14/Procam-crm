"""
No screen may show only an error.

CRM-LEAD-FLOW-01 §5. The finding: when the CRM refuses something it
says "no" and nothing else, so the user rings the Tech team. Every
refusal now carries what happened, why, and numbered steps with
buttons that take the person there.

The wording lives in `crm_help_guides` so an administrator can fix a
sentence without a deploy. The tests below hold the two things that
make that safe: a code with no row still answers (from the built-in
seed), and an unknown code answers too rather than showing a blank
box — a refusal that explains nothing is the thing this replaces.
"""
import importlib
import json
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'guide-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'GuideTest-123456')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'guide.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
importlib.import_module('app.models.help_content')
flask_app = _main.app
flask_app.config['WTF_CSRF_ENABLED'] = False
db = _main.db
Employee = _main.Employee

from app.services import guided_help as gh                # noqa: E402

ADMIN, USER = 'GH_ADMIN', 'GH_USER'


def _flag(state):
    from app.models.integration import AppSetting
    from app.services import flags
    row = AppSetting.query.filter_by(key='flag.' + gh.FLAG).first()
    if row is None:
        row = AppSetting(key='flag.' + gh.FLAG)
        db.session.add(row)
    row.value_json = json.dumps(bool(state))
    db.session.commit()
    flags.cache_clear()


def _purge():
    from app.models.help_content import HelpGuide, HelpGuideShown
    HelpGuide.query.delete(synchronize_session=False)
    HelpGuideShown.query.delete(synchronize_session=False)
    Employee.query.filter(Employee.emp_code.like('GH_%')).delete(
        synchronize_session=False)
    _flag(False)
    db.session.commit()


@pytest.fixture(autouse=True)
def _clean():
    with flask_app.app_context():
        db.create_all()
        _purge()
        for code, role in ((ADMIN, 'admin'), (USER, 'user')):
            db.session.add(Employee(
                emp_code=code, name=code, password_hash='x', role=role,
                is_active=True,
                email=code.lower() + '@procamlogistics.com'))
        db.session.commit()
        _flag(True)
        yield
        _purge()


def _client(code=ADMIN, role='admin'):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s['emp_code'] = code
        s['role'] = role
    return c


# ── the shape of a guide ─────────────────────────────────────────────
def test_every_seeded_guide_has_all_three_parts():
    """What happened, why, what to do. A guide missing one of them
    is the error message it was meant to replace."""
    for row in gh.SEED:
        assert row['title'].strip(), row['code']
        assert row['why'].strip(), row['code']
        assert row['steps'], row['code']
        for step in row['steps']:
            assert step.strip(), row['code']


def test_every_guide_offers_a_way_forward():
    for row in gh.SEED:
        assert row['actions'], row['code']
        for action in row['actions']:
            assert action['label'].strip()
            assert action['action'] in gh.ACTIONS, action


def test_the_catalogue_covers_the_codes_the_brief_named():
    wanted = {'CLIENT_BLOCKED', 'NO_ACCOUNT', 'NO_OWNER',
              'NOT_YOUR_LEAD', 'STAGE_FIELDS_MISSING',
              'HOLD_NOT_ALLOWED', 'POSSIBLE_DUPLICATE',
              'DELETE_NOT_ALLOWED', 'MAIL_NOT_SENT',
              'RATE_SOURCING_EMPTY', 'QUOTE_EXPIRED',
              'SESSION_EXPIRED', 'NO_PERMISSION'}
    assert wanted <= set(gh.CODES)


def test_no_guide_mentions_a_code_or_a_vendor():
    """Plain words only: no stack traces, no error numbers, and no
    AI provider name anywhere a user can read."""
    banned = ('traceback', 'exception', 'http 4', 'http 5', 'null',
              'openai', 'anthropic', 'claude', 'gpt', 'llm')
    for row in gh.SEED:
        text = ' '.join([row['title'], row['why']] + row['steps']).lower()
        for word in banned:
            assert word not in text, (row['code'], word)


# ── reading one ──────────────────────────────────────────────────────
def test_a_guide_with_no_row_falls_back_to_the_built_in_words():
    with flask_app.app_context():
        found = gh.guide('NO_ACCOUNT')
        assert found['title'] == gh.SEED_BY_CODE['NO_ACCOUNT']['title']
        assert found['steps']


def test_an_edited_guide_beats_the_built_in_one():
    with flask_app.app_context():
        from app.models.help_content import HelpGuide
        db.session.add(HelpGuide(code='NO_ACCOUNT',
                                 title='Pick the customer first.',
                                 why='Because.', steps=['Do it.'],
                                 actions=[], active=True))
        db.session.commit()
        assert gh.guide('NO_ACCOUNT')['title'] == 'Pick the customer first.'


def test_a_guide_switched_off_falls_back_rather_than_vanishing():
    with flask_app.app_context():
        from app.models.help_content import HelpGuide
        db.session.add(HelpGuide(code='NO_ACCOUNT', title='Hidden',
                                 why='', steps=[], actions=[],
                                 active=False))
        db.session.commit()
        assert gh.guide('NO_ACCOUNT')['title'] != 'Hidden'
        assert gh.guide('NO_ACCOUNT')['steps']


def test_an_unknown_code_still_answers():
    """A blank box is the failure this module exists to end."""
    with flask_app.app_context():
        found = gh.guide('NOT_A_REAL_CODE')
        assert found['title'].strip()
        assert found['steps']
        assert found['actions']


def test_the_context_travels_with_the_guide():
    with flask_app.app_context():
        found = gh.guide('STAGE_FIELDS_MISSING',
                         {'missing': ['quote_no'], 'lead_id': 7})
        assert found['context']['missing'] == ['quote_no']


def test_a_refusal_keeps_the_error_key_it_always_had():
    """Every caller and every older test reads `error`. The guide
    is added beside it, never instead of it."""
    with flask_app.app_context():
        body, status = gh.refusal('NO_ACCOUNT')
        assert status == 400
        assert body['ok'] is False
        assert body['error'] == body['guide']['title']
        assert body['guide']['steps']


# ── the catalogue and the admin screen ───────────────────────────────
def test_the_catalogue_lists_every_code_without_seeding():
    with flask_app.app_context():
        assert len(gh.catalogue()) >= len(gh.CODES)


def test_seeding_is_idempotent_and_never_overwrites():
    with flask_app.app_context():
        from app.models.help_content import HelpGuide
        assert gh.seed(actor=ADMIN) == len(gh.SEED)
        row = HelpGuide.query.filter_by(code='NO_ACCOUNT').one()
        row.title = 'Edited by a person'
        db.session.commit()
        assert gh.seed(actor=ADMIN) == 0
        assert HelpGuide.query.filter_by(
            code='NO_ACCOUNT').one().title == 'Edited by a person'


def test_the_api_serves_one_guide():
    data = _client(USER, 'user').get(
        '/api/help/guides/NO_ACCOUNT').get_json()
    assert data['ok'] is True
    assert data['guide']['code'] == 'NO_ACCOUNT'
    assert data['guide']['steps']


def test_the_api_needs_a_session():
    assert flask_app.test_client().get(
        '/api/help/guides/NO_ACCOUNT').status_code == 401


def test_the_admin_screen_renders_and_lists_the_catalogue():
    r = _client().get('/admin/help-texts')
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'Help texts' in body
    assert 'NO_ACCOUNT' in body
    assert 'show only an error' in body


def test_a_user_cannot_edit_the_wording():
    r = _client(USER, 'user').get('/admin/help-texts')
    assert r.status_code in (302, 403)


def test_an_admin_can_rewrite_a_sentence_without_a_deploy():
    r = _client().post('/api/admin/help-guides',
                       json={'code': 'NO_ACCOUNT',
                             'title': 'Name the customer first.',
                             'why': 'Every lead has one.',
                             'steps': 'Type three letters.\nPick it.'})
    assert r.status_code == 200, r.get_data(as_text=True)
    with flask_app.app_context():
        found = gh.guide('NO_ACCOUNT')
        assert found['title'] == 'Name the customer first.'
        assert found['steps'] == ['Type three letters.', 'Pick it.']
        assert found['last_edited_by'] == ADMIN


def test_a_guide_with_no_title_is_refused():
    r = _client().post('/api/admin/help-guides',
                       json={'code': 'NO_ACCOUNT', 'title': '  '})
    assert r.status_code == 400
    assert 'tells nobody anything' in r.get_json()['error']


def test_the_admin_can_load_the_catalogue_into_the_database():
    r = _client().post('/api/admin/help-guides/seed')
    assert r.get_json()['added'] == len(gh.SEED)


# ── measuring it (§5.3) ──────────────────────────────────────────────
def test_showing_a_guide_is_logged():
    with flask_app.app_context():
        from app.models.help_content import HelpGuideShown
        _client(USER, 'user').get(
            '/api/help/guides/NO_ACCOUNT?page=lead&lead_id=99')
        row = HelpGuideShown.query.filter_by(code='NO_ACCOUNT').one()
        assert row.emp_code == USER
        assert row.lead_id == 99
        assert row.clicked is None


def test_pressing_a_guides_button_is_recorded_against_that_showing():
    with flask_app.app_context():
        from app.models.help_content import HelpGuideShown
        c = _client(USER, 'user')
        c.get('/api/help/guides/NO_ACCOUNT')
        c.post('/api/help/guides/NO_ACCOUNT/clicked',
               json={'label': 'Link an account'})
        rows = HelpGuideShown.query.filter_by(code='NO_ACCOUNT').all()
        assert len(rows) == 1, 'one event, not two'
        assert rows[0].clicked == 'Link an account'


def test_the_weekly_table_counts_shown_and_acted_on():
    with flask_app.app_context():
        c = _client(USER, 'user')
        for _ in range(3):
            c.get('/api/help/guides/NO_OWNER')
        c.post('/api/help/guides/NO_OWNER/clicked',
               json={'label': 'Assign to the account owner'})
        row = [r for r in gh.weekly() if r['code'] == 'NO_OWNER'][0]
        assert row['shown'] == 3
        assert row['acted'] == 1
        assert row['acted_pct'] == 33.3


def test_logging_never_breaks_the_guide():
    """Best-effort: a failure to log must not swallow the help."""
    with flask_app.app_context():
        assert gh.record('NO_ACCOUNT', emp_code='X' * 400) in (True, False)
        assert gh.guide('NO_ACCOUNT')['steps']


# ── the flag ─────────────────────────────────────────────────────────
def test_the_flag_is_off_by_default():
    from app.services import flags
    assert flags.DEFAULTS[gh.FLAG] is False


def test_the_catalogue_is_readable_even_with_the_flag_off():
    """The words are not the behaviour. Reading them with the flag
    off is how an administrator writes them before switching on."""
    with flask_app.app_context():
        _flag(False)
        assert gh.enabled() is False
        assert gh.guide('NO_ACCOUNT')['steps']
