"""
scripts/feature_flag.py — the release switch, and the way back.

The dry run is the default and it writes nothing: no flag row, no
rollback file. `--apply` writes the rollback file first and stops if
it cannot, because a switch-on with no way back is the thing this
script exists to replace.

`--all-on` means all users, not a pilot: it lifts the named-people
and vertical limits as well as turning the flag on. A flag on with a
stale pilot list behind it is on for four people and off for sixty.

The restore tests are the point. Two ways of being off are kept
apart — a flag with no row reads its default, and writing `false`
into a row that was never there is a new decision, not a restore —
so the file records which it was and the restore puts that back.
"""
import importlib
import importlib.util
import json
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'flagscript-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'FlagScriptTest-123456')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'flagscript.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app = _main.app
db = _main.db

from app.services import flags                            # noqa: E402

ONE = 'FEATURE_FF_CRM'
TWO = 'FEATURE_LEAD_POPUP_V2'
ACTOR = 'FS_ACTOR'


def _script():
    path = os.path.join(_ROOT, 'scripts', 'feature_flag.py')
    spec = importlib.util.spec_from_file_location('featureflagscript',
                                                  path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ff = _script()


def _wipe():
    from app.models.integration import AppSetting
    AppSetting.query.filter(AppSetting.key.like('flag.%')).delete(
        synchronize_session=False)
    db.session.commit()
    flags.cache_clear()


@pytest.fixture(autouse=True)
def _clean(tmp_path):
    with flask_app.app_context():
        db.create_all()
        _wipe()
        ff.ROLLBACK_DIR = str(tmp_path)
        yield
        _wipe()


def _run(*argv):
    with flask_app.app_context():
        return ff.main(list(argv) + ['--actor', ACTOR])


def _rollback_dir(tmp_path):
    return ['--rollback-dir', str(tmp_path)]


def _files(tmp_path):
    return sorted(p for p in os.listdir(tmp_path) if p.endswith('.json'))


# ── the dry run is the default ───────────────────────────────────────
def test_the_dry_run_changes_nothing_and_writes_nothing(tmp_path):
    assert _run('--all-on', *_rollback_dir(tmp_path)) == 0
    with flask_app.app_context():
        assert not flags.on(ONE)
        assert not flags.on(TWO)
    assert _files(tmp_path) == []


def test_one_flag_dry_run_leaves_it_off(tmp_path):
    assert _run('--code', ONE, '--on', *_rollback_dir(tmp_path)) == 0
    with flask_app.app_context():
        assert not flags.on(ONE)
    assert _files(tmp_path) == []


def test_the_listing_needs_no_flags_at_all():
    assert _run() == 0


# ── --apply ──────────────────────────────────────────────────────────
def test_apply_turns_one_flag_on_and_writes_the_rollback_file(tmp_path):
    assert _run('--code', ONE, '--on', '--apply',
                *_rollback_dir(tmp_path)) == 0
    with flask_app.app_context():
        flags.cache_clear()
        assert flags.on(ONE)
        assert not flags.on(TWO)
    files = _files(tmp_path)
    assert len(files) == 1
    body = json.load(open(os.path.join(tmp_path, files[0])))
    assert body['actor'] == ACTOR
    assert body['what'] == f'{ONE} on'
    saved = {r['name']: r for r in body['flags']}
    # The file holds the state as it was BEFORE the change, which is
    # the only state worth keeping.
    assert saved[ONE]['stored'] is None
    assert saved[ONE]['live'] is False


def test_the_prefix_is_optional_and_case_does_not_matter(tmp_path):
    assert _run('--code', 'ff_crm', '--on', '--apply',
                *_rollback_dir(tmp_path)) == 0
    with flask_app.app_context():
        flags.cache_clear()
        assert flags.on(ONE)


def test_all_on_switches_everything_except_the_kill_switch(tmp_path):
    assert _run('--all-on', '--apply', *_rollback_dir(tmp_path)) == 0
    with flask_app.app_context():
        flags.cache_clear()
        for name, _d, _s in flags.FLAGS:
            if name == flags.KILL_SWITCH:
                assert not flags.on(name), 'the kill switch stayed off'
            else:
                assert flags.on(name), name


def test_the_kill_switch_goes_on_only_when_it_is_asked_for(tmp_path):
    assert _run('--all-on', '--include-kill-switch', '--apply',
                *_rollback_dir(tmp_path)) == 0
    with flask_app.app_context():
        flags.cache_clear()
        assert flags.on(flags.KILL_SWITCH)


def test_the_kill_switch_can_still_be_set_by_name(tmp_path):
    assert _run('--code', flags.KILL_SWITCH, '--on', '--apply',
                *_rollback_dir(tmp_path)) == 0
    with flask_app.app_context():
        flags.cache_clear()
        assert flags.on(flags.KILL_SWITCH)


def test_off_turns_a_flag_off(tmp_path):
    with flask_app.app_context():
        flags.set(ONE, True, actor=ACTOR)
    assert _run('--code', ONE, '--off', '--apply',
                *_rollback_dir(tmp_path)) == 0
    with flask_app.app_context():
        flags.cache_clear()
        assert not flags.on(ONE)


def test_nothing_to_do_writes_no_rollback_file(tmp_path):
    with flask_app.app_context():
        flags.set(ONE, True, actor=ACTOR)
    assert _run('--code', ONE, '--on', '--apply',
                *_rollback_dir(tmp_path)) == 0
    assert _files(tmp_path) == []


# ── all users, not a pilot ───────────────────────────────────────────
def test_all_on_lifts_the_pilot_limits(tmp_path):
    with flask_app.app_context():
        flags.set(ONE, True, actor=ACTOR)
        flags.limit_to(ONE, ['DIR12010'], actor=ACTOR)
        flags.limit_verticals(ONE, ['Warehousing'], actor=ACTOR)
        assert flags.only_for(ONE) == ('DIR12010',)
    assert _run('--all-on', '--apply', *_rollback_dir(tmp_path)) == 0
    with flask_app.app_context():
        flags.cache_clear()
        assert flags.only_for(ONE) == ()
        assert flags.only_verticals(ONE) == ()
        # on for somebody who was never on the pilot list
        assert flags.on_for(ONE, 'NOBODY_IN_PARTICULAR')


def test_one_flag_on_lifts_its_pilot_limits_too(tmp_path):
    with flask_app.app_context():
        flags.set(ONE, True, actor=ACTOR)
        flags.limit_to(ONE, ['DIR12010'], actor=ACTOR)
    assert _run('--code', ONE, '--on', '--apply',
                *_rollback_dir(tmp_path)) == 0
    with flask_app.app_context():
        flags.cache_clear()
        assert flags.only_for(ONE) == ()
        assert flags.on_for(ONE, 'SOMEONE_ELSE')


def test_the_lifted_pilot_is_in_the_rollback_file(tmp_path):
    with flask_app.app_context():
        flags.set(ONE, True, actor=ACTOR)
        flags.limit_to(ONE, ['DIR12010'], actor=ACTOR)
        flags.limit_verticals(ONE, ['Warehousing'], actor=ACTOR)
    _run('--all-on', '--apply', *_rollback_dir(tmp_path))
    body = json.load(open(os.path.join(tmp_path, _files(tmp_path)[0])))
    saved = {r['name']: r for r in body['flags']}
    assert saved[ONE]['only_people'] == ['DIR12010']
    assert saved[ONE]['only_verticals'] == ['Warehousing']


# ── the way back ─────────────────────────────────────────────────────
def test_the_restore_line_names_the_file_that_was_written(tmp_path):
    path = os.path.join(tmp_path, 'flags-20261031-064500.json')
    line = ff.restore_line(path)
    assert '--restore' in line and '--apply' in line
    assert 'flags-20261031-064500.json' in line
    assert 'scripts/feature_flag.py' in line


def test_a_restore_puts_a_pilot_back_exactly(tmp_path):
    with flask_app.app_context():
        flags.set(ONE, True, actor=ACTOR)
        flags.limit_to(ONE, ['DIR12010'], actor=ACTOR)
        flags.limit_verticals(ONE, ['Warehousing'], actor=ACTOR)
    _run('--all-on', '--apply', *_rollback_dir(tmp_path))
    saved_file = os.path.join(tmp_path, _files(tmp_path)[0])

    assert _run('--restore', saved_file, '--apply',
                *_rollback_dir(tmp_path)) == 0
    with flask_app.app_context():
        flags.cache_clear()
        assert flags.on(ONE)
        assert flags.only_for(ONE) == ('DIR12010',)
        assert flags.only_verticals(ONE) == ('Warehousing',)
        assert not flags.on(TWO), 'the others went back off'


def test_a_restore_dry_run_changes_nothing(tmp_path):
    _run('--all-on', '--apply', *_rollback_dir(tmp_path))
    saved_file = os.path.join(tmp_path, _files(tmp_path)[0])
    assert _run('--restore', saved_file, *_rollback_dir(tmp_path)) == 0
    with flask_app.app_context():
        flags.cache_clear()
        assert flags.on(ONE), 'still on — the dry run wrote nothing'


def test_a_restore_takes_the_row_away_when_there_never_was_one(tmp_path):
    """The difference the file keeps: no row is not the same as a row
    saying false. One reads the default, the other overrides it."""
    from app.models.integration import AppSetting
    _run('--code', ONE, '--on', '--apply', *_rollback_dir(tmp_path))
    saved_file = os.path.join(tmp_path, _files(tmp_path)[0])
    _run('--restore', saved_file, '--apply', *_rollback_dir(tmp_path))
    with flask_app.app_context():
        flags.cache_clear()
        assert AppSetting.query.filter_by(key='flag.' + ONE).first() is None
        assert flags.source_of(ONE) in ('default', 'environment')


def test_a_restore_keeps_a_flag_that_was_on_by_default_on(tmp_path):
    """FEATURE_DAILY_BRIEF is the one flag whose default is on. A
    restore must not turn it off on the way back."""
    brief = 'FEATURE_DAILY_BRIEF'
    assert flags.DEFAULTS[brief] is True
    _run('--all-on', '--apply', *_rollback_dir(tmp_path))
    saved_file = os.path.join(tmp_path, _files(tmp_path)[0])
    _run('--restore', saved_file, '--apply', *_rollback_dir(tmp_path))
    with flask_app.app_context():
        flags.cache_clear()
        assert flags.on(brief)


def test_a_missing_rollback_file_stops_the_run(tmp_path):
    with pytest.raises(SystemExit):
        _run('--restore', str(tmp_path / 'not-here.json'), '--apply')


# ── refusals ─────────────────────────────────────────────────────────
def test_an_unknown_flag_stops_the_run():
    with pytest.raises(SystemExit):
        _run('--code', 'FEATURE_NOT_A_THING', '--on')


def test_code_without_on_or_off_stops_the_run():
    with pytest.raises(SystemExit):
        _run('--code', ONE)


def test_two_modes_at_once_stop_the_run():
    with pytest.raises(SystemExit):
        _run('--all-on', '--code', ONE, '--on')


def test_on_and_off_together_stop_the_run():
    with pytest.raises(SystemExit):
        _run('--code', ONE, '--on', '--off')


# ── the plan the dry run prints is the plan it carries out ───────────
def test_the_plan_is_empty_once_everything_is_on(tmp_path):
    _run('--all-on', '--include-kill-switch', '--apply',
         *_rollback_dir(tmp_path))
    with flask_app.app_context():
        flags.cache_clear()
        names = [n for n, _d, _s in flags.FLAGS]
        assert ff.plan_on(names) == []


def test_the_snapshot_records_where_each_answer_came_from():
    with flask_app.app_context():
        flags.set(ONE, True, actor=ACTOR)
        rows = {r['name']: r for r in ff.snapshot()}
    assert rows[ONE]['source'] == 'database'
    assert rows[ONE]['stored'] is True
    assert rows[TWO]['stored'] is None
    assert rows[ONE]['description']


def test_every_flag_in_the_service_is_in_the_snapshot():
    with flask_app.app_context():
        names = {r['name'] for r in ff.snapshot()}
    assert names == {n for n, _d, _s in flags.FLAGS}


# ── the neighbour this one replaces for releases ─────────────────────
def _pilot_script():
    path = os.path.join(_ROOT, 'scripts', 'set_pilot.py')
    spec = importlib.util.spec_from_file_location('setpilotscript', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_set_pilot_off_actually_turns_the_flag_off(monkeypatch):
    """It called `flags.set_flag`, which has never existed, behind a
    hasattr guard — so --off lifted both limits and left the flag ON
    for everybody, the opposite of what it says."""
    pilot = _pilot_script()
    with flask_app.app_context():
        flags.set(ONE, True, actor=ACTOR)
        flags.limit_to(ONE, ['DIR12010'], actor=ACTOR)
        monkeypatch.setattr(
            sys, 'argv',
            ['set_pilot.py', '--flag', ONE, '--off', '--actor', ACTOR])
        pilot.main()
        flags.cache_clear()
        assert not flags.on(ONE)
        assert flags.only_for(ONE) == ()
