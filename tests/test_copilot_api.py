"""
Procam AI over HTTP — the surface a browser actually touches.

The RBAC suite proves the boundary at the service layer. This proves
the endpoints do not route around it: that anonymous callers are
refused, that no request parameter can widen a scope, that the panel is
present on every screen, and that the audit row and its feedback line
up with the answer the user saw.
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'CopilotApiOnly12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'copilot_api.db')
os.environ.pop('PROCAM_AI_BASE_URL', None)

from app import app as flask_app, db, Employee, Lead      # noqa: E402
from app.access.service import set_profile                # noqa: E402
from app.models.access import DataScope                   # noqa: E402

flask_app.config['WTF_CSRF_ENABLED'] = False


@pytest.fixture(scope='module')
def world():
    with flask_app.app_context():
        db.create_all()
        for code, role in (('CAADM', 'admin'), ('CAREP', 'user')):
            e = Employee.query.filter_by(emp_code=code).first()
            if e is None:
                e = Employee(emp_code=code, name=code)
                db.session.add(e)
            e.role, e.is_active, e.must_change_pw = role, True, False
            e.vertical, e.is_super_admin = 'All', False
        db.session.commit()

        ids = {}
        for code, tag in (('CAREP', 'mine'), ('CAADM', 'theirs')):
            name = f'ApiCo {code}'
            l = Lead.query.filter_by(company=name).first()
            if l is None:
                l = Lead(company=name, source='manual')
                db.session.add(l)
            l.assigned_to, l.stage = code, 'Quoted'
            db.session.flush()
            ids[tag] = l.id
        set_profile('CAREP', DataScope.OWN, ['module.funnels'],
                    actor='CAADM')
        set_profile('CAADM', DataScope.ALL,
                    ['module.funnels', 'admin.access'], actor='CAADM')
        db.session.commit()
        return ids


def _c(code='CAREP'):
    c = flask_app.test_client()
    with flask_app.app_context():
        emp = Employee.query.filter_by(emp_code=code).first()
        role = emp.role
    with c.session_transaction() as s:
        s.update(emp_code=code, name=code, role=role, vertical='All')
    return c


# ── authentication ───────────────────────────────────────────────────
def test_anonymous_cannot_ask(world):
    r = flask_app.test_client().post('/api/copilot/ask', json={'q': 'my day'})
    assert r.status_code == 401


def test_anonymous_cannot_read_suggestions(world):
    r = flask_app.test_client().get('/api/copilot/suggestions')
    assert r.status_code == 401


# ── no request parameter may widen the scope ─────────────────────────
def test_the_request_cannot_ask_as_somebody_else(world):
    """The obvious attack on an endpoint like this: pass an emp_code.

    The scope is resolved from the session, server-side, and there is no
    parameter that reaches it — so these extras are simply ignored.
    """
    for extra in ({'emp_code': 'CAADM'}, {'scope': 'all'},
                  {'data_scope': 'all'}, {'as_user': 'CAADM'},
                  {'sc': 'all'}):
        payload = {'q': 'my open leads'}
        payload.update(extra)
        body = _c('CAREP').post('/api/copilot/ask', json=payload).get_json()
        names = {r['Company'] for r in body.get('rows', [])}
        assert names <= {'ApiCo CAREP'}, f'widened by {extra}'


def test_the_admin_really_does_see_more(world):
    """The control: without it the test above passes on an empty CRM."""
    body = _c('CAADM').post('/api/copilot/ask',
                            json={'q': 'my open leads'}).get_json()
    names = {r['Company'] for r in body.get('rows', [])}
    assert 'ApiCo CAREP' in names and 'ApiCo CAADM' in names


# ── the answer carries what the panel needs ──────────────────────────
def test_an_answer_carries_its_scope_note(world):
    body = _c('CAREP').post('/api/copilot/ask',
                            json={'q': 'my open leads'}).get_json()
    assert 'own records' in (body.get('scope_note') or '')


def test_an_answer_carries_its_sources(world):
    body = _c('CAREP').post('/api/copilot/ask',
                            json={'q': 'my open leads'}).get_json()
    assert body.get('sources'), 'every material answer cites its source'


def test_an_answer_can_be_rated(world):
    """§6.4 — feedback attaches to the answer the user actually saw."""
    from app.models.copilot import CopilotLog

    body = _c('CAREP').post('/api/copilot/ask',
                            json={'q': 'my open leads'}).get_json()
    log_id = body.get('log_id')
    assert log_id, 'the answer must be identifiable for feedback'

    r = _c('CAREP').post('/api/copilot/feedback',
                         json={'log_id': log_id, 'helpful': False,
                               'reason': 'Wrong record'})
    assert r.status_code == 200
    with flask_app.app_context():
        row = db.session.get(CopilotLog, log_id)
        assert row.helpful is False
        assert row.feedback_reason == 'Wrong record'


def test_an_unknown_feedback_reason_is_refused(world):
    body = _c('CAREP').post('/api/copilot/ask',
                            json={'q': 'my open leads'}).get_json()
    r = _c('CAREP').post('/api/copilot/feedback',
                         json={'log_id': body['log_id'], 'helpful': False,
                               'reason': 'because I said so'})
    assert r.status_code == 400


# ── the audit trail, §11 ─────────────────────────────────────────────
def test_every_question_is_logged(world):
    from app.models.copilot import CopilotLog

    with flask_app.app_context():
        before = CopilotLog.query.count()
    _c('CAREP').post('/api/copilot/ask', json={'q': 'my open leads'})
    with flask_app.app_context():
        assert CopilotLog.query.count() == before + 1
        row = CopilotLog.query.order_by(CopilotLog.id.desc()).first()
        assert row.emp_code == 'CAREP'
        assert row.intent == 'leads_open'
        assert row.data_scope == DataScope.OWN


def test_an_unrecognised_question_is_logged_without_an_intent(world):
    """The column that tells an admin what to build next."""
    from app.models.copilot import CopilotLog

    _c('CAREP').post('/api/copilot/ask',
                     json={'q': 'please forecast next quarter revenue by '
                                'region and vertical with a chart'})
    with flask_app.app_context():
        row = CopilotLog.query.order_by(CopilotLog.id.desc()).first()
        assert not row.intent
        assert row.answered is False


def test_the_log_holds_no_answer_rows(world):
    """§11 records what was asked and which sources were touched — not
    the business data itself, which has different retention."""
    from app.models.copilot import CopilotLog

    _c('CAADM').post('/api/copilot/ask', json={'q': 'my open leads'})
    with flask_app.app_context():
        row = CopilotLog.query.order_by(CopilotLog.id.desc()).first()
        blob = ' '.join(str(v) for v in row.to_dict().values())
        assert 'ApiCo CAREP' not in blob


# ── analytics is admin-only ──────────────────────────────────────────
def test_analytics_needs_admin(world):
    assert _c('CAREP').get('/api/copilot/analytics').status_code == 403
    assert _c('CAADM').get('/api/copilot/analytics').status_code == 200


def test_analytics_page_renders_for_an_admin(world):
    assert _c('CAADM').get('/copilot-analytics').status_code == 200


# ── §6.1 the panel is on every screen ────────────────────────────────
def test_the_launcher_is_present_on_the_main_app(world):
    html = _c('CAREP').get('/app').get_data(as_text=True)
    assert 'Ask Procam AI' in html
    assert 'paiPanel' in html


def test_the_launcher_is_present_on_a_standalone_screen(world):
    """Those pages include the shared topbar, not app.html — the panel
    has to be on both or it is not "every screen"."""
    import os as _os
    root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    with open(_os.path.join(root, 'templates', '_crm_topbar.html')) as fh:
        assert '_copilot_panel.html' in fh.read()


def test_the_panel_has_a_keyboard_shortcut_and_escape(world):
    import os as _os
    root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    with open(_os.path.join(root, 'templates',
                            '_copilot_panel.html')) as fh:
        src = fh.read()
    assert 'metaKey' in src and "'k'" in src
    assert "'Escape'" in src
    # §6.7 — reduced motion respected
    assert 'prefers-reduced-motion' in src


# ── §12 fallback ─────────────────────────────────────────────────────
def test_it_answers_with_no_model_host(world):
    from app.copilot import model as model_mod

    assert model_mod.available() is False
    body = _c('CAREP').post('/api/copilot/ask',
                            json={'q': 'my open leads'}).get_json()
    assert body['ok'] and body['rows']
    assert body['model_used'] is False


def test_a_public_ai_host_is_refused_outright(world, monkeypatch):
    """§3.1 is enforced in code, not left to configuration review."""
    from app.copilot import model as model_mod

    for host in ('https://api.openai.com/v1', 'https://api.groq.com/openai/v1',
                 'https://api.anthropic.com'):
        monkeypatch.setenv('PROCAM_AI_BASE_URL', host)
        assert model_mod.available() is False
        assert 'public' in (model_mod.refusal() or '').lower()

    monkeypatch.setenv('PROCAM_AI_BASE_URL', 'http://10.0.0.9:8000/v1')
    assert model_mod.available() is True


# ── §6.4 pinning · §6.5 morning brief ────────────────────────────────
def test_a_question_can_be_pinned_and_unpinned(world):
    body = _c('CAREP').post('/api/copilot/ask',
                            json={'q': 'my open leads'}).get_json()
    r = _c('CAREP').post('/api/copilot/pin',
                         json={'log_id': body['log_id'], 'pinned': True})
    assert r.status_code == 200
    assert any(p['question'] == 'my open leads'
               for p in r.get_json()['pinned'])

    r = _c('CAREP').post('/api/copilot/pin',
                         json={'log_id': body['log_id'], 'pinned': False})
    assert not any(p['question'] == 'my open leads'
                   for p in r.get_json()['pinned'])


def test_pinning_saves_the_question_not_the_answer(world):
    """A saved table of last month's pipeline would quietly become wrong
    while still looking authoritative. The question re-runs."""
    from app.models.copilot import CopilotLog

    body = _c('CAREP').post('/api/copilot/ask',
                            json={'q': 'my open leads'}).get_json()
    _c('CAREP').post('/api/copilot/pin',
                     json={'log_id': body['log_id'], 'pinned': True})
    with flask_app.app_context():
        row = db.session.get(CopilotLog, body['log_id'])
        blob = ' '.join(str(v) for v in row.to_dict().values())
        assert 'ApiCo' not in blob, 'the answer rows must not be stored'


def test_you_cannot_pin_somebody_elses_question(world):
    body = _c('CAADM').post('/api/copilot/ask',
                            json={'q': 'my open leads'}).get_json()
    r = _c('CAREP').post('/api/copilot/pin',
                         json={'log_id': body['log_id'], 'pinned': True})
    assert r.status_code == 400


def test_the_morning_brief_matches_my_day(world):
    """§6.5 is built from the same intent, so the greeting and the
    question can never disagree."""
    brief = _c('CAREP').get('/api/copilot/brief').get_json()
    assert brief['ok']
    asked = _c('CAREP').post('/api/copilot/ask',
                             json={'q': 'my day'}).get_json()
    assert brief['brief']['headline'] == asked['headline']


def test_the_brief_is_scoped_like_everything_else(world):
    rep = _c('CAREP').get('/api/copilot/brief').get_json()['brief']
    adm = _c('CAADM').get('/api/copilot/brief').get_json()['brief']
    assert rep['figures'] != adm['figures'] or rep['empty']
