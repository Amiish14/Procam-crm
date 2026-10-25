"""
Marking 254 auctions that arrived before the CRM could tell.

The backfill's whole risk is the stage column. 245 of the 254 are
closed, many of them closed deliberately, and a backfill that
nudged them back into the pipeline would hand the sales team 245
dead leads to re-close — a worse morning than the one that
deleted them in the first place. The type changes; nothing else
does.

The other half of these tests is the closing date, which is read
only where the message states one. Two of the nine live auctions
carry "Eta at Haldia 20.10.26" — the cargo arriving, not the
auction closing. A parser that grabs any date it sees would print
that as a deadline and send somebody to bid against a date that
means nothing, so "not stated" has to survive contact with a
message that is full of dates.
"""
import importlib
import os
import sys
import tempfile
from datetime import date, datetime, timedelta

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'backfill-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'BackfillTest-12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'backfill.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
importlib.import_module('app.models.master_data')
importlib.import_module('app.models.audit')
flask_app = _main.app
db = _main.db
Lead = _main.Lead
Employee = _main.Employee

from app.models.audit import AuditEvent                   # noqa: E402
from app.models.integration import AppSetting             # noqa: E402
from app.models.master_data import MasterItem             # noqa: E402
from app.services import admin_settings, lead_kinds       # noqa: E402

sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
import auction_backfill as ab                             # noqa: E402

OWNER = 'EMP_AUCT'


def _purge():
    for model in (Lead, AuditEvent):
        model.query.delete()
    Employee.query.filter(Employee.emp_code.in_(
        (OWNER, 'EMP_OTHER'))).delete(synchronize_session=False)
    # The suite shares one database. An owner left nominated here
    # fails test_lead_kinds' "nobody by default" guard in whatever
    # module runs next, and the seeded patterns would make another
    # module's ordinary mail classify as an auction.
    AppSetting.query.filter_by(
        key=lead_kinds.OWNER_SETTING).delete(synchronize_session=False)
    MasterItem.query.filter_by(
        list_key=lead_kinds.LIST_KEY).delete(synchronize_session=False)
    db.session.commit()


@pytest.fixture(autouse=True)
def _clean():
    with flask_app.app_context():
        db.create_all()
        _purge()
        for code, name in ((OWNER, 'Auction Desk'),
                           ('EMP_OTHER', 'Someone Else')):
            db.session.add(Employee(
                emp_code=code, name=name,
                email=f'{code.lower()}@procamlogistics.com',
                password_hash='x', role='Sales'))
        admin_settings.save(lead_kinds.OWNER_SETTING, OWNER,
                            actor='test')
        lead_kinds.seed(actor='test')
        db.session.commit()
        yield
        _purge()


def _lead(subject, *, stage='New', owner='EMP_OTHER', company='Isgec',
          body='', archived=False, lost_reason=None):
    lead = Lead(company=company, stage=stage, source='email',
                assigned_to=owner, assigned_name=owner,
                forwarded_subject=subject, original_email_body=body,
                original_email_from='noreply@alerts.superprocure.com',
                lost_reason=lost_reason, is_archived=archived,
                created_at=datetime.utcnow() - timedelta(days=3))
    db.session.add(lead)
    db.session.commit()
    return lead


# ── the stage is never touched ───────────────────────────────────────
@pytest.mark.parametrize('stage,reason', [
    ('Lost', 'Others: junk'),
    ('Won', None),
    ('On Hold', None),
])
def test_a_closed_auction_stays_closed(stage, reason):
    with flask_app.app_context():
        lead = _lead('FW: Isgec : Load Post Notification',
                     stage=stage, lost_reason=reason)
        rows = ab.survey()
        ab.apply_rows(rows)
        db.session.commit()

        fresh = db.session.get(Lead, lead.id)
        assert fresh.lead_type == 'auction'
        assert fresh.stage == stage, 'the backfill reopened a lead'
        assert fresh.lost_reason == reason


def test_a_closed_auction_keeps_the_person_who_closed_it():
    """Reassigning 245 dead leads tells people they own old work."""
    with flask_app.app_context():
        lead = _lead('FW: Isgec : Load Post Notification',
                     stage='Lost', owner='EMP_OTHER')
        ab.apply_rows(ab.survey())
        db.session.commit()
        assert db.session.get(Lead, lead.id).assigned_to == 'EMP_OTHER'


# ── the live ones ────────────────────────────────────────────────────
def test_an_open_auction_goes_to_the_configured_owner():
    with flask_app.app_context():
        lead = _lead('FW: Isgec : Load Post Notification',
                     stage='New', owner='EMP_OTHER')
        ab.apply_rows(ab.survey())
        db.session.commit()

        fresh = db.session.get(Lead, lead.id)
        assert fresh.assigned_to == OWNER
        # The person who had it does not lose sight of it.
        assert fresh.secondary_owner == 'EMP_OTHER'


def test_an_archived_lead_is_not_live():
    with flask_app.app_context():
        _lead('FW: Isgec : Load Post Notification', stage='New',
              archived=True)
        rows = ab.survey()
        assert rows and not rows[0]['open']


def test_the_owner_is_required_before_anything_is_assigned():
    """Unowned is how the last 226 were swept."""
    with flask_app.app_context():
        admin_settings.save(lead_kinds.OWNER_SETTING, '', actor='test')
        db.session.commit()
        assert lead_kinds.owner() == ''
        lead = _lead('FW: Isgec : Load Post Notification')
        ab.apply_rows(ab.survey())
        db.session.commit()
        fresh = db.session.get(Lead, lead.id)
        # Typed, so the sweeps leave it alone, but not handed to
        # a person who was never nominated.
        assert fresh.lead_type == 'auction'
        assert fresh.assigned_to == 'EMP_OTHER'


# ── the closing date ─────────────────────────────────────────────────
@pytest.mark.parametrize('subject,expected,clock', [
    ('FW: Japanese reverse auction | 03/09/2026@ 3:00 PM',
     date(2026, 9, 3), '3:00 PM'),
    ('Japanese reverse auction | 05/10/2026 @ 12:30 PM',
     date(2026, 10, 5), '12:30 PM'),
    ('Reverse Auction | 20.10.26', date(2026, 10, 20), ''),
])
def test_a_stated_deadline_is_read(subject, expected, clock):
    with flask_app.app_context():
        lead = _lead(subject)
        when, got, _where = ab.closing_of(lead, subject)
        assert when == expected
        assert got == clock


@pytest.mark.parametrize('subject', [
    # The real trap: a date that is the cargo's arrival.
    'FW: 2 x 40 HC Kolkata : Eta at Haldia 20.10.26',
    'Shipment update — ETA 20.10.26, please confirm delivery',
    # Ariba names no date in the mail; it is inside the event.
    'Jindal Stainless has invited you to participate in an event: '
    'RFQ_Transporter Required Doc5566124961',
    'FW: Isgec : Load Post Notification',
])
def test_a_date_that_is_not_a_deadline_is_not_read_as_one(subject):
    with flask_app.app_context():
        lead = _lead(subject)
        when, _clock, where = ab.closing_of(lead, subject)
        assert when is None, f'read a deadline out of {subject!r}'
        assert 'not stated' in where


def test_a_deadline_in_the_body_is_found():
    with flask_app.app_context():
        lead = _lead('FW: Isgec : Load Post Notification',
                     body='Kindly submit your best rate up to '
                          '14.09.2026 for the below load.')
        when, _c, where = ab.closing_of(lead, lead.forwarded_subject)
        assert when == date(2026, 9, 14)
        assert where == 'the message'


def test_a_past_deadline_is_flagged():
    with flask_app.app_context():
        _lead('Reverse Auction | 03/09/2026 @ 3:00 PM')
        row = ab.survey()[0]
        assert row['overdue'] is True


def test_a_future_deadline_is_not_flagged():
    with flask_app.app_context():
        later = date.today() + timedelta(days=20)
        _lead(f'Reverse Auction | {later:%d/%m/%Y} @ 3:00 PM')
        assert ab.survey()[0]['overdue'] is False


# ── audit and reversal ───────────────────────────────────────────────
def test_every_change_leaves_an_audit_row():
    with flask_app.app_context():
        for n in range(3):
            _lead(f'FW: Isgec : Load Post Notification {n}')
        ab.apply_rows(ab.survey())
        db.session.commit()
        assert AuditEvent.query.filter_by(action=ab.ACTION).count() == 3


def test_running_it_twice_changes_nothing_the_second_time():
    with flask_app.app_context():
        _lead('FW: Isgec : Load Post Notification')
        ab.apply_rows(ab.survey())
        db.session.commit()
        typed, _a, _w = ab.apply_rows(ab.survey())
        db.session.commit()
        assert typed == []
        assert AuditEvent.query.filter_by(action=ab.ACTION).count() == 1


def test_down_clears_the_type_and_leaves_the_stage():
    with flask_app.app_context():
        lead = _lead('FW: Isgec : Load Post Notification',
                     stage='Lost', lost_reason='Others: junk')
        ab.apply_rows(ab.survey())
        db.session.commit()
        ab.undo()
        db.session.commit()
        fresh = db.session.get(Lead, lead.id)
        assert not fresh.lead_type
        assert fresh.stage == 'Lost'
        assert fresh.lost_reason == 'Others: junk'


# ── an ordinary enquiry is left alone ────────────────────────────────
def test_an_ordinary_enquiry_is_not_swept_up():
    with flask_app.app_context():
        lead = Lead(company='Tata Steel', stage='New', source='email',
                    forwarded_subject='Quotation request for 2x20ft '
                                      'Nhava Sheva to Jebel Ali',
                    original_email_from='buyer@tatasteel.com',
                    created_at=datetime.utcnow() - timedelta(days=2))
        db.session.add(lead)
        db.session.commit()
        assert ab.survey() == []
