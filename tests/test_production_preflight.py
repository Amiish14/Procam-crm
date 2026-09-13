"""
The preflight catches the failures a deploy actually hits, and never
prints a secret.
"""
import os
import sqlite3
import subprocess
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, 'scripts'))

import production_preflight as pf                              # noqa: E402
from data_quality_report import read_only_engine               # noqa: E402


def _status(rep, name):
    return [r for r in rep.rows if r['check'] == name][0]['status']


@pytest.fixture()
def clean_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith(('PROCAM_AI_', 'MS_', 'EMAIL_', 'LEAD_INTAKE',
                         'GROQ_', 'ANTHROPIC_', 'NOTIFY_')) or k in (
                'SECRET_KEY', 'ADMIN_INITIAL_PASSWORD', 'URL_PREFIX',
                'DEBUG', 'SESSION_COOKIE_SECURE'):
            monkeypatch.delenv(k, raising=False)
    return monkeypatch


def test_a_placeholder_secret_fails_and_is_never_printed(clean_env):
    clean_env.setenv('SECRET_KEY', 'procam-crm-secret-change-me-2025')
    clean_env.setenv('EMAIL_WEBHOOK_SECRET', 'set-a-random-string-here')
    rep = pf.Report()
    pf.check_config(rep, '/nonexistent/.env')
    assert _status(rep, 'SECRET_KEY') == pf.FAIL
    assert _status(rep, 'EMAIL_WEBHOOK_SECRET') == pf.FAIL
    text = repr(rep.rows)
    assert 'change-me-2025' not in text and 'set-a-random' not in text


def test_a_real_configuration_passes(clean_env):
    clean_env.setenv('SECRET_KEY', 'k' * 64)
    clean_env.setenv('EMAIL_WEBHOOK_SECRET', 'w' * 40)
    clean_env.setenv('URL_PREFIX', '/CRM')
    rep = pf.Report()
    pf.check_config(rep, '/nonexistent/.env')
    for name in ('SECRET_KEY', 'EMAIL_WEBHOOK_SECRET', 'URL_PREFIX',
                 'PROCAM_AI_ACTIONS', 'DEBUG', 'SESSION_COOKIE_SECURE'):
        assert _status(rep, name) == pf.PASS, name


def test_the_published_bootstrap_password_fails(clean_env):
    clean_env.setenv('ADMIN_INITIAL_PASSWORD', 'admin@Procam25')
    rep = pf.Report()
    pf.check_config(rep, '/nonexistent/.env')
    assert _status(rep, 'ADMIN_INITIAL_PASSWORD') == pf.FAIL


@pytest.mark.parametrize('url,private', [
    ('http://10.0.0.7:11434/v1', True),
    ('http://127.0.0.1:8080', True),
    ('http://localhost:11434', True),
    ('https://api.openai.com/v1', False),
    ('https://api.groq.com/openai/v1', False),
    ('https://openrouter.ai/api/v1', False),
    ('http://8.8.8.8/v1', False),
])
def test_ai_hosts_must_be_provably_private(url, private):
    assert pf.host_is_private(url, resolve=False)[0] is private


def test_a_public_ai_host_fails_the_preflight(clean_env):
    clean_env.setenv('PROCAM_AI_BASE_URL', 'https://api.together.xyz/v1')
    rep = pf.Report()
    pf.check_config(rep, '/nonexistent/.env')
    assert _status(rep, 'PROCAM_AI_BASE_URL') == pf.FAIL


def test_schema_drift_is_found_against_the_real_models():
    """Boot the real app into a file, drop a column the code needs, and
    the preflight must name it — the outage a restart-before-migrate
    would cause."""
    path = os.path.join(tempfile.mkdtemp(), 'drift.db')
    env = dict(os.environ, DATABASE_URL='sqlite:///' + path,
               SECRET_KEY='test', ADMIN_INITIAL_PASSWORD='DriftTestOnly12345',
               SESSION_COOKIE_SECURE='false')
    subprocess.run([sys.executable, '-c', 'import app, app.models; '
                    'app.app.app_context().push(); app.db.create_all()'],
                   cwd=_ROOT, env=env, check=True, capture_output=True,
                   timeout=120)
    db = sqlite3.connect(path)
    db.execute('ALTER TABLE leads DROP COLUMN vertical_reason')
    db.execute('DROP INDEX IF EXISTS ix_leads_classification')
    db.commit()
    db.close()
    rep = pf.Report()
    with read_only_engine('sqlite:///' + path).connect() as conn:
        missing = pf.check_schema(rep, conn, pf.expected_schema())
    assert missing == ['leads.vertical_reason']
    assert _status(rep, 'columns') == pf.FAIL
    assert 'ix_leads_classification' in [
        r for r in rep.rows if r['check'] == 'indexes'][0]['detail']


def test_a_missing_backup_fails(tmp_path, monkeypatch):
    path = tmp_path / 'live.db'
    sqlite3.connect(path).execute('CREATE TABLE leads (id INTEGER)').close()
    monkeypatch.setattr(pf, '_ROOT', str(tmp_path))
    rep = pf.Report()
    with read_only_engine('sqlite:///' + str(path)).connect() as conn:
        pf.check_ops(rep, conn, str(path))
    assert _status(rep, 'backups') == pf.FAIL
    (tmp_path / 'backups').mkdir()
    (tmp_path / 'backups' / 'procam_crm.db.bak-today').write_bytes(
        path.read_bytes())
    rep = pf.Report()
    with read_only_engine('sqlite:///' + str(path)).connect() as conn:
        pf.check_ops(rep, conn, str(path))
    assert _status(rep, 'backups') == pf.PASS
