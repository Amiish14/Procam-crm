"""The thread key: filled at last, and what it now catches.

CRM-USER-ISSUES-02NOV §2: "A reply on the same email thread creates a
duplicate lead."

The column, the key function, the classifier step and the merge rule
all existed. The column was never written — by anything, ever, except
three tests that set it by hand. So the one *certain* duplicate rule in
`lead_merge.candidates()` compared NULL to NULL and could not fire.

The test that matters most here is
`test_the_certain_merge_rule_can_actually_fire_now`: it is the one
that would have failed before this change and the reason §2 was still
being reported after CRM-LEAD-FLOW-01 shipped.
"""
import importlib
import os
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'thread-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'LeadThread-12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'thread.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app = _main.app
db = _main.db
Lead = _main.Lead

from app.services import lead_intake as li                 # noqa: E402
from app.services import lead_intake_db as lidb            # noqa: E402
from app.services import lead_merge as lm                  # noqa: E402
from app.services import lead_thread as lt                 # noqa: E402

DOMAIN = 'zzthread-client.example'
SENDER = f'buyer@{DOMAIN}'
SUBJECT = '40 MVA transformer from Nashik to Bhopal'


@pytest.fixture(autouse=True)
def _clean():
    with flask_app.app_context():
        db.create_all()
        Lead.query.delete()
        db.session.commit()
        yield
        Lead.query.delete()
        db.session.commit()


def _msg(*, subject=SUBJECT, sender=SENDER, conversation=None,
         in_reply_to=None, references=None, message_id=None):
    headers = []
    if in_reply_to:
        headers.append({'name': 'In-Reply-To', 'value': in_reply_to})
    if references:
        headers.append({'name': 'References', 'value': references})
    return {
        'subject': subject,
        'from': {'emailAddress': {'address': sender}},
        'conversationId': conversation,
        'internetMessageId': message_id,
        'internetMessageHeaders': headers,
        'toRecipients': [],
        'ccRecipients': [],
    }


def _lead(*, subject=SUBJECT, sender=SENDER, conversation=None,
          thread_key=None, created=None, stage='New'):
    lead = Lead(company='Thread Client', stage=stage, source='email',
                original_email_subject=subject,
                original_email_from=sender,
                email=sender,
                conversation_id=conversation,
                thread_key=thread_key,
                created_at=created or datetime.utcnow())
    db.session.add(lead)
    db.session.commit()
    return lead


# ── the key itself ───────────────────────────────────────────────────
def test_a_conversation_id_wins_over_the_subject():
    key = lt.key_for_message(_msg(conversation='AAQkAD00'))
    assert key.startswith('c:')
    assert lt.is_windowed(key) is False


def test_without_one_it_falls_back_to_subject_and_domain():
    key = lt.key_for_message(_msg())
    assert key.startswith('s:')
    assert DOMAIN in key
    assert lt.is_windowed(key) is True


def test_the_reply_prefix_does_not_change_the_key():
    """The whole point: a reply keys the same as what it replies to."""
    first = lt.key_for_message(_msg(subject=SUBJECT))
    reply = lt.key_for_message(_msg(subject='RE: ' + SUBJECT))
    fwd = lt.key_for_message(_msg(subject='FW: Re: ' + SUBJECT))
    assert first == reply == fwd


def test_a_different_customer_on_the_same_subject_is_a_different_thread():
    mine = lt.key_for_message(_msg())
    theirs = lt.key_for_message(_msg(sender='buyer@zzsomeone-else.example'))
    assert mine != theirs


def test_the_resolved_sender_is_used_when_given():
    """A relayed enquiry must key on the customer, not the colleague."""
    relayed = _msg(sender='colleague@procamlogistics.com')
    keyed_on_relayer = lt.key_for_message(relayed)
    keyed_on_client = lt.key_for_message(relayed, sender=SENDER)
    assert keyed_on_relayer != keyed_on_client
    assert DOMAIN in keyed_on_client


def test_thread_keys_carries_the_stored_key():
    keys = li.thread_keys(_msg(conversation='AAQkAD00'))
    assert keys['thread_key'].startswith('c:')


def test_thread_keys_prefers_the_resolved_sender_the_classifier_set():
    relayed = _msg(sender='colleague@procamlogistics.com')
    relayed['_resolved_sender'] = SENDER
    assert DOMAIN in li.thread_keys(relayed)['thread_key']


# ── filling it ───────────────────────────────────────────────────────
def test_fill_sets_it_and_never_overwrites():
    with flask_app.app_context():
        lead = _lead(thread_key=None)
        lt.fill(lead, _msg(conversation='AAQkAD00'))
        first = lead.thread_key
        assert first.startswith('c:')
        lt.fill(lead, _msg(conversation='AAQkDIFFERENT'))
        assert lead.thread_key == first


def test_key_for_lead_reads_the_columns_the_intake_stored():
    with flask_app.app_context():
        lead = _lead(conversation='AAQkAD00')
        assert lt.key_for_lead(lead).startswith('c:')
        plain = _lead(conversation=None)
        assert lt.key_for_lead(plain).startswith('s:')


# ── looking it up ────────────────────────────────────────────────────
def test_the_earliest_lead_on_the_thread_is_the_one_found():
    """Not the newest: the thread belongs to the enquiry that started
    it, and a reply filed against the newest duplicate builds the
    trail on the wrong lead."""
    with flask_app.app_context():
        key = lt.key_for_message(_msg())
        first = _lead(thread_key=key)
        _lead(thread_key=key)
        assert lt.find_by_key(key) == first.id


def test_an_archived_or_junked_lead_is_not_resurrected_by_a_reply():
    with flask_app.app_context():
        key = lt.key_for_message(_msg())
        gone = _lead(thread_key=key)
        gone.is_archived = True
        db.session.commit()
        assert lt.find_by_key(key) is None


def test_a_subject_key_is_windowed_and_a_conversation_key_is_not():
    with flask_app.app_context():
        old = datetime.utcnow() - timedelta(days=lm.WINDOW_DAYS + 10)

        s_key = lt.key_for_message(_msg())
        _lead(thread_key=s_key, created=old)
        assert lt.find_by_key(s_key) is None, \
            'an old subject key is a different enquiry'

        c_key = lt.key_for_message(_msg(conversation='AAQkAD00'))
        kept = _lead(thread_key=c_key, created=old,
                     conversation='AAQkAD00')
        assert lt.find_by_key(c_key) == kept.id, \
            'a conversation id is the same thread at any age'


# ── the classifier reaches it ────────────────────────────────────────
def test_find_by_thread_falls_through_to_the_stored_key():
    with flask_app.app_context():
        key = lt.key_for_message(_msg())
        lead = _lead(thread_key=key)
        # No conversation id, no In-Reply-To, no References — the case
        # the headers cannot answer and the stored key can.
        assert lidb.find_by_thread(thread_key=key) == lead.id


def test_the_headers_are_still_tried_first():
    with flask_app.app_context():
        wanted = _lead(conversation='AAQkAD00', thread_key=None)
        other_key = lt.key_for_message(_msg())
        _lead(thread_key=other_key)
        got = lidb.find_by_thread(conversation_id='AAQkAD00',
                                  thread_key=other_key)
        assert got == wanted.id


def test_a_reply_with_no_prefix_and_no_headers_still_finds_its_lead():
    """The gap §2 is actually about. Step 5 of the classifier only
    fires on a Re:/Fw: prefix; a client whose mail client dropped it
    had nothing to match on at all."""
    with flask_app.app_context():
        key = lt.key_for_message(_msg())
        lead = _lead(thread_key=key)
        plain_reply = _msg(subject=SUBJECT)      # no RE:, no headers
        keys = li.thread_keys(plain_reply)
        assert lidb.find_by_thread(**{
            k: keys[k] for k in ('conversation_id', 'in_reply_to',
                                 'reference_ids', 'thread_key')
        }) == lead.id


# ── the rule that could never fire ───────────────────────────────────
def test_the_certain_merge_rule_can_actually_fire_now():
    """This is the test that would have failed before the change.

    `lead_merge.candidates()` calls a thread match "certain". With
    `thread_key` NULL on every row — which it was, in production,
    since the column was created — the comparison was NULL == NULL and
    the rule was dead code.
    """
    with flask_app.app_context():
        key = lt.key_for_message(_msg())
        first = _lead(thread_key=key)
        second = _lead(thread_key=key)
        found = lm.candidates(second)
        mine = [c for c in found if c['lead_id'] == first.id]
        assert mine, f'the thread duplicate was not offered: {found!r}'
        # Not merely "offered": offered on the thread rule, as a
        # certainty. These two also share a subject and a sender, and
        # that weaker rule only ever "draws a banner" — so asserting
        # the row exists would pass without the fix.
        assert mine[0]['why'] == 'same email thread'
        assert mine[0]['certain'] is True


def test_two_leads_with_no_key_are_not_called_duplicates():
    """NULL == NULL must not mean "same thread"."""
    with flask_app.app_context():
        _lead(thread_key=None, subject='One enquiry')
        second = _lead(thread_key=None, subject='A different enquiry')
        assert all(c['why'] != 'same email thread'
                   for c in lm.candidates(second))


# ── the regression that actually happened ────────────────────────────
#
# A column declared, migrated, indexed, read in two places and never
# written. Unit tests on the key function all passed; the feature did
# not work. The guard is therefore on the writing, in the same shape
# as the CRM-NO-DELETE-01 source guard: find every Lead(...) the two
# intake paths construct and insist thread_key is among its keywords.
import ast                                                 # noqa: E402


def _lead_constructions(path):
    """Every `Lead(...)` call in a file, as a list of keyword names."""
    tree = ast.parse(open(path).read())
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (getattr(func, 'id', None)
                or getattr(func, 'attr', None))
        if name != 'Lead':
            continue
        out.append({kw.arg for kw in node.keywords if kw.arg})
    return out


@pytest.mark.parametrize('path', [
    'email_ingest/pipeline.py',
    'email_ingest/single_message.py',
])
def test_every_intake_path_writes_the_thread_key(path):
    full = os.path.join(_ROOT, path)
    builds = _lead_constructions(full)
    assert builds, f'no Lead(...) found in {path} — has it moved?'
    for kwargs in builds:
        # A Lead built from **kwargs alone is this check's blind spot,
        # so insist the name is present either directly or in the
        # dict the call spreads.
        assert 'thread_key' in kwargs, (
            f'{path} creates a Lead without thread_key. That is the '
            f'§2 bug exactly: the column exists, is indexed, is read '
            f'by lead_merge.candidates() as its one certain rule, and '
            f'was never written by anything.')


# ── the report §2 asked for ──────────────────────────────────────────
import importlib.util                                      # noqa: E402


def _script():
    path = os.path.join(_ROOT, 'scripts',
                        '2026_11_03_thread_duplicates.py')
    spec = importlib.util.spec_from_file_location('thread_dups', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_report_groups_two_leads_on_one_thread():
    with flask_app.app_context():
        mod = _script()
        key = lt.key_for_message(_msg())
        a = _lead(thread_key=key)
        b = _lead(thread_key=key)
        groups, _unkeyable = mod.survey()
        found = mod.pairs(groups)
        ids = [[l.id for l in g['leads']] for g in found]
        assert [a.id, b.id] in ids


def test_a_lone_lead_is_not_a_pair():
    with flask_app.app_context():
        mod = _script()
        _lead(thread_key=lt.key_for_message(_msg()))
        assert mod.pairs(mod.survey()[0]) == []


def test_the_oldest_is_the_one_suggested_to_keep():
    with flask_app.app_context():
        mod = _script()
        key = lt.key_for_message(_msg())
        first = _lead(thread_key=key)
        _lead(thread_key=key)
        group = mod.pairs(mod.survey()[0])[0]
        assert group['leads'][0].id == first.id


def test_a_conversation_pair_is_reported_as_certain():
    with flask_app.app_context():
        mod = _script()
        key = 'c:AAQkAD00'
        _lead(thread_key=key, conversation='AAQkAD00')
        _lead(thread_key=key, conversation='AAQkAD00')
        group = mod.pairs(mod.survey()[0])[0]
        assert group['certain'] is True


def test_two_subject_matches_months_apart_are_not_a_pair():
    """Same customer, same word in the subject, two different
    enquiries. The window is measured between them, not against
    today, so a pair from March is still a pair in November."""
    with flask_app.app_context():
        mod = _script()
        key = lt.key_for_message(_msg())
        _lead(thread_key=key,
              created=datetime.utcnow() - timedelta(days=200))
        _lead(thread_key=key, created=datetime.utcnow())
        assert mod.pairs(mod.survey()[0]) == []


def test_an_old_pair_close_together_is_still_a_pair():
    with flask_app.app_context():
        mod = _script()
        key = lt.key_for_message(_msg())
        old = datetime.utcnow() - timedelta(days=200)
        _lead(thread_key=key, created=old)
        _lead(thread_key=key, created=old + timedelta(days=1))
        assert len(mod.pairs(mod.survey()[0])) == 1


def test_the_backfill_dry_run_writes_nothing():
    with flask_app.app_context():
        mod = _script()
        lead = _lead(thread_key=None, conversation='AAQkAD00')
        groups, unkeyable = mod.survey()
        mod.backfill(groups, unkeyable, apply=False)
        assert Lead.query.get(lead.id).thread_key is None


def test_the_backfill_fills_it_when_applied_and_never_overwrites():
    with flask_app.app_context():
        mod = _script()
        fresh = _lead(thread_key=None, conversation='AAQkAD00')
        held = _lead(thread_key='c:SOMETHING-ELSE',
                     conversation='AAQkAD00')
        groups, unkeyable = mod.survey()
        mod.backfill(groups, unkeyable, apply=True)
        assert Lead.query.get(fresh.id).thread_key == 'c:AAQkAD00'
        assert Lead.query.get(held.id).thread_key == 'c:SOMETHING-ELSE'


def test_there_is_no_merge_option():
    """§2: "Don't merge them; I decide."

    Asked of the registered options and the assignments, not of the
    source text — the module docstring says the words "--merge" and
    "merge" on purpose, to explain their absence.
    """
    path = os.path.join(_ROOT, 'scripts',
                        '2026_11_03_thread_duplicates.py')
    tree = ast.parse(open(path).read())

    options = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and getattr(node.func, 'attr', None) == 'add_argument'):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and \
                        isinstance(arg.value, str):
                    options.add(arg.value)
    assert options, 'no options found — has the parser moved?'
    assert not [o for o in options if 'merge' in o.lower()]

    # And nothing assigns the column a merge would set.
    written = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute):
                    written.add(target.attr)
    assert 'merged_into_id' not in written
    assert written <= {'thread_key'}, (
        f'the report writes more than the backfill it declares: '
        f'{sorted(written)}')
