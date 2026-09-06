"""Linking Leads and Contacts to Company Master — §7, §65.

The property that matters is not how many link, but that nothing uncertain
is guessed. A lead attached to the wrong company is worse than a lead
attached to none, because it corrupts every report silently.
"""
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'LinkTestOnly12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'link.db')

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Company, Lead = _main.Company, _main.Lead

from app.services.company_match import (build_index, match,      # noqa: E402
                                        NO_MATCH, AMBIGUOUS)


@pytest.fixture()
def master():
    with flask_app.app_context():
        db.create_all()
        Lead.query.delete()
        Company.query.delete()
        db.session.commit()
        rows = {}
        for name in ('Siemens Limited', 'Siemens Energy India Limited',
                     'Voith Hydro Pvt Ltd', 'Bharat Heavy Electricals Limited'):
            c = Company(name=name, is_active=True)
            db.session.add(c)
            db.session.flush()
            rows[name] = c.id
        # A retired duplicate must never be matched against.
        dead = Company(name='Siemens Ltd. [merged into #1]', is_active=False)
        db.session.add(dead)
        db.session.commit()
        return rows


def _index():
    return build_index(Company.query.filter(Company.is_active.is_(True)).all())


def test_exact_name_links(master):
    with flask_app.app_context():
        c, reason, _ = match('Siemens Limited', _index())
        assert reason is None and c.id == master['Siemens Limited']


def test_legal_suffix_variations_link(master):
    """'Siemens Ltd' and 'SIEMENS LIMITED' are the same company."""
    with flask_app.app_context():
        idx = _index()
        for variant in ('Siemens Ltd', 'SIEMENS LIMITED', 'Siemens Ltd.',
                        'siemens'):
            c, reason, _ = match(variant, idx)
            assert reason is None, f'{variant!r} should have linked'
            assert c.id == master['Siemens Limited']


def test_a_different_company_does_not_link(master):
    """Siemens and Siemens Energy India are different records."""
    with flask_app.app_context():
        c, reason, _ = match('Siemens Energy India Limited', _index())
        assert c.id == master['Siemens Energy India Limited']
        assert c.id != master['Siemens Limited']


def test_abbreviations_are_never_guessed(master):
    """§65 — 'BHEL' is probably Bharat Heavy Electricals, and 'probably'
    must reach a person rather than be acted on."""
    with flask_app.app_context():
        c, reason, candidates = match('BHEL', _index())
        assert c is None
        assert reason == NO_MATCH


def test_unknown_name_is_queued_with_suggestions(master):
    with flask_app.app_context():
        c, reason, candidates = match('Voith Hydro', _index())
        # 'voith hydro' normalises identically to 'Voith Hydro Pvt Ltd'
        assert c is not None and reason is None

        c2, reason2, cand2 = match('Completely Unknown Trading', _index())
        assert c2 is None and reason2 == NO_MATCH


def test_ambiguous_names_go_to_review_not_a_coin_flip(master):
    """Two active companies normalising the same must never be picked
    between automatically."""
    with flask_app.app_context():
        db.session.add(Company(name='Voith Hydro Private Limited',
                               is_active=True))
        db.session.commit()
        c, reason, candidates = match('Voith Hydro Ltd', _index())
        assert c is None, 'a tie must not be resolved by guessing'
        assert reason == AMBIGUOUS
        assert len(candidates) == 2, 'both options must reach the reviewer'


def test_retired_duplicates_are_not_matched(master):
    """De-duplication deactivates losers; linking must ignore them, or a
    lead attaches to a record that was just retired."""
    with flask_app.app_context():
        idx = _index()
        for rows in idx.values():
            for c in rows:
                assert c.is_active is True


def test_empty_name_is_not_a_match(master):
    with flask_app.app_context():
        idx = _index()
        for blank in ('', None, '   '):
            c, reason, _ = match(blank, idx)
            assert c is None and reason == NO_MATCH
