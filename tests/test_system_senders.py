"""
Mail from a machine is not an enquiry.

Two Microsoft 365 quarantine digests became leads called
"Microsoft". They sat in the pipeline until somebody closed them,
and one is why the word "Claude" turned up in a notes field — the
digest was listing a spammer's subject line.

The inbox is deliberately the filter: `single_message` captures
everything, because a missed enquiry costs more than a junk lead.
These tests hold that trade in place. The rule may only catch
senders that are definitionally not customers, and the cost of
getting it wrong — a real enquiry held — is what most of them are
about.
"""
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'sender-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'SenderTest-12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'senders.db'))

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app = _main.app
db = _main.db

from app.services import system_senders as ss             # noqa: E402

# `app.models` does not reach master_data, so the table it lives in
# is not on the metadata unless it is imported by name. Without
# this the list tests fail on "no such table" rather than on
# anything they are testing.
importlib.import_module('app.models.master_data')
with flask_app.app_context():
    db.create_all()


@pytest.fixture
def empty_list():
    from app.models.master_data import MasterItem

    with flask_app.app_context():
        MasterItem.query.filter_by(list_key=ss.LIST_KEY).delete()
        db.session.commit()
        yield MasterItem
        MasterItem.query.filter_by(list_key=ss.LIST_KEY).delete()
        db.session.commit()


# ── what it catches ──────────────────────────────────────────────────
@pytest.mark.parametrize('sender', [
    'quarantine@messaging.microsoft.com',
    'QUARANTINE@Messaging.Microsoft.Com',
    'postmaster@someone.com',
    'mailer-daemon@mail.example.net',
    'bounces@campaign.example',
    'alerts@protection.outlook.com',
    'googlealerts-noreply@google.com',
])
def test_a_machine_is_recognised(sender):
    blocked, pattern, why = ss.blocks(sender, rules=ss.SEEDS)
    assert blocked, sender
    assert pattern and why


@pytest.mark.parametrize('sender,what', [
    ('noreply@alerts.superprocure.com',
     'freight load postings — "Load posted by Isgec Heavy '
     'Engineering Ltd", route, vehicle type, tonnage'),
    ('noreply@alerts2.superprocure.com', 'the same portal'),
    ('noreply@us.bn.cloud.ariba.com',
     'RFQ invitations — "Jindal Stainless has invited you to '
     'participate in an event: RFQ_Transporter Required"'),
    ('do-not-reply@hlag.com', 'a carrier\'s own notices'),
    ('noreply@drewry.co.uk', 'shipping research somebody reads'),
    ('noreply@fffai.org',
     'the freight forwarders\' federation — an industry body'),
])
def test_a_reply_less_address_is_not_a_robot(sender, what):
    """The seeds had noreply@*, no-reply@*, donotreply@* and
    do-not-reply@* until the 90-day report was read. They would
    have held 247 leads, 226 of them live freight enquiries.

    A procurement portal sends from noreply@ precisely because the
    reply belongs in the portal. The address says nothing about
    whether the message is business, and blocking that shape drops
    the enquiries this company exists to win.
    """
    blocked, pattern, _why = ss.blocks(sender, rules=ss.SEEDS)
    assert not blocked, f'{sender} ({what}) held by {pattern}'


def test_the_wide_reply_less_patterns_are_gone_from_the_seeds():
    patterns = [p for p, _w in ss.SEEDS]
    for wide in ('noreply@*', 'no-reply@*', 'donotreply@*',
                 'do-not-reply@*'):
        assert wide not in patterns, (
            f'{wide} is back. It held 226 SuperProcure load '
            f'postings and the Ariba RFQ invitations in the '
            f'90-day report.')


# ── what it must never catch ─────────────────────────────────────────
@pytest.mark.parametrize('sender', [
    'praveen.kolli@coscon.com',
    'buyer@siemens.com',
    'noreplacement@client.com',          # starts with "norep"
    'bouncecastle@realcustomer.com',     # starts with "bounce"
    'postmaster.general@ministry.gov.in',
    'john.noreply@client.com',           # not the local part
    'sales@messaging-microsoft.com.cn',  # not the real domain
    '',
    None,
])
def test_a_person_is_never_held(sender):
    """A missed enquiry costs more than a junk lead, which is why
    the inbox captures everything in the first place."""
    blocked, _p, _w = ss.blocks(sender, rules=ss.SEEDS)
    assert not blocked, sender


def test_the_wildcard_is_only_a_wildcard():
    """`*` is the only special character. Anything cleverer is a
    regex language in a box an administrator types into, and the
    first mistake silently holds a customer."""
    rules = (('a.b@c.com', 'exact'),)
    assert ss.blocks('a.b@c.com', rules=rules)[0]
    assert not ss.blocks('axb@c.com', rules=rules)[0], \
        'the dot was treated as a regex wildcard'


def test_a_pattern_matches_the_whole_address():
    rules = (('no-reply@*', 'x'),)
    assert ss.blocks('no-reply@a.com', rules=rules)[0]
    assert not ss.blocks('x-no-reply@a.com', rules=rules)[0]
    assert not ss.blocks('no-reply@a.com.evil', rules=rules)[0] is False


# ── the admin's list wins ────────────────────────────────────────────
def test_the_seeds_answer_until_a_person_edits_the_list(empty_list):
    with flask_app.app_context():
        assert ss.patterns() == list(ss.SEEDS)


def test_an_admin_list_replaces_the_seeds_entirely(empty_list):
    """Including when they switch one off. A rule somebody turned
    off that came back from the seeds would be a rule nobody can
    turn off."""
    MasterItem = empty_list
    with flask_app.app_context():
        db.session.add(MasterItem(
            list_key=ss.LIST_KEY, code='robot@example.com',
            label='robot@example.com', description='theirs',
            is_active=True))
        db.session.add(MasterItem(
            list_key=ss.LIST_KEY, code='postmaster@*',
            label='postmaster@*', description='turned off',
            is_active=False))
        db.session.commit()
        rules = ss.patterns()
        assert rules == [('robot@example.com', 'theirs')]
        assert ss.blocks('postmaster@x.com', rules=rules)[0] \
            is False, 'a rule switched off came back'
        assert ss.blocks('robot@example.com', rules=rules)[0]


def test_seeding_is_idempotent(empty_list):
    with flask_app.app_context():
        first = ss.seed(actor='T')
        db.session.commit()
        second = ss.seed(actor='T')
        db.session.commit()
        assert len(first) == len(ss.SEEDS)
        assert second == []


# ── off until somebody turns it on ───────────────────────────────────
def test_it_is_off_by_default():
    with flask_app.app_context():
        from app.services import flags
        flags.cache_clear()
        assert 'FEATURE_SENDER_BLOCK' in flags.NAMES
        assert ss.enabled() is False


def test_the_ingest_checks_the_flag_and_keeps_the_message():
    """Held mail is logged with its own outcome and never deleted:
    an administrator filtering that screen sees what was held and
    the pattern that caught it."""
    src = open(os.path.join(_ROOT, 'email_ingest',
                            'single_message.py')).read()
    assert 'system_senders.enabled()' in src
    assert "_record_ingest('blocked'" in src
    # and a person replaying a message by hand overrides it
    block = src.split('system_senders.enabled()')[0][-400:]
    assert 'if not force:' in block

    from app.models.ingest_log import BLOCKED, OUTCOMES
    assert BLOCKED == 'blocked' and BLOCKED in OUTCOMES


def test_nothing_is_deleted_by_any_of_this():
    src = open(os.path.join(_ROOT, 'app', 'services',
                            'system_senders.py')).read()
    for banned in ('session.delete', '.delete()', 'DELETE'):
        assert banned not in src
