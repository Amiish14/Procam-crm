"""What each Data Quality check is, and every threshold it uses — §66.

Pure on purpose: this file imports nothing from the app, so the read-only
report (scripts/data_quality_report.py) can load it by file path without
importing app.py, whose boot autoheal is a write. The live module and the
report then read the same thresholds, stage lists and name normalisation,
and a change made here reaches both.

The queries themselves are not here. The live module needs the Access
Matrix and the ORM; the report needs a read-only SQL connection and no
app. tests/test_data_quality_agreement.py holds the two to the same
answer on one seeded database.
"""
import re

# ── thresholds ───────────────────────────────────────────────────────
# Change them here and nowhere else. Each is a judgement about when silence
# becomes a problem, and the Data Quality Guide quotes these names.

#: An open lead with no call, activity or email for this long is neglected.
NO_CONTACT_DAYS = 30
#: An open opportunity nobody has updated or worked for this long is stale.
STALE_OPPORTUNITY_DAYS = 45
#: An account with no lead, opportunity or activity for this long is
#: treated as an inactive customer.
INACTIVE_CUSTOMER_MONTHS = 12
#: An email classification waiting for a reviewer longer than this is stuck.
REVIEW_BACKLOG_DAYS = 7

#: Largest batch correction. Beyond this a mistake is too big to review.
BATCH_MAX = 500
#: How long a preview stays valid. After it, preview again.
PREVIEW_TTL_SECONDS = 600
#: How many daily snapshots the dashboard sparkline draws.
TREND_POINTS = 30
#: Records per page on the detail screen.
PAGE_SIZE = 100
#: Rows in one CSV export. A bigger problem than this is not a spreadsheet job.
EXPORT_MAX = 20000
#: A follow-up or close date further out than this is almost always a typo.
MAX_FUTURE_DAYS = 3 * 366

SEVERITIES = ('critical', 'high', 'medium', 'low')

# ── vocabularies the checks share ────────────────────────────────────
LEAD_TERMINAL_STAGES = ('Won', 'Lost', 'On Hold', 'Not Interested')

OPP_WON = ('Won', 'Closed Won')
OPP_LOST = ('Lost', 'Closed Lost')
#: Opportunities past these are not "overdue to close" — they closed.
OPP_CLOSED = OPP_WON + OPP_LOST + ('On Hold', 'Not Interested')

#: RFQ statuses that no longer need a quote.
RFQ_NO_QUOTE_NEEDED = ('Lost', 'Withdrawn')

#: The only intake class that should ever have created a lead.
LEAD_CREATING_CLASSES = ('A_new_lead',)
REVIEW_CLASS = 'J_needs_review'
#: Email classes that, filed as outbound from outside Procam, mean a quote
#: or rate received was recorded as one Procam sent.
RECEIVED_AS_SENT_CLASSES = ('H_quote_submission', 'G_rate_sourcing')
INTERNAL_EMAIL_DOMAINS = ('procamlogistics.com', 'procamgroup.in')


def internal_domains(extra=''):
    """Procam's own mail domains, plus INTERNAL_EMAIL_DOMAINS from the env."""
    return set(INTERNAL_EMAIL_DOMAINS) | {
        d.strip().lower() for d in (extra or '').split(',') if d.strip()}


def blank(v):
    return v is None or str(v).strip() == ''


def norm_name(name):
    """Loose company-name key for spotting duplicates.

    Looser than company_match.norm, which links records and must not
    guess: here a false match costs a person a look, a missed one costs a
    split account history.
    """
    s = ' ' + (name or '').lower() + ' '
    s = re.sub(r'[.,&/()\-]', ' ', s)
    for junk in ('pvt', 'private', 'ltd', 'limited', 'llp', 'inc', 'llc',
                 'gmbh', 'co', 'corporation', 'corp', 'company', 'the',
                 'india'):
        # Twice: adjacent junk words share the space between them.
        s = re.sub(rf'\s{junk}\s', ' ', s)
        s = re.sub(rf'\s{junk}\s', ' ', s)
    return ' '.join(s.split())


def norm_phone(p):
    """Last ten digits, so +91 98765 43210 and 9876543210 match."""
    digits = re.sub(r'\D', '', p or '')
    return digits[-10:] if len(digits) >= 10 else ''


def email_domains(value):
    """Company.email_domains holds a JSON list; older rows hold text."""
    if isinstance(value, (list, tuple)):
        parts = value
    else:
        text = str(value or '')
        parts = re.split(r'[,;\s\[\]"\']+', text)
    return {str(d).strip().lower().lstrip('@') for d in parts
            if str(d).strip().strip('@')}


# ── batch corrections ────────────────────────────────────────────────
#: action → (label, kind of value it needs, entity types it applies to)
BATCH_ACTIONS = {
    'assign_owner': ('Assign an owner', 'employee',
                     ('lead', 'opportunity', 'company')),
    'set_followup': ('Set the follow-up date', 'date', ('lead',)),
    'set_close_date': ('Set the expected close date', 'date',
                       ('opportunity',)),
    'set_vertical': ('Set the vertical', 'vertical', ('lead', 'company')),
    'archive': ('Archive (reversible)', None, ('lead',)),
    'link_account': ('Link to the one account with this name', None,
                     ('lead',)),
}


# ── the checks ───────────────────────────────────────────────────────
# One entry per check. `report` names the matching CSV in the read-only
# report, where there is one. `actions` are the batch corrections offered
# on its detail page; an empty tuple means the fix is a person's decision
# on each record and no batch tool is offered.

def _c(key, title, severity, entity, cost, suggestion, route,
       actions=(), report=None):
    assert severity in SEVERITIES, severity
    for a in actions:
        assert entity in BATCH_ACTIONS[a][2], (key, a)
    return {'key': key, 'title': title, 'severity': severity,
            'entity': entity, 'cost': cost, 'suggestion': suggestion,
            'route': route, 'actions': tuple(actions), 'report': report}


CHECKS = [
    # ── ownership ────────────────────────────────────────────────────
    _c('unowned_leads', 'Leads with no owner', 'critical', 'lead',
       'They never appear in anyone\'s My Work, so no task, reminder or '
       'escalation ever reaches them.',
       'Assign an owner with a batch correction, or from Admin → Bulk '
       'Leads.',
       '/admin/leads?unowned=1', ('assign_owner', 'archive'),
       report='leads_without_owner'),
    _c('leads_of_leavers', 'Leads owned by someone who has left',
       'critical', 'lead',
       'Assigned to an inactive employee, so the enquiry sits with nobody.',
       'Reassign to an active colleague with a batch correction, or '
       'archive what is dead.',
       '/admin/leads', ('assign_owner', 'archive'),
       report='leads_without_owner'),
    _c('unowned_opps', 'Opportunities with no owner', 'critical',
       'opportunity',
       'Nobody is accountable for progressing the deal or forecasting it.',
       'Assign an owner with a batch correction.', '/app', ('assign_owner',),
       report='opportunities_without_owner'),
    _c('opps_of_leavers', 'Opportunities owned by someone who has left',
       'high', 'opportunity',
       'The deal and its forecast belong to someone who can no longer act '
       'on them.',
       'Reassign to an active colleague with a batch correction.', '/app',
       ('assign_owner',),
       report='opportunities_without_owner'),
    _c('no_pic', 'Accounts with no owner', 'high', 'company',
       'No one is accountable for the relationship, and new leads from the '
       'account cannot be routed.',
       'Assign an owner with a batch correction, or in Accounts → '
       'Owners.',
       '/accounts/owners', ('assign_owner',),
       report='accounts_without_owner'),
    _c('accounts_of_leavers', 'Accounts owned by someone who has left',
       'high', 'company',
       'New leads from the account route to an inactive employee.',
       'Assign an active owner with a batch correction, or in '
       'Accounts → Owners.',
       '/accounts/owners', ('assign_owner',),
       report='accounts_without_owner'),
    _c('tasks_no_owner', 'Tasks with no owner', 'critical', 'task',
       'They sit in the task engine and appear for nobody.',
       'Open My Work as an administrator and reassign each task.',
       '/my-work'),
    _c('tasks_of_leavers', 'Tasks owned by someone who has left', 'high',
       'task',
       'They will never be completed or escalated.',
       'Reassign the tasks from My Work, or reassign the records they '
       'belong to.', '/my-work'),

    # ── activity and pipeline hygiene ────────────────────────────────
    _c('stale_leads', f'Open leads with no contact in {NO_CONTACT_DAYS} days',
       'high', 'lead',
       'An enquiry nobody has called or emailed goes to a competitor who '
       'did.',
       'Call or email the customer and log it, set a follow-up date, or '
       'archive what is dead.', '/app',
       ('set_followup', 'assign_owner', 'archive')),
    _c('leads_no_followup', 'Open leads with no follow-up, or one overdue',
       'medium', 'lead',
       'Without a follow-up date nothing reminds the owner, so the next '
       'step depends on memory.',
       'Set a follow-up date with a batch correction, or open the lead '
       'and plan the next step.', '/app', ('set_followup',)),
    _c('stale_opps', 'Stale opportunities', 'high', 'opportunity',
       f'Open deals past their close date or untouched for '
       f'{STALE_OPPORTUNITY_DAYS} days inflate the forecast.',
       'Update the stage, or set a realistic expected close date with a '
       'batch correction.',
       '/app', ('set_close_date', 'assign_owner'),
       report='opportunities_overdue_close'),
    _c('opps_no_close_date', 'Open opportunities with no close date',
       'medium', 'opportunity',
       'A deal with no expected close date is left out of every forecast '
       'by month.',
       'Set the expected close date with a batch correction.', '/app', ('set_close_date',)),
    _c('rfq_no_quote', 'RFQs past their quote-by date with no quote',
       'high', 'rfq',
       'The customer asked for a price by a date that has passed, and '
       'none was recorded.',
       'Open the RFQ and raise the quote, or mark it Lost or Withdrawn.',
       '/rfqs'),

    # ── deals won ────────────────────────────────────────────────────
    _c('won_no_value', 'Won deals with no value recorded', 'medium',
       'opportunity',
       'They count as zero in every value report however well linked.',
       'Open each deal and enter the won value.', '/app'),
    _c('won_no_po', 'Won deals with no customer PO yet', 'critical',
       'handover',
       'The Project and Job are created against the PO, so these cannot '
       'move until the Customer PO, Sales Order or Contract is recorded.',
       'Record the PO on the handover in Handovers → Awaiting PO.',
       '/handovers?status=Awaiting%20PO', report='won_without_po'),
    _c('dupe_po_refs', 'One PO on more than one handover', 'critical',
       'handover',
       'Two projects would be created against a single customer order.',
       'Correct the PO reference on the wrong handover, or cancel it.',
       '/handovers?status=all'),
    _c('won_no_handover', 'Won deals with no TMS handover', 'low',
       'opportunity',
       'Operations have nothing to act on for business already won.',
       'Create the handover from the deal, or from Handovers.',
       '/handovers', report='won_without_handover'),
    _c('lost_no_competitor', 'Lost deals with no competitor recorded',
       'low', 'opportunity',
       'No competitive learning is captured from the loss (§20).',
       'Record who won on the deal\'s Competitors tab.', '/competitors'),

    # ── linking and duplicates ───────────────────────────────────────
    _c('unlinked_opps', 'Opportunities not linked to an account',
       'critical', 'opportunity',
       'They are excluded from every account report, so real value is '
       'invisible.',
       'Open each deal and choose its account.', '/companies'),
    _c('unlinked_leads', 'Leads not linked to an account', 'medium', 'lead',
       'They do not appear on their Company 360, so the account history '
       'is incomplete.',
       'Link with a batch correction where the name matches exactly one '
       'account; decide the rest in Data Mapping.', '/companies', ('link_account',)),
    _c('pending_mappings', 'Company names awaiting a decision', 'medium',
       'mapping',
       'Each undecided name leaves every record carrying it unlinked.',
       'Decide them on the Data Mapping screen.', '/app'),
    _c('dupe_companies', 'Duplicate accounts', 'medium', 'company',
       'The same organisation held twice splits its history, owners and '
       'value across two records.',
       'Merge with scripts/2026_09_11_company_dedup.py — it previews first '
       'and every merge can be reversed.', '/companies',
       report='duplicate_accounts'),
    _c('dupe_contacts', 'Duplicate contacts', 'medium', 'contact',
       'Two records for one person means notes and history land on either '
       'at random.',
       'Keep one record per person and deactivate the other.',
       '/app', report='duplicate_contacts'),

    # ── integrity ────────────────────────────────────────────────────
    _c('orphan_records', 'Records pointing at a lead or account that is '
       'gone', 'medium', 'mixed',
       'Emails, notes and deals attached to nothing are invisible — and '
       'SQLite hands a deleted id to the next new lead, which then inherits '
       'them.',
       'Report these to the CRM administrator; they are repaired by script, '
       'never in bulk from here.', '/admin/data-quality'),
    _c('broken_relationships', 'Broken relationships', 'medium', 'mixed',
       'A deal whose lead belongs to another account, or a contact whose '
       'account is gone, is reported under the wrong customer.',
       'Open each record and correct its account.', '/companies'),
    _c('empty_mandatory', 'Mandatory fields left empty', 'medium', 'mixed',
       'A lead with no company or stage, a deal with no stage or an '
       'account with no name cannot be filtered, routed or reported.',
       'Open each record and fill in the missing field.', '/app'),
    _c('lead_account_mismatch', 'Lead name does not match its account',
       'medium', 'lead',
       'A lead linked to the wrong account shows up on another customer\'s '
       'Company 360.',
       'Open the lead and relink it, or confirm the name is a trading name '
       'of the account.', '/companies'),
    _c('lead_unknown_vertical', 'Leads with a vertical not in Master Data',
       'medium', 'lead',
       'A vertical outside the master list escapes every vertical report '
       'and vertical-scoped view.',
       'Set a vertical from Master Data with a batch correction, or add '
       'the missing one under Master Data first.', '/admin/leads', ('set_vertical',)),
    _c('account_unknown_vertical',
       'Accounts with a vertical not in Master Data', 'medium', 'company',
       'Leads from the account route to a desk that does not exist.',
       'Set a vertical from Master Data with a batch correction.', '/accounts/owners',
       ('set_vertical',)),

    # ── email intake ─────────────────────────────────────────────────
    _c('email_leads_non_lead', 'Email leads the classifier says are not '
       'leads', 'high', 'lead',
       'Supplier mail, replies or internal mail in the pipeline waste a '
       'salesperson\'s day and distort conversion rates.',
       'Open each lead: archive it with a batch correction if it is not an '
       'enquiry, or correct its classification in Lead Review.', '/lead-review',
       ('archive', 'assign_owner')),
    _c('review_backlog', f'Leads stuck in review over '
       f'{REVIEW_BACKLOG_DAYS} days', 'medium', 'mixed',
       'An enquiry waiting for a reviewer is an enquiry nobody has '
       'answered.',
       'Work the Lead Review queue: accept, reject or reclassify each.',
       '/lead-review'),
    _c('classification_orphans', 'Classifications pointing at a deleted '
       'lead', 'low', 'classification',
       'The training record says a lead was created that no longer '
       'exists, so the learning engine counts a lead that is gone.',
       'Reopen each in Lead Review and decide it again.', '/lead-review'),
    _c('quotes_filed_as_sent', 'Quotes received filed as sent', 'high',
       'email',
       'An agent\'s price recorded as Procam\'s quote may have moved the '
       'lead to Quoted with the wrong amount.',
       'Open each lead and check its stage and quoted amount.', '/app',
       report='quotes_received_filed_as_sent'),

    # ── customers ────────────────────────────────────────────────────
    _c('inactive_customers', f'Accounts inactive for '
       f'{INACTIVE_CUSTOMER_MONTHS} months', 'low', 'company',
       'A customer nobody has spoken to in a year is one a competitor is '
       'already serving.',
       'Plan an account review with the owner, or mark the account '
       'inactive.', '/reports/dormant-accounts'),
]

BY_KEY = {c['key']: c for c in CHECKS}
