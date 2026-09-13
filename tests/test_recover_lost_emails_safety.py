"""
The mailbox recovery script can only repair what was damaged.

It overwrites a lead's original email with the copy read back from the
mailbox. Two ways that used to reach healthy data: --ids accepted any
lead, and the trail row replaced was simply the lead's first inbound
email, which on a lead with replies is a real customer message.
"""
import importlib.util
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'RecoverTestOnly12345')
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'recover.db'))

import pytest                                                  # noqa: E402

from app import app as flask_app, db, Lead, LeadEmail          # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    'recover', os.path.join(_ROOT, 'scripts',
                            '2026_09_22_recover_lost_emails.py'))
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)


@pytest.fixture()
def leads():
    with flask_app.app_context():
        db.create_all()
        made = {}
        for tag, source, body in (
                ('healthy', 'ingested', 'Dear team, please quote 3 ODC.'),
                ('damaged', 'migrated_from_notes', 'called, no answer')):
            l = Lead(company=f'Recover {tag}', source='email',
                     email_message_id=f'<{tag}-rls@x>',
                     original_email_source=source,
                     original_email_body=body)
            db.session.add(l)
            db.session.flush()
            made[tag] = l.id
        db.session.commit()
        yield made
        LeadEmail.query.filter(LeadEmail.lead_id.in_(made.values())).delete(
            synchronize_session=False)
        Lead.query.filter(Lead.id.in_(made.values())).delete(
            synchronize_session=False)
        db.session.commit()


def test_ids_cannot_reach_a_healthy_lead(leads):
    with flask_app.app_context():
        got = R.damaged_leads(Lead, [leads['healthy'], leads['damaged']])
        assert [l.id for l in got] == [leads['damaged']]


def test_all_still_finds_the_damaged_lead(leads):
    with flask_app.app_context():
        assert leads['damaged'] in {l.id for l in R.damaged_leads(Lead)}
        assert leads['healthy'] not in {l.id for l in R.damaged_leads(Lead)}


def test_a_later_reply_is_never_the_row_replaced(leads):
    with flask_app.app_context():
        lid = leads['damaged']
        lead = db.session.get(Lead, lid)
        reply = LeadEmail(lead_id=lid, direction='inbound',
                          message_id='<a-later-reply@x>', body='real reply')
        db.session.add(reply)
        db.session.commit()
        assert R._trail_row(LeadEmail, lead, lead.email_message_id) is None

        damaged = LeadEmail(lead_id=lid, direction='inbound',
                            message_id=None, body='called, no answer')
        db.session.add(damaged)
        db.session.commit()
        assert R._trail_row(LeadEmail, lead,
                            lead.email_message_id).id == damaged.id


def test_the_previous_values_are_saved_before_writing(monkeypatch):
    folder = tempfile.mkdtemp()
    monkeypatch.setattr(R, '_ROOT', folder)
    path = R._save_preimage([{'lead_id': 1, 'original_email_body': 'x'}])
    assert path.startswith(os.path.join(folder, 'backups'))
    assert oct(os.stat(path).st_mode & 0o777) == '0o600'
    assert json.load(open(path))[0]['original_email_body'] == 'x'
