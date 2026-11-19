"""UAT 19 Nov: a contact can be edited — account picked, audited,
duplicate check on save, placeholder accounts refused."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_contact_search_15nov import (  # noqa: E402,F401
    Contact, _account, _clean, _client, _contact, _token, db)

from app.models.audit import AuditEvent  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_HTML = open(os.path.join(_ROOT, 'templates', 'app.html')).read()
SEARCH_HTML = open(os.path.join(_ROOT, 'templates', 'contact_search.html')).read()


_C = {}


def _put(cid, body):
    if 'c' not in _C:
        _C['c'] = _client()
        _C['t'] = _token(_C['c'])
    return _C['c'].put(f'/api/contacts/{cid}', json=body,
                       headers={'X-CSRFToken': _C['t']})


def test_edit_links_the_picked_account_and_is_audited():
    acct = _account('ZT Fried Spedition GmbH')
    ct = _contact('ZT Joachim Holst', company='New Lead')
    r = _put(ct.id, {'company_id': acct.id, 'designation': 'ZT Manager',
                     'country': 'Germany', 'state': 'Hamburg',
                     'vertical': 'Freight Forwarding'})
    assert r.status_code == 200, r.get_data(as_text=True)
    row = db.session.get(Contact, ct.id)
    assert (row.company, row.company_id, row.account_id) == (
        'ZT Fried Spedition GmbH', acct.id, acct.id)
    assert row.country == 'Germany' and row.state == 'Hamburg'
    assert row.vertical == 'Freight Forwarding'
    ev = AuditEvent.query.filter_by(action='contact.edited',
                                    entity_id=str(ct.id)).first() or \
        AuditEvent.query.filter_by(action='contact.edited',
                                   entity_id=ct.id).first()
    assert ev is not None


def test_a_placeholder_account_is_refused():
    ph = _account('ZT placeholder')
    ph.name = 'Unknown'
    db.session.commit()
    try:
        ct = _contact('ZT Mayukh Dam')
        r = _put(ct.id, {'company_id': ph.id})
        assert r.status_code == 400
        assert db.session.get(Contact, ct.id).company_id is None
    finally:
        db.session.delete(ph)
        db.session.commit()


def test_duplicate_check_then_save_anyway():
    _contact('ZT Rinku Biswas', email='zt.rinku@vikramsolar.com')
    ct = _contact('ZT Other Person', email='zt.other@example.com')
    r = _put(ct.id, {'email': 'zt.rinku@vikramsolar.com'})
    assert r.status_code == 409 and r.get_json()['needs'] == 'duplicate_check'
    assert db.session.get(Contact, ct.id).email == 'zt.other@example.com'
    r = _put(ct.id, {'email': 'zt.rinku@vikramsolar.com', 'save_anyway': True})
    assert r.status_code == 200, r.get_data(as_text=True)
    db.session.expire_all()
    assert db.session.get(Contact, ct.id).email == 'zt.rinku@vikramsolar.com'


def test_saving_itself_is_not_a_duplicate():
    ct = _contact('ZT Nutan Mishra', email='zt.nutan@rashmigroup.com')
    r = _put(ct.id, {'name': 'ZT Nutan Mishra', 'email': 'zt.nutan@rashmigroup.com',
                     'city': 'Kolkata'})
    assert r.status_code == 200, r.get_data(as_text=True)


def test_the_screens_offer_edit():
    assert 'function openContactEdit' in APP_HTML
    assert "location.hash === '#edit'" in APP_HTML
    assert "'#edit'" in SEARCH_HTML and 'fs-edit' in SEARCH_HTML
