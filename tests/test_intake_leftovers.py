"""Vertical determination, quote capture, learning and retention.

§6, §11, §16, §17, §23 — the pieces that were designed and not built.
"""
import importlib.util
import os
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)


def _pure(name, path):
    spec = importlib.util.spec_from_file_location(name,
                                                  os.path.join(_ROOT, path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lv = _pure('lv_test', 'app/services/lead_vertical.py')
qd = _pure('qd_test', 'app/services/quote_details.py')


# ─── §17 vertical determination ──────────────────────────────────────────
@pytest.mark.parametrize('text,expected', [
    ('RFQ for transportation of 165MVA Transformer by hydraulic axle',
     'Project Logistics'),
    ('ODC movement, breakbulk cargo, SPMT required', 'Project Logistics'),
    ('FCL container ocean freight ex Nhava Sheva, bill of lading',
     'Sea Freight'),
    ('Air freight AWB from Shanghai, chargeable weight 400kg', 'Air Freight'),
    ('Customs clearance and bill of entry for import', 'Customs'),
    ('Bonded warehouse storage, pallet racking, 5000 sqft', 'Warehousing'),
    ('FTL trailer rate Mundra to Chennai, road transport', 'Transportation'),
])
def test_the_vertical_is_recognised(text, expected):
    vertical, confidence, _why = lv.recommend(text)
    assert vertical == expected
    assert confidence >= 50


def test_nothing_is_recommended_from_nothing():
    vertical, confidence, _ = lv.recommend('Hello, following up on our chat')
    assert vertical is None
    assert confidence == 0


def test_the_account_master_wins_a_close_call():
    """Who handles an account is a relationship, not a keyword match."""
    vertical, _c, why = lv.recommend(
        'Please arrange the vehicle', account_vertical='Warehousing')
    assert vertical == 'Warehousing'
    assert 'Account Master' in why


def test_a_decisive_signal_overrides_the_account_master():
    """A warehousing client can still send a project-cargo enquiry."""
    vertical, _c, why = lv.recommend(
        'RFQ: ODC hydraulic axle movement of a 220MT reactor, breakbulk',
        account_vertical='Warehousing')
    assert vertical == 'Project Logistics'
    assert 'overriding' in why


def test_the_recommendation_explains_itself():
    _v, _c, why = lv.recommend('FCL ocean freight container')
    assert 'matched' in why


def test_confidence_reflects_the_margin_not_the_score():
    """Two verticals scoring equally is a coin toss however strong."""
    _v, clear, _ = lv.recommend('hydraulic axle ODC breakbulk SPMT')
    _v2, mixed, _ = lv.recommend('container warehouse storage vessel pallet')
    assert clear > mixed


# ─── §6 quote details ────────────────────────────────────────────────────
def test_a_quote_reference_and_amount_are_read():
    got = qd.extract('Please find attached our quotation PCM/QT/4471 '
                     'dated 09/09/2026. Total INR 12,45,000 all inclusive.')
    assert got['reference'] == 'PCM/QT/4471'
    assert got['amount'] == 1245000.0
    assert got['currency'] == 'INR'
    assert str(got['quoted_on']) == '2026-09-09'


def test_indian_scales_are_understood():
    assert qd.extract('Rate Rs 4.5 lakhs per trailer')['amount'] == 450000.0
    assert qd.extract('Approx INR 1.2 crore total')['amount'] == 12000000.0


def test_foreign_currency_is_kept_as_itself():
    got = qd.extract('Our offer ref QTN-2026-0087. Amount USD 18,500 CIF.')
    assert got['currency'] == 'USD'
    assert got['amount'] == 18500.0


def test_a_reference_hiding_in_the_filename_is_found():
    got = qd.extract('Please find our quote.',
                     attachments=['Procam_Quotation_5512.pdf'])
    assert got['reference'] == 'QUOTE-5512'


def test_a_bare_number_is_not_mistaken_for_a_price():
    """A logistics email is full of numbers that are not money."""
    got = qd.extract('Quote as below. 40 containers, 220 MT, PIN 400093.')
    assert got['amount'] is None


def test_nothing_is_invented_when_there_is_nothing_to_read():
    got = qd.extract('Please find our quote for the movement.')
    assert got['reference'] is None and got['amount'] is None


# ─── the rest needs the app ──────────────────────────────────────────────
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'LeftoverTest12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'leftovers.db')

import importlib                                              # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Employee, EmailClassification = _main.Employee, _main.EmailClassification
VendorDomain = _main.VendorDomain
flask_app.config['WTF_CSRF_ENABLED'] = False

from app.intake import learning                               # noqa: E402
from app.services import lead_intake as li                    # noqa: E402
from app.services import lead_assignment                      # noqa: E402


@pytest.fixture(scope='module')
def world():
    with flask_app.app_context():
        db.create_all()
        e = Employee.query.filter_by(emp_code='LFTADM').first()
        if not e:
            e = Employee(emp_code='LFTADM', name='Leftover Admin')
            db.session.add(e)
        e.role, e.is_active, e.must_change_pw = 'admin', True, False
        e.is_super_admin, e.vertical = True, 'All'
        db.session.commit()
    return True


def _rejected(domain, reason, n=3, subject='Rates'):
    with flask_app.app_context():
        for i in range(n):
            db.session.add(EmailClassification(
                message_id=f'<{domain}{reason}{i}@x>',
                subject=f'{subject} {i}',
                from_addr=f'sales{i}@{domain}', from_domain=domain,
                classification=li.Klass.NEW_LEAD, decided_by='step_10',
                corrected_to=li.Klass.RATE_SOURCING,
                correction_reason=reason, corrected_by='LFTADM',
                corrected_at=datetime.utcnow(), review_state='rejected'))
        db.session.commit()


# ─── §11 learning ────────────────────────────────────────────────────────
def test_a_domain_rejected_repeatedly_is_proposed_as_a_supplier(world):
    _rejected('learnedline.com', 'Shipping Line Rate Sourcing', n=3)
    with flask_app.app_context():
        props = learning.proposals()
    vendor = [p for p in props if p['key'] == 'vendor:learnedline.com']
    assert vendor, 'three rejections should propose a rule'
    assert vendor[0]['evidence'] == 3
    assert 'supplier' in vendor[0]['title']


def test_two_rejections_are_a_coincidence_not_a_pattern(world):
    _rejected('twiceonly.com', 'Vendor Rate Sourcing', n=2)
    with flask_app.app_context():
        props = learning.proposals()
    assert not [p for p in props if p['key'] == 'vendor:twiceonly.com']


def test_nothing_applies_itself(world):
    """An auto-learned rule that quietly drops a customer's mail is worse
    than the noise it removes.

    proposals() is called first: reading the proposals is the moment an
    auto-apply would fire, so a test that never reads them cannot catch
    one.
    """
    _rejected('notyet.com', 'Vendor Rate Sourcing', n=4)
    with flask_app.app_context():
        props = learning.proposals()
        assert any(p['key'] == 'vendor:notyet.com' for p in props), \
            'the fixture should produce a proposal to begin with'
        assert VendorDomain.query.filter_by(domain='notyet.com').first() \
            is None, 'reading a proposal must not apply it'


def test_applying_a_proposal_creates_the_rule(world):
    _rejected('applyme.com', 'Transporter Rate Sourcing', n=3)
    with flask_app.app_context():
        ok, msg = learning.apply_proposal('vendor:applyme.com', actor='LFTADM')
        assert ok, msg
        db.session.commit()
        row = VendorDomain.query.filter_by(domain='applyme.com').first()
        assert row is not None
        assert row.learned_from == 'learned_from_rejections'


def test_a_dismissed_proposal_stops_being_suggested(world):
    _rejected('dismissme.com', 'Vendor Rate Sourcing', n=3)
    with flask_app.app_context():
        assert any(p['key'] == 'vendor:dismissme.com'
                   for p in learning.proposals())
        learning.dismiss_proposal('vendor:dismissme.com', actor='LFTADM')
        assert not any(p['key'] == 'vendor:dismissme.com'
                       for p in learning.proposals())


def test_a_phrase_rule_is_proposed_but_not_applied(world):
    """A rule that edits its own source is not one anyone can review."""
    with flask_app.app_context():
        ok, msg = learning.apply_proposal('phrase:quote:some new wording')
        assert not ok
        assert 'code review' in msg


def test_an_overruled_step_is_reported_as_an_observation(world):
    with flask_app.app_context():
        for i in range(3):
            db.session.add(EmailClassification(
                message_id=f'<obs{i}@x>', classification=li.Klass.NEW_LEAD,
                decided_by='step_10', corrected_to=li.Klass.INTERNAL,
                corrected_at=datetime.utcnow(), corrected_by='LFTADM'))
        db.session.commit()
        props = learning.proposals()
    obs = [p for p in props if p['kind'] == 'observation']
    assert obs
    assert 'No rule is proposed' in obs[0]['detail']


# ─── §16 reassignment reasons ────────────────────────────────────────────
def test_the_reassignment_reasons_are_a_fixed_list():
    assert 'Incorrect automatic assignment' in \
        lead_assignment.REASSIGNMENT_REASONS
    assert 'Other' in lead_assignment.REASSIGNMENT_REASONS


def test_the_reason_reaches_the_assignment_history(world):
    from app import Lead, LeadAssignmentHistory
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code='LFTADM', name='Leftover Admin', role='admin',
                 vertical='All')
    with flask_app.app_context():
        lead = Lead(company='Reason Ltd', source='manual')
        db.session.add(lead)
        db.session.commit()
        lid = lead.id

    c.put(f'/api/leads/{lid}',
          json={'assigned_to': 'LFTADM',
                'reassignment_reason': 'Incorrect automatic assignment'})

    with flask_app.app_context():
        row = (LeadAssignmentHistory.query.filter_by(lead_id=lid)
               .order_by(LeadAssignmentHistory.id.desc()).first())
        assert row is not None
        assert row.note == 'Incorrect automatic assignment'


# ─── §23 retention ───────────────────────────────────────────────────────
def test_retention_never_deletes_a_decision():
    src = open(os.path.join(_ROOT, 'scripts', 'intake_retention.py')).read()
    assert 'db.session.delete' not in src
    assert '.delete()' not in src
    assert "payload['body'] = ''" in src, \
        'it should trim the body and keep the row'


def test_retention_keeps_anything_that_produced_a_lead_or_was_corrected():
    src = open(os.path.join(_ROOT, 'scripts', 'intake_retention.py')).read()
    assert 'created_lead_id.is_(None)' in src
    assert 'corrected_at.is_(None)' in src
