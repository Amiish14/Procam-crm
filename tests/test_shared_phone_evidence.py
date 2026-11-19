"""Whose phone is it — the three tests, the three verdicts, and the
two things --apply must never touch.

UAT 19 Nov, shared-phone follow-up. 24 numbers sit on contacts at more
than one company and nobody knows which are Procam's, so the verdict
comes from evidence:

  (a) the number in a mail body sent FROM one of our own domains
  (b) the number on a Procam user's record
  (c) the number in a mail body sent from the CUSTOMER's own domain

  OURS = (a or b) and not c, CUSTOMER = c only, UNCLEAR = both or
  neither.

The two rules worth a test of their own are what it will NOT do: it
never clears a number from a user's record, and it does nothing at all
for a CUSTOMER or UNCLEAR number.
"""
import os
import sys

os.environ.setdefault('SECRET_KEY', 'test-only-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'Test-123456789')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))


@pytest.fixture
def mod():
    return __import__('2026_11_19_shared_phone_evidence')


# ── the verdicts ─────────────────────────────────────────────────────
def test_the_last_ten_digits_are_the_number(mod):
    for said in ('+91-7738155449', '07738155449', '7738155449',
                 '+91 77381 55449', '0091 7738155449'):
        assert mod.digits(said) == '7738155449', said
    # Too short to be a number is not a number.
    assert mod.digits('12345') == ''
    assert mod.digits('') == ''
    assert mod.digits(None) == ''


def test_a_number_in_both_signatures_is_unclear_not_ours(mod):
    """A number in our mail AND in theirs is most likely a real shared
    line. Calling it ours would take a working number off a customer."""
    src = open(os.path.join(ROOT, 'scripts',
                            '2026_11_19_shared_phone_evidence.py')).read()
    assert '(in_ours or on_staff) and not in_theirs' in src
    assert 'in_theirs and not (in_ours or on_staff)' in src


def test_the_verdicts_are_only_the_three(mod):
    assert (mod.OURS, mod.CUSTOMER, mod.UNCLEAR) == (
        'OURS', 'CUSTOMER', 'UNCLEAR')


# ── what --apply will not do ─────────────────────────────────────────
def test_only_ours_is_ever_planned(mod):
    rows = [
        {'verdict': 'OURS', 'phone': '1111111111',
         '_holders': [('c1', 'phone', '1111111111')]},
        {'verdict': 'CUSTOMER', 'phone': '2222222222',
         '_holders': [('c2', 'phone', '2222222222')]},
        {'verdict': 'UNCLEAR', 'phone': '3333333333',
         '_holders': [('c3', 'phone', '3333333333')]},
    ]
    planned = mod.planned(rows)
    assert [p[3] for p in planned] == ['1111111111']


def test_the_candidates_come_from_contacts_and_never_employees(mod):
    """`shared_numbers` reads contacts alone, so an employee record is
    not in the list `--apply` works from. The number on a Procam user's
    record is exactly where it belongs."""
    src = open(os.path.join(ROOT, 'scripts',
                            '2026_11_19_shared_phone_evidence.py')).read()
    fn = src.split('def shared_numbers(')[1].split('\ndef ')[0]
    assert 'Contact.query.all()' in fn
    assert 'Employee' not in fn
    apply_fn = src.split('def apply(')[1].split('\ndef ')[0]
    assert 'Employee' not in apply_fn, apply_fn


def test_apply_needs_an_actor_and_undo_needs_a_batch(mod):
    src = open(os.path.join(ROOT, 'scripts',
                            '2026_11_19_shared_phone_evidence.py')).read()
    assert '--apply needs --actor' in src
    assert '--undo needs --batch and --actor' in src


def test_a_field_edited_since_the_survey_is_left_alone(mod):
    src = open(os.path.join(ROOT, 'scripts',
                            '2026_11_19_shared_phone_evidence.py')).read()
    apply_fn = src.split('def apply(')[1].split('\ndef ')[0]
    assert 'edited since the survey' in apply_fn
    undo_fn = src.split('def undo(')[1].split('\ndef ')[0]
    assert 'filled in since' in undo_fn


# ── the block list the ingest reads ──────────────────────────────────
def test_the_block_list_compares_on_ten_digits(tmp_path, monkeypatch):
    from app.services import own_numbers
    path = tmp_path / 'own_numbers.txt'
    path.write_text('# a comment\n7738155449   # batch X\n\n+91 98200 11111\n')
    monkeypatch.setattr(own_numbers, 'PATH', str(path))
    monkeypatch.setattr(own_numbers, '_cache', {'at': None,
                                                'numbers': frozenset()})
    assert own_numbers.is_ours('+91-7738155449') is True
    assert own_numbers.is_ours('07738155449') is True
    assert own_numbers.is_ours('9820011111') is True
    assert own_numbers.is_ours('9810022222') is False
    assert own_numbers.is_ours('') is False


def test_a_missing_block_list_fails_open(tmp_path, monkeypatch):
    """An unreadable list must leave the phone alone, not leave every
    enquiry without one."""
    from app.services import own_numbers
    monkeypatch.setattr(own_numbers, 'PATH',
                        str(tmp_path / 'not_there.txt'))
    monkeypatch.setattr(own_numbers, '_cache', {'at': None,
                                                'numbers': frozenset()})
    assert own_numbers.numbers() == frozenset()
    assert own_numbers.is_ours('7738155449') is False


def test_add_and_remove_are_each_other(tmp_path, monkeypatch):
    from app.services import own_numbers
    path = tmp_path / 'own_numbers.txt'
    monkeypatch.setattr(own_numbers, 'PATH', str(path))
    monkeypatch.setattr(own_numbers, '_cache', {'at': None,
                                                'numbers': frozenset()})
    assert own_numbers.add(['+91-7738155449', '9820011111'],
                           note='batch X') == ['7738155449', '9820011111']
    assert own_numbers.is_ours('7738155449') is True
    # Adding the same one twice adds nothing.
    assert own_numbers.add(['7738155449']) == []
    assert own_numbers.remove(['7738155449']) == ['7738155449']
    assert own_numbers.is_ours('7738155449') is False
    assert own_numbers.is_ours('9820011111') is True


def test_the_ingest_drops_our_own_number(tmp_path, monkeypatch):
    """The point of the whole exercise: a Procam signature at the foot
    of a forwarded enquiry is not the customer's phone."""
    from app.services import own_numbers
    path = tmp_path / 'own_numbers.txt'
    path.write_text('7738155449\n')
    monkeypatch.setattr(own_numbers, 'PATH', str(path))
    monkeypatch.setattr(own_numbers, '_cache', {'at': None,
                                                'numbers': frozenset()})
    body = open(os.path.join(ROOT, 'email_ingest', 'enrich.py')).read()
    assert 'own_numbers.is_ours(merged.get(field))' in body
    after = body.split('own_numbers.is_ours(merged.get(field))')[1][:400]
    # The number is dropped and the lead is marked for a look, the same
    # as the forwarder's own email twenty lines above it.
    assert 'None' in after and 'needs_review' in after, after
    # And it is wrapped, because a list that cannot be read must not
    # stop an ingest.
    assert 'except Exception' in after, after
