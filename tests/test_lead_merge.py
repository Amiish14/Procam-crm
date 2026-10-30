"""
One enquiry, one lead.

CRM-LEAD-FLOW-01 §2.3. Every reply on an email thread was becoming
a new lead — Indo Tech Transformers made four on 19 October and
three on the 22nd, Hitachi Energy two on the 29th under two
spellings of the account. Intake already matches threads; what was
missing was what to do about the ones already split.

Nothing here deletes. A merged lead keeps its row, says where its
work went, and every moved row is counted and audited.
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
os.environ.setdefault('SECRET_KEY', 'merge-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'MergeTest-123456')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'merge.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app = _main.app
db = _main.db
Company, Lead, LeadEmail = _main.Company, _main.Lead, _main.LeadEmail

from app.services import lead_merge as lm                 # noqa: E402

PREFIX = 'LM '
ACTOR = 'LM_ACTOR'


def _purge():
    from app.models.audit import AuditEvent
    ids = [r.id for r in Lead.query.filter(
        Lead.company.like(PREFIX + '%')).all()]
    if ids:
        LeadEmail.query.filter(LeadEmail.lead_id.in_(ids)).delete(
            synchronize_session=False)
        AuditEvent.query.filter(
            AuditEvent.entity_type == 'lead',
            AuditEvent.entity_id.in_([str(i) for i in ids])).delete(
                synchronize_session=False)
    Lead.query.filter(Lead.company.like(PREFIX + '%')).delete(
        synchronize_session=False)
    Company.query.filter(Company.name.like(PREFIX + '%')).delete(
        synchronize_session=False)
    db.session.commit()


@pytest.fixture(autouse=True)
def _clean():
    with flask_app.app_context():
        db.create_all()
        _purge()
        yield
        _purge()


def _account(name='Indo Tech Transformers'):
    account = Company(name=PREFIX + name, is_active=True)
    db.session.add(account)
    db.session.flush()
    return account


def _lead(subject, *, account=None, thread='', stage='New Opportunity',
          email='rfq@indotech-test.com', body='', days=0, project=''):
    lead = Lead(company=PREFIX + 'Indo Tech Transformers',
                company_id=account.id if account else None,
                stage=stage, source='email', email=email,
                original_email_subject=subject,
                original_email_body=body, project=project,
                thread_key=thread,
                created_at=datetime.utcnow() - timedelta(days=days))
    db.session.add(lead)
    db.session.flush()
    return lead


# ── reading a message ────────────────────────────────────────────────
@pytest.mark.parametrize('subject,want', [
    ('RE: 40 MVA transformer', '40 mva transformer'),
    ('FW: Re: Fwd:  40 MVA transformer', '40 mva transformer'),
    ('40 MVA transformer', '40 mva transformer'),
])
def test_the_reply_prefixes_come_off(subject, want):
    assert lm.strip_subject(subject) == want


def test_the_clients_own_reference_is_kept():
    """It is the strongest thing in the line. Stripping it would
    make two different enquiries from one customer look alike."""
    assert 'ttl-hl-in-std-2026-017' in lm.strip_subject(
        'RE: RFQ TTL-HL-IN-STD-2026-017')


def test_a_reference_is_found_in_the_subject_or_the_body():
    assert 'TTL-HL-IN-STD-2026-017' in lm.references(
        'RFQ TTL-HL-IN-STD-2026-017 please quote')
    assert 'GEM/2026/B/1234567' in lm.references(
        'body mentions GEM/2026/B/1234567 somewhere')


def test_ordinary_words_are_not_references():
    assert lm.references('RE: FW: urgent enquiry for a trailer') == []


def test_a_thread_key_prefers_the_conversation_id():
    assert lm.thread_key(conversation_id='AAQk123').startswith('c:')
    assert lm.thread_key(subject='RE: 40 MVA',
                         domain='x.com') == 's:x.com:40 mva'
    assert lm.thread_key() == ''


# ── finding the duplicate ────────────────────────────────────────────
def test_the_same_thread_is_a_certain_duplicate():
    with flask_app.app_context():
        account = _account()
        first = _lead('40 MVA transformer', account=account,
                      thread='c:AAQk1')
        second = _lead('RE: 40 MVA transformer', account=account,
                       thread='c:AAQk1')
        db.session.commit()
        found = lm.candidates(second)
        assert [r['lead_id'] for r in found] == [first.id]
        assert found[0]['certain'] is True
        assert found[0]['why'] == 'same email thread'


def test_the_same_client_reference_is_a_certain_duplicate():
    with flask_app.app_context():
        account = _account()
        first = _lead('Enquiry TTL-HL-IN-STD-2026-017', account=account)
        second = _lead('Transformer move', account=account,
                       body='our ref TTL-HL-IN-STD-2026-017 refers')
        db.session.commit()
        found = lm.candidates(second)
        assert found and found[0]['lead_id'] == first.id
        assert found[0]['certain'] is True
        assert '2026-017' in found[0]['why']


def test_the_same_subject_and_sender_is_only_a_suggestion():
    """Rule 2 alone draws a banner. It never merges anything on its
    own — that is the difference between helping and guessing."""
    with flask_app.app_context():
        account = _account()
        _lead('Transformer to Lapanga', account=account)
        second = _lead('RE: Transformer to Lapanga', account=account)
        db.session.commit()
        found = lm.candidates(second)
        assert found and found[0]['certain'] is False


def test_a_different_sender_is_not_a_duplicate():
    with flask_app.app_context():
        account = _account()
        _lead('Transformer to Lapanga', account=account,
              email='a@one-test.com')
        second = _lead('Transformer to Lapanga', account=account,
                       email='b@other-test.com')
        db.session.commit()
        assert lm.candidates(second) == []


def test_a_closed_lead_is_not_offered_as_a_duplicate():
    with flask_app.app_context():
        account = _account()
        _lead('40 MVA transformer', account=account, thread='c:A',
              stage='Won')
        second = _lead('RE: 40 MVA transformer', account=account,
                       thread='c:A')
        db.session.commit()
        assert lm.candidates(second) == []


def test_an_old_lead_is_outside_the_window():
    with flask_app.app_context():
        account = _account()
        _lead('40 MVA transformer', account=account, thread='c:A',
              days=lm.WINDOW_DAYS + 5)
        second = _lead('RE: 40 MVA transformer', account=account,
                       thread='c:A')
        db.session.commit()
        assert lm.candidates(second) == []


def test_a_lead_is_never_its_own_duplicate():
    with flask_app.app_context():
        account = _account()
        only = _lead('40 MVA transformer', account=account,
                     thread='c:A')
        db.session.commit()
        assert lm.candidates(only) == []


# ── the merge ────────────────────────────────────────────────────────
def test_a_merge_moves_the_emails():
    with flask_app.app_context():
        account = _account()
        keep = _lead('40 MVA transformer', account=account)
        fold = _lead('RE: 40 MVA transformer', account=account)
        db.session.add(LeadEmail(lead_id=fold.id, subject='a reply',
                                 direction='in'))
        db.session.commit()
        ok, detail = lm.merge(fold, keep, actor=ACTOR)
        assert ok, detail
        assert detail['moved'].get('lead_emails.lead_id') == 1
        assert LeadEmail.query.filter_by(lead_id=keep.id).count() == 1


def test_the_merged_lead_is_kept_and_says_where_it_went():
    with flask_app.app_context():
        account = _account()
        keep = _lead('40 MVA transformer', account=account)
        fold = _lead('RE: 40 MVA transformer', account=account)
        db.session.commit()
        fold_id = fold.id
        lm.merge(fold, keep, actor=ACTOR)
        row = db.session.get(Lead, fold_id)
        assert row is not None
        assert row.merged_into_id == keep.id
        assert row.stage == 'Lost'
        assert row.lost_reason == 'Duplicate'


def test_a_merge_writes_an_audit_row_on_both_sides():
    with flask_app.app_context():
        from app.models.audit import AuditEvent
        account = _account()
        keep = _lead('40 MVA transformer', account=account)
        fold = _lead('RE: 40 MVA transformer', account=account)
        db.session.commit()
        lm.merge(fold, keep, actor=ACTOR, reason='Duplicate of the RFQ')
        assert AuditEvent.query.filter_by(
            action='lead.merged', entity_id=str(fold.id)).first()
        absorbed = AuditEvent.query.filter_by(
            action='lead.absorbed', entity_id=str(keep.id)).first()
        assert absorbed is not None and absorbed.actor == ACTOR


def test_a_won_lead_is_never_merged_away():
    """A won deal is a reported figure. Folding it into another
    rewrites a number somebody has already acted on."""
    with flask_app.app_context():
        account = _account()
        keep = _lead('40 MVA transformer', account=account)
        fold = _lead('RE: 40 MVA transformer', account=account,
                     stage='Won')
        db.session.commit()
        ok, why = lm.merge(fold, keep, actor=ACTOR)
        assert ok is False
        assert 'reported figure' in why
        assert db.session.get(Lead, fold.id).merged_into_id is None


def test_a_lead_cannot_be_merged_into_itself():
    with flask_app.app_context():
        account = _account()
        only = _lead('40 MVA transformer', account=account)
        db.session.commit()
        ok, why = lm.merge(only, only, actor=ACTOR)
        assert ok is False and 'same lead' in why


def test_merging_an_already_merged_lead_is_refused():
    with flask_app.app_context():
        account = _account()
        keep = _lead('a', account=account)
        fold = _lead('b', account=account)
        third = _lead('c', account=account)
        db.session.commit()
        lm.merge(fold, keep, actor=ACTOR)
        ok, why = lm.merge(fold, third, actor=ACTOR)
        assert ok is False and 'already merged' in why


def test_merging_into_a_merged_lead_points_at_the_survivor():
    with flask_app.app_context():
        account = _account()
        keep = _lead('a', account=account)
        middle = _lead('b', account=account)
        third = _lead('c', account=account)
        db.session.commit()
        lm.merge(middle, keep, actor=ACTOR)
        ok, why = lm.merge(third, middle, actor=ACTOR)
        assert ok is False
        assert f'merge into that one' in why


# ── the clean-up report (§6.1) ───────────────────────────────────────
def test_the_survivor_is_the_one_with_the_work_on_it():
    with flask_app.app_context():
        account = _account()
        bare = _lead('40 MVA transformer', account=account,
                     thread='c:A')
        worked = _lead('RE: 40 MVA transformer', account=account,
                       thread='c:A')
        db.session.add(LeadEmail(lead_id=worked.id, subject='reply',
                                 direction='in'))
        db.session.commit()
        assert lm.survivor_of([bare, worked]).id == worked.id


def test_the_group_report_finds_a_split_thread():
    with flask_app.app_context():
        account = _account()
        for _ in range(3):
            _lead('40 MVA transformer', account=account, thread='c:A')
        db.session.commit()
        found = [g for g in lm.groups()
                 if g['company'].startswith(PREFIX)]
        assert len(found) == 1
        assert len(found[0]['members']) == 3
        assert sum(1 for m in found[0]['members'] if m['keep']) == 1
        assert found[0]['certain'] is True


def test_the_group_report_changes_nothing():
    with flask_app.app_context():
        account = _account()
        one = _lead('40 MVA transformer', account=account, thread='c:A')
        _lead('RE: 40 MVA transformer', account=account, thread='c:A')
        db.session.commit()
        lm.groups()
        assert db.session.get(Lead, one.id).merged_into_id is None


# ── the flag ─────────────────────────────────────────────────────────
def test_the_flag_is_off_by_default():
    from app.services import flags
    assert flags.DEFAULTS[lm.FLAG] is False
