"""The backup is a consistent, verified, private copy, and pruning is
confined to the script's own files."""
import os
import sqlite3
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, 'scripts'))

import backup_database as B                                    # noqa: E402


@pytest.fixture()
def live(tmp_path):
    path = tmp_path / 'live.db'
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE leads (id INTEGER PRIMARY KEY, company TEXT)')
    db.executemany('INSERT INTO leads (company) VALUES (?)',
                   [(f'Co {i}',) for i in range(500)])
    db.commit()
    db.close()
    return str(path)


def test_the_backup_is_complete_checked_and_private(live, tmp_path):
    r = B.backup(live, str(tmp_path / 'b'), 'pre-deploy')
    assert r['ok'] and r['leads'] == 500
    assert r['path'].endswith('-pre-deploy.db')
    assert oct(os.stat(r['path']).st_mode & 0o777) == '0o600'


def test_a_backup_taken_during_a_write_is_consistent(live, tmp_path):
    """An open, uncommitted transaction on the live file must not leak
    into the copy."""
    writer = sqlite3.connect(live)
    writer.execute('BEGIN')
    writer.execute("INSERT INTO leads (company) VALUES ('half-written')")
    try:
        r = B.backup(live, str(tmp_path / 'b'))
    finally:
        writer.rollback()
        writer.close()
    assert r['ok'] and r['leads'] == 500


def test_pruning_keeps_the_newest_and_only_touches_its_own_files(live,
                                                                 tmp_path):
    folder = tmp_path / 'b'
    folder.mkdir()
    other = folder / 'procam_crm.db.bak-handmade'
    other.write_text('keep me')
    made = []
    for i in range(5):
        p = folder / f'procam_crm-2026-01-0{i + 1}-000000.db'
        p.write_text('x')
        os.utime(p, (1_700_000_000 + i, 1_700_000_000 + i))
        made.append(p)
    gone = B.prune(str(folder), 3)
    assert sorted(os.path.basename(g) for g in gone) == sorted(
        p.name for p in made[:2])
    assert other.exists()
