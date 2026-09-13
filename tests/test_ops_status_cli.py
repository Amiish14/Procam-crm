"""
scripts/ops_status.py is safe to put on a timer and in a cron alert:
the exit code means FAIL and nothing else, the status file is replaced
whole, and it never boots the application.
"""
import json
import os
import sqlite3
import subprocess
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, 'scripts'))

import ops_status                                               # noqa: E402

C = ops_status.load_checks()


@pytest.fixture()
def paths(tmp_path):
    db = tmp_path / 'live.db'
    conn = sqlite3.connect(db)
    conn.execute('CREATE TABLE leads (id INTEGER PRIMARY KEY)')
    conn.commit()
    conn.close()
    (tmp_path / 'backups').mkdir()
    return tmp_path, str(db)


def _fake(statuses, monkeypatch):
    checks = [(f'k{i}', (lambda s: lambda ctx: C.result('k', 'k', s))(s))
              for i, s in enumerate(statuses)]
    monkeypatch.setattr(C, 'CHECKS', checks)
    monkeypatch.setattr(C, 'CHECK_KEYS', [k for k, _f in checks])


@pytest.mark.parametrize('statuses,code', [
    ([C.OK, C.WARN, C.UNKNOWN], 0),
    ([C.OK, C.FAIL], 1),
    ([], 0),
])
def test_exit_code_is_one_only_on_fail(statuses, code, monkeypatch, capsys):
    _fake(statuses, monkeypatch)
    assert ops_status.main(['--no-network', '--no-schema']) == code
    assert 'operations status' in capsys.readouterr().out


def test_only_unknown_keys_are_refused(monkeypatch):
    with pytest.raises(SystemExit) as exc:
        ops_status.main(['--only', 'disk,nonsense'])
    assert exc.value.code == 2


def test_json_and_write_status(paths, monkeypatch, capsys):
    tmp, db = paths
    out = tmp / 'instance' / 'ops_status.json'
    code = ops_status.main(['--json', '--no-network', '--no-schema',
                            '--only', 'disk,backups,database',
                            '--db', db, '--backups-dir', str(tmp / 'backups'),
                            '--write-status', str(out)])
    printed = json.loads(capsys.readouterr().out)
    assert code == 1                              # no backup is a FAIL
    assert [c['key'] for c in printed['checks']] == ['database', 'disk',
                                                     'backups']
    assert printed['only'] == ['backups', 'database', 'disk']
    with open(out) as fh:
        assert json.load(fh)['checks'] == printed['checks']


def test_the_cli_never_imports_the_application(paths):
    """Importing app.py boots the app against DATABASE_URL. Run the real
    script in a clean interpreter and look at what it loaded."""
    tmp, db = paths
    code = (
        'import runpy, sys\n'
        f'sys.argv = ["ops_status.py", "--json", "--no-network", '
        f'"--no-schema", "--only", "database,backups,restore,copilot_index,'
        f'config", "--db", {db!r}, "--backups-dir", '
        f'{str(tmp / "backups")!r}]\n'
        'try:\n'
        '    runpy.run_path("scripts/ops_status.py", run_name="__main__")\n'
        'except SystemExit:\n'
        '    pass\n'
        'bad = [m for m in sys.modules if m == "app" or m.startswith("app.")'
        ' or m in ("flask", "email_ingest")]\n'
        'print("LOADED", bad)\n')
    env = dict(os.environ, DATABASE_URL='sqlite:///' + db)
    proc = subprocess.run([sys.executable, '-c', code], cwd=_ROOT, env=env,
                          capture_output=True, text=True, timeout=120)
    assert 'LOADED []' in proc.stdout, proc.stdout + proc.stderr
    report = json.loads(proc.stdout[:proc.stdout.rindex('LOADED')])
    assert {c['key'] for c in report['checks']} == {
        'database', 'backups', 'restore', 'copilot_index', 'config'}
