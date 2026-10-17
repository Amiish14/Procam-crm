"""
CRM-MAIL-VISIBILITY-01 — where did my mail go?

Sanjeev forwarded two enquiries to leads@ on 23 September and told
Seema the next day that neither was in the CRM.

One of them was. It had been attached to lead #11807, which is stage
Lost, so it was nowhere in his pipeline and nothing told him. The
other was dropped: the top message in the forward was the client's
"Will revert asap", our own offer was further down the chain, and the
classifier read a reply and stopped.

Neither was a failure of the rules. Both were failures to tell
anybody.
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'mailvis-test-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'MailVisTest12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'mailvis.db'))

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Employee, Lead = _main.Employee, _main.Lead
LeadStageHistory = _main.LeadStageHistory
flask_app.config['WTF_CSRF_ENABLED'] = False

from app.models.access import AccessProfile, DataScope    # noqa: E402
from app.models.ingest_log import MailIngestLog           # noqa: E402
from app.models.mailops import EmailOutbox                # noqa: E402
from app.services import flags, lead_intake as li         # noqa: E402
from app.services import mail_signals, my_mail            # noqa: E402

SANJEEV, SEEMA, OTHER = 'MVSANJ', 'MVSEEMA', 'MVOTHER'
TAG = 'MV '


def _emp(code, name, *, admin=False, scope=DataScope.OWN):
    e = Employee.query.filter_by(emp_code=code).first() or Employee(emp_code=code)
    e.name, e.is_active, e.must_change_pw = name, True, False
    e.email = f'{code.lower()}@procamgroup.in'
    e.role = 'admin' if admin else 'user'
    e.is_super_admin, e.is_vertical_head, e.session_version = admin, False, 0
    e.vertical = 'Project Freight'
    db.session.add(e)
    db.session.flush()
    AccessProfile.query.filter_by(emp_code=code).delete()
    db.session.add(AccessProfile(emp_code=code, data_scope=scope, perms=[]))
    return e


def _wipe():
    MailIngestLog.query.filter(
        MailIngestLog.subject.like(TAG + '%')).delete(
            synchronize_session=False)
    EmailOutbox.query.filter(
        EmailOutbox.dedupe_key.like('forward-receipt:%')).delete(
            synchronize_session=False)
    ids = [l.id for l in Lead.query.filter(Lead.company.like(TAG + '%'))]
    if ids:
        LeadStageHistory.query.filter(
            LeadStageHistory.lead_id.in_(ids)).delete(
                synchronize_session=False)
        Lead.query.filter(Lead.id.in_(ids)).delete(synchronize_session=False)
    db.session.commit()


@pytest.fixture()
def world(monkeypatch):
    for flag in ('FEATURE_MY_MAIL', 'FEATURE_FORWARD_RECEIPT',
                 'FEATURE_REOPEN_ON_MAIL', 'FEATURE_QUOTE_REPLY_CLASS'):
        monkeypatch.setenv(flag, 'true')
    flags.cache_clear()
    with flask_app.app_context():
        db.create_all()
        _wipe()
        _emp(SANJEEV, 'Sanjeev Paliwal')
        _emp(SEEMA, 'Seema Admin', admin=True, scope=DataScope.ALL)
        _emp(OTHER, 'Someone Else')
        lost = Lead(company=TAG + 'Pandacs Ltd', assigned_to=SANJEEV,
                    assigned_name=SANJEEV, stage='Lost',
                    procam_vertical='Sea Freight',
                    created_at=datetime.utcnow() - timedelta(days=40))
        theirs = Lead(company=TAG + 'Not Mine Ltd', assigned_to=OTHER,
                      assigned_name=OTHER, stage='Quoted',
                      created_at=datetime.utcnow() - timedelta(days=10))
        db.session.add_all([lost, theirs])
        db.session.flush()
        db.session.add(LeadStageHistory(
            lead_id=lost.id, from_stage='Under Negotiation', to_stage='Lost',
            changed_at=datetime.utcnow() - timedelta(days=20)))
        db.session.commit()
        yield {'lost': lost.id, 'theirs': theirs.id}
        _wipe()
        flags.cache_clear()


def _row(**kw):
    row = MailIngestLog(
        internet_message_id=kw.pop('imid', f'<mv-{datetime.utcnow().timestamp()}>'),
        received_at=kw.pop('received_at', datetime.utcnow()),
        subject=kw.pop('subject', TAG + 'Enquiry DAP AE260904542'),
        from_addr=kw.pop('from_addr', 'buyer@pandacs.com'),
        outcome=kw.pop('outcome', 'attached'),
        **kw)
    db.session.add(row)
    db.session.commit()
    return row


def _client(code):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code=code, name=code, role='user', sv=0)
    return c


# ── A. My Mail ───────────────────────────────────────────────────────
def test_i_see_what_i_forwarded(world):
    _row(forwarded_by_emp=SANJEEV, forwarded_by='mvsanj@procamgroup.in',
         lead_id=world['lost'], lead_stage_at_ingest='Lost')
    rows = my_mail.rows(SANJEEV)
    assert len(rows) == 1
    assert rows[0]['lead_id'] == world['lost']
    assert rows[0]['outcome_label'] == 'Attached'


def test_the_stage_it_landed_on_is_shown(world):
    """"It was attached to a Lost lead" is the whole explanation, and
    the stage has usually moved by the time anybody asks."""
    _row(forwarded_by_emp=SANJEEV, lead_id=world['lost'],
         lead_stage_at_ingest='Lost')
    row = my_mail.rows(SANJEEV)[0]
    assert row['stage_then'] == 'Lost'
    assert row['lead_stage'] == 'Lost'
    assert row['stage_moved'] is False

    lead = db.session.get(Lead, world['lost'])
    lead.stage = 'Under Negotiation'
    db.session.commit()
    row = my_mail.rows(SANJEEV)[0]
    assert row['stage_moved'] is True


def test_i_see_mail_that_landed_on_my_lead_even_if_i_did_not_forward_it(world):
    _row(forwarded_by_emp=None, lead_id=world['lost'])
    assert len(my_mail.rows(SANJEEV)) == 1


def test_i_do_not_see_anyone_elses(world):
    """A page that showed one person another's client mail would be a
    worse problem than the one it fixes."""
    _row(forwarded_by_emp=OTHER, lead_id=world['theirs'])
    assert my_mail.rows(SANJEEV) == []
    assert len(my_mail.rows(SEEMA, is_admin=True)) == 1


def test_an_admin_can_filter_by_forwarder(world):
    _row(forwarded_by_emp=SANJEEV, lead_id=world['lost'])
    _row(forwarded_by_emp=OTHER, lead_id=world['theirs'],
         imid='<mv-other>')
    assert len(my_mail.rows(SEEMA, is_admin=True)) == 2
    only = my_mail.rows(SEEMA, is_admin=True, forwarded_by=SANJEEV)
    assert len(only) == 1 and only[0]['forwarded_by_emp'] == SANJEEV


def test_the_badge_counts_only_skipped_and_only_this_week(world):
    _row(forwarded_by_emp=SANJEEV, outcome='skipped', imid='<mv-s1>')
    _row(forwarded_by_emp=SANJEEV, outcome='created', imid='<mv-s2>')
    _row(forwarded_by_emp=SANJEEV, outcome='skipped', imid='<mv-s3>',
         received_at=datetime.utcnow() - timedelta(days=30))
    assert my_mail.badge(SANJEEV) == 1


def test_a_row_id_in_a_url_is_not_proof_it_is_mine(world):
    row = _row(forwarded_by_emp=OTHER, lead_id=world['theirs'])
    assert my_mail.may_see(SANJEEV, row) is False
    assert my_mail.may_see(OTHER, row) is True
    assert my_mail.may_see(SANJEEV, row, is_admin=True) is True


def test_the_page_answers_and_refuses_the_original_of_someone_elses(world):
    row = _row(forwarded_by_emp=OTHER, lead_id=world['theirs'])
    c = _client(SANJEEV)
    assert c.get('/my-mail').status_code == 200
    assert c.get(f'/my-mail/{row.id}/original').status_code == 403


def test_the_page_says_so_when_the_switch_is_off(world, monkeypatch):
    monkeypatch.setenv('FEATURE_MY_MAIL', 'false')
    flags.cache_clear()
    res = _client(SANJEEV).get('/my-mail')
    assert res.status_code == 200
    assert b'not switched on' in res.data


# ── B. the receipt ───────────────────────────────────────────────────
def test_the_forwarder_is_told_what_happened(world):
    from app.services import forward_receipt
    row = _row(forwarded_by_emp=SANJEEV, lead_id=world['lost'],
               outcome='attached', lead_stage_at_ingest='Lost')
    assert forward_receipt.send_for(row) is True
    queued = EmailOutbox.query.filter_by(
        dedupe_key=f'forward-receipt:{row.id}').first()
    assert queued is not None
    assert queued.to_addr == 'mvsanj@procamgroup.in'
    assert f'#{world["lost"]}' in queued.subject
    assert 'Lost' in queued.html


def test_a_skipped_mail_says_why_and_offers_the_way_out(world):
    from app.services import forward_receipt
    row = _row(forwarded_by_emp=SANJEEV, outcome='skipped',
               reason='Top message is a reply; no enquiry found')
    forward_receipt.send_for(row)
    queued = EmailOutbox.query.filter_by(
        dedupe_key=f'forward-receipt:{row.id}').first()
    assert 'Top message is a reply' in queued.html
    assert 'Create a lead from it' in queued.html
    assert f'/my-mail?msg={row.id}' in queued.html


def test_a_mail_nobody_internal_forwarded_gets_no_receipt(world):
    """It came straight from a client. There is no colleague waiting
    to hear back, and the client must never be written to."""
    from app.services import forward_receipt
    row = _row(forwarded_by_emp=None, from_addr='buyer@pandacs.com')
    assert forward_receipt.send_for(row) is False


def test_the_receipt_is_behind_its_flag(world, monkeypatch):
    from app.services import forward_receipt
    monkeypatch.setenv('FEATURE_FORWARD_RECEIPT', 'false')
    flags.cache_clear()
    row = _row(forwarded_by_emp=SANJEEV, lead_id=world['lost'])
    assert forward_receipt.send_for(row) is False


# ── C. mail on a closed lead ─────────────────────────────────────────
def test_a_pre_alert_is_shipment_activity(world):
    assert mail_signals.looks_like_shipment(subject='Pre-alert AE260904542')
    assert mail_signals.looks_like_shipment(subject='HBL and MBL attached')
    assert mail_signals.looks_like_shipment(
        subject='Booking confirmation - vessel')
    assert not mail_signals.looks_like_shipment(
        subject='Newsletter: our October update')


def test_a_closed_lead_comes_back_to_where_it_was(world):
    """Not to a nominated stage. A deal that was in Under Negotiation
    when it was lost is not a fresh RFQ."""
    lead = db.session.get(Lead, world['lost'])
    assert mail_signals.last_open_stage(lead, fallback='RFQ Generated') \
        == 'Under Negotiation'


def test_a_lead_with_no_history_falls_back(world):
    lead = Lead(company=TAG + 'No History Ltd', stage='Lost',
                assigned_to=SANJEEV)
    db.session.add(lead)
    db.session.commit()
    assert mail_signals.last_open_stage(lead, fallback='RFQ Generated') \
        == 'RFQ Generated'


def test_shipment_mail_reopens_a_lost_lead(world):
    from app.services import mail_ingest
    lead = db.session.get(Lead, world['lost'])
    msg = {'subject': 'Pre-alert for AE260904542',
           'bodyPreview': 'HBL attached, vessel ETA 14 Oct'}
    assert mail_ingest.should_reopen(lead, 'E_reply', msg=msg) is True
    assert mail_ingest.reopen_for(lead, msg, classification='E_reply') is True
    assert lead.stage == 'Under Negotiation'
    assert 'Reopened by inbound email' in (lead.history or '')
    assert 'shipment activity' in (lead.history or '')


def test_an_ordinary_reply_does_not_reopen(world):
    from app.services import mail_ingest
    lead = db.session.get(Lead, world['lost'])
    msg = {'subject': 'Thanks', 'bodyPreview': 'Noted with thanks.'}
    assert mail_ingest.should_reopen(lead, 'E_reply', msg=msg) is False
    assert lead.stage == 'Lost'


def test_reopening_is_behind_its_flag(world, monkeypatch):
    from app.services import mail_ingest
    monkeypatch.setenv('FEATURE_REOPEN_ON_MAIL', 'false')
    flags.cache_clear()
    lead = db.session.get(Lead, world['lost'])
    msg = {'subject': 'Pre-alert AE260904542', 'bodyPreview': 'HBL attached'}
    assert mail_ingest.should_reopen(lead, 'E_reply', msg=msg) is False


def test_a_closed_lead_with_mail_reaches_my_work(world):
    """lead_items reads open leads only, which is exactly why #11807
    was invisible."""
    from app.access import scope as sc_mod
    from app.workbench import service as wb

    _row(forwarded_by_emp=SANJEEV, lead_id=world['lost'],
         lead_stage_at_ingest='Lost')
    items = wb.closed_mail_items(sc_mod.for_employee(SANJEEV))
    assert [i['id'] for i in items] == [world['lost']]
    assert 'mail_on_closed' in items[0]['conditions']

    board = wb.board(sc_mod.for_employee(SANJEEV), per_page=200)
    assert board['counts']['new_mail_on_closed'] == 1
    got = wb.board(sc_mod.for_employee(SANJEEV),
                   filter_key='new_mail_on_closed', per_page=200)
    assert got['total'] == 1


# ── D. forwarded offers and client replies ───────────────────────────
BRAHMOS = {
    'subject': 'Fw: Our Offer - Import Customs Clearance (BAPL)',
    'from': {'emailAddress': {'address': 'mvsanj@procamgroup.in'}},
    'toRecipients': [{'emailAddress': {'address': 'leads@procamgroup.in'}}],
    'bodyPreview': ('Will revert asap\n\nFrom: ops@procamgroup.in\n'
                    'To: karansampath@brahmos.com\n'
                    'Subject: Our Offer - Import Customs Clearance\n'
                    'Please find our offer for customs clearance.'),
}


def test_a_forwarded_offer_is_business_not_a_reply(world):
    assert mail_signals.looks_like_quote(
        subject=BRAHMOS['subject'], body=BRAHMOS['bodyPreview'],
        chain_froms=['ops@procamgroup.in', 'karansampath@brahmos.com'])


def test_the_contact_is_the_client_not_the_colleague(world):
    """A lead whose contact is your own sales manager is worse than
    no contact."""
    outsiders = mail_signals.external_parties(
        ['mvsanj@procamgroup.in', 'karansampath@brahmos.com',
         'ops@procamlogistics.com'])
    assert outsiders == ['karansampath@brahmos.com']


def test_the_vertical_comes_from_the_words(world):
    assert mail_signals.vertical_for(
        subject='Import Customs Clearance') == 'Customs'
    assert mail_signals.vertical_for(subject='DAP Shanghai FCL') \
        == 'Sea Freight'
    assert mail_signals.vertical_for(subject='ODC trailer movement') \
        == 'Heavy Transport'


def test_the_classifier_creates_a_lead_at_quoted_when_nothing_matches(world):
    ctx = li.Context(internal_domains=('procamgroup.in',))
    decision = li.classify(BRAHMOS, ctx)
    assert decision.klass == li.Klass.QUOTE_OR_REPLY
    assert decision.creates_lead is True
    assert decision.create_stage == 'Quoted'
    assert 'karansampath@brahmos.com' in decision.extra['external_parties']
    assert decision.extra['vertical'] == 'Customs'


def test_a_thread_match_attaches_without_needing_the_new_class(world):
    """Step 3 already handles a conversation-id match, and attaching
    to the right lead is the outcome that matters. The new class is
    for what step 3 cannot see."""
    ctx = li.Context(internal_domains=('procamgroup.in',),
                     find_by_thread=lambda **kw: world['lost'])
    decision = li.classify(BRAHMOS, ctx)
    assert decision.lead_id == world['lost']
    assert decision.creates_lead is False


def test_the_new_class_attaches_on_a_subject_match(world):
    """No thread to go on — a forward usually breaks the
    conversation-id — but the subject and the client's address find
    the lead the offer belongs to."""
    seen = {}

    def by_subject(subject=None, counterparties=(), **kw):
        seen['counterparties'] = list(counterparties or ())
        return world['lost']

    ctx = li.Context(internal_domains=('procamgroup.in',),
                     find_by_subject=by_subject)
    decision = li.classify(BRAHMOS, ctx)
    assert decision.lead_id == world['lost']
    assert decision.creates_lead is False


def test_the_class_is_behind_its_flag(world, monkeypatch):
    monkeypatch.setenv('FEATURE_QUOTE_REPLY_CLASS', 'false')
    flags.cache_clear()
    ctx = li.Context(internal_domains=('procamgroup.in',))
    decision = li.classify(BRAHMOS, ctx)
    assert decision.klass != li.Klass.QUOTE_OR_REPLY


def test_a_quote_arriving_from_outside_is_not_this_class(world):
    """Somebody quoting us is step 7's business, not this one."""
    inbound = dict(BRAHMOS)
    inbound['from'] = {'emailAddress': {'address': 'agent@vendor.com'}}
    ctx = li.Context(internal_domains=('procamgroup.in',))
    decision = li.classify(inbound, ctx)
    assert decision.klass != li.Klass.QUOTE_OR_REPLY
