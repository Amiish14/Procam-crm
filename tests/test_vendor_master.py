"""Vendor Master — the domains whose mail is filed as rate sourcing.

A row here stops a domain's mail becoming a lead, so the tests hold the
two ways that goes wrong: matching too little (a shipping line writing
from a subdomain creates leads) and matching too much (a customer whose
domain merely ends in the same letters loses their enquiries).
"""
import os
import sys
import tempfile
from datetime import datetime

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'VendorTestOnly12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'vendors.db')

import importlib                                              # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Employee, VendorDomain = _main.Employee, _main.VendorDomain
EmailClassification = _main.EmailClassification
flask_app.config['WTF_CSRF_ENABLED'] = False

from app.intake import learning, vendors                      # noqa: E402
from app.models.access import AccessProfile                   # noqa: E402
from app.models.audit import AuditEvent                       # noqa: E402
from app.services import lead_intake as li                    # noqa: E402
from app.services import lead_intake_db as lidb               # noqa: E402

#: Every domain a test registers ends in this, so teardown can find them.
_SUFFIX = 'vmtest.example'


def _d(name):
    return f'{name}.{_SUFFIX}'


def _client(code, role='admin'):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code=code, name=code, role=role, vertical='All')
    return c


@pytest.fixture(scope='module')
def world():
    with flask_app.app_context():
        db.create_all()
        for code, role, sup in (('VMADM', 'admin', True),
                                ('VMREP', 'user', False),
                                ('VMTRI', 'user', False)):
            e = Employee.query.filter_by(emp_code=code).first()
            if e is None:
                e = Employee(emp_code=code, name=f'Vendor {code}')
                db.session.add(e)
            e.role, e.is_active, e.must_change_pw = role, True, False
            e.is_super_admin, e.vertical = sup, 'All'
        # A triage reviewer who does not hold Master Data.
        prof = AccessProfile.query.filter_by(emp_code='VMTRI').first()
        if prof is None:
            prof = AccessProfile(emp_code='VMTRI')
            db.session.add(prof)
        prof.data_scope, prof.perms = 'all', ['admin.triage']
        db.session.commit()
    yield {'admin': _client('VMADM'), 'rep': _client('VMREP', 'user'),
           'triage': _client('VMTRI', 'user')}
    with flask_app.app_context():
        VendorDomain.query.filter(
            VendorDomain.domain.like(f'%{_SUFFIX}')).delete(
                synchronize_session=False)
        EmailClassification.query.filter(
            EmailClassification.from_domain.like(f'%{_SUFFIX}')).delete(
                synchronize_session=False)
        AccessProfile.query.filter_by(emp_code='VMTRI').delete()
        db.session.commit()


def _add(domain, vendor_type='shipping line', active=True, **kw):
    with flask_app.app_context():
        row = VendorDomain(domain=domain, vendor_type=vendor_type,
                           is_active=active, learned_from='manual', **kw)
        db.session.add(row)
        db.session.commit()
        return row.id


# ─── domain normalisation ────────────────────────────────────────────────
@pytest.mark.parametrize('raw, want', [
    ('Maersk.COM', 'maersk.com'),
    ('  maersk.com  ', 'maersk.com'),
    ('@maersk.com', 'maersk.com'),
    ('www.maersk.com', 'maersk.com'),
    ('mail.maersk.com', 'mail.maersk.com'),
    ('hapag-lloyd.com', 'hapag-lloyd.com'),
    ('shipper.co.in', 'shipper.co.in'),
])
def test_harmless_differences_are_normalised(raw, want):
    assert vendors.normalise_domain(raw) == (want, None)


@pytest.mark.parametrize('raw, says', [
    ('', 'Enter a domain'),
    ('sales@maersk.com', 'email address'),
    ('https://www.maersk.com', 'web address'),
    ('maersk.com/about', 'web address'),
    ('maersk .com', 'spaces'),
    ('maersk', 'dot'),
    ('co.in', 'public suffix'),
    ('gmail.com', 'free mail'),
    ('-bad-.com', 'not a valid'),
    ('10.0.0.1', 'IP address'),
])
def test_anything_that_is_not_a_bare_domain_is_refused(raw, says):
    domain, err = vendors.normalise_domain(raw)
    assert domain is None
    assert says.lower() in err.lower()


# ─── categories ──────────────────────────────────────────────────────────
@pytest.mark.parametrize('stored, category', [
    ('shipping line', 'shipping line'),
    ('Shipping_Line', 'shipping line'),
    ('transporter', 'transporter'),
    ('CHA', 'cha'),
    ('agent', 'overseas agent'),
    ('airline', 'airline'),
    ('warehouse', 'warehouse partner'),
    ('something typed once', 'other'),
    ('', 'other'),
    (None, 'other'),
])
def test_old_free_text_is_read_into_a_category(stored, category):
    assert vendors.category_of(stored) == category


def test_the_eight_categories_are_offered():
    labels = [label for _k, label in vendors.CATEGORIES]
    assert len(labels) == 8
    for want in ('Supplier', 'Shipping Line', 'Transporter', 'CHA',
                 'Warehouse Partner', 'Airline', 'Overseas Agent', 'Other'):
        assert any(l.startswith(want) for l in labels), want


def test_a_submitted_category_that_does_not_exist_is_not_filed_as_other():
    assert vendors.known_category('Shipping Line') == 'shipping line'
    assert vendors.known_category('CHA (customs house agent)') == 'cha'
    assert vendors.known_category('spaceship') is None


# ─── matching ────────────────────────────────────────────────────────────
def test_a_subdomain_of_a_registered_domain_matches(world):
    _add(_d('liner'))
    with flask_app.app_context():
        assert lidb.is_vendor_domain(_d('liner'))
        assert lidb.is_vendor_domain('mail.' + _d('liner'))
        assert lidb.is_vendor_domain('eu.mail.' + _d('liner'))
        assert lidb.is_vendor_domain(('MAIL.' + _d('liner')).upper())


def test_a_domain_that_only_ends_in_the_same_letters_does_not(world):
    _add(_d('maersk'))
    with flask_app.app_context():
        assert not lidb.is_vendor_domain('not' + _d('maersk'))
        assert not lidb.is_vendor_domain('notmaersk.' + _SUFFIX)
        # nor does the parent of a registered domain
        assert not lidb.is_vendor_domain(_SUFFIX)
        assert not lidb.is_vendor_domain('')
        assert not lidb.is_vendor_domain(None)


def test_a_deactivated_vendor_never_matches(world):
    _add(_d('retired'), active=False)
    with flask_app.app_context():
        assert not lidb.is_vendor_domain(_d('retired'))
        assert not lidb.is_vendor_domain('mail.' + _d('retired'))


def test_the_classifier_files_subdomain_mail_as_rate_sourcing(world):
    _add(_d('carrierline'))
    m = {'subject': 'Rates for Nhava Sheva to Jebel Ali',
         'body': {'content': 'Please find our rates for 20ft containers.'},
         'from': {'emailAddress': {'address': 'pricing@mail.' + _d('carrierline')}},
         'toRecipients': [{'emailAddress': {'address': 'leads@procamgroup.in'}}],
         'ccRecipients': []}
    with flask_app.app_context():
        d = li.classify(m, lidb.build_context())
        assert d.klass == li.Klass.RATE_SOURCING and d.step == 6

        row = VendorDomain.query.filter_by(domain=_d('carrierline')).first()
        row.is_active = False
        db.session.commit()
        d = li.classify(m, lidb.build_context())
        assert d.klass != li.Klass.RATE_SOURCING, \
            'deactivating a vendor must put its mail back through the tree'


def test_parent_domains_are_whole_labels_only():
    assert li.domain_and_parents('a.b.maersk.com') == [
        'a.b.maersk.com', 'b.maersk.com', 'maersk.com']
    assert li.domain_and_parents('maersk.com') == ['maersk.com']
    assert li.domain_and_parents('localhost') == []
    assert li.domain_and_parents('') == []


# ─── the API ─────────────────────────────────────────────────────────────
def test_an_admin_adds_a_vendor_with_a_category(world):
    r = world['admin'].post('/api/intake/vendors', json={
        'domain': '@WWW.' + _d('newline').upper(), 'category': 'Shipping Line',
        'name': 'New Line Pvt Ltd', 'notes': 'rates desk'})
    assert r.status_code == 201, r.get_data(as_text=True)
    v = r.get_json()['vendor']
    assert v['domain'] == _d('newline')
    assert v['category'] == 'shipping line'
    assert v['name'] == 'New Line Pvt Ltd'
    assert v['learned_from'] == 'manual'
    assert v['added_by'] == 'VMADM'
    with flask_app.app_context():
        assert lidb.is_vendor_domain('ops.' + _d('newline'))


def test_a_domain_already_registered_is_a_conflict(world):
    vid = _add(_d('twice'), active=False)
    r = world['admin'].post('/api/intake/vendors',
                            json={'domain': 'www.' + _d('twice')})
    assert r.status_code == 409
    body = r.get_json()
    assert body['existing_id'] == vid
    assert 'deactivated' in body['error']


def test_invalid_input_is_refused_with_a_reason(world):
    c = world['admin']
    for payload in ({'domain': 'buyer@' + _d('bad')},
                    {'domain': 'https://' + _d('bad')},
                    {'domain': 'nodot'},
                    {'domain': _d('bad'), 'category': 'spaceship'}):
        r = c.post('/api/intake/vendors', json=payload)
        assert r.status_code == 400, payload
        assert r.get_json()['ok'] is False and r.get_json()['error']
    with flask_app.app_context():
        assert VendorDomain.query.filter_by(domain=_d('bad')).first() is None


def test_editing_changes_category_name_and_notes_and_says_who(world):
    vid = _add(_d('editme'), vendor_type='agent')
    r = world['admin'].put(f'/api/intake/vendors/{vid}', json={
        'category': 'cha', 'name': 'Edit Me Customs', 'notes': 'Mundra'})
    assert r.status_code == 200
    v = r.get_json()['vendor']
    assert (v['category'], v['name'], v['notes']) == \
        ('cha', 'Edit Me Customs', 'Mundra')
    assert v['updated_by'] == 'VMADM' and v['updated_at']
    with flask_app.app_context():
        assert db.session.get(VendorDomain, vid).domain == _d('editme'), \
            'the domain is not editable'

    r = world['admin'].put(f'/api/intake/vendors/{vid}',
                           json={'category': 'spaceship'})
    assert r.status_code == 400
    assert world['admin'].put('/api/intake/vendors/99999999',
                              json={'name': 'x'}).status_code == 404


def test_deactivating_and_reactivating_never_deletes(world):
    vid = _add(_d('toggle'))
    c = world['admin']
    r = c.post(f'/api/intake/vendors/{vid}/deactivate',
               json={'reason': 'Turned out to be a customer'})
    assert r.status_code == 200 and r.get_json()['vendor']['is_active'] is False
    with flask_app.app_context():
        assert db.session.get(VendorDomain, vid) is not None
        assert not lidb.is_vendor_domain(_d('toggle'))
        ev = (AuditEvent.query.filter_by(action='config.vendor_change',
                                         entity_id=str(vid))
              .order_by(AuditEvent.id.desc()).first())
        assert ev is not None and ev.new_value == {'is_active': False}
        assert ev.reason == 'Turned out to be a customer'

    r = c.post(f'/api/intake/vendors/{vid}/activate', json={})
    assert r.status_code == 200 and r.get_json()['vendor']['is_active'] is True
    with flask_app.app_context():
        assert lidb.is_vendor_domain(_d('toggle'))

    assert c.delete(f'/api/intake/vendors/{vid}').status_code in (404, 405)


def test_the_list_filters_by_category_state_and_search(world):
    _add(_d('legacyagent'), vendor_type='agent', name='Far East Agency')
    _add(_d('oldtrucks'), vendor_type='transporter', active=False)
    c = world['admin']

    def domains(qs):
        r = c.get('/api/intake/vendors?' + qs)
        assert r.status_code == 200
        return {v['domain'] for v in r.get_json()['vendors']}

    got = domains('category=overseas+agent&active=1')
    assert _d('legacyagent') in got, 'a legacy "agent" row is an Overseas Agent'
    assert _d('oldtrucks') not in got

    assert _d('oldtrucks') in domains('active=0')
    assert _d('oldtrucks') not in domains('active=1')
    assert _d('oldtrucks') in domains('category=transporter')
    assert domains('q=far+east') >= {_d('legacyagent')}
    assert domains('category=spaceship') == set()

    row = [v for v in c.get('/api/intake/vendors?q=legacyagent')
           .get_json()['vendors']][0]
    assert row['vendor_type'] == 'agent', 'the stored value stays visible'
    assert row['learned_label'] and 'rejection_count' in row


def test_only_master_data_holders_can_reach_the_vendor_master(world):
    for path in ('/intake/vendors', '/api/intake/vendors'):
        assert world['admin'].get(path).status_code == 200
        assert world['rep'].get(path).status_code in (302, 403)
        assert world['triage'].get(path).status_code in (302, 403)
    assert world['triage'].get('/lead-review').status_code == 200, \
        'the fixture reviewer should still reach the review screen'
    r = world['rep'].post('/api/intake/vendors', json={'domain': _d('sneaky')})
    assert r.status_code == 403
    with flask_app.app_context():
        assert VendorDomain.query.filter_by(domain=_d('sneaky')).first() is None


def test_the_intelligence_page_links_to_the_vendor_master(world):
    html = world['admin'].get('/intake-intelligence').get_data(as_text=True)
    assert '/intake/vendors' in html
    src = open(os.path.join(_ROOT, 'templates', 'intake',
                            'vendors.html')).read()
    assert "u.indexOf('{{ url_prefix }}') !== 0" in src
    assert 'X-CSRFToken' in src and 'application/json' in src


# ─── applying a learned proposal ─────────────────────────────────────────
def _rejected(domain, reason='Shipping Line Rate Sourcing', n=3):
    with flask_app.app_context():
        for i in range(n):
            db.session.add(EmailClassification(
                message_id=f'<{domain}-{i}@vm>', subject=f'Rates {i}',
                from_addr=f'sales{i}@{domain}', from_domain=domain,
                classification=li.Klass.NEW_LEAD, decided_by='step_10',
                corrected_to=li.Klass.RATE_SOURCING,
                correction_reason=reason, corrected_by='VMADM',
                corrected_at=datetime.utcnow(), review_state='rejected'))
        db.session.commit()


def test_a_vendor_proposal_suggests_a_category(world):
    _rejected(_d('suggested'))
    with flask_app.app_context():
        p = [p for p in learning.proposals()
             if p['key'] == 'vendor:' + _d('suggested')]
    assert p and p[0]['payload']['category'] == 'shipping line'


def test_applying_a_proposal_uses_the_category_the_admin_chose(world):
    _rejected(_d('chosen'), n=4)
    r = world['admin'].post('/api/intake/proposals/apply', json={
        'key': 'vendor:' + _d('chosen'), 'category': 'transporter'})
    assert r.status_code == 200, r.get_data(as_text=True)
    with flask_app.app_context():
        row = VendorDomain.query.filter_by(domain=_d('chosen')).first()
        assert row.vendor_type == 'transporter'
        assert row.learned_from == 'learned_from_rejections'
        assert row.rejection_count == 4


def test_applying_without_a_category_files_it_as_other(world):
    _rejected(_d('nocategory'))
    with flask_app.app_context():
        ok, msg = learning.apply_proposal('vendor:' + _d('nocategory'),
                                          actor='VMADM')
        assert ok, msg
        db.session.commit()
        assert VendorDomain.query.filter_by(
            domain=_d('nocategory')).first().vendor_type == 'other'


def test_applying_with_an_unknown_category_changes_nothing(world):
    _rejected(_d('unknowncat'))
    r = world['admin'].post('/api/intake/proposals/apply', json={
        'key': 'vendor:' + _d('unknowncat'), 'category': 'spaceship'})
    assert r.status_code == 400
    with flask_app.app_context():
        assert VendorDomain.query.filter_by(
            domain=_d('unknowncat')).first() is None
