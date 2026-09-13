"""
The conversation layer: follow-ups, the record in context, confidence,
"how I answered", clarification, follow-up chips and citations.

Two things matter more than the rest and are tested hardest. Memory
never crosses users — a conversation id is signed for one person and
reads only that person's log rows. And the panel's record context never
widens anything — a record the viewer may not see is answered as
restricted, without so much as its name.
"""
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'ConversationTestOnly1')
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'conversation.db'))
os.environ.pop('PROCAM_AI_BASE_URL', None)

from app import (app as flask_app, db, Company, Employee, Lead,   # noqa: E402
                 Opportunity)
from app.access import scope as scope_mod                        # noqa: E402
from app.access.service import set_profile                       # noqa: E402
from app.copilot import service as svc                           # noqa: E402
from app.models.access import DataScope                          # noqa: E402

flask_app.config['WTF_CSRF_ENABLED'] = False

ALL_PERMS = ['module.rfq', 'module.quotes', 'module.handovers',
             'module.funnels', 'reports.action', 'reports.accounts',
             'reports.competitor', 'admin.master']


@pytest.fixture(scope='module')
def world():
    with flask_app.app_context():
        db.create_all()
        for code in ('CVREP', 'CVOUT', 'CVADM'):
            e = Employee.query.filter_by(emp_code=code).first()
            if e is None:
                e = Employee(emp_code=code, name=f'{code} name')
                db.session.add(e)
            e.role = 'admin' if code == 'CVADM' else 'user'
            e.vertical = 'Heavy Transport' if code != 'CVOUT' else 'Chartering'
            e.is_active, e.must_change_pw, e.is_super_admin = True, False, False
        db.session.commit()
        mine = Company(name='Convo Mine Co', is_active=True,
                       pic_emp_code='CVREP', vertical='Heavy Transport')
        theirs = Company(name='Convo Theirs Co', is_active=True,
                         pic_emp_code='CVOUT', vertical='Chartering')
        db.session.add_all([mine, theirs])
        db.session.flush()
        lead_mine = Lead(company='Convo Mine Co', source='manual',
                         company_id=mine.id, assigned_to='CVREP',
                         stage='Quoted', estimated_value_inr=4_000_000,
                         procam_vertical='Project Logistics',
                         created_at=datetime.utcnow() - timedelta(days=20),
                         pic='Convo Buyer Mine')
        lead_theirs = Lead(company='Convo Theirs Co', source='manual',
                           company_id=theirs.id, assigned_to='CVOUT',
                           stage='Quoted', estimated_value_inr=66_000_000)
        db.session.add_all([lead_mine, lead_theirs])
        db.session.flush()
        opp_mine = Opportunity(opp_number='OPP-CV-MINE', owner_emp_code='CVREP',
                               company_id=mine.id, lead_id=lead_mine.id,
                               stage='Quoted', value_inr=4_000_000,
                               probability=50)
        opp_theirs = Opportunity(opp_number='OPP-CV-THEIRS',
                                 owner_emp_code='CVOUT', company_id=theirs.id,
                                 stage='Quoted', value_inr=66_000_000)
        db.session.add_all([opp_mine, opp_theirs])
        db.session.flush()
        from app.models.quote import Quote
        quote = Quote(quote_number='Q-CV-1', subject='mine',
                      lead_id=lead_mine.id, account_id=mine.id,
                      prepared_by_id='CVREP', status='Submitted',
                      total_amount=900_000)
        db.session.add(quote)
        db.session.commit()
        ids = {'mine': mine.id, 'theirs': theirs.id,
               'lead_mine': lead_mine.id, 'lead_theirs': lead_theirs.id,
               'opp_mine': opp_mine.id, 'opp_theirs': opp_theirs.id,
               'quote': quote.id}
    yield ids
    with flask_app.app_context():
        from app.models.copilot import CopilotLog
        from app.models.quote import Quote
        Quote.query.filter_by(quote_number='Q-CV-1').delete()
        Opportunity.query.filter(Opportunity.opp_number.like('OPP-CV-%')
                                 ).delete(synchronize_session=False)
        Lead.query.filter(Lead.id.in_([ids['lead_mine'],
                                       ids['lead_theirs']])).delete(
            synchronize_session=False)
        Company.query.filter(Company.id.in_([ids['mine'], ids['theirs']])
                             ).delete(synchronize_session=False)
        CopilotLog.query.filter(CopilotLog.emp_code.in_(
            ['CVREP', 'CVOUT', 'CVADM'])).delete(synchronize_session=False)
        db.session.commit()


def _sc(code, data_scope=DataScope.OWN, perms=ALL_PERMS):
    set_profile(code, data_scope, list(perms), actor='CVADM')
    return scope_mod.for_employee(code)


def _text(answer):
    d = answer.to_dict()
    parts = [str(d.get(k) or '') for k in ('prose', 'headline',
                                           'how_answered')]
    for r in d.get('rows') or []:
        parts.extend(str(v) for v in r.values())
    parts.append(str(d.get('context')))
    parts.append(str(d.get('clarification')))
    return ' | '.join(parts)


# ══════════════════════════════════════════════════════════════════
#  the new fields, and the old ones untouched
# ══════════════════════════════════════════════════════════════════
def test_an_answer_keeps_its_old_fields_and_adds_the_new(world):
    with flask_app.app_context():
        d = svc.ask('my open leads', sc=_sc('CVREP')).to_dict()
        for old in ('intent', 'prose', 'clarify', 'ms', 'model_used',
                    'scope_note', 'log_id', 'headline', 'columns', 'rows',
                    'figures', 'sources', 'empty', 'restricted', 'notes',
                    'recommendation'):
            assert old in d, old
        for new in ('confidence', 'how_answered', 'follow_ups', 'citations',
                    'clarification', 'conversation_id', 'context',
                    'resolution'):
            assert new in d, new


def test_confidence_follows_how_the_intent_was_reached(world, monkeypatch):
    from app.copilot import model as model_mod

    with flask_app.app_context():
        sc = _sc('CVREP')
        assert svc.ask('my open leads', sc=sc).confidence == 'high'
        assert svc.ask('quotations pending with me',
                       sc=sc).confidence == 'medium'

        monkeypatch.setattr(model_mod, 'available', lambda: True)
        monkeypatch.setattr(model_mod, 'classify',
                            lambda *a, **k: {'intent': 'leads_open'})
        monkeypatch.setattr(model_mod, 'narrate', lambda *a, **k: None)
        routed = svc.ask('which ones deserve my morning', sc=sc)
        assert routed.intent_key == 'leads_open'
        assert routed.confidence == 'low'
        assert routed.model_used is True


def test_how_answered_names_the_intent_and_filters_but_never_a_query(world):
    with flask_app.app_context():
        sc = _sc('CVREP')
        a = svc.ask('stale leads 14 days in Project Freight', sc=sc)
        how = a.how_answered
        assert how.startswith('Answered with “Stale leads”')
        assert 'last 14 day(s)' in how
        assert 'service: Project Freight' in how
        assert 'your own records' in how
        for forbidden in ('select ', ' from ', ' where ', 'join ', '.py',
                          'http', 'sqlite', 'emp_code in'):
            assert forbidden not in how.lower(), forbidden


def test_row_facts_become_citations(world):
    with flask_app.app_context():
        d = svc.ask('my open leads', sc=_sc('CVREP')).to_dict()
        assert {'type': 'lead', 'id': world['lead_mine'],
                'label': 'Convo Mine Co'} in d['citations']


def test_text_search_hits_cite_lead_source_and_date(world):
    from app import LeadEmail
    from app.copilot import retrieval

    with flask_app.app_context():
        mail = LeadEmail(lead_id=world['lead_mine'], direction='inbound',
                         subject='Crawler crane', body='crawler crane for '
                         'the transformer move at the plant',
                         sent_or_received_at=datetime(2026, 8, 1, 9, 0))
        db.session.add(mail)
        db.session.commit()
        retrieval.index_lead(db.session.get(Lead, world['lead_mine']))
        try:
            d = svc.ask('anything about the crawler crane',
                        sc=_sc('CVREP')).to_dict()
            assert d['intent'] == 'search_text'
            cite = d['citations'][0]
            assert cite['type'] == 'lead' and cite['id'] == world['lead_mine']
            assert cite['source'] == 'email'
            assert cite['date'] == '2026-08-01'
        finally:
            retrieval.invalidate_lead(world['lead_mine'])
            LeadEmail.query.filter_by(id=mail.id).delete()
            db.session.commit()


def test_follow_up_chips_are_offered_and_entitlement_aware(world):
    with flask_app.app_context():
        full = svc.ask('pipeline value', sc=_sc('CVREP'))
        assert 'pipeline health' in full.follow_ups
        assert 2 <= len(full.follow_ups) <= 3
        for q in full.follow_ups:
            assert svc._match_patterns(q)[0], q

        # After "recent wins" the chips are the handover queue and win
        # rate by vertical — both gated. Without those permissions there
        # is nothing to offer, rather than chips that would refuse.
        entitled = svc.ask('recent wins', sc=_sc('CVREP'))
        assert 'won deals awaiting PO' in entitled.follow_ups
        bare = svc.ask('recent wins', sc=_sc('CVREP', perms=[]))
        assert bare.intent_key == 'deals_won_recent'
        assert bare.follow_ups == []


def test_memory_reads_only_the_askers_rows(world):
    """Defence in depth under the signed id: even handed a valid start
    id, the reader filters on the asker."""
    with flask_app.app_context():
        start = svc.ask('new leads this week', sc=_sc('CVREP')).log_id
        svc.ask('pipeline value', sc=_sc('CVOUT'))
        turns = svc._memory('CVREP', start)
        assert turns and all(t['question'] != 'pipeline value'
                             for t in turns)
        assert svc._memory('CVOUT', start) == [
            {'question': 'pipeline value', 'intent': 'pipeline_value'}]


# ══════════════════════════════════════════════════════════════════
#  clarification
# ══════════════════════════════════════════════════════════════════
def test_a_one_word_question_is_answered_with_options(world):
    with flask_app.app_context():
        a = svc.ask('quotes', sc=_sc('CVREP'))
        assert a.intent_key is None
        c = a.to_dict()['clarification']
        assert 2 <= len(c['options']) <= 4
        assert all(svc._match_patterns(o['question'])[0]
                   for o in c['options'])


def test_options_the_viewer_cannot_use_are_not_offered(world):
    with flask_app.app_context():
        a = svc.ask('quotes', sc=_sc('CVREP', perms=[]))
        assert a.to_dict()['clarification'] is None


def test_several_matching_accounts_are_a_clarification_through_ask(world):
    with flask_app.app_context():
        extra = Company(name='Convo Mine Co Two', is_active=True,
                        pic_emp_code='CVREP')
        db.session.add(extra)
        db.session.commit()
        try:
            a = svc.ask('account health for Convo Mine', sc=_sc('CVREP'))
            c = a.to_dict()['clarification']
            assert c and [o['label'] for o in c['options']] == \
                ['Convo Mine Co', 'Convo Mine Co Two']
            assert a.confidence is None
        finally:
            db.session.delete(extra)
            db.session.commit()


# ══════════════════════════════════════════════════════════════════
#  memory
# ══════════════════════════════════════════════════════════════════
def test_a_follow_up_reuses_the_previous_intent_with_a_new_window(world):
    with flask_app.app_context():
        sc = _sc('CVREP')
        first = svc.ask('new leads this week', sc=sc)
        assert first.conversation_id
        second = svc.ask('what about last month', sc=sc,
                         conversation_id=first.conversation_id)
        assert second.intent_key == 'leads_new'
        assert second.result.figures['days'] == 30
        assert second.confidence == 'medium'
        assert second.resolution == 'memory'
        assert 'a follow-up to your previous question' in second.how_answered
        assert second.conversation_id == first.conversation_id


def test_follow_ups_chain_and_narrow_by_service(world):
    with flask_app.app_context():
        sc = _sc('CVREP')
        cid = svc.ask('my open leads', sc=sc).conversation_id
        narrowed = svc.ask('only Project Freight', sc=sc,
                           conversation_id=cid)
        assert narrowed.intent_key == 'leads_open'
        assert narrowed.result.filters == {'vertical': 'Project Freight'}
        assert 'Convo Mine Co' in _text(narrowed)
        other = svc.ask('just Chartering', sc=sc, conversation_id=cid)
        assert other.result.filters == {'vertical': 'Chartering'}
        assert 'Convo Mine Co' not in _text(other)


def test_and_for_an_account_switches_to_its_account_variant(world):
    with flask_app.app_context():
        sc = _sc('CVREP')
        cid = svc.ask('pipeline value', sc=sc).conversation_id
        a = svc.ask('and for Convo Mine Co?', sc=sc, conversation_id=cid)
        assert a.intent_key == 'account_pipeline'
        assert 'OPP-CV-MINE' in _text(a)


def test_an_account_name_keeps_its_own_small_words(world):
    """"and", "of", "the" inside a company's name are part of the name."""
    state = ('pipeline_value', {})
    plan = svc._follow_up('what about Sons and Partners of the Coast',
                          state, None)
    assert plan.key == 'account_pipeline'
    assert plan.params == {'account': 'Sons and Partners of the Coast'}


def test_a_new_question_with_a_time_word_is_not_a_follow_up(world):
    """Only questions made of modifiers continue the conversation —
    "how is everyone today" has a window in it and is not "the last
    answer, for one day"."""
    with flask_app.app_context():
        sc = _sc('CVREP')
        cid = svc.ask('new leads this week', sc=sc).conversation_id
        for question in ('how is everyone today',
                         'is the office open this month'):
            a = svc.ask(question, sc=sc, conversation_id=cid)
            assert a.resolution != 'memory', question
        bare = svc.ask('Project Freight only', sc=sc, conversation_id=cid)
        assert bare.resolution == 'memory'
        assert bare.result.filters == {'vertical': 'Project Freight'}


def test_a_modifier_the_intent_cannot_take_is_said_not_dropped(world):
    with flask_app.app_context():
        sc = _sc('CVREP')
        cid = svc.ask('my open leads', sc=sc).conversation_id
        a = svc.ask('what about last month', sc=sc, conversation_id=cid)
        assert a.intent_key == 'leads_open'
        assert any('was not applied' in n for n in a.result.notes)


def test_memory_never_crosses_users(world):
    with flask_app.app_context():
        rep = _sc('CVREP')
        cid = svc.ask('new leads this week', sc=rep).conversation_id
        other = svc.ask('what about last month', sc=_sc('CVOUT'),
                        conversation_id=cid)
        assert other.intent_key is None
        assert other.resolution is None
        # and the id they were handed is their own, not the rep's
        assert other.conversation_id != cid


def test_a_tampered_conversation_id_is_ignored(world):
    with flask_app.app_context():
        rep = _sc('CVREP')
        cid = svc.ask('new leads this week', sc=rep).conversation_id
        start, sig = cid[1:].split('.')
        for forged in (f'c{int(start) - 1}.{sig}', f'c{start}.{"0" * 24}',
                       'c1.abc', '', None, '../../etc'):
            a = svc.ask('what about last month', sc=rep,
                        conversation_id=forged)
            assert a.intent_key is None, forged


def test_memory_expires(world):
    from app.models.copilot import CopilotLog

    with flask_app.app_context():
        rep = _sc('CVREP')
        first = svc.ask('new leads this week', sc=rep)
        row = db.session.get(CopilotLog, first.log_id)
        row.created_at = datetime.utcnow() - timedelta(
            minutes=svc.MEMORY_MINUTES + 1)
        db.session.commit()
        a = svc.ask('what about last month', sc=rep,
                    conversation_id=first.conversation_id)
        assert a.intent_key is None


def test_no_conversation_id_means_no_memory(world):
    with flask_app.app_context():
        rep = _sc('CVREP')
        svc.ask('new leads this week', sc=rep)
        assert svc.ask('what about last month', sc=rep).intent_key is None


def test_a_revoked_permission_bites_on_a_follow_up(world):
    with flask_app.app_context():
        cid = svc.ask('pipeline value', sc=_sc('CVREP')).conversation_id
        revoked = _sc('CVREP', perms=[])
        a = svc.ask('only Project Freight', sc=revoked, conversation_id=cid)
        assert a.result.restricted is True
        assert 'access' in a.prose.lower()


# ══════════════════════════════════════════════════════════════════
#  record context
# ══════════════════════════════════════════════════════════════════
def test_summarise_this_uses_the_lead_the_panel_is_open_on(world):
    with flask_app.app_context():
        a = svc.ask('summarise this', sc=_sc('CVREP'),
                    context={'type': 'lead', 'id': world['lead_mine']})
        assert a.intent_key == 'lead_360'
        assert a.result.figures['lead_id'] == world['lead_mine']
        assert a.confidence == 'high'
        assert a.to_dict()['context']['label'] == 'Convo Mine Co'


def test_whats_next_and_who_is_the_contact_here(world):
    with flask_app.app_context():
        sc = _sc('CVREP')
        ctx = {'type': 'lead', 'id': str(world['lead_mine'])}
        nxt = svc.ask("what's next here", sc=sc, context=ctx)
        assert nxt.intent_key == 'next_best_action'
        assert nxt.result.figures.get('lead_id') == world['lead_mine']
        who = svc.ask('who is the contact here', sc=sc, context=ctx)
        assert who.intent_key == 'key_contacts'
        assert 'Convo Buyer Mine' in _text(who)


def test_account_and_opportunity_context(world):
    with flask_app.app_context():
        sc = _sc('CVREP')
        acct = svc.ask('summarise this account', sc=sc,
                       context={'type': 'company', 'id': world['mine']})
        assert acct.intent_key == 'account_360'
        assert 'Convo Mine Co' in acct.result.headline
        narrow = svc.ask('summarise this', sc=_sc('CVREP', perms=[]),
                         context={'type': 'account', 'id': world['mine']})
        assert narrow.intent_key == 'account_health'
        opp = svc.ask('what is the risk here', sc=sc,
                      context={'type': 'opportunity',
                               'id': world['opp_mine']})
        assert opp.intent_key == 'opportunity_risk'
        assert 'OPP-CV-MINE' in opp.result.headline


def test_a_quote_context_becomes_its_lead(world):
    with flask_app.app_context():
        a = svc.ask('summarise this', sc=_sc('CVREP'),
                    context={'type': 'quote', 'id': world['quote']})
        assert a.intent_key == 'lead_360'
        assert a.result.figures['lead_id'] == world['lead_mine']


@pytest.mark.parametrize('kind,key', [('lead', 'lead_theirs'),
                                      ('company', 'theirs'),
                                      ('opportunity', 'opp_theirs')])
def test_a_context_the_viewer_cannot_see_is_restricted_and_unnamed(
        world, kind, key):
    with flask_app.app_context():
        for question in ('summarise this', "what's next here",
                         'who is the contact here'):
            a = svc.ask(question, sc=_sc('CVREP'),
                        context={'type': kind, 'id': world[key]})
            text = _text(a)
            assert a.result.restricted is True, (kind, question)
            assert 'Convo Theirs Co' not in text, (kind, question)
            assert 'OPP-CV-THEIRS' not in text
            assert '6.60 cr' not in text
            assert a.confidence is None


def test_the_owner_of_that_context_is_answered(world):
    with flask_app.app_context():
        a = svc.ask('summarise this', sc=_sc('CVOUT'),
                    context={'type': 'lead', 'id': world['lead_theirs']})
        assert a.intent_key == 'lead_360'
        assert a.result.restricted is False


def test_a_general_question_ignores_an_unseen_context(world):
    """Context narrows questions about "this". It does not turn every
    other question into a refusal."""
    with flask_app.app_context():
        a = svc.ask('my open leads', sc=_sc('CVREP'),
                    context={'type': 'lead', 'id': world['lead_theirs']})
        assert a.intent_key == 'leads_open'
        assert a.result.restricted is False
        assert 'Convo Theirs Co' not in _text(a)


def test_a_named_account_beats_the_page_it_was_asked_on(world):
    with flask_app.app_context():
        a = svc.ask('who handles Convo Mine Co', sc=_sc('CVREP'),
                    context={'type': 'company', 'id': world['theirs']})
        assert a.intent_key == 'account_owner'
        assert a.result.headline.startswith('Convo Mine Co')


def test_garbage_context_is_ignored(world):
    with flask_app.app_context():
        for ctx in ({'type': 'lead', 'id': 'abc'}, {'type': 'x', 'id': 1},
                    {'id': 5}, {'type': 'lead', 'id': -3}, 'lead:5', None):
            a = svc.ask('my open leads', sc=_sc('CVREP'), context=ctx)
            assert a.intent_key == 'leads_open'


# ══════════════════════════════════════════════════════════════════
#  over HTTP
# ══════════════════════════════════════════════════════════════════
def _client(code):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code=code, name=code, role='user', vertical='All')
    return c


def test_the_api_hands_back_and_accepts_a_conversation_id(world):
    with flask_app.app_context():
        _sc('CVREP')
    c = _client('CVREP')
    first = c.post('/api/copilot/ask',
                   json={'q': 'new leads this week'}).get_json()
    assert re.fullmatch(r'c\d+\.[0-9a-f]{24}', first['conversation_id'])
    second = c.post('/api/copilot/ask',
                    json={'q': 'what about last month',
                          'conversation_id': first['conversation_id']}
                    ).get_json()
    assert second['intent'] == 'leads_new'
    assert second['confidence'] == 'medium'

    other = _client('CVOUT').post(
        '/api/copilot/ask', json={'q': 'what about last month',
                                  'conversation_id':
                                      first['conversation_id']}).get_json()
    assert other['intent'] is None


def test_the_stream_carries_the_conversation_and_the_new_fields(world):
    import json

    with flask_app.app_context():
        _sc('CVREP')
    c = _client('CVREP')
    first = c.post('/api/copilot/ask',
                   json={'q': 'new leads this week'}).get_json()
    r = c.post('/api/copilot/ask/stream',
               json={'q': 'what about last month',
                     'conversation_id': first['conversation_id']})
    stages = [json.loads(b.strip()[5:]) for b in
              r.get_data(as_text=True).split('\n\n')
              if b.strip().startswith('data:')]
    done = stages[-1]
    assert done['stage'] == 'done'
    assert done['intent'] == 'leads_new'
    assert done['conversation_id'] == first['conversation_id']
    for field in ('confidence', 'how_answered', 'follow_ups', 'citations'):
        assert field in done


def test_the_api_context_is_scoped_too(world):
    with flask_app.app_context():
        _sc('CVREP')
    body = _client('CVREP').post(
        '/api/copilot/ask',
        json={'q': 'summarise this',
              'context': {'type': 'lead', 'id': world['lead_theirs']}}
    ).get_json()
    assert body['restricted'] is True
    assert 'Convo Theirs Co' not in str(body)
