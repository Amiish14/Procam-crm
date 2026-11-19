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
import re
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


# ── the sender's own part, not the thread under it ───────────────────
#
# The 19 Nov follow-up: 0 OURS, because test (c) read the whole body.
# Our signature quoted beneath a customer's reply counted as evidence
# that the number was the customer's, so 8800394614 came out CUSTOMER
# for companies in Zagreb, Korea, Spain and Japan at once.
@pytest.mark.parametrize('tail', [
    '\nOn Wed, 12 Nov 2026 at 14:02, A <a@b.c> wrote:\n> 8800394614',
    '\n-----Original Message-----\nFrom: A <a@b.c>\n8800394614',
    '\n________________________________\nFrom: A\n8800394614',
    '\nFrom: Procam <x@procamgroup.in>\nSent: Wed\n8800394614',
    '\n> 8800394614',
    '\n---------- Forwarded message ----------\n8800394614',
    '\nBegin forwarded message:\n8800394614',
])
def test_every_quote_marker_cuts_the_thread_off(mod, tail):
    body = 'Our rate is attached.\nRegards\nSomebody\n' + tail
    own = mod.unquoted(body)
    assert '8800394614' not in re.sub(r'\D', '', own), own


def test_the_senders_own_signature_is_kept(mod):
    """It has to be: our number in our own signature is evidence (a).
    A stripper that cut at "Sent from my" or "Best regards" would
    delete the very thing being looked for."""
    body = ('Dear Sir, our rate is attached.\n'
            'Best regards\nProcam Logistics\n'
            'Sent from my iPhone\n'
            'Mob +91 88003 94614\n'
            '\nOn Wed, 12 Nov 2026 at 14:02, X <x@y.z> wrote:\n> old\n')
    own = mod.unquoted(body)
    assert '8800394614' in re.sub(r'\D', '', own)


def test_a_body_with_no_quote_is_kept_whole(mod):
    body = 'First enquiry. Please call 98200 11111.'
    assert mod.unquoted(body) == body


def test_an_empty_body_is_empty(mod):
    assert mod.unquoted('') == ''
    assert mod.unquoted(None) == ''


# ── company groups ──────────────────────────────────────────────────
def test_one_group_trading_under_two_domains_is_one_group(mod):
    """jupiterwagons.com and jupitertatravagonka.com share no domain and
    are one customer. The first distinguishing word is what joins
    them."""
    a = mod.company_group_keys('Jupiter Wagons', 'e@jupiterwagons.com')
    b = mod.company_group_keys('Jupiter Tatravagonka',
                               'f@jupitertatravagonka.com')
    assert a & b


def test_the_same_name_twice_is_one_group(mod):
    for one, two in (
        (('Indo Tech Transformers', 'g@indotech.com'),
         ('Indo Tech', 'h@indotech.co.in')),
        (('Vedanta Ltd', 'a@vedanta.co.in'),
         ('Vedanta Aluminium', 'b@vedantaalum.com')),
    ):
        assert mod.company_group_keys(*one) & mod.company_group_keys(*two), \
            (one, two)


def test_unrelated_companies_share_nothing(mod):
    keys = [mod.company_group_keys(co, em) for co, em in (
        ('Agentplus', 'a@agentplus.hr'),
        ('Hyupjin', 'b@hyupjin.co.kr'),
        ('Modal Trade', 'c@modaltrade.es'),
        ('Voith Fuji', 'd@voith-fuji.jp'))]
    for i, one in enumerate(keys):
        for two in keys[i + 1:]:
            assert not (one & two), (one, two)


def test_a_legal_suffix_is_not_the_distinguishing_word(mod):
    """"Pvt Ltd" and "Logistics" must not group two companies: half the
    book would become one group and every number would read as ours."""
    a = mod.company_group_keys('Alpha Logistics Pvt Ltd', 'a@alpha.com')
    b = mod.company_group_keys('Beta Logistics Pvt Ltd', 'b@beta.com')
    assert not (a & b), (a, b)


def test_a_free_mailbox_groups_nobody(mod):
    """Two contacts on gmail are not one company."""
    a = mod.company_group_keys('', 'someone@gmail.com')
    b = mod.company_group_keys('', 'another@gmail.com')
    assert a == set() and b == set()


def test_three_unrelated_groups_without_customer_evidence_is_ours(mod):
    src = open(os.path.join(ROOT, 'scripts',
                            '2026_11_19_shared_phone_evidence.py')).read()
    assert 'unrelated >= 3 and not in_theirs' in src
    # And the rule is only reached when (c) is NOT met, so a genuine
    # shared line quoted by its owner stays theirs.
    assert src.index('(in_ours or on_staff) and not in_theirs') < \
        src.index('unrelated >= 3 and not in_theirs')


def test_the_summary_comes_before_the_lists(mod):
    src = open(os.path.join(ROOT, 'scripts',
                            '2026_11_19_shared_phone_evidence.py')).read()
    main = src.split('def main(')[1]
    assert main.index("shared number(s):") < main.index('── {verdict}')
    assert "for verdict in (OURS, CUSTOMER, UNCLEAR)" in main
    assert "for verdict in (OURS, UNCLEAR)" in main
