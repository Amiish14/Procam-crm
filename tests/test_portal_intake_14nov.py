"""The portal backlog driven through the real intake, not described.

14 Nov. The grouping, the ordering and the plan are asserted in
`tests/test_portal_events_14nov.py`. This file does the thing the plan
only promises: it puts a set of portal messages through
`process_single_message` itself, in the order `--apply` would, and
asks what the database looks like afterwards.

Three messages about one auction have to become ONE lead with two
messages on its trail, and the withdrawal has to close it. The only
way to be sure of that is to run it.

`graph=None` is how the other intake suites drive this function: the
Graph client is only needed to fetch attachments, and nothing here
has any.
"""
import importlib
import os
import sys
import tempfile
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.modules.setdefault('msal', types.ModuleType('msal'))

os.environ['URL_PREFIX'] = ''
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'portal-intake')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'PortalIntake12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'portalintake.db')
os.environ['LEAD_INTAKE_MODE'] = 'enforce'
os.environ['LEAD_INTAKE_AI'] = 'off'

_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Lead = _main.Lead

from app.models.ingest_log import MailIngestLog                # noqa: E402
from app.services import portal_rfq                            # noqa: E402
from email_ingest import single_message as SM                   # noqa: E402

MAILBOX = 'leads@procamgroup.in'
PORTAL = 'no-reply@procol.in'

with flask_app.app_context():
    db.create_all()


def _msg(mid, subject, body, sender=PORTAL, when=None):
    return {
        'id': 'AAMk' + mid.strip('<>@.').replace('-', ''),
        'internetMessageId': mid,
        'subject': subject,
        'body': {'contentType': 'text', 'content': body},
        'receivedDateTime': when or '2026-11-10T09:00:00Z',
        'from': {'emailAddress': {'address': sender}},
        'toRecipients': [{'emailAddress': {'address': MAILBOX}}],
    }


def _leads(key):
    with flask_app.app_context():
        return Lead.query.filter_by(portal_key=key).all()


def _log(mid):
    with flask_app.app_context():
        return MailIngestLog.query.filter_by(
            internet_message_id=mid).first()


# ── one auction, four mails, one lead ────────────────────────────────
INVITE = _msg(
    '<zq-inv@procol.in>', 'RFX 44811: invitation to quote',
    'Buyer organisation: ZQ Cement Works\n'
    'You are invited to quote for 40 MT, Chennai to Hosur.\n'
    'Event RFX 44811.', when='2026-11-10T09:00:00Z')
REMINDER = _msg(
    '<zq-rem@procol.in>', 'Reminder: RFX 44811 awaits your response',
    'Your response is due. Event RFX 44811.',
    when='2026-11-11T09:00:00Z')
ENDING = _msg(
    '<zq-end@procol.in>', 'RFX 44811 is about to end',
    'The event RFX 44811 is about to end on 30 Nov 2026 15:15:00 IST.',
    when='2026-11-12T09:00:00Z')
WITHDRAWN = _msg(
    '<zq-wdr@procol.in>', 'RFX 44811 has been withdrawn',
    'The buyer has withdrawn event RFX 44811.',
    when='2026-11-13T09:00:00Z')


def test_the_whole_set_lands_as_one_lead_and_ends_closed():
    """Oldest first, exactly as `apply_order` returns them."""
    out = SM.process_single_message(None, MAILBOX, INVITE)
    assert out['status'] in ('created', 'ok'), out
    made = _leads('RFX-44811')
    assert len(made) == 1, [l.id for l in made]
    lead_id = made[0].id

    # The invitation is the lead, and it announces itself as unread.
    with flask_app.app_context():
        lead = db.session.get(Lead, lead_id)
        assert lead.needs_review is True
        assert lead.lead_platform == 'procol.in'
        assert lead.company == 'ZQ Cement Works'
        assert lead.portal_key == 'RFX-44811'

    # The reminder joins it. No second lead.
    second = SM.process_single_message(None, MAILBOX, REMINDER)
    assert second['lead_id'] == lead_id, second
    assert 'RFX-44811' in second['reason']
    assert len(_leads('RFX-44811')) == 1

    # "About to end" carries the deadline.
    third = SM.process_single_message(None, MAILBOX, ENDING)
    assert third['lead_id'] == lead_id, third
    with flask_app.app_context():
        lead = db.session.get(Lead, lead_id)
        assert lead.quote_due_at is not None
        assert lead.quote_due_confirmed is not True
        assert lead.stage not in ('Lost', 'Won')

    # And the withdrawal closes it, in the words asked for.
    fourth = SM.process_single_message(None, MAILBOX, WITHDRAWN)
    assert fourth['lead_id'] == lead_id, fourth
    with flask_app.app_context():
        lead = db.session.get(Lead, lead_id)
        assert lead.stage == 'Lost'
        assert lead.lost_reason == portal_rfq.LOST_REASON
    assert len(_leads('RFX-44811')) == 1


def test_every_one_of_the_four_is_in_the_ingest_log():
    """Four messages, four rows, and the reasons name the event."""
    for mid in ('<zq-inv@procol.in>', '<zq-rem@procol.in>',
                '<zq-end@procol.in>', '<zq-wdr@procol.in>'):
        row = _log(mid)
        assert row is not None, mid
        assert row.outcome in ('created', 'attached'), (mid, row.outcome)
    assert 'RFX-44811' in (_log('<zq-wdr@procol.in>').reason or '')
    assert 'Lost' in (_log('<zq-wdr@procol.in>').reason or '')


def test_a_withdrawal_of_an_event_we_never_saw_makes_no_lead():
    """~30 of the 74. A lead born Lost is not work anybody can do."""
    before = Lead.query.count() if False else None
    with flask_app.app_context():
        before = Lead.query.count()
    out = SM.process_single_message(None, MAILBOX, _msg(
        '<zq-ghost@procol.in>', 'RFX 90001 has been withdrawn',
        'The buyer has withdrawn event RFX 90001.'))
    assert out['lead_id'] is None, out
    assert out['classification'] == 'portal_withdrawn_unknown'
    with flask_app.app_context():
        assert Lead.query.count() == before
        assert not Lead.query.filter_by(portal_key='RFX-90001').all()
    # Refused, with a record. Not a silent drop.
    row = _log('<zq-ghost@procol.in>')
    assert row is not None
    assert row.outcome == 'skipped'
    assert 'RFX-90001' in (row.reason or '')
    assert 'withdrawn' in (row.reason or '')


def test_a_withdrawal_reaches_a_lead_somebody_already_closed():
    """It must not make a second lead for an event that has one, even
    a closed one."""
    out = SM.process_single_message(None, MAILBOX, _msg(
        '<zq-inv2@procol.in>', 'RFX 44822: invitation to quote',
        'Buyer organisation: ZQ Pipes Limited\nEvent RFX 44822.'))
    assert out.get('lead_id') or _leads('RFX-44822')
    lead_id = _leads('RFX-44822')[0].id
    with flask_app.app_context():
        lead = db.session.get(Lead, lead_id)
        lead.stage = 'Lost'
        lead.lost_reason = 'Price too high'
        db.session.commit()

    again = SM.process_single_message(None, MAILBOX, _msg(
        '<zq-wdr2@procol.in>', 'RFX 44822 has been withdrawn',
        'The buyer has withdrawn event RFX 44822.'))
    assert again['lead_id'] == lead_id, again
    assert len(_leads('RFX-44822')) == 1
    with flask_app.app_context():
        lead = db.session.get(Lead, lead_id)
        # A person's own reason is not overwritten by a machine's.
        assert lead.lost_reason == 'Price too high'


def test_a_reminder_for_an_unseen_event_still_becomes_a_lead():
    """The opposite decision to the withdrawal, and deliberately: a
    reminder means the auction is still open and we can still bid."""
    out = SM.process_single_message(None, MAILBOX, _msg(
        '<zq-rem3@procol.in>', 'Reminder: RFX 44833 awaits your response',
        'Buyer organisation: ZQ Glass Works\nYour response is due for '
        'event RFX 44833.'))
    made = _leads('RFX-44833')
    assert len(made) == 1, out
    with flask_app.app_context():
        lead = db.session.get(Lead, made[0].id)
        assert lead.needs_review is True
        assert lead.stage not in ('Lost',)


def test_a_portal_lead_really_does_land_on_the_rfq_desk():
    """Asserted on the lead the intake writes, not on the helper that
    decides. The helper was tested on 13 Nov and was never reached:
    everything above it refused the message first."""
    with flask_app.app_context():
        from app import Employee
        from app.services import admin_settings
        db.create_all()
        Employee.query.filter_by(emp_code='ZQ_DESK').delete()
        db.session.add(Employee(
            emp_code='ZQ_DESK', name='Desk Seat', password_hash='x',
            role='user', email='zq.desk@procamlogistics.com',
            is_active=True))
        admin_settings.save('rfq_coordinator', 'ZQ_DESK', actor='test')
        db.session.commit()

    SM.process_single_message(None, MAILBOX, _msg(
        '<zq-desk@procol.in>', 'RFX 44844: invitation to quote',
        'Buyer organisation: ZQ Paper Mills\nEvent RFX 44844.'))
    made = _leads('RFX-44844')
    assert len(made) == 1
    with flask_app.app_context():
        lead = db.session.get(Lead, made[0].id)
        assert lead.assigned_to == 'ZQ_DESK', lead.assigned_to
