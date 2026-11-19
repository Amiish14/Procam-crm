"""UAT 19 Nov: placeholder accounts are never linked or created from the
card scanner or the contact form."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_contact_search_15nov import (  # noqa: E402,F401
    Company, _account, _clean, _client, _token, db)


def test_the_card_scanner_never_answers_a_placeholder():
    from app.business_card import routes
    _account(name='New Lead', website='ztplaceholder.example')
    assert routes._account_for('New Lead', 'www.ztplaceholder.example') is None


def test_the_contact_form_never_creates_a_placeholder_account():
    client = _client()
    before = Company.query.filter(db.func.lower(Company.name) == 'unknown').count()
    r = client.post('/api/contacts', json={
        'type': 'company', 'name': 'ZT Somebody', 'company': 'Unknown',
        'use_existing': True}, headers={'X-CSRFToken': _token(client)})
    assert r.status_code in (200, 201, 400, 409), r.get_data(as_text=True)
    after = Company.query.filter(db.func.lower(Company.name) == 'unknown').count()
    assert after == before
