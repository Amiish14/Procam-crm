"""Find a person — CRM-SEARCH-SCAN-01 Part 1.

THE DECISION THIS IS BUILT ON

667 contacts against 12,000 leads carrying a PIC, an email, a phone, a
company and a vertical. A contact search that reads the `contacts`
table answers "we don't know them" about somebody we quoted last week,
so a result row is a PERSON assembled from both and de-duplicated on
email and phone.

Six filters, ANDed, each reading several columns. Two of them are
deliberately different questions: the VERTICAL is which Procam desk
handles the work (`Lead.procam_vertical`, `Company.vertical`) and the
INDUSTRY is what the customer does (`Contact.industry`,
`Company.industry`) -- `app.py:1294` says so, and a screen that merged
them would answer neither.
"""
import importlib
import os
import sys
import tempfile
import time
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.modules.setdefault('msal', types.ModuleType('msal'))

os.environ['URL_PREFIX'] = ''
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'findperson')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'FindPerson-12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'findperson.db')

import pytest                                              # noqa: E402

_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Company, Contact, Employee, Lead = (_main.Company, _main.Contact,
                                    _main.Employee, _main.Lead)

from app.services import people_search as ps                # noqa: E402
from app.services import vertical_master as vm              # noqa: E402

ADMIN = 'ZT_ADM'


def _admin():
    row = Employee.query.filter_by(emp_code=ADMIN).first()
    if row is None:
        row = Employee(emp_code=ADMIN, name='Admin', password_hash='x',
                       role='admin', is_active=True,
                       email='zt.adm@procamlogistics.com')
        db.session.add(row)
        db.session.commit()
    return row


def _client():
    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess['emp_code'] = ADMIN
        sess['role'] = 'admin'
    return client


def _token(client):
    for header in client.get('/app').headers.getlist('Set-Cookie'):
        if header.startswith('csrf_token='):
            return header.split('=', 1)[1].split(';')[0]
    return ''


def _purge():
    Lead.query.filter(db.or_(Lead.company.like('ZT%'),
                             Lead.pic.like('ZT%'))).delete(
        synchronize_session=False)
    Contact.query.filter(db.or_(Contact.name.like('ZT%'),
                                Contact.company.like('ZT%'))).delete(
        synchronize_session=False)
    Company.query.filter(Company.name.like('ZT%')).delete(
        synchronize_session=False)
    db.session.commit()


@pytest.fixture(autouse=True)
def _clean():
    """A request context for the whole test.

    `people_search` asks `app/access/scope.py` who is looking, and
    that reads the session — which is the point: a search without a
    viewer would be a search without visibility rules. So the tests
    run as a signed-in person rather than reaching past it.
    """
    from flask import session

    ctx = flask_app.test_request_context('/')
    ctx.push()
    db.create_all()
    _admin()
    session['emp_code'] = ADMIN
    session['role'] = 'admin'
    _purge()
    yield
    _purge()
    ctx.pop()


def _account(name='ZT Steel Works Limited', **kw):
    kw.setdefault('is_active', True)
    account = Company(name=name, **kw)
    db.session.add(account)
    db.session.commit()
    return account


def _contact(name='ZT Asha Menon', **kw):
    kw.setdefault('assigned_to', ADMIN)
    row = Contact(name=name, **kw)
    db.session.add(row)
    db.session.commit()
    return row


def _lead(pic='ZT Ravi Kumar', **kw):
    kw.setdefault('company', 'ZT Steel Works Limited')
    kw.setdefault('stage', 'New Opportunity')
    kw.setdefault('assigned_to', ADMIN)
    row = Lead(pic=pic, **kw)
    db.session.add(row)
    db.session.commit()
    return row


# ═══ the verticals ═════════════════════════════════════════════════
def test_thirteen_spellings_become_ten_desks():
    for value, want in (
            ('Heavy Transport', 'Heavy Transport'),
            ('heavy transport', 'Heavy Transport'),
            ('HEAVY  TRANSPORT', 'Heavy Transport'),
            ('Heavy transport - North', 'Heavy Transport'),
            ('PFM', 'Project Freight'),
            ('project freight', 'Project Freight'),
            ('project cargo', 'Project Freight'),
            ('FF', 'Freight Forwarding'),
            ('Ocean Freight', 'Sea Freight'),
            ('Air Freight (Export)', 'Air Freight'),
            ('Customs Clearance', 'Customs'),
            ('CHA', 'Customs'),
            ('Heavy Lift', 'Heavy Cargo'),
            ('Warehouse', 'Warehousing'),
            ('installation & commissioning', 'Installation'),
            ('Others', 'Other')):
        assert vm.canonical(value) == want, value


def test_transport_spellings_are_the_heavy_transport_desk():
    """Named on 15 Nov. Neither is an "Other": the rest of the CRM
    (`scripts/vertical_master_apply.py`, `vertical_heads`) has read
    General Transport as Heavy Transport since the master list went
    in, and this was the one place that disagreed."""
    for value in ('Transportation', 'transportation',
                  'General Transport', 'general transport',
                  'GENERAL TRANSPORT', 'General Transportation',
                  'General Transport - South', 'Road Transportation'):
        assert vm.canonical(value) == 'Heavy Transport', value
    # and "General" on its own still cannot be told apart from Other.
    assert vm.canonical('General') == 'Other'


def test_a_desk_it_cannot_place_is_not_filed_under_other():
    """"I don't know which desk this is" and "this is the Other desk"
    are different answers, and a sweep that files the first under the
    second loses the question."""
    for value in ('Marine Insurance', 'Zzz', '', None, '   '):
        assert vm.canonical(value) == ''


def test_the_ten_are_the_ten_agreed():
    assert vm.CANONICAL == (
        'Heavy Transport', 'Project Freight', 'Installation',
        'Warehousing', 'Freight Forwarding', 'Sea Freight',
        'Air Freight', 'Customs', 'Heavy Cargo', 'Other')


# ═══ the search ════════════════════════════════════════════════════
def test_a_lead_pic_is_a_person_in_the_results():
    """The decision this is built on: the contact data is on leads."""
    if True:
        account = _account()
        _lead(pic='ZT Ravi Kumar', company_id=account.id,
              email='ravi@ztsteel.example', phone='+91 98200 11111',
              procam_vertical='heavy transport', city='Mumbai',
              state='Maharashtra')
        found = ps.search(name='ZT Ravi')
        assert found['total'] == 1
        row = found['rows'][0]
        assert row['person'] == 'ZT Ravi Kumar'
        assert row['label'].startswith('From lead #')
        assert row['vertical'] == 'Heavy Transport'   # canonicalised
        assert row['url'].startswith('/?lead=')


def test_one_person_on_a_contact_and_three_leads_is_one_row():
    if True:
        account = _account()
        _contact(name='ZT Asha Menon', company='ZT Steel Works Limited',
                 account_id=account.id, email='asha@ztsteel.example',
                 designation='Head - Logistics', mobile='+91 98200 22222')
        for n in range(3):
            _lead(pic='ZT Asha Menon', company_id=account.id,
                  email='ASHA@ztsteel.example',   # same person, shouting
                  procam_vertical='PFM')
        found = ps.search(name='ZT Asha')
        assert found['total'] == 1, [r['person'] for r in found['rows']]
        row = found['rows'][0]
        # The contact survives, and carries what only it knows.
        assert row['kind'] == 'contact'
        assert row['designation'] == 'Head - Logistics'
        # And the lead it came with is reachable.
        assert row['lead_id']
        assert row['source_note'] == 'Contact · also on 3 leads'


def test_two_people_with_the_same_name_stay_two_people():
    """Merging on a name would hide one of them completely."""
    if True:
        _contact(name='ZT Rajesh Kumar', company='ZT Alpha',
                 email='rajesh@ztalpha.example')
        _contact(name='ZT Rajesh Kumar', company='ZT Beta',
                 email='rajesh@ztbeta.example')
        found = ps.search(name='ZT Rajesh')
        assert found['total'] == 2


def test_a_phone_written_two_ways_is_one_person():
    if True:
        account = _account()
        _contact(name='ZT Priya Nair', company='ZT Steel Works Limited',
                 account_id=account.id, mobile='+91 98200 33333')
        _lead(pic='ZT Priya Nair', company_id=account.id,
              phone='9820033333')
        found = ps.search(name='ZT Priya')
        assert found['total'] == 1


def test_the_filters_are_anded():
    if True:
        account = _account(city='Mumbai', state='Maharashtra',
                           vertical='Heavy Transport', industry='Steel')
        other = _account(name='ZT Cement Works', city='Pune',
                         state='Maharashtra', vertical='Warehousing',
                         industry='Cement')
        _lead(pic='ZT Ravi Kumar', company_id=account.id,
              company='ZT Steel Works Limited', city='Mumbai',
              procam_vertical='Heavy Transport', email='r1@zt.example')
        _lead(pic='ZT Ravi Kumar', company_id=other.id,
              company='ZT Cement Works', city='Pune',
              procam_vertical='Warehousing', email='r2@zt.example')
        assert ps.search(name='ZT Ravi')['total'] == 2
        assert ps.search(name='ZT Ravi', location='Mumbai')['total'] == 1
        assert ps.search(name='ZT Ravi',
                         vertical='Warehousing')['total'] == 1
        # A combination nothing satisfies answers nothing, rather than
        # falling back to either half.
        assert ps.search(name='ZT Ravi', location='Mumbai',
                         vertical='Warehousing')['total'] == 0


def test_vertical_and_industry_are_different_questions():
    if True:
        account = _account(vertical='Heavy Transport', industry='Cement')
        _lead(pic='ZT Ravi Kumar', company_id=account.id,
              procam_vertical='Heavy Transport', email='r@zt.example')
        assert ps.search(vertical='Heavy Transport')['total'] == 1
        assert ps.search(industry='Cement')['total'] == 1
        assert ps.search(vertical='Cement')['total'] == 0
        assert ps.search(industry='Heavy Transport')['total'] == 0


def test_one_spelling_of_the_filter_finds_all_thirteen():
    if True:
        account = _account()
        for n, spelling in enumerate(('Heavy Transport', 'heavy transport',
                                      'HEAVY TRANSPORT')):
            _lead(pic=f'ZT Person {n}', company_id=account.id,
                  procam_vertical=spelling, email=f'p{n}@zt.example')
        assert ps.search(vertical='Heavy Transport')['total'] == 3


def test_company_matches_an_account_alias():
    """Reusing the alias reading built for the portal buyers: "Amns"
    answers to "ArcelorMittal Nippon Steel"."""
    if True:
        account = _account(name='ZT Amns')
        account.aliases = ['ZT ArcelorMittal Nippon Steel']
        db.session.commit()
        _lead(pic='ZT Ravi Kumar', company_id=account.id,
              company='ZT Amns', email='r@zt.example')
        assert ps.search(company='ZT ArcelorMittal')['total'] == 1
        assert ps.search(company='ZT Amns')['total'] == 1


def test_country_falls_back_to_the_dialling_code():
    assert ps.country_from_phone('+971 50 123 4567') == \
        'United Arab Emirates'
    assert ps.country_from_phone('00 44 20 7946 0000') == \
        'United Kingdom'
    assert ps.country_from_phone('+91 98200 00000') == 'India'
    # A local ten-digit number says nothing, and guessing India from it
    # is the fault this exists to work around, not a fix for it.
    assert ps.country_from_phone('9820000000') == ''
    assert ps.country_from_phone('', None) == ''

    if True:
        account = _account(country=None)
        lead = _lead(pic='ZT Gulf Person', company_id=account.id,
                     phone='+971 50 123 4567', email='g@zt.example')
        # `Lead.country` is declared `default='India'`, so a lead
        # NEVER arrives without one — see the test below. Blanked
        # here to exercise the fallback the spec asked for.
        lead.country = ''
        db.session.commit()
        found = ps.search(name='ZT Gulf')
        assert found['rows'][0]['country'] == 'United Arab Emirates'
        assert found['rows'][0]['country_from'] == 'the phone code'
        assert ps.search(country='United Arab')['total'] == 1


def test_location_reads_the_account_and_the_lead():
    if True:
        account = _account(city='Chennai', state='Tamil Nadu')
        _contact(name='ZT Account Person', account_id=account.id,
                 email='ap@zt.example')          # no city of its own
        _lead(pic='ZT Lead Person', company_id=account.id,
              city='Hosur', state='Tamil Nadu', email='lp@zt.example')
        # The account's address counts for both of them: "who do we
        # know in Chennai" means the company's address as much as the
        # person's, and a contact often carries no city at all.
        assert ps.search(location='Chennai')['total'] == 2
        # The lead's own city is its own answer.
        assert ps.search(location='Hosur')['total'] == 1
        assert ps.search(location='Tamil Nadu')['total'] == 2


def test_nothing_is_returned_for_an_empty_search():
    if True:
        _contact(name='ZT Somebody', email='s@zt.example')
        client = _client()
        body = client.get('/api/contacts/search').get_json()
        assert body['ok'] is True
        assert body['rows'] == []
        assert 'at least one field' in body['note']


def test_a_person_outside_the_viewers_scope_is_not_shown():
    """A search box is the easiest place in a CRM to leak a record."""
    if True:
        _admin()
        somebody = Employee(emp_code='ZT_USR', name='User',
                            password_hash='x', role='user',
                            is_active=True,
                            email='zt.usr@procamlogistics.com')
        db.session.add(somebody)
        account = _account()
        _lead(pic='ZT Hidden Person', company_id=account.id,
              assigned_to='ZT_OTHER', email='hidden@zt.example')
        db.session.commit()

    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess['emp_code'] = 'ZT_USR'
        sess['role'] = 'user'
    body = client.get('/api/contacts/search?name=ZT Hidden').get_json()
    assert body['ok'] is True
    assert body['total'] == 0, body['rows']

    # And the admin, who may see everything, does see it.
    body = _client().get('/api/contacts/search?name=ZT Hidden').get_json()
    assert body['total'] == 1

    if True:
        Employee.query.filter_by(emp_code='ZT_USR').delete()
        db.session.commit()


def test_the_page_and_the_api_answer():
    if True:
        account = _account()
        _lead(pic='ZT Ravi Kumar', company_id=account.id,
              email='r@zt.example', procam_vertical='PFM')
    client = _client()
    page = client.get('/contacts/search')
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    for field in ('name', 'company', 'country', 'location', 'vertical',
                  'industry'):
        assert f'id="{field}"' in html
    body = client.get('/api/contacts/search?name=ZT Ravi&sort=company'
                      '&page=1').get_json()
    assert body['ok'] is True
    assert body['total'] == 1
    assert body['rows'][0]['vertical'] == 'Project Freight'
    assert 'took_ms' in body


def test_the_results_page_and_sort():
    if True:
        account = _account()
        for name in ('ZT Carol', 'ZT Alice', 'ZT Bob'):
            _contact(name=name, account_id=account.id,
                     email=f'{name[3:].lower()}@zt.example')
        got = ps.search(name='ZT ', sort='name')
        assert [r['person'] for r in got['rows']] == \
            ['ZT Alice', 'ZT Bob', 'ZT Carol']
        back = ps.search(name='ZT ', sort='name', descending=True)
        assert [r['person'] for r in back['rows']] == \
            ['ZT Carol', 'ZT Bob', 'ZT Alice']
        paged = ps.search(name='ZT ', per_page=2, page=2)
        assert paged['pages'] == 2 and len(paged['rows']) == 1


def test_the_type_ahead_offers_what_the_data_holds():
    if True:
        _account(name='ZT Steel Works Limited', city='Vadodara',
                 industry='Steel')
        assert 'ZT Steel Works Limited' in ps.suggest('company', 'ZT Steel')
        assert 'Vadodara' in ps.suggest('location', 'Vad')
        assert 'Steel' in ps.suggest('industry', 'Ste')
        # The vertical list is the master list, not whatever is in the
        # data: that is the point of normalising it.
        assert sorted(ps.suggest('vertical', 'Heavy')) == \
            ['Heavy Cargo', 'Heavy Transport']


def test_a_search_of_this_book_is_quick():
    """A number rather than an adjective. The live book is 667
    contacts and 12,000 leads; this is a tenth of it, and the shape of
    the cost is linear."""
    if True:
        account = _account()
        db.session.add_all([
            Lead(pic=f'ZT Person {n}', company='ZT Steel Works Limited',
                 company_id=account.id, stage='New Opportunity',
                 email=f'p{n}@zt.example', assigned_to=ADMIN,
                 procam_vertical='Heavy Transport')
            for n in range(1200)])
        db.session.commit()
        started = time.time()
        found = ps.search(name='ZT Person', vertical='Heavy Transport')
        took = time.time() - started
    assert found['total'] == 1200
    assert took < 1.0, f'{took:.2f}s'


def test_a_capped_search_says_the_count_is_a_floor():
    """A count that is quietly wrong is worse than a slow search."""
    assert ps.CAP >= 1000
    if True:
        account = _account()
        db.session.add_all([
            Lead(pic=f'ZT Many {n}', company='ZT Steel Works Limited',
                 company_id=account.id, stage='New Opportunity',
                 email=f'm{n}@zt.example', assigned_to=ADMIN)
            for n in range(ps.CAP + 10)])
        db.session.commit()
        found = ps.search(name='ZT Many')
    assert found['capped'] is True
    assert 'floor' in found['capped_note']


# ═══ no silent duplicates ══════════════════════════════════════════
def test_a_new_contact_that_already_exists_is_refused_with_the_matches():
    if True:
        account = _account()
        _contact(name='ZT Asha Menon', company='ZT Steel Works Limited',
                 account_id=account.id, email='asha@ztsteel.example',
                 mobile='+91 98200 22222')
    client = _client()
    r = client.post('/api/contacts',
                    json={'name': 'ZT Asha Menon',
                          'company': 'ZT Steel Works Limited',
                          'email': 'asha@ztsteel.example'},
                    headers={'X-CSRFToken': _token(client)})
    assert r.status_code == 409, r.get_data(as_text=True)
    body = r.get_json()
    assert body['needs'] == 'duplicate_check'
    assert body['duplicates']
    assert body['duplicates'][0]['why'] == 'the same email address'
    if True:
        assert Contact.query.filter_by(name='ZT Asha Menon').count() == 1


def test_a_second_record_can_still_be_made_on_purpose():
    if True:
        _contact(name='ZT Asha Menon', email='asha@ztsteel.example')
    client = _client()
    r = client.post('/api/contacts',
                    json={'name': 'ZT Asha Menon',
                          'email': 'asha@ztsteel.example',
                          'use_existing': True},
                    headers={'X-CSRFToken': _token(client)})
    assert r.status_code == 200, r.get_data(as_text=True)
    if True:
        assert Contact.query.filter_by(name='ZT Asha Menon').count() == 2


def test_a_genuinely_new_person_is_not_refused():
    if True:
        _contact(name='ZT Asha Menon', email='asha@ztsteel.example')
    client = _client()
    r = client.post('/api/contacts',
                    json={'name': 'ZT Vikram Shah',
                          'email': 'vikram@ztother.example'},
                    headers={'X-CSRFToken': _token(client)})
    assert r.status_code == 200, r.get_data(as_text=True)


def test_the_duplicate_check_finds_a_person_who_is_only_on_a_lead():
    if True:
        account = _account()
        _lead(pic='ZT Ravi Kumar', company_id=account.id,
              email='ravi@ztsteel.example', phone='+91 98200 11111')
        found = ps.duplicates_of(email='RAVI@ztsteel.example')
        assert found and found[0]['why'] == 'the same email address'
        by_phone = ps.duplicates_of(phone='9820011111')
        assert by_phone and by_phone[0]['why'] == 'the same phone number'
        by_name = ps.duplicates_of(name='ZT Ravi Kumar',
                                   company='ZT Steel Works Limited')
        assert by_name


def test_the_country_filter_says_when_it_cannot_be_trusted():
    """Every contact carries a country because the create form has
    always defaulted it. A filter that looks authoritative and is not
    is worse than one marked unreliable."""
    if True:
        for n in range(12):
            _contact(name=f'ZT Person {n}', country='India',
                     email=f'p{n}@zt.example')
    body = _client().get('/api/contacts/search/country-health').get_json()
    assert body['ok'] is True
    assert body['total'] >= 12
    assert body['dominant']['count'] >= 12

    # The contract, not the live ratio: this suite shares a database
    # with every other one, so how dominant "India" is depends on who
    # ran first. What must always hold is that a column dominated by
    # one value is reported as untrustworthy AND explained, and that
    # one which is not is reported as usable with nothing to explain.
    # The threshold is the module's, not a number repeated here: it
    # moved from 90 to 80 on 15 Nov and a copy of it in a test is a
    # copy that goes stale between suites.
    if body['dominant']['percent'] >= ps.DOMINANT_AT:
        assert body['trustworthy'] is False
        assert 'defaulted' in body['note']
    else:
        assert body['trustworthy'] is True
        assert body['note'] == ''
    html = _client().get('/contacts/search').get_data(as_text=True)
    assert 'country-warn' in html


def test_the_create_form_is_why_every_contact_has_a_country():
    """Not a guess: `api_create_contact` has always written
    `d.get('country','India')`, so the column is full and says
    nothing. The health endpoint exists because of this line."""
    source = open(os.path.join(_ROOT, 'app.py')).read()
    assert "country      = d.get('country','India')" in source


def test_the_country_column_defaults_on_leads_too():
    """The same trap as the contact form, one table along.

    `Lead.country` is declared `default='India'`, so every lead
    carries a country whether or not anybody stated one — which is
    why the phone-code fallback almost never fires on live data, and
    why the country filter cannot be trusted on leads either. Written
    down here because it is a finding, not a design.
    """
    assert Lead.__table__.c.country.default is not None
    assert Lead.__table__.c.country.default.arg == 'India'
    if True:
        account = _account()
        lead = _lead(pic='ZT Default Person', company_id=account.id,
                     email='d@zt.example')
        assert lead.country == 'India'   # nobody said so


# ═══ the two sweeps ════════════════════════════════════════════════
def _script(name):
    import importlib.util

    path = os.path.join(_ROOT, 'scripts', name)
    spec = importlib.util.spec_from_file_location(
        name.replace('.', '_') + str(time.time_ns()), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_vertical_sweep_proposes_then_writes_then_undoes(capsys):
    mod = _script('2026_11_15_verticals.py')
    if True:
        account = _account(vertical='heavy transport')
        one = _lead(pic='ZT A', company_id=account.id,
                    procam_vertical='PFM', email='a@zt.example')
        two = _lead(pic='ZT B', company_id=account.id,
                    procam_vertical='Marine Insurance',
                    email='b@zt.example')
        found = mod.survey()
        mod.report(found)
        out = capsys.readouterr().out
        assert 'PFM' in out and 'Project Freight' in out
        # The one it will not place is named, and left alone.
        assert 'NOT PLACED' in out
        assert 'Marine Insurance' in out

        batch = mod.next_batch_id()
        mod.apply(batch=batch, actor=ADMIN)
        assert db.session.get(Lead, one.id).procam_vertical == \
            'Project Freight'
        assert db.session.get(Lead, two.id).procam_vertical == \
            'Marine Insurance'      # untouched
        assert db.session.get(Company, account.id).vertical == \
            'Heavy Transport'
        # And the ten are on the master list.
        from app.master_data import service as md
        assert 'Heavy Transport' in [i.code for i in md.items('vertical')]

        mod.undo(batch)
        assert db.session.get(Lead, one.id).procam_vertical == 'PFM'
        assert db.session.get(Company, account.id).vertical == \
            'heavy transport'


def test_the_vertical_sweep_needs_an_actor():
    source = open(os.path.join(_ROOT, 'scripts',
                               '2026_11_15_verticals.py')).read()
    assert '--apply needs --actor' in source


def test_the_gap_sweep_fills_from_the_code_and_the_account():
    mod = _script('2026_11_15_contact_gaps.py')
    if True:
        account = _account(industry='Cement', city='Pune',
                           state='Maharashtra')
        gulf = _contact(name='ZT Gulf Person', account_id=account.id,
                        email='g@zt.example', mobile='+971 50 123 4567',
                        country='', industry='', city='', state='')
        found = mod.survey()
        mine = [f for f in found if f[0].id == gulf.id]
        assert mine, 'nothing proposed for a contact with gaps'
        fills = mine[0][1]
        assert fills['country'] == ('United Arab Emirates',
                                    'the dialling code')
        assert fills['industry'][0] == 'Cement'
        assert fills['city'][0] == 'Pune'

        batch = mod.next_batch_id()
        mod.apply(mine, batch=batch, actor=ADMIN)
        again = db.session.get(Contact, gulf.id)
        assert again.country == 'United Arab Emirates'
        assert again.industry == 'Cement'

        mod.undo(batch)
        back = db.session.get(Contact, gulf.id)
        assert not (back.country or '')
        assert not (back.industry or '')


def test_the_gap_sweep_never_overwrites_a_stored_country():
    """Every contact carries one because the form defaulted it. The
    sweep cannot know which of them are real, so it does not decide."""
    mod = _script('2026_11_15_contact_gaps.py')
    if True:
        account = _account(country='Germany')
        held = _contact(name='ZT Stored Person', account_id=account.id,
                        email='s@zt.example', country='India',
                        mobile='+49 30 123456')
        found = {row.id: fills for row, fills in mod.survey()}
        assert 'country' not in (found.get(held.id) or {})
        assert db.session.get(Contact, held.id).country == 'India'


# ═══ the UAT of 15 Nov ═════════════════════════════════════════════
# Five findings from the Chrome run on live. Each one is a test here
# so the fix is the thing that is kept, not the thing that was done.

def test_three_spellings_of_india_are_one_country():
    """The finding: 664 "India" + 2 "INDIA" + 1 "Indian" of 667, and
    the page reported the top value as "Indian", 1 contact, 0.1%, and
    called the filter trustworthy. Counting case-sensitively made the
    dominant value the rarest spelling of it."""
    for n in range(20):
        _contact(name=f'ZT India {n}', country='India',
                 email=f'i{n}@zt.example')
    _contact(name='ZT Shout', country='INDIA', email='shout@zt.example')
    _contact(name='ZT Adj', country='Indian', email='adj@zt.example')
    found = ps.country_health()
    assert found['dominant']['value'] == 'India'
    assert found['dominant']['count'] >= 22
    spellings = found['values'][0]['spellings']
    assert {s['value'] for s in spellings} >= {'India', 'INDIA', 'Indian'}


def test_the_warning_fires_above_eighty_percent():
    """80%, on the instruction of 15 Nov. 664 of 667 is 99.6% and
    nothing honest sits near 80 in this column."""
    assert ps.DOMINANT_AT == 80.0
    for n in range(20):
        _contact(name=f'ZT Dom {n}', country='india',
                 email=f'd{n}@zt.example')
    body = _client().get('/api/contacts/search/country-health').get_json()
    assert body['ok'] is True
    if body['dominant']['percent'] >= 80.0:
        assert body['trustworthy'] is False
        assert 'defaulted' in body['note']
    else:
        assert body['trustworthy'] is True


def test_a_country_it_does_not_know_is_its_own_country():
    """Grouping is not guessing: only spellings the business uses are
    folded together, and anything else stands alone."""
    assert ps.country_group('india') == ps.country_group('INDIA') == 'India'
    assert ps.country_group('Indian') == 'India'
    assert ps.country_group('UAE') == 'United Arab Emirates'
    assert ps.country_group('Zanzibar') == 'Zanzibar'
    assert ps.country_group('  ') == ''


def test_the_portal_is_not_a_person():
    """#12645 showed "partner@procol.in" as a person under Amns. It is
    the portal's own delivery address — the one the matcher is
    forbidden to learn — and a search result is somebody to ring."""
    account = _account()
    _lead(pic='partner@procol.in', company_id=account.id,
          email='partner@procol.in')
    assert ps.search(name='partner')['total'] == 0
    assert ps.search(company='ZT Steel Works')['total'] == 0
    assert ps.duplicates_of(email='partner@procol.in') == []


def test_the_same_list_the_matcher_uses():
    for address in ('partner@procol.in', 'x@ansmtp.ariba.com',
                    'rfq@superprocure.in', 'a@procurengine.com',
                    'noreply@anything.example',
                    'do-not-reply@buyer.example'):
        assert ps.is_system_address(address), address
    for address in ('ravi@ztsteel.example', 'purchase@jindal.example',
                    'partner@realcustomer.example'):
        assert not ps.is_system_address(address), address


def test_a_real_person_keeps_their_phone_and_loses_the_robot_address():
    """The row is dropped only when there is nothing of a person left.
    A named PIC with a phone is a person whose email was the portal's."""
    account = _account()
    _lead(pic='ZT Ravi Kumar', company_id=account.id,
          email='noreply@procol.in', phone='+91 98200 33333')
    found = ps.search(name='ZT Ravi')
    assert found['total'] == 1
    row = found['rows'][0]
    assert row['emails'] == []
    assert row['phones']


def test_the_source_note_counts_leads_the_same_way_every_time():
    """"Contact · and 1 lead" beside "and 2 more lead(s)" was two
    wordings of one fact."""
    account = _account()
    _contact(name='ZT Nita Shah', account_id=account.id,
             email='nita@ztsteel.example')
    _lead(pic='ZT Nita Shah', company_id=account.id,
          email='nita@ztsteel.example')
    row = ps.search(name='ZT Nita')['rows'][0]
    assert row['source_note'] == 'Contact · also on 1 lead'
    _lead(pic='ZT Nita Shah', company_id=account.id,
          email='NITA@ztsteel.example')
    row = ps.search(name='ZT Nita')['rows'][0]
    assert row['source_note'] == 'Contact · also on 2 leads'


def test_the_type_ahead_offers_the_name_that_starts_that_way_first():
    """"Am" offered BHEL above Amns, because BHEL was on more rows."""
    _account(name='ZT Amns')
    for n in range(6):
        _lead(pic=f'ZT P{n}', company='ZT Bharat Am Works')
    values = ps.suggest('company', 'ZT Am')
    assert values[0] == 'ZT Amns', values


def test_the_search_page_closes_its_menus():
    """Escape, a click outside and Clear all left a dropdown covering
    the results. One function closes them; everything calls it."""
    html = _client().get('/contacts/search').get_data(as_text=True)
    assert 'function closeMenus' in html
    assert "ev.key === 'Escape'" in html
    assert "closest('.fs-hits')" in html
    # and Clear closes them before it clears the results
    assert html.index('function clearAll') < html.index('function closeMenus')
    assert 'cap-warn' in html


def test_the_new_contact_form_shows_the_duplicates_it_is_sent():
    """The server has answered 409 with the matches since Part 1; the
    form threw the body away, so Save appeared to do nothing."""
    client = _client()
    token = _token(client)
    account = _account()
    _contact(name='ZT Dup Person', account_id=account.id,
             email='dup@ztsteel.example', mobile='+91 98200 44444')
    answer = client.post('/api/contacts',
                         json={'name': 'ZT Dup Person',
                               'email': 'dup@ztsteel.example'},
                         headers={'X-CSRF-Token': token})
    assert answer.status_code == 409
    body = answer.get_json()
    assert body['needs'] == 'duplicate_check'
    assert body['duplicates'] and body['duplicates'][0]['person']
    emp = Employee.query.filter_by(emp_code=ADMIN).first()
    emp.must_change_pw = False          # /app is the change form until then
    db.session.commit()
    html = client.get('/app').get_data(as_text=True)
    assert 'function ctDupes' in html
    assert "r.needs==='duplicate_check'" in html
    assert 'id="ctDupe"' in html
    # and it can still be created on purpose
    made = client.post('/api/contacts',
                       json={'name': 'ZT Dup Person',
                             'email': 'dup@ztsteel.example',
                             'use_existing': True},
                       headers={'X-CSRF-Token': token})
    assert made.status_code == 200 and made.get_json()['ok'] is True


def test_the_duplicate_message_names_the_button_not_the_field():
    """UAT 17 Nov: "send use_existing to create a second record" is
    what the browser does, not what the person presses."""
    client = _client()
    token = _token(client)
    _contact(name='ZT Wording Person', email='wording@zt.example')
    answer = client.post('/api/contacts',
                         json={'name': 'ZT Wording Person',
                               'email': 'wording@zt.example'},
                         headers={'X-CSRF-Token': token})
    assert answer.status_code == 409
    said = answer.get_json()['error']
    assert 'press Save anyway if this is a different person' in said
    assert 'use_existing' not in said


def test_a_name_only_duplicate_has_to_be_the_whole_name():
    """UAT 17 Nov: "Test" was offered as a duplicate of "Integration
    Test". A false duplicate costs the record not being created."""
    _contact(name='ZT Integration Test', email='integration@zt.example')
    assert ps.duplicates_of(name='ZT Integration Test')
    assert ps.duplicates_of(name='Test') == []
    assert ps.duplicates_of(name='ZT Integration') == []


def test_a_middle_name_is_still_the_same_person():
    _contact(name='ZT Rajesh Kumar Sharma', email='rks@zt.example')
    assert ps.duplicates_of(name='ZT Rajesh Kumar Sharma')
    # first + last, middle name dropped on the card or in the CRM
    assert ps.same_person_name('Rajesh Sharma', 'Rajesh Kumar Sharma')
    assert ps.same_person_name('Rajesh Kumar Sharma', 'Rajesh Sharma')
    assert not ps.same_person_name('Rajesh', 'Rajesh Kumar Sharma')
    assert not ps.same_person_name('Kumar', 'Rajesh Kumar Sharma')


def test_the_email_and_phone_checks_are_untouched():
    """Those ARE the person; only the name-only match was loosened."""
    _contact(name='ZT Narrow Person', email='narrow@zt.example',
             mobile='+91 98200 77777')
    assert ps.duplicates_of(email='NARROW@zt.example')
    assert ps.duplicates_of(phone='9820077777')


def test_the_search_itself_still_matches_a_part_of_a_name():
    """The duplicate check tightened; the SEARCH must not. Somebody
    looking for a person they half remember needs the substring."""
    _contact(name='ZT Integration Test', email='sub@zt.example')
    assert ps.search(name='Integration')['total'] >= 1
    assert ps.search(name='Test')['total'] >= 1


def test_the_type_ahead_puts_an_exact_match_first():
    """UAT 17 Nov §4. Typing the whole of a short account name and
    finding it third is the 15 Nov complaint one step further on."""
    _account(name='ZT Am')
    for n in range(8):
        _lead(pic=f'ZT Q{n}', company='ZT Amalgamated Movers')
    values = ps.suggest('company', 'ZT Am')
    assert values[0] == 'ZT Am', values


def test_an_account_alias_beats_a_name_a_lead_happens_to_carry():
    account = _account(name='ZT Amns',
                       aliases=['ZT ArcelorMittal Nippon Steel'])
    _lead(pic='ZT R1', company_id=account.id)
    for n in range(6):
        _lead(pic=f'ZT S{n}', company='ZT Arcelor Trading Company')
    values = ps.suggest('company', 'ZT Arcelor')
    assert values[0] == 'ZT ArcelorMittal Nippon Steel', values
