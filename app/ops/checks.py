"""
Operational checks for the production CRM. Read-only.

One library, two callers:

  scripts/ops_status.py   the full set, from a systemd timer or by hand;
                          writes the JSON the admin page reads
  app/ops/routes.py       the cheap subset the web process can afford
                          (row counts and ages, never quick_check or a
                          network call)

Every check is a plain function of a Context (paths, URLs, flags) and
returns one dict:

    {key, label, status: OK|WARN|FAIL|UNKNOWN, detail, measured_at, value}

Rules this module keeps, because it runs against production:

  * The live database and the backups are opened with SQLite mode=ro —
    the driver refuses a write, whatever the code does.
  * Nothing here changes system state. git runs with --no-optional-locks
    (plain `git status` rewrites the index), systemctl only `show`s and
    `list`s.
  * Network calls are limited to a Graph token, one Graph GET
    /subscriptions and a TLS handshake with the public CRM host, each
    with a short timeout, and all of them are skipped by --no-network.
  * A missing tool, permission or dependency is UNKNOWN, not a crash and
    not a FAIL: a monitor that cries wolf when it cannot see is ignored
    on the day it is right.
  * No secret value is ever put in a result. Details are scrubbed of
    every secret-looking environment value as a last line of defence.

It deliberately imports nothing from the application: importing app.py
boots the app and runs its autoheal against the real database file.
Production preflight's checks are reused by loading that script by path.
"""
import importlib.util
import json
import os
import re
import shutil
import socket
import sqlite3
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

OK, WARN, FAIL, UNKNOWN = 'OK', 'WARN', 'FAIL', 'UNKNOWN'
#: Worst first, for the headline of a report.
SEVERITY = {FAIL: 3, WARN: 2, UNKNOWN: 1, OK: 0}

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(_HERE))
SCRIPTS = os.path.join(ROOT, 'scripts')

DEFAULT_HEALTH_URL = 'http://127.0.0.1:8002/healthz'
DEFAULT_SERVICE = 'procam-crm'
DEFAULT_BIND = '127.0.0.1:8002'
NET_TIMEOUT = 8          # seconds, every network call
CMD_TIMEOUT = 10         # seconds, every subprocess

# ── thresholds (documented in docs/operations/MONITORING_GUIDE.md) ───
HEALTH_SLOW_MS = 2000
BACKUP_WARN_H, BACKUP_FAIL_H = 26, 72
SUB_WARN_H = 24
#: Graph's maximum lifetime for a message subscription (subscription.py).
SUB_LIFETIME_MIN = 4230
CERT_WARN_D, CERT_FAIL_D = 21, 7
SECRET_WARN_D, SECRET_FAIL_D = 60, 14
DISK_WARN_GB, DISK_FAIL_GB = 5, 1
DISK_WARN_PCT, DISK_FAIL_PCT = 85, 95
GROWTH_WARN_MB_DAY = 250
WAL_WARN_MB = 256
QUEUE_STUCK_WARN_MIN, QUEUE_STUCK_FAIL_MIN = 30, 360
REVIEW_WARN_COUNT, REVIEW_WARN_H = 50, 48
INDEX_STALE_H = 26
INDEX_COVERAGE = 0.8
WORKER_RSS_WARN_MB = 1024
STATUS_STALE_MIN = 30

#: The tables whose counts should move slowly. email_events and
#: copilot_chunk are left out: they swing by thousands a day, and a
#: rebuild replaces the index wholesale.
CORE_TABLES = ('employees', 'leads', 'companies', 'contacts',
               'opportunities', 'lead_notes', 'lead_emails')

#: Timers from docs/operations/deploy/, with the longest normal gap
#: between runs in hours. A gap beyond it means the job has stopped.
EXPECTED_TIMERS = {
    'procam-crm-backup.timer': 26,
    'procam-crm-copilot-index.timer': 26,
    'procam-crm-graph-subscription.timer': 13,
    'procam-crm-sla-sweep.timer': 0.5,
    'procam-crm-ops-status.timer': 0.5,
}

_SECRET_NAME = re.compile(r'SECRET|PASSWORD|PASSWD|TOKEN|API_KEY|_KEY$|'
                          r'CREDENTIAL', re.I)


# ── plumbing ─────────────────────────────────────────────────────────
def _utcnow():
    return datetime.now(timezone.utc)


def _iso(ts=None):
    dt = (datetime.fromtimestamp(ts, timezone.utc) if ts is not None
          else _utcnow())
    return dt.replace(microsecond=0).isoformat()


def result(key, label, status, detail='', value=None):
    return {'key': key, 'label': label, 'status': status,
            'detail': detail, 'measured_at': _iso(), 'value': value}


def worst(statuses):
    statuses = list(statuses)
    return max(statuses, key=lambda s: SEVERITY.get(s, 0)) if statuses \
        else UNKNOWN


def _mb(n):
    return round(n / 1_048_576, 1)


def _hours(seconds):
    return round(seconds / 3600, 1)


def _env(name, environ=None):
    return ((environ if environ is not None else os.environ).get(name)
            or '').strip()


def _run(cmd, timeout=CMD_TIMEOUT):
    """(returncode, stdout, stderr), or None when the program is not
    installed or did not answer in time. The one door to subprocess, so
    tests replace it and nothing here can hang the timer."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.returncode, p.stdout, p.stderr


def _http(url, *, data=None, headers=None, timeout=NET_TIMEOUT):
    """(status, body bytes). Raises OSError when nothing answered.
    urllib, not requests: the CLI should not need the app's packages to
    tell you the app is down."""
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(65536)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(65536)


def _secret_values(environ=None):
    env = environ if environ is not None else os.environ
    # GRAPH_CLIENT_SECRET_EXPIRES names a secret but holds a date the
    # expiry check has to be able to print.
    return sorted({v for k, v in env.items()
                   if _SECRET_NAME.search(k) and not k.endswith('_EXPIRES')
                   and v and len(v) >= 6},
                  key=len, reverse=True)


def scrub(obj, secrets=None):
    """Replace any secret value found anywhere in ``obj``. Details are
    written by hand and should never hold one; this is for the day an
    exception message quotes a connection string."""
    secrets = _secret_values() if secrets is None else secrets
    if isinstance(obj, str):
        for s in secrets:
            if s in obj:
                obj = obj.replace(s, '[redacted]')
        return obj
    if isinstance(obj, dict):
        return {k: scrub(v, secrets) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [scrub(v, secrets) for v in obj]
    return obj


def sqlite_path(url):
    """The file behind a SQLite URL, or '' for anything else."""
    url = (url or '').strip()
    if not url.startswith('sqlite:///'):
        return ''
    path = url[len('sqlite:///'):]
    if path.startswith('file:'):
        path = path[len('file:'):].split('?', 1)[0]
    return path


def open_ro(path, timeout=5):
    """A sqlite3 connection that cannot write — enforced by SQLite."""
    if not path or not os.path.exists(path):
        raise FileNotFoundError(path or 'no database path')
    return sqlite3.connect(
        f'file:{urllib.parse.quote(os.path.abspath(path))}?mode=ro',
        uri=True, timeout=timeout)


def _scalar(conn, sql, params=()):
    try:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def _tables(conn):
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _parse_dt(value):
    """A timestamp as SQLAlchemy writes it to SQLite (naive UTC)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '')[:26])
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc)


def _sql_time(dt):
    # The format SQLAlchemy stores, so a string comparison in SQL orders
    # the same way the datetimes do.
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


_PREFLIGHT = None


def preflight():
    """scripts/production_preflight.py, loaded by path.

    Its scripts/ directory is on sys.path only while it loads: that
    folder holds email_ingest.py, which would otherwise shadow the
    email_ingest package inside the web process."""
    global _PREFLIGHT
    if _PREFLIGHT is not None:
        return _PREFLIGHT
    mod = sys.modules.get('production_preflight')
    if mod is None:
        saved = list(sys.path)
        try:
            spec = importlib.util.spec_from_file_location(
                'production_preflight',
                os.path.join(SCRIPTS, 'production_preflight.py'))
            mod = importlib.util.module_from_spec(spec)
            sys.path.insert(0, SCRIPTS)
            sys.modules['production_preflight'] = mod
            spec.loader.exec_module(mod)
        except BaseException:
            sys.modules.pop('production_preflight', None)
            raise
        finally:
            sys.path[:] = saved
    _PREFLIGHT = mod
    return mod


class Context:
    """Where things are and what a run may do. Built from the
    environment by default; every field can be overridden for a test or
    a one-off run against a copy."""

    def __init__(self, *, root=ROOT, db_path=None, backups_dir=None,
                 env_path=None, health_url=None, base_url=None,
                 service=None, bind=None, sub_id_file=None, network=True,
                 schema=True, environ=None):
        env = environ if environ is not None else os.environ
        self.environ = env
        self.root = root
        if db_path is None:
            url = _env('DATABASE_URL', env) or (
                'sqlite:///' + os.path.join(root, 'procam_crm.db'))
            db_path = sqlite_path(url)
        self.db_path = db_path
        self.backups_dir = backups_dir or os.path.join(root, 'backups')
        self.env_path = env_path or os.path.join(root, '.env')
        self.health_url = (health_url or _env('OPS_HEALTH_URL', env)
                           or DEFAULT_HEALTH_URL)
        self.base_url = (base_url if base_url is not None
                         else _env('CRM_BASE_URL', env))
        self.service = service or _env('OPS_SERVICE_NAME', env) \
            or DEFAULT_SERVICE
        self.bind = bind or DEFAULT_BIND
        self.sub_id_file = sub_id_file or _env(
            'LEADS_SUBSCRIPTION_ID_FILE', env) or os.path.join(
            root, '.leads_subscription_id')
        self.network = network
        self.schema = schema
        # Held for one run, never serialised: the Graph token is shared
        # by the token and subscription checks so a run asks once.
        self._graph = None
        self._git = {}


# ── production health ────────────────────────────────────────────────
def check_health(ctx):
    key, label = 'health', 'Production health (/healthz)'
    t0 = time.monotonic()
    try:
        status, body = _http(ctx.health_url, timeout=5)
    except (OSError, ValueError) as exc:
        return result(key, label, FAIL,
                      f'{ctx.health_url} did not answer: '
                      f'{type(exc).__name__}: {str(exc)[:120]} — is '
                      f'{ctx.service} running?')
    ms = int((time.monotonic() - t0) * 1000)
    try:
        ok = bool(json.loads(body or b'{}').get('ok'))
    except (ValueError, AttributeError):
        ok = False
    value = {'http_status': status, 'latency_ms': ms}
    if status != 200 or not ok:
        return result(key, label, FAIL,
                      f'HTTP {status} in {ms} ms — '
                      + ('the app is up but its database query failed'
                         if status == 503 else 'not the healthy answer'),
                      value)
    if ms > HEALTH_SLOW_MS:
        return result(key, label, WARN, f'healthy but slow: {ms} ms '
                      f'(workers busy or the VM is loaded)', value)
    return result(key, label, OK, f'healthy in {ms} ms', value)


# ── backups ──────────────────────────────────────────────────────────
def check_backups(ctx):
    key, label = 'backups', 'Newest backup'
    pf = preflight()
    newest = pf.newest_backup(ctx.backups_dir)
    if not newest:
        return result(key, label, FAIL,
                      f'no database backup in {ctx.backups_dir}')
    age_h = (time.time() - os.path.getmtime(newest)) / 3600
    size = os.path.getsize(newest)
    live = os.path.getsize(ctx.db_path) if ctx.db_path and \
        os.path.exists(ctx.db_path) else 0
    value = {'age_hours': round(age_h, 1), 'size_mb': _mb(size),
             'count': len(pf.backup_files(ctx.backups_dir))}
    name = os.path.basename(newest)
    if size == 0:
        return result(key, label, FAIL, f'{name} is empty', value)
    if age_h > BACKUP_FAIL_H:
        return result(key, label, FAIL, f'{name} is {age_h:.0f} h old — '
                      f'the nightly backup has stopped', value)
    notes = []
    if age_h > BACKUP_WARN_H:
        notes.append(f'{age_h:.0f} h old — last night\'s backup is missing')
    if live and size < 0.5 * live:
        notes.append('under half the live database size')
    return result(key, label, WARN if notes else OK,
                  f'{name}, {age_h:.0f} h old, {_mb(size)} MB'
                  + (' — ' + '; '.join(notes) if notes else ''), value)


def _table_counts(conn, tables):
    present = _tables(conn)
    return {t: (_scalar(conn, f'SELECT COUNT(*) FROM "{t}"')
                if t in present else None) for t in tables}


def check_restore(ctx):
    """Would the newest backup restore? Opened read-only, checked, and
    its row counts held against the live database."""
    key, label = 'restore', 'Restore verification'
    newest = preflight().newest_backup(ctx.backups_dir)
    if not newest:
        return result(key, label, FAIL, 'no backup to verify')
    name = os.path.basename(newest)
    try:
        b = open_ro(newest)
    except (OSError, sqlite3.Error) as exc:
        return result(key, label, FAIL, f'{name} cannot be opened: {exc}')
    try:
        qc = _scalar(b, 'PRAGMA quick_check')
        if qc is None:
            return result(key, label, FAIL, f'{name} is not a readable '
                          f'SQLite database')
        backup_counts = _table_counts(b, CORE_TABLES)
    finally:
        b.close()
    if qc != 'ok':
        return result(key, label, FAIL, f'{name}: quick_check says '
                      f'{str(qc)[:200]} — do not rely on it')
    try:
        live = open_ro(ctx.db_path)
    except (OSError, sqlite3.Error) as exc:
        return result(key, label, UNKNOWN, f'{name} passes quick_check; '
                      f'live database not readable to compare: {exc}',
                      {'backup': backup_counts})
    try:
        live_counts = _table_counts(live, CORE_TABLES)
    finally:
        live.close()
    missing, fewer, behind = [], [], []
    for t in CORE_TABLES:
        lc, bc = live_counts[t], backup_counts[t]
        if lc is None:
            continue
        if bc is None:
            missing.append(t)
            continue
        # Rows removed since the backup (live below it) are rare here —
        # records are archived, not deleted — so a small margin. Rows
        # added since are normal for a day-old backup, so a wide one.
        if lc < bc - max(5, 0.02 * bc):
            fewer.append(f'{t} {lc} < {bc}')
        elif lc > bc + max(200, 0.25 * bc):
            behind.append(f'{t} {lc} vs {bc}')
    value = {'backup': backup_counts, 'live': live_counts}
    if missing:
        return result(key, label, FAIL, f'{name} lacks tables the live '
                      f'database has: {", ".join(missing)}', value)
    notes = []
    if fewer:
        notes.append('live has fewer rows than the backup (deleted '
                     'since?): ' + ', '.join(fewer))
    if behind:
        notes.append('backup is far behind live: ' + ', '.join(behind))
    return result(key, label, WARN if notes else OK,
                  f'{name} passes quick_check; core table counts '
                  + ('; '.join(notes) if notes else 'match live'), value)


# ── Microsoft Graph ──────────────────────────────────────────────────
def _graph_creds(ctx):
    names = ('MS_TENANT_ID', 'MS_CLIENT_ID', 'MS_CLIENT_SECRET')
    vals = {n: _env(n, ctx.environ) for n in names}
    return vals, [n for n in names if not vals[n]]


def graph_token(ctx):
    """('ok', token) | ('missing', names) | ('skipped', why) |
    ('unreachable', why) | ('refused', why). Cached on the context."""
    if ctx._graph is not None:
        return ctx._graph
    vals, missing = _graph_creds(ctx)
    if missing:
        ctx._graph = ('missing', ', '.join(missing))
    elif not ctx.network:
        ctx._graph = ('skipped', 'network checks disabled (--no-network)')
    else:
        url = ('https://login.microsoftonline.com/'
               f'{urllib.parse.quote(vals["MS_TENANT_ID"])}/oauth2/v2.0/token')
        form = urllib.parse.urlencode({
            'client_id': vals['MS_CLIENT_ID'],
            'client_secret': vals['MS_CLIENT_SECRET'],
            'scope': 'https://graph.microsoft.com/.default',
            'grant_type': 'client_credentials'}).encode()
        try:
            status, body = _http(url, data=form, headers={
                'Content-Type': 'application/x-www-form-urlencoded'})
        except (OSError, ValueError) as exc:
            ctx._graph = ('unreachable', f'{type(exc).__name__}: '
                                         f'{str(exc)[:120]}')
        else:
            try:
                data = json.loads(body or b'{}')
            except ValueError:
                data = {}
            if status == 200 and data.get('access_token'):
                ctx._graph = ('ok', data['access_token'])
            else:
                # Only the error code and the AADSTS number: the
                # description carries trace ids and nothing an operator
                # acts on that the number does not already say.
                code = re.search(r'AADSTS\d+',
                                 str(data.get('error_description') or ''))
                ctx._graph = ('refused', f'HTTP {status} '
                              f'{data.get("error") or ""} '
                              f'{code.group(0) if code else ""}'.strip())
    return ctx._graph


_AADSTS_HINTS = {
    'AADSTS7000222': 'the client secret has EXPIRED — create a new one '
                     '(Graph Setup Guide §5) and update MS_CLIENT_SECRET',
    'AADSTS7000215': 'the client secret is wrong — check MS_CLIENT_SECRET',
    'AADSTS700016': 'the app registration was not found — check '
                    'MS_CLIENT_ID and MS_TENANT_ID',
    'AADSTS90002': 'the tenant was not found — check MS_TENANT_ID',
}


def check_graph_token(ctx):
    key, label = 'graph_token', 'Graph token'
    state, info = graph_token(ctx)
    if state == 'ok':
        return result(key, label, OK, 'a client-credentials token was '
                      'issued (not shown)')
    if state == 'missing':
        return result(key, label, UNKNOWN, f'not configured: {info} — '
                      f'email ingest cannot reach the mailbox')
    if state == 'skipped':
        return result(key, label, UNKNOWN, info)
    if state == 'unreachable':
        return result(key, label, UNKNOWN, f'login.microsoftonline.com '
                      f'did not answer: {info}')
    hint = next((h for code, h in _AADSTS_HINTS.items() if code in info),
                'read the AADSTS code in the Graph Setup Guide')
    return result(key, label, FAIL, f'token refused ({info}) — {hint}')


def check_graph_secret_expiry(ctx):
    key, label = 'graph_secret_expiry', 'Graph client secret expiry'
    raw = _env('GRAPH_CLIENT_SECRET_EXPIRES', ctx.environ)
    if not raw:
        # Reading passwordCredentials needs Application.Read.All, which
        # the app registration should not be given just to be monitored.
        return result(key, label, UNKNOWN, 'not readable without extra '
                      'Graph permission — copy the expiry date from Entra '
                      '(App registrations → Certificates & secrets) into '
                      'GRAPH_CLIENT_SECRET_EXPIRES=YYYY-MM-DD in .env')
    try:
        expires = date.fromisoformat(raw[:10])
    except ValueError:
        return result(key, label, UNKNOWN, 'GRAPH_CLIENT_SECRET_EXPIRES is '
                      'not a YYYY-MM-DD date')
    days = (expires - _utcnow().date()).days
    value = {'days_left': days}
    if days < SECRET_FAIL_D:
        return result(key, label, FAIL, f'expires {expires} ({days} days) '
                      + ('— EXPIRED, ingest has stopped' if days < 0
                         else '— rotate it now'), value)
    if days < SECRET_WARN_D:
        return result(key, label, WARN, f'expires {expires} ({days} days) '
                      f'— schedule the rotation', value)
    return result(key, label, OK, f'expires {expires} ({days} days)', value)


def _parse_graph_time(value):
    try:
        return datetime.strptime(str(value)[:19], '%Y-%m-%dT%H:%M:%S') \
            .replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _expiry_result(key, label, hours_left, what, value):
    value = dict(value, hours_left=round(hours_left, 1))
    if hours_left <= 0:
        return result(key, label, FAIL, f'{what} expired '
                      f'{abs(hours_left):.0f} h ago — real-time ingest has '
                      f'stopped; run the graph-subscription service', value)
    if hours_left < SUB_WARN_H:
        return result(key, label, WARN, f'{what} expires in '
                      f'{hours_left:.0f} h — the renewal timer should have '
                      f'extended it', value)
    return result(key, label, OK, f'{what} expires in {hours_left:.0f} h',
                  value)


def check_subscription(ctx):
    key, label = 'graph_subscription', 'Graph mailbox subscription'
    sub_id = ''
    if os.path.exists(ctx.sub_id_file):
        try:
            with open(ctx.sub_id_file) as fh:
                sub_id = fh.read().strip()
        except OSError:
            sub_id = ''
    why_not = ''
    state, info = graph_token(ctx)
    if state == 'ok':
        try:
            status, body = _http(
                'https://graph.microsoft.com/v1.0/subscriptions',
                headers={'Authorization': f'Bearer {info}',
                         'Accept': 'application/json'})
        except (OSError, ValueError) as exc:
            status, body = None, b''
            why_not = f'Graph did not answer ({type(exc).__name__})'
        if status == 200:
            try:
                subs = json.loads(body or b'{}').get('value') or []
            except ValueError:
                subs = []
            if not subs:
                return result(key, label, FAIL, 'Graph lists no active '
                              'subscription — nothing pushes new mail to '
                              'the CRM', {'subscriptions': 0})
            ours = [s for s in subs if sub_id and s.get('id') == sub_id]
            pick = ours[0] if ours else max(
                subs, key=lambda s: str(s.get('expirationDateTime') or ''))
            exp = _parse_graph_time(pick.get('expirationDateTime'))
            if exp is None:
                return result(key, label, UNKNOWN, 'Graph returned a '
                              'subscription without a readable expiry')
            left = (exp - _utcnow()).total_seconds() / 3600
            res = _expiry_result(key, label, left, 'subscription',
                                 {'subscriptions': len(subs),
                                  'source': 'graph'})
            if sub_id and not ours and res['status'] == OK:
                res['status'] = WARN
                res['detail'] += ('; the id file names a subscription '
                                  'Graph does not list, so the next '
                                  'renewal will recreate it')
            return res
        if status is not None:
            why_not = f'Graph answered HTTP {status}'
    else:
        why_not = f'no Graph token ({state}: {info})'

    # Fallback: the id file is rewritten on every create/renew, so its
    # age bounds the expiry. An estimate, and labelled as one.
    if not os.path.exists(ctx.sub_id_file):
        return result(key, label, WARN, 'no subscription id file — this '
                      'host has never created or renewed a subscription'
                      + (f' ({why_not})' if why_not else ''))
    age_h = (time.time() - os.path.getmtime(ctx.sub_id_file)) / 3600
    left = SUB_LIFETIME_MIN / 60 - age_h
    res = _expiry_result(key, label, left, 'subscription (estimated from '
                         'the id file age)', {'source': 'id file',
                                              'file_age_hours':
                                                  round(age_h, 1)})
    res['detail'] += f' — {why_not}'
    return res


# ── TLS certificate ──────────────────────────────────────────────────
def _peer_cert(host, port, timeout=NET_TIMEOUT):
    tls = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=timeout) as sock:
        with tls.wrap_socket(sock, server_hostname=host) as conn:
            return conn.getpeercert()


def check_certificate(ctx):
    key, label = 'tls_certificate', 'TLS certificate'
    if not ctx.base_url:
        return result(key, label, UNKNOWN, 'CRM_BASE_URL is not set — '
                      'nothing to check')
    parsed = urllib.parse.urlparse(ctx.base_url)
    if parsed.scheme != 'https' or not parsed.hostname:
        return result(key, label, WARN, f'CRM_BASE_URL is not an https URL '
                      f'({parsed.scheme or "no scheme"})')
    if not ctx.network:
        return result(key, label, UNKNOWN, 'network checks disabled '
                      '(--no-network)')
    host, port = parsed.hostname, parsed.port or 443
    try:
        cert = _peer_cert(host, port)
    except ssl.SSLCertVerificationError as exc:
        return result(key, label, FAIL, f'{host}: certificate rejected — '
                      f'{exc.verify_message or exc}')
    except (OSError, ssl.SSLError) as exc:
        return result(key, label, UNKNOWN, f'could not reach {host}:{port} '
                      f'from this host: {type(exc).__name__}')
    try:
        expires = ssl.cert_time_to_seconds(cert['notAfter'])
    except (KeyError, ValueError, TypeError):
        return result(key, label, UNKNOWN, f'{host}: no readable expiry')
    days = (expires - time.time()) / 86400
    value = {'days_left': round(days, 1), 'expires': _iso(expires)}
    when = _iso(expires)[:10]
    if days < CERT_FAIL_D:
        return result(key, label, FAIL, f'{host} certificate expires {when}'
                      f' ({days:.0f} days) — renew now (certbot renew)',
                      value)
    if days < CERT_WARN_D:
        return result(key, label, WARN, f'{host} certificate expires {when}'
                      f' ({days:.0f} days) — automatic renewal has not run',
                      value)
    return result(key, label, OK, f'{host} certificate expires {when} '
                  f'({days:.0f} days)', value)


# ── database ─────────────────────────────────────────────────────────
def _db_stats(ctx, conn):
    size = os.path.getsize(ctx.db_path)
    wal = ctx.db_path + '-wal'
    wal_size = os.path.getsize(wal) if os.path.exists(wal) else 0
    journal = _scalar(conn, 'PRAGMA journal_mode')
    value = {'size_mb': _mb(size), 'wal_mb': _mb(wal_size),
             'journal_mode': journal}
    newest = preflight().newest_backup(ctx.backups_dir)
    if newest:
        days = (time.time() - os.path.getmtime(newest)) / 86400
        if days >= 0.5:
            value['growth_mb_per_day'] = round(
                (size - os.path.getsize(newest)) / 1_048_576 / days, 1)
    return value


def _database(ctx, key, label, integrity):
    if not ctx.db_path:
        return result(key, label, UNKNOWN, 'DATABASE_URL is not SQLite')
    try:
        conn = open_ro(ctx.db_path)
    except FileNotFoundError:
        return result(key, label, FAIL, f'{ctx.db_path} not found')
    except (OSError, sqlite3.Error) as exc:
        return result(key, label, UNKNOWN,
                      f'cannot open {ctx.db_path} read-only: {exc}')
    try:
        value = _db_stats(ctx, conn)
        qc = _scalar(conn, 'PRAGMA quick_check') if integrity else None
    finally:
        conn.close()
    parts = [f'{value["size_mb"]} MB', f'journal {value["journal_mode"]}']
    notes = []
    status = OK
    if integrity:
        value['quick_check'] = qc
        if qc != 'ok':
            return result(key, label, FAIL, f'quick_check: {str(qc)[:200]}'
                          f' — stop writes and read the Disaster Recovery '
                          f'Guide', value)
        parts.insert(0, 'quick_check ok')
    growth = value.get('growth_mb_per_day')
    if growth is not None:
        parts.append(f'{growth:+} MB/day since the newest backup')
        if growth > GROWTH_WARN_MB_DAY:
            notes.append('growing unusually fast')
    if value['wal_mb'] > WAL_WARN_MB:
        notes.append(f'WAL file {value["wal_mb"]} MB — checkpoints are '
                     f'not keeping up')
    if notes:
        status = WARN
    return result(key, label, status, ', '.join(parts)
                  + (' — ' + '; '.join(notes) if notes else ''), value)


def check_database(ctx):
    return _database(ctx, 'database', 'Database integrity', True)


def check_database_stats(ctx):
    """For the web process: the same numbers without quick_check, which
    reads every page and would hold a worker for seconds."""
    return _database(ctx, 'database_stats', 'Database size and journal',
                     False)


# ── disk ─────────────────────────────────────────────────────────────
def check_disk(ctx):
    key, label = 'disk', 'Disk space'
    places = [('database', os.path.dirname(os.path.abspath(ctx.db_path))
               if ctx.db_path else ctx.root),
              ('backups', ctx.backups_dir if os.path.isdir(ctx.backups_dir)
               else ctx.root)]
    db_size = os.path.getsize(ctx.db_path) if ctx.db_path and \
        os.path.exists(ctx.db_path) else 0
    seen, value, parts, statuses = set(), {}, [], []
    for name, path in places:
        try:
            dev = os.stat(path).st_dev
            usage = shutil.disk_usage(path)
        except OSError as exc:
            parts.append(f'{name}: unreadable ({exc.strerror})')
            statuses.append(UNKNOWN)
            continue
        if dev in seen:
            parts.append(f'{name}: same volume')
            continue
        seen.add(dev)
        free_gb = usage.free / 1_073_741_824
        pct = 100.0 * usage.used / usage.total if usage.total else 0
        value[f'{name}_free_gb'] = round(free_gb, 1)
        value[f'{name}_used_pct'] = round(pct, 1)
        st = OK
        if free_gb < DISK_FAIL_GB or pct >= DISK_FAIL_PCT or (
                name == 'backups' and usage.free < 1.2 * db_size):
            st = FAIL
        elif free_gb < DISK_WARN_GB or pct >= DISK_WARN_PCT or (
                usage.free < 3 * db_size):
            st = WARN
        statuses.append(st)
        parts.append(f'{name} volume {free_gb:.1f} GB free ({pct:.0f}% used)'
                     + ('' if st == OK else f' [{st}]'))
    status = worst(statuses) if statuses else UNKNOWN
    if status == FAIL:
        parts.append('free space now — the next backup or SQLite '
                     'journal may not fit')
    return result(key, label, status, '; '.join(parts), value)


# ── queues ───────────────────────────────────────────────────────────
def _with_db(ctx, key, label, fn):
    if not ctx.db_path:
        return result(key, label, UNKNOWN, 'DATABASE_URL is not SQLite')
    try:
        conn = open_ro(ctx.db_path)
    except (OSError, sqlite3.Error) as exc:
        return result(key, label, UNKNOWN, f'database not readable: {exc}')
    try:
        return fn(conn)
    finally:
        conn.close()


def check_email_queue(ctx):
    key, label = 'email_queue', 'Email ingest queue (24 h)'

    def run(conn):
        if 'email_events' not in _tables(conn):
            return result(key, label, UNKNOWN, 'email_events table missing')
        now = _utcnow()
        since = _sql_time(now - timedelta(hours=24))
        by_status = dict(conn.execute(
            'SELECT COALESCE(status, \'\'), COUNT(*) FROM email_events '
            'WHERE received_at >= ? GROUP BY status', (since,)).fetchall())
        total = sum(by_status.values())
        # A row left at received/processing means the webhook died part
        # way through a message: a lead that may never be created. Looked
        # for over a week so it does not quietly age out of the window.
        week = _sql_time(now - timedelta(days=7))
        pending = _scalar(conn, "SELECT COUNT(*) FROM email_events WHERE "
                          "status IN ('received', 'processing') AND "
                          "received_at >= ?", (week,)) or 0
        oldest = _parse_dt(_scalar(
            conn, "SELECT MIN(received_at) FROM email_events WHERE "
            "status IN ('received', 'processing') AND received_at >= ?",
            (week,)))
        oldest_min = int((now - oldest).total_seconds() // 60) \
            if oldest else None
        failed = by_status.get('failed', 0)
        rejected = by_status.get('rejected', 0)
        value = {'events_24h': total, 'by_status': by_status,
                 'failed_24h': failed, 'rejected_24h': rejected,
                 'pending': pending, 'oldest_pending_minutes': oldest_min}
        parts = [f'{total} events', f'{failed} failed',
                 f'{rejected} rejected', f'{pending} unprocessed']
        status, notes = OK, []
        if oldest_min is not None and oldest_min >= QUEUE_STUCK_FAIL_MIN:
            status = FAIL
            notes.append(f'oldest unprocessed is {oldest_min // 60} h old — '
                         f'retry it from Email Inbox')
        elif oldest_min is not None and oldest_min >= QUEUE_STUCK_WARN_MIN:
            status = WARN
            notes.append(f'oldest unprocessed is {oldest_min} min old')
        if failed:
            status = worst([status, WARN])
            notes.append('failures — read the reasons in Email Inbox')
        if rejected:
            status = worst([status, WARN])
            notes.append('notifications for another mailbox — a stray '
                         'subscription exists')
        if total == 0:
            status = worst([status, WARN])
            notes.append('no email at all in 24 h — check the subscription')
        return result(key, label, status, ', '.join(parts)
                      + (' — ' + '; '.join(notes) if notes else ''), value)

    return _with_db(ctx, key, label, run)


def check_review_queue(ctx):
    key, label = 'review_queue', 'Lead review queue'

    def run(conn):
        if 'email_classifications' not in _tables(conn):
            return result(key, label, UNKNOWN,
                          'email_classifications table missing')
        n = _scalar(conn, "SELECT COUNT(*) FROM email_classifications "
                    "WHERE review_state = 'pending'") or 0
        oldest = _parse_dt(_scalar(
            conn, "SELECT MIN(created_at) FROM email_classifications "
            "WHERE review_state = 'pending'"))
        age_h = (_utcnow() - oldest).total_seconds() / 3600 \
            if oldest else None
        value = {'pending': n, 'oldest_hours':
                 round(age_h, 1) if age_h is not None else None}
        if not n:
            return result(key, label, OK, 'empty', value)
        notes = []
        if n > REVIEW_WARN_COUNT:
            notes.append('larger than usual — the classifier or the '
                         'mailbox may have changed')
        if age_h is not None and age_h > REVIEW_WARN_H:
            notes.append('items waiting more than two days — assign a '
                         'reviewer')
        return result(key, label, WARN if notes else OK,
                      f'{n} waiting, oldest {age_h:.0f} h'
                      + (' — ' + '; '.join(notes) if notes else ''), value)

    return _with_db(ctx, key, label, run)


# ── Copilot index ────────────────────────────────────────────────────
def check_copilot_index(ctx):
    key, label = 'copilot_index', 'Copilot index'
    if not ctx.db_path or not os.path.exists(ctx.db_path):
        return result(key, label, UNKNOWN, 'database not readable')
    pf = preflight()
    try:
        engine = pf.read_only_engine('sqlite:///' + ctx.db_path)
    except (Exception, SystemExit) as exc:  # read_only_engine SystemExits
        return result(key, label, UNKNOWN, f'database not readable: {exc}')
    try:
        with engine.connect() as sa:
            leads, indexed, with_text = pf.copilot_coverage(sa)
    finally:
        engine.dispose()

    def run(conn):
        if indexed is None:
            return result(key, label, WARN, 'copilot_chunk table missing — '
                          'run the copilot index migration')
        present = _tables(conn)
        newest = _parse_dt(_scalar(conn,
                                   'SELECT MAX(indexed_at) FROM copilot_chunk'))
        value = {'leads': leads, 'indexed_leads': indexed,
                 'leads_with_enquiry': with_text,
                 'coverage_pct': round(100.0 * indexed / with_text, 1)
                 if with_text else None}
        if newest is None:
            return result(key, label, WARN if with_text else OK,
                          'never built — run scripts/build_copilot_index.py'
                          if with_text else 'nothing to index yet', value)
        t = _sql_time(newest)
        newer = {
            'leads': _scalar(conn, 'SELECT COUNT(*) FROM leads WHERE '
                             'updated_at > ?', (t,)) or 0,
            'emails': (_scalar(conn, 'SELECT COUNT(*) FROM lead_emails '
                               'WHERE created_at > ?', (t,)) or 0)
            if 'lead_emails' in present else 0,
            'notes': (_scalar(conn, 'SELECT COUNT(*) FROM lead_notes WHERE '
                              'COALESCE(updated_at, created_at) > ?', (t,))
                      or 0) if 'lead_notes' in present else 0,
        }
        age_h = (_utcnow() - newest).total_seconds() / 3600
        value.update(newest_chunk_hours=round(age_h, 1),
                     changed_since=newer)
        pending = sum(newer.values())
        notes = []
        if with_text and indexed < INDEX_COVERAGE * with_text:
            notes.append(f'only {indexed} of {with_text} leads with an '
                         f'enquiry are indexed')
        if pending and age_h > INDEX_STALE_H:
            notes.append(f'{pending} changes since the last build '
                         f'{age_h:.0f} h ago — the nightly rebuild has not '
                         f'run')
        return result(key, label, WARN if notes else OK,
                      f'{indexed} leads indexed of {with_text} with an '
                      f'enquiry; built {age_h:.0f} h ago, {pending} changes '
                      f'since' + (' — ' + '; '.join(notes) if notes else ''),
                      value)

    return _with_db(ctx, key, label, run)


# ── gunicorn workers ─────────────────────────────────────────────────
def _processes():
    """[(pid, ppid, rss_kb, args)] or None when neither ps nor /proc can
    be read."""
    out = _run(['ps', '-eo', 'pid=,ppid=,rss=,args='])
    if out and out[0] == 0:
        procs = []
        for line in out[1].splitlines():
            bits = line.split(None, 3)
            if len(bits) == 4 and bits[0].isdigit() and bits[1].isdigit():
                procs.append((int(bits[0]), int(bits[1]),
                              int(bits[2]) if bits[2].isdigit() else 0,
                              bits[3]))
        return procs
    if not os.path.isdir('/proc'):
        return None
    procs = []
    for pid in (p for p in os.listdir('/proc') if p.isdigit()):
        try:
            with open(f'/proc/{pid}/stat') as fh:
                stat = fh.read()
            with open(f'/proc/{pid}/cmdline', 'rb') as fh:
                args = fh.read().replace(b'\0', b' ').decode(
                    'utf-8', 'replace').strip()
            rss = 0
            with open(f'/proc/{pid}/status') as fh:
                for line in fh:
                    if line.startswith('VmRSS:'):
                        rss = int(line.split()[1])
            # comm may contain spaces; the fields after its ')' do not
            ppid = int(stat.rsplit(')', 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        procs.append((int(pid), ppid, rss, args))
    return procs


def _expected_workers(ctx, master_args):
    out = _run(['systemctl', 'show', ctx.service, '-p', 'ExecStart',
                '--no-pager'])
    for text, source in (((out[1] if out and out[0] == 0 else ''),
                          'systemd ExecStart'), (master_args, 'master args')):
        m = re.search(r'(?:--workers[= ]|-w\s*)(\d+)', text or '')
        if m:
            return int(m.group(1)), source
    return None, None


def check_workers(ctx):
    key, label = 'workers', 'gunicorn workers'
    procs = _processes()
    if procs is None:
        return result(key, label, UNKNOWN, 'neither ps nor /proc is '
                      'readable on this host')
    guni = {p[0]: p for p in procs if 'gunicorn' in p[3]}
    masters = [p for p in guni.values() if p[1] not in guni]
    if len(masters) > 1:
        port = ctx.bind.rsplit(':', 1)[-1]
        masters = [p for p in masters if port in p[3]] or masters
    if not masters:
        return result(key, label, FAIL, f'no gunicorn master process — '
                      f'{ctx.service} is not running',
                      {'master_pid': None, 'workers': 0})
    master = masters[0]
    workers = [p for p in guni.values() if p[1] == master[0]]
    expected, source = _expected_workers(ctx, master[3])
    rss = [round(p[2] / 1024, 1) for p in workers]
    value = {'master_pid': master[0], 'workers': len(workers),
             'expected': expected, 'worker_rss_mb': rss,
             'total_rss_mb': round(sum(rss) + master[2] / 1024, 1)}
    detail = (f'master {master[0]}, {len(workers)} worker(s)'
              + (f' of {expected} ({source})' if expected else
                 ' (expected count not readable)')
              + (f', {min(rss):.0f}–{max(rss):.0f} MB each' if rss else ''))
    if not workers:
        return result(key, label, FAIL, detail + ' — no workers are '
                      'serving requests', value)
    notes = []
    if expected and len(workers) < expected:
        notes.append('fewer workers than configured — they are crashing '
                     'or being killed (journalctl -u ' + ctx.service + ')')
    heavy = [r for r in rss if r > WORKER_RSS_WARN_MB]
    if heavy:
        notes.append(f'{len(heavy)} worker(s) above {WORKER_RSS_WARN_MB} MB')
    return result(key, label, WARN if notes else OK,
                  detail + (' — ' + '; '.join(notes) if notes else ''), value)


# ── scheduled tasks ──────────────────────────────────────────────────
def parse_systemd_time(text):
    """Unix seconds from `systemctl show` output, or None for n/a."""
    text = (text or '').strip()
    if not text or text in ('n/a', '0'):
        return None
    if text.startswith('@') and text[1:].replace('.', '', 1).isdigit():
        return float(text[1:])
    m = re.search(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:\s+([A-Za-z]+))?',
                  text)
    if not m:
        return None
    dt = datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S')
    if (m.group(2) or '').upper() in ('UTC', 'GMT'):
        return dt.replace(tzinfo=timezone.utc).timestamp()
    # systemctl prints in the host's own zone; so does mktime.
    return time.mktime(dt.timetuple())


def _show(unit, *props):
    cmd = ['systemctl', 'show', unit, '--no-pager']
    for p in props:
        cmd += ['-p', p]
    out = _run(cmd)
    if not out or out[0] != 0:
        return None
    return dict(line.split('=', 1) for line in out[1].splitlines()
                if '=' in line)


def check_timers(ctx):
    key, label = 'timers', 'Scheduled tasks'
    out = _run(['systemctl', 'list-timers', '--all', '--no-pager',
                '--no-legend'])
    if out is None:
        return result(key, label, UNKNOWN, 'systemctl is not available '
                      'on this host')
    if out[0] != 0:
        return result(key, label, UNKNOWN, 'systemctl list-timers failed: '
                      + (out[2] or '').strip()[:120])
    present = sorted(set(re.findall(r'(procam-crm[\w@.-]*?\.timer)',
                                    out[1])))
    now = time.time()
    timers, statuses, notes = {}, [], []
    for name in present:
        props = _show(name, 'LastTriggerUSec', 'NextElapseUSecRealtime',
                      'Unit') or {}
        unit = props.get('Unit') or name[:-len('.timer')] + '.service'
        svc = _show(unit, 'Result', 'ExecMainStatus', 'ActiveState') or {}
        last = parse_systemd_time(props.get('LastTriggerUSec'))
        nxt = parse_systemd_time(props.get('NextElapseUSecRealtime'))
        res = svc.get('Result') or ''
        timers[name] = {
            'last_run': _iso(last) if last else None,
            'next_run': _iso(nxt) if nxt else None,
            'last_result': res or None,
            'exit_status': svc.get('ExecMainStatus'),
            'hours_since_run': _hours(now - last) if last else None,
        }
        gap = EXPECTED_TIMERS.get(name)
        short = name[len('procam-crm-'):-len('.timer')]
        if res and res != 'success':
            statuses.append(FAIL)
            notes.append(f'{short} last run {res} (journalctl -u {unit})')
        elif last is None:
            statuses.append(WARN)
            notes.append(f'{short} has never run')
        elif gap and now - last > gap * 3600:
            statuses.append(WARN)
            notes.append(f'{short} overdue, last ran {_hours(now - last)} h '
                         f'ago')
        else:
            statuses.append(OK)
    missing = [n for n in EXPECTED_TIMERS if n not in present]
    if missing:
        statuses.append(WARN)
        notes.append('not installed: ' + ', '.join(
            n[len('procam-crm-'):-len('.timer')] for n in missing))
    status = worst(statuses) if statuses else WARN
    return result(key, label, status,
                  f'{len(present)} procam-crm timer(s)'
                  + (' — ' + '; '.join(notes) if notes else ', all ran '
                     'on schedule'), {'timers': timers, 'missing': missing})


# ── deployment and rollback ──────────────────────────────────────────
def _git(ctx, *args):
    # --no-optional-locks: `git status` otherwise refreshes and rewrites
    # .git/index, a write on a server that is supposed to be pull-only.
    cache = ctx._git
    if args not in cache:
        cache[args] = _run(['git', '--no-optional-locks', '-C', ctx.root]
                           + list(args))
    out = cache[args]
    # rstrip only: porcelain status lines begin with a meaningful space.
    return out[1].rstrip() if out and out[0] == 0 else None


def check_deployment(ctx):
    key, label = 'deployment', 'Deployed code'
    commit = _git(ctx, 'rev-parse', 'HEAD')
    if not commit:
        return result(key, label, UNKNOWN, 'git cannot read the checkout '
                      '(not a git tree, git missing, or git refusing a '
                      'checkout owned by another user)')
    branch = _git(ctx, 'rev-parse', '--abbrev-ref', 'HEAD') or '?'
    ct = _git(ctx, 'log', '-1', '--format=%ct', 'HEAD')
    dirty = _git(ctx, 'status', '--porcelain', '--untracked-files=no')
    reflog = _git(ctx, 'log', '-g', '-1', '--format=%gd%x09%gs',
                  '--date=unix', 'HEAD')
    moved_at, moved_how = None, ''
    if reflog:
        m = re.match(r'.*@\{(\d+)\}\t?(.*)', reflog)
        if m:
            moved_at, moved_how = int(m.group(1)), m.group(2)[:80]
    svc = _show(ctx.service, 'ActiveEnterTimestamp') or {}
    started = parse_systemd_time(svc.get('ActiveEnterTimestamp'))
    changed = [l[3:] for l in (dirty or '').splitlines() if l.strip()]
    value = {'commit': commit[:12], 'branch': branch,
             'commit_time': _iso(int(ct)) if ct and ct.isdigit() else None,
             'last_deploy': _iso(moved_at) if moved_at else None,
             'last_deploy_action': moved_how or None,
             'service_started': _iso(started) if started else None,
             'modified_files': len(changed)}
    parts = [f'{commit[:12]} on {branch}']
    if moved_at:
        parts.append(f'checked out {_iso(moved_at)[:16]} UTC ({moved_how})')
    notes, status = [], OK
    if dirty is None:
        notes.append('working tree state not readable')
    elif changed:
        status = WARN
        notes.append(f'{len(changed)} tracked file(s) modified on the '
                     f'server ({", ".join(changed[:5])}) — the server is '
                     f'pull-only')
    # A minute of slack: a restart run straight after the pull can start
    # within the same second the reflog records.
    if moved_at and started and moved_at > started + 60:
        status = WARN
        notes.append('code changed after the service started — restart '
                     'pending, the old code is still serving')
    return result(key, label, status, '; '.join(parts)
                  + (' — ' + '; '.join(notes) if notes else ''), value)


def check_rollback(ctx):
    """Is there a way back from the running commit? A pre-deploy backup
    taken after that commit was made, and one that opens."""
    key, label = 'rollback', 'Rollback readiness'
    ct = _git(ctx, 'log', '-1', '--format=%ct', 'HEAD')
    if not ct or not ct.isdigit():
        return result(key, label, UNKNOWN, 'running commit time not '
                      'readable from git')
    commit_time = int(ct)
    pre = [f for f in preflight().backup_files(ctx.backups_dir)
           if 'pre-deploy' in os.path.basename(f)]
    if not pre:
        return result(key, label, WARN, 'no pre-deploy backup in '
                      f'{ctx.backups_dir} — a database rollback would use '
                      f'the nightly backup and lose more')
    newest = max(pre, key=os.path.getmtime)
    name = os.path.basename(newest)
    mtime = os.path.getmtime(newest)
    value = {'backup': name, 'backup_time': _iso(mtime),
             'commit_time': _iso(commit_time)}
    if mtime < commit_time:
        return result(key, label, WARN, f'newest pre-deploy backup {name} '
                      f'is older than the running commit — this deploy was '
                      f'made without one', value)
    try:
        conn = open_ro(newest)
        try:
            qc = _scalar(conn, 'PRAGMA quick_check')
        finally:
            conn.close()
    except (OSError, sqlite3.Error) as exc:
        return result(key, label, FAIL, f'{name} cannot be opened: {exc}',
                      value)
    value['quick_check'] = qc
    if qc != 'ok':
        return result(key, label, FAIL, f'{name} fails quick_check — there '
                      f'is no safe database rollback for this deploy', value)
    return result(key, label, OK, f'{name} taken after the running commit, '
                  f'passes quick_check', value)


# ── preflight: schema and configuration ──────────────────────────────
def _summarise(rows):
    bad = [r for r in rows if r['status'] in ('FAIL', 'WARN')]
    status = FAIL if any(r['status'] == 'FAIL' for r in rows) else \
        WARN if bad else OK
    counts = {s: sum(r['status'] == s for r in rows)
              for s in ('PASS', 'WARN', 'FAIL', 'INFO')}
    detail = (f'{counts["PASS"]} pass, {counts["WARN"]} warn, '
              f'{counts["FAIL"]} fail')
    if bad:
        # A fresh database lists sixty missing tables; the page needs the
        # gist, and the preflight prints the whole list.
        detail += ' — ' + '; '.join(
            f'{r["status"]} {r["check"]}: '
            + (r['detail'] if len(r['detail']) <= 240
               else r['detail'][:240] + '…') for r in bad)
    return status, detail, counts


def check_schema(ctx):
    key, label = 'schema', 'Schema drift'
    if not ctx.schema:
        return result(key, label, UNKNOWN, 'skipped (--no-schema)')
    if not ctx.db_path or not os.path.exists(ctx.db_path):
        return result(key, label, UNKNOWN, 'database not readable')
    pf = preflight()
    try:
        expected = pf.expected_schema()
    except Exception as exc:
        return result(key, label, UNKNOWN, 'could not build the expected '
                      f'schema: {str(exc)[-200:]}')
    rep = pf.Report()
    engine = pf.read_only_engine('sqlite:///' + ctx.db_path)
    try:
        with engine.connect() as conn:
            pf.check_schema(rep, conn, expected)
    finally:
        engine.dispose()
    status, detail, counts = _summarise(rep.rows)
    return result(key, label, status, detail, counts)


def check_config(ctx):
    key, label = 'config', 'Configuration'
    pf = preflight()
    rep = pf.Report()
    pf.check_config(rep, ctx.env_path)
    status, detail, counts = _summarise(rep.rows)
    return result(key, label, status, detail, counts)


# ── running a set ────────────────────────────────────────────────────
#: (key, function). Order is the order of the report.
CHECKS = [
    ('health', check_health),
    ('workers', check_workers),
    ('database', check_database),
    ('disk', check_disk),
    ('backups', check_backups),
    ('restore', check_restore),
    ('rollback', check_rollback),
    ('deployment', check_deployment),
    ('schema', check_schema),
    ('config', check_config),
    ('timers', check_timers),
    ('email_queue', check_email_queue),
    ('review_queue', check_review_queue),
    ('graph_subscription', check_subscription),
    ('graph_token', check_graph_token),
    ('graph_secret_expiry', check_graph_secret_expiry),
    ('tls_certificate', check_certificate),
    ('copilot_index', check_copilot_index),
]
CHECK_KEYS = [k for k, _f in CHECKS]

#: What the web process may run on every page load: local, read-only,
#: and bounded by indexed queries.
LIVE_CHECKS = [
    ('database_stats', check_database_stats),
    ('email_queue', check_email_queue),
    ('review_queue', check_review_queue),
    ('copilot_index', check_copilot_index),
]


def _guarded(key, fn, ctx):
    try:
        res = fn(ctx)
    except Exception as exc:
        res = result(key, key, UNKNOWN, f'check crashed: '
                     f'{type(exc).__name__}: {str(exc)[:200]}')
    return scrub(res)


def run_checks(ctx, only=None, checks=None):
    wanted = set(only) if only else None
    return [_guarded(k, f, ctx) for k, f in (checks or CHECKS)
            if wanted is None or k in wanted]


def live_checks(ctx):
    return run_checks(ctx, checks=LIVE_CHECKS)


def build_report(results, ctx, only=None):
    counts = {s: sum(r['status'] == s for r in results)
              for s in (OK, WARN, FAIL, UNKNOWN)}
    return {'generated_at': _iso(), 'host': socket.gethostname(),
            'overall': worst(r['status'] for r in results),
            'counts': counts, 'network': ctx.network, 'schema': ctx.schema,
            'only': sorted(only) if only else None, 'checks': results}


def exit_code(results):
    """1 when anything FAILs. WARN and UNKNOWN are for people, not
    pagers."""
    return 1 if any(r['status'] == FAIL for r in results) else 0


def write_status(report, path):
    """Atomically: the web page must never read half a file."""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, mode=0o750, exist_ok=True)
    tmp = os.path.join(folder, f'.{os.path.basename(path)}.{os.getpid()}.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    try:
        with os.fdopen(fd, 'w') as fh:
            json.dump(report, fh, indent=1, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def default_status_path(environ=None):
    return _env('OPS_STATUS_FILE', environ) or os.path.join(
        ROOT, 'instance', 'ops_status.json')


def read_status(path):
    """{path, report, age_seconds, stale, error} — never raises."""
    out = {'path': path, 'report': None, 'age_seconds': None,
           'stale': True, 'error': None}
    try:
        with open(path) as fh:
            report = json.load(fh)
        age = time.time() - os.path.getmtime(path)
    except FileNotFoundError:
        out['error'] = 'no status file yet — install the ops-status timer'
        return out
    except (OSError, ValueError) as exc:
        out['error'] = f'status file not readable: {type(exc).__name__}'
        return out
    if not isinstance(report, dict) or not isinstance(
            report.get('checks'), list):
        out['error'] = 'status file is not an ops_status report'
        return out
    out.update(report=report, age_seconds=int(age),
               stale=age > STATUS_STALE_MIN * 60)
    return out
