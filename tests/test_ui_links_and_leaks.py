"""
Small defects found tracing each feature from the page to the database:
links to pages that do not exist, a drawer that drew reassignments as
blank rows, an HTML sink, and responses that told users server details.
"""
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'UiLinksTestOnly12345')
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'uilinks.db'))

from app import app as flask_app, db, Employee, Lead, LeadAttachment  # noqa

flask_app.config['WTF_CSRF_ENABLED'] = False
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(_ROOT, 'templates', *parts)) as fh:
        return fh.read()


def _routes():
    return {r.rule for r in flask_app.url_map.iter_rules()}


def test_every_copilot_chip_goes_to_a_page_that_exists():
    src = _read('_copilot_panel.html')
    block = src[src.index('function wireChips'):src.index('[data-pin]')]
    block = '\n'.join(l for l in block.splitlines()
                      if not l.strip().startswith('//'))
    targets = re.findall(r"'(/[a-z-]+)(?:/|\?|')", block)
    assert targets, 'no chip targets found'
    rules = _routes()
    for t in set(targets):
        assert any(r == t or r.startswith(t + '/') for r in rules), \
            f'chip target {t} has no route'
    for dead in ('/opportunities/', '/accounts/', "'/handovers/'"):
        assert dead not in block, dead


def test_the_drawer_draws_reassignments():
    src = _read('app.html')
    i = src.index("if (h.kind === 'assignment')")
    body = src[i:i + 900]
    for key in ('h.from_primary', 'h.to_primary', 'h.note'):
        assert key in body


def test_the_card_scanner_escapes_names_before_markup():
    src = _read('business_card', 'scan.html')
    assert '<strong>${a.name}</strong>' not in src
    assert src.count('<strong>${escHtml(a.name)}</strong>') == 2


def test_proposal_buttons_do_not_put_keys_inside_js_strings():
    src = _read('intake', 'intelligence.html')
    assert "applyProp('${" not in src and "dismissProp('${" not in src
    assert 'applyProp(this.dataset.key)' in src


def test_a_missing_attachment_does_not_reveal_the_server_path():
    with flask_app.app_context():
        db.create_all()
        e = Employee.query.filter_by(emp_code='UIADM').first()
        if e is None:
            e = Employee(emp_code='UIADM', name='UIADM')
            db.session.add(e)
        e.role, e.is_active, e.must_change_pw = 'admin', True, False
        e.vertical, e.is_super_admin = 'All', True
        lead = Lead(company='Ui Links Co', source='manual',
                    assigned_to='UIADM')
        db.session.add(lead)
        db.session.flush()
        att = LeadAttachment(lead_id=lead.id, filename='q.pdf',
                             storage_path='/var/secret/place/q.pdf')
        db.session.add(att)
        db.session.commit()
        lid, aid = lead.id, att.id
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code='UIADM', name='UIADM', role='admin', vertical='All')
    r = c.get(f'/api/leads/{lid}/attachments/{aid}/download')
    assert r.status_code == 404
    assert b'/var/secret' not in r.data


def test_ordinary_users_are_not_told_the_model_host(monkeypatch):
    monkeypatch.setenv('PROCAM_AI_BASE_URL', 'http://10.0.0.7:11434/v1')
    with flask_app.app_context():
        db.create_all()
        e = Employee.query.filter_by(emp_code='UIREP').first()
        if e is None:
            e = Employee(emp_code='UIREP', name='UIREP')
            db.session.add(e)
        e.role, e.is_active, e.must_change_pw = 'user', True, False
        e.vertical, e.is_super_admin = 'All', False
        db.session.commit()
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code='UIREP', name='UIREP', role='user', vertical='All')
    r = c.get('/api/copilot/suggestions')
    assert r.status_code == 200
    assert b'10.0.0.7' not in r.data
    assert 'available' in r.get_json()['model']
