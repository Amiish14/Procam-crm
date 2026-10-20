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
    args = ap.parse_args()
    writes = [a for a in (args.apply, args.apply_aliases,
                          args.set_alias) if a]
    if len(writes) + bool(args.check) != 1:
        raise SystemExit(
            'Choose exactly one: --check, --apply, '
            '--apply-aliases or --set-alias.')

    with app.app_context():
        print(f'database: {db.engine.url}')
        mode = ('APPLY (part 3 only)' if args.apply
                else 'APPLY (aliases only)' if args.apply_aliases
                else 'APPLY (aliases by hand)' if args.set_alias
                else 'DRY RUN — nothing is written')
        print(f'MODE:     {mode}\n')

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
