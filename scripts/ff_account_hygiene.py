"""
FF-LINK-01, parts 3 to 5 — accounts for the unmatched, and the
accounts nobody should have created.

    .venv/bin/python scripts/ff_account_hygiene.py --check
    .venv/bin/python scripts/ff_account_hygiene.py --apply   # part 3 only

Three jobs, one read and two of them read-only for good.

  3. Create an account for each open FF lead that matched nothing.
     The rule, as given: the company name somebody typed, and
     failing that a name derived from the email domain. `--apply`
     writes these and nothing else.

  4. List the accounts whose name is a mail-system word (Mail,
     Asia, Info) or was taken from a *subdomain* rather than a
     company — with how many leads hang off each. **Nothing is
     changed.**

     "The name matches its own domain" was the first rule here and
     it was wrong: it flagged 297 accounts including NTPC, RITES and
     AGUNSA, because most companies' domains are their names. A list
     that long and that wrong is a list nobody reads. Merging or renaming an account moves every lead,
     contact and quote attached to it, and which of two names is
     the real company is not a thing to infer.

  5. For the four accounts too short to redact, show the sender
     domains their leads use and any company name that appears in a
     mail signature, so a full legal name can be entered by hand.
"""
import argparse
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from app import Company, Lead, STAGES_TERMINAL, app, db   # noqa: E402
from app.services import (account_match, company_match,   # noqa: E402
                          ff_privacy, lead_mode)

#: Words that are a mail system or a place, never a company. Used
#: alongside the data-driven test below, because an account called
#: "Info" is junk whether or not its domain still exists.
DOMAIN_LABELS = {
    'mail', 'info', 'sales', 'admin', 'contact', 'support', 'office',
    'service', 'services', 'web', 'www', 'smtp', 'mta', 'noreply',
    'no-reply', 'enquiry', 'enquiries', 'export', 'exports', 'import',
    'imports', 'asia', 'india', 'global', 'group', 'corp', 'team',
    'docs', 'ops', 'operations', 'accounts', 'billing', 'finance',
    'marketing', 'hr', 'care', 'help', 'desk', 'inbox', 'crm',
}

#: Two-part public suffixes. `ntpc.co.in` has three labels and no
#: subdomain: `co.in` is the suffix, `ntpc` is the company. Without
#: this list every Indian, British, Japanese and Brazilian company
#: reads as a mail host — which is what the first version of this
#: check did, flagging NTPC, ISGEC and Hyundai Rotem as junk.
PUBLIC_SUFFIX_2 = {
    'co.in', 'net.in', 'org.in', 'gen.in', 'firm.in', 'ind.in',
    'co.uk', 'org.uk', 'me.uk', 'ltd.uk', 'plc.uk',
    'com.au', 'net.au', 'org.au', 'com.br', 'net.br', 'com.cn',
    'net.cn', 'org.cn', 'co.jp', 'or.jp', 'ne.jp', 'co.kr', 'or.kr',
    'com.my', 'com.sg', 'com.tr', 'com.tw', 'com.hk', 'com.mx',
    'com.ar', 'co.za', 'co.nz', 'com.pk', 'com.bd', 'com.vn',
    'com.ph', 'com.sa', 'com.eg', 'co.id', 'com.ua', 'co.th',
}


def registrable(domain):
    """`mail.oyaso.com` -> `oyaso.com`; `ntpc.co.in` -> `ntpc.co.in`.

    Only as good as the suffix list above, which is the common
    cases rather than the whole Public Suffix List — getting this
    wrong in the safe direction means a junk account is missed, not
    that a real one is condemned.
    """
    parts = [p for p in (domain or '').lower().split('.') if p]
    if len(parts) < 3:
        return '.'.join(parts)
    if '.'.join(parts[-2:]) in PUBLIC_SUFFIX_2:
        return '.'.join(parts[-3:])
    return '.'.join(parts[-2:])


def is_subdomain(domain):
    d = (domain or '').lower().strip('.')
    return bool(d) and d != registrable(d)


#: A legal suffix at the end of a line is the strongest sign that
#: the line is a company name in a signature.
SIG_SUFFIX = re.compile(
    r'\b(?:pvt\.?\s*ltd\.?|private\s+limited|limited|ltd\.?|llp|llc|'
    r'inc\.?|gmbh|b\.?v\.?|s\.?a\.?|s\.?r\.?l\.?|co\.?,?\s*ltd\.?|'
    r'corporation|company)\b', re.IGNORECASE)


#: "company" and "corporation" appear in prose — "View our company
#: presentation" is not a company name. A legal form at the end of a
#: line is what marks a signature, so the strong test anchors there.
SIG_LEGAL = re.compile(
    r'(?:pvt\.?\s*ltd\.?|private\s+limited|limited|ltd\.?|llp|llc|'
    r'inc\.?|gmbh|b\.?v\.?|s\.?r\.?l\.?|corporation|co\.?,?\s*ltd\.?)'
    r'\.?\s*$', re.IGNORECASE)

#: Domains that send mail on somebody else's behalf. Their label is
#: never the client: ccsend.com is Constant Contact, onmicrosoft.com
#: is a Microsoft tenant, ariba.com is SAP's procurement network —
#: the Ariba leads on this system are really Jindal Stainless.
MAIL_PLATFORMS = {
    'ccsend.com', 'onmicrosoft.com', 'ariba.com', 'brevosend.com',
    'sendgrid.net', 'mailchimp.com', 'mcsv.net', 'rsgsv.net',
    'mailgun.org', 'amazonses.com', 'sparkpostmail.com',
    'createsend.com', 'cmail19.com', 'hubspot.com', 'constantcontact.com',
    'outlook.com', 'protection.outlook.com', 'mimecast.com',
}


# ── 3. accounts for the unmatched ────────────────────────────────────
def _name_from_domain(domain):
    """`coscon.com` -> `Coscon`. The fallback, not the preference."""
    label = (domain or '').split('.')[0]
    return label.replace('-', ' ').strip().title()


def unmatched_plan():
    """[(lead, name, rule, warning)] for every open FF lead with no
    account and no match."""
    import scripts.ff_link_accounts as link

    rows, _buckets = link.survey()
    out = []
    for r in rows:
        if r['verdict'] != link.NONE:
            continue
        lead = r['lead']
        typed = (lead.company or '').strip()
        domain = account_match.domain_of(
            account_match.normalise_address(lead.email))
        free = account_match.is_free_mail(domain)
        warning = ''
        if typed:
            name, rule = typed, 'company name as typed'
            # The rule was given as "typed first", and it is followed
            # — but when what somebody typed is just the domain
            # label, following it creates exactly the kind of account
            # part 4 is about. Said out loud rather than quietly
            # overridden.
            if domain and company_match.norm(typed) == \
                    company_match.norm(_name_from_domain(domain)):
                warning = (f'the typed name is the domain label of '
                           f'{domain} — this will create another '
                           f'domain-label account')
        elif domain and not free:
            name, rule = _name_from_domain(domain), 'email domain'
        else:
            name, rule = '', 'nothing to go on'
            warning = ('no company name and no usable domain'
                       + (' (free mail)' if free else ''))
        if not ff_privacy.variants(name):
            warning = (warning + '; ' if warning else '') + \
                'too short for the masking to protect — needs an alias'
        out.append((lead, name, rule, warning))
    return out


def create_accounts(plan, *, actor='FF-LINK-01'):
    """Make the accounts and link their leads. Caller commits."""
    from app.services import audit

    made, skipped = [], []
    by_name = {}
    for lead, name, rule, _w in plan:
        if not name:
            skipped.append((lead, 'nothing to name it after'))
            continue
        key = company_match.norm(name)
        acct = by_name.get(key)
        if acct is None:
            acct = next((c for c in Company.query.filter(
                Company.is_active.isnot(False)).all()
                if company_match.norm(c.name) == key), None)
        if acct is None:
            domain = account_match.domain_of(
                account_match.normalise_address(lead.email))
            acct = Company(
                name=name[:200], is_active=True,
                vertical=lead_mode.VERTICAL,
                email_domains=([domain] if domain
                               and not account_match.is_free_mail(domain)
                               else []),
                created_by=actor)
            db.session.add(acct)
            db.session.flush()
            made.append((acct, rule))
            audit.record('company.created', 'company', str(acct.id),
                         new={'name': acct.name, 'rule': rule,
                              'from_lead': lead.id},
                         actor=actor,
                         reason=f'No account matched lead #{lead.id}; '
                                f'created by {rule} before FF goes on.')
        by_name[key] = acct
        lead.company_id = acct.id
        audit.record('lead.account_linked', 'lead', str(lead.id),
                     old={'company_id': None},
                     new={'company_id': acct.id, 'account': acct.name,
                          'rule': f'{rule} (account created)'},
                     actor=actor,
                     reason='Linked to an account created for it.')
    return made, skipped


# ── 4. the accounts nobody should have created ───────────────────────
def domain_label_accounts():
    """[(account, why, open_leads, all_leads)] — read only."""
    counts_open = Counter()
    counts_all = Counter()
    for l in Lead.query.filter(Lead.company_id.isnot(None)).all():
        counts_all[l.company_id] += 1
        if (l.stage or '') not in STAGES_TERMINAL:
            counts_open[l.company_id] += 1

    out = []
    for c in Company.query.filter(Company.is_active.isnot(False)).all():
        name = (c.name or '').strip()
        toks = [t for t in re.split(r'[^0-9A-Za-z]+', name) if t]
        why = []
        if len(toks) == 1 and toks[0].lower() in DOMAIN_LABELS:
            why.append('a mail-system or place word, not a company')
        # "The name matches its own domain" is NOT a junk signal —
        # most companies' domains are their names, and that rule
        # flagged NTPC, RITES, AGUNSA and 290 others. The real tell
        # is a name taken from a *subdomain*: mail.oyaso.com gave an
        # account called "Mail" and asia.meap.com gave "Asia", while
        # agunsa.com giving "Agunsa" is simply correct.
        for d in (c.email_domains or []):
            if not is_subdomain(d):
                continue
            label = _name_from_domain(d)
            if label and company_match.norm(label) == \
                    company_match.norm(name):
                why.append(f'taken from the subdomain of {d}, which '
                           f'names a mail host rather than a company')
                break
        if why:
            out.append((c, '; '.join(why), counts_open[c.id],
                        counts_all[c.id]))
    return sorted(out, key=lambda r: (-r[3], (r[0].name or '').lower()))


#: What Nilesh decided on 2026-10-20, having read the proposal the
#: rule produced. These are not exceptions to the rule so much as
#: the places where a person knows something the data does not: MEAP
#: is Mitsubishi Electric Asia Pacific, ccsend.com is a mail host
#: with TRT International behind it, and marineinsight.com is a
#: newsletter nobody ever sold anything to.
#:
#: Kept here rather than passed on the command line because each one
#: is several decisions at once — a rename plus an alias, a merge
#: plus a rename of the target — and a shell line that long is a
#: thing nobody can check before running it.
DECISIONS = {
    661: {'action': 'rename', 'to': 'Mitsubishi Electric Asia Pacific',
          'alias': 'Mitsubishi Electric Corporation',
          'why': 'MEAP is Mitsubishi Electric Asia Pacific; the mail '
                 'is signed Mitsubishi Electric Corporation'},
    985: {'action': 'rename', 'to': 'TRT International Ltd',
          'why': 'ccsend.com sends the mail; TRT International signs '
                 'it'},
    849: {'action': 'merge', 'into': 1031,
          'add_domains': ['delpagroup.com'],
          'why': 'the same Delpa, reached on a second domain'},
    892: {'action': 'merge', 'into': 749,
          'then_rename': 'Hapag-Lloyd India Pvt Ltd',
          'why': 'sales.hlag.com is Hapag-Lloyd, and "Hlag" is no '
                 'better a name than "Sales"'},
    762: {'action': 'retire', 'dev_stage': 'Dormant',
          'lead_stage': 'Lost',
          'lost_reason': 'Others: newsletter, not a client',
          'why': 'marineinsight.com is a maritime newsletter, not a '
                 'client'},
}


def junk_evidence(account):
    """What this account's own leads say it is."""
    leads = Lead.query.filter(Lead.company_id == account.id).all()
    typed, senders, sigs = Counter(), Counter(), Counter()
    for l in leads:
        name = (l.company or '').strip()
        if name and company_match.norm(name) != \
                company_match.norm(account.name):
            typed[name] += 1
        for addr in (l.email, l.original_email_from):
            d = account_match.domain_of(
                account_match.normalise_address(addr))
            if d and not account_match.is_free_mail(d):
                senders[d] += 1
        for n in _signature_names(l.original_email_body):
            sigs[n] += 1
    return {'leads': leads, 'typed': typed, 'senders': senders,
            'signatures': sigs}


def junk_plan():
    """One action per junk account: rename, merge, or leave.

    Merging moves every lead, contact and quote onto another
    account, and renaming changes what the masking redacts, so each
    row has to carry the evidence it was decided on. Where the
    evidence does not reach, the action is "leave" — an account with
    a silly name and no leads costs nothing, and a wrong merge costs
    a client's history.
    """
    junk = domain_label_accounts()
    junk_ids = {c.id for c, _w, _o, _a in junk}

    # Everything that is not itself junk, indexed for lookup.
    others, by_domain, by_name = [], defaultdict(list), {}
    for c in Company.query.filter(Company.is_active.isnot(False)).all():
        if c.id in junk_ids:
            continue
        others.append(c)
        by_name.setdefault(company_match.norm(c.name), c)
        for d in (c.email_domains or []):
            reg = registrable(d)
            if reg:
                by_domain[reg].append(c)

    rows = []
    for c, why, open_n, all_n in junk:
        ev = junk_evidence(c)
        alias = next(iter(c.aliases or []), None)
        regs = {registrable(d) for d in ev['senders']} | {
            registrable(d) for d in (c.email_domains or [])}
        regs.discard('')

        # Nothing hangs off it, so there is nothing to merge and no
        # evidence to rename it from. A silly name with no leads
        # costs nothing; a wrong guess written into the master data
        # costs somebody an afternoon.
        if not all_n:
            rows.append({'account': c, 'why': why, 'open': open_n,
                         'leads': all_n, 'action': 'leave', 'to': '',
                         'reason': 'no leads, so nothing says what it '
                                   'should be called',
                         'evidence': ev, 'target': None})
            continue

        target = None
        for reg in sorted(regs):
            for cand in by_domain.get(reg, []):
                target = cand
                break
            if target:
                break

        typed_name = None
        for name, _n in ev['typed'].most_common():
            if ff_privacy.variants(name):
                typed_name = name
                break
        if typed_name and not target:
            hit = by_name.get(company_match.norm(typed_name))
            if hit is not None:
                target = hit

        # A signature with a legal form at the end of it is a
        # company naming itself. It beats the domain label, which is
        # only ever a guess: tlog.sbs-group.com gives "Sbs Group"
        # while the mail is signed Toshiba Logistics India Pvt Ltd.
        signed = []
        for name, _k in ev['signatures'].most_common():
            if not SIG_LEGAL.search(name):
                continue
            if any(company_match.norm(name) == company_match.norm(x)
                   for x in signed):
                continue
            signed.append(name)

        if target is not None:
            action, to, reason = ('merge', target.name,
                                  f'another account already holds '
                                  f'{sorted(regs)[0] if regs else "this domain"}')
        elif alias:
            action, to, reason = ('rename', alias,
                                  'the alias already researched for '
                                  'this account')
        elif typed_name:
            action, to, reason = ('rename', typed_name,
                                  f'what the sender typed on '
                                  f'{ev["typed"][typed_name]} lead(s)')
        elif len(signed) == 1:
            action, to, reason = ('rename', signed[0],
                                  'the only company that signs the '
                                  'mail on this account')
        elif len(signed) > 1:
            action, to, reason = ('leave', '',
                                  f'{len(signed)} different companies '
                                  f'sign the mail here — a person has '
                                  f'to say which is the client')
        else:
            label = None
            for reg in sorted(regs):
                if reg in MAIL_PLATFORMS:
                    continue
                cand = _name_from_domain(reg)
                if cand and ff_privacy.variants(cand) and \
                        company_match.norm(cand) != \
                        company_match.norm(c.name):
                    label = cand
                    break
            if label:
                action, to, reason = ('rename', label,
                                      f'the company part of '
                                      f'{sorted(regs)[0]}')
            else:
                action, to, reason = ('leave', '',
                                      'the domain belongs to a mail '
                                      'platform, not a company'
                                      if regs & MAIL_PLATFORMS
                                      else 'no lead, name or domain '
                                           'says what it should be')
        row = {'account': c, 'why': why, 'open': open_n,
               'leads': all_n, 'action': action, 'to': to,
               'reason': reason, 'evidence': ev,
               'target': target if action == 'merge' else None}
        rows.append(_decided(row))
    return rows


def _decided(row):
    """A person's decision replaces the rule's, and says so."""
    d = DECISIONS.get(row['account'].id)
    if not d:
        return row
    row['action'] = d['action']
    row['decision'] = d
    row['reason'] = f'{d["why"]} (decided by hand)'
    if d['action'] == 'merge':
        row['target'] = db.session.get(Company, d['into'])
        row['to'] = (row['target'].name if row['target']
                     else f'#{d["into"]} — MISSING')
    elif d['action'] == 'rename':
        row['to'] = d['to']
        row['target'] = None
    else:
        row['to'] = ''
        row['target'] = None
    return row


def apply_junk(rows, *, actor='FF-LINK-01'):
    """Carry out the renames and merges. Caller commits."""
    from app.services import audit

    renamed, merged, retired = [], [], []
    for r in rows:
        c, to = r['account'], r['to']
        d = r.get('decision') or {}
        if r['action'] == 'retire':
            # Not a client and never was. The lead closes with its
            # tasks, the account stops appearing in the live lists,
            # and nothing is renamed to a name it never had.
            from app.services import task_autoclose
            before = c.dev_stage
            c.dev_stage = d.get('dev_stage', 'Dormant')
            audit.record('company.retired', 'company', str(c.id),
                         old={'dev_stage': before},
                         new={'dev_stage': c.dev_stage},
                         actor=actor, reason=d.get('why', ''))
            closed = 0
            for l in Lead.query.filter(Lead.company_id == c.id).all():
                if (l.stage or '') in STAGES_TERMINAL:
                    continue
                was = l.stage
                l.stage = d.get('lead_stage', 'Lost')
                l.lost_reason = d.get('lost_reason')
                audit.record('lead.stage_change', 'lead', str(l.id),
                             old={'stage': was},
                             new={'stage': l.stage,
                                  'lost_reason': l.lost_reason},
                             actor=actor, reason=d.get('why', ''))
                task_autoclose.close_for_lead(l, actor)
                closed += 1
            retired.append((c, closed))
            continue
        if r['action'] == 'rename':
            old = c.name
            if company_match.norm(old) != company_match.norm(to):
                c.name = to[:200]
                audit.record('company.renamed', 'company', str(c.id),
                             old={'name': old}, new={'name': c.name},
                             actor=actor,
                             reason=f'"{old}" was {r["why"]}; renamed '
                                    f'from {r["reason"]}.')
                renamed.append((c, old, c.name))
            if d.get('alias'):
                add_alias(c, d['alias'], 'the other name it trades '
                                         'under', actor=actor)
        elif r['action'] == 'merge':
            target = r['target']
            if target is None or target.id == c.id:
                continue
            moved = 0
            for l in Lead.query.filter(Lead.company_id == c.id).all():
                l.company_id = target.id
                moved += 1
                audit.record('lead.account_linked', 'lead', str(l.id),
                             old={'company_id': c.id, 'account': c.name},
                             new={'company_id': target.id,
                                  'account': target.name},
                             actor=actor,
                             reason=f'"{c.name}" was {r["why"]}; '
                                    f'merged into {target.name}.')
            # Deactivated, not deleted: the id is referenced from
            # audit rows that have to keep resolving.
            c.is_active = False
            domains = list(target.email_domains or [])
            for dom in (c.email_domains or []):
                if dom not in domains:
                    domains.append(dom)
            for extra in d.get('add_domains', []):
                if extra not in domains:
                    domains.append(extra)
            target.email_domains = domains
            audit.record('company.merged', 'company', str(c.id),
                         old={'name': c.name, 'is_active': True},
                         new={'merged_into': target.id,
                              'account': target.name,
                              'leads_moved': moved,
                              'domains': domains},
                         actor=actor,
                         reason=f'"{c.name}" was {r["why"]}; '
                                f'{r["reason"]}.')
            merged.append((c, target, moved))
            # The target can be as badly named as the thing merged
            # into it — "Hlag" is no better than "Sales".
            if d.get('then_rename') and company_match.norm(
                    target.name) != company_match.norm(
                        d['then_rename']):
                was = target.name
                target.name = d['then_rename'][:200]
                audit.record('company.renamed', 'company',
                             str(target.id),
                             old={'name': was},
                             new={'name': target.name},
                             actor=actor,
                             reason=f'{r["reason"]} — the account '
                                    f'merged into was named no better.')
                renamed.append((target, was, target.name))
    return renamed, merged, retired


# ── 5. evidence for the four short names ─────────────────────────────
def _signature_names(body):
    """Lines in a mail body that look like a company name."""
    found = []
    for raw in (body or '').splitlines():
        line = re.sub(r'<[^>]+>', ' ', raw).strip(' \t|*-–—')
        if not (4 <= len(line) <= 90):
            continue
        if '@' in line or 'http' in line.lower():
            continue
        if SIG_SUFFIX.search(line):
            found.append(' '.join(line.split()))
    return found


def short_name_evidence(accounts=None):
    """What the mail says these companies are actually called."""
    short = accounts or [c for c in Company.query.all()
                         if not ff_privacy.variants(c.name)]
    out = []
    for c in short:
        leads = Lead.query.filter(Lead.company_id == c.id).all()
        domains = Counter()
        sigs = Counter()
        for l in leads:
            for addr in (l.email, l.original_email_from):
                d = account_match.domain_of(
                    account_match.normalise_address(addr))
                if d and not account_match.is_free_mail(d):
                    domains[d] += 1
            for name in _signature_names(l.original_email_body):
                sigs[name] += 1
        out.append({'account': c, 'leads': len(leads),
                    'domains': domains, 'signatures': sigs,
                    'declared_domains': list(c.email_domains or [])})
    return out


#: Words that carry no signal in an initialism. "Zenith (Tianjin)
#: International Logistics" is ZTI to its own staff, so the legal
#: form goes and everything else stays.
LEGAL_FORMS = {
    'pvt', 'private', 'ltd', 'limited', 'llc', 'llp', 'inc', 'co',
    'corp', 'company', 'corporation', 'gmbh', 'srl', 'bv', 'nv',
    'plc', 'ag', 'sa', 'spa', 'as', 'oy', 'ab', 'kk', 'pte', 'sdn',
    'bhd', 'incorporated', 'the', 'of', 'and',
}


def _sig_words(name):
    out = []
    for raw in re.split(r'[^A-Za-z&]+', name or ''):
        if not raw:
            continue
        if raw == '&':
            out.append('&')
        elif raw.lower() not in LEGAL_FORMS:
            out.append(raw)
    return out


def corroborates(account_name, signature_name):
    """Does this signature name belong to *this* account?

    A signature is whatever sat at the bottom of a thread, which is
    very often not the account: #886 Ril's signature is PROCAM's own,
    and #990 Vie's is Siemens Healthcare, the shipper. Writing either
    one in as an alias would tell the masking to redact the wrong
    company and leave the right one exposed — worse than no alias,
    because it would look done.

    So the signature has to account for the name already on the
    record. Returns the rule that fired, or None.
    """
    short = (account_name or '').strip()
    if not short:
        return None
    words = _sig_words(signature_name)
    if not words:
        return None
    if short.lower() in [w.lower() for w in words]:
        return 'the account name is a word in the signature'
    def initials(ws):
        # "Dun & Bradstreet" is DnB to its customers and DB in a
        # filename, so both spellings count.
        yield ''.join('n' if w == '&' else w[0] for w in ws)
        yield ''.join(w[0] for w in ws if w != '&')

    if any(i.lower() == short.lower() for i in initials(words)):
        return 'the account name is the initials of the signature'
    # A legal name usually carries more than the initialism does:
    # "Dun & Bradstreet Information Services India Pvt Ltd" is DnB
    # from its first three words. A leading run counts, but only for
    # three letters or more — two initials off the front of any two
    # words is a coincidence waiting to happen.
    if len(short) >= 3:
        for n in range(2, len(words)):
            if any(i.lower() == short.lower()
                   for i in initials(words[:n])):
                return ('the account name is the initials the '
                        'signature opens with')
    return None


def alias_plan(rows=None):
    """[(account, name, rule, n_seen)] and the ones with no answer."""
    take, leave = [], []
    for row in (rows if rows is not None else short_name_evidence()):
        c = row['account']
        if c.name in (c.aliases or []):
            pass
        hits = []
        for name, n in row['signatures'].most_common():
            rule = corroborates(c.name, name)
            if rule:
                hits.append((name, rule, n))
        if hits:
            # The longest corroborated name is the legal one:
            # "EXPO FREIGHT PRIVATE LIMITED (EFL GLOBAL - INDIA)"
            # over "EFL GLOBAL".
            name, rule, n = max(hits, key=lambda h: len(h[0]))
            take.append((c, name, rule, n))
        else:
            leave.append(row)
    return take, leave


def add_alias(c, name, why, *, actor='FF-LINK-01'):
    """One alias, once. Returns True if it was new."""
    from app.services import audit

    existing = list(c.aliases or [])
    if any(company_match.norm(a) == company_match.norm(name)
           for a in existing):
        return False
    c.aliases = existing + [name]
    audit.record('company.alias_added', 'company', str(c.id),
                 old={'aliases': existing},
                 new={'aliases': c.aliases},
                 actor=actor,
                 reason=f'"{c.name}" is too short for the vendor '
                        f'masking to redact; {why}.')
    return True


def apply_aliases(take, *, actor='FF-LINK-01'):
    """Write the corroborated names. Caller commits."""
    return [(c, name) for c, name, rule, _n in take
            if add_alias(c, name, rule, actor=actor)]


def set_aliases(pairs, *, actor='FF-LINK-01'):
    """Write names a person supplied. Caller commits.

    No corroboration: that rule exists to stop the script guessing
    from a mail signature, and a name typed by the person who owns
    the data is not a guess. The audit reason says which it was, so
    the two can be told apart later — "Ril" is Reliance because
    somebody knew, not because anything in the mail said so.
    """
    written, missing = [], []
    for raw in pairs:
        if '=' not in raw:
            raise SystemExit(f'--set-alias wants ID=NAME, got {raw!r}')
        sid, name = raw.split('=', 1)
        name = name.strip()
        try:
            c = db.session.get(Company, int(sid.strip()))
        except ValueError:
            raise SystemExit(f'--set-alias wants a numeric id, '
                             f'got {sid!r}')
        if c is None:
            missing.append((sid.strip(), name))
            continue
        if not name:
            raise SystemExit(f'--set-alias got an empty name for #{sid}')
        if add_alias(c, name, 'supplied by hand', actor=actor):
            written.append((c, name))
    return written, missing


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--apply-aliases', dest='apply_aliases',
                    action='store_true')
    ap.add_argument('--set-alias', dest='set_alias', action='append',
                    metavar='ID=NAME',
                    help='write an alias by hand, repeatable')
    ap.add_argument('--actor', default='FF-LINK-01')
    ap.add_argument('--propose-junk', dest='propose_junk',
                    action='store_true',
                    help='part 4 as one action per account, read-only')
    ap.add_argument('--apply-junk', dest='apply_junk',
                    action='store_true',
                    help='carry out those renames and merges')
    args = ap.parse_args()
    writes = [a for a in (args.apply, args.apply_aliases,
                          args.set_alias, args.apply_junk) if a]
    if len(writes) + bool(args.check) + bool(args.propose_junk) != 1:
        raise SystemExit(
            'Choose exactly one: --check, --propose-junk, --apply, '
            '--apply-aliases, --set-alias or --apply-junk.')

    with app.app_context():
        print(f'database: {db.engine.url}')
        mode = ('APPLY (part 3 only)' if args.apply
                else 'APPLY (aliases only)' if args.apply_aliases
                else 'APPLY (aliases by hand)' if args.set_alias
                else 'APPLY (renames and merges)' if args.apply_junk
                else 'DRY RUN — nothing is written')
        print(f'MODE:     {mode}\n')

        if args.propose_junk or args.apply_junk:
            rows = junk_plan()
            counts = Counter(r['action'] for r in rows)
            print(f'=== one action per junk account ({len(rows)}) — '
                  f'{dict(counts)}\n')
            for r in rows:
                c, ev = r['account'], r['evidence']
                arrow = f' -> {r["to"]}' if r['to'] else ''
                print(f'  #{c.id:<6} {(c.name or "")[:16]:<16} '
                      f'{r["open"]:>2} open / {r["leads"]:>2} leads   '
                      f'{r["action"].upper()}{arrow}')
                print(f'          because {r["reason"]}')
                typed = ', '.join(f'{n} (x{k})' for n, k
                                  in ev['typed'].most_common(3))
                print(f'          typed by senders: {typed or "nothing"}')
                send = ', '.join(f'{d} (x{k})' for d, k
                                 in ev['senders'].most_common(3))
                print(f'          sender domains:   {send or "none"}')
                if ev['signatures']:
                    print('          signatures:       '
                          + '; '.join(n for n, _ in
                                      ev['signatures'].most_common(2)))
                print()

            if args.propose_junk:
                print('DRY RUN — nothing written. `--apply-junk` '
                      'carries these out: a rename changes the name '
                      'only; a merge moves the leads, folds the '
                      'domains in and deactivates the old account '
                      'rather than deleting it, so audit rows still '
                      'resolve.')
                return

            renamed, merged, retired = apply_junk(
                rows, actor=args.actor)
            try:
                db.session.commit()
            except Exception as exc:                      # noqa: BLE001
                db.session.rollback()
                print(f'  !! ROLLED BACK: {type(exc).__name__}: '
                      f'{str(exc)[:200]}')
                raise SystemExit(1)
            print(f'  {len(renamed)} renamed, {len(merged)} merged, '
                  f'{len(retired)} retired, each with an audit row '
                  f'naming {args.actor}.')
            for c, old_name, new_name in renamed:
                print(f'    #{c.id:<6} {old_name} -> {new_name}')
            for c, target, moved in merged:
                print(f'    #{c.id:<6} {c.name} -> #{target.id} '
                      f'{target.name} ({moved} lead(s) moved)')
            for c, closed in retired:
                print(f'    #{c.id:<6} {c.name} -> {c.dev_stage} '
                      f'({closed} lead(s) closed)')
            return

        if args.set_alias:
            # Straight to the point: the surveys below take a while
            # and say nothing about a name somebody already knows.
            written, missing = set_aliases(args.set_alias,
                                           actor=args.actor)
            try:
                db.session.commit()
            except Exception as exc:                      # noqa: BLE001
                db.session.rollback()
                print(f'  !! ROLLED BACK: {type(exc).__name__}: '
                      f'{str(exc)[:200]}')
                raise SystemExit(1)
            print(f'  {len(written)} alias(es) written, each with an '
                  f'audit row naming {args.actor}.')
            for c, name in written:
                print(f'    #{c.id:<6} {c.name:<8} -> {name}')
            already = (len(args.set_alias) - len(written)
                       - len(missing))
            if already:
                print(f'  {already} already had that alias.')
            if missing:
                print(f'\n  !! {len(missing)} account(s) do not exist:')
                for sid, name in missing:
                    print(f'    #{sid} ({name})')
                raise SystemExit(1)
            for c, _name in written:
                if ff_privacy.variants(c.name):
                    continue
                print(f'  #{c.id} {c.name}: the masking still cannot '
                      f'redact the account name itself — the alias is '
                      f'what protects it.')
            return

        # ── 3 ────────────────────────────────────────────────────────
        plan = unmatched_plan()
        print(f'=== 3. accounts to create for unmatched FF leads '
              f'({len(plan)})')
        print('  by rule:', dict(Counter(r[2] for r in plan)))
        warned = [r for r in plan if r[3]]
        for lead, name, rule, warning in plan:
            print(f'    #{lead.id:<6} {(lead.company or "(no name)")[:30]:<30} '
                  f'{(lead.email or "")[:30]:<30}')
            print(f'            -> {name or "(cannot name it)":<34} '
                  f'{rule}')
            if warning:
                print(f'            !  {warning}')
        if warned:
            print(f'\n  {len(warned)} of {len(plan)} carry a warning.')

        # ── 4 ────────────────────────────────────────────────────────
        junk = domain_label_accounts()
        print(f'\n=== 4. accounts named after a domain label '
              f'({len(junk)}) — nothing is changed')
        print(f'    {"id":<7}{"name":<30}{"open":>6}{"leads":>7}  why')
        for c, why, open_n, all_n in junk:
            print(f'    {c.id:<7}{(c.name or "")[:29]:<30}{open_n:>6}'
                  f'{all_n:>7}  {why}')

        # ── 5 ────────────────────────────────────────────────────────
        shorts = short_name_evidence()
        take, leave = alias_plan(shorts)
        print(f'\n=== 5. accounts too short for the masking '
              f'({len(shorts)})')
        for row in shorts:
            c = row['account']
            print(f'\n    #{c.id} {c.name}  — {row["leads"]} lead(s)')
            print(f'      declared domains: '
                  f'{", ".join(row["declared_domains"]) or "none"}')
            if row['domains']:
                print('      sender domains:   '
                      + ', '.join(f'{d} ({n})' for d, n
                                  in row['domains'].most_common(6)))
            else:
                print('      sender domains:   none on file')
            if row['signatures']:
                print('      names in signatures:')
                for name, n in row['signatures'].most_common(5):
                    print(f'         {n:>3}x  {name}')
            else:
                print('      names in signatures: none found')
            chosen = {c.id: (n, r) for c, n, r, _ in take}.get(c.id)
            if chosen:
                print(f'      ALIAS -> {chosen[0]}')
                print(f'              ({chosen[1]})')
            else:
                print('      ALIAS -> none; no signature accounts for '
                      'the name on the record')

        print(f'\n  {len(take)} of {len(shorts)} have a corroborated '
              f'legal name; {len(leave)} need one by hand:')
        for row in leave:
            c = row['account']
            uncorroborated = ', '.join(
                n for n, _ in row['signatures'].most_common(3)) or 'none'
            print(f'    #{c.id:<6} {(c.name or ""):<8} '
                  f'domains: {", ".join(row["declared_domains"]) or "none"}'
                  f'  |  signatures seen: {uncorroborated[:90]}')

        if args.check:
            print('\nDRY RUN — nothing written. `--apply` creates the '
                  'part 3 accounts and links their leads; '
                  '`--apply-aliases` writes the part 5 aliases; '
                  'part 4 never writes.')
            return

        if args.apply_aliases:
            written = apply_aliases(take)
            try:
                db.session.commit()
            except Exception as exc:                      # noqa: BLE001
                db.session.rollback()
                print(f'\n  !! ROLLED BACK: {type(exc).__name__}: '
                      f'{str(exc)[:200]}')
                raise SystemExit(1)
            print(f'\n  {len(written)} alias(es) written, each with an '
                  f'audit row.')
            for c, name in written:
                print(f'    #{c.id:<6} {c.name:<8} -> {name}')
            print(f'  {len(leave)} account(s) still need a name by hand.')
            print('  Parts 3 and 4 changed nothing.')
            return

        made, skipped = create_accounts(plan)
        try:
            db.session.commit()
        except Exception as exc:                          # noqa: BLE001
            db.session.rollback()
            print(f'\n  !! ROLLED BACK: {type(exc).__name__}: '
                  f'{str(exc)[:200]}')
            raise SystemExit(1)
        print(f'\n  {len(made)} account(s) created and their leads '
              f'linked, in one transaction, each with an audit row.')
        for acct, rule in made[:20]:
            print(f'    #{acct.id:<6} {acct.name}  ({rule})')
        if skipped:
            print(f'\n  {len(skipped)} lead(s) left unlinked:')
            for lead, why in skipped:
                print(f'    #{lead.id}: {why}')
        print('\n  Parts 4 and 5 changed nothing.')


if __name__ == '__main__':
    main()
