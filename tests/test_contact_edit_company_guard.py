"""The company picker asks only when the company has been changed.

UAT 19 Nov. Contact #443, Jenna Nirho, carries the company TEXT
"HACKLIN LOGISTICS LTD" and no linked account. The Edit form opened
with that text in the box and `accountId` null, so

    if (typed && !ceState.accountId) → refuse

refused every save, including one that only touched the phone. A
contact whose company nobody has linked yet is most of this book, and
being unable to correct a number until somebody finds the right account
is a worse fault than the untidiness the guard was protecting against.

The rule now: an account is required only where the company field has
actually been CHANGED. The placeholder refusal of 850ee8d is kept --
a placeholder opens the box EMPTY, so typing into it is a change and is
still asked about.

The decision is four lines of browser JavaScript, so the test models
those four lines and asserts they are the ones in the file. If somebody
edits the guard, `test_the_guard_in_the_file_is_the_one_modelled` fails
rather than the behaviour tests quietly passing against a copy.
"""
import os
import re
import sys

os.environ.setdefault('SECRET_KEY', 'test-only-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'Test-123456789')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_HTML = os.path.join(ROOT, 'templates', 'app.html')

#: The three lines the behaviour below models.
GUARD_LINES = (
    'const companyChanged = typed !== ceState.companyAtOpen;',
    'if (companyChanged && typed && !ceState.accountId)',
    'if (companyChanged && !typed)',
)

SAVED = 'saved'
SAVED_LINKED = 'saved and linked'
NEEDS_PICK = 'needs a pick'
CLEARING_REFUSED = 'clearing refused'


def decide(opened_with, typed_now, account_id, picked):
    """The guard, as the template runs it."""
    typed = (typed_now or '').strip()
    company_changed = typed != opened_with
    if company_changed and typed and not account_id:
        return NEEDS_PICK
    if company_changed and not typed:
        return CLEARING_REFUSED
    return SAVED_LINKED if (picked and account_id) else SAVED


def _save_fn():
    body = open(APP_HTML).read()
    return body.split('async function saveContactEdit(')[1].split(
        '\n  const body = {')[0]


def test_the_guard_in_the_file_is_the_one_modelled():
    fn = _save_fn()
    for line in GUARD_LINES:
        assert line in fn, line


# ── the fault reported ───────────────────────────────────────────────
def test_443_saves_when_only_the_phone_changed():
    """The company text is there, no account is linked, and the company
    box has not been touched."""
    assert decide('HACKLIN LOGISTICS LTD', 'HACKLIN LOGISTICS LTD',
                  None, False) == SAVED


def test_an_already_linked_contact_saves_untouched():
    assert decide('Hacklin Logistics', 'Hacklin Logistics', 12,
                  False) == SAVED


def test_typing_and_putting_it_back_is_not_a_change():
    assert decide('HACKLIN LOGISTICS LTD', 'HACKLIN LOGISTICS LTD',
                  None, False) == SAVED


# ── what is still asked about ────────────────────────────────────────
def test_changing_the_company_still_needs_a_pick():
    assert decide('HACKLIN LOGISTICS LTD', 'Hacklin Group', None,
                  False) == NEEDS_PICK


def test_changing_it_and_picking_links_the_account():
    assert decide('HACKLIN LOGISTICS LTD', 'Hacklin Group Oy', 77,
                  True) == SAVED_LINKED


def test_a_placeholder_company_is_still_asked_about():
    """850ee8d opens the box EMPTY for "New Lead" / "Unknown", so typing
    into it is a change and the pick is still required."""
    assert decide('', 'Hacklin Log', None, False) == NEEDS_PICK
    assert decide('', 'Hacklin Logistics Ltd', 12, True) == SAVED_LINKED


def test_a_placeholder_left_alone_does_not_block_the_save():
    """The rest of the form is still editable on a contact nobody has
    given a real company yet."""
    assert decide('', '', None, False) == SAVED


def test_emptying_the_box_is_refused_rather_than_ignored():
    """The save carries no company text at all, so a cleared box would
    be silently dropped. It says so instead."""
    assert decide('HACKLIN LOGISTICS LTD', '', None,
                  False) == CLEARING_REFUSED
    assert decide('HACKLIN LOGISTICS LTD', '   ', None,
                  False) == CLEARING_REFUSED


# ── the wiring the behaviour depends on ──────────────────────────────
def test_the_opened_value_is_read_from_the_box_after_it_is_drawn():
    """It has to be the box's own value: for a placeholder the box opens
    empty while the contact's `company` still holds the placeholder."""
    body = open(APP_HTML).read()
    fn = body.split('async function openContactEdit(')[1][:4000]
    assert "ceState.companyAtOpen = (g('ceCo').value || '').trim();" in fn
    assert fn.index('document.body.appendChild(mob)') < \
        fn.index('ceState.companyAtOpen ='), (
        'the value is read before the form is in the document')


def test_an_untouched_company_sends_no_account_at_all():
    """So the server leaves the text and the link exactly as they were.
    `company_id` is optional on the PUT -- app.py only reads it when it
    is present."""
    fn = _save_fn() + open(APP_HTML).read().split(
        'async function saveContactEdit(')[1][:3000]
    assert ('if (ceState.picked && ceState.accountId) '
            'body.company_id = ceState.accountId;') in fn


def test_picking_from_the_list_records_that_it_was_picked():
    body = open(APP_HTML).read()
    fn = body.split('function cePick(')[1][:400]
    assert 'ceState.picked = true' in fn


def test_typing_drops_a_previous_pick():
    """Otherwise an account picked and then typed over would be linked
    to a name nobody chose."""
    body = open(APP_HTML).read()
    fn = body.split('function ceSearch(')[1][:900]
    assert 'ceState.picked = false' in fn
    assert 'ceState.accountId = null' in fn


def test_the_server_still_refuses_a_placeholder_account():
    """The client guard got looser; the server's did not."""
    body = open(os.path.join(ROOT, 'app.py')).read()
    fn = body.split("@app.route('/api/contacts/<int:cid>', methods=['PUT'])")[1][:3000]
    assert 'not_a_customer(account)' in fn
    assert 'pick the real company' in fn


def test_the_put_only_reads_the_account_when_one_is_sent():
    body = open(os.path.join(ROOT, 'app.py')).read()
    fn = body.split("@app.route('/api/contacts/<int:cid>', methods=['PUT'])")[1][:3000]
    assert "if d.get('company_id'):" in fn
