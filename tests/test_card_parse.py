"""Deterministic business-card parsing — WP2.

Extraction was Claude Vision and nothing else: no key, no credit, or a
malformed response and the reviewer got an empty form with no way to
proceed. These tests hold the deterministic layer that now sits under it.

The parser is pure text processing, so it is loaded straight from its
file — booting Flask to test a regex would hide a dependency creeping in.
"""
import importlib.util
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, 'tests', 'fixtures'))

from business_cards import CARDS                                # noqa: E402

_spec = importlib.util.spec_from_file_location(
    'card_parse_under_test',
    os.path.join(_ROOT, 'app', 'services', 'card_parse.py'))
cp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cp)


def test_the_parser_needs_nothing_but_the_standard_library():
    """It must run when the app, the API and the network are all absent —
    that is the whole point of having it."""
    src = open(os.path.join(_ROOT, 'app', 'services',
                            'card_parse.py')).read()
    for banned in ('from app import', 'import requests', 'import anthropic',
                   'flask'):
        assert banned not in src, f'card_parse must not depend on {banned}'


# ─── every card yields the fields it actually carries ────────────────────
EXPECTED = {
    'name_first':    ('Rajesh Kumar Sharma', 'ambujacement.com'),
    'company_first': ('Priya Menon',         'bhel.in'),
    'all_caps':      ('SURESH IYER',         'oceanicfreight.com'),
    'many_phones':   ('Anita Desai',         'globalcargo.co.in'),
    'no_website':    ('Vikram Singh',        'jindalsteel.com'),
    'generic_email': ('Mohammed Farooq',     ''),
    'international': ('Dr. Klaus Weber',     'schenker.de'),
    'multi_email':   ('Sanjay Gupta',        'seaways.in'),
    'minimal':       ('Neha Kapoor',         'kapoorexports.com'),
    'awkward':       ('Arun Balakrishnan',   'tataprojects.com'),
}


@pytest.mark.parametrize('key', sorted(CARDS))
def test_the_person_is_found_whatever_the_layout(key):
    """Name-first, company-first, all-caps and title-buried cards."""
    assert cp.parse(CARDS[key])['name'] == EXPECTED[key][0]


@pytest.mark.parametrize('key', sorted(CARDS))
def test_the_website_is_found_or_derived(key):
    """Printed where there is one; from the work email domain where not —
    and never from a personal one."""
    assert cp.parse(CARDS[key])['website'] == EXPECTED[key][1]


@pytest.mark.parametrize('key', sorted(CARDS))
def test_every_email_on_the_card_is_captured(key):
    parsed = cp.parse(CARDS[key])
    for line in CARDS[key].splitlines():
        if '@' in line and '.' in line:
            token = [w for w in line.replace(':', ' ').split() if '@' in w]
            for t in token:
                assert any(t.lower() == e.lower() for e in parsed['emails']), \
                    f'{t} was on the card and is not in {parsed["emails"]}'


@pytest.mark.parametrize('key', sorted(CARDS))
def test_a_card_always_parses_to_the_full_contract(key):
    parsed = cp.parse(CARDS[key])
    for field in ('name', 'designation', 'company', 'emails', 'phones',
                  'website', 'address', 'raw_text'):
        assert field in parsed
    assert isinstance(parsed['emails'], list)
    assert isinstance(parsed['phones'], list)


# ─── the harder individual behaviours ────────────────────────────────────
def test_all_four_numbers_are_kept_and_the_mobile_leads():
    p = cp.parse(CARDS['many_phones'])
    assert len(p['phones']) == 4, p['phones']
    assert p['phones'][0] == '+91 98765 43210'
    labels = {d['value']: d['label'] for d in p['phone_details']}
    assert labels['+91 22 4004 1299'] == 'fax'
    assert labels['+91 22 4004 1234'] == 'direct'


def test_a_fax_is_never_chosen_as_the_number_to_call():
    fields = cp.to_contact_fields(cp.parse(CARDS['many_phones']))
    assert 'fax' not in fields['mobile'].lower()
    assert fields['mobile'] == '+91 98765 43210'
    assert '1299' not in fields['telephone'], 'the fax became the telephone'


def test_two_numbers_on_one_line_get_their_own_labels():
    """"Mobile 970… | Board 040…" — one label per line tagged the board
    number as a mobile, which put a switchboard in the mobile field."""
    labels = {d['value']: d['label']
              for d in cp.parse(CARDS['awkward'])['phone_details']}
    assert labels['9701234567'] == 'mobile'
    assert labels['040-6612 3000'] == 'board'


def test_an_unlabelled_indian_mobile_is_recognised_by_shape():
    p = cp.parse(CARDS['multi_email'])
    labels = {d['value']: d['label'] for d in p['phone_details']}
    assert labels['+91 98490 12345'] == 'mobile'


def test_both_addresses_on_a_two_email_card_are_kept():
    p = cp.parse(CARDS['multi_email'])
    assert len(p['emails']) == 2
    assert p['emails'][0] == 'sanjay@seaways.in'


def test_a_personal_domain_is_not_treated_as_the_company_website():
    """farooq.transport@gmail.com says nothing about the employer."""
    p = cp.parse(CARDS['generic_email'])
    assert p['website'] == ''
    assert p['company'] == 'Farooq Transport Agencies'


def test_the_company_is_derived_from_the_domain_when_the_card_omits_it():
    p = cp.parse(CARDS['no_website'])
    assert p['company'] == 'Jindalsteel'


def test_the_address_is_multi_line_and_keeps_its_pin():
    p = cp.parse(CARDS['name_first'])
    assert 'MIDC' in p['address'] and '400093' in p['address']
    assert 'Maharashtra' in p['address']


def test_the_phone_number_does_not_leak_into_the_address():
    for key in CARDS:
        addr = cp.parse(CARDS[key])['address']
        assert '98200 11223' not in addr
        assert '+91' not in addr


def test_a_longer_title_wins_over_the_one_inside_it():
    """"Deputy General Manager" contains "Manager"; the specific one is
    the designation."""
    assert cp.parse(CARDS['awkward'])['designation'] == \
        'Deputy General Manager - Procurement'


def test_an_all_caps_card_parses_like_any_other():
    p = cp.parse(CARDS['all_caps'])
    assert p['name'] == 'SURESH IYER'
    assert p['designation'] == 'MANAGING DIRECTOR'
    assert p['company'] == 'OCEANIC FREIGHT FORWARDERS PVT LTD'


def test_a_non_english_card_still_yields_its_contact_details():
    p = cp.parse(CARDS['international'])
    assert p['company'] == 'SCHENKER DEUTSCHLAND GMBH'
    assert p['emails'] == ['k.weber@schenker.de']
    assert len(p['phones']) == 2


def test_a_minimal_card_does_not_invent_fields():
    p = cp.parse(CARDS['minimal'])
    assert p['name'] == 'Neha Kapoor'
    assert p['designation'] == ''
    assert p['address'] == ''


def test_empty_input_is_safe():
    for bad in ('', None, '   \n\n  '):
        p = cp.parse(bad)
        assert p['name'] == '' and p['emails'] == []


# ─── phone normalisation, which duplicate detection rests on ─────────────
@pytest.mark.parametrize('a,b', [
    ('+91 98200 11223', '09820011223'),
    ('+91-98200-11223', '98200 11223'),
    ('(0) 98200 11223', '9820011223'),
    ('0091 98200 11223', '+91 98200 11223'),
])
def test_the_same_number_written_differently_compares_equal(a, b):
    """This is what stops the same person being created three times."""
    assert cp.normalise_phone(a) == cp.normalise_phone(b)


def test_different_numbers_do_not_collide():
    assert cp.normalise_phone('9820011223') != cp.normalise_phone('9820011224')


# ─── validation and cross-check of model output ──────────────────────────
def test_a_model_that_returns_nulls_still_yields_the_contract():
    out = cp.normalise({'name': None, 'company': None, 'email': None,
                        'website': None})
    assert out['name'] == '' and out['emails'] == []


def test_a_model_that_crams_two_emails_into_one_field_is_split():
    out = cp.normalise({'email': 'a@x.com, b@y.com'})
    assert out['emails'] == ['a@x.com', 'b@y.com']


def test_junk_that_is_not_an_email_is_dropped_from_the_email_list():
    out = cp.normalise({'email': 'not an email'})
    assert out['emails'] == []


def test_a_model_answering_with_a_list_does_not_crash_normalise():
    assert cp.normalise(['unexpected'])['name'] == ''
    assert cp.normalise(None)['emails'] == []


def test_the_model_wins_where_it_answered_and_the_parser_fills_the_gaps():
    model = {'name': 'Rajesh Kumar Sharma', 'company': '', 'emails': [],
             'phones': ['+91 98200 11223'], 'designation': '',
             'website': '', 'address': '', 'raw_text': ''}
    merged = cp.cross_check(model, cp.parse(CARDS['name_first']))
    assert merged['name'] == 'Rajesh Kumar Sharma'
    assert merged['company'] == 'AMBUJA CEMENT LIMITED'   # parser filled it
    assert len(merged['phones']) == 2                     # parser added one


def test_a_real_disagreement_is_reported_not_silently_resolved():
    model = dict(cp.blank(), name='Someone Else', company='Wrong Ltd')
    merged = cp.cross_check(model, cp.parse(CARDS['name_first']))
    assert 'name' in merged['conflicts']
    assert 'company' in merged['conflicts']
    assert merged['name'] == 'Someone Else', 'the model still wins'
