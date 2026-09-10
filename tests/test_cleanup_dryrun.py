"""Imported-data cleanup dry run — WP3.

The report must be conservative: anything referenced, worked, or carrying
a customer's own words is kept. And it must never write — these tests
hold both.
"""
import os
import re
import subprocess
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT = os.path.join(_ROOT, 'scripts', '2026_09_20_cleanup_dryrun.py')
sys.path.insert(0, _ROOT)


@pytest.fixture(scope='module')
def report():
    """Build a database with one lead per keep-reason, and report on it."""
    tmp = tempfile.mkdtemp()
    db_path = os.path.join(tmp, 'cleanup.db')
    env = dict(os.environ,
               DATABASE_URL='sqlite:///' + db_path,
               SECRET_KEY='test',
               ADMIN_INITIAL_PASSWORD='CleanupTestOnly12345')

    build = '''
import sys; sys.path.insert(0, %r)
from app import app as fa, db, Lead, LeadActivity, Opportunity
import app.models, app.models.audit
from app.models.task_engine import TaskInstance
from app.models.notification import Notification
with fa.app_context():
    db.create_all()
    def L(**kw):
        l = Lead(**kw); db.session.add(l); db.session.flush(); return l
    for i in range(4):
        L(company='Junk Import %%d' %% i, source='import',
          stage='New Opportunity')
    L(company='No Source Junk', source='', stage='New Opportunity')
    a = L(company='Has Activity Ltd', source='import', stage='New Opportunity')
    db.session.add(LeadActivity(lead_id=a.id, kind='call', subject='rang'))
    t = L(company='Has Open Task Ltd', source='import',
          stage='New Opportunity')
    db.session.add(TaskInstance(task_key='lead.qualify', entity_type='Lead',
                                entity_id=t.id, owner_user_id='X',
                                status='Pending', priority=2))
    o = L(company='Has Opportunity Ltd', source='import',
          stage='New Opportunity')
    db.session.add(Opportunity(lead_id=o.id, stage='RFQ',
                               opp_number='OPP-0001'))
    L(company='Real Enquiry Ltd', source='email', stage='New Opportunity',
      email_message_id='<abc@customer.com>')
    L(company='Owned Ltd', source='import', stage='New Opportunity',
      assigned_to='DIR12010')
    L(company='Has Notes Ltd', source='import', stage='New Opportunity',
      notes='met at expo')
    L(company='Progressed Ltd', source='import', stage='Qualified')
    L(company='Manual Entry Ltd', source='manual', stage='New Opportunity')
    n = L(company='Only A Notification Ltd', source='import',
          stage='New Opportunity')
    db.session.add(Notification(user_id='PCM001', kind='lead_assigned',
                                title='x', entity_type='Lead',
                                entity_id=n.id))
    db.session.commit()
''' % _ROOT
    subprocess.run([sys.executable, '-c', build], env=env, check=True,
                   capture_output=True)

    before = os.path.getsize(db_path)
    out_dir = os.path.join(tmp, 'report')
    proc = subprocess.run(
        [sys.executable, _SCRIPT, '--out', out_dir],
        env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return {'stdout': proc.stdout, 'out_dir': out_dir,
            'db_path': db_path, 'size_before': before}


def _csv_rows(report):
    import csv
    path = os.path.join(report['out_dir'], 'cleanup_candidates_leads.csv')
    with open(path) as fh:
        return list(csv.DictReader(fh))


# ─── it must not write ───────────────────────────────────────────────────
def test_the_script_has_no_apply_mode():
    """A dry run with an --apply flag is one keystroke from a deletion."""
    src = open(_SCRIPT).read()
    for banned in ('--apply', '--force', '--yes', '--delete'):
        assert banned not in src, f'{banned} must not exist in a dry run'
    for sql in ('DELETE FROM', 'DROP TABLE', 'UPDATE ', 'INSERT INTO'):
        assert sql not in src.upper().replace('INSERT INTO A ', ''), \
            f'the dry run contains {sql}'


def test_nothing_in_the_database_changed(report):
    assert os.path.getsize(report['db_path']) == report['size_before']


def test_the_report_says_plainly_that_nothing_changed(report):
    assert 'NOTHING HAS BEEN CHANGED' in report['stdout']


# ─── the candidates ──────────────────────────────────────────────────────
def test_only_unreferenced_import_junk_is_a_candidate(report):
    names = {r['company'] for r in _csv_rows(report)}
    assert names == {'Junk Import 0', 'Junk Import 1', 'Junk Import 2',
                     'Junk Import 3', 'No Source Junk'}, names


@pytest.mark.parametrize('company,why', [
    ('Has Activity Ltd',    'a logged activity'),
    ('Has Open Task Ltd',   'an open task'),
    ('Has Opportunity Ltd', 'a child opportunity'),
    ('Real Enquiry Ltd',    'an inbound customer email'),
    ('Owned Ltd',           'an owner'),
    ('Has Notes Ltd',       "someone's notes"),
    ('Progressed Ltd',      'a stage it was moved to'),
    ('Manual Entry Ltd',    'a person who typed it in'),
])
def test_a_lead_with_something_behind_it_is_kept(report, company, why):
    names = {r['company'] for r in _csv_rows(report)}
    assert company not in names, f'{company} has {why} and must be kept'


def test_a_lead_referenced_without_a_foreign_key_is_kept(report):
    """task_instances and notifications address a lead by
    entity_type/entity_id and declare no foreign key. Missing them is how
    a "safe" cleanup orphans somebody's open task.

    The check is made against a notification rather than a task: an open
    task is also caught by its own count, so it would pass even if the
    soft-reference scan were removed entirely.
    """
    names = {r['company'] for r in _csv_rows(report)}
    assert 'Only A Notification Ltd' not in names, \
        'a lead referenced only by a notification was offered for deletion'
    assert 'Has Open Task Ltd' not in names


def test_no_candidate_has_a_pending_task(report):
    """The number the brief asks for by name; a non-zero value here is a
    contradiction in the criteria, not a finding."""
    assert all(int(r['open_tasks'] or 0) == 0 for r in _csv_rows(report))
    m = re.search(r'pending tasks on candidates:\s*([\d,]+)',
                  report['stdout'])
    assert m and m.group(1).replace(',', '') == '0'


# ─── the report itself ───────────────────────────────────────────────────
def test_the_schema_map_lists_every_referencing_table(report):
    out = report['stdout']
    for table in ('lead_activities.lead_id', 'opportunities.lead_id',
                  'quotes.lead_id', 'rfqs.lead_id',
                  'lead_attachments.lead_id', 'lead_stage_history.lead_id'):
        assert table in out, f'{table} missing from the schema map'
    assert "task_instances.entity_id where entity_type = 'Lead'" in out


def test_every_kept_lead_has_a_stated_reason(report):
    out = report['stdout']
    assert 'WHY LEADS WERE KEPT' in out
    for reason in ('referenced by another table',
                   'created from an inbound customer email',
                   'has an owner', 'source=manual'):
        assert reason in out


def test_the_csv_carries_the_id_and_the_reason(report):
    rows = _csv_rows(report)
    assert rows
    for r in rows:
        assert r['id'].isdigit()
        assert r['reason']
        assert 'open_tasks' in r


def test_the_totals_reconcile(report):
    out = report['stdout']
    total = int(re.search(r'leads in database:\s+([\d,]+)',
                          out).group(1).replace(',', ''))
    cands = int(re.search(r'cleanup candidates:\s+([\d,]+)',
                          out).group(1).replace(',', ''))
    kept = int(re.search(r'kept:\s+([\d,]+)', out).group(1).replace(',', ''))
    assert cands + kept == total
    assert cands == len(_csv_rows(report))


def test_it_refuses_a_database_that_is_not_production():
    """The report is meaningless against an empty database, and running
    it there would suggest everything is junk."""
    tmp = tempfile.mkdtemp()
    env = dict(os.environ,
               DATABASE_URL='sqlite:///' + os.path.join(tmp, 'empty.db'))
    proc = subprocess.run([sys.executable, _SCRIPT], env=env,
                          capture_output=True, text=True)
    assert proc.returncode != 0
    assert 'not the production one' in (proc.stdout + proc.stderr)
