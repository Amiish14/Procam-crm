"""Training content — §74, §75.

Ten levels, each following LEARN → PRACTICE → VALIDATE.  The material
describes the CRM as it actually behaves, including the rules that catch
people out: why a lead with no owner is invisible, why deleting a Won deal
is refused, why "Siemens Ltd" and "Siemens Limited" are one company.

Practice is validated inside this module (§77) — a learner types an answer
and it is checked here.  Nothing touches production data.
"""

CONTENT_VERSION = '1'

# key, level, title, why it matters, sections, practice, quiz
LEVELS = [
    {
        'key': 'basics', 'level': 1, 'title': 'CRM Basics',
        'why': 'Where to start each day, and why My Work is the only list '
               'that matters.',
        'sections': [
            ('My Work is your day',
             'My Work is your landing page. It shows what is assigned to '
             'you, what is due today, and what is overdue. Everything else '
             'in the CRM is context; this is the list of things that need '
             'you.'),
            ('Ownership is what makes work visible',
             'A lead with no owner appears in nobody\'s My Work. It is not '
             'neglected — it is invisible. There is no task, no reminder '
             'and no escalation. If you find an unassigned lead that '
             'should be worked, assign it to someone rather than leaving '
             'it.'),
            ('Overdue means the SLA was missed',
             'Tasks carry a due time from their SLA. When one passes, it '
             'is escalated to the vertical head automatically. Clearing '
             'overdue work matters more than clearing new work.'),
        ],
        'practice': {
            'prompt': 'Open My Work and read your Overdue count. Type the '
                      'number you see.',
            'kind': 'number',
            'validate': 'any_number',
            'hint': 'It is the first tile. Type 0 if you have none.',
        },
        'quiz': [
            ('A lead has no owner. Who sees it in My Work?',
             ['Nobody', 'Every admin', 'The vertical head',
              'The person who created it'], 0),
            ('What happens to a task when its SLA passes?',
             ['It is deleted', 'It is escalated to the vertical head',
              'Nothing', 'It is reassigned at random'], 1),
        ],
    },
    {
        'key': 'company_people', 'level': 2,
        'title': 'Companies, People and Card Scan',
        'why': 'One organisation, one record — however many ways you deal '
               'with it.',
        'sections': [
            ('Create once, classify, link everywhere',
             'An organisation exists once in Company Master. It can be a '
             'Customer AND a Vendor AND a Competitor at the same time — '
             'those are classifications on one record, not three records. '
             'Never create a second company because the relationship '
             'changed.'),
            ('Names are matched loosely on purpose',
             '"Siemens Ltd", "Siemens Limited" and "SIEMENS LTD." are the '
             'same organisation; legal suffixes and punctuation are '
             'ignored. But "BHEL" and "Bharat Heavy Electricals" are NOT '
             'matched automatically — an abbreviation is a guess, and the '
             'CRM will ask a person instead of guessing.'),
            ('Scanning a card',
             'On a phone, My Work carries a Scan card button. Capture the '
             'card, check the extracted fields — they are never saved '
             'without your review — classify the organisation, and add a '
             'follow-up if there is one. Adding a follow-up creates a lead '
             'assigned to you; leaving it blank just files the contact.'),
        ],
        'practice': {
            'prompt': 'A card reads "Vedanta Ltd." and Company Master '
                      'already holds "Vedanta Limited". What should '
                      'happen when you save the card?',
            'kind': 'choice',
            'options': ['A second company is created',
                        'The contact attaches to the existing Vedanta',
                        'The card is rejected',
                        'Both companies are merged automatically'],
            'answer': 1,
            'hint': 'Legal suffixes are ignored when matching.',
        },
        'quiz': [
            ('A company is both a customer and a competitor. How many '
             'records should exist?',
             ['One, with both classifications', 'Two', 'Three',
              'One per vertical'], 0),
            ('Why does the CRM refuse to match "BHEL" to "Bharat Heavy '
             'Electricals" automatically?',
             ['They are different companies',
              'An abbreviation is a guess, so a person decides',
              'BHEL is not in the system', 'It is a bug'], 1),
        ],
    },
    {
        'key': 'accounts', 'level': 3, 'title': 'Account Development',
        'why': 'Company 360 is the whole institutional relationship in one '
               'place.',
        'sections': [
            ('Company 360',
             'Clicking a company anywhere — a report, an opportunity, a '
             'search — opens the same Company 360. It shows the '
             'classifications, the account owner, every lead and '
             'opportunity, the win rate, the contacts, and a timeline of '
             'everything that has happened.'),
            ('Activities are what make the timeline worth reading',
             'Calls, meetings and visits logged against a lead appear on '
             'the company timeline. An account with no activities has no '
             'history, and the next person to own it starts from nothing.'),
            ('Account owner',
             'Every company should have an account owner. A company with '
             'no owner appears on the Data Quality dashboard, because '
             'nobody is accountable for that relationship.'),
        ],
        'practice': {
            'prompt': 'Open any company from the Companies list and type '
                      'the number of opportunities shown on its Company '
                      '360 header.',
            'kind': 'number', 'validate': 'any_number',
            'hint': 'It is one of the tiles under the company name.',
        },
        'quiz': [
            ('Where do the activities on a Company 360 timeline come from?',
             ['Typed on the company', 'Logged against its leads',
              'Imported monthly', 'Generated automatically'], 1),
        ],
    },
    {
        'key': 'leads', 'level': 4, 'title': 'Leads and Opportunities',
        'why': 'Every enquiry enters one pool, and one person is '
               'accountable for each.',
        'sections': [
            ('One central pool',
             'Leads arrive from the mailbox, from imports, and from people '
             'creating them. However they arrive, they enter the same '
             'pool, are linked to a company, and are assigned to an owner.'),
            ('Visibility is not accountability',
             'A team may be able to see an opportunity while one person is '
             'accountable for acting on it. The assigned owner is who the '
             'task belongs to.'),
            ('Stage drives the task',
             'Moving a lead through its stages creates the next task '
             'automatically. You do not need to remember to make one.'),
        ],
        'practice': {
            'prompt': 'An opportunity is not linked to any company. Which '
                      'reports will it appear in?',
            'kind': 'choice',
            'options': ['All of them', 'None of the account reports',
                        'Only the funnel', 'Only Won Value'],
            'answer': 1,
            'hint': 'Account reports group by company.',
        },
        'quiz': [
            ('An opportunity has no company_id. What happens in Won Value '
             'by Account?',
             ['It is counted normally',
              'It appears on a "not linked to an account" line',
              'It is deleted', 'It doubles'], 1),
        ],
    },
    {
        'key': 'rfq', 'level': 5, 'title': 'RFQs and Rate Sourcing',
        'why': 'The handover from enquiry to priced offer.',
        'sections': [
            ('Raising an RFQ',
             'An RFQ records what the customer asked for: origin, '
             'destination, cargo and scope. It carries a lead driver — the '
             'person accountable for getting it quoted.'),
            ('Rate sourcing',
             'Each service line can be sent to a different person for a '
             'rate. Each becomes their task, with its own SLA.'),
            ('The SLA is the promise',
             'Rate sourcing, quote preparation and quote submission each '
             'have a target time. Missing one escalates it.'),
        ],
        'practice': {
            'prompt': 'Who is accountable for an RFQ being quoted on time?',
            'kind': 'choice',
            'options': ['Whoever sourced the rate', 'The lead driver',
                        'The vertical head', 'The customer'],
            'answer': 1,
            'hint': 'Every task has exactly one accountable owner.',
        },
        'quiz': [
            ('Two people are sourcing rates for one RFQ. How many people '
             'are accountable for the RFQ itself?',
             ['One', 'Two', 'Three', 'Nobody'], 0),
        ],
    },
    {
        'key': 'quote', 'level': 6, 'title': 'Quotes',
        'why': 'What was offered, at what price, and what happened next.',
        'sections': [
            ('Preparing and submitting',
             'A quote is prepared against an RFQ, approved where required, '
             'and submitted. Each step is a task with an owner.'),
            ('Value must be recorded',
             'A quote or a won deal with no value counts as zero in every '
             'report, however well it is linked. If you know the number, '
             'record it.'),
            ('Revisions',
             'A revised quote supersedes the previous one rather than '
             'overwriting it, so the negotiation history survives.'),
        ],
        'practice': {
            'prompt': 'A Won deal is linked to the right account but has '
                      'no value. What does Won Value by Account show for '
                      'it?',
            'kind': 'choice',
            'options': ['The average deal size', 'Zero',
                        'It is excluded', 'An estimate'],
            'answer': 1,
            'hint': 'Nothing is estimated on your behalf.',
        },
        'quiz': [
            ('Why does a revision supersede rather than overwrite?',
             ['To save space', 'To keep the negotiation history',
              'It does overwrite', 'For legal reasons only'], 1),
        ],
    },
    {
        'key': 'won_lost', 'level': 7, 'title': 'Negotiation, Won and Lost',
        'why': 'A loss is only useful if you record why.',
        'sections': [
            ('Closing',
             'Marking a deal Won creates the handover task. Marking it '
             'Lost asks for the reason and the competitor.'),
            ('Lost without a competitor teaches nothing',
             'Thousands of lost deals in the CRM have no competitor '
             'recorded. That means no pattern can be seen — who beats us, '
             'where, and on what. Recording it takes seconds and is the '
             'only way the next quote improves.'),
        ],
        'practice': {
            'prompt': 'You lose a deal to a rival on price. What are the '
                      'two things worth recording?',
            'kind': 'choice',
            'options': ['Nothing, the deal is closed',
                        'The loss reason and the competitor',
                        'Only the loss reason', 'Only the value'],
            'answer': 1,
            'hint': 'Competitive learning needs both.',
        },
        'quiz': [
            ('What is lost when a Lost deal has no competitor recorded?',
             ['Nothing', 'The ability to see who beats us and where',
              'The deal value', 'The customer record'], 1),
        ],
    },
    {
        'key': 'competitor', 'level': 8, 'title': 'Competitor Intelligence',
        'why': 'A competitor is a company you already have a record for.',
        'sections': [
            ('Competitor is a classification',
             'Competitors are not a separate list. A competitor is a '
             'company in Company Master carrying the Competitor '
             'classification, so its Company 360 shows the competitive '
             'record alongside everything else.'),
            ('What to log',
             'Tender results, known pricing, new equipment, a new office, '
             'a senior appointment, public news. Each entry carries a '
             'date, a source and who added it.'),
            ('Several competitors on one deal',
             'An opportunity can carry several competitors — possible, '
             'likely, confirmed, and the one who won.'),
        ],
        'practice': {
            'prompt': 'Where do you find a competitor\'s win/loss record '
                      'against Procam?',
            'kind': 'choice',
            'options': ['A separate Competitors database',
                        'On its Company 360, when classified as Competitor',
                        'Only in reports', 'It is not recorded'],
            'answer': 1,
            'hint': 'One record per organisation.',
        },
        'quiz': [
            ('A company sells to us and competes with us. How is that '
             'recorded?',
             ['Two records', 'One record with both classifications',
              'A note', 'It cannot be'], 1),
        ],
    },
    {
        'key': 'handover', 'level': 9, 'title': 'Won → Operations Handover',
        'why': 'A won deal is not finished until operations can act on it.',
        'sections': [
            ('The handover queue',
             'Every Won deal creates a handover task. Until it is done, '
             'the deal shows on the Data Quality dashboard as won without '
             'handover.'),
            ('What operations needs',
             'Scope, rates, timeline, contacts and any commitments made '
             'during negotiation. The person who won it knows these; '
             'nobody else does.'),
        ],
        'practice': {
            'prompt': 'You have just marked a deal Won. What happens next '
                      'in the CRM?',
            'kind': 'choice',
            'options': ['Nothing', 'A handover task is created',
                        'The lead is deleted', 'The customer is emailed'],
            'answer': 1,
            'hint': 'Closing a deal creates the next task automatically.',
        },
        'quiz': [
            ('Why does the handover matter to the person who won the deal?',
             ['It does not', 'Only they know what was committed',
              'It is a formality', 'For invoicing only'], 1),
        ],
    },
    {
        'key': 'reports', 'level': 10,
        'title': 'Reports, Data Quality and Access',
        'why': 'Reading the numbers, and knowing when not to trust them.',
        'sections': [
            ('Reports show their gaps',
             'Won Value by Account carries a line for deals not linked to '
             'an account. If you see it, the total is complete but some '
             'value is unattributed — the report is telling you where the '
             'data is thin rather than hiding it.'),
            ('What you can see',
             'Admins see the whole company. Vertical heads see their own '
             'vertical. Everyone else works from My Work. That applies '
             'identically to reports, PIC 360 and exports — there is no '
             'route that shows more than you are allowed.'),
            ('Data Quality',
             'The Data Quality dashboard lists what is wrong with the data '
             'right now and what each problem costs. It is measured live, '
             'so a number there always agrees with what the reports see.'),
        ],
        'practice': {
            'prompt': 'A vertical head opens Won Value by Account. Whose '
                      'deals are in it?',
            'kind': 'choice',
            'options': ['Every deal in the company',
                        'Only their own vertical\'s deals',
                        'Only deals they personally own',
                        'None — they cannot open it'],
            'answer': 1,
            'hint': 'Scope is the same everywhere.',
        },
        'quiz': [
            ('You see a "(not linked to an account)" line on a report. '
             'What does it mean?',
             ['The report is broken',
              'Some value could not be attributed, and is shown rather '
              'than dropped',
              'Those deals are cancelled', 'Nothing'], 1),
            ('Who can see the whole company\'s reports?',
             ['Everyone', 'Admins', 'Vertical heads', 'Nobody'], 1),
        ],
    },
]

BY_KEY = {level['key']: level for level in LEVELS}
PASS_MARK = 70
