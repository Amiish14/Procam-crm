"""City/state mismatch survey, fix and undo (CRM-SEARCH-SCAN-01 fix 7)."""
import importlib.util
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_contact_search_15nov import (  # noqa: E402,F401
    _clean, _contact, _lead, db)
from app.services import india_places as ip  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    'locfix', os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'scripts',
        '2026_11_19_location_mismatch.py'))
locfix = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(locfix)


def test_the_reference_knows_cities_pins_and_spellings():
    assert ip.state_for_city('Mumbai') == 'Maharashtra'
    assert ip.state_for_city('Kolkata') == 'West Bengal'
    assert ip.state_for_city('Aurangabad') == ''      # two states
    assert ip.state_for_pin('392001') == 'Gujarat'
    assert ip.state_for_pin('831001') == 'Jharkhand'
    assert ip.canonical_state('MH') == 'Maharashtra'
    assert ip.find_pin('Plot 4, Dahej 392 130, Gujarat') == '392130'


def test_survey_apply_and_undo():
    wrong = _contact(name='ZT Prasad', city='Mumbai', state='Gujarat')
    ok = _contact(name='ZT Ok', city='Pune', state='MH')
    unknown = _contact(name='ZT Unknown', city='Nowhere', state='Gujarat')
    lead = _lead(pic='ZT Lead', city='Kolkata', state='Jharkhand')
    found = [(k, r.id) for k, r, *_ in locfix.survey()]
    assert ('contact', wrong.id) in found and ('lead', lead.id) in found
    assert ('contact', ok.id) not in found
    assert ('contact', unknown.id) not in found
    mine = [f for f in locfix.survey()
            if (f[0], f[1].id) in {('contact', wrong.id), ('lead', lead.id)}]
    locfix.apply(mine, batch='LOCFIX-TEST-1', actor='DIR12010')
    assert db.session.get(type(wrong), wrong.id).state == 'Maharashtra'
    assert db.session.get(type(lead), lead.id).state == 'West Bengal'
    locfix.undo('LOCFIX-TEST-1', 'DIR12010')
    assert db.session.get(type(wrong), wrong.id).state == 'Gujarat'
    assert db.session.get(type(lead), lead.id).state == 'Jharkhand'
