"""
CRM-VERTICAL-SAVE-500-01 — Save worked and answered 500.

Reported 31 October 2026: Admin → Email → Vertical heads, Freight
Forwarding, Deputy "nobody" → Sanjeev Kumar Paliwal, Save shows
"500 Internal Server Error" and the change is saved anyway.

The cause is the cache, not the save:

    the save commits, which expires every attribute on the rows it
      touched;
    it clears `vertical_heads`' cache — but `sync_employee_flags()`
      runs next, calls `_load()`, and refills the cache with
      instances from that same request's session;
    the request ends and Flask-SQLAlchemy removes the session, so
      those instances are detached as well as expired;
    the redirect lands on the screen, `gaps()` reads
      `row.head_emp_code` off one of them inside the 30-second TTL,
      and SQLAlchemy raises DetachedInstanceError.

So the 500 was on the *redirect target*, which is why the change was
saved: it had been committed a request earlier.

Two things are tested here. The symptom, end to end, in the exact
order the report gives. And the shape underneath it: no module-level
cache may hand out ORM instances, because the next one that does
will fail the same way. Two others did —
`notification_policy` (Recipients) and `client_restrictions`, where
the failure would have been worse than a 500: `check()` catches
everything and answers "not restricted", so a blocked client would
have been let through with only a line in the log.
"""
import importlib
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'save500-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'Save500Test-123456')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'save500.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app = _main.app
flask_app.config['WTF_CSRF_ENABLED'] = False
db = _main.db
Employee = _main.Employee

from app.models.access import AccessProfile, DataScope   # noqa: E402
from app.models.email_admin import VerticalAlias, VerticalHead  # noqa: E402
from app.services import vertical_heads as vh            # noqa: E402

ADMIN = 'S5_ADMIN'
HEAD = 'S5_HEAD'
DEPUTY = 'S5_DEPUTY'
VERTICAL = 'Freight Forwarding'


def _purge():
    from app.models.audit import AuditEvent
    VerticalHead.query.filter(
        VerticalHead.vertical == VERTICAL).delete(synchronize_session=False)
    VerticalAlias.query.filter(
        VerticalAlias.alias.like('S5 %')).delete(synchronize_session=False)
    AuditEvent.query.filter(
        AuditEvent.action.like('vertical_%')).delete(
            synchronize_session=False)
    AccessProfile.query.filter(
        AccessProfile.emp_code.like('S5_%')).delete(synchronize_session=False)
    Employee.query.filter(
        Employee.emp_code.like('S5_%')).delete(synchronize_session=False)
    db.session.commit()
    vh.cache_clear()


@pytest.fixture(autouse=True)
def _clean():
    with flask_app.app_context():
        db.create_all()
        _purge()
        for code, name in ((ADMIN, 'Nilesh Kumar Sinha'),
                           (HEAD, 'Sanjna Vardhan'),
                           (DEPUTY, 'Sanjeev Kumar Paliwal')):
            db.session.add(Employee(
                emp_code=code, name=name, password_hash='x', role='admin',
                vertical='Project Freight', is_active=True,
                is_super_admin=True, must_change_pw=False,
                email=code.lower() + '@procamgroup.in'))
            db.session.add(AccessProfile(emp_code=code,
                                         data_scope=DataScope.ALL,
                                         perms=['admin.email',
                                                'admin.employees']))
        db.session.add(VerticalHead(vertical=VERTICAL,
                                    head_emp_code=HEAD, active=True))
        db.session.commit()
        vh.cache_clear()
        yield
        _purge()


def _client(code=ADMIN):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s['emp_code'] = code
        s['role'] = 'admin'
        s['must_change_pw'] = False
    return c


def _save(c, **over):
    data = {'vertical': VERTICAL, 'head': HEAD, 'deputy': DEPUTY,
            'active': 'on', 'note': ''}
    data.update(over)
    return c.post('/admin/email/verticals/save', data=data)


# ── the report, in its own order ─────────────────────────────────────
def test_setting_a_deputy_saves_and_the_screen_opens():
    c = _client()
    assert c.get('/admin/email/verticals').status_code == 200
    posted = _save(c)
    assert posted.status_code == 302
    # The 500 was here: on the page the redirect lands on, inside the
    # cache's 30-second window.
    assert c.get('/admin/email/verticals').status_code == 200
    with flask_app.app_context():
        row = VerticalHead.query.filter_by(vertical=VERTICAL).first()
        assert row.deputy_emp_code == DEPUTY


def test_the_save_is_post_redirect_get():
    c = _client()
    r = _save(c)
    assert r.status_code == 302
    assert '/admin/email/verticals' in r.headers['Location']
    assert 'saved=1' in r.headers['Location']


def test_the_screen_says_it_saved():
    c = _client()
    c.post('/admin/email/verticals/save',
           data={'vertical': VERTICAL, 'head': HEAD, 'deputy': DEPUTY,
                 'active': 'on', 'note': ''})
    page = c.get('/admin/email/verticals?saved=1').get_data(as_text=True)
    assert 'Saved' in page


def test_saving_twice_in_a_row_still_opens():
    """The cache lives 30 seconds, so the second save and every page
    load after it are inside the same window."""
    c = _client()
    for _ in range(3):
        assert _save(c).status_code == 302
        assert c.get('/admin/email/verticals').status_code == 200


def test_the_change_is_audited():
    """The audit row used to ride a transaction that only
    `sync_employee_flags()` happened to commit."""
    from app.models.audit import AuditEvent
    c = _client()
    _save(c)
    with flask_app.app_context():
        rows = AuditEvent.query.filter_by(
            action='vertical_head.changed').all()
        assert rows, 'the change was not recorded'
        assert rows[-1].new_value.get('deputy') == DEPUTY


def test_an_alias_change_is_audited_too():
    from app.models.audit import AuditEvent
    c = _client()
    r = c.post('/admin/email/verticals/alias',
               data={'alias': 'S5 Ocean Freight', 'maps_to': VERTICAL})
    assert r.status_code == 302
    with flask_app.app_context():
        assert AuditEvent.query.filter_by(
            action='vertical_alias.changed').count() == 1


# ── the shape underneath ─────────────────────────────────────────────
def test_the_cache_holds_no_orm_instances():
    """The rule this bug came from. An ORM instance in a cache that
    outlives its request is detached by the time the next request
    reads it, and expired too if anything committed."""
    from app.models.email_admin import NotificationPolicy
    from app.services import client_restrictions, notification_policy

    with flask_app.app_context():
        vh.cache_clear()
        heads, aliases = vh._load()
        for row in heads.values():
            assert isinstance(row, vh.Head), type(row)
        for target in aliases.values():
            assert isinstance(target, str)

        notification_policy.cache_clear()
        for row in notification_policy._stored().values():
            assert isinstance(row, notification_policy.Stored), type(row)
            assert not isinstance(row, NotificationPolicy)

        client_restrictions.cache_clear()
        for row in client_restrictions.live_rows():
            assert isinstance(row, client_restrictions.Live), type(row)


def test_a_cached_head_survives_its_session():
    """The exact failure: load the cache in one application context,
    commit, leave, and read it in the next."""
    with flask_app.app_context():
        vh.cache_clear()
        vh._load()
        row = VerticalHead.query.filter_by(vertical=VERTICAL).first()
        row.deputy_emp_code = DEPUTY
        db.session.commit()          # expires every loaded attribute
    with flask_app.app_context():
        # No cache_clear: this is the 30-second window the bug lived in.
        assert isinstance(vh.gaps(), list)
        assert vh.heads_for_vertical(VERTICAL) == [HEAD]
        assert vh.master_vertical(VERTICAL) == VERTICAL


def test_a_cached_restriction_survives_its_session():
    """Worse than a 500: `check()` catches everything and answers
    "not restricted", so this failure would let a blocked client
    through with nothing on screen to say so."""
    from app.models.restriction import ClientRestriction
    from app.services import client_restrictions as cr

    with flask_app.app_context():
        cr.cache_clear()
        row = ClientRestriction(
            status='blocked', company_name='S5 Blocked Client',
            name_key=cr.normalise('S5 Blocked Client'),
            reason_category='Payment default', reason_detail='Test',
            scope='entity')
        db.session.add(row)
        db.session.commit()
        cr.cache_clear()
        assert cr.check(company_name='S5 Blocked Client').blocked
        # Something else commits in this same session, expiring the
        # rows the cache is holding.
        db.session.add(ClientRestriction(
            status='caution', company_name='S5 Other',
            name_key=cr.normalise('S5 Other'),
            reason_category='Payment default', reason_detail='Test',
            scope='entity'))
        db.session.commit()
    with flask_app.app_context():
        assert cr.check(company_name='S5 Blocked Client').blocked, \
            'the gate failed open'
    with flask_app.app_context():
        ClientRestriction.query.filter(
            ClientRestriction.company_name.like('S5 %')).delete(
                synchronize_session=False)
        db.session.commit()
        cr.cache_clear()


def test_a_cached_policy_survives_its_session():
    from app.services import notification_policy as np

    with flask_app.app_context():
        np.cache_clear()
        np._stored()
        db.session.commit()
    with flask_app.app_context():
        assert isinstance(np.rows(), list)


# ── a side effect that fails is a warning, never a 500 ───────────────
def test_a_failing_side_effect_warns_and_keeps_the_change(monkeypatch):
    from app.services import audit

    def boom(*a, **kw):
        raise RuntimeError('the audit table is on fire')

    monkeypatch.setattr(audit, 'record', boom)
    c = _client()
    r = _save(c)
    assert r.status_code == 302
    assert 'warn=audit' in r.headers['Location']
    with flask_app.app_context():
        row = VerticalHead.query.filter_by(vertical=VERTICAL).first()
        assert row.deputy_emp_code == DEPUTY, 'the change was kept'
    page = c.get(r.headers['Location']).get_data(as_text=True)
    assert 'The change is saved' in page


def test_the_cache_is_cleared_last():
    """`cache_clear()` ran before `sync_employee_flags()`, which
    called `_load()` and refilled the cache with the very rows the
    commit had expired. Order is the fix as much as the record is."""
    import app.mailops.routes as mr
    import inspect
    src = inspect.getsource(mr.verticals_save)
    assert src.index("'employee flags'") < src.index("'cache'")
