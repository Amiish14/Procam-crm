"""Find a person — UAT fixes of 17/18 Nov (CRM-SEARCH-SCAN-01).

1 an email or phone in the Name box finds the person, contact or PIC
2 a result opens the record, not the /crm2 redirect
4 the same name at the same company, with nothing contradicting it,
  is one row
5 a name that matches from the start ranks above one that only
  contains what was typed
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_contact_search_15nov import (  # noqa: E402,F401
    _account, _clean, _client, _contact, _lead, flask_app, ps)


def test_an_email_in_the_name_box_finds_the_contact():
    _contact(name='ZT Ravi Nistala', email='ravi@ztopterix.example',
             mobile='+91-9958200001')
    found = ps.search(name='ravi@ztopterix.example')
    assert [r['person'] for r in found['rows']] == ['ZT Ravi Nistala']


def test_a_phone_in_the_name_box_finds_the_contact():
    _contact(name='ZT Ravi Nistala', email='ravi@ztopterix.example',
             mobile='+91-9958200001')
    for typed in ('9958200001', '+91 99582 00001', '99582000'):
        found = ps.search(name=typed)
        assert [r['person'] for r in found['rows']] == ['ZT Ravi Nistala'], typed


def test_a_lead_pic_is_found_by_email_and_by_phone():
    _lead(pic='ZT Ravi Sonkar', email='ravi.s@ztsbm.example',
          phone='+91 02836 269113')
    assert ps.search(name='ravi.s@ztsbm.example')['total'] == 1
    assert ps.search(name='02836 269113')['total'] == 1


def test_a_name_is_still_a_name():
    assert ps.query_kind('Ravi') == 'name'
    assert ps.query_kind('a@b.in') == 'email'
    assert ps.query_kind('+91 98200 11111') == 'phone'
    assert ps.query_kind('12345') == 'name'      # too short to be a phone


def test_the_result_opens_the_record_not_the_landing_redirect():
    client = _client()
    page = client.get('/contacts/search').get_data(as_text=True)
    assert 'r.person_url' in page
    assert "const where = r.lead_url || r.url" not in page
    contact = _contact(name='ZT Asha Link', email='asha@ztlink.example')
    lead = _lead(pic='ZT Lead Only', email='lo@ztlink.example')
    rows = {r['person']: r for r in ps.search(name='ZT ')['rows']}
    assert rows['ZT Asha Link']['person_url'] == f'/app?contact={contact.id}'
    assert rows['ZT Lead Only']['person_url'] == f'/app?lead={lead.id}'


def test_one_person_on_a_contact_and_two_bare_leads_is_one_row():
    """Alina Kovalenko, UAT 18 Nov: shown three times."""
    _contact(name='ZT Alina Kova', company='ZT Hacklin',
             email='alina@zthacklin.example')
    _lead(pic='ZT Alina Kova', company='ZT Hacklin Logistics Ltd')
    _lead(pic='ZT Alina Kova', company='Oy ZT Hacklin Logistics Ltd.')
    found = ps.search(name='ZT Alina')
    assert found['total'] == 1, [(r['person'], r['company'])
                                 for r in found['rows']]
    row = found['rows'][0]
    assert row['kind'] == 'contact'
    assert row['source_note'] == 'Contact · also on 2 leads'


def test_same_name_with_different_emails_stays_two_people():
    _contact(name='ZT Rajesh Kumar', company='ZT Hacklin',
             email='rajesh1@zthacklin.example')
    _lead(pic='ZT Rajesh Kumar', company='ZT Hacklin Logistics',
          email='rajesh2@zthacklin.example')
    assert ps.search(name='ZT Rajesh')['total'] == 2


def test_same_name_at_another_company_stays_two_people():
    _contact(name='ZT Mohan Das', company='ZT Alpha Steel')
    _lead(pic='ZT Mohan Das', company='ZT Beta Cement')
    assert ps.search(name='ZT Mohan')['total'] == 2


def test_a_name_that_starts_with_what_was_typed_ranks_first():
    """"Nis": Ravi Nistala was 7th of 7, below Manish ×3 and Manisha."""
    for name in ('ZT Manish Kumar', 'ZT Karishma Daanish', 'ZT Nisha Kayat'):
        _contact(name=name)
    _contact(name='ZTNis Exact')
    found = ps.search(name='ZT Nis')
    assert found['rows'][0]['person'] == 'ZT Nisha Kayat'
    assert ps.name_rank('nis', 'Ravi Nistala') == 2
    assert ps.name_rank('nis', 'Manish Kumar') == 3
    assert ps.name_rank('nis', 'Nisha Kayat') == 1


def test_a_column_sort_is_still_honoured():
    for name in ('ZT B Nis', 'ZT A Xnis'):
        _contact(name=name)
    found = ps.search(name='nis', sort='name', descending=True)
    names = [r['person'] for r in found['rows'] if r['person'].startswith('ZT')]
    assert names == sorted(names, key=str.lower, reverse=True)


def test_the_new_contact_form_checks_when_the_email_or_phone_is_left():
    """Fix 6: the warning appears before Save, from the same check."""
    client = _client()
    page = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'templates', 'app.html')).read()
    assert 'id="ctE" type="email" onblur="ctLiveCheck()"' in page
    assert 'id="ctP" onblur="ctLiveCheck()"' in page
    assert "/api/contacts/search/duplicates?" in page
    contact = _contact(name='ZT Ravi Nistala', mobile='+91-9958200001')
    body = client.get('/api/contacts/search/duplicates?phone=9958200001'
                      '&name=ZT%20Ravi%20Nistala').get_json()
    assert body['ok'] and body['rows'][0]['contact_id'] == contact.id
