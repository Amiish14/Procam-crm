"""
A restart before a migration must not take the CRM down.

A model column the database lacks breaks every query on that table. The
final audit added three — leads.vertical_confidence, leads.vertical_reason
and lead_notes.revisions — and a deploy that restarts the service before
running their scripts would have broken the lead list and the notes panel
until someone noticed. init_db's autoheal adds them at boot. This proves
it, against a database built WITHOUT them.
"""
import os
import sqlite3
import subprocess
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_boot_adds_the_columns_a_stale_database_is_missing():
    path = os.path.join(tempfile.mkdtemp(), 'stale.db')
    # First boot builds the full schema, then the three columns are
    # dropped to simulate a production database that predates them.
    env = dict(os.environ, DATABASE_URL='sqlite:///' + path,
               SECRET_KEY='test', ADMIN_INITIAL_PASSWORD='BootTestOnly12345',
               SESSION_COOKIE_SECURE='false')
    boot = [sys.executable, '-c', 'import app']
    subprocess.run(boot, cwd=_ROOT, env=env, check=True,
                   capture_output=True, timeout=120)

    db = sqlite3.connect(path)
    for table, col in (('leads', 'vertical_confidence'),
                       ('leads', 'vertical_reason'),
                       ('lead_notes', 'revisions')):
        db.execute(f'ALTER TABLE {table} DROP COLUMN {col}')
    db.commit()
    cols = {r[1] for r in db.execute('PRAGMA table_info(leads)')}
    assert 'vertical_confidence' not in cols
    db.close()

    # Second boot: the autoheal must put them back.
    subprocess.run(boot, cwd=_ROOT, env=env, check=True,
                   capture_output=True, timeout=120)
    db = sqlite3.connect(path)
    leads = {r[1] for r in db.execute('PRAGMA table_info(leads)')}
    notes = {r[1] for r in db.execute('PRAGMA table_info(lead_notes)')}
    db.close()
    assert {'vertical_confidence', 'vertical_reason'} <= leads
    assert 'revisions' in notes


def test_a_table_not_created_yet_is_not_reported_as_a_failure():
    """copilot_log is created by its migration, not by create_all, so a
    database that has not run that migration has no table to heal. The
    boot used to log "autoheal FAILED copilot_log.answer" there — a false
    alarm on a healthy database, and the kind that teaches people to
    ignore the real ones."""
    path = os.path.join(tempfile.mkdtemp(), 'fresh.db')
    env = dict(os.environ, DATABASE_URL='sqlite:///' + path,
               SECRET_KEY='test', ADMIN_INITIAL_PASSWORD='BootTestOnly12345',
               SESSION_COOKIE_SECURE='false')
    boot = [sys.executable, '-c', 'import app']
    subprocess.run(boot, cwd=_ROOT, env=env, check=True,
                   capture_output=True, timeout=120)
    db = sqlite3.connect(path)
    db.execute('DROP TABLE IF EXISTS copilot_log')
    db.commit()
    db.close()
    out = subprocess.run(boot, cwd=_ROOT, env=env, check=True,
                         capture_output=True, text=True, timeout=120)
    assert 'autoheal FAILED' not in out.stdout + out.stderr, out.stderr[-500:]


def test_boot_builds_the_indexes_an_existing_database_lacks():
    """create_all never adds an index to a table that already exists, so
    production would never get them. The boot does."""
    path = os.path.join(tempfile.mkdtemp(), 'idx.db')
    env = dict(os.environ, DATABASE_URL='sqlite:///' + path,
               SECRET_KEY='test', ADMIN_INITIAL_PASSWORD='BootTestOnly12345',
               SESSION_COOKIE_SECURE='false')
    boot = [sys.executable, '-c', 'import app']
    subprocess.run(boot, cwd=_ROOT, env=env, check=True,
                   capture_output=True, timeout=120)
    db = sqlite3.connect(path)
    db.execute('DROP INDEX IF EXISTS ix_leads_assigned_to')
    db.commit()
    db.close()
    subprocess.run(boot, cwd=_ROOT, env=env, check=True,
                   capture_output=True, timeout=120)
    db = sqlite3.connect(path)
    plan = ' '.join(r[3] for r in db.execute(
        "EXPLAIN QUERY PLAN SELECT id FROM leads WHERE assigned_to = 'X'"))
    db.close()
    assert 'ix_leads_assigned_to' in plan


def test_boot_index_names_match_the_models():
    src = subprocess.run(
        [sys.executable, '-c',
         'import app as A\n'
         'names = {i.name for t in A.db.metadata.sorted_tables '
         'for i in t.indexes}\n'
         'missing = [n for n, _t, _c in A.BOOT_INDEXES if n not in names]\n'
         'print("MISSING", missing)'],
        cwd=_ROOT, capture_output=True, text=True, timeout=120,
        env=dict(os.environ, DATABASE_URL='sqlite://', SECRET_KEY='t',
                 ADMIN_INITIAL_PASSWORD='BootTestOnly12345'))
    assert 'MISSING []' in src.stdout, src.stdout[-300:] + src.stderr[-300:]


def test_seeded_employees_have_no_guessable_password_and_pcm001_opens_the_install():
    folder = tempfile.mkdtemp()
    seed = os.path.join(folder, 'seed.csv')
    with open(seed, 'w') as fh:
        fh.write('emp_code,name,email,department,designation,vertical,role\n'
                 'SEEDADM1,Seed Administrator,,Corporate,Director,All,admin\n'
                 'SEEDUSR1,Seed User,,Sales,Executive,All,user\n')
    path = os.path.join(folder, 'seeded.db')
    env = dict(os.environ, DATABASE_URL='sqlite:///' + path,
               SECRET_KEY='test', ADMIN_INITIAL_PASSWORD='BootSeedTest-12345',
               SESSION_COOKIE_SECURE='false', SEED_EMPLOYEES_CSV=seed)
    check = (
        "import app as A\n"
        "with A.app.app_context():\n"
        "    E = A.Employee\n"
        "    adm = E.query.filter_by(emp_code='SEEDADM1').first()\n"
        "    usr = E.query.filter_by(emp_code='SEEDUSR1').first()\n"
        "    pcm = E.query.filter_by(emp_code='PCM001').first()\n"
        "    print('RESULT', adm is not None, usr is not None,\n"
        "          adm.check_password('seedadm1'), adm.check_password('SEEDADM1'),\n"
        "          adm.must_change_pw, pcm is not None,\n"
        "          pcm is not None and pcm.check_password('BootSeedTest-12345'))\n")
    out = subprocess.run([sys.executable, '-c', check], cwd=_ROOT, env=env,
                         capture_output=True, text=True, timeout=180)
    line = [l for l in out.stdout.splitlines() if l.startswith('RESULT')]
    assert line, out.stderr[-600:]
    assert line[0] == 'RESULT True True False False True True True', line[0]
