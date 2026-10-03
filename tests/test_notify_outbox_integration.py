"""From an assignment to an email with the client's request attached.

This is the release's headline claim, so it is tested end to end:
assign a lead that arrived by email, and the person it was given to is
sent the original message and its files — without asking anybody.
"""
import json
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'notify-outbox-test-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'NotifyOutboxTest12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'notifyoutbox.db'))

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Employee, Lead, LeadAttachment = _main.Employee, _main.Lead, _main.LeadAttachment

from app.models.mailops import EmailOutbox, LeadRawEmail  # noqa: E402
from app.services import notify, outbox                   # noqa: E402

TAG = 'NOX-'
OWNER = 'NOXOWN'


def _wipe():
    ids = [l.id for l in Lead.query.filter(Lead.company.like(TAG + '%')).all()]
    if ids:
        LeadAttachment.query.filter(LeadAttachment.lead_id.in_(ids)).delete(
            synchronize_session=False)
        LeadRawEmail.query.filter(LeadRawEmail.lead_id.in_(ids)).delete(
            synchronize_session=False)
        Lead.query.filter(Lead.id.in_(ids)).delete(synchronize_session=False)
    EmailOutbox.query.filter(EmailOutbox.user_code == OWNER).delete(
        synchronize_session=False)
    db.session.commit()


@pytest.fixture()
def world(tmp_path, monkeypatch):
    monkeypatch.setenv('FEATURE_EMAIL_NOTIFY', 'true')
    with flask_app.app_context():
        db.create_all()
        _wipe()
        emp = Employee.query.filter_by(emp_code=OWNER).first() \
            or Employee(emp_code=OWNER)
        emp.name, emp.is_active, emp.must_change_pw = 'Lead Owner', True, False
        emp.email, emp.role, emp.session_version = \
            'lead.owner@procamgroup.in', 'user', 0
        emp.is_vertical_head = emp.is_super_admin = False
        db.session.add(emp)

        lead = Lead(company=TAG + 'Heavy Lift Customer', stage='New Opportunity',
                    source='email')
        db.session.add(lead)
        db.session.flush()

        eml = tmp_path / 'original.eml'
        eml.write_bytes(b'Subject: 3 transformers\r\n\r\nPlease quote.')
        db.session.add(LeadRawEmail(
            lead_id=lead.id, internet_message_id='<nox-1@customer.test>',
            subject='3 transformers', storage_path=str(eml),
            size_bytes=eml.stat().st_size, status='stored'))

        boq = tmp_path / 'BOQ.pdf'
        boq.write_bytes(b'%PDF-1.4 fake')
        db.session.add(LeadAttachment(
            lead_id=lead.id, filename='BOQ.pdf',
            content_type='application/pdf', size_bytes=boq.stat().st_size,
            storage_path=str(boq), source='email'))
        # Quiet hours are on by default and would hold the email until
        # the morning — correct behaviour, and not what these two tests
        # are about. The quiet window has its own tests in test_outbox.
        from app.services import notify_prefs
        notify_prefs.save(OWNER, quiet_enabled=False)
        db.session.commit()
        yield lead
        _wipe()


def test_an_assignment_queues_an_email_carrying_the_original(world):
    from app.services import lead_assignment

    ok, err = lead_assignment.assign(world, primary_code=OWNER,
                                     actor='TESTER', note='for testing')
    assert ok, err

    row = (EmailOutbox.query
           .filter_by(user_code=OWNER, event_key='lead.assigned')
           .order_by(EmailOutbox.id.desc()).first())
    assert row is not None, 'the assignment queued no email'
    assert row.to_addr == 'lead.owner@procamgroup.in'
    refs = json.loads(row.attachments_json or '[]')
    assert refs[0]['kind'] == 'raw_email'
    assert any(r['kind'] == 'lead_attachment' for r in refs)
    assert "original email" in row.html.lower()


def test_the_worker_sends_it_with_the_files_attached(world):
    from app.services import lead_assignment

    lead_assignment.assign(world, primary_code=OWNER, actor='TESTER',
                           note='for testing')
    seen = {}

    def _send(to, subject, html, cc=None, attachments=None, **kw):
        seen['to'] = to
        seen['names'] = [a['filename'] for a in (attachments or [])]
        seen['bytes'] = [a['content'] for a in (attachments or [])]
        return True

    report = outbox.run_once(send=_send, use_lease=False)
    assert report['sent'] >= 1
    assert seen['to'] == ['lead.owner@procamgroup.in']
    assert any(n.endswith('.eml') for n in seen['names']), seen['names']
    assert 'BOQ.pdf' in seen['names']
    assert b'Please quote.' in b''.join(seen['bytes'])


def test_with_the_flag_off_nothing_is_queued(world, monkeypatch):
    monkeypatch.setenv('FEATURE_EMAIL_NOTIFY', 'false')
    sent = []
    from email_ingest import notifier
    monkeypatch.setattr(notifier, 'send',
                        lambda *a, **kw: sent.append(a) or True)

    before = EmailOutbox.query.filter_by(user_code=OWNER).count()
    notify.send(OWNER, kind='lead_assigned', event_key='lead.assigned',
                title='Direct', body='b', email=True, in_app=False,
                dedupe=False)
    assert EmailOutbox.query.filter_by(user_code=OWNER).count() == before
    assert sent, 'with the flag off it must send directly, as it did before'
