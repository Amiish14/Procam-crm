"""
§15 — the question library, and the test that keeps it honest.

Two hundred and more real questions across the personas in the brief,
each mapped to the intent that must answer it. It is a deliverable and
it is acceptance testing: `tests/test_copilot_library.py` asserts every
line here classifies to the intent named, so a phrasing that stops
working fails the build instead of quietly falling back to search.

The phrasings are deliberately uneven — some are typed properly, some
are three words, some are Hinglish, some carry the abbreviations a
logistics desk actually uses. §6.3 asks for that tolerance, and a
library of well-formed sentences would test the wrong thing.

    QUESTIONS   (persona, question, intent)
    by_persona()
    coverage()  which intents no question exercises
"""
from __future__ import annotations

SALES = 'salesperson'
HEAD = 'vertical head'
MGMT = 'management'
OPS = 'operations'
ADMIN = 'crm admin'
FIN = 'finance'
ACCT = 'account management'


#: (persona, question, expected intent)
QUESTIONS = [
    # ── My Day ───────────────────────────────────────────────────────
    (SALES, 'what needs my attention today', 'my_day'),
    (SALES, 'my day', 'my_day'),
    (SALES, 'what should I work on', 'my_day'),
    (SALES, "what's pending for me", 'my_day'),
    (SALES, 'what is on my plate', 'my_day'),
    (SALES, 'my tasks today', 'my_day'),
    (SALES, 'kya karna hai', 'my_day'),
    (SALES, 'morning brief', 'my_day'),
    (HEAD, 'what needs my attention', 'my_day'),
    (SALES, "today's work", 'my_day'),

    # ── Open and stale leads ─────────────────────────────────────────
    (SALES, 'my open leads', 'leads_open'),
    (SALES, 'show me active leads', 'leads_open'),
    (SALES, 'what leads are open', 'leads_open'),
    (HEAD, 'open leads', 'leads_open'),
    (SALES, 'stale leads', 'leads_stale'),
    (SALES, 'leads with no activity for 7 days', 'leads_stale'),
    (SALES, 'which of my leads have gone quiet', 'leads_stale'),
    (SALES, 'which of my leads have gone cold', 'leads_stale'),
    (SALES, 'untouched leads', 'leads_stale'),
    (SALES, 'leads with no movement', 'leads_stale'),
    (HEAD, 'leads idle 14 days', 'leads_stale'),
    (HEAD, 'leads sitting idle', 'leads_stale'),
    (SALES, 'leads not touched', 'leads_stale'),
    (ADMIN, 'leads with no follow up', 'leads_no_next_action'),
    (ADMIN, 'leads with no next action', 'leads_no_next_action'),
    (HEAD, 'which leads have no follow-up date', 'leads_no_next_action'),

    # ── RFQs ─────────────────────────────────────────────────────────
    (SALES, 'which RFQs have I not quoted', 'rfqs_unquoted'),
    (SALES, 'unquoted RFQs', 'rfqs_unquoted'),
    (SALES, 'RFQs pending quotation', 'rfqs_unquoted'),
    (SALES, 'pending quote', 'rfqs_unquoted'),
    (SALES, 'quotations pending with me', 'rfqs_unquoted'),
    (SALES, 'quotes pending', 'rfqs_unquoted'),
    (SALES, 'awaiting quotation', 'rfqs_unquoted'),
    (SALES, 'RFQ I have not quoted', 'rfqs_unquoted'),
    (SALES, 'yet to quote', 'rfqs_unquoted'),
    (HEAD, 'RFQs not quoted yet', 'rfqs_unquoted'),
    (SALES, 'rfqs to quote', 'rfqs_unquoted'),

    # ── Quotes ───────────────────────────────────────────────────────
    (SALES, 'quotes with no follow-up', 'quotes_awaiting_reply'),
    (SALES, 'quotes waiting for a reply', 'quotes_awaiting_reply'),
    (SALES, 'which quotes has the customer not answered',
     'quotes_awaiting_reply'),
    (SALES, 'quotes with no response', 'quotes_awaiting_reply'),
    (HEAD, 'quotes awaiting a reply', 'quotes_awaiting_reply'),
    (HEAD, 'quotes above 50 lakh', 'quotes_above'),
    (HEAD, 'quotes over 1 crore', 'quotes_above'),
    (MGMT, 'quotes more than 25 lakh', 'quotes_above'),
    (MGMT, 'quotes greater than 5000000', 'quotes_above'),
    (SALES, 'what did we last quote Tata Steel', 'last_quote_for_account'),
    (SALES, 'last price we gave JSW', 'last_quote_for_account'),
    (SALES, 'our most recent quote to BEML', 'last_quote_for_account'),
    (ACCT, 'the latest quote for Godrej', 'last_quote_for_account'),
    (SALES, 'what was the last rate we quoted Siemens',
     'last_quote_for_account'),
    (HEAD, 'how long do we take to quote', 'quote_turnaround'),
    (HEAD, 'quotation turnaround time', 'quote_turnaround'),
    (MGMT, 'RFQ to quote time', 'quote_turnaround'),
    (MGMT, 'what is our turnaround', 'quote_turnaround'),

    # ── Pipeline ─────────────────────────────────────────────────────
    (SALES, "what's my pipeline worth", 'pipeline_value'),
    (SALES, 'my pipeline', 'pipeline_value'),
    (HEAD, 'total pipeline value', 'pipeline_value'),
    (HEAD, 'weighted pipeline', 'pipeline_value'),
    (MGMT, 'pipeline value', 'pipeline_value'),
    (HEAD, 'show me the funnel value', 'pipeline_value'),
    (MGMT, 'our 20 biggest opportunities', 'top_opportunities'),
    (MGMT, 'largest open deals', 'top_opportunities'),
    (MGMT, 'top opportunities by value', 'top_opportunities'),
    (HEAD, 'biggest deals', 'top_opportunities'),
    (HEAD, 'which deals are stuck', 'stalled_deals'),
    (HEAD, 'stalled opportunities', 'stalled_deals'),
    (MGMT, 'deals not moving', 'stalled_deals'),
    (MGMT, 'opportunities sitting still 30 days', 'stalled_deals'),

    # ── Accounts ─────────────────────────────────────────────────────
    (SALES, 'who handles JSW', 'account_owner'),
    (SALES, 'who owns Tata Steel', 'account_owner'),
    (SALES, 'who is the pic for Godrej', 'account_owner'),
    (OPS, 'who manages Alstom', 'account_owner'),
    (SALES, 'who is looking after BEML', 'account_owner'),
    (SALES, 'whose account is Siemens', 'account_owner'),
    (SALES, 'is Tata Steel already handled', 'account_status'),
    (SALES, 'do we already work with Siemens', 'account_status'),
    (SALES, 'do we work with Godrej', 'account_status'),
    (SALES, 'have we ever worked with Alstom', 'account_status'),
    (SALES, 'did we work with TBEA', 'account_status'),
    (SALES, 'is BEML already a customer', 'account_status'),
    (SALES, 'is Jakson already an account', 'account_status'),
    (MGMT, 'which customers have gone quiet', 'accounts_inactive'),
    (MGMT, 'inactive accounts', 'accounts_inactive'),
    (HEAD, 'accounts we have not contacted', 'accounts_inactive'),
    (ACCT, 'dormant accounts', 'accounts_inactive'),
    (ACCT, 'customers inactive 120 days', 'accounts_inactive'),

    # ── Handovers ────────────────────────────────────────────────────
    (OPS, 'what was handed over this month', 'handovers_recent'),
    (OPS, 'recent handovers to operations', 'handovers_recent'),
    (OPS, 'handovers in the last 60 days', 'handovers_recent'),
    (MGMT, 'what has been handed over', 'handovers_recent'),

    # ── Search ───────────────────────────────────────────────────────
    (SALES, 'Tata', 'universal_search'),
    (SALES, 'find Siemens', 'universal_search'),
    (SALES, 'search for JSW', 'universal_search'),
    (SALES, 'look up Godrej', 'universal_search'),
    (OPS, 'JSW Steel', 'universal_search'),
    (ACCT, 'search Alstom', 'universal_search'),
    (SALES, 'show me BEML', 'universal_search'),

    # ── Loss and data quality ────────────────────────────────────────
    (MGMT, 'where are we losing', 'loss_analysis'),
    (MGMT, 'why do we lose', 'loss_analysis'),
    (HEAD, 'top loss reasons', 'loss_analysis'),
    (MGMT, 'where did we lose business', 'loss_analysis'),
    (ADMIN, 'lost leads with no reason', 'dq_lost_no_reason'),
    (ADMIN, 'which losses have no reason recorded', 'dq_lost_no_reason'),

    # ── Next best action ─────────────────────────────────────────────
    (SALES, 'who should I call this week', 'next_best_action'),
    (SALES, 'who should I call', 'next_best_action'),
    (SALES, 'what should I do next', 'next_best_action'),
    (SALES, 'next best action', 'next_best_action'),
    (SALES, 'who should I chase', 'next_best_action'),
    (HEAD, 'who should I contact first', 'next_best_action'),

    # ── Phase 4 · email and documents ────────────────────────────────
    (SALES, 'what did the customer ask in the latest email',
     'thread_summary'),
    (SALES, 'summarise the email thread', 'thread_summary'),
    (SALES, 'what was the last email', 'thread_summary'),
    (SALES, 'show me the email trail', 'thread_summary'),
    (SALES, 'what did the client say', 'thread_summary'),
    (SALES, 'latest email on this', 'thread_summary'),
    (SALES, 'what is in the attachment', 'attachment_contents'),
    (SALES, 'read the RFQ attachment', 'attachment_contents'),
    (OPS, 'what does the BOQ say', 'attachment_contents'),
    (OPS, 'what is in the cargo list', 'attachment_contents'),
    (SALES, 'check the enquiry sheet', 'attachment_contents'),

    # ── 360 briefings ────────────────────────────────────────────────
    (SALES, 'summarise this lead', 'lead_360'),
    (SALES, 'brief me on this lead', 'lead_360'),
    (SALES, 'lead 360', 'lead_360'),
    (HEAD, 'summarise Tata Steel account', 'account_360'),
    (HEAD, 'brief me on JSW account', 'account_360'),
    (MGMT, 'account 360 for Godrej', 'account_360'),
    (ACCT, 'summarise Alstom account', 'account_360'),

    # ── Pipeline breakdowns, performance, data quality ───────────────
    (HEAD, 'pipeline by stage', 'pipeline_by_stage'),
    (HEAD, 'pipeline by vertical', 'pipeline_by_stage'),
    (MGMT, 'pipeline by owner', 'pipeline_by_stage'),
    (MGMT, 'pipeline by city', 'pipeline_by_stage'),
    (HEAD, 'break down the pipeline', 'pipeline_by_stage'),
    (HEAD, "who hasn't updated CRM this week", 'team_activity_gap'),
    (MGMT, 'who has not logged anything', 'team_activity_gap'),
    (HEAD, 'team activity', 'team_activity_gap'),
    (HEAD, 'what is my conversion rate', 'conversion_rate'),
    (MGMT, 'quote to order conversion', 'conversion_rate'),
    (HEAD, 'win rate', 'conversion_rate'),
    (MGMT, 'what happened in sales today', 'daily_digest'),
    (MGMT, 'sales today', 'daily_digest'),
    (MGMT, 'what changed today', 'daily_digest'),
    (MGMT, 'new RFQs above 50 lakh this week', 'rfqs_high_value_recent'),
    (MGMT, 'big RFQs this week', 'rfqs_high_value_recent'),
    (HEAD, 'which accounts use Transport but never Warehousing',
     'cross_sell_gap'),
    (HEAD, 'cross sell opportunities', 'cross_sell_gap'),
    (ACCT, 'single service accounts', 'cross_sell_gap'),
    (OPS, 'won deals with no PO', 'handover_missing_po'),
    (OPS, 'handovers missing a PO', 'handover_missing_po'),
    (ADMIN, 'which leads are missing an owner', 'dq_missing_fields'),
    (ADMIN, 'data quality', 'dq_missing_fields'),
    (ADMIN, 'incomplete records', 'dq_missing_fields'),
    (ADMIN, 'are there duplicate accounts', 'dq_duplicates'),
    (ADMIN, 'duplicate customers', 'dq_duplicates'),
    (SALES, 'how am I doing', 'my_performance'),
    (SALES, 'my numbers', 'my_performance'),
    (SALES, 'my performance', 'my_performance'),
    (HEAD, 'what have I booked', 'my_performance'),
    (SALES, 'what is likely to close this month', 'closing_this_month'),
    (MGMT, 'likely bookings', 'closing_this_month'),
    (HEAD, 'closing this month', 'closing_this_month'),

    # ── §4 text retrieval ────────────────────────────────────────────
    (SALES, 'what did anyone say about the Airoli transformer',
     'search_text'),
    (SALES, 'search the emails for hydraulic axle', 'search_text'),
    (OPS, 'find mentions of demurrage', 'search_text'),
    (HEAD, 'anything about the Kandla job', 'search_text'),
    (SALES, 'what did someone say about the tender deadline',
     'search_text'),

    # ── Follow-ups ───────────────────────────────────────────────────
    (SALES, 'which follow-ups are due', 'followups_due'),
    (SALES, 'pending follow-ups', 'followups_due'),
    (SALES, 'my overdue follow-ups', 'followups_due'),
    (HEAD, 'follow ups due this week', 'followups_due'),
    (SALES, 'follow-ups due today', 'followups_due'),

    # ── Questions the catalogue does NOT answer ──────────────────────
    #
    # As important as the rest. §3.4 forbids inventing an answer, and a
    # classifier that stretches to fit is how invention starts. Each of
    # these must come back unrecognised — the honest reply, and the row
    # in the analytics that says what to build next.
    (MGMT, 'forecast next quarter revenue by region', None),
    (MGMT, 'what will we close next month', None),
    (SALES, 'write an email to the customer', None),
    (SALES, 'draft a quotation for this', None),
    (MGMT, 'what is the weather in Mumbai', None),
    (FIN, 'has the customer paid the invoice', None),
    (FIN, 'outstanding receivables', None),
    (FIN, 'what is our GST liability this quarter', None),
    (OPS, 'where is the truck right now', None),
    (OPS, 'live vehicle tracking', None),
    (MGMT, 'compare us against the competition on price', None),
    (ADMIN, 'delete all test leads', None),
    (SALES, 'change the stage of this lead to won', None),
    (SALES, 'assign this lead to Amit', None),
    (MGMT, 'how many people work in operations', None),
    (SALES, 'what is my sales target', None),
    (MGMT, 'profit margin on this job', None),
]


def by_persona():
    out = {}
    for persona, question, intent in QUESTIONS:
        out.setdefault(persona, []).append(
            {'question': question, 'intent': intent})
    return out


def coverage():
    """(covered, uncovered) intent keys.

    An intent nobody asks about in the library is an intent nobody has
    written a real question for — which usually means it does not
    answer a real question.
    """
    from app.copilot import intents as catalogue

    asked = {i for _p, _q, i in QUESTIONS if i}
    known = set(catalogue.REGISTRY)
    return sorted(asked & known), sorted(known - asked)


def stats():
    answerable = [q for q in QUESTIONS if q[2]]
    return {
        'total': len(QUESTIONS),
        'answerable': len(answerable),
        'out_of_scope': len(QUESTIONS) - len(answerable),
        'personas': len({p for p, _q, _i in QUESTIONS}),
        'intents_exercised': len({i for _p, _q, i in QUESTIONS if i}),
    }
