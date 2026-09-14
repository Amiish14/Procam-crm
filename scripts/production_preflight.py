"""
Production preflight. Read-only. Run before and after every deploy.

    .venv/bin/python scripts/production_preflight.py
    .venv/bin/python scripts/production_preflight.py --json

Answers, from the server itself rather than from memory:

  config    every setting that decides whether the CRM is safe to run —
            secrets present and not placeholders, cookies secure, AI hosts
            private, email ingest wired — without ever printing a value
  schema    the live database compared with what the code expects:
            missing tables, columns and indexes (the drift that takes a
            page down after a deploy)
  database  integrity check, journal mode, foreign-key enforcement
  ops       backups present and recent, disk space, the Graph
            subscription's age, Copilot index coverage, and the row counts
            that a retention decision needs

Exit status is 1 when any check FAILs, so it can gate a deploy script.

How "expected schema" is found without touching production: the app is
imported in a child process pointed at an in-memory SQLite database. That
builds the models' metadata (and runs the boot autoheal against memory,
not against the real file). The production database is only ever opened
with SQLite mode=ro.
"""
import argparse
import ipaddress
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)

try:
    from dotenv import dotenv_values, load_dotenv
    load_dotenv(os.path.join(_ROOT, '.env'))
except ImportError:                                  # pragma: no cover
    dotenv_values = None

from sqlalchemy import text                          # noqa: E402

from data_quality_report import read_only_engine     # noqa: E402

PASS, WARN, FAIL, INFO = 'PASS', 'WARN', 'FAIL', 'INFO'

#: Values that have appeared in this repository's docs or examples. A
#: server using one is using a secret the public has.
PUBLISHED = {
    'SECRET_KEY': {'procam-crm-secret-change-me-2025', 'test', 'dev'},
    'EMAIL_WEBHOOK_SECRET': {'set-a-random-string-here'},
    'ADMIN_INITIAL_PASSWORD': {'admin@Procam25'},
}

#: Public AI endpoints. CRM data must never be sent to one (Procam AI §3.1).
PUBLIC_AI_HOSTS = ('api.openai.com', 'api.anthropic.com', 'api.groq.com',
                   'generativelanguage.googleapis.com', 'openrouter.ai',
                   'api.together.xyz', 'api.mistral.ai', 'api.cohere.ai',
                   'api.deepseek.com', 'api.fireworks.ai')


class Report:
    def __init__(self):
        self.rows = []

    def add(self, area, name, status, detail=''):
        self.rows.append({'area': area, 'check': name, 'status': status,
                          'detail': detail})

    @property
    def failed(self):
        return any(r['status'] == FAIL for r in self.rows)


def _env(name):
    return (os.environ.get(name) or '').strip()


def _on(name, default='off'):
    return (_env(name) or default).lower() in ('1', 'true', 'on', 'yes')


# ── config ───────────────────────────────────────────────────────────
def host_is_private(url, resolve=True):
    """(verdict, why). True only when the host is provably internal."""
    host = re.sub(r'^[a-z]+://', '', url.strip(), flags=re.I)
    host = host.split('/')[0].split('@')[-1]
    host = host.rsplit(':', 1)[0] if host.count(':') == 1 else host
    host = host.strip('[]').lower()
    if not host:
        return False, 'no host'
    for bad in PUBLIC_AI_HOSTS:
        if host == bad or host.endswith('.' + bad):
            return False, f'{host} is a public AI API'
    if host in ('localhost',):
        return True, 'localhost'
    try:
        ip = ipaddress.ip_address(host)
        return (ip.is_private or ip.is_loopback), f'{host} literal'
    except ValueError:
        pass
    if not resolve:
        return False, f'{host} not resolved'
    try:
        addrs = {a[4][0] for a in socket.getaddrinfo(host, None)}
    except OSError:
        return False, f'{host} does not resolve'
    private = all(ipaddress.ip_address(a).is_private or
                  ipaddress.ip_address(a).is_loopback for a in addrs)
    return private, f'{host} resolves to {"private" if private else "public"}' \
                    f' addresses'


def check_config(rep, env_path):
    area = 'config'

    key = _env('SECRET_KEY')
    if not key:
        rep.add(area, 'SECRET_KEY', FAIL, 'unset — each gunicorn worker '
                'would sign sessions with a different random key')
    elif key in PUBLISHED['SECRET_KEY'] or len(key) < 32:
        rep.add(area, 'SECRET_KEY', FAIL, 'a published placeholder or '
                'shorter than 32 characters — rotate it')
    else:
        rep.add(area, 'SECRET_KEY', PASS, f'set, {len(key)} characters')

    if _env('ADMIN_INITIAL_PASSWORD') in PUBLISHED['ADMIN_INITIAL_PASSWORD']:
        rep.add(area, 'ADMIN_INITIAL_PASSWORD', FAIL,
                'is the password published in DEPLOY.md')
    else:
        rep.add(area, 'ADMIN_INITIAL_PASSWORD', PASS,
                'not the published value' if _env('ADMIN_INITIAL_PASSWORD')
                else 'unset (only needed to seed an empty database)')

    if _env('SESSION_COOKIE_SECURE').lower() == 'false':
        rep.add(area, 'SESSION_COOKIE_SECURE', FAIL, 'false in production')
    else:
        rep.add(area, 'SESSION_COOKIE_SECURE', PASS, 'secure cookies')
    rep.add(area, 'DEBUG', FAIL if _on('DEBUG') else PASS,
            'on' if _on('DEBUG') else 'off')
    prefix = _env('URL_PREFIX')
    rep.add(area, 'URL_PREFIX', PASS if prefix == '/CRM' else WARN,
            prefix or 'unset — links and the CSP report path lose /CRM')

    url = _env('DATABASE_URL')
    if not url:
        rep.add(area, 'DATABASE_URL', WARN, 'unset — the app would use '
                'procam_crm.db in its working directory')
    elif url.startswith('sqlite:///'):
        path = url[len('sqlite:///'):]
        rep.add(area, 'DATABASE_URL', PASS if os.path.exists(path) else FAIL,
                f'SQLite at {path}' + ('' if os.path.exists(path)
                                       else ' — FILE NOT FOUND'))
    else:
        rep.add(area, 'DATABASE_URL', INFO, 'not SQLite')

    # Email ingest and Graph
    creds = [k for k in ('MS_TENANT_ID', 'MS_CLIENT_ID', 'MS_CLIENT_SECRET')
             if not _env(k)]
    rep.add(area, 'Graph credentials', WARN if creds else PASS,
            ('missing: ' + ', '.join(creds)) if creds else 'all three set')
    secret = _env('EMAIL_WEBHOOK_SECRET')
    if not secret:
        rep.add(area, 'EMAIL_WEBHOOK_SECRET', FAIL, 'unset — the webhook '
                'refuses every notification (fails closed)')
    elif secret in PUBLISHED['EMAIL_WEBHOOK_SECRET'] or len(secret) < 16:
        rep.add(area, 'EMAIL_WEBHOOK_SECRET', FAIL,
                'placeholder or shorter than 16 characters')
    else:
        rep.add(area, 'EMAIL_WEBHOOK_SECRET', PASS, 'set')
    inbox = _env('CRM_INBOX_EMAIL') or 'leads@procamgroup.in (default)'
    rep.add(area, 'CRM_INBOX_EMAIL', INFO, inbox)
    rep.add(area, 'EMAIL_INGESTION_MODE', INFO,
            _env('EMAIL_INGESTION_MODE') or 'mailbox (default)')
    rep.add(area, 'LEAD_INTAKE_MODE', INFO,
            _env('LEAD_INTAKE_MODE') or 'enforce (default)')
    if _on('NOTIFY_ENABLED', 'true'):
        rep.add(area, 'NOTIFY_ENABLED', INFO, 'on (default) — assignment '
                'emails fail with 403 until Mail.Send is granted; failures '
                'are logged, nothing else breaks')
    else:
        rep.add(area, 'NOTIFY_ENABLED', INFO, 'off')

    # AI
    rep.add(area, 'PROCAM_AI_ACTIONS', WARN if _on('PROCAM_AI_ACTIONS')
            else PASS, 'ON — Copilot can propose writes' if
            _on('PROCAM_AI_ACTIONS') else 'off (default)')
    for name in ('PROCAM_AI_BASE_URL', 'PROCAM_AI_EMBED_URL'):
        val = _env(name)
        if not val:
            rep.add(area, name, INFO, 'unset — '
                    + ('Copilot answers deterministically, no model'
                       if name.endswith('BASE_URL')
                       else 'lexical retrieval'))
            continue
        ok, why = host_is_private(val)
        rep.add(area, name, PASS if ok else FAIL, why)
    public = []
    if _on('LEAD_INTAKE_AI') and (_env('GROQ_API_KEY') or
                                  _env('ANTHROPIC_API_KEY')):
        public.append('intake classifier (LEAD_INTAKE_AI) sends email text '
                      'to Groq/Anthropic')
    if _env('GROQ_API_KEY'):
        public.append('email extraction uses Groq')
    if _env('ANTHROPIC_API_KEY'):
        public.append('AI Outreach, card OCR and fallback extraction use '
                      'Anthropic')
    rep.add(area, 'public AI providers', WARN if public else PASS,
            '; '.join(public) + ' — a business decision, see the AI '
            'Configuration Guide' if public else 'none configured')

    # the .env file itself
    if os.path.exists(env_path):
        mode = stat.S_IMODE(os.stat(env_path).st_mode)
        loose = mode & (stat.S_IRWXG | stat.S_IRWXO)
        rep.add(area, '.env permissions', WARN if loose else PASS,
                f'{oct(mode)}' + (' — readable beyond its owner; '
                                  'chmod 600 .env' if loose else ''))
    else:
        rep.add(area, '.env permissions', WARN, f'{env_path} not found')
    rep.add(area, 'LOG_LEVEL', INFO, _env('LOG_LEVEL') or 'INFO (default)')


# ── schema ───────────────────────────────────────────────────────────
_EXPECTED = r'''
import json, os, sys
os.environ["DATABASE_URL"] = "sqlite://"
os.environ.setdefault("SECRET_KEY", "preflight-only-" + "x" * 32)
os.environ.setdefault("ADMIN_INITIAL_PASSWORD", "PreflightOnly-123456")
import logging; logging.disable(logging.CRITICAL)
import io, contextlib
with contextlib.redirect_stdout(io.StringIO()):
    import app as A
    import app.models  # noqa
    for mod in ("app.models.copilot", "app.models.audit",
                "app.models.business_card", "app.models.tms_handover",
                "app.models.rfq", "app.models.quote"):
        try:
            __import__(mod)
        except Exception:
            pass
out = {}
for t in A.db.metadata.sorted_tables:
    out[t.name] = {
        "columns": sorted(c.name for c in t.columns),
        "indexes": sorted(i.name for i in t.indexes if i.name),
    }
print("PREFLIGHT_SCHEMA" + json.dumps(out))
'''


def expected_schema():
    env = {k: v for k, v in os.environ.items()}
    env['DATABASE_URL'] = 'sqlite://'
    proc = subprocess.run([sys.executable, '-c', _EXPECTED], cwd=_ROOT,
                          env=env, capture_output=True, text=True,
                          timeout=180)
    for line in proc.stdout.splitlines():
        if line.startswith('PREFLIGHT_SCHEMA'):
            return json.loads(line[len('PREFLIGHT_SCHEMA'):])
    raise RuntimeError('could not build the expected schema: '
                       + (proc.stderr or '')[-600:])


def check_schema(rep, conn, expected):
    area = 'schema'
    live_tables = {r[0] for r in conn.execute(text(
        "SELECT name FROM sqlite_master WHERE type='table'"))}
    missing_tables, missing_cols, missing_idx = [], [], []
    for table, spec in expected.items():
        if table not in live_tables:
            missing_tables.append(table)
            continue
        cols = {r[1] for r in conn.execute(text(
            f'PRAGMA table_info("{table}")'))}
        missing_cols += [f'{table}.{c}' for c in spec['columns']
                         if c not in cols]
        idx = {r[1] for r in conn.execute(text(
            f'PRAGMA index_list("{table}")'))}
        missing_idx += [f'{table}.{i}' for i in spec['indexes']
                        if i not in idx]
    # A missing column breaks every query on its table — that is a FAIL.
    rep.add(area, 'columns', FAIL if missing_cols else PASS,
            ', '.join(missing_cols) or
            f'all columns of {len(expected)} tables present')
    # A missing table is usually a feature whose migration has not run
    # (create_all at boot adds it); worth fixing, not an outage.
    rep.add(area, 'tables', WARN if missing_tables else PASS,
            ', '.join(missing_tables) or 'all present')
    # Indexes are performance, not correctness.
    rep.add(area, 'indexes', WARN if missing_idx else PASS,
            (f'{len(missing_idx)} missing: ' + ', '.join(missing_idx[:12])
             + (' …' if len(missing_idx) > 12 else '')
             + ' — add with scripts/ensure_model_indexes.py') if missing_idx
            else 'all declared indexes present')
    return missing_cols


def check_database(rep, conn, db_path):
    area = 'database'
    qc = conn.execute(text('PRAGMA quick_check')).scalar()
    rep.add(area, 'integrity (quick_check)', PASS if qc == 'ok' else FAIL,
            qc)
    rep.add(area, 'journal_mode', INFO,
            conn.execute(text('PRAGMA journal_mode')).scalar())
    fk = conn.execute(text('PRAGMA foreign_keys')).scalar()
    rep.add(area, 'foreign key enforcement', INFO,
            'on' if fk else 'off (SQLite default) — the app deletes '
            'dependants explicitly; declared FKs are not enforced')
    if db_path and os.path.exists(db_path):
        rep.add(area, 'size', INFO,
                f'{os.path.getsize(db_path) / 1_048_576:.1f} MB')


# ── ops ──────────────────────────────────────────────────────────────
def _one(conn, sql):
    try:
        return conn.execute(text(sql)).scalar()
    except Exception:
        return None


def backup_files(folder):
    """Every file in ``folder`` that looks like a database backup — the
    script's own procam_crm-*.db and the hand-made .bak copies."""
    if not os.path.isdir(folder):
        return []
    return [os.path.join(folder, f) for f in os.listdir(folder)
            if f.endswith(('.db', '.sqlite', '.bak')) or '.db.' in f]


def newest_backup(folder):
    files = backup_files(folder)
    return max(files, key=os.path.getmtime) if files else None


def subscription_id_file(root=None):
    return _env('LEADS_SUBSCRIPTION_ID_FILE') or os.path.join(
        root or _ROOT, '.leads_subscription_id')


def copilot_coverage(conn):
    """(leads, indexed leads or None when the table is missing, leads
    with an enquiry to index)."""
    leads = _one(conn, 'SELECT COUNT(*) FROM leads') or 0
    indexed = _one(conn, 'SELECT COUNT(DISTINCT lead_id) FROM copilot_chunk')
    with_text = _one(conn, "SELECT COUNT(*) FROM leads WHERE "
                           "COALESCE(original_email_body, '') != ''")
    return leads, indexed, with_text


def check_ops(rep, conn, db_path):
    area = 'ops'
    backups = os.path.join(_ROOT, 'backups')
    newest = newest_backup(backups)
    if not newest:
        rep.add(area, 'backups', FAIL, f'no database backup in {backups}')
    else:
        age_h = (time.time() - os.path.getmtime(newest)) / 3600
        small = (db_path and os.path.exists(db_path) and
                 os.path.getsize(newest) < 0.5 * os.path.getsize(db_path))
        status = WARN if age_h > 26 or small else PASS
        rep.add(area, 'backups', status,
                f'newest {os.path.basename(newest)}, {age_h:.0f} h old'
                + (' — under half the live size, check it' if small else ''))

    target = os.path.dirname(db_path) if db_path else _ROOT
    free = shutil.disk_usage(target).free / 1_073_741_824
    rep.add(area, 'disk free', FAIL if free < 1 else WARN if free < 5
            else PASS, f'{free:.1f} GB on the database volume')

    sub = subscription_id_file()
    if os.path.exists(sub):
        age_d = (time.time() - os.path.getmtime(sub)) / 86400
        rep.add(area, 'Graph subscription', WARN if age_d > 2.5 else PASS,
                f'last created/renewed {age_d:.1f} days ago — subscriptions '
                f'expire after ~2.9 days' + (', renewal is probably not '
                                            'scheduled' if age_d > 2.5
                                            else ''))
    else:
        rep.add(area, 'Graph subscription', WARN, 'no subscription id file '
                '— real-time ingest is not subscribed from this host')

    leads, indexed, with_text = copilot_coverage(conn)
    if indexed is None:
        rep.add(area, 'Copilot index', WARN, 'copilot_chunk table missing')
    else:
        rep.add(area, 'Copilot index', WARN if with_text and
                indexed < 0.8 * with_text else PASS,
                f'{indexed} leads indexed of {with_text} with an enquiry '
                f'({leads} leads total)')

    for label, sql in (
            ('copilot_log rows', 'SELECT COUNT(*) FROM copilot_log'),
            ('email_events rows', 'SELECT COUNT(*) FROM email_events'),
            ('email_classifications rows',
             'SELECT COUNT(*) FROM email_classifications'),
            ('deletion_audit rows', 'SELECT COUNT(*) FROM deletion_audit')):
        n = _one(conn, sql)
        rep.add(area, label, INFO, 'table missing' if n is None else
                f'{n} — no retention job; retention period is a policy '
                f'decision')


def run(env_path=None, db_url=None, *, schema=True):
    env_path = env_path or os.path.join(_ROOT, '.env')
    rep = Report()
    check_config(rep, env_path)
    url = db_url or _env('DATABASE_URL') or (
        'sqlite:///' + os.path.join(_ROOT, 'procam_crm.db'))
    db_path = url[len('sqlite:///'):] if url.startswith('sqlite:///') else ''
    if db_path and not os.path.exists(db_path):
        rep.add('database', 'open', FAIL, f'{db_path} not found')
        return rep
    with read_only_engine(url).connect() as conn:
        check_database(rep, conn, db_path)
        if schema:
            try:
                check_schema(rep, conn, expected_schema())
            except Exception as exc:
                rep.add('schema', 'expected schema', FAIL, str(exc)[:300])
        check_ops(rep, conn, db_path)
    return rep


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--no-schema', action='store_true',
                    help='skip the schema comparison (it imports the app '
                         'in a child process against an in-memory database)')
    args = ap.parse_args()
    rep = run(schema=not args.no_schema)
    if args.json:
        print(json.dumps(rep.rows, indent=1))
    else:
        print(f'\n  Procam CRM preflight — {datetime.now():%Y-%m-%d %H:%M}')
        area = None
        for r in rep.rows:
            if r['area'] != area:
                area = r['area']
                print(f'\n  {area.upper()}')
            print(f'    {r["status"]:<5} {r["check"]:<28} {r["detail"]}')
        counts = {s: sum(r['status'] == s for r in rep.rows)
                  for s in (PASS, WARN, FAIL, INFO)}
        print(f'\n  {counts[PASS]} pass · {counts[WARN]} warn · '
              f'{counts[FAIL]} fail · {counts[INFO]} info')
    return 1 if rep.failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
