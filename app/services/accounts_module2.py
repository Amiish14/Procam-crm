"""
PROCAM-DECISIONS-02NOV Module 2 — accounts, ownership and merges.

Three rule sets, each written so a person can read why one record
went one way:

  2.1  the account check queue, cleared by rule
  2.2  the 37 owner mismatches, resolved in the decision's order
  2.3  duplicate accounts, auto-merged where the evidence is strong

Everything here *decides*. Nothing here writes: the script holds the
transaction, so rule 0.3 of the pack — "Before any bulk save, send
Nilesh a preview with counts per rule. Save after his OK" — is a
property of the code rather than a habit of whoever runs it.

The 2 November pack reinstates that preview step, which
CRM-COCKPIT-02 §0.3 had removed ("no dry-run approval step"). The
later decision wins, and it is the safer of the two.
"""
from __future__ import annotations

from datetime import timedelta

#: How far back "the person who handled the most leads" looks
#: (Module 2.2 rule 3).
RECENT_MONTHS = 4

#: Rule 4's floor: no vertical, no data, no head — Nilesh.
BACKSTOP = 'DIR12010'


# ── 2.2 rule 1: the named overrides ─────────────────────────────────
#: Account name → who owns it, by NAME. Resolved against the employee
#: master at run time and refused where a name matches nobody or two
#: people: a wrong employee code in an owner column is silent, and
#: the account simply stops appearing on anybody's list.
NAMED_OVERRIDES = (
    ('BEML', 'James Francis Xavier'),
    ('Sarens', 'Tamal Das'),
    ('Siemens Energy', 'Srinivas Marella'),
    ('Nippon Express', 'Gowdhaman Rajakrishnan'),
)


# ── 2.2 rule 2: the agreed map ──────────────────────────────────────
#: "Inactive owners, as per the agreed map". Read the note below
#: before trusting the heading.
#:
#: The pack calls these inactive owners, and several of them are
#: active in the employee master today — Manjurul Hoque, Venkatesh
#: Ramarao Althada, Sumit Mondal, Laxmi Ram Singh, Abhishek Singh and
#: Dhanashree Pawar are all `is_active = 1`. Dhanashree is confirmed
#: active in the same pack, twice.
#:
#: So this map is read as what it does rather than what it is called:
#: a re-pointing of **CRM account and lead ownership**. Nothing here
#: touches `is_active`, and nothing here is a deactivation — Module 8
#: is the only thing that deactivates anybody, from PRERNA, and it
#: has its own rule about her.
#:
#: `plan()` reports which of these people are still active, so the
#: list can be corrected rather than quietly applied under a heading
#: that does not match the data.
REPLACEMENTS = (
    # from (by name, as the pack writes it)   → to
    ('Manju', 'Sanjna Vardhan'),
    ('Manas', 'Sanjna Vardhan'),
    ('Suketu', 'James Francis Xavier'),
    ('Venkatesh', 'Vijay T V'),
    ('Setu', 'Srinivas Marella'),
    ('Sumit', 'Pravin Choudhary'),
    ('Dhanashree', 'Pravin Choudhary'),
    ('Laxmi', 'Sharayu Uday Bhosale'),
    ('Abhishek Singh', 'Seema Chattopadhyay'),
)

#: One exception inside rule 2, named in the pack: "Abhishek Singh →
#: Seema (Sarens → Tamal)". Sarens is already a rule-1 override, so
#: this is belt and braces — rule 1 runs first and wins — but it is
#: written down because the pack writes it down, and a reader
#: checking the two against each other should find both.
REPLACEMENT_EXCEPTIONS = (
    ('Abhishek Singh', 'Sarens', 'Tamal Das'),
)

#: Whose ownership moves, and whose does not. Module 2 says of
#: Dhanashree: "Module 2 moves only her CRM lead/account ownership to
#: Pravin. Do not deactivate her in Module 8." Both halves are
#: enforced: here, and by the absence of her name from anything that
#: writes `is_active`.
CRM_OWNERSHIP_ONLY = ('Dhanashree',)


def _employee_by_name(name, *, Employee):
    """(employee, problem). Exact match first, then a contains match.

    Refuses on ambiguity rather than taking the first row. Two people
    whose names both contain 'Sumit' is a question for a person, and
    guessing writes a wrong owner that nobody sees until the account
    stops appearing on a list.
    """
    name = (name or '').strip()
    if not name:
        return None, 'no name given'
    rows = Employee.query.filter(Employee.name == name).all()
    if not rows:
        rows = Employee.query.filter(
            Employee.name.ilike(f'%{name}%')).all()
    if not rows:
        return None, f'no employee called {name!r}'
    if len(rows) > 1:
        exact = [r for r in rows
                 if (r.name or '').strip().lower() == name.lower()]
        if len(exact) == 1:
            return exact[0], None
        return None, (f'{len(rows)} people match {name!r}: '
                      + ', '.join(f'{r.name} ({r.emp_code})'
                                  for r in rows))
    return rows[0], None


# ── 2.1 what may never become an account ────────────────────────────
#: A name this module refuses to create an account from.
#:
#: Found by the dry run. Rule 2 says "create the account from the name
#: the user typed", and leads #10096 and #10098 had the typed name
#: "Unknown" — so the rule, followed literally, would have created an
#: account called Unknown and linked them to it. That is the exact
#: fault `account_match.not_a_customer` was written to stop on the
#: LINKING side: a lead filed under "Unknown" looks placed, so it never
#: reaches Account check again, and the histories of several real
#: customers pool under one meaningless row. Account #847 "New Lead"
#: already holds both fried-sped.de and jindalstainless.com.
#:
#: The linking guard could not catch this one, because the account did
#: not exist yet. So the same rule is applied one step earlier: at
#: creation.
#:
#: The list is NOT written out again here. `PLACEHOLDER_NAMES` and
#: `OUR_DOMAINS` already exist and are read by the matcher and by
#: `not_a_customer`; a second copy is how a name gets added to one and
#: not the other, and then an account nobody may link to is an account
#: this module happily creates.

#: What is left of a name once the punctuation and spacing are gone.
#: '???' and '...' and '  -  ' are all blank, and all three are in the
#: company column today.
def _bare(text):
    return ''.join(c for c in (text or '').lower() if c.isalnum())


def squash(text):
    """`company_match.norm`, then the spaces out too.

    One key for "Hitachi Energy", "Hitachienergy" and "HITACHI
    ENERGY"; one key for "TQCERT", "TQ Cert" and "T.Q. Cert". `norm`
    alone keeps the spaces, which is right for its own job — it is the
    key the matcher LINKS on, and linking on a squashed key would join
    two companies whose names differ only by a word break. Here the
    squashed key is used for two softer things: collapsing the
    spellings of one proposed account, and *proposing* a link for a
    person to confirm. Neither links anything by itself.
    """
    from app.services.company_match import norm
    return ''.join(ch for ch in norm(text) if ch.isalnum())


#: A name must have this many letters or digits left after squashing.
#: "Se" and "ABB" do not; both go to Account check for a person to
#: name properly. Set by the decision of 2 Nov, and it is the rule
#: that costs the most false refusals — BEML has exactly four, and
#: anything shorter that is a real customer reaches Account check
#: rather than being lost.
MIN_NAME_LETTERS = 4

#: A word that is not a company on its own. The whole name, not a part
#: of it: "Global" is refused and "Global Calcium" is not.
#:
#: These are the words the company column holds when somebody typed
#: what the business DOES instead of who it is. An account called
#: Logistics collects every enquiry whose sender signed off with their
#: department.
GENERIC_NAMES = frozenset((
    'global', 'group', 'logistics', 'logistic', 'transport',
    'transports', 'transportation', 'shipping', 'shipment', 'freight',
    'cargo', 'courier', 'services', 'service', 'solutions', 'solution',
    'india', 'international', 'enterprises', 'enterprise', 'industries',
    'industry', 'trading', 'traders', 'exports', 'export', 'imports',
    'import', 'company', 'corporation', 'projects', 'project',
    'engineering', 'engineers', 'warehouse', 'warehousing', 'supply',
    'supplies', 'procurement', 'purchase', 'purchasing', 'sales',
    'marketing', 'accounts', 'admin', 'office', 'se', 'sa', 'ag',
))


def why_not_an_account_name(text, *, domain=None):
    """'' if an account may be created with this name, else why not.

    Five reasons, in the order they are cheapest to check:

      * nothing to name it after;
      * a placeholder — the shared list, so adding one word protects
        the matcher, `not_a_customer` and this at the same time;
      * punctuation only, which `_bare` reduces to nothing;
      * us. An account called Procam anything is an internal forward
        that matched itself, and once created it swallows every later
        one;
      * our own mail domain, for the same reason — #983 "Unknown"
        lists `procamgroup.in`, and every internal forward matched it
        on an exact rule.

    A refusal is not a failure. The lead stays in Account check, where
    a person types the real customer's name, which is the one thing no
    rule can do.
    """
    from app.services.account_match import PLACEHOLDER_NAMES

    name = (text or '').strip()
    if not name:
        return 'there is no company name to create an account from'
    if name.lower() in PLACEHOLDER_NAMES:
        return (f'{name!r} is a placeholder, not a company — an account '
                f'named this collects unrelated customers and is never '
                f'looked at again')
    if not _bare(name):
        return (f'{name!r} is punctuation, not a name')
    if 'procam' in _bare(name):
        return (f'{name!r} is us, not a customer — an account named '
                f'after Procam swallows every internal forward')
    key = squash(name)
    if len(key) < MIN_NAME_LETTERS:
        return (f'{name!r} is {len(key)} letter(s) long once the '
                f'punctuation and the legal suffix are off — too short '
                f'to be sure what company it is')
    if key in GENERIC_NAMES or name.strip().lower() in GENERIC_NAMES:
        return (f'{name!r} is what the business does, not who it is — '
                f'an account named this collects every enquiry whose '
                f'sender typed their department')
    if domain:
        try:
            from app.services.mail_signals import OUR_DOMAINS
            low = str(domain).strip().lower()
            if any(low == d or low.endswith('.' + d) for d in OUR_DOMAINS):
                return (f'{low} is our own mail domain — the sender is a '
                        f'colleague forwarding an enquiry, not the '
                        f'customer')
        except Exception:                                 # noqa: BLE001
            pass
    return ''


# ── 2.1 senders that are not customers at all ───────────────────────
#: The Not-relevant classes, each with the words that identify it.
#:
#: Matched against the sender address, the subject and the typed
#: company name — never the body, because a real enquiry quoting a
#: conference it is shipping to would match `events` on its own text.
#:
#: Narrower than it reads. Every pattern below was chosen so that a
#: real enquiry does not match it: `insurance` looks for a solicitation
#: ("we offer cargo cover"), not for the word insurance, which appears
#: in half of all freight enquiries because the client is telling us
#: who insures the cargo.
NOT_RELEVANT_CLASSES = (
    ('marketing', 'marketing or a newsletter', (
        r'\bnewsletter\b', r'\bunsubscribe\b', r'\bwebinar\b',
        r'\bpromotional?\b', r'\bdiscount\b', r'\bfree trial\b',
        r'\bsubscribe now\b', r'\bdigest\b', r'\bcampaign\b',
        r'\bmailchimp\b', r'\bsendgrid\b', r'\bhubspot\b',
        r'@mailer\.', r'@sendgrid\.', r'@mailchimp',
    )),
    ('events', 'an event, a conference or sponsorship', (
        r'\bconferences?\b', r'\bsummit\b', r'\bexpo\b',
        r'\bexhibition\b', r'\bawards?\b', r'\bdelegate\b',
        r'\bsponsorship\b', r'\bspeaker (slot|opportunity)\b',
        r'\bregister (now|today)\b', r'\bearly bird\b',
        r'\btrade fair\b',
    )),
    ('portals', 'a directory or listing portal soliciting us', (
        r'@justdial\.', r'@indiamart\.', r'@tradeindia\.',
        r'@exportersindia\.', r'@sulekha\.', r'@yellowpages',
        r'\blisting (package|plan)\b', r'\bpremium listing\b',
        r'\bverified supplier\b', r'\bbuy leads?\b',
    )),
    ('insurance', 'an insurance or cover solicitation', (
        r'\b(we|our firm) (offer|provide)s? .{0,30}'
        r'(insuranc|cover|polic)',
        r'\bmarine (cargo )?(insuranc|cover) (quote|offer|plan)s?\b',
        r'\binsurance (broker|advisor|agency)\b',
        r'\bpolicy renewal\b', r'\bpremium quote\b',
    )),
    ('broker', 'a forwarder or broker soliciting business', (
        r'\b(agent|partner)(ship)? (network|enquiry|proposal)\b',
        r'\blooking for (a )?(partner|agent|associate)s?\b',
        r'\bwe are a (leading |reputed )?(freight |customs )?'
        r'(forwarder|broker|agent)\b',
        r'\bour (rates|tariff) (for|are) (your )?reference\b',
        r'\boffer(ing)? (you )?(our )?(best )?(rates|tariff)s?\b',
        r'\bco-?loader\b', r'\bnvocc\b.{0,20}\b(offer|rates)\b',
    )),
)

#: What a `portals` match deliberately does NOT catch.
#:
#: `junk_suggest` records the measurement: `noreply@*` was left off the
#: block list because 226 of the 247 leads it would have held were
#: freight load postings and SAP Ariba RFQ invitations, "two of which
#: are open opportunities with owners right now". A tender or
#: procurement portal is how a large client sends an RFQ, so the
#: pattern list above names the directory portals that solicit US and
#: not the procurement portals that buy FROM us.
PROCUREMENT_PORTALS = ('ariba', 'gem.gov.in', 'eproc', 'tender',
                       'coupa', 'jaggaer', 'sap.com', 'bidding')


def not_relevant_class(lead):
    """(code, why) when this lead is not a customer, else (None, '').

    Two kinds, in order:

      1. the sender is on the block list. That list is a decision
         somebody already made, in Master Data, and this reads it
         rather than holding a second copy;
      2. the mail is one of the five classes above.

    Nothing here marks anything. The caller stops short of creating an
    account and reports the lead for the Not-relevant queue, which is
    where a person decides.
    """
    import re

    from app.services import junk_delete as jd

    sender = jd.sender_of(lead)

    try:
        from app.services import system_senders
        hit, pattern, why = system_senders.blocks(sender)
        if hit:
            return 'blocked_sender', (f'the sender is on the block list '
                                      f'({pattern}): {why}')
    except Exception:                                     # noqa: BLE001
        pass

    subject = (getattr(lead, 'original_email_subject', '') or '')
    typed = (getattr(lead, 'company', '') or '')
    haystack = ' '.join((sender, subject, typed)).lower()

    if any(p in haystack for p in PROCUREMENT_PORTALS):
        # A procurement portal is how a large client sends an RFQ. Two
        # of the leads this would otherwise have caught are open
        # opportunities with owners.
        return None, ''

    for code, label, patterns in NOT_RELEVANT_CLASSES:
        for pattern in patterns:
            if re.search(pattern, haystack, re.I):
                return code, (f'{label} — matched {pattern!r} in the '
                              f'sender, subject or typed name')
    return None, ''


# ── 2.1 a name that is already an account under another spelling ────
def _alias_index(accounts):
    """{squashed spelling: (account, which spelling)} over names and aliases.

    Aliases are included because a merge keeps the loser's spelling as
    an alias of the survivor — which is exactly the record that says
    "Lntecc is L&T", and the only place that fact can live.
    """
    out = {}
    for account in accounts:
        for spelling, source in ([(account.name, 'its name')]
                                 + [(a, 'an alias')
                                    for a in (account.aliases or [])]):
            key = squash(spelling)
            if key and key not in out:
                out[key] = (account, source, str(spelling))
    return out


def looks_like_existing(name, domain, aliases, by_domain):
    """(account, how) when this name is probably an account already.

    Wider than `company_match.match` on purpose, because the outcome is
    different: `match` LINKS, and this only PROPOSES a link for a
    person to confirm on Account check. So it can afford the comparisons
    that would be unsafe to act on automatically.

    Four, in descending certainty:

      1. the squashed name is exactly an account's name or alias —
         "Hitachienergy" is "Hitachi Energy" with the space missing;
      2. the sender's domain, read as a name, is — a mail from
         hitachienergy.com with nothing typed at all;
      3. the squashed name starts with an ALIAS of three or more
         letters — "Lntecc" starts with the alias "LNT". Aliases only,
         and three is low on purpose: an alias is a spelling somebody
         deliberately recorded, and the outcome here is a proposal a
         person confirms. A prefix of an account NAME is held to eight,
         because proposing that "Global Calcium" is "Global" would be
         wrong often enough to teach people to ignore the suggestion;
      4. the squashed name starts with an account name of eight or
         more letters, which is long enough that a shared prefix is
         not a coincidence.
    """
    key = squash(name)
    if not key:
        return None, ''

    if key in aliases:
        account, source, spelling = aliases[key]
        return account, (f'{name!r} is {account.name} — same name once '
                         f'the spacing and punctuation are off'
                         + ('' if source == 'its name'
                            else f' (matched {spelling!r}, {source})'))

    if domain:
        from_domain = squash(_name_from_domain(domain))
        if from_domain and from_domain in aliases:
            account, _source, _spelling = aliases[from_domain]
            return account, (f'{domain} reads as {account.name}')
        account = by_domain.get(domain)
        if account is not None:
            return account, f'{domain} is already {account.name}'

    best = None
    for other, (account, source, spelling) in aliases.items():
        floor = 3 if source == 'an alias' else 8
        if len(other) < floor or not key.startswith(other):
            continue
        if best is None or len(other) > len(best[0]):
            best = (other, account, spelling, source)
    if best is not None:
        _other, account, spelling, source = best
        return account, (f'{name!r} starts with {spelling!r} ({source} '
                         f'of {account.name})')
    return None, ''


# ── 2.1 the account check queue ─────────────────────────────────────
def autoclear_plan():
    """What the queue would do to itself (Module 2.1).

    The rules, in the decision's order:

      1. the email domain or the name matches an existing account →
         link the lead to it
      2. no match → create the account from the name the user typed,
         else the domain name — unless that name is a placeholder or
         is us, in which case the lead STAYS IN ACCOUNT CHECK for a
         person to name. See `why_not_an_account_name`.
      3. the owner is the lead owner, else the vertical head
      4. anything the rules cannot settle → Nilesh's Thursday review

    Returns (rows, review, stays): the leads the rules clear, the ones
    for Thursday, and the ones a person must name. Three lists and not
    two, because "a person types the customer's name" and "Nilesh
    decides something" are different jobs for different people.
    """
    from app import Company, Employee, Lead, db
    from app.services import (account_match, company_match,
                              vertical_heads)

    accounts = Company.query.filter(Company.is_active.is_(True)).all()
    accounts = [a for a in accounts
                if not account_match.not_a_customer(a)]
    index = company_match.build_index(accounts)
    by_domain = _domain_index(accounts)
    aliases = _alias_index(accounts)

    rows, review, stays, not_relevant = [], [], [], []
    for lead in _queue_leads(Lead=Lead, db=db):
        # Before any of the four rules: is this a customer at all. A
        # lead from a conference organiser has a company name, a sender
        # domain and nothing else wrong with it, so every rule below
        # would clear it happily and create the account.
        nr_code, nr_why = not_relevant_class(lead)
        if nr_code:
            from app.services import junk_delete as jd
            not_relevant.append({
                'lead': lead, 'class': nr_code, 'why': nr_why,
                'sender': jd.sender_of(lead),
            })
            continue
        vertical = None
        try:
            vertical = account_gate.vertical_of(lead)
        except Exception:                                 # noqa: BLE001
            vertical = (lead.procam_vertical or '').strip() or None

        account, rule, why = None, '', ''
        domain = _domain_of(lead)

        # 1 — the domain, then the name.
        if domain and domain in by_domain:
            account, rule = by_domain[domain], 'domain matches'
            why = f'{domain} is already {account.name}'
        if account is None:
            typed = (lead.company or '').strip()
            # `match` answers (company, reason, candidates): a company
            # with reason None is certain, AMBIGUOUS means several
            # equally good. Only the certain answer is a link — the
            # near-neighbour candidates it offers are suggestions for
            # a person and were never an automatic link.
            found, reason, candidates = (
                company_match.match(typed, index) if typed
                else (None, company_match.NO_MATCH, []))
            if found is not None and reason is None:
                account, rule = found, 'name matches'
                why = f'{typed!r} normalises to {account.name}'
            elif reason == company_match.AMBIGUOUS:
                # Two accounts match one name: the ambiguous *master*
                # that Module 2.3 exists to fix. Not a question about
                # this lead, so it goes to the review rather than
                # being guessed at.
                review.append({
                    'lead': lead, 'why': (
                        f'{typed!r} matches {len(candidates)} active '
                        f'accounts — merge them first (Module 2.3)')})
                continue

        # 2 — create it, if there is anything real to name it after.
        created_name = ''
        if account is None:
            typed = (lead.company or '').strip()
            created_name = typed or _name_from_domain(domain)
            # Both candidates are checked, and the typed name is checked
            # as a NAME while the domain is checked as a domain: "Procam
            # Logistics" typed into the company box and an enquiry
            # forwarded from procamlogistics.com are the same mistake
            # arriving two ways.
            refused = why_not_an_account_name(created_name, domain=domain)
            if refused:
                stays.append({
                    'lead': lead,
                    'candidate': created_name or '(nothing)',
                    'why': refused,
                    'suggest': None,
                    'owner': (lead.assigned_to or '').strip(),
                })
                continue
            # One more question before creating: is this already an
            # account under a different spelling. `match` said no,
            # because it compares the normalised name with the spaces
            # still in it — so "Hitachienergy" and "Hitachi Energy" are
            # two different keys to it and one company to a reader.
            #
            # A proposal, never a link. The lead goes to Account check
            # with the account named on it, and a person presses the
            # button. That is the difference between this and rule 1.
            similar, how = looks_like_existing(created_name, domain,
                                               aliases, by_domain)
            if similar is not None:
                stays.append({
                    'lead': lead,
                    'candidate': created_name,
                    'why': (f'{how} — link it to the account that exists '
                            f'rather than creating a second one'),
                    'suggest': similar,
                    'owner': (lead.assigned_to or '').strip(),
                })
                continue
            rule = ('create from the typed name' if typed
                    else 'create from the sender domain')
            why = f'no account matched; create {created_name!r}'

        # 3 — the owner.
        owner = (lead.assigned_to or '').strip()
        owner_why = 'the lead owner'
        if not owner:
            # `heads_for_vertical` returns the head first, then the
            # deputy. Only the head: a deputy standing in silently is
            # the thing that release was written to stop.
            heads = (vertical_heads.heads_for_vertical(vertical)
                     if vertical else [])
            owner = heads[0] if heads else ''
            owner_why = f'the {vertical} head' if owner else ''
        if not owner:
            owner, owner_why = BACKSTOP, 'the backstop — no owner, ' \
                                         'no vertical head'

        rows.append({
            'lead': lead, 'rule': rule, 'why': why,
            'account_id': (account.id if account else None),
            'account_name': (account.name if account
                             else created_name),
            'create': account is None,
            'vertical': vertical or '',
            'owner': owner, 'owner_why': owner_why,
        })
    collapse_creations(rows)
    return rows, review, stays, not_relevant


def collapse_creations(rows):
    """One proposed account per company, however many ways it is spelt.

    "Drewes Group" and "Drewes", "Westfracht" and "WESTFRACHT",
    "TQCERT" and "T.Q. Cert" are each one customer. Keyed on `squash`,
    so case, punctuation, spacing and the legal suffix all stop
    mattering.

    The canonical spelling is the one the most leads used. A tie is
    broken in three steps, and the order was settled by watching it get
    one wrong:

      1. the FEWEST punctuation marks. Raw length was tried first and
         picked "T.Q. Cert" over "TQCERT", because the dots made it
         longer without saying anything;
      2. then the MOST words — "Drewes Group" over "Drewes", because
         the extra word is information and the shorter name can be read
         out of the longer one;
      3. then the one that is not shouting, so "Westfracht" beats
         "WESTFRACHT". An account name is printed on a quote.

    `apply_queue` already keys its own cache on the lower-cased name,
    which collapsed the case variants and nothing else. Doing it here
    instead means the number in the preview is the number of accounts
    that will exist, which is what rule 0.3 asks Nilesh to approve.
    """
    from collections import Counter

    groups = {}
    for row in rows:
        if not row['create']:
            continue
        groups.setdefault(squash(row['account_name']), []).append(row)

    collapsed = []
    for key, members in groups.items():
        spellings = Counter(r['account_name'].strip() for r in members)
        def _rank(item):
            name, count = item
            punctuation = sum(1 for ch in name
                              if not ch.isalnum() and not ch.isspace())
            return (-count, punctuation, -len(name.split()),
                    name.isupper(), name)

        canonical = sorted(spellings.items(), key=_rank)[0][0]
        if len(spellings) > 1:
            collapsed.append((canonical, dict(spellings)))
        for row in members:
            if row['account_name'].strip() != canonical:
                row['why'] += (f'; spelt {row["account_name"].strip()!r} '
                               f'on this lead, filed as {canonical!r}')
                row['account_name'] = canonical
    return collapsed


def _domain_index(accounts):
    """{domain: account}, from the account's own `email_domains`.

    The same reading `account_match` and `account_duplicates` use —
    the column, lower-cased, with the leading @ off and free-mail
    domains dropped. One reading, in three places, because an
    account that the matcher thinks owns a domain and this does not
    is an account whose leads land in two different places.

    First row wins on a clash. A domain claimed by two accounts is
    the ambiguous master Module 2.3 exists to fix, and the merge
    plan reports it; linking leads to whichever account sorted first
    would hide it.
    """
    from app.services import account_match

    out = {}
    for account in accounts:
        for raw in (getattr(account, 'email_domains', None) or []):
            domain = str(raw).strip().lower().lstrip('@')
            if domain and not account_match.is_free_mail(domain):
                out.setdefault(domain, account)
    return out


def _queue_leads(*, Lead, db):
    """The leads Account check is holding — the gate's own reading.

    `pending_leads()` is the population behind the tile, the badge
    and the queue, so this counts the same thing all three do. It
    scopes to the signed-in person and returns [] with the flag off,
    neither of which suits a script, so the fallback is the gate's
    own definition read without a scope.
    """
    from app.services import account_gate

    try:
        if account_gate.enabled():
            found = account_gate.pending_leads()
            if found:
                return found
    except Exception:                                     # noqa: BLE001
        db.session.rollback()

    from app.services import sales_rules

    leads = (Lead.query
             .filter(Lead.is_archived.isnot(True))
             .filter(Lead.stage.in_(sales_rules.open_stages()))
             .order_by(Lead.id.asc()).all())
    out = []
    for lead in leads:
        try:
            if account_gate.why_pending(lead):
                out.append(lead)
        except Exception:                                 # noqa: BLE001
            db.session.rollback()
            if lead.company_id is None:
                out.append(lead)
    return out


def _domain_of(lead):
    raw = (getattr(lead, 'email', '') or '').strip().lower()
    if '@' not in raw:
        return ''
    from app.services import account_match

    domain = account_match.domain_of(raw)
    if not domain or account_match.is_free_mail(domain):
        # gmail.com is not a company. Matching on it would join every
        # personal-address enquiry into one account.
        return ''
    return domain


def _name_from_domain(domain):
    if not domain:
        return ''
    stem = domain.split('.')[0]
    return stem.replace('-', ' ').title() if stem else ''


# ── 2.2 the owner mismatches ────────────────────────────────────────
def owner_plan():
    """Who should own each account, and under which rule.

    Returns ([{account, vertical, was, now, rule, why}], problems).
    """
    from app import Company, Employee, Lead, db
    from app.models.assignment import AccountVerticalOwner
    from app.services import vertical_heads

    out, problems, notes = [], [], []

    # Rule 1 — the named overrides, resolved by name.
    named = {}
    for account_name, person in NAMED_OVERRIDES:
        emp, problem = _employee_by_name(person, Employee=Employee)
        if problem:
            problems.append(f'{account_name}: {problem} — left alone')
            continue
        named[account_name.lower()] = (emp, person)

    # Rule 2 — the agreed map, resolved by name.
    replace = {}
    for from_name, to_name in REPLACEMENTS:
        src, src_problem = _employee_by_name(from_name,
                                             Employee=Employee)
        dst, dst_problem = _employee_by_name(to_name, Employee=Employee)
        if dst_problem:
            problems.append(f'{from_name} → {to_name}: {dst_problem} '
                            f'— nothing moved')
            continue
        if src_problem:
            # Not a fault: a name in the map that matches nobody may
            # be somebody who has already gone from the master.
            notes.append(f'{from_name}: {src_problem} — nothing of '
                         f'theirs to move')
            continue
        if src.is_active:
            notes.append(
                f'{src.name} ({src.emp_code}) is ACTIVE in the '
                f'employee master, and the pack lists them under '
                f'"inactive owners". Their CRM ownership moves to '
                f'{dst.name}; nothing deactivates them.')
        replace[src.emp_code] = (dst, from_name, to_name)

    rows = (AccountVerticalOwner.query
            .filter(AccountVerticalOwner.active.is_(True)).all())
    for row in rows:
        account = db.session.get(Company, row.account_id)
        if account is None:
            problems.append(f'owner row {row.id} points at account '
                            f'{row.account_id}, which is not there')
            continue
        decided = _decide_owner(row, account, named, replace,
                               Employee=Employee, Lead=Lead,
                               vertical_heads=vertical_heads)
        if decided is None:
            continue
        emp_code, rule, why = decided
        if emp_code == (row.emp_code or ''):
            continue
        out.append({
            'account_id': account.id, 'account': account.name,
            'vertical': row.vertical, 'row_id': row.id,
            'was': row.emp_code or '', 'now': emp_code,
            'rule': rule, 'why': why,
        })
    return out, problems, notes


def _decide_owner(row, account, named, replace, *, Employee, Lead,
                  vertical_heads):
    """(emp_code, rule, why) — the decision's four rules, in order."""
    name_key = (account.name or '').strip().lower()

    # 1 — a named override. Matched on the account name containing
    # the decision's word: "BEML" is BEML LIMITED, "Sarens" is
    # Sarens India Pvt Ltd.
    for key, (emp, person) in named.items():
        if key and key in name_key:
            return emp.emp_code, '1 named override', \
                f'the decision names {person} for {key.upper()}'

    # 2 — the agreed map.
    moved = replace.get(row.emp_code or '')
    if moved is not None:
        dst, from_name, to_name = moved
        for who, account_word, instead in REPLACEMENT_EXCEPTIONS:
            if who == from_name and account_word.lower() in name_key:
                emp, problem = _employee_by_name(instead,
                                                 Employee=Employee)
                if emp is not None:
                    return emp.emp_code, '2 agreed map (exception)', \
                        f'{from_name} → {instead} for {account_word}'
        return dst.emp_code, '2 agreed map', \
            f'{from_name} → {to_name}'

    # 3 — whoever handled the most leads on this account lately.
    busiest, count = _busiest_on(account.id, row.vertical, Lead=Lead)
    if busiest:
        return busiest, '3 most leads in 4 months', \
            f'{count} lead(s) on this account since then'

    # 4 — the vertical head, else the backstop.
    head = ''
    try:
        heads = vertical_heads.heads_for_vertical(row.vertical)
        head = heads[0] if heads else ''
    except Exception:                                     # noqa: BLE001
        head = ''
    if head:
        return head, '4 vertical head', \
            f'no data; the {row.vertical} head'
    return BACKSTOP, '4 backstop', \
        'no data, no vertical head — the floor, not a preference'


def _busiest_on(account_id, vertical, *, Lead):
    """(emp_code, n) for the busiest owner, or ('', 0) on a tie.

    A tie returns nothing, so rule 4 runs. The decision says "On a
    tie or no data: the vertical head", and a tie broken by whichever
    employee code sorts first is not a decision.
    """
    from collections import Counter
    from datetime import datetime

    from app import db
    from app.services import quote_due as qd

    since = datetime.utcnow() - timedelta(days=30 * RECENT_MONTHS)
    query = (Lead.query
             .filter(Lead.company_id == account_id)
             .filter(Lead.created_at >= since)
             .filter(Lead.assigned_to.isnot(None)))
    if vertical:
        query = query.filter(Lead.procam_vertical == vertical)
    counts = Counter((lead.assigned_to or '').strip()
                     for lead in query.all()
                     if (lead.assigned_to or '').strip())
    if not counts:
        return '', 0
    ranked = counts.most_common()
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return '', ranked[0][1]
    return ranked[0]


# ── 2.3 duplicate accounts ──────────────────────────────────────────
#: Why a cluster is or is not safe to merge without a person.
AUTO = 'auto'
ASK = 'ask'


def merge_confidence(cluster):
    """(AUTO|ASK, reason) for one cluster from account_duplicates.

    The decision says "Auto-merge where they share the same GSTIN,
    the same email domain, or the same name after normalising… Only
    low-confidence pairs go to Seema's in-CRM queue". This is where
    the line is drawn, and one rule does most of the work:

    **Two different GSTINs in a cluster is never one customer.** A
    GSTIN is a legal identity. Godrej & Boyce and Godrej Agrovet
    share godrej.com and are different companies — the same pack says
    so, in the sentence that blocks one Godrej and not the others. A
    domain joins them, `clusters()` joins transitively, and an
    auto-merge would make them one account and apply one block
    decision to both.

    So:

      same GSTIN                 → auto. The strongest evidence there
                                   is; two rows, one legal entity.
      names normalise alike,     → auto. "Praj Industries Ltd" and
      no GSTIN conflict            "PRAJ INDUSTRIES LIMITED".
      domain only, no GSTIN      → ask. A corporate domain is shared
      and no name match            by a group, not by a company.
      any GSTIN conflict         → ask, whatever else matches.
    """
    gstins = {(row.get('gstin') or '').strip().upper()
              for row in cluster['accounts']}
    gstins.discard('')
    if len(gstins) > 1:
        return ASK, (f'{len(gstins)} different GSTINs in one cluster '
                     f'({", ".join(sorted(gstins))}) — a GSTIN is a '
                     f'legal identity, so these are separate '
                     f'companies that share a domain or a name')

    reasons = set(cluster.get('reasons') or ())
    if len(gstins) == 1 and 'same GSTIN' in reasons:
        return AUTO, 'one GSTIN across the cluster'

    norms = {(row.get('norm') or '').strip()
             for row in cluster['accounts']}
    norms.discard('')
    if len(norms) == 1 and 'names normalise alike' in reasons:
        return AUTO, f'every name normalises to {sorted(norms)[0]!r}'

    if reasons == {'shared email domain'}:
        return ASK, ('joined only by a shared email domain, which a '
                     'group of companies has in common')

    return ASK, ('the evidence is mixed: '
                 + ', '.join(sorted(reasons) or ['none']))


def merge_plan(found):
    """([auto], [ask]) — clusters split by confidence."""
    auto, ask = [], []
    for cluster in found:
        verdict, reason = merge_confidence(cluster)
        entry = dict(cluster)
        entry['verdict'], entry['verdict_why'] = verdict, reason
        (auto if verdict == AUTO else ask).append(entry)
    return auto, ask
