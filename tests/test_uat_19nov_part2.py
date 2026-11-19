"""The second half of the 19 Nov UAT.

  5  the country survey covers lead PICs. Mikko Lindroos is the person
     on lead #12168 and is not in the contacts table at all, so no
     amount of reading that table was going to find him.
  6  a phone that is one of OUR numbers is not evidence about where a
     customer sits. With it set aside, Ville Ekholm's `.fi` domain is
     the only evidence and he is in Finland rather than a conflict.
  7  --trace says where one number appears, so a person can decide;
     --mark-ours adds it to the list by hand, audited.
  8  contacts filed under the placeholder company "New Lead", and the
     pairs that are one person twice with the email in two cases.
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
def country():
    return __import__('2026_11_18_country_vs_domain')


@pytest.fixture
def newlead():
    return __import__('2026_11_19_new_lead_contacts')


class Row:
    """A stand-in for a contact or a lead: both carry these columns."""
    def __init__(self, **kw):
        self.email = self.email2 = None
        self.phone = self.phone2 = self.mobile = self.alt_phone = None
        self.name = self.pic = self.country = None
        for k, v in kw.items():
            setattr(self, k, v)


# ── 5: lead PICs are read too ────────────────────────────────────────
def test_the_survey_reads_contacts_and_leads(country):
    src = open(os.path.join(ROOT, 'scripts',
                            '2026_11_18_country_vs_domain.py')).read()
    fn = src.split('def _sources(')[1].split('\ndef ')[0]
    assert "'contact', Contact.query" in fn
    assert "'lead', (Lead.query" in fn
    # A lead with nobody on it is not a person, and an archived or junked
    # one is not worth correcting.
    for guard in ('is_archived', 'junk_deleted', 'merged_into_id',
                  'Lead.pic'):
        assert guard in fn, guard


def test_a_lead_pic_is_named_by_its_own_column(country):
    assert country.name_of(Row(pic='Mikko Lindroos')) == 'Mikko Lindroos'
    assert country.name_of(Row(name='Ville Ekholm')) == 'Ville Ekholm'


def test_a_lead_carries_two_addresses(country):
    said = country.emails_of(Row(email='a@one.fi', email2='b@two.de'))
    assert said == ['a@one.fi', 'b@two.de']
    # and a blank one is not an address
    assert country.emails_of(Row(email='', email2=None)) == []


def test_a_finnish_lead_pic_reads_as_finland(country):
    row = Row(pic='Mikko Lindroos', email='mikko.lindroos@hacklin.fi',
              country='India')
    said, how, _detail = country.evidence(row)
    assert said == 'Finland'
    assert how == 'the email domain'


# ── 6: our own number is not evidence ────────────────────────────────
def test_our_own_number_is_set_aside(country, tmp_path, monkeypatch):
    from app.services import own_numbers
    path = tmp_path / 'own_numbers.txt'
    path.write_text('7738155449\n')
    monkeypatch.setattr(own_numbers, 'PATH', str(path))
    monkeypatch.setattr(own_numbers, '_cache',
                        {'at': None, 'numbers': frozenset()})

    row = Row(name='Ville Ekholm', email='ville@hacklin.fi',
              phone='+91-7738155449', country='India')
    worth, ours = country.phones_of(row)
    assert worth == [] and ours == ['+91-7738155449']
    # And so the domain is the only evidence: Finland, not a conflict.
    said, how, detail = country.evidence(row)
    assert said == 'Finland', (said, how, detail)
    assert 'set aside' in detail


def test_without_the_list_the_same_row_is_a_conflict(country, tmp_path,
                                                     monkeypatch):
    """The fault as reported: an Indian mobile against a .fi domain read
    as two kinds of evidence disagreeing, so nothing was corrected."""
    from app.services import own_numbers
    monkeypatch.setattr(own_numbers, 'PATH', str(tmp_path / 'empty.txt'))
    monkeypatch.setattr(own_numbers, '_cache',
                        {'at': None, 'numbers': frozenset()})
    row = Row(name='Ville Ekholm', email='ville@hacklin.fi',
              phone='+91-7738155449', country='India')
    said, how, _d = country.evidence(row)
    assert said is None and how == 'disagree'


def test_the_shared_phone_survey_still_sees_our_numbers(country):
    """Item 4's CSV must keep listing them -- a number of ours on a
    customer is exactly what that report is for."""
    src = open(os.path.join(ROOT, 'scripts',
                            '2026_11_18_country_vs_domain.py')).read()
    fn = src.split('def shared_phones(')[1].split('\ndef ')[0]
    assert 'drop_ours=False' in fn, fn


# ── 7: trace and mark-ours ───────────────────────────────────────────
def test_trace_and_mark_ours_exist_and_are_gated():
    src = open(os.path.join(ROOT, 'scripts',
                            '2026_11_19_shared_phone_evidence.py')).read()
    assert 'def trace(' in src and 'def mark_ours(' in src
    assert '--mark-ours needs --actor' in src
    # Trace changes nothing, so it needs no actor and no batch.
    fn = src.split('def trace(')[1].split('\ndef ')[0]
    for banned in ('db.session.commit', 'setattr(', 'audit.record'):
        assert banned not in fn, banned


def test_mark_ours_does_not_clear_anybodys_number():
    src = open(os.path.join(ROOT, 'scripts',
                            '2026_11_19_shared_phone_evidence.py')).read()
    fn = src.split('def mark_ours(')[1].split('\ndef ')[0]
    assert 'own_numbers.add' in fn
    assert 'setattr' not in fn
    assert 'Nothing was cleared' in fn


def test_trace_separates_quoted_from_written():
    """The whole point of the earlier fix: a number quoted under a reply
    is not evidence, and the trace has to say which it saw."""
    src = open(os.path.join(ROOT, 'scripts',
                            '2026_11_19_shared_phone_evidence.py')).read()
    fn = src.split('def trace(')[1].split('\ndef ')[0]
    assert 'unquoted(body)' in fn
    assert 'NOT evidence' in fn


# ── 8: the placeholder company, and the duplicates ───────────────────
def test_new_lead_is_a_placeholder_and_hacklin_is_not(newlead):
    for said in ('New Lead', 'new lead', 'NEW  LEAD', 'Unknown', '-',
                 'n/a', 'TBD'):
        assert newlead.is_placeholder(said) is True, said
    for said in ('Hacklin', 'Fried Sped GmbH', 'New Leads Ltd',
                 'Leadec India'):
        assert newlead.is_placeholder(said) is False, said


def test_a_company_is_read_out_of_the_domain(newlead):
    assert newlead.company_from_domain('fried-sped.de') == 'Fried Sped'
    assert newlead.company_from_domain('hacklin.fi') == 'Hacklin'
    assert newlead.company_from_domain('indotech.co.in') == 'Indotech'
    # A free mailbox says nothing about a company.
    assert newlead.company_from_domain('gmail.com') == ''
    assert newlead.company_from_domain('') == ''


def test_only_the_same_name_and_the_same_address_are_one_person(newlead):
    src = open(os.path.join(ROOT, 'scripts',
                            '2026_11_19_new_lead_contacts.py')).read()
    fn = src.split('def duplicate_pairs(')[1].split('\ndef ')[0]
    # Name AND email, both squeezed; and a record with no address is
    # never merged, because every blank would equal every other blank.
    assert 'squeeze(c.name), squeeze(c.email)' in fn
    assert 'if not name or not email' in fn
    assert "'@' not in email" in fn


def test_the_older_record_is_the_one_kept(newlead):
    src = open(os.path.join(ROOT, 'scripts',
                            '2026_11_19_new_lead_contacts.py')).read()
    fn = src.split('def duplicate_pairs(')[1].split('\ndef ')[0]
    assert "group.sort(key=lambda c: (getattr(c, 'created_at', None)" in fn
    assert 'keep = group[0]' in fn


def test_nothing_is_ever_deleted(newlead):
    src = open(os.path.join(ROOT, 'scripts',
                            '2026_11_19_new_lead_contacts.py')).read()
    fn = src.split('def apply_merge(')[1].split('\ndef ')[0]
    assert 'fold.is_active = False' in fn
    for banned in ('db.session.delete', 'DELETE FROM', '.delete()'):
        assert banned not in fn, banned
    # And the record says what it became.
    assert 'Merged into contact #' in fn


def test_the_links_are_read_from_the_schema_not_a_list(newlead):
    """A table added next month must move too, rather than being
    silently left pointing at a deactivated record."""
    src = open(os.path.join(ROOT, 'scripts',
                            '2026_11_19_new_lead_contacts.py')).read()
    fn = src.split('def _link_tables(')[1].split('\ndef ')[0]
    assert 'db.metadata.sorted_tables' in fn
    assert "fk.column.table.name == 'contacts'" in fn


def test_every_table_pointing_at_contacts_is_found(newlead):
    from app import app as flask_app
    with flask_app.app_context():
        found = set(newlead._link_tables())
    # The six that exist today. The assertion is that none is MISSING;
    # a new one appearing here is the mechanism working.
    for pair in (('account_activities', 'contact_id'),
                 ('contact_assignments', 'contact_id'),
                 ('contact_relationships', 'contact_id'),
                 ('project_contacts', 'contact_id'),
                 ('business_card_imports', 'linked_contact_id'),
                 ('business_card_imports', 'created_contact_id')):
        assert pair in found, pair


def test_the_merge_and_the_fill_are_separate_commands(newlead):
    src = open(os.path.join(ROOT, 'scripts',
                            '2026_11_19_new_lead_contacts.py')).read()
    main = src.split('def main(')[1]
    assert 'if a.apply and a.merge:' in main
    assert 'elif a.apply:' in main
    assert '--apply needs --actor' in src
    assert '--undo needs --batch and --actor' in src
