"""
A revision keeps the first quote's number.

CRM-QUOTE-REV-02 (R3).

Today each revision takes a brand-new QUO-YYYY-NNNN. The customer
who received QUO-2026-0045 is then sent QUO-2026-0061 and has to
be told they are the same quotation, and the pair is discoverable
only by walking parent_quote_id, which nobody does in a meeting.

The tests that matter here are the two about what reaches the
customer: an internal note must never appear on a quotation, and
a quotation must never say two different things about validity.
Both are failures somebody outside the company finds first.
"""
import importlib
import os
import sys
import tempfile
from datetime import date

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'quoterev-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'QuoteRev-123456')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'quoterev.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
importlib.import_module('app.models.quote')
importlib.import_module('app.models.quote_terms')
importlib.import_module('app.models.rfq')
flask_app = _main.app
flask_app.config['WTF_CSRF_ENABLED'] = False
db = _main.db
Employee, Company = _main.Employee, _main.Company

from app.models.quote import Quote                         # noqa: E402
from app.models.quote_terms import QuoteSpecialCondition    # noqa: E402
from app.models.rfq import RFQ, RfqCondition                # noqa: E402
from app.services import quote_rev as qr                    # noqa: E402

ACTOR = 'QR_USER'


def _purge():
    QuoteSpecialCondition.query.delete()
    RfqCondition.query.delete()
    Quote.query.filter(Quote.quote_number.like('QR-%')).delete(
        synchronize_session=False)
    RFQ.query.filter(RFQ.subject.like('QR %')).delete(
        synchronize_session=False)
    Employee.query.filter_by(emp_code=ACTOR).delete(
        synchronize_session=False)
    db.session.commit()


@pytest.fixture(autouse=True)
def _clean():
    with flask_app.app_context():
        db.create_all()
        _purge()
        db.session.add(Employee(emp_code=ACTOR, name='QR User',
                                password_hash='x', role='admin',
                                email='qr@procamlogistics.com'))
        db.session.commit()
        _flag(True)
        yield
        _flag(False)
        _purge()


def _flag(state):
    import json
    from app.models.integration import AppSetting
    from app.services import flags
    row = AppSetting.query.filter_by(
        key='flag.FEATURE_QUOTE_REV_V2').first()
    if row is None:
        row = AppSetting(key='flag.FEATURE_QUOTE_REV_V2')
        db.session.add(row)
    row.value_json = json.dumps(bool(state))
    db.session.commit()
    flags.cache_clear()


_SEQ = [0]


def _quote(number=None, **kw):
    _SEQ[0] += 1
    quote = Quote(quote_number=number or f'QR-{_SEQ[0]:04d}',
                  subject='QR test', quote_date=date.today(),
                  status=kw.pop('status', 'Draft'), **kw)
    db.session.add(quote)
    db.session.commit()
    return quote


def _revise(parent, rev_no=None):
    root = qr.root_of(parent)
    child = _quote(parent_quote_id=parent.id,
                   root_quote_id=root.id if root else parent.id,
                   rev_no=rev_no if rev_no is not None
                   else qr.next_rev_no(root))
    if parent.root_quote_id is None:
        parent.root_quote_id = parent.id
        parent.rev_no = 0
        db.session.commit()
    return child


# ── the number a customer sees ───────────────────────────────────────
def test_the_first_quote_is_rev_00():
    with flask_app.app_context():
        quote = _quote('QR-0045')
        quote.root_quote_id, quote.rev_no = quote.id, 0
        db.session.commit()
        assert qr.display_number(quote) == 'QR-0045 / Rev-00'


def test_a_revision_keeps_the_first_number():
    with flask_app.app_context():
        first = _quote('QR-0045')
        first.root_quote_id, first.rev_no = first.id, 0
        db.session.commit()
        second = _revise(first)
        third = _revise(second)
        assert qr.display_number(second) == 'QR-0045 / Rev-01'
        assert qr.display_number(third) == 'QR-0045 / Rev-02'
        # Their own stored numbers are untouched and still unique.
        assert second.quote_number != first.quote_number


def test_the_chain_reads_in_order():
    with flask_app.app_context():
        first = _quote('QR-0045')
        first.root_quote_id, first.rev_no = first.id, 0
        db.session.commit()
        second = _revise(first)
        third = _revise(second)
        assert [q.id for q in qr.chain(third)] == \
            [first.id, second.id, third.id]


def test_the_root_is_found_by_walking_parents_when_unset():
    """An un-backfilled chain still reads correctly."""
    with flask_app.app_context():
        first = _quote('QR-0045')
        second = _quote('QR-0046', parent_quote_id=first.id)
        third = _quote('QR-0047', parent_quote_id=second.id)
        assert qr.root_of(third).id == first.id


def test_with_the_flag_off_the_number_is_what_it_always_was():
    with flask_app.app_context():
        _flag(False)
        quote = _quote('QR-0045')
        assert qr.display_number(quote) == 'QR-0045'


def test_sending_a_revision_supersedes_the_one_before():
    with flask_app.app_context():
        first = _quote('QR-0045', status='Sent')
        first.root_quote_id, first.rev_no = first.id, 0
        db.session.commit()
        second = _revise(first)
        qr.mark_superseded(second, actor=ACTOR)
        db.session.commit()
        assert db.session.get(Quote, first.id).status == 'Superseded'


def test_a_won_quote_is_never_superseded():
    with flask_app.app_context():
        first = _quote('QR-0045', status='Won')
        first.root_quote_id, first.rev_no = first.id, 0
        db.session.commit()
        second = _revise(first)
        qr.mark_superseded(second, actor=ACTOR)
        db.session.commit()
        assert db.session.get(Quote, first.id).status == 'Won'


# ── what reaches the customer ────────────────────────────────────────
def _rfq_with(*conditions):
    rfq = RFQ(rfq_number='QR-RFQ-1', subject='QR rfq')
    db.session.add(rfq)
    db.session.flush()
    for text, visibility, approved in conditions:
        db.session.add(RfqCondition(
            rfq_id=rfq.id, step_no=4, text=text,
            visibility=visibility, approved=approved))
    db.session.commit()
    return rfq


def test_an_internal_note_never_reaches_the_quotation():
    """The one failure a customer finds first."""
    with flask_app.app_context():
        rfq = _rfq_with(
            ('Crane not included', RfqCondition.CUSTOMER, True),
            ('Ask ops about the axle load before we commit',
             RfqCondition.INTERNAL, True))
        quote = _quote(rfq_id=rfq.id)
        added, _merged, _c = qr.consolidate(quote, actor=ACTOR,
                                            commit=True)
        assert added == ['Crane not included']
        texts = [c.text for c in QuoteSpecialCondition.query
                 .filter_by(quote_id=quote.id).all()]
        assert 'Crane not included' in texts
        assert not any('axle load' in t for t in texts)


def test_an_unapproved_condition_does_not_travel_either():
    with flask_app.app_context():
        rfq = _rfq_with(('Crane not included',
                         RfqCondition.CUSTOMER, False))
        quote = _quote(rfq_id=rfq.id)
        added, _m, _c = qr.consolidate(quote, actor=ACTOR,
                                       commit=True)
        assert added == []


def test_the_same_condition_twice_is_merged():
    with flask_app.app_context():
        rfq = _rfq_with(
            ('Crane not included', RfqCondition.CUSTOMER, True),
            ('  crane   NOT included ', RfqCondition.CUSTOMER, True))
        quote = _quote(rfq_id=rfq.id)
        added, merged, _c = qr.consolidate(quote, actor=ACTOR,
                                           commit=True)
        # One added, the duplicate counted as merged rather than
        # silently dropped — a customer must not read the same
        # condition twice, and we must be able to say why.
        assert len(added) == 1 and merged == 1
        assert QuoteSpecialCondition.query.filter_by(
            quote_id=quote.id).count() == 1


@pytest.mark.parametrize('a,b,topic', [
    ('Offer validity 30 days', 'Validity 45 days from quote date',
     'validity'),
    ('Payment terms 30 days credit', 'Payment term 60 days credit',
     'payment'),
    ('Free time 7 days at destination', 'Free time 14 days',
     'free time'),
])
def test_two_answers_to_one_question_is_a_conflict(a, b, topic):
    found = qr.conflicts([a, b])
    assert found and found[0][0] == topic


def test_two_conditions_on_different_topics_are_not_a_conflict():
    assert qr.conflicts(['Offer validity 30 days',
                         'Free time 7 days']) == []


def test_the_same_number_twice_is_not_a_conflict():
    assert qr.conflicts(['Validity 30 days',
                         'Quote valid for 30 days']) == []


def test_a_conflict_blocks_approval_with_both_halves_named():
    with flask_app.app_context():
        quote = _quote()
        for seq, text in enumerate(('Offer validity 30 days',
                                    'Validity 45 days'), start=1):
            db.session.add(QuoteSpecialCondition(
                quote_id=quote.id, seq=seq, text=text))
        db.session.commit()
        why = qr.approval_block(quote)
        assert 'Conflict to resolve before approval' in why
        assert '30 days' in why and '45 days' in why


def test_a_clean_quote_does_not_block_approval():
    with flask_app.app_context():
        quote = _quote()
        db.session.add(QuoteSpecialCondition(
            quote_id=quote.id, seq=1, text='Offer validity 30 days'))
        db.session.commit()
        assert qr.approval_block(quote) == ''


# ── the PDF (§5.5) ───────────────────────────────────────────────────
def test_the_customer_copy_names_what_it_answers():
    from app.services import quote_pdf_parts as pdf

    with flask_app.app_context():
        account = Company(name='QR Steel Ltd', is_active=True,
                          address='12 Dock Road, Kolkata')
        db.session.add(account)
        db.session.flush()
        rfq = RFQ(rfq_number='RFQ-2026-0007', subject='QR rfq')
        db.session.add(rfq)
        db.session.flush()
        quote = _quote(account_id=account.id, rfq_id=rfq.id,
                       lead_id=10421)
        assert pdf.our_ref(quote) == 'RFQ-2026-0007, Lead #10421'
        assert pdf.business_unit(quote)['name']
        db.session.delete(account)
        db.session.commit()


def test_the_internal_status_line_is_gone_from_the_customer_copy():
    """It told customers the quotation was a 'Draft'."""
    src = open(os.path.join(_ROOT, 'app', 'quote',
                            'routes.py')).read()
    block = src[src.index('story.append(Paragraph('):
                src.index('hdrs = [')]
    assert "_rev_on else" in block
    assert "<b>Status:</b>" in block        # still there, flag off
