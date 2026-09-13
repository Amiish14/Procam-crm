"""
§4 retrieval ranking — what puts one passage above another.

Each test builds two or three passages that differ in exactly one way
(the words' order, the date, the kind of text, the spelling) and checks
that difference, and only it, decides the order. The permission filter
is re-checked with every new filter, because a filter is a new way to
ask for rows and the boundary has to hold for each of them.
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'RankingTestOnly12345')
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'ranking.db'))
os.environ.pop('PROCAM_AI_EMBED_URL', None)

from app import app as flask_app, db                           # noqa: E402
from app.access import scope as scope_mod                      # noqa: E402
from app.copilot import retrieval, vocabulary                  # noqa: E402
from app.models.copilot import CopilotChunk                    # noqa: E402

BASE = 9_200_000               # lead ids no other module uses
NOW = datetime.utcnow()


def _scope(codes):
    sc = scope_mod.for_employee('RKOWN')
    sc.codes = codes
    return sc


@pytest.fixture()
def put():
    """Insert chunks for one test and always remove them."""
    made = []

    def add(n, text, *, owner='RKOWN', source='note', days_old=1,
            account='Rank Co', company_id=None):
        row = CopilotChunk(lead_id=BASE + n, owner_emp_code=owner,
                           secondary_emp_code='', source=source,
                           account_name=account, company_id=company_id,
                           occurred_at=NOW - timedelta(days=days_old),
                           text=text)
        db.session.add(row)
        db.session.commit()
        made.append(row.id)
        return row

    with flask_app.app_context():
        db.create_all()
        yield add
        CopilotChunk.query.filter(CopilotChunk.lead_id >= BASE,
                                  CopilotChunk.lead_id < BASE + 1000
                                  ).delete(synchronize_session=False)
        db.session.commit()


def _leads(found):
    return [h['lead_id'] - BASE for h in found['hits']]


# ── synonyms and abbreviations ───────────────────────────────────────
def test_an_abbreviation_finds_the_spelled_out_form(put):
    put(1, 'Please share the bill of lading copy for the Kandla job.')
    put(2, 'Rates for the Mundra movement attached.')
    found = retrieval.search(_scope({'RKOWN'}), 'BL copy')
    assert _leads(found) == [1]
    assert 'bill of lading' in found['expanded']


def test_the_spelled_out_form_finds_the_abbreviation(put):
    put(1, 'ODC consignment, 5.2 m wide, needs route survey.')
    found = retrieval.search(_scope({'RKOWN'}), 'over dimensional cargo')
    assert _leads(found) == [1]


def test_a_short_abbreviation_never_matches_inside_a_word(put):
    """"bl" is the bill of lading, not the middle of "table"."""
    put(1, 'Please see the rate table and the capable crane list.')
    found = retrieval.search(_scope({'RKOWN'}), 'bl')
    assert found['hits'] == []


@pytest.mark.parametrize('typed,written', [
    ('OOG', 'out of gauge'), ('CHA', 'customs broker'),
    ('ETA', 'estimated time of arrival'), ('HS code', 'hsn'),
    ('FCL', 'full container load'), ('LCL', 'less than container load'),
    ('RoRo', 'roll on roll off'), ('breakbulk', 'break bulk'),
    ('heavy lift', 'heavy-lift'), ('reefer', 'refrigerated container'),
    ('demurrage', 'detention'), ('EXW', 'ex works'),
    ('FOB', 'free on board'), ('CIF', 'cost insurance freight'),
    ('DAP', 'delivered at place'), ('DDP', 'delivered duty paid'),
    ('LOLO', 'lift on lift off'), ('ETD', 'estimated time of departure'),
])
def test_the_desk_vocabulary_expands(typed, written):
    assert written in vocabulary.retrieval_expansions(typed), typed


def test_a_typed_word_outranks_its_expansion(put):
    """An expansion is a hint; the word the person typed is evidence.

    Both passages have the same number of words, and the one using the
    typed spelling is inserted first so the id tie-break favours the
    other — only the expansion weight can put it on top."""
    put(2, 'Kandla shipment: BL original copy pending.')
    put(1, 'Kandla shipment: bill of lading pending.')
    found = retrieval.search(_scope({'RKOWN'}), 'BL pending')
    assert _leads(found)[0] == 2


# ── phrase, proximity, recency, source ───────────────────────────────
def test_the_words_together_beat_the_words_apart(put):
    put(2, 'We need hydraulic axle trailers for the transformer move to '
           'site before the end of next week, please confirm.')
    put(1, 'The axle count is fine. Separately, the hydraulic pump on '
           'the crane needs service before the move to site next week.')
    found = retrieval.search(_scope({'RKOWN'}), 'hydraulic axle')
    assert _leads(found)[0] == 2


def test_nearer_words_beat_distant_ones_with_no_phrase_either_way(put):
    """Neither passage has the phrase in order, and both have the same
    number of words, so only nearness separates them."""
    put(2, 'axle hydraulic checked yesterday, trailer booked, crane ready')
    put(1, 'axle checked yesterday, trailer booked, crane ready, hydraulic')
    found = retrieval.search(_scope({'RKOWN'}), 'hydraulic axle')
    assert _leads(found) == [2, 1]


def test_the_words_in_order_beat_the_same_words_reversed(put):
    """Same words, same nearness — only the order differs, so only the
    phrase bonus separates them."""
    put(2, 'hydraulic axle trailer booked for the transformer')
    put(1, 'axle hydraulic trailer booked for the transformer')
    found = retrieval.search(_scope({'RKOWN'}), 'hydraulic axle')
    assert _leads(found) == [2, 1]


def test_newer_wins_when_everything_else_is_equal(put):
    # The newer passage is inserted first, so the id tie-break would put
    # the older one on top; recency has to overturn it.
    put(2, 'Transformer movement from Mandideep to Jalandhar.', days_old=2)
    put(1, 'Transformer movement from Mandideep to Jalandhar.',
        days_old=720)
    found = retrieval.search(_scope({'RKOWN'}), 'transformer movement')
    assert _leads(found) == [2, 1]


def test_relevance_still_beats_recency(put):
    """Recency is a tie-breaker with teeth, not a filter."""
    put(1, 'Airoli transformer: 220 MT, hydraulic axles, route survey '
           'done.', days_old=500)
    put(2, 'Transformer rates for next quarter, no job attached yet.',
        days_old=1)
    found = retrieval.search(_scope({'RKOWN'}), 'Airoli transformer')
    assert _leads(found)[0] == 1


def test_the_recency_half_life_is_what_the_docs_say(put):
    row = put(1, 'x', days_old=retrieval.RECENCY_HALF_LIFE_DAYS)
    fresh = put(2, 'x', days_old=0)
    source = retrieval.SOURCE_WEIGHT['note']
    share = retrieval.RECENCY_SHARE
    assert retrieval._prior(row, now=NOW) == pytest.approx(
        source * ((1 - share) + share * 0.5), rel=0.02)
    assert retrieval._prior(fresh, now=NOW) == pytest.approx(
        source * 1.0, rel=0.01)


def test_a_note_outranks_the_same_words_in_an_outbound_email(put):
    put(2, 'Crane barge available at Kandla in October.', source='note')
    put(1, 'Crane barge available at Kandla in October.',
        source='email:outbound')
    found = retrieval.search(_scope({'RKOWN'}), 'crane barge Kandla')
    assert _leads(found) == [2, 1]


def test_a_signature_block_ranks_below_the_enquiry(put):
    """Same source; the signature is shorter, so on word frequency alone
    it would win. Only the signature weight puts the enquiry first."""
    put(2, 'Quote needed: sixty tonne generator move, Kandla port yard '
           'to Bhuj plant site, crane loading, trailer, escort, permits.',
        source='email:inbound')
    put(1, 'Regards\nOps Desk, Kandla Office\nMob: +91 98200 00000\n'
           'desk@example.com', source='email:inbound')
    found = retrieval.search(_scope({'RKOWN'}), 'Kandla')
    assert _leads(found)[0] == 2


def test_one_regards_line_does_not_make_an_enquiry_a_signature():
    assert not retrieval.looks_like_signature(
        'Please quote 40 MT to Kandla.\nThe cargo is ready on 12th.\n'
        'Loading at our yard.\nRegards')


# ── filters, each inside the boundary ────────────────────────────────
def test_the_source_filter(put):
    put(1, 'Kandla job note', source='note')
    put(2, 'Kandla job email', source='email:inbound')
    put(3, 'Kandla job enquiry', source='enquiry')
    sc = _scope({'RKOWN'})
    assert _leads(retrieval.search(sc, 'Kandla job', source='note')) == [1]
    assert _leads(retrieval.search(sc, 'Kandla job', source='email')) == [2]
    assert _leads(retrieval.search(sc, 'Kandla job',
                                   source='enquiry')) == [3]


def test_the_date_filter(put):
    put(1, 'Kandla job, the old one', days_old=400)
    put(2, 'Kandla job, the new one', days_old=3)
    sc = _scope({'RKOWN'})
    since = (NOW - timedelta(days=30)).date()
    assert _leads(retrieval.search(sc, 'Kandla job', date_from=since)) == [2]
    until = (NOW - timedelta(days=100)).date().isoformat()
    assert _leads(retrieval.search(sc, 'Kandla job', date_to=until)) == [1]


def test_the_owner_and_account_filters(put):
    put(1, 'Kandla job', owner='RKOWN', account='North Steel', company_id=1)
    put(2, 'Kandla job', owner='RKTWO', account='South Power', company_id=2)
    everyone = _scope(None)
    assert _leads(retrieval.search(everyone, 'Kandla job',
                                   owner='RKTWO')) == [2]
    assert _leads(retrieval.search(everyone, 'Kandla job',
                                   account='North')) == [1]
    assert _leads(retrieval.search(everyone, 'Kandla job',
                                   company_id=2)) == [2]
    found = retrieval.search(everyone, 'Kandla job', owner='RKTWO')
    assert found['filters'] == {'owner': 'RKTWO'}


def test_no_filter_reaches_past_the_scope(put):
    """A filter naming somebody else's records narrows to nothing — it is
    ANDed onto the boundary, never ORed around it."""
    put(1, 'Kandla job secret', owner='RKTWO', account='South Power',
        company_id=2)
    mine = _scope({'RKOWN'})
    for extra in ({}, {'owner': 'RKTWO'}, {'account': 'South'},
                  {'company_id': 2}, {'lead_id': BASE + 1},
                  {'source': 'note'}):
        assert retrieval.search(mine, 'Kandla job', **extra)['hits'] == [], \
            extra


def test_the_old_call_shape_still_works(put):
    put(1, 'zanzibar gantry')
    found = retrieval.search(_scope({'RKOWN'}), 'zanzibar gantry', limit=3,
                             lead_id=BASE + 1)
    hit = found['hits'][0]
    for key in ('lead_id', 'account', 'source', 'vertical', 'when', 'text',
                'score'):
        assert key in hit
    assert hit['citation'] == {'type': 'lead', 'id': BASE + 1,
                               'label': 'Rank Co', 'source': 'note',
                               'date': hit['when']}


# ── hybrid ───────────────────────────────────────────────────────────
def test_hybrid_mixes_meaning_and_wording(put, monkeypatch):
    a = put(1, 'transformer move, hydraulic axles')
    b = put(2, 'the big electrical unit needs a special trailer')
    c = put(3, 'transformer')

    monkeypatch.setattr(retrieval, '_backend', lambda: 'vector')
    # The embedder thinks b means the most, and has no vector for c.
    monkeypatch.setattr(retrieval, '_vector_rank',
                        lambda q, cands: [(0.95, b), (0.40, a)])
    found = retrieval.search(_scope({'RKOWN'}), 'transformer move')
    assert found['backend'] == 'hybrid'
    order = _leads(found)
    assert order[0] == 2                     # meaning led
    assert 3 in order                        # wording kept c in the list
    assert order.index(1) < order.index(3)


def test_without_an_embedder_it_is_lexical(put):
    put(1, 'transformer move')
    assert retrieval.search(_scope({'RKOWN'}),
                            'transformer')['backend'] == 'lexical'


# ── indexing dates each passage ──────────────────────────────────────
def test_each_indexed_passage_carries_its_own_date():
    from app import Lead, LeadEmail

    with flask_app.app_context():
        db.create_all()
        lead = Lead(company='Ranking Dated Co', source='manual',
                    assigned_to='RKOWN', stage='New')
        db.session.add(lead)
        db.session.commit()
        sent = datetime(2024, 3, 5, 10, 0)
        mail = LeadEmail(lead_id=lead.id, direction='inbound',
                         subject='Old enquiry', body='A 2024 enquiry body.',
                         sent_or_received_at=sent)
        db.session.add(mail)
        db.session.commit()
        try:
            retrieval.index_lead(lead)
            chunk = CopilotChunk.query.filter_by(lead_id=lead.id).first()
            assert chunk.occurred_at == sent
        finally:
            retrieval.invalidate_lead(lead.id)
            LeadEmail.query.filter_by(id=mail.id).delete()
            db.session.delete(lead)
            db.session.commit()


# ── the intent reads filters out of the question ─────────────────────
def test_search_text_reads_where_and_when_from_the_words(put):
    from app.copilot import intents as catalogue
    from app.copilot import queries        # noqa: F401 — registers

    put(1, 'crawler crane for the Kandla job', source='note', days_old=5)
    put(2, 'crawler crane for the Kandla job', source='email:inbound',
        days_old=5)
    put(3, 'crawler crane for the Kandla job', source='note', days_old=90)
    sc = _scope({'RKOWN'})
    r = catalogue.get('search_text').handler(
        sc, {'term': 'crawler crane in the notes last 30 days'})
    assert [c['id'] - BASE for c in r.citations] == [1]
    assert r.filters.get('source') == 'note'
    assert 'date_from' in r.filters
    assert 'notes' not in r.headline.split('"')[1]
