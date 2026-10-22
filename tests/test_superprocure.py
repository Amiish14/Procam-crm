"""
Reading a load posting out of an email.

SuperProcure flattens a table into the message body, so the
columns run together: a source address ends in a pincode and the
destination begins at the next capital letter. Everything here is
a parser that can be quietly wrong, which is the worst kind — a
lane of "Maharashtra → Pin" looks like data.

The samples are real postings, trimmed.
"""
import importlib.util as _u
import os
import sys
from datetime import date

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ.setdefault('DATABASE_URL', 'sqlite://')
os.environ.setdefault('SECRET_KEY', 'sp' * 20)
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'SpTest-12345')


def _mod():
    spec = _u.spec_from_file_location(
        'superprocure_report',
        os.path.join(_ROOT, 'scripts', 'superprocure_report.py'))
    m = _u.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


LOAD_POST = (
    'Isgec Heavy Engineering Ltd.  |  Load Post\n'
    'Load posted by Isgec Heavy Engineering Ltd.\n'
    'IDLoad TypeLoading DateSourceDestinationVehicleDistance(Km)\n'
    '#25236064LIVE15 Sep 8:00 AMM/s BHEL, Trichy, Tamil Nadu – '
    '620014, Trichy, Tamil Nadu, 620014ISGEC Heavy Engineering, '
    'Rattangarh, Haryana, Rattangarh, Haryana, Pin-135002'
    'III axle PHB2635.54Vehicles Required:2, Qty/Vehicle:40MT,\n'
)

AUCTION = (
    'Isgec Heavy Engineering Ltd.  |  Auction Start\n'
    'Auction starting for Isgec Heavy Engineering Ltd.\n'
    '#25152148E-AUCTION9 Sep - 2:47 pm9 SepShreegee Private '
    'Limited,MEERUT(1000004069), Meerut, Uttar Pradesh, 245206'
    'ATHARV INTERTRADE PRIVATE LIMITED,CHANDGAD(SS-00275), '
    'Chandgad, Maharashtra, Pin-416552'
    'Trailer – 40 x 8 x 8 Feet, Weight 20 MT1673.68'
    'Vehicles Required:1, Qty/Vehicle:20MT,\n'
)


def test_the_shipper_is_read_from_either_form():
    m = _mod()

    class _L:
        company = ''
    assert m._shipper(LOAD_POST, _L()) == 'Isgec Heavy Engineering Ltd'
    assert m._shipper(AUCTION, _L()) == 'Isgec Heavy Engineering Ltd'


def test_a_lane_is_two_towns_not_two_states():
    """The first pass gave "Maharashtra → Pin". A lane of two
    states is not a lane anybody can act on, and "Pin" is not a
    place at all."""
    m = _mod()
    src, dst = m._lane(LOAD_POST)
    assert m._place(src) == 'Trichy'
    assert m._place(dst) == 'Rattangarh'

    src, dst = m._lane(AUCTION)
    assert m._place(src) == 'MEERUT'
    assert m._place(dst) == 'CHANDGAD'


@pytest.mark.parametrize('address,expected', [
    ('ISGEC, Rattangarh, Haryana, Pin-135002', 'Rattangarh'),
    ('M/s BHEL, Trichy, Tamil Nadu – 620014', 'Trichy'),
    ('Shreegee Private Limited,MEERUT(1000004069), Meerut, '
     'Uttar Pradesh, 245206', 'MEERUT'),
    ('', '(unparsed)'),
    ('(unparsed)', '(unparsed)'),
])
def test_a_place_is_never_a_pincode_or_a_label(address, expected):
    assert _mod()._place(address) == expected


def test_the_vehicle_and_tonnage_come_out():
    m = _mod()
    assert m.RX_VEHICLES.search(LOAD_POST).group(1) == '2'
    assert m.RX_QTY.search(LOAD_POST).group(1) == '40'
    assert m.RX_QTY.search(AUCTION).group(1) == '20'
    assert m.RX_DIST.search(LOAD_POST).group(1) == '2635.54'


def test_the_posting_id_and_type():
    m = _mod()
    hit = m.RX_ID.search(LOAD_POST)
    assert hit.group(1) == '25236064' and hit.group(2) == 'LIVE'
    hit = m.RX_ID.search(AUCTION)
    assert hit.group(1) == '25152148' and hit.group(2) == 'E-AUCTION'


def test_a_loading_date_with_no_year_takes_the_reading_a_person_takes():
    """The posting gives a day and a month. A load posted on 28
    December for 3 January is next year's, and that is the only
    reading ever right."""
    m = _mod()
    from datetime import datetime

    assert m._loading_date(LOAD_POST,
                           datetime(2026, 9, 11)) == date(2026, 9, 15)
    december = LOAD_POST.replace('15 Sep 8:00 AM', '3 Jan 8:00 AM')
    assert m._loading_date(december,
                           datetime(2026, 12, 28)) == date(2027, 1, 3)


def test_an_unreadable_body_says_so_rather_than_guessing():
    m = _mod()
    src, dst = m._lane('nothing structured in here at all')
    assert src == '(unparsed)' and dst == '(unparsed)'


def test_the_report_changes_nothing():
    import ast

    tree = ast.parse(open(os.path.join(
        _ROOT, 'scripts', 'superprocure_report.py')).read())
    called = {ast.unparse(n.func) for n in ast.walk(tree)
              if isinstance(n, ast.Call)}
    for banned in ('db.session.commit', 'db.session.add',
                   'db.session.delete'):
        assert banned not in called, banned
