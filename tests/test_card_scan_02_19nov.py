"""CRM-SCAN-02 — content-first scoring, block reading, learning, accuracy."""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_card_scan_16nov import (  # noqa: E402,F401
    BusinessCardImport, Company, _clean, card_samples, client, db,
    flask_app, _upload)
from app.services import card_read, card_smart  # noqa: E402


CARD = """OPTERIX TECHNOLOGIES PVT LTD
Founder, ProductRx
Ravi Nistala
+91 99582 48203
ravi@opterix.in
Plot 12, Sector 18, Gurugram 122015"""


def test_company_from_the_email_domain_and_the_legal_suffix():
    lines = card_smart._lines(CARD)
    ranked = card_smart.company_candidates(lines, 'ravi@opterix.in')
    assert ranked[0]['value'] == 'OPTERIX TECHNOLOGIES PVT LTD'
    assert 'email domain' in ranked[0]['why']


def test_company_worked_out_from_the_domain_when_only_a_logo():
    lines = card_smart._lines('Ravi Nistala\nFounder\nravi@opterix.in')
    ranked = card_smart.company_candidates(lines, 'ravi@opterix.in')
    assert ranked[0]['value'] == 'Opterix'
    assert ranked[0]['score'] < 70           # a guess, flagged


def test_name_from_the_email_local_part_below_the_designation():
    lines = card_smart._lines(CARD)
    ranked = card_smart.name_candidates(lines, 'ravi@opterix.in',
                                        'Founder, ProductRx')
    assert ranked[0]['value'] == 'Ravi Nistala'
    assert 'email' in ranked[0]['why']


def test_designation_and_state_from_the_pin():
    lines = card_smart._lines(CARD)
    assert card_smart.designation_candidates(lines)[0]['value'] == 'Founder, ProductRx'
    assert card_smart.state_candidates(CARD)[0]['value'] == 'Haryana'


def test_rescore_fixes_a_top_to_bottom_misread_and_offers_alternatives():
    with flask_app.app_context():
        fields = {'name': 'OPTERIX TECHNOLOGIES PVT LTD', 'company': '',
                  'email': 'ravi@opterix.in', 'raw_text': CARD,
                  'confidence': {}}
        out = card_smart.rescore(fields, CARD)
    assert out['name'] == 'Ravi Nistala'
    assert out['first_name'] == 'Ravi' and out['last_name'] == 'Nistala'
    assert out['company'] == 'OPTERIX TECHNOLOGIES PVT LTD'
    assert out['state'] == 'Haryana'
    assert out['candidates']['name']
    assert 'score' in out['confidence']['company']


def test_blocks_are_read_column_first():
    # Left column: name and title; right column: phone and email.
    data = {'text': ['Ravi', 'Nistala', 'Founder', '+91', '99582', 'ravi@x.in'],
            'conf': ['90'] * 6, 'block_num': [1, 1, 1, 2, 2, 2],
            'par_num': [1] * 6, 'line_num': [1, 1, 2, 1, 1, 2],
            'left': [10, 60, 10, 400, 440, 400], 'top': [10, 10, 40, 10, 10, 40],
            'width': [40, 60, 70, 30, 50, 90]}
    text = card_read.blocks_from_data(data, 600)
    assert text.split('\n')[:2] == ['Ravi Nistala', 'Founder']
    assert text.index('Founder') < text.index('+91 99582')


def test_four_segmentations_are_tried():
    assert len(card_read.configs()) == 4
    assert '--psm 4' in card_read.configs()[2]


def test_a_correction_is_learned_for_the_domain(client):
    with flask_app.app_context():
        from app.models.business_card import CardDomainHint
        CardDomainHint.query.filter_by(domain='zcopterix.example').delete()
        db.session.commit()
        assert card_smart.learn({'company': 'Zcopterix',
                                 'email': 'a@zcopterix.example'},
                                {'company': 'ZC Opterix Technologies Pvt Ltd',
                                 'email': 'a@zcopterix.example'})
        db.session.commit()
        assert card_smart.learned_company('b@zcopterix.example') \
            == 'ZC Opterix Technologies Pvt Ltd'
        ranked = card_smart.company_candidates(
            ['Someone'], 'b@zcopterix.example',
            learned=card_smart.learned_company('b@zcopterix.example'))
        assert ranked[0]['score'] == 97


def test_the_upload_carries_candidates_and_the_accuracy_page_loads(client):
    card = _upload(client, card_samples.by_key('plain'))
    assert card['extracted'].get('candidates')
    page = client.get('/business-cards/accuracy')
    assert page.status_code == 200
    assert 'Card reading accuracy' in page.get_data(as_text=True)


def test_the_review_form_shows_confidence_and_a_dropdown():
    html = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'templates', 'business_card',
        'scan.html')).read()
    assert 'function confBadge' in html and 'cand-pick' in html
