"""The five things the 18 Nov UAT asked for that were not built.

The rest of that list was built on 17 Nov and pushed; the server had
not pulled it. These five had not been built at all:

  6a  <meta name="color-scheme" content="light"> on every page, so a
      phone in dark mode stops recolouring form fields the CSS does
      not control.
  6b  "Ask Procam AI" was white on white. The pill took its background
      from `--ink`, which is a TEXT token, so on a dark machine it
      resolved to near-white under a white label.
  8   Country arrived holding "India" on Add Contact, so 670 contacts
      are filed as Indian because nobody emptied a box.
  9   A search for "Hacklin" returned a lead whose PIC is a company
      name, not a person.

Item 7 is not here because it was already done: Stage 3 gave the Find
a person table a card view at <=767px, which covers the 640px asked
for and the 390px it has to survive. The test for it lives in
tests/test_contact_search_19nov.py.
"""
import glob
import os
import re
import sys

os.environ.setdefault('SECRET_KEY', 'test-only-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'Test-123456789')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _templates():
    return glob.glob(os.path.join(ROOT, 'templates', '**', '*.html'),
                     recursive=True)


# ── 6a: the meta, on every page that has a head ──────────────────────
def test_every_page_declares_the_light_scheme():
    missing = []
    for path in _templates():
        body = open(path).read()
        if 'name="viewport"' not in body:
            continue            # a fragment, not a page
        if 'name="color-scheme"' not in body:
            missing.append(os.path.relpath(path, ROOT))
    assert missing == [], (
        'these pages would let a phone darken their form fields:\n  '
        + '\n  '.join(missing))


def test_the_scheme_is_light_and_not_a_pair():
    """`content="light dark"` would be the bug with extra words: it
    tells the browser either is fine, which is what we are stopping."""
    for path in _templates():
        body = open(path).read()
        for said in re.findall(r'name="color-scheme"[^>]*content="([^"]*)"',
                               body):
            assert said.strip() == 'light', (path, said)


def test_no_dark_palette_survives_in_the_css():
    for path in glob.glob(os.path.join(ROOT, 'static', 'css', '*.css')):
        body = open(path).read()
        assert 'prefers-color-scheme: dark' not in body, path
        assert 'prefers-color-scheme:dark' not in body, path
        assert '[data-theme="dark"]' not in body, path


# ── 6b: the AI button is readable ────────────────────────────────────
def test_the_ask_procam_button_does_not_paint_itself_with_a_text_token():
    """A text colour used as a background is the fault, not the shade
    it happened to resolve to. Light mode makes today's value dark; the
    rule is that the surface is named outright."""
    body = open(os.path.join(ROOT, 'templates',
                             '_copilot_panel.html')).read()
    rule = re.search(r'\.pai-fab\{(.*?)\}', body, re.S)
    assert rule, 'the .pai-fab rule has moved'
    said = rule.group(1)
    assert 'background:#16202B' in said.replace(' ', ''), said
    assert 'var(--ink' not in said.split('background:')[1][:40], (
        'the pill is taking its background from a text token again')


# ── 8: Country is asked for, not assumed ─────────────────────────────
def test_add_contact_does_not_arrive_holding_india():
    body = open(os.path.join(ROOT, 'templates', 'app.html')).read()
    field = re.search(r'<input[^>]*id="ctCo"[^>]*>', body)
    assert field, 'the Country field has moved'
    assert 'value="India"' not in field.group(0), field.group(0)
    assert 'required' in field.group(0), field.group(0)


def test_opening_the_form_does_not_fill_the_country_back_in():
    body = open(os.path.join(ROOT, 'templates', 'app.html')).read()
    assert "g('ctCo').value='India'" not in body
    assert "g('ctCo').value=''" in body


def test_saving_without_a_country_is_refused_before_the_request():
    body = open(os.path.join(ROOT, 'templates', 'app.html')).read()
    save = body.split('async function saveCt(')[1][:900]
    assert "g('ctCo').value.trim()" in save, save[:300]
    # Refused in the browser, before the POST, so the person is told
    # which box rather than being handed a server error.
    assert save.index("ctCo") < save.index("api('/api/contacts'"), save[:400]


# ── 9: a company in the name box is not a person ─────────────────────
def test_a_legal_form_in_the_name_box_is_a_company():
    from app.services.people_search import _pic_reads_as_a_company as f
    for said in ('Oy Hacklin Ltd', 'Hacklin Logistics Ltd',
                 'Steinweg GmbH', 'Bollore Logistics Pte',
                 'ABC Pvt. Ltd', 'Kuehne Nagel AG'):
        assert f(said, '') is True, said


def test_a_persons_name_is_still_a_person():
    from app.services.people_search import _pic_reads_as_a_company as f
    for said in ('Alina Kovalenko', 'Ville Ekholm', 'Ravi Nistala',
                 'Rajesh Kumar Sharma', 'Ravi', 'Anna-Maria Virtanen',
                 # 'Oy' and 'Co' as the START of a real word must not
                 # catch: the match is on whole words only.
                 'Oyelaran Adebayo', 'Coimbatore Subramanian',
                 'Abhilash Nair'):
        assert f(said, 'Some Company Ltd') is False, said


def test_the_name_box_filled_in_with_the_company_is_a_company():
    from app.services.people_search import _pic_reads_as_a_company as f
    assert f('Procam Forward', 'Procam Forward') is True
    assert f('procam  forward', 'Procam Forward') is True


def test_unknown_as_a_company_does_not_make_every_pic_a_company():
    """A lead with company "Unknown" is most of them. Matching the PIC
    against that word would empty the search."""
    from app.services.people_search import _pic_reads_as_a_company as f
    assert f('Unknown', 'Unknown') is False
    assert f('Alina Kovalenko', 'Unknown') is False


def test_a_blank_name_is_not_a_company():
    from app.services.people_search import _pic_reads_as_a_company as f
    assert f('', 'Anything Ltd') is False
    assert f(None, None) is False
