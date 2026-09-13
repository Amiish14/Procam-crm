"""
Performance benchmark. Never runs against the live database.

    # seeded, production-scale scratch data (the default)
    .venv/bin/python scripts/perf_benchmark.py

    # a COPY of a production backup, on a spare port
    cp backups/procam_crm.db.bak-XXXX /tmp/perf.db
    .venv/bin/python scripts/perf_benchmark.py --db /tmp/perf.db --port 8099

Starts its own gunicorn (production's settings: 2 sync workers, 120 s
timeout) on 127.0.0.1, measures, stops it. Refuses a --db that is the
DATABASE_URL in .env, so it cannot load-test production by accident.

Measures
    boot          process start to first healthy response (includes the
                  boot autoheal)
    cold / warm   first request to each endpoint after boot, then the
                  median and p95 of 20 sequential requests
    concurrency   100 and 500 simultaneous clients, each making requests
                  for a fixed window: throughput, p50/p95/p99, failures
    large cases   a 2,000-lead account's 360, a 500-email thread, a RAG
                  search over every chunk, a 15 MB attachment download and
                  an over-limit (21 MB) upload

Writes JSON to --out and prints a table.
"""
import argparse
import json
import os
import random
import signal
import socket
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

try:
    import requests
except ImportError:                                   # pragma: no cover
    raise SystemExit('requests is required (it is in requirements.txt)')

PASSWORD = 'PerfBench-Only-2026'
WORDS = ('crane hydraulic axle transformer ODC demurrage Kandla Mundra '
         'barge project cargo lashing survey route permit trailer '
         'breakbulk charter vessel customs clearance warehouse turbine '
         'reactor boiler heat exchanger jetty RoRo lift plan quote rate '
         'please share best offer delivery schedule site Airoli Pune').split()


def _text(rng, n):
    return ' '.join(rng.choice(WORDS) for _ in range(n))


# ── seeding ──────────────────────────────────────────────────────────
_SCHEMA = r'''
import os, sys, io, contextlib
with contextlib.redirect_stdout(io.StringIO()):
    import app as A
    import app.models
    from app.models import copilot, audit, business_card, tms_handover
    from app.access.service import set_profile
    from app.models.access import DataScope
    with A.app.app_context():
        A.db.create_all()
        for code, role, sup in (("PERFADM", "admin", True),
                                ("PERFREP", "user", False)):
            e = A.Employee.query.filter_by(emp_code=code).first()
            if e is None:
                e = A.Employee(emp_code=code, name=code)
                A.db.session.add(e)
            e.role, e.is_active, e.must_change_pw = role, True, False
            e.vertical, e.is_super_admin = "All", sup
            e.set_password(os.environ["PERF_PASSWORD"])
        A.db.session.commit()
        set_profile("PERFREP", DataScope.OWN, [], actor="PERFADM")
        A.db.session.commit()
print("ok")
'''


def seed(db_path, leads=10_000, companies=2_000, big_account=2_000,
         thread=500, seed_value=7):
    env = _env(db_path)
    env['PERF_PASSWORD'] = PASSWORD
    subprocess.run([sys.executable, '-c', _SCHEMA], cwd=_ROOT, env=env,
                   check=True, capture_output=True, timeout=300)
    rng = random.Random(seed_value)
    db = sqlite3.connect(db_path)
    x = db.executemany
    codes = [r[0] for r in db.execute(
        "SELECT emp_code FROM employees WHERE is_active = 1")]
    now = datetime.utcnow()

    x('INSERT INTO companies (id, name, pic_emp_code, vertical, is_active) '
      'VALUES (?,?,?,?,1)',
      [(i, f'Perf Account {i}', rng.choice(codes + [None]),
        rng.choice(['Project Freight', 'Transportation', 'Warehousing']))
       for i in range(1, companies + 1)])

    stages = ['New', 'Call Done', 'Profile Sent', 'Meeting', 'Quoted',
              'Under Negotiation', 'Won', 'Lost']
    rows = []
    for i in range(1, leads + 1):
        cid = 1 if i <= big_account else rng.randint(2, companies)
        owner = 'PERFREP' if i % 20 == 0 else (
            None if i % 2 else rng.choice(codes))
        created = now - timedelta(days=rng.randint(0, 720))
        rows.append((i, f'Perf Account {cid}', cid, owner, rng.choice(stages),
                     'email', _text(rng, 250), f'RFQ {_text(rng, 5)}',
                     'Project Freight', created, created))
    x('INSERT INTO leads (id, company, company_id, assigned_to, stage, source,'
      ' original_email_body, original_email_subject, procam_vertical,'
      ' created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)', rows)

    thread_lead = big_account + 1
    emails = []
    for i in range(1, leads + 1):
        for k in range(2):
            emails.append((i, 'inbound' if k == 0 else 'outbound',
                           _text(rng, 6), _text(rng, 120), 'received',
                           now - timedelta(days=rng.randint(0, 720))))
    for k in range(thread):
        emails.append((thread_lead, 'inbound' if k % 2 else 'outbound',
                       f'RE: thread {k}', _text(rng, 180), 'received',
                       now - timedelta(hours=thread - k)))
    x('INSERT INTO lead_emails (lead_id, direction, subject, body, status,'
      ' sent_or_received_at) VALUES (?,?,?,?,?,?)', emails)
    x('INSERT INTO lead_notes (lead_id, note_text, author) VALUES (?,?,?)',
      [(rng.randint(1, leads), _text(rng, 40), rng.choice(codes))
       for _ in range(leads // 2)])
    x('INSERT INTO lead_activities (lead_id, kind, subject, created_at) '
      'VALUES (?,?,?,?)',
      [(rng.randint(1, leads), 'call', _text(rng, 5),
        now - timedelta(days=rng.randint(0, 60)))
       for _ in range(leads // 2)])
    x('INSERT INTO opportunities (opp_number, lead_id, company_id, stage,'
      ' value_inr, owner_emp_code, expected_close_date) '
      'VALUES (?,?,?,?,?,?,?)',
      [(f'PERF-{i}', i, 1 if i <= big_account else rng.randint(2, companies),
        rng.choice(['Qualification', 'Proposal', 'Won', 'Lost']),
        rng.randint(1, 500) * 100_000, rng.choice(codes),
        (now + timedelta(days=rng.randint(-120, 120))).date())
       for i in range(1, leads // 6)])

    folder = os.path.join(os.path.dirname(db_path), 'attachments')
    os.makedirs(folder, exist_ok=True)
    big = os.path.join(folder, 'drawing-15mb.pdf')
    with open(big, 'wb') as fh:
        fh.write(os.urandom(15 * 1024 * 1024))
    cur = db.execute('INSERT INTO lead_attachments (lead_id, filename, '
                     'storage_path) VALUES (?,?,?)',
                     (thread_lead, 'drawing-15mb.pdf', big))
    db.commit()
    attachment_id = cur.lastrowid
    db.close()

    t0 = time.time()
    subprocess.run([sys.executable, 'scripts/build_copilot_index.py'],
                   cwd=_ROOT, env=env, check=True, capture_output=True,
                   timeout=3600)
    return {'leads': leads, 'companies': companies,
            'emails': len(emails), 'big_account_id': 1,
            'thread_lead_id': thread_lead, 'attachment_id': attachment_id,
            'index_build_s': round(time.time() - t0, 1)}


def largest_cases(db_path):
    """On real data: the account with most leads, the lead with most
    emails, the largest attachment still on disk."""
    db = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    try:
        acct = db.execute('SELECT company_id FROM leads WHERE company_id IS '
                          'NOT NULL GROUP BY company_id ORDER BY COUNT(*) '
                          'DESC LIMIT 1').fetchone()
        thread = db.execute('SELECT lead_id FROM lead_emails GROUP BY '
                            'lead_id ORDER BY COUNT(*) DESC LIMIT 1').fetchone()
        att = None
        for aid, lid, path in db.execute(
                'SELECT id, lead_id, storage_path FROM lead_attachments '
                'ORDER BY id DESC LIMIT 500'):
            if path and os.path.exists(path):
                size = os.path.getsize(path)
                if att is None or size > att[2]:
                    att = (aid, lid, size)
    finally:
        db.close()
    return {'big_account_id': acct[0] if acct else 1,
            'thread_lead_id': (att[1] if att else
                               thread[0] if thread else 1),
            'email_thread_lead_id': thread[0] if thread else 1,
            'attachment_id': att[0] if att else 1}


# ── the server ───────────────────────────────────────────────────────
def _env(db_path):
    env = dict(os.environ)
    env.update(DATABASE_URL='sqlite:///' + db_path, URL_PREFIX='',
               SESSION_COOKIE_SECURE='false', LOG_LEVEL='WARNING',
               SECRET_KEY='perf-benchmark-' + 'k' * 40,
               ADMIN_INITIAL_PASSWORD='PerfBenchAdmin-123456',
               PROCAM_AI_BASE_URL='', PROCAM_AI_EMBED_URL='',
               PROCAM_AI_ACTIONS='off', LEAD_INTAKE_AI='off',
               NOTIFY_ENABLED='false', ANTHROPIC_API_KEY='', GROQ_API_KEY='')
    return env


def _free(port):
    with socket.socket() as s:
        return s.connect_ex(('127.0.0.1', port)) != 0


def start_server(db_path, port, workers=2, threads=1):
    if not _free(port):
        raise SystemExit(f'port {port} is in use')
    t0 = time.time()
    proc = subprocess.Popen(
        [os.path.join(os.path.dirname(sys.executable), 'gunicorn'),
         'app:app', '--workers', str(workers), '--timeout', '120',
         *(['--worker-class', 'gthread', '--threads', str(threads)]
           if threads > 1 else []),
         '--bind', f'127.0.0.1:{port}', '--log-level', 'warning'],
        cwd=_ROOT, env=_env(db_path), stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE, preexec_fn=os.setsid)
    base = f'http://127.0.0.1:{port}'
    while time.time() - t0 < 180:
        try:
            if requests.get(base + '/healthz', timeout=2).status_code == 200:
                return proc, base, round(time.time() - t0, 2)
        except requests.RequestException:
            pass
        if proc.poll() is not None:
            raise SystemExit('gunicorn exited: '
                             + proc.stderr.read().decode()[-800:])
        time.sleep(0.2)
    stop_server(proc)
    raise SystemExit('gunicorn did not become healthy in 180 s')


def stop_server(proc):
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=20)
    except Exception:
        pass


class Client:
    """Cookies carried by hand: the session cookie is Secure, and requests
    will not send a Secure cookie over plain http."""

    def __init__(self, base, code):
        self.base = base
        r = requests.get(base + '/login', timeout=30)
        self.cookies = {c.name: c.value for c in r.cookies}
        self.token = self.cookies.get('csrf_token', '')
        r = self.post('/login', {'emp_code': code, 'password': PASSWORD})
        if not (r.ok and r.json().get('ok')):
            raise SystemExit(f'login failed for {code}: {r.text[:200]}')

    def _headers(self):
        return {'Cookie': '; '.join(f'{k}={v}' for k, v in
                                    self.cookies.items()),
                'X-CSRFToken': self.token}

    def _keep(self, r):
        for c in r.cookies:
            self.cookies[c.name] = c.value
        return r

    def get(self, path, **kw):
        return self._keep(requests.get(self.base + path,
                                       headers=self._headers(),
                                       timeout=kw.pop('timeout', 120), **kw))

    def post(self, path, body=None, **kw):
        return self._keep(requests.post(self.base + path, json=body,
                                        headers=self._headers(),
                                        timeout=kw.pop('timeout', 120), **kw))


# ── measuring ────────────────────────────────────────────────────────
def _pct(values, p):
    if not values:
        return None
    values = sorted(values)
    k = max(0, min(len(values) - 1, int(round(p / 100 * len(values))) - 1))
    return round(values[k] * 1000, 1)


def timed(fn):
    t0 = time.perf_counter()
    r = fn()
    return time.perf_counter() - t0, r


def endpoints(ids):
    return [
        ('lead list (500)', 'get', '/api/leads?limit=500', None),
        ('dashboard summary', 'get', '/api/dashboard/summary', None),
        ('my work', 'get', '/api/my-work', None),
        ('copilot: my open leads', 'post', '/api/copilot/ask',
         {'q': 'my open leads'}),
        ('copilot: RAG search', 'post', '/api/copilot/ask',
         {'q': 'search the emails for hydraulic axle'}),
        ('company 360 (2,000 leads)', 'get',
         f'/api/companies/{ids["big_account_id"]}/360', None),
        ('email thread (500)', 'get',
         f'/api/leads/{ids.get("email_thread_lead_id", ids["thread_lead_id"])}'
         '/emails', None),
    ]


def cold_and_warm(client, eps, n=20):
    out = []
    for name, method, path, body in eps:
        call = (lambda: client.get(path)) if method == 'get' else \
               (lambda: client.post(path, body))
        cold, r = timed(call)
        warm = []
        for _ in range(n):
            t, _r = timed(call)
            warm.append(t)
        out.append({'endpoint': name, 'status': r.status_code,
                    'bytes': len(r.content),
                    'cold_ms': round(cold * 1000, 1),
                    'warm_p50_ms': _pct(warm, 50),
                    'warm_p95_ms': _pct(warm, 95)})
    return out


def concurrency(client, eps, users, seconds):
    """`users` simultaneous clients, each looping over the mix for
    `seconds`. Shares one session: the question is server capacity, and
    2 sync workers serve one request each whoever sends it."""
    stop = time.time() + seconds
    lat, fails, lock = [], [0], threading.Lock()
    kinds = {}
    mix = [e for e in eps if e[0] in ('lead list (500)', 'my work',
                                      'copilot: my open leads',
                                      'dashboard summary')]

    def user(i):
        rng = random.Random(i)
        while time.time() < stop:
            name, method, path, body = rng.choice(mix)
            try:
                t, r = timed((lambda: client.get(path, timeout=60))
                             if method == 'get' else
                             (lambda: client.post(path, body, timeout=60)))
                ok = r.status_code < 500
                kind = f'HTTP {r.status_code}'
            except requests.RequestException as exc:
                t, ok, kind = 60.0, False, type(exc).__name__
            with lock:
                if ok:
                    lat.append(t)
                else:
                    fails[0] += 1
                    kinds[kind] = kinds.get(kind, 0) + 1

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=users) as pool:
        list(pool.map(user, range(users)))
    wall = time.time() - t0
    done = len(lat)
    return {'users': users, 'seconds': round(wall, 1), 'completed': done,
            'failed': fails[0], 'failure_kinds': kinds,
            'req_per_s': round(done / wall, 1),
            'p50_ms': _pct(lat, 50), 'p95_ms': _pct(lat, 95),
            'p99_ms': _pct(lat, 99)}


def large_cases(client, ids):
    out = {}
    t, r = timed(lambda: client.get(
        f'/api/leads/{ids["thread_lead_id"]}/attachments/'
        f'{ids["attachment_id"]}/download'))
    out['attachment_download_15mb'] = {
        'status': r.status_code, 'mb': round(len(r.content) / 1_048_576, 1),
        'ms': round(t * 1000, 1)}
    payload = os.urandom(21 * 1024 * 1024)
    t, r = timed(lambda: requests.post(
        client.base + '/api/rfqs/1/attachments', headers=client._headers(),
        files={'file': ('too-big.pdf', payload)}, timeout=120))
    out['upload_21mb_over_limit'] = {'status': r.status_code,
                                     'ms': round(t * 1000, 1),
                                     'expected': 413}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', help='a COPY of a backup; default seeds one')
    ap.add_argument('--port', type=int, default=8099)
    ap.add_argument('--leads', type=int, default=10_000)
    ap.add_argument('--seconds', type=int, default=20)
    ap.add_argument('--users', type=int, nargs='*', default=[100, 500])
    ap.add_argument('--workers', type=int, default=2)
    ap.add_argument('--threads', type=int, default=1,
                    help='>1 uses gunicorn gthread workers')
    ap.add_argument('--only-concurrency', action='store_true')
    ap.add_argument('--out', default=os.path.join(
        tempfile.gettempdir(), 'procam_perf.json'))
    args = ap.parse_args()

    live = (os.environ.get('DATABASE_URL') or '').replace('sqlite:///', '')
    try:
        from dotenv import dotenv_values
        live = (dotenv_values(os.path.join(_ROOT, '.env'))
                .get('DATABASE_URL') or live).replace('sqlite:///', '')
    except ImportError:
        pass

    result = {'when': datetime.now().isoformat(timespec='seconds'),
              'host': socket.gethostname(), 'cpus': os.cpu_count()}
    if args.db:
        if live and os.path.realpath(args.db) == os.path.realpath(live):
            raise SystemExit('refusing to benchmark the live database — '
                             'copy a backup first')
        db_path = args.db
        # It is a copy, so the benchmark's two users can be added to it.
        env = _env(db_path)
        env['PERF_PASSWORD'] = PASSWORD
        subprocess.run([sys.executable, '-c', _SCHEMA], cwd=_ROOT, env=env,
                       check=True, capture_output=True, timeout=300)
        ids = json.loads(os.environ.get('PERF_IDS', 'null')) or \
            largest_cases(db_path)
        result['data'] = {'source': 'copy of ' + db_path, **ids}
    else:
        folder = tempfile.mkdtemp(prefix='procam-perf-')
        db_path = os.path.join(folder, 'perf.db')
        print(f'  seeding {args.leads} leads into {db_path} …', flush=True)
        ids = seed(db_path, leads=args.leads,
                   big_account=min(2000, args.leads // 5))
        result['data'] = ids

    proc, base, boot = start_server(db_path, args.port, args.workers,
                                    args.threads)
    result['server'] = {'workers': args.workers, 'threads': args.threads}
    result['boot_to_healthy_s'] = boot
    try:
        admin = Client(base, 'PERFADM')
        eps = endpoints(ids)
        if args.only_concurrency:
            result['concurrency'] = [concurrency(admin, eps, u, args.seconds)
                                     for u in args.users]
            return _finish(result, args.out)
        result['cold_warm_admin'] = cold_and_warm(admin, eps)
        rep = Client(base, 'PERFREP')
        result['cold_warm_scoped_rep'] = cold_and_warm(
            rep, [e for e in eps if 'company 360' not in e[0]
                  and 'thread' not in e[0]], n=10)
        result['concurrency'] = [concurrency(admin, eps, u, args.seconds)
                                 for u in args.users]
        result['large'] = large_cases(admin, ids)
    finally:
        stop_server(proc)

    return _finish(result, args.out)


def _finish(result, out):
    with open(out, 'w') as fh:
        json.dump(result, fh, indent=1, default=str)
    print(json.dumps(result, indent=1, default=str))
    print(f'\n  written to {out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
