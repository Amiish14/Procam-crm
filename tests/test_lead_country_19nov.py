"""UAT 19 Nov: the lead form carries the contact person's country."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_contact_search_15nov import (  # noqa: E402,F401
    Lead, _clean, _client, _lead, _token, db)

HTML = open(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'templates', 'app.html')).read()


def test_the_form_has_the_field_and_sends_it_only_when_changed():
    assert 'id="lCtry"' in HTML and 'data-was=' in HTML
    assert "g('lCtry').value.trim() !== (g('lCtry').dataset.was||'')" in HTML


def test_the_lead_api_saves_the_country():
    lead = _lead(pic='ZT Jenna', country='India')
    client = _client()
    r = client.put(f'/api/leads/{lead.id}', json={'country': 'Finland'},
                   headers={'X-CSRFToken': _token(client)})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert db.session.get(Lead, lead.id).country == 'Finland'
