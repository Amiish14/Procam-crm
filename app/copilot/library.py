"""
§15 — the question library, and the test that keeps it honest.

Three hundred and more real questions across the personas in the
brief, each mapped to the intent that must answer it. It is a deliverable and
it is acceptance testing: `tests/test_copilot_library.py` asserts every
line here classifies to the intent named, so a phrasing that stops
working fails the build instead of quietly falling back to search.

The phrasings are deliberately uneven — some are typed properly, some
are three words, some are Hinglish, some carry the abbreviations a
logistics desk actually uses. §6.3 asks for that tolerance, and a
library of well-formed sentences would test the wrong thing.

    QUESTIONS    (persona, question, intent) — every one routes on the
                 rules alone, no model
    MODEL_ONLY   phrasings the rules deliberately do not route; a
                 configured model may. Marked apart so the deterministic
                 guarantee above stays exact at this size
    by_persona()
    coverage()   which intents no question exercises
    duplicates() phrasings listed twice, however they are punctuated
"""
from __future__ import annotations

SALES = 'salesperson'
HEAD = 'vertical head'
MGMT = 'management'
OPS = 'operations'
ADMIN = 'crm admin'
FIN = 'finance'
ACCT = 'account management'
PROJ = 'projects'


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

    # ── My tasks ─────────────────────────────────────────────────────
    (SALES, 'my open tasks', 'my_tasks'),
    (SALES, 'overdue tasks', 'my_tasks'),
    (OPS, 'my work queue', 'my_tasks'),
    (SALES, 'tasks assigned to me', 'my_tasks'),
    (ADMIN, 'pending tasks', 'my_tasks'),
    (PROJ, 'what is on my to-do list', 'my_tasks'),
    (SALES, 'which follow ups are overdue', 'followups_due'),
    (HEAD, 'overdue follow-ups for my team', 'followups_due'),

    # ── Leads: new, by stage, sources, ownership ─────────────────────
    (SALES, 'new leads this week', 'leads_new'),
    (HEAD, 'how many leads came in today', 'leads_new'),
    (MGMT, 'new leads this month', 'leads_new'),
    (SALES, 'fresh leads', 'leads_new'),
    (HEAD, 'leads created in the last 30 days', 'leads_new'),
    (MGMT, 'new Project Freight leads this week', 'leads_new'),
    (HEAD, 'leads by stage', 'leads_by_stage'),
    (MGMT, 'lead funnel', 'leads_by_stage'),
    (SALES, 'how many leads are at each stage', 'leads_by_stage'),
    (HEAD, 'stage wise leads', 'leads_by_stage'),
    (MGMT, 'lead sources', 'lead_sources'),
    (MGMT, 'where do our leads come from', 'lead_sources'),
    (HEAD, 'leads by source this quarter', 'lead_sources'),
    (ADMIN, 'unassigned leads', 'leads_unassigned'),
    (ADMIN, 'leads with no owner', 'leads_unassigned'),
    (MGMT, 'which leads is nobody working', 'leads_unassigned'),
    (ADMIN, 'orphaned leads', 'leads_unassigned'),
    (SALES, 'my leads without follow-up', 'leads_no_next_action'),
    (SALES, 'leads without a next step', 'leads_no_next_action'),
    (SALES, 'my open Heavy Transport leads', 'leads_open'),
    (SALES, 'active leads in Warehousing', 'leads_open'),
    (SALES, 'leads gone cold this month', 'leads_stale'),
    (HEAD, 'idle leads in my vertical', 'leads_stale'),

    # ── RFQs: overdue, recent, by status ─────────────────────────────
    (SALES, 'overdue RFQs', 'rfqs_unquoted'),
    (HEAD, 'which RFQs are past their quote-by date', 'rfqs_unquoted'),
    (SALES, 'RFQs received this week', 'rfqs_recent'),
    (MGMT, 'recent RFQs', 'rfqs_recent'),
    (HEAD, 'new RFQs today', 'rfqs_recent'),
    (SALES, 'RFQs that came in yesterday', 'rfqs_recent'),
    (MGMT, 'RFQs by status', 'rfqs_by_status'),
    (HEAD, 'RFQ status summary', 'rfqs_by_status'),
    (MGMT, 'status of all RFQs', 'rfqs_by_status'),
    (MGMT, 'RFQs above 1 crore this month', 'rfqs_high_value_recent'),
    (HEAD, 'high value RFQs', 'rfqs_high_value_recent'),

    # ── Quotes: approval, validity, recent, by status ────────────────
    (HEAD, 'quotes pending approval', 'quotes_pending_approval'),
    (HEAD, 'which quotes need approval', 'quotes_pending_approval'),
    (MGMT, 'quotations awaiting approval', 'quotes_pending_approval'),
    (HEAD, 'quotes waiting for my sign-off', 'quotes_pending_approval'),
    (HEAD, 'what is pending my approval', 'quotes_pending_approval'),
    (SALES, 'quotes expiring this week', 'quotes_expiring'),
    (SALES, 'which quotes are about to expire', 'quotes_expiring'),
    (HEAD, 'offers lapsing this month', 'quotes_expiring'),
    (SALES, 'expired quotes', 'quotes_expiring'),
    (SALES, 'quote validity ending soon', 'quotes_expiring'),
    (SALES, 'quotes sent this week', 'quotes_recent'),
    (MGMT, 'recent quotes', 'quotes_recent'),
    (HEAD, 'quotations issued in the last 30 days', 'quotes_recent'),
    (SALES, 'latest quotes', 'quotes_recent'),
    (MGMT, 'quotes by status', 'quotes_by_status'),
    (HEAD, 'quote status summary', 'quotes_by_status'),
    (SALES, 'quotes over 20 lakh this month', 'quotes_above'),

    # ── Pipeline: health, risk, close dates, one account ─────────────
    (HEAD, 'pipeline health', 'pipeline_health'),
    (MGMT, 'how healthy is our pipeline', 'pipeline_health'),
    (HEAD, 'pipeline hygiene check', 'pipeline_health'),
    (SALES, 'is my pipeline healthy', 'pipeline_health'),
    (HEAD, 'which deals are at risk', 'opportunity_risk'),
    (MGMT, 'opportunities at risk', 'opportunity_risk'),
    (HEAD, 'risky opportunities', 'opportunity_risk'),
    (MGMT, 'deals likely to slip', 'opportunity_risk'),
    (SALES, 'at risk deals in Project Freight', 'opportunity_risk'),
    (HEAD, 'opportunities past their close date', 'opps_overdue_close'),
    (MGMT, 'deals with overdue close dates', 'opps_overdue_close'),
    (HEAD, 'which close dates have passed', 'opps_overdue_close'),
    (SALES, 'slipped deals', 'opps_overdue_close'),
    (SALES, 'open deals for Tata Steel', 'account_pipeline'),
    (HEAD, 'pipeline for JSW', 'account_pipeline'),
    (ACCT, 'opportunities with Godrej', 'account_pipeline'),
    (SALES, 'deals with BEML', 'account_pipeline'),
    (HEAD, 'stale opportunities', 'stalled_deals'),
    (MGMT, 'which deals have gone cold', 'stalled_deals'),
    (MGMT, 'Project Freight pipeline value', 'pipeline_value'),
    (HEAD, 'pipeline for Heavy Transport', 'pipeline_value'),
    (MGMT, 'weighted pipeline for Warehousing', 'pipeline_value'),
    (SALES, 'biggest deals in Project Freight', 'top_opportunities'),
    (MGMT, 'what is closing this month in Heavy Transport',
     'closing_this_month'),

    # ── Accounts and customers: health, cross-sell, relationships ────
    (SALES, 'account health for Tata Steel', 'account_health'),
    (HEAD, 'how healthy is JSW', 'account_health'),
    (ACCT, 'health check of Godrej', 'account_health'),
    (MGMT, 'which accounts are at risk', 'account_health'),
    (ACCT, 'account health', 'account_health'),
    (MGMT, 'customer health', 'account_health'),
    (ACCT, 'at risk customers', 'account_health'),
    (SALES, 'cross sell for Tata Steel', 'cross_sell_account'),
    (ACCT, 'what else can we sell to JSW', 'cross_sell_account'),
    (HEAD, 'which services does Godrej not buy', 'cross_sell_account'),
    (SALES, 'what does BEML buy from us', 'cross_sell_account'),
    (ACCT, 'upsell to Siemens', 'cross_sell_account'),
    (SALES, 'who knows Tata Steel', 'relationship_map'),
    (HEAD, 'who at Procam has worked with JSW', 'relationship_map'),
    (ACCT, 'relationship map for Godrej', 'relationship_map'),
    (SALES, 'who is in touch with Alstom', 'relationship_map'),
    (MGMT, 'relationship intelligence', 'relationship_map'),
    (SALES, 'who is the contact at Tata Steel', 'key_contacts'),
    (SALES, 'key contacts for JSW', 'key_contacts'),
    (HEAD, 'decision makers at Godrej', 'key_contacts'),
    (SALES, 'who should I speak to at BEML', 'key_contacts'),
    (OPS, 'contacts at Alstom', 'key_contacts'),
    (SALES, 'who is the contact here', 'key_contacts'),
    (ACCT, 'key contacts', 'key_contacts'),
    (MGMT, 'top accounts', 'top_accounts'),
    (MGMT, 'our biggest customers', 'top_accounts'),
    (HEAD, 'top 10 customers', 'top_accounts'),
    (MGMT, 'accounts by revenue', 'top_accounts'),
    (ACCT, 'who are our best customers', 'top_accounts'),
    (ADMIN, 'accounts with no owner', 'dq_accounts_no_owner'),
    (ADMIN, 'unowned accounts', 'dq_accounts_no_owner'),
    (HEAD, 'which customers have no PIC', 'dq_accounts_no_owner'),
    (SALES, 'accounts without an owner in my scope', 'dq_accounts_no_owner'),
    (ACCT, 'is Jakson a new customer', 'account_status'),
    (ACCT, 'which clients have gone quiet this quarter', 'accounts_inactive'),

    # ── Handovers and projects ───────────────────────────────────────
    (OPS, 'won deals awaiting PO', 'handovers_awaiting_po'),
    (OPS, 'pending handovers', 'handovers_awaiting_po'),
    (MGMT, 'handovers waiting for a purchase order', 'handovers_awaiting_po'),
    (OPS, 'handover backlog', 'handovers_awaiting_po'),
    (SALES, 'deals awaiting customer PO', 'handovers_awaiting_po'),
    (PROJ, 'which projects are waiting for a PO', 'handovers_awaiting_po'),
    (PROJ, 'what was handed over to projects last month',
     'handovers_recent'),
    (PROJ, 'won deals with no handover', 'handover_missing_po'),

    # ── Wins and losses ──────────────────────────────────────────────
    (MGMT, 'what did we win this month', 'deals_won_recent'),
    (HEAD, 'recent wins', 'deals_won_recent'),
    (SALES, 'deals won in the last 30 days', 'deals_won_recent'),
    (MGMT, 'won business this quarter', 'deals_won_recent'),
    (MGMT, 'what did we lose this month', 'deals_lost_recent'),
    (HEAD, 'recent losses', 'deals_lost_recent'),
    (SALES, 'leads lost this week', 'deals_lost_recent'),
    (MGMT, 'lost deals in the last 90 days', 'deals_lost_recent'),
    (ADMIN, 'lost deals without a reason', 'dq_lost_no_reason'),

    # ── Performance ──────────────────────────────────────────────────
    (HEAD, 'win rate by vertical', 'win_rate_by_vertical'),
    (MGMT, 'conversion rate by service', 'win_rate_by_vertical'),
    (MGMT, 'vertical wise win rate', 'win_rate_by_vertical'),
    (SALES, 'my win rate', 'my_win_rate'),
    (SALES, 'my own conversion rate', 'my_win_rate'),
    (SALES, 'how many deals have I won', 'my_win_rate'),
    (SALES, 'my personal hit rate', 'my_win_rate'),
    (HEAD, 'team workload', 'team_workload'),
    (MGMT, 'who has the most open leads', 'team_workload'),
    (HEAD, 'leads per salesperson', 'team_workload'),
    (MGMT, "everyone's workload", 'team_workload'),
    (MGMT, 'what is our win rate this year', 'conversion_rate'),
    (SALES, 'how am I doing this quarter', 'my_performance'),

    # ── Data quality and administration ──────────────────────────────
    (ADMIN, 'intake review queue', 'intake_review_pending'),
    (ADMIN, 'emails pending review', 'intake_review_pending'),
    (ADMIN, 'unreviewed enquiries', 'intake_review_pending'),
    (ADMIN, 'leads missing a vertical', 'dq_missing_fields'),
    (ADMIN, 'duplicate companies', 'dq_duplicates'),

    # ── Operations and project text search ───────────────────────────
    (OPS, 'what is in the BOQ for this lead', 'attachment_contents'),
    (OPS, 'search the notes for crane barge', 'search_text'),
    (PROJ, 'anything about the transformer move', 'search_text'),
    (OPS, 'mentions of ODC in emails', 'search_text'),
    (PROJ, 'what did anyone say about demurrage at Kandla', 'search_text'),
    (OPS, 'search the emails for B/L copy', 'search_text'),
    (PROJ, 'find mentions of heavy lift in the notes last 30 days',
     'search_text'),

    # ── Company 360 and the record in hand ───────────────────────────
    (HEAD, 'tell me about Godrej', 'account_360'),
    (MGMT, 'account 360 for Siemens', 'account_360'),
    (SALES, 'summarise this account', 'account_360'),
    (SALES, 'brief me on this lead before the meeting', 'lead_360'),
    (SALES, 'what should I do next on this lead', 'next_best_action'),
    (SALES, 'who should I call today', 'next_best_action'),

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
    (SALES, 'assign this lead to a colleague', None),
    (MGMT, 'how many people work in operations', None),
    (SALES, 'what is my sales target', None),
    (MGMT, 'profit margin on this job', None),
    (FIN, 'raise an invoice for this job', None),
    (OPS, 'book a trailer for tomorrow', None),
    (MGMT, 'what is the fuel price today', None),
    (SALES, 'send the quote to the customer', None),
    (ADMIN, 'reset my password', None),
    (MGMT, 'what will revenue be next year', None),
    (PROJ, 'how many cranes do we own', None),
]

#: Phrasings a person really types that the rules deliberately leave
#: alone — too loose to route without guessing. A configured model may
#: route them; the rules must not route them anywhere ELSE, which the
#: library test checks. Kept apart so QUESTIONS stays an exact,
#: model-free guarantee.
MODEL_ONLY = [
    (ACCT, 'is the Kandla customer happy with us', 'account_health'),
    (MGMT, 'how exposed are we on the big steel accounts', 'account_health'),
    (HEAD, 'which of our deals will probably not close', 'opportunity_risk'),
    (SALES, 'anything I promised a customer and forgot', 'followups_due'),
    (OPS, 'which jobs are stuck before operations', 'handovers_awaiting_po'),
    (SALES, 'who can introduce me at the transformer plant',
     'relationship_map'),
    (MGMT, 'are we selling enough warehousing to existing clients',
     'cross_sell_gap'),
    (HEAD, 'is anyone drowning in work', 'team_workload'),
]


def by_persona():
    out = {}
    for persona, question, intent in QUESTIONS:
        out.setdefault(persona, []).append(
            {'question': question, 'intent': intent})
    return out


def _normalise(question):
    return ' '.join(''.join(ch if ch.isalnum() else ' '
                            for ch in question.lower()).split())


def duplicates():
    """Phrasings that appear more than once once case and punctuation
    are set aside — "follow-ups due" and "follow ups due" are one entry,
    and a library that counts them twice overstates its reach."""
    seen, dupes = set(), []
    for _p, question, _i in QUESTIONS + MODEL_ONLY:
        key = _normalise(question)
        if key in seen:
            dupes.append(question)
        seen.add(key)
    return dupes


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
