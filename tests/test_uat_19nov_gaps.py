"""The four things the 19 Nov UAT asked for.

  1  Lead #10029 "Procam Forward" on procamadmin@procamgroup.in is us,
     not a customer. Out of Find a person AND out of the duplicate
     check -- it was only ever filtered out of the first.
  2  the account form defaulted Country to India as well.
  3  the country backfill reads the dialling code, not only the domain.
     +358 was not in the dial-code table at all, which is why a Hacklin
     contact on a Finnish mobile stayed Indian.
  4  one phone on contacts of two companies is reported, never changed.
"""
import os
import re
import sys

os.environ.setdefault('SECRET_KEY', 'test-only-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'Test-123456789')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── 1: us is not a customer person ───────────────────────────────────
def test_our_own_domains_are_all_four_of_them():
    from app.services import own_people
    held = own_people.own_domains()
    for said in ('procamgroup.in', 'procamlogistics.com',
                 'procamlogitech.com', 'orsayinternational.com'):
        assert said in held, said


def test_the_older_lists_are_read_rather_than_copied():
    """A domain added to mail_signals is ours here too, with no second
    edit. That is the whole reason this module reads them."""
    from app.services import mail_signals, own_people
    for said in mail_signals.OUR_DOMAINS:
        assert said.lower() in own_people.own_domains(), said


def test_an_address_on_our_domain_is_us():
    from app.services.own_people import is_own_address as f
    assert f('procamadmin@procamgroup.in') is True
    assert f('anyone@procamlogitech.com') is True
    assert f('x@mail.procamgroup.in') is True          # a subdomain is ours
    assert f('sales@hacklin.fi') is False
    # The tail is matched with a dot, so a lookalike domain is not ours.
    assert f('x@notprocamgroup.in') is False


def test_a_pic_named_procam_is_us_whatever_the_address():
    from app.services.own_people import is_own_name as f
    assert f('Procam Forward') is True
    assert f('Procam Admin') is True
    assert f('Orsay Desk') is True
    assert f('Alina Kovalenko') is False
    # Whole first word only.
    assert f('Procamo Nair') is False


def test_lead_10029_is_not_a_person_in_the_search():
    from app.services.people_search import not_a_customer_person as f
    row = {'kind': 'lead', 'person': 'Procam Forward', 'company': 'Unknown',
           'emails': ['procamadmin@procamgroup.in'], 'phones': []}
    assert 'our own domain' in f(row)


def test_a_real_customer_is_still_a_person():
    from app.services.people_search import not_a_customer_person as f
    for row in (
        {'kind': 'contact', 'person': 'Ravi Nistala', 'company': 'SBM',
         'emails': ['ravi.s@sbmapl.com'], 'phones': ['9958248203']},
        {'kind': 'lead', 'person': 'Alina Kovalenko', 'company': 'Hacklin',
         'emails': ['alina@hacklin.fi'], 'phones': ['+358401234567']},
    ):
        assert f(row) == '', (row['person'], f(row))


def test_the_duplicate_check_uses_the_same_predicate():
    """It had its own `_is_a_robot` filter, so a row hidden from the
    results was still offered as somebody to merge with."""
    body = open(os.path.join(ROOT, 'app', 'services',
                             'people_search.py')).read()
    inside = body.split('def duplicates_of(')[1]
    assert inside.count('not_a_customer_person') >= 2, (
        'the duplicate check is not using the shared predicate')
    assert '_is_a_robot(r)' not in inside, (
        'the duplicate check still has a filter of its own')


# ── 2: the account form ──────────────────────────────────────────────
def test_the_account_form_does_not_default_to_india():
    body = open(os.path.join(ROOT, 'templates', 'app.html')).read()
    field = re.search(r'<input[^>]*id="naCty"[^>]*>', body)
    assert field, 'the account Country field has moved'
    assert 'value="India"' not in field.group(0), field.group(0)
    assert 'required' in field.group(0), field.group(0)


def test_the_account_form_asks_before_it_posts():
    body = open(os.path.join(ROOT, 'templates', 'app.html')).read()
    fn = body.split('async function acctSubmit(')[1][:2000]
    assert "g('naCty').value.trim()" in fn, fn[:300]
    # Refused in the browser, before the POST, so the person is told
    # which box rather than being handed a server error.
    assert fn.index('naCty') < fn.index('/api/accounts'), fn[:600]


# ── 3: the dialling code is evidence ─────────────────────────────────
def test_finland_is_in_the_dial_codes_now():
    from app.services.people_search import country_from_phone as f
    assert f('+358 40 123 4567') == 'Finland'
    assert f('+86 139 1234 5678') == 'China'
    assert f('+49 170 1234567') == 'Germany'
    assert f('+971 50 123 4567') == 'United Arab Emirates'


def test_a_bare_indian_number_is_still_not_evidence():
    """Every number in this book looks like a ten-digit Indian mobile.
    Reading India into one would make the filter agree with itself."""
    from app.services.people_search import country_from_phone as f
    assert f('9958248203') == ''
    assert f('77381 55449') == ''


def test_no_dial_code_is_a_guess_between_two_countries():
    """+7 is Russia and Kazakhstan both. The table's own rule is that an
    unknown code is left blank rather than guessed, so a prefix that
    cannot decide must not be in it."""
    from app.services.people_search import DIAL_CODES
    assert '7705' not in DIAL_CODES
    for code in DIAL_CODES:
        assert code.isdigit(), code


def test_two_kinds_of_evidence_that_disagree_change_nothing():
    sys.path.insert(0, os.path.join(ROOT, 'scripts'))
    mod = __import__('2026_11_18_country_vs_domain')

    class C:
        email = 'x@zwei.de'            # Germany
        phone = '+358 40 999 0000'     # Finland
        phone2 = mobile = alt_phone = None

    country, how, detail = mod.evidence(C())
    assert country is None
    assert how == 'disagree'
    assert 'left alone' in detail


def test_a_dot_com_is_not_a_country_but_its_phone_still_is():
    sys.path.insert(0, os.path.join(ROOT, 'scripts'))
    mod = __import__('2026_11_18_country_vs_domain')

    class C:
        email = 'raki@hacklingroup.com'
        phone = '+86 139 1234 5678'
        phone2 = mobile = alt_phone = None

    country, how, _d = mod.evidence(C())
    assert country == 'China'
    assert how == 'the dialling code'


# ── 4: shared phones are reported, never written ─────────────────────
def test_the_apply_path_never_touches_a_shared_phone():
    body = open(os.path.join(ROOT, 'scripts',
                             '2026_11_18_country_vs_domain.py')).read()
    fn = body.split('def apply(')[1].split('\ndef ')[0]
    for said in ('phone', 'shared'):
        assert f'row.{said}' not in fn, said
    # The only column it writes is the country.
    assert fn.count('row.country = want') == 1, fn


def test_the_survey_scripts_carry_an_actor_and_an_undo():
    body = open(os.path.join(ROOT, 'scripts',
                             '2026_11_18_country_vs_domain.py')).read()
    assert "--apply needs --actor" in body
    assert "--undo needs --batch and --actor" in body
    assert 'def undo(' in body


def test_the_procam_pic_survey_has_no_apply_at_all():
    body = open(os.path.join(ROOT, 'scripts',
                             '2026_11_19_procam_pic_leads.py')).read()
    assert "add_argument('--apply'" not in body
    assert 'READ ONLY' in body
