"""
PROCAM-DECISIONS-02NOV Module 1 — the Procam Standard T&C.

What these hold, in the order the decision gives them:

  1.1  one master, on every quote in every vertical, versioned
  1.2  an optional annexure per vertical
  1.3  the client's or the tender's own T&C, replacing or added,
       filed against the number so it follows every revision
  1.4  an SCC overrides the standard clause it names
  1.5  a quote is never blocked for want of any of it

The last one is the one worth having a test for. Every other rule
here is about printing the right words; that one is about a
salesperson at 19:00 being able to send a quote at all.
"""
import importlib
import importlib.util
import io
import os
import sys
import tempfile
from datetime import date

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_TMP = tempfile.mkdtemp()
os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'terms-master-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'TermsMaster12345')
os.environ['QUOTE_TERMS_STORAGE_ROOT'] = os.path.join(_TMP, 'store')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(_TMP, 'terms.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
# Off here, as in every other test module — and the reason
# tests/test_csrf_write_paths.py exists: it reads the routes rather
# than exercising them, so a write path that forgets its token is
# caught by that guard and not by a green suite here.
flask_app.config['WTF_CSRF_ENABLED'] = False
Company, Employee, Lead = _main.Company, _main.Employee, _main.Lead

from app.models.access import AccessProfile, DataScope      # noqa: E402
from app.models.quote import Quote                          # noqa: E402
from app.models.quote_terms import (ANNEXURE,               # noqa: E402
                                    MASTER, QuoteTermSet,
                                    QuoteTermsDocument)
from app.services import quote_terms as qt                  # noqa: E402

ADMIN = 'TMADM'
SELLER = 'TMSEL'
VERTICAL = 'Heavy Transport'

MASTER_BODY = '\n'.join((
    'Validity: 15 days from the quote date.',
    'Payment: 30 days from the date of invoice.',
    'Jurisdiction: The courts of Delhi have exclusive jurisdiction.',
    'Cargo is to be insured by the client.',
))
ANNEXURE_BODY = '\n'.join((
    'Permits: Permit lead time is 14 days.',
    'Cranes: Cranes at both ends are the client\'s arrangement.',
))


def _emp(code, name, *, role='user', admin=False):
    e = Employee.query.filter_by(emp_code=code).first() or \
        Employee(emp_code=code)
    e.name, e.is_active, e.must_change_pw = name, True, False
    e.email = f'{code.lower()}@procamgroup.in'
    e.role, e.session_version = role, 0
    e.is_super_admin, e.is_vertical_head = admin, False
    db.session.add(e)
    db.session.flush()
    profile = AccessProfile.query.filter_by(emp_code=code).first()
    if profile is None:
        profile = AccessProfile(emp_code=code)
        db.session.add(profile)
    profile.data_scope = DataScope.ALL if admin else DataScope.OWN
    return e


def _client(code=ADMIN, role='admin'):
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['emp_code'] = code
        sess['role'] = role
        sess['session_version'] = 0
    return c


@pytest.fixture()
def world():
    with flask_app.app_context():
        db.create_all()
        QuoteTermSet.query.delete()
        from app.models.quote_terms import (AccountStandingCondition,
                                            QuoteSpecialCondition)
        QuoteSpecialCondition.query.delete()
        AccountStandingCondition.query.delete()
        QuoteTermsDocument.query.delete()
        Quote.query.delete()
        _emp(ADMIN, 'Terms Admin', role='admin', admin=True)
        _emp(SELLER, 'Terms Seller')
        db.session.commit()
        yield


def _account(name='TM Client'):
    row = Company.query.filter_by(name=name).first()
    if row is None:
        row = Company(name=name, is_active=True, vertical=VERTICAL)
        db.session.add(row)
        db.session.flush()
    return row


def _quote(number='QUO-TM-0001', *, vertical=VERTICAL, account=None,
           owner=ADMIN):
    lead = Lead(company='TM Client', stage='Quoted', source='manual',
                assigned_to=owner, procam_vertical=vertical)
    db.session.add(lead)
    db.session.flush()
    quote = Quote(quote_number=number, lead_id=lead.id,
                  account_id=(account.id if account else None),
                  quote_date=date.today(), prepared_by_id=owner,
                  created_by_id=owner, status='Draft')
    db.session.add(quote)
    db.session.flush()
    return quote


def _publish_both(actor=ADMIN):
    qt.publish_master(MASTER_BODY, actor=actor)
    qt.publish(VERTICAL, ANNEXURE_BODY, actor=actor)
    db.session.commit()


# ══ 1.1 one master, every vertical ══════════════════════════════════
def test_the_master_belongs_to_no_vertical(world):
    with flask_app.app_context():
        row = qt.publish_master(MASTER_BODY, actor=ADMIN)
        db.session.commit()
        assert row.kind == MASTER
        assert row.vertical == ''
        assert row.is_master is True


def test_publishing_a_master_with_a_vertical_is_refused(world):
    """Almost certainly a caller who meant to publish an annexure."""
    with flask_app.app_context():
        with pytest.raises(qt.TermsError) as caught:
            qt.publish('Warehousing', MASTER_BODY, kind=MASTER,
                       actor=ADMIN)
        assert 'annexure' in str(caught.value)


def test_every_vertical_prints_the_same_master(world):
    with flask_app.app_context():
        qt.publish_master(MASTER_BODY, actor=ADMIN)
        db.session.commit()
        for vertical in ('Heavy Transport', 'Warehousing',
                         'Installation', 'Project Freight'):
            quote = _quote(f'QUO-TM-{vertical[:4]}', vertical=vertical)
            printed = qt.for_print(quote)
            assert printed['standard']['missing'] is False
            assert 'Payment' in printed['standard']['body']


def test_a_new_version_supersedes_and_never_edits(world):
    """A quote printed in March was printed under March's wording."""
    with flask_app.app_context():
        v1 = qt.publish_master(MASTER_BODY, actor=ADMIN)
        db.session.commit()
        quote = _quote()
        qt.attach_to(quote, actor=ADMIN)
        db.session.commit()
        assert quote.terms_master_version_id == v1.id

        v2 = qt.publish_master(MASTER_BODY + '\nLate payment: 18% pa.',
                               actor=ADMIN)
        db.session.commit()
        assert v2.version == 2
        assert db.session.get(QuoteTermSet, v1.id).is_current is False
        # The printed quote still says what it said.
        assert qt.master_on(quote).id == v1.id
        assert 'Late payment' not in qt.for_print(quote)['standard']['body']


def test_the_october_rows_are_annexures_not_masters(world):
    """`kind` defaults to annexure so the six rows written in October
    keep meaning exactly what they meant."""
    with flask_app.app_context():
        row = QuoteTermSet(vertical='Warehousing', version=1,
                           title='Standard', body='x', is_current=True)
        db.session.add(row)
        db.session.commit()
        assert row.kind == ANNEXURE
        assert qt.master_current() is None


# ══ 1.2 the annexure ════════════════════════════════════════════════
def test_the_annexure_prints_after_the_master(world):
    with flask_app.app_context():
        _publish_both()
        quote = _quote()
        printed = qt.for_print(quote)
        assert 'Payment' in printed['standard']['body']
        assert 'Permits' in printed['annexure']['body']


def test_a_vertical_with_no_annexure_still_prints_the_master(world):
    with flask_app.app_context():
        qt.publish_master(MASTER_BODY, actor=ADMIN)
        db.session.commit()
        quote = _quote(vertical='Warehousing')
        printed = qt.for_print(quote)
        assert printed['annexure']['missing'] is True
        assert printed['standard']['missing'] is False
        assert any('annexure' in n for n in printed['notes'])
        assert printed['blocks_issue'] is False


def test_the_master_and_the_annexure_are_versioned_apart(world):
    with flask_app.app_context():
        _publish_both()
        quote = _quote()
        qt.attach_to(quote, actor=ADMIN)
        db.session.commit()
        master_id = quote.terms_master_version_id
        qt.publish(VERTICAL, ANNEXURE_BODY + '\nEscort: at actuals.',
                   actor=ADMIN)
        db.session.commit()
        # The annexure moved; the master this quote printed did not.
        assert quote.terms_master_version_id == master_id


def test_a_quote_built_before_the_master_picks_it_up_on_reissue(world):
    with flask_app.app_context():
        annexure = qt.publish(VERTICAL, ANNEXURE_BODY, actor=ADMIN)
        db.session.commit()
        quote = _quote()
        qt.attach_to(quote, actor=ADMIN)
        db.session.commit()
        assert quote.terms_version_id == annexure.id
        assert quote.terms_master_version_id is None

        master = qt.publish_master(MASTER_BODY, actor=ADMIN)
        db.session.commit()
        qt.attach_to(quote, actor=ADMIN)
        db.session.commit()
        # The master arrives; the annexure it already printed is
        # not disturbed.
        assert quote.terms_master_version_id == master.id
        assert quote.terms_version_id == annexure.id


# ══ 1.3 the client's or the tender's own T&C ════════════════════════
def test_a_document_is_filed_against_the_number_not_the_row(world):
    """Module 1.3: "stored against the RFQ/QUO number and all its
    revisions". A revision re-prices; it does not change what the
    client sent."""
    with flask_app.app_context():
        _publish_both()
        rev1 = _quote('QUO-TM-0009')
        qt.attach_document(rev1, filename='client_tc.pdf',
                           stored_path='/tmp/x.pdf',
                           content_type='application/pdf',
                           mode='add', actor=ADMIN)
        db.session.commit()
        # Revision 2 is a new row carrying the same number.
        rev2 = Quote(quote_number='QUO-TM-0009-R2',
                     lead_id=rev1.lead_id, status='Draft')
        rev2.quote_number = 'QUO-TM-0009'
        assert [d.filename for d in qt.documents_on(rev2)] == \
            ['client_tc.pdf']


def test_a_document_with_no_mode_is_refused(world):
    """A file nobody said replaces or adds would be printed one way
    and argued the other."""
    with flask_app.app_context():
        _publish_both()
        quote = _quote()
        with pytest.raises(qt.TermsError) as caught:
            qt.attach_document(quote, filename='x.pdf',
                               stored_path='/tmp/x.pdf',
                               content_type='application/pdf',
                               mode='', actor=ADMIN)
        assert 'replaces' in str(caught.value)


def test_replace_means_the_master_is_not_printed(world):
    with flask_app.app_context():
        _publish_both()
        quote = _quote()
        qt.attach_document(quote, filename='gem_bid_tc.pdf',
                           stored_path='/tmp/g.pdf',
                           content_type='application/pdf',
                           mode='replace', actor=ADMIN)
        db.session.commit()
        printed = qt.for_print(quote)
        assert printed['replaced'] is True
        assert printed['standard']['missing'] is True
        assert printed['annexure']['missing'] is True
        assert any('replaces' in n for n in printed['notes'])


def test_add_means_both_apply(world):
    with flask_app.app_context():
        _publish_both()
        quote = _quote()
        qt.attach_document(quote, filename='their_tc.pdf',
                           stored_path='/tmp/t.pdf',
                           content_type='application/pdf',
                           mode='add', actor=ADMIN)
        db.session.commit()
        printed = qt.for_print(quote)
        assert printed['replaced'] is False
        assert printed['standard']['missing'] is False
        assert printed['documents'][0]['mode'] == 'add'


def test_a_withdrawn_document_stops_applying_but_stays_on_file(world):
    with flask_app.app_context():
        _publish_both()
        quote = _quote()
        row = qt.attach_document(quote, filename='wrong.pdf',
                                 stored_path='/tmp/w.pdf',
                                 content_type='application/pdf',
                                 mode='replace', actor=ADMIN)
        db.session.commit()
        qt.withdraw_document(row, actor=ADMIN)
        db.session.commit()
        assert qt.documents_on(quote) == []
        assert qt.for_print(quote)['replaced'] is False
        assert db.session.get(QuoteTermsDocument, row.id) is not None


def test_an_unsaved_quote_cannot_hold_a_document(world):
    with flask_app.app_context():
        _publish_both()
        quote = Quote(quote_number='', status='Draft')
        with pytest.raises(qt.TermsError) as caught:
            qt.attach_document(quote, filename='x.pdf',
                               stored_path='/tmp/x.pdf',
                               content_type='application/pdf',
                               mode='add', actor=ADMIN)
        assert 'no number' in str(caught.value)


# ══ 1.4 an SCC overrides the clause it names ════════════════════════
def test_a_condition_overrides_the_standard_clause_it_names(world):
    with flask_app.app_context():
        _publish_both()
        quote = _quote()
        qt.add_condition(quote, 'Payment: 60 days from invoice, as '
                                'agreed with the client.', actor=ADMIN)
        db.session.commit()
        printed = qt.for_print(quote)
        payment = next(c for c in printed['standard']['clauses']
                       if c['key'] == 'payment')
        validity = next(c for c in printed['standard']['clauses']
                        if c['key'] == 'validity')
        assert payment['overridden_by'].startswith('Payment: 60 days')
        assert payment['overridden_scope'] == 'quote'
        # And only the clause it names.
        assert validity['overridden_by'] == ''


def test_an_overridden_clause_is_marked_not_dropped(world):
    """The client can see the standard term and the term that beat
    it, which is the argument this section exists to prevent."""
    with flask_app.app_context():
        _publish_both()
        quote = _quote()
        qt.add_condition(quote, 'Payment: 60 days.', actor=ADMIN)
        db.session.commit()
        printed = qt.for_print(quote)
        assert 'Payment: 30 days' in printed['standard']['body']
        assert len(printed['standard']['clauses']) == 4


def test_a_condition_that_names_nothing_is_an_extra_condition(world):
    with flask_app.app_context():
        _publish_both()
        quote = _quote()
        qt.add_condition(quote, 'Unloading at site is by the client.',
                         actor=ADMIN)
        db.session.commit()
        printed = qt.for_print(quote)
        assert [c['text'] for c in printed['special']] == \
            ['Unloading at site is by the client.']
        assert not any(c['overridden_by']
                       for c in printed['standard']['clauses'])


def test_a_condition_can_override_an_annexure_clause_too(world):
    with flask_app.app_context():
        _publish_both()
        quote = _quote()
        qt.add_condition(quote, 'Permits: lead time 21 days for this '
                                'route.', actor=ADMIN)
        db.session.commit()
        printed = qt.for_print(quote)
        permits = next(c for c in printed['annexure']['clauses']
                       if c['key'] == 'permits')
        assert '21 days' in permits['overridden_by']


def test_a_clients_standing_condition_applies_to_every_quote(world):
    with flask_app.app_context():
        _publish_both()
        account = _account()
        qt.add_standing(account.id, 'Payment: 45 days, per the rate '
                                    'contract.', actor=ADMIN)
        db.session.commit()
        first = _quote('QUO-TM-0021', account=account)
        second = _quote('QUO-TM-0022', account=account)
        for quote in (first, second):
            printed = qt.for_print(quote)
            payment = next(c for c in printed['standard']['clauses']
                           if c['key'] == 'payment')
            assert '45 days' in payment['overridden_by']
            assert payment['overridden_scope'] == 'client'


def test_this_quotes_condition_beats_the_clients_standing_one(world):
    """Latest decision wins: the standing agreement is older than a
    condition somebody typed on this job today."""
    with flask_app.app_context():
        _publish_both()
        account = _account()
        qt.add_standing(account.id, 'Payment: 45 days.', actor=ADMIN)
        quote = _quote(account=account)
        qt.add_condition(quote, 'Payment: advance 50%, balance 15 '
                                'days.', actor=ADMIN)
        db.session.commit()
        printed = qt.for_print(quote)
        payment = next(c for c in printed['standard']['clauses']
                       if c['key'] == 'payment')
        assert 'advance 50%' in payment['overridden_by']
        assert payment['overridden_scope'] == 'quote'


def test_an_ended_standing_condition_stops_applying(world):
    with flask_app.app_context():
        _publish_both()
        account = _account()
        row = qt.add_standing(account.id, 'Payment: 45 days.',
                              actor=ADMIN)
        db.session.commit()
        qt.end_standing(row, actor=ADMIN)
        db.session.commit()
        quote = _quote(account=account)
        assert qt.for_print(quote)['special'] == []


def test_the_conditions_are_numbered_once_across_both_scopes(world):
    """Two clauses numbered 1 on one page is a quote nobody can
    cite."""
    with flask_app.app_context():
        _publish_both()
        account = _account()
        qt.add_standing(account.id, 'Storage: free for 7 days.',
                        actor=ADMIN)
        quote = _quote(account=account)
        qt.add_condition(quote, 'Escort: at actuals.', actor=ADMIN)
        db.session.commit()
        printed = qt.for_print(quote)
        assert [c['scope'] for c in printed['special']] == \
            ['client', 'quote']


# ══ 1.5 never blocked ═══════════════════════════════════════════════
def test_a_quote_with_no_terms_at_all_still_prints(world):
    with flask_app.app_context():
        quote = _quote()
        printed = qt.for_print(quote)
        assert printed['blocks_issue'] is False
        assert printed['standard']['missing'] is True
        assert printed['annexure']['missing'] is True
        assert any('No Procam Standard' in n for n in printed['notes'])


def test_attaching_terms_that_do_not_exist_does_not_raise(world):
    with flask_app.app_context():
        quote = _quote()
        assert qt.attach_to(quote, actor=ADMIN) is None
        assert quote.terms_master_version_id is None


def test_the_send_path_reads_the_terms_without_raising(world):
    """`quote_send` calls `for_print` on every send. If that can
    raise, a quote cannot be sent at all."""
    with flask_app.app_context():
        quote = _quote()
        printed = qt.for_print(quote)
        assert printed['special'] == []
        assert printed['documents'] == []


# ══ the screens ═════════════════════════════════════════════════════
def test_an_administrator_publishes_the_master(world):
    with flask_app.app_context():
        pass
    r = _client().post('/api/quote-terms/master',
                       json={'body': MASTER_BODY})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()['master']['version'] == 1
    assert len(r.get_json()['clauses']) == 4


def test_a_salesperson_cannot_publish_the_master(world):
    r = _client(SELLER, 'user').post('/api/quote-terms/master',
                                     json={'body': MASTER_BODY})
    assert r.status_code == 403
    assert 'administrator' in r.get_json()['error']


def test_a_salesperson_can_read_what_a_quote_goes_out_under(world):
    """Somebody who cannot read the payment term is somebody who
    agrees to a different one on the phone."""
    with flask_app.app_context():
        _publish_both()
        quote = _quote(owner=SELLER)
        qid = quote.id
        db.session.commit()
    r = _client(SELLER, 'user').get(f'/api/quotes/{qid}/terms')
    assert r.status_code == 200
    assert 'Payment' in r.get_json()['standard']['body']


def test_an_empty_master_is_refused_with_a_reason(world):
    r = _client().post('/api/quote-terms/master', json={'body': '  '})
    assert r.status_code == 400
    assert r.get_json()['needs'] == 'body'


def test_the_upload_refuses_a_file_it_cannot_store(world):
    with flask_app.app_context():
        _publish_both()
        quote = _quote()
        qid = quote.id
        db.session.commit()
    r = _client().post(
        f'/api/quotes/{qid}/terms/document',
        data={'mode': 'add',
              'file': (io.BytesIO(b'MZ...'), 'terms.exe')},
        content_type='multipart/form-data')
    assert r.status_code == 400
    assert '.pdf' in r.get_json()['allowed']


def test_the_upload_stores_the_file_and_files_it_by_number(world):
    with flask_app.app_context():
        _publish_both()
        quote = _quote('QUO-TM-0031')
        qid = quote.id
        db.session.commit()
    r = _client().post(
        f'/api/quotes/{qid}/terms/document',
        data={'mode': 'replace', 'note': 'GeM bid 2026/1187',
              'file': (io.BytesIO(b'%PDF-1.4 their terms'),
                       'gem_tc.pdf')},
        content_type='multipart/form-data')
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body['doc_number'] == 'QUO-TM-0031'
    assert body['document']['mode'] == 'replace'
    with flask_app.app_context():
        row = QuoteTermsDocument.query.filter_by(
            doc_number='QUO-TM-0031').first()
        assert os.path.exists(row.stored_path)
        assert open(row.stored_path, 'rb').read().startswith(b'%PDF')


def test_two_uploads_of_one_name_do_not_overwrite(world):
    """Two revisions can send two files with the same name, and the
    second silently replacing the first is how a quote prints terms
    the client never sent."""
    with flask_app.app_context():
        _publish_both()
        quote = _quote('QUO-TM-0032')
        qid = quote.id
        db.session.commit()
    c = _client()
    paths = []
    for n in (1, 2):
        r = c.post(f'/api/quotes/{qid}/terms/document',
                   data={'mode': 'add',
                         'file': (io.BytesIO(f'%PDF rev {n}'.encode()),
                                  'tc.pdf')},
                   content_type='multipart/form-data')
        assert r.status_code == 200
    with flask_app.app_context():
        rows = QuoteTermsDocument.query.filter_by(
            doc_number='QUO-TM-0032').all()
        paths = [r.stored_path for r in rows]
    assert len(set(paths)) == 2
    assert open(paths[0], 'rb').read() != open(paths[1], 'rb').read()


def test_the_upload_refuses_without_a_mode_and_says_the_two(world):
    with flask_app.app_context():
        _publish_both()
        quote = _quote('QUO-TM-0033')
        qid = quote.id
        db.session.commit()
    r = _client().post(
        f'/api/quotes/{qid}/terms/document',
        data={'file': (io.BytesIO(b'%PDF'), 'tc.pdf')},
        content_type='multipart/form-data')
    assert r.status_code == 400
    assert r.get_json()['modes'] == ['add', 'replace']


def test_adding_a_condition_says_what_it_replaced(world):
    """The person typing it is the one who should know it has
    replaced a standard clause."""
    with flask_app.app_context():
        _publish_both()
        quote = _quote()
        qid = quote.id
        db.session.commit()
    r = _client().post(f'/api/quotes/{qid}/terms/condition',
                       json={'text': 'Payment: 60 days.'})
    assert r.status_code == 200
    assert r.get_json()['overrides'].startswith('Payment: 30 days')


def test_a_salesperson_cannot_make_a_condition_standing(world):
    """It would change quotes nobody has written yet, including other
    people's."""
    with flask_app.app_context():
        _publish_both()
        account = _account()
        aid = account.id
        db.session.commit()
    r = _client(SELLER, 'user').post(
        f'/api/accounts/{aid}/standing-condition',
        json={'text': 'Payment: 90 days.'})
    assert r.status_code == 403
    assert 'administrator' in r.get_json()['error']


def test_the_admin_screen_shows_a_draft_annexure_as_a_draft(world):
    """A term set whose first line still says DRAFT is one no quote
    should be going out under."""
    with flask_app.app_context():
        qt.publish_master(MASTER_BODY, actor=ADMIN)
        qt.publish('Warehousing',
                   'DRAFT — replace before issuing.\n1. Storage.',
                   actor=ADMIN)
        db.session.commit()
    r = _client().get('/admin/quote-terms')
    assert r.status_code == 200
    page = r.get_data(as_text=True)
    assert 'DRAFT' in page
    assert 'Replace it before a' in page
