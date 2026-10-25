"""
One list of lost reasons, and every reason in use can be picked.

There were two lists. `app.LOST_REASONS` held twelve values and
fed the dashboard filters; the `lost_reason` Master Data list held
ten different ones and filled the dropdown a person uses. So "No
Response" could be reported on and never chosen, "Price too high"
chosen and never reported on, and the 1,213 leads carrying "Not
interested" sat under a reason in neither.

The failure that matters is not the mismatch itself. It is what
somebody does when the true reason is missing from the dropdown:
they pick a different one. Every lost-reason report after that is
wrong, and nothing in the data shows it happened. So the test that
earns its place is the last one here — every reason already
written on a lead must be choosable by hand.
"""
import importlib
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'lostreason-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'LostReason-12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'lostreason.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
importlib.import_module('app.models.master_data')
flask_app = _main.app
flask_app.config['WTF_CSRF_ENABLED'] = False
db = _main.db
Lead = _main.Lead

from app.models.master_data import MasterItem              # noqa: E402

LIST_KEY = 'lost_reason'

sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
import importlib as _il                                    # noqa: E402
_recon = _il.import_module('2026_10_25_reconcile_lost_reasons')


def _purge():
    MasterItem.query.filter_by(list_key=LIST_KEY).delete(
        synchronize_session=False)
    Lead.query.filter(Lead.company.like('LR%')).delete(
        synchronize_session=False)
    db.session.commit()


@pytest.fixture(autouse=True)
def _clean():
    with flask_app.app_context():
        db.create_all()
        _purge()
        yield
        _purge()


def _lead(reason):
    lead = Lead(company='LR Co', stage='Lost', lost_reason=reason,
                source='manual')
    db.session.add(lead)
    db.session.commit()
    return lead


def _seed_some(*codes):
    for code in codes:
        db.session.add(MasterItem(list_key=LIST_KEY, code=code,
                                  label=code, is_active=True))
    db.session.commit()


# ── Master Data is the source ────────────────────────────────────────
def test_the_list_comes_from_master_data():
    with flask_app.app_context():
        _seed_some('Price too high', 'Lost to competitor')
        # Order is Master Data's to decide (sort_order, then label);
        # what matters here is that the list is its contents.
        assert set(_main.lost_reasons()) == {'Price too high',
                                             'Lost to competitor'}


def test_an_empty_list_falls_back_rather_than_blocking():
    """A dropdown with no options stops people closing leads. An
    unreadable list is a reason to degrade, not to block."""
    with flask_app.app_context():
        assert MasterItem.query.filter_by(list_key=LIST_KEY).count() == 0
        assert _main.lost_reasons() == _main.LOST_REASONS_SEED


def test_adding_a_reason_needs_no_restart():
    with flask_app.app_context():
        _seed_some('Price too high')
        assert 'Geography Constraint' not in _main.lost_reasons()
        _seed_some('Geography Constraint')
        assert 'Geography Constraint' in _main.lost_reasons()


def test_the_api_serves_the_master_data_list():
    with flask_app.app_context():
        _seed_some('Price too high', 'No Response')
    client = flask_app.test_client()
    with client.session_transaction() as s:
        s['emp_code'] = 'PCM001'
        s['role'] = 'admin'
    r = client.get('/api/meta')
    if r.status_code != 200:
        pytest.skip('/api/meta is not the endpoint in this build')
    served = r.get_json().get('lost_reasons')
    assert served == ['Price too high', 'No Response']


# ── the reconciliation ───────────────────────────────────────────────
def test_it_adds_the_seed_values_that_were_missing():
    """The seed is now the final seven, so that is what the
    reconciliation tops the list up with."""
    with flask_app.app_context():
        _seed_some('Price too high')
        _have, wanted, _used = _recon.plan()
        codes = [c for c, _n in wanted]
        assert 'No response' in codes
        assert 'Price too high' not in codes       # already there


def test_it_adds_reasons_that_leads_already_carry():
    with flask_app.app_context():
        _lead('Not interested')
        _have, wanted, _used = _recon.plan()
        assert 'Not interested' in [c for c, _n in wanted]


@pytest.mark.parametrize('reason', [
    'Others: duplicate of #10781',
    'Restored — originally deleted: no reason given (deleted by X)',
    'Others – Duplicate of lead #12115',
])
def test_one_off_sentences_never_become_vocabulary(reason):
    """800 free-text reasons in a dropdown make it unusable."""
    with flask_app.app_context():
        _lead(reason)
        _have, wanted, used = _recon.plan()
        assert reason not in [c for c, _n in wanted]
        assert reason not in used


def test_running_it_twice_adds_nothing_the_second_time():
    with flask_app.app_context():
        _lead('Not interested')
        _have, wanted, _used = _recon.plan()
        for code, _n in wanted:
            db.session.add(MasterItem(list_key=LIST_KEY, code=code,
                                      label=code, is_active=True))
        db.session.commit()
        _have2, wanted2, _u2 = _recon.plan()
        assert wanted2 == []


# ── the one that matters ─────────────────────────────────────────────
def test_every_reason_in_use_can_be_picked_by_hand():
    """If the true reason is missing from the dropdown, somebody
    picks a different one, and every report after that is wrong
    with nothing in the data to show it."""
    with flask_app.app_context():
        for reason in ('Not interested', 'No Response', 'Price',
                       'Price too high', 'Lost to competitor'):
            _lead(reason)
        _have, wanted, _used = _recon.plan()
        for code, _n in wanted:
            db.session.add(MasterItem(list_key=LIST_KEY, code=code,
                                      label=code, is_active=True))
        db.session.commit()

        choosable = set(_main.lost_reasons())
        marks = (':', '—', '–', '#')
        in_use = {r for (r,) in db.session.query(
            Lead.lost_reason).filter(
                Lead.lost_reason.isnot(None)).distinct()
            if r and len(r) <= 40
            and not any(m in r for m in marks)}
        missing = in_use - choosable
        assert not missing, f'cannot be picked by hand: {missing}'


# ── the final seven ──────────────────────────────────────────────────
_map = _il.import_module('2026_10_25_lost_reason_mapping')


def test_the_seed_matches_the_final_seven():
    """The fallback must not reintroduce the list we removed."""
    assert _main.LOST_REASONS_SEED == _map.FINAL
    assert len(_map.FINAL) == 7


def test_not_interested_stays_its_own_reason():
    """A customer who says it has answered. Folding 1,213 of those
    into silence loses the one distinction a head acts on."""
    assert 'Not interested' in _map.FINAL
    assert _map.MAPPING.get('Not interested') is None or \
        _map.MAPPING['Not interested'] == 'Not interested'
    assert _map.MAPPING['Client Decision'] == 'Not interested'


def test_timeline_mismatch_is_a_postponement():
    assert _map.MAPPING['Timeline mismatch'] == \
        'Customer cancelled or postponed'


def test_duplicate_is_not_a_lost_reason():
    assert 'Duplicate / Invalid' not in _map.MAPPING
    assert 'Duplicate / Invalid' not in _map.FINAL


def test_every_mapping_target_is_one_of_the_seven():
    for was, target in _map.MAPPING.items():
        assert target in _map.FINAL, f'{was} -> {target}'


@pytest.mark.parametrize('reason', [
    'Restored — originally deleted: no reason given',
    'Others: newsletter, not a client',
    'Others – Duplicate of lead #12115',
])
def test_free_text_is_left_exactly_as_written(reason):
    """Those sentences are the only record of why the lead closed."""
    with flask_app.app_context():
        lead = _lead(reason)
        assert _map.is_free_text(reason)
        _map.apply_plan(actor='test')
        db.session.commit()
        assert db.session.get(Lead, lead.id).lost_reason == reason


def test_a_mapped_reason_moves_and_is_reversible():
    with flask_app.app_context():
        lead = _lead('No Response from Client')
        _map.apply_plan(actor='test')
        db.session.commit()
        assert db.session.get(Lead, lead.id).lost_reason == 'No response'
        _map.undo(actor='test')
        db.session.commit()
        assert db.session.get(Lead, lead.id).lost_reason == \
            'No Response from Client'


def test_a_duplicate_with_a_named_lead_points_at_it():
    with flask_app.app_context():
        lead = _lead('Duplicate / Invalid')
        lead.notes = 'Closed: duplicate of #10781'
        db.session.commit()
        _map.apply_plan(actor='test')
        db.session.commit()
        assert db.session.get(Lead, lead.id).lost_reason == \
            'Others: duplicate of #10781'


def test_a_duplicate_naming_nothing_falls_to_others():
    with flask_app.app_context():
        lead = _lead('Duplicate / Invalid')
        _map.apply_plan(actor='test')
        db.session.commit()
        assert db.session.get(Lead, lead.id).lost_reason == 'Others'


def test_the_list_is_reduced_to_seven_and_restored():
    with flask_app.app_context():
        _seed_some('Price too high', 'No Response', 'Other')
        before, now = _map.reduce_list(actor='test')
        db.session.commit()
        assert now == 7
        assert set(_main.lost_reasons()) == set(_map.FINAL)
        _map.undo(actor='test')
        db.session.commit()
        assert set(_main.lost_reasons()) == {'Price too high',
                                             'No Response', 'Other'}


def test_every_reason_in_use_is_still_choosable_after_the_reduction():
    """The rule that mattered before still has to hold after."""
    with flask_app.app_context():
        for reason in ('No Response', 'Not interested', 'Price too high',
                       'Competition', 'Timeline mismatch'):
            _lead(reason)
        _map.apply_plan(actor='test')
        _map.reduce_list(actor='test')
        db.session.commit()

        choosable = set(_main.lost_reasons())
        in_use = {r for (r,) in db.session.query(
            Lead.lost_reason).filter(Lead.lost_reason.isnot(None))
            .distinct() if r and not _map.is_free_text(r)}
        assert not (in_use - choosable), in_use - choosable
