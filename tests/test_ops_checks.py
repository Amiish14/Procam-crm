"""
The operations checks say the right thing about production, and cannot
hurt it: nothing writes to the database, nothing touches the network
unless allowed, nothing crashes when a tool or permission is missing,
and no secret reaches a result.

The library is loaded by path, the way scripts/ops_status.py loads it,
so these tests never import app.py either.
"""
import hashlib
import json
import os
import sqlite3
import ssl
import sys
import time
from collections import namedtuple
from datetime import datetime, timedelta, timezone

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, 'scripts'))

import ops_status                                               # noqa: E402

C = ops_status.load_checks()
OK, WARN, FAIL, UNKNOWN = C.OK, C.WARN, C.FAIL, C.UNKNOWN


def _ts(**delta):
    """A timestamp as SQLAlchemy stores it: naive UTC text."""
    return (datetime.now(timezone.utc) - timedelta(**delta)).strftime(
        '%Y-%m-%d %H:%M:%S.%f')


def _make_db(path, leads=20):
    db = sqlite3.connect(path)
    db.executescript('''
        CREATE TABLE employees (id INTEGER PRIMARY KEY, emp_code TEXT);
        CREATE TABLE companies (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE leads (id INTEGER PRIMARY KEY, company TEXT,
            original_email_body TEXT, updated_at TEXT);
        CREATE TABLE lead_notes (id INTEGER PRIMARY KEY, lead_id INTEGER,
            note_text TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE lead_emails (id INTEGER PRIMARY KEY, lead_id INTEGER,
            created_at TEXT);
        CREATE TABLE email_events (id INTEGER PRIMARY KEY, received_at TEXT,
            status TEXT);
        CREATE TABLE email_classifications (id INTEGER PRIMARY KEY,
            review_state TEXT, created_at TEXT);
        CREATE TABLE copilot_chunk (id INTEGER PRIMARY KEY, lead_id INTEGER,
            text TEXT, indexed_at TEXT);
    ''')
    db.executemany('INSERT INTO employees (emp_code) VALUES (?)',
                   [(f'E{i}',) for i in range(5)])
    db.executemany('INSERT INTO companies (name) VALUES (?)',
                   [(f'Co {i}',) for i in range(10)])
    db.executemany(
        'INSERT INTO leads (company, original_email_body, updated_at) '
        'VALUES (?, ?, ?)',
        [(f'Co {i}', 'please quote', _ts(days=3)) for i in range(leads)])
    db.commit()
    db.close()


@pytest.fixture()
def live(tmp_path):
    path = str(tmp_path / 'live.db')
    _make_db(path)
    return path


@pytest.fixture()
def ctx(tmp_path, live):
    (tmp_path / 'backups').mkdir()
    return C.Context(root=str(tmp_path), db_path=live,
                     backups_dir=str(tmp_path / 'backups'),
                     env_path=str(tmp_path / '.env'), base_url='',
                     sub_id_file=str(tmp_path / '.leads_subscription_id'),
                     network=False, schema=False, environ={})


def _exec(path, sql, params=()):
    db = sqlite3.connect(path)
    db.execute(sql, params)
    db.commit()
    db.close()


def _backup(ctx, name='procam_crm-2026-09-01-013000-daily.db', age_h=1):
    path = os.path.join(ctx.backups_dir, name)
    src = sqlite3.connect(ctx.db_path)
    dst = sqlite3.connect(path)
    src.backup(dst)
    dst.close()
    src.close()
    t = time.time() - age_h * 3600
    os.utime(path, (t, t))
    return path


@pytest.fixture()
def no_network(monkeypatch):
    """Any call off this machine fails the test."""
    def http(url, **kw):
        assert url.startswith('http://127.0.0.1'), f'network call to {url}'
        raise OSError('connection refused')

    def cert(*a, **kw):
        raise AssertionError('TLS handshake attempted')

    monkeypatch.setattr(C, '_http', http)
    monkeypatch.setattr(C, '_peer_cert', cert)


# ── read-only guarantees ─────────────────────────────────────────────
def test_the_database_connection_refuses_writes(live):
    conn = C.open_ro(live)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO leads (company) VALUES ('x')")
    conn.close()


def test_a_full_run_changes_no_byte_of_the_database_or_backups(
        ctx, no_network, monkeypatch):
    monkeypatch.setattr(C, '_run', lambda cmd, timeout=None: None)
    b = _backup(ctx, 'procam_crm-x-pre-deploy.db')

    def digest(p):
        return hashlib.sha256(open(p, 'rb').read()).hexdigest(), \
            os.path.getmtime(p)

    before = digest(ctx.db_path), digest(b)
    results = C.run_checks(ctx)
    assert (digest(ctx.db_path), digest(b)) == before
    assert not os.path.exists(ctx.db_path + '-journal')
    assert {r['key'] for r in results} == set(C.CHECK_KEYS)
    for r in results:
        assert set(r) == {'key', 'label', 'status', 'detail', 'measured_at',
                          'value'}
        assert r['status'] in (OK, WARN, FAIL, UNKNOWN)


def test_without_tools_or_network_every_check_degrades_instead_of_crashing(
        ctx, no_network, monkeypatch):
    monkeypatch.setattr(C, '_run', lambda cmd, timeout=None: None)
    monkeypatch.setattr(os.path, 'isdir', lambda p: False)  # no /proc
    by_key = {r['key']: r for r in C.run_checks(ctx)}
    for key in ('timers', 'deployment', 'rollback', 'workers', 'graph_token',
                'tls_certificate', 'graph_secret_expiry', 'schema'):
        assert by_key[key]['status'] == UNKNOWN, key
        assert 'crashed' not in by_key[key]['detail'], key


def test_a_check_that_raises_is_reported_unknown(ctx):
    def boom(_ctx):
        raise RuntimeError('permission denied')
    [r] = C.run_checks(ctx, checks=[('boom', boom)])
    assert r['status'] == UNKNOWN and 'permission denied' in r['detail']


def test_secret_values_are_scrubbed_from_any_result(ctx, monkeypatch):
    monkeypatch.setenv('MS_CLIENT_SECRET', 'super-secret-value-123')
    monkeypatch.setenv('GRAPH_CLIENT_SECRET_EXPIRES', '2030-01-01')

    def leaky(_ctx):
        return C.result('leak', 'leak', FAIL,
                        'error: super-secret-value-123 rejected',
                        {'nested': ['super-secret-value-123', '2030-01-01']})
    [r] = C.run_checks(ctx, checks=[('leak', leaky)])
    assert 'super-secret-value-123' not in json.dumps(r)
    assert '2030-01-01' in json.dumps(r)


# ── health ───────────────────────────────────────────────────────────
def test_health(ctx, monkeypatch):
    monkeypatch.setattr(C, '_http', lambda url, **kw: (200, b'{"ok": true}'))
    r = C.check_health(ctx)
    assert r['status'] == OK and r['value']['http_status'] == 200
    monkeypatch.setattr(C, 'HEALTH_SLOW_MS', -1)
    assert C.check_health(ctx)['status'] == WARN
    monkeypatch.setattr(C, '_http', lambda url, **kw: (503, b'{"ok": false}'))
    assert C.check_health(ctx)['status'] == FAIL

    def refused(url, **kw):
        raise OSError('connection refused')
    monkeypatch.setattr(C, '_http', refused)
    r = C.check_health(ctx)
    assert r['status'] == FAIL and 'did not answer' in r['detail']


# ── backups and restore ──────────────────────────────────────────────
def test_backup_age_and_size(ctx):
    assert C.check_backups(ctx)['status'] == FAIL
    b = _backup(ctx, age_h=2)
    r = C.check_backups(ctx)
    assert r['status'] == OK and r['value']['count'] == 1
    t = time.time() - 30 * 3600
    os.utime(b, (t, t))
    assert C.check_backups(ctx)['status'] == WARN
    t = time.time() - 80 * 3600
    os.utime(b, (t, t))
    assert C.check_backups(ctx)['status'] == FAIL


def test_a_backup_under_half_the_live_size_warns(ctx):
    _backup(ctx)
    _exec(ctx.db_path, 'CREATE TABLE padding (x BLOB)')
    _exec(ctx.db_path, 'INSERT INTO padding VALUES (?)', (b'0' * 400_000,))
    assert 'half' in C.check_backups(ctx)['detail']
    assert C.check_backups(ctx)['status'] == WARN


def test_restore_verification_passes_a_good_backup(ctx):
    _backup(ctx)
    r = C.check_restore(ctx)
    assert r['status'] == OK, r['detail']
    assert r['value']['backup']['leads'] == 20 == r['value']['live']['leads']


def test_restore_verification_fails_an_unreadable_backup(ctx):
    with open(os.path.join(ctx.backups_dir, 'procam_crm-bad.db'), 'wb') as f:
        f.write(b'this is not a database' * 100)
    r = C.check_restore(ctx)
    assert r['status'] == FAIL


def test_restore_verification_fails_when_quick_check_does(ctx, monkeypatch):
    _backup(ctx)
    real = C._scalar
    monkeypatch.setattr(C, '_scalar', lambda conn, sql, params=(): (
        '*** in database main *** page 3 is never used'
        if sql == 'PRAGMA quick_check' else real(conn, sql, params)))
    assert C.check_restore(ctx)['status'] == FAIL


def test_restore_notices_rows_missing_from_live(ctx):
    _backup(ctx)
    _exec(ctx.db_path, 'DELETE FROM leads WHERE id > 5')
    r = C.check_restore(ctx)
    assert r['status'] == WARN and 'fewer rows' in r['detail']


def test_restore_fails_when_the_backup_lacks_a_core_table(ctx):
    _backup(ctx)
    _exec(ctx.db_path, 'CREATE TABLE opportunities (id INTEGER)')
    r = C.check_restore(ctx)
    assert r['status'] == FAIL and 'opportunities' in r['detail']


# ── Graph ────────────────────────────────────────────────────────────
_CREDS = {'MS_TENANT_ID': 'tenant-guid', 'MS_CLIENT_ID': 'client-guid',
          'MS_CLIENT_SECRET': 'the-client-secret-value'}


def _graph(monkeypatch, token_status=200, token_body=None, subs=None,
           subs_status=200):
    calls = []

    def http(url, data=None, headers=None, timeout=None):
        calls.append((url, data, headers, timeout))
        if 'login.microsoftonline.com' in url:
            body = token_body if token_body is not None else \
                {'access_token': 'eyJ-the-token', 'expires_in': 3599}
            return token_status, json.dumps(body).encode()
        if url.endswith('/subscriptions'):
            assert headers['Authorization'] == 'Bearer eyJ-the-token'
            return subs_status, json.dumps({'value': subs or []}).encode()
        raise AssertionError(url)
    monkeypatch.setattr(C, '_http', http)
    return calls


def _net_ctx(ctx, **env):
    ctx.environ = dict(_CREDS, **env)
    ctx.network = True
    return ctx


def test_graph_token_is_acquired_and_never_shown(ctx, monkeypatch):
    calls = _graph(monkeypatch)
    r = C.check_graph_token(_net_ctx(ctx))
    assert r['status'] == OK
    assert 'eyJ-the-token' not in json.dumps(r)
    url, data, _h, timeout = calls[0]
    assert url.endswith('/tenant-guid/oauth2/v2.0/token')
    assert b'grant_type=client_credentials' in data
    assert timeout is None or timeout <= 10     # the default is short


def test_an_expired_client_secret_fails_with_the_fix(ctx, monkeypatch):
    _graph(monkeypatch, token_status=401, token_body={
        'error': 'invalid_client',
        'error_description': 'AADSTS7000222: The provided client secret '
                             'keys for app are expired. Trace ID: abc'})
    r = C.check_graph_token(_net_ctx(ctx))
    assert r['status'] == FAIL and 'EXPIRED' in r['detail']
    assert 'the-client-secret-value' not in json.dumps(r)
    assert 'Trace ID' not in r['detail']


def test_graph_token_unknown_without_credentials_network_or_answer(
        ctx, monkeypatch):
    assert C.check_graph_token(ctx)['status'] == UNKNOWN      # no creds
    ctx.environ = dict(_CREDS)
    ctx._graph = None
    assert 'network' in C.check_graph_token(ctx)['detail']   # --no-network

    def down(url, **kw):
        raise OSError('timed out')
    monkeypatch.setattr(C, '_http', down)
    ctx.network, ctx._graph = True, None
    assert C.check_graph_token(ctx)['status'] == UNKNOWN


@pytest.mark.parametrize('days,status', [(100, OK), (30, WARN), (5, FAIL),
                                         (-2, FAIL)])
def test_client_secret_expiry_from_the_admin_supplied_date(ctx, days, status):
    when = (datetime.now(timezone.utc).date() + timedelta(days=days))
    ctx.environ = {'GRAPH_CLIENT_SECRET_EXPIRES': when.isoformat()}
    assert C.check_graph_secret_expiry(ctx)['status'] == status


def test_client_secret_expiry_is_unknown_with_guidance_when_unset(ctx):
    r = C.check_graph_secret_expiry(ctx)
    assert r['status'] == UNKNOWN and 'GRAPH_CLIENT_SECRET_EXPIRES' in \
        r['detail']
    ctx.environ = {'GRAPH_CLIENT_SECRET_EXPIRES': 'next spring'}
    assert C.check_graph_secret_expiry(ctx)['status'] == UNKNOWN


def _sub(hours, sid='sub-1'):
    exp = datetime.now(timezone.utc) + timedelta(hours=hours)
    return {'id': sid, 'resource': "/users/x/mailFolders('Inbox')/messages",
            'expirationDateTime': exp.strftime('%Y-%m-%dT%H:%M:%S.0000000Z')}


@pytest.mark.parametrize('hours,status', [(48, OK), (10, WARN), (-1, FAIL)])
def test_subscription_expiry_from_graph(ctx, monkeypatch, hours, status):
    _graph(monkeypatch, subs=[_sub(hours)])
    with open(ctx.sub_id_file, 'w') as fh:
        fh.write('sub-1\n')
    r = C.check_subscription(_net_ctx(ctx))
    assert r['status'] == status and r['value']['source'] == 'graph'


def test_no_subscription_in_graph_fails(ctx, monkeypatch):
    _graph(monkeypatch, subs=[])
    assert C.check_subscription(_net_ctx(ctx))['status'] == FAIL


def test_an_id_file_graph_does_not_know_warns(ctx, monkeypatch):
    _graph(monkeypatch, subs=[_sub(60, 'other')])
    with open(ctx.sub_id_file, 'w') as fh:
        fh.write('sub-1\n')
    r = C.check_subscription(_net_ctx(ctx))
    assert r['status'] == WARN and 'does not list' in r['detail']


def test_subscription_falls_back_to_the_id_file_age(ctx, monkeypatch):
    _graph(monkeypatch, subs_status=403)
    with open(ctx.sub_id_file, 'w') as fh:
        fh.write('sub-1\n')
    r = C.check_subscription(_net_ctx(ctx))
    assert r['status'] == OK and r['value']['source'] == 'id file'
    assert 'HTTP 403' in r['detail']
    t = time.time() - 60 * 3600                  # 10.5 h left of 70.5
    os.utime(ctx.sub_id_file, (t, t))
    assert C.check_subscription(_net_ctx(ctx))['status'] == WARN
    t = time.time() - 80 * 3600
    os.utime(ctx.sub_id_file, (t, t))
    assert C.check_subscription(_net_ctx(ctx))['status'] == FAIL
    os.remove(ctx.sub_id_file)
    assert C.check_subscription(ctx)['status'] == WARN


# ── TLS ──────────────────────────────────────────────────────────────
def _cert_in(days):
    t = time.time() + days * 86400
    return {'notAfter': time.strftime('%b %d %H:%M:%S %Y GMT', time.gmtime(t))}


@pytest.mark.parametrize('days,status', [(60, OK), (15, WARN), (3, FAIL)])
def test_certificate_expiry(ctx, monkeypatch, days, status):
    seen = []
    monkeypatch.setattr(C, '_peer_cert', lambda host, port, timeout=None: (
        seen.append((host, port)) or _cert_in(days)))
    ctx.base_url, ctx.network = 'https://crm.example.test/CRM', True
    assert C.check_certificate(ctx)['status'] == status
    assert seen == [('crm.example.test', 443)]


def test_certificate_problems(ctx, monkeypatch):
    assert C.check_certificate(ctx)['status'] == UNKNOWN        # no URL
    ctx.base_url = 'http://crm.example.test'
    assert C.check_certificate(ctx)['status'] == WARN
    ctx.base_url = 'https://crm.example.test'
    assert C.check_certificate(ctx)['status'] == UNKNOWN        # no network
    ctx.network = True

    def invalid(*a, **kw):
        err = ssl.SSLCertVerificationError('verify failed')
        err.verify_message = 'certificate has expired'
        raise err
    monkeypatch.setattr(C, '_peer_cert', invalid)
    r = C.check_certificate(ctx)
    assert r['status'] == FAIL and 'expired' in r['detail']

    def unreachable(*a, **kw):
        raise socket_timeout()
    monkeypatch.setattr(C, '_peer_cert', unreachable)
    assert C.check_certificate(ctx)['status'] == UNKNOWN


def socket_timeout():
    import socket
    return socket.timeout('timed out')


# ── database and disk ────────────────────────────────────────────────
def test_database_integrity(ctx, monkeypatch):
    r = C.check_database(ctx)
    assert r['status'] == OK and r['value']['quick_check'] == 'ok'
    assert r['value']['journal_mode'] == 'delete'
    real = C._scalar
    monkeypatch.setattr(C, '_scalar', lambda conn, sql, params=(): (
        'row 7 missing from index' if sql == 'PRAGMA quick_check'
        else real(conn, sql, params)))
    assert C.check_database(ctx)['status'] == FAIL
    ctx.db_path = ctx.db_path + '.gone'
    assert C.check_database(ctx)['status'] == FAIL


def test_database_stats_for_the_web_never_run_quick_check(ctx, monkeypatch):
    real = C._scalar

    def spy(conn, sql, params=()):
        assert 'quick_check' not in sql
        return real(conn, sql, params)
    monkeypatch.setattr(C, '_scalar', spy)
    r = C.check_database_stats(ctx)
    assert r['status'] == OK and 'quick_check' not in r['value']


def test_fast_growth_warns(ctx, monkeypatch):
    b = _backup(ctx, age_h=24)
    os.truncate(b, 0)                          # a day ago the file was tiny
    t = time.time() - 24 * 3600
    os.utime(b, (t, t))
    monkeypatch.setattr(C, 'GROWTH_WARN_MB_DAY', -1)
    r = C.check_database(ctx)
    assert r['status'] == WARN and 'growing' in r['detail']


Usage = namedtuple('Usage', 'total used free')


@pytest.mark.parametrize('free_gb,used,status', [
    (50, 50, OK), (3, 60, WARN), (20, 90, WARN), (0.5, 99, FAIL)])
def test_disk(ctx, monkeypatch, free_gb, used, status):
    gb = 1_073_741_824
    monkeypatch.setattr(C.shutil, 'disk_usage', lambda p: Usage(
        100 * gb, used * gb, int(free_gb * gb)))
    r = C.check_disk(ctx)
    assert r['status'] == status
    assert 'database_free_gb' in r['value']


# ── queues ───────────────────────────────────────────────────────────
def _events(ctx, rows):
    db = sqlite3.connect(ctx.db_path)
    db.executemany('INSERT INTO email_events (received_at, status) '
                   'VALUES (?, ?)', rows)
    db.commit()
    db.close()


def test_a_normal_email_day_is_ok(ctx):
    _events(ctx, [(_ts(hours=h), 'lead_created') for h in range(1, 6)]
            + [(_ts(hours=2), 'skipped')])
    r = C.check_email_queue(ctx)
    assert r['status'] == OK, r['detail']
    assert r['value']['events_24h'] == 6


def test_email_queue_problems(ctx):
    assert C.check_email_queue(ctx)['status'] == WARN      # silence
    _events(ctx, [(_ts(hours=1), 'lead_created'), (_ts(hours=2), 'failed')])
    r = C.check_email_queue(ctx)
    assert r['status'] == WARN and r['value']['failed_24h'] == 1
    _events(ctx, [(_ts(minutes=45), 'received')])
    assert C.check_email_queue(ctx)['value']['oldest_pending_minutes'] >= 44
    _events(ctx, [(_ts(hours=7), 'processing')])
    r = C.check_email_queue(ctx)
    assert r['status'] == FAIL and r['value']['pending'] == 2


def test_an_unprocessed_event_older_than_a_week_is_not_counted(ctx):
    _events(ctx, [(_ts(hours=1), 'lead_created'), (_ts(days=9), 'received')])
    r = C.check_email_queue(ctx)
    assert r['status'] == OK and r['value']['pending'] == 0


def test_review_queue(ctx):
    db = sqlite3.connect(ctx.db_path)
    db.executemany('INSERT INTO email_classifications (review_state, '
                   'created_at) VALUES (?, ?)',
                   [('pending', _ts(hours=3)), ('accepted', _ts(days=9))])
    db.commit()
    r = C.check_review_queue(ctx)
    assert r['status'] == OK and r['value']['pending'] == 1
    db.execute("INSERT INTO email_classifications (review_state, created_at)"
               " VALUES ('pending', ?)", (_ts(days=3),))
    db.commit()
    db.close()
    assert C.check_review_queue(ctx)['status'] == WARN
    _exec(ctx.db_path, 'DROP TABLE email_classifications')
    assert C.check_review_queue(ctx)['status'] == UNKNOWN


# ── Copilot index ────────────────────────────────────────────────────
def _chunks(ctx, n, age_h):
    db = sqlite3.connect(ctx.db_path)
    db.executemany('INSERT INTO copilot_chunk (lead_id, text, indexed_at) '
                   'VALUES (?, ?, ?)',
                   [(i + 1, 't', _ts(hours=age_h)) for i in range(n)])
    db.commit()
    db.close()


def test_a_fresh_full_index_is_ok(ctx):
    _chunks(ctx, 20, 2)
    r = C.check_copilot_index(ctx)
    assert r['status'] == OK, r['detail']
    assert r['value']['coverage_pct'] == 100.0


def test_index_coverage_and_staleness(ctx):
    r = C.check_copilot_index(ctx)
    assert r['status'] == WARN and 'never built' in r['detail']
    _chunks(ctx, 5, 2)
    assert 'only 5 of 20' in C.check_copilot_index(ctx)['detail']
    _exec(ctx.db_path, 'DELETE FROM copilot_chunk')
    _chunks(ctx, 20, 40)
    assert C.check_copilot_index(ctx)['status'] == OK      # nothing changed
    _exec(ctx.db_path, 'INSERT INTO lead_notes (lead_id, note_text, '
          'created_at) VALUES (1, ?, ?)', ('called', _ts(hours=1)))
    r = C.check_copilot_index(ctx)
    assert r['status'] == WARN and r['value']['changed_since']['notes'] == 1
    _exec(ctx.db_path, 'DROP TABLE copilot_chunk')
    assert 'missing' in C.check_copilot_index(ctx)['detail']


# ── workers ──────────────────────────────────────────────────────────
_PS = """\
    1     0   9000 /sbin/init
  900     1  60000 /var/www/procam-crm/.venv/bin/python /var/www/procam-crm/.venv/bin/gunicorn app:app --workers 2 --bind 127.0.0.1:8002
  901   900 250000 /var/www/procam-crm/.venv/bin/python /var/www/procam-crm/.venv/bin/gunicorn app:app --workers 2 --bind 127.0.0.1:8002
  902   900 260000 /var/www/procam-crm/.venv/bin/python /var/www/procam-crm/.venv/bin/gunicorn app:app --workers 2 --bind 127.0.0.1:8002
  950     1   3000 nginx: master process
"""
_EXEC = ('ExecStart={ path=/var/www/procam-crm/.venv/bin/gunicorn ; argv[]='
         '/var/www/procam-crm/.venv/bin/gunicorn app:app --workers 3 --bind '
         '127.0.0.1:8002 ; }\n')


def _fake_run(table):
    def run(cmd, timeout=None):
        for prefix, out in table.items():
            if ' '.join(cmd).startswith(prefix):
                return out
        return None
    return run


def test_workers_counted_against_systemd(ctx, monkeypatch):
    monkeypatch.setattr(C, '_run', _fake_run({
        'ps ': (0, _PS, ''), 'systemctl show': (0, _EXEC, '')}))
    r = C.check_workers(ctx)
    assert r['value']['workers'] == 2 and r['value']['expected'] == 3
    assert r['status'] == WARN and 'fewer workers' in r['detail']
    assert r['value']['worker_rss_mb'] == [244.1, 253.9]


def test_workers_ok_from_master_args_when_systemd_is_silent(ctx, monkeypatch):
    monkeypatch.setattr(C, '_run', _fake_run({'ps ': (0, _PS, '')}))
    r = C.check_workers(ctx)
    assert r['status'] == OK and r['value']['expected'] == 2


def test_workers_memory_and_absence(ctx, monkeypatch):
    monkeypatch.setattr(C, '_run', _fake_run({'ps ': (0, _PS, '')}))
    monkeypatch.setattr(C, 'WORKER_RSS_WARN_MB', 245)
    assert 'above 245 MB' in C.check_workers(ctx)['detail']
    only_master = '\n'.join(_PS.splitlines()[:2])
    monkeypatch.setattr(C, '_run', _fake_run({'ps ': (0, only_master, '')}))
    assert C.check_workers(ctx)['status'] == FAIL
    monkeypatch.setattr(C, '_run', _fake_run({'ps ': (0, '1 0 10 init', '')}))
    assert C.check_workers(ctx)['status'] == FAIL


def test_workers_read_proc_when_ps_is_missing(ctx, monkeypatch, tmp_path):
    proc = {
        '900': ('900 (gunicorn) S 1 900', b'gunicorn\0app:app\0', 60000),
        '901': ('901 (gunicorn: w) S 900 900', b'gunicorn\0app:app\0', 2048),
    }
    real_open, real_isdir, real_listdir = open, os.path.isdir, os.listdir

    def fake_open(path, mode='r', *a, **kw):
        m = str(path).split('/')
        if str(path).startswith('/proc/') and m[2] in proc:
            stat, cmdline, rss = proc[m[2]]
            import io
            return {'stat': io.StringIO(stat + ' 0 0\n'),
                    'cmdline': io.BytesIO(cmdline),
                    'status': io.StringIO(f'VmRSS:\t{rss} kB\n')}[m[3]]
        return real_open(path, mode, *a, **kw)
    monkeypatch.setattr(C, '_run', lambda cmd, timeout=None: None)
    monkeypatch.setattr('builtins.open', fake_open)
    monkeypatch.setattr(os.path, 'isdir', lambda p: p == '/proc' or
                        real_isdir(p))
    monkeypatch.setattr(os, 'listdir', lambda p: list(proc) if p == '/proc'
                        else real_listdir(p))
    r = C.check_workers(ctx)
    assert r['value']['master_pid'] == 900 and r['value']['workers'] == 1
    assert r['value']['worker_rss_mb'] == [2.0]


def test_workers_unknown_without_ps_or_proc(ctx, monkeypatch):
    monkeypatch.setattr(C, '_run', lambda cmd, timeout=None: None)
    monkeypatch.setattr(os.path, 'isdir', lambda p: False)
    assert C.check_workers(ctx)['status'] == UNKNOWN


# ── timers ───────────────────────────────────────────────────────────
def test_systemd_times_parse():
    assert C.parse_systemd_time('n/a') is None
    assert C.parse_systemd_time('') is None
    assert C.parse_systemd_time('@1757770000') == 1757770000
    assert C.parse_systemd_time('Sat 2026-09-12 01:30:04 UTC') == \
        datetime(2026, 9, 12, 1, 30, 4, tzinfo=timezone.utc).timestamp()


def _utc_text(hours_ago):
    return time.strftime('%a %Y-%m-%d %H:%M:%S UTC',
                         time.gmtime(time.time() - hours_ago * 3600))


def _systemd(timers, results=None, ages=None):
    listing = '\n'.join(
        f'Sat 2026-09-13 12:00:00 UTC 5min left Sat 2026-09-13 11:45:00 UTC '
        f'9min ago {t} {t[:-6]}.service' for t in timers)
    results, ages = results or {}, ages or {}

    def run(cmd, timeout=None):
        if cmd[:2] == ['systemctl', 'list-timers']:
            return 0, listing + '\n', ''
        if cmd[:2] == ['systemctl', 'show']:
            unit = cmd[2]
            if unit.endswith('.timer'):
                return 0, (f'LastTriggerUSec={_utc_text(ages.get(unit, 0.1))}'
                           f'\nNextElapseUSecRealtime={_utc_text(-1)}\n'
                           f'Unit={unit[:-6]}.service\n'), ''
            return 0, (f'Result={results.get(unit, "success")}\n'
                       f'ExecMainStatus=0\nActiveState=inactive\n'), ''
        return None
    return run


def test_all_timers_present_and_healthy(ctx, monkeypatch):
    monkeypatch.setattr(C, '_run', _systemd(list(C.EXPECTED_TIMERS)))
    r = C.check_timers(ctx)
    assert r['status'] == OK, r['detail']
    t = r['value']['timers']['procam-crm-backup.timer']
    assert t['last_result'] == 'success' and t['hours_since_run'] < 1


def test_a_failed_job_fails_and_a_missing_timer_warns(ctx, monkeypatch):
    names = list(C.EXPECTED_TIMERS)
    monkeypatch.setattr(C, '_run', _systemd(
        names, results={'procam-crm-backup.service': 'exit-code'}))
    r = C.check_timers(ctx)
    assert r['status'] == FAIL and 'backup last run exit-code' in r['detail']
    monkeypatch.setattr(C, '_run', _systemd(names[:-1]))
    r = C.check_timers(ctx)
    assert r['status'] == WARN and 'ops-status' in r['detail']
    monkeypatch.setattr(C, '_run', _systemd(
        names, ages={'procam-crm-sla-sweep.timer': 3}))
    assert 'sla-sweep overdue' in C.check_timers(ctx)['detail']


def test_timers_unknown_without_systemd(ctx, monkeypatch):
    monkeypatch.setattr(C, '_run', lambda cmd, timeout=None: None)
    assert C.check_timers(ctx)['status'] == UNKNOWN
    monkeypatch.setattr(C, '_run', lambda cmd, timeout=None: (
        1, '', 'System has not been booted with systemd'))
    assert C.check_timers(ctx)['status'] == UNKNOWN


# ── deployment and rollback ──────────────────────────────────────────
def _git_run(commit_time, dirty='', moved_at=None, started=None, calls=None):
    moved_at = moved_at or commit_time + 600

    def run(cmd, timeout=None):
        if calls is not None:
            calls.append(cmd)
        if cmd[0] == 'git':
            assert cmd[1] == '--no-optional-locks'
            args = cmd[4:]
            if args == ['rev-parse', 'HEAD']:
                return 0, 'abcdef1234567890\n', ''
            if args[:2] == ['rev-parse', '--abbrev-ref']:
                return 0, 'main\n', ''
            if args[:2] == ['log', '-1']:
                return 0, f'{commit_time}\n', ''
            if args[0] == 'status':
                return 0, dirty, ''
            if args[:2] == ['log', '-g']:
                return 0, (f'HEAD@{{{moved_at}}}\tpull --ff-only origin '
                           f'main: Fast-forward\n'), ''
        if cmd[:2] == ['systemctl', 'show'] and started:
            return 0, f'ActiveEnterTimestamp=@{started}\n', ''
        return None
    return run


def test_a_clean_restarted_deploy_is_ok(ctx, monkeypatch):
    now = int(time.time())
    calls = []
    monkeypatch.setattr(C, '_run', _git_run(now - 3600, started=now - 60,
                                            calls=calls))
    r = C.check_deployment(ctx)
    assert r['status'] == OK, r['detail']
    assert r['value']['commit'] == 'abcdef123456'
    assert r['value']['last_deploy_action'].startswith('pull')
    for cmd in calls:
        assert cmd[0] != 'git' or cmd[1] == '--no-optional-locks'
        assert not ({'checkout', 'reset', 'pull', 'fetch', 'commit', 'stash'}
                    & set(cmd))


def test_server_edits_and_a_pending_restart_warn(ctx, monkeypatch):
    now = int(time.time())
    monkeypatch.setattr(C, '_run', _git_run(
        now - 3600, dirty=' M app.py\n M static/css/crm.css\n',
        started=now - 60))
    r = C.check_deployment(ctx)
    assert r['status'] == WARN and 'app.py, static/css/crm.css' in r['detail']
    monkeypatch.setattr(C, '_run', _git_run(
        now - 3600, moved_at=now - 100, started=now - 7200))
    ctx._git = {}
    r = C.check_deployment(ctx)
    assert r['status'] == WARN and 'restart pending' in r['detail']


def test_rollback_readiness(ctx, monkeypatch):
    commit = int(time.time()) - 3600
    monkeypatch.setattr(C, '_run', _git_run(commit))
    assert C.check_rollback(ctx)['status'] == WARN            # none at all
    _backup(ctx, 'procam_crm-old-pre-deploy.db', age_h=5)
    r = C.check_rollback(ctx)
    assert r['status'] == WARN and 'older than the running' in r['detail']
    _backup(ctx, 'procam_crm-new-pre-deploy.db', age_h=0.5)
    assert C.check_rollback(ctx)['status'] == OK
    with open(os.path.join(ctx.backups_dir,
                           'procam_crm-newest-pre-deploy.db'), 'wb') as f:
        f.write(b'garbage' * 1000)
    assert C.check_rollback(ctx)['status'] == FAIL


# ── preflight reuse ──────────────────────────────────────────────────
def test_config_reuses_preflight_and_hides_values(ctx, monkeypatch):
    monkeypatch.setenv('SECRET_KEY', 'procam-crm-secret-change-me-2025')
    monkeypatch.setenv('EMAIL_WEBHOOK_SECRET', 'w' * 40)
    r = C.check_config(ctx)
    assert r['status'] == FAIL and 'SECRET_KEY' in r['detail']
    assert 'change-me' not in json.dumps(r) and 'w' * 40 not in json.dumps(r)


def test_schema_drift_reuses_preflight(ctx, monkeypatch):
    assert C.check_schema(ctx)['status'] == UNKNOWN        # --no-schema
    ctx.schema = True
    pf = C.preflight()
    monkeypatch.setattr(pf, 'expected_schema', lambda: {
        'leads': {'columns': ['id', 'company', 'vertical_reason'],
                  'indexes': []}})
    r = C.check_schema(ctx)
    assert r['status'] == FAIL and 'leads.vertical_reason' in r['detail']
    monkeypatch.setattr(pf, 'expected_schema', lambda: {
        'leads': {'columns': ['id', 'company'], 'indexes': []}})
    assert C.check_schema(ctx)['status'] == OK


def test_loading_preflight_leaves_sys_path_alone(monkeypatch):
    """scripts/ holds email_ingest.py; left on sys.path inside the web
    process it would shadow the email_ingest package."""
    monkeypatch.delitem(sys.modules, 'production_preflight', raising=False)
    monkeypatch.setattr(C, '_PREFLIGHT', None)
    before = list(sys.path)
    assert C.preflight().newest_backup
    assert sys.path == before


# ── report, status file ──────────────────────────────────────────────
def test_exit_code_and_overall():
    r = [C.result('a', 'a', OK), C.result('b', 'b', WARN),
         C.result('c', 'c', UNKNOWN)]
    assert C.exit_code(r) == 0 and C.worst(x['status'] for x in r) == WARN
    r.append(C.result('d', 'd', FAIL))
    assert C.exit_code(r) == 1


def test_status_file_is_written_atomically_and_read_back(ctx, tmp_path):
    path = str(tmp_path / 'instance' / 'ops_status.json')
    report = C.build_report([C.result('disk', 'Disk', OK, 'fine')], ctx)
    C.write_status(report, path)
    assert os.listdir(tmp_path / 'instance') == ['ops_status.json']
    assert oct(os.stat(path).st_mode & 0o777) == '0o640'
    got = C.read_status(path)
    assert got['report']['checks'][0]['key'] == 'disk'
    assert got['stale'] is False and got['error'] is None
    t = time.time() - 3600
    os.utime(path, (t, t))
    assert C.read_status(path)['stale'] is True


def test_a_failed_write_leaves_the_old_status_file(ctx, tmp_path,
                                                   monkeypatch):
    path = str(tmp_path / 'ops_status.json')
    C.write_status({'checks': [], 'overall': OK}, path)

    def broken(*a, **kw):
        raise TypeError('not serialisable')
    monkeypatch.setattr(C.json, 'dump', broken)
    with pytest.raises(TypeError):
        C.write_status({'checks': []}, path)
    with open(path) as fh:
        assert json.load(fh)['overall'] == OK
    assert not [f for f in os.listdir(tmp_path) if f.endswith('.tmp')]


def test_read_status_never_raises(tmp_path):
    assert 'no status file' in C.read_status(str(tmp_path / 'x.json'))['error']
    bad = tmp_path / 'bad.json'
    bad.write_text('{half')
    assert C.read_status(str(bad))['report'] is None
    bad.write_text('[1, 2]')
    assert 'not an ops_status report' in C.read_status(str(bad))['error']


def test_live_checks_are_local_and_cheap(ctx, monkeypatch):
    def forbidden(*a, **kw):
        raise AssertionError('the web process must not call this')
    monkeypatch.setattr(C, '_run', forbidden)
    monkeypatch.setattr(C, '_http', forbidden)
    monkeypatch.setattr(C, '_peer_cert', forbidden)
    live = C.live_checks(ctx)
    assert [r['key'] for r in live] == ['database_stats', 'email_queue',
                                        'review_queue', 'copilot_index']
    assert all('crashed' not in r['detail'] for r in live)
