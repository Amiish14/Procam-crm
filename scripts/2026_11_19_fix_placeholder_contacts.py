"""Seven contacts filed under a placeholder, two countries, and the
placeholder accounts listed — UAT 19 Nov.

    .venv/bin/python scripts/2026_11_19_fix_placeholder_contacts.py
    .venv/bin/python scripts/2026_11_19_fix_placeholder_contacts.py --csv
    .venv/bin/python scripts/2026_11_19_fix_placeholder_contacts.py --apply --actor DIR12010
    .venv/bin/python scripts/2026_11_19_fix_placeholder_contacts.py --undo --batch PHFIX-20261119-1 --actor DIR12010

PART 1 — THE NAMED FIXES, AND ONLY THEM

Each contact gets the company TEXT and the LINK (`company_id` and
`account_id`) to an existing account. The account is found by name and
alias through the same matcher the CRM uses, then by the contact's
email domain. Exactly one account must answer; if several do, the
contact is reported and left alone. An account is created only when
none exists, and the dry run says which. A placeholder account is never
the answer.

    #91  Joachim Holst          Fried Spedition (fried-sped.de)
    #89  Soumya Sundar Khanra   Luminous Power Technologies
    #226 Nutan Mishra           Rashmi Group
    #88  Mayukh Dam             Siemens Ltd
    #201 Raghavan, Narsimhan    Siemens Healthineers
    #150 Pratap Ch Pradhan      the account of his other record (Vedanta
                                Ltd - Ferro Alloys Business (FACOR))
    #233 Rinku Biswas           Vikram Solar

Country Finland on contact #443 (Jenna Nirho) and on lead #10504.

PART 2 — THE PLACEHOLDER ACCOUNTS, LISTED, NOT MOVED

Every account whose name is a placeholder (`account_match.
PLACEHOLDER_NAMES`: "New Lead", "Unknown", "n/a" …), with every contact
and lead still linked to it and the account its email domain suggests.
Nothing in part 2 is relinked: the seven above are the only moves.
The same name list is what stops mail intake, the lead account picker,
the account gate, the card scanner and the contact form linking to them
(`account_match.not_a_customer`).

Every write carries an audit row with the batch id and the old value;
--undo puts the old values back unless the field was edited since.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from app import app, db                                    # noqa: E402

ACTION = 'contact.placeholder_fixed'
UNDONE = 'contact.placeholder_fix_undone'
BATCH_PREFIX = 'PHFIX'
DEFAULT_CSV = os.path.join(_ROOT, 'data', 'placeholder_accounts_19NOV.csv')

#: (contact id, the company as it should read, its email domain)
TARGETS = (
    (91, 'Fried Spedition', 'fried-sped.de'),
    (89, 'Luminous Power Technologies', 'luminousindia.com'),
    (226, 'Rashmi Group', 'rashmigroup.com'),
    (88, 'Siemens Ltd', 'siemens.com'),
    (201, 'Siemens Healthineers', 'siemens-healthineers.com'),
    (150, None, 'vedanta.co.in'),      # the account of his other record
    (233, 'Vikram Solar', 'vikramsolar.com'),
)
COUNTRY_CONTACTS = ((443, 'Finland'),)
COUNTRY_LEADS = ((10504, 'Finland'),)


def _domain(email):
    text = str(email or '').strip().lower()
    return text.split('@', 1)[1] if '@' in text else ''


def find_account(name, domain=''):
    """(account, how) or (None, why) — one existing, non-placeholder."""
    from app import Company
    from app.services import account_match, company_match

    want = account_match.strict_norm(name or '')
    hits = {}
    for account, norms, stricts, _own in account_match.active_index():
        if account_match.not_a_customer(account):
            continue
        if want and (want in stricts or want in norms):
            hits[account.id] = (account, 'name or alias')
    if len(hits) == 1:
        return next(iter(hits.values()))
    if len(hits) > 1 and domain:
        own = [a for a, _h in hits.values()
               if domain in [str(d).lower().lstrip('@')
                             for d in (a.email_domains or [])]
               or domain in str(a.website or '').lower()]
        if len(own) == 1:
            return own[0], 'name, and the email domain'
    if len(hits) > 1:
        return None, ('several accounts answer to this name: '
                      + ', '.join(f'#{a.id} {a.name}' for a, _ in hits.values()))
    if domain:
        by_domain = [a for a in Company.query.all()
                     if not account_match.not_a_customer(a)
                     and (domain in [str(d).lower().lstrip('@')
                                     for d in (a.email_domains or [])]
                          or (a.website and domain in a.website.lower()))]
        if len(by_domain) == 1:
            return by_domain[0], 'the email domain'
        if len(by_domain) > 1:
            return None, ('several accounts carry this email domain: '
                          + ', '.join(f'#{a.id} {a.name}' for a in by_domain))
    norm = company_match.norm(name or '')
    if norm:
        loose = [a for a in Company.query.filter(
            Company.name.ilike(f'%{norm.split()[0]}%')).limit(20).all()
            if not account_match.not_a_customer(a)
            and company_match.norm(a.name).startswith(norm)]
        if len(loose) == 1:
            return loose[0], 'name, ignoring the legal suffix'
    return None, 'no account'


def plan():
    """[(kind, row, changes {field: (old, new)}, note)]"""
    from app import Contact, Lead

    out = []
    for cid, company, domain in TARGETS:
        contact = db.session.get(Contact, cid)
        if contact is None:
            out.append(('contact', None, {}, f'#{cid}: no such contact'))
            continue
        domain = _domain(contact.email) or domain
        note = ''
        account, how = None, ''
        if company is None:
            # His other record: same name, a real account.
            twins = [c for c in Contact.query.filter(
                db.func.lower(Contact.name) == (contact.name or '').strip().lower(),
                Contact.id != contact.id).all()
                if (c.account_id or c.company_id)]
            from app import Company
            from app.services import account_match
            twins = [c for c in twins
                     if not account_match.not_a_customer(
                         db.session.get(Company, c.account_id or c.company_id))]
            if len(twins) != 1:
                out.append(('contact', contact, {},
                            f'#{cid}: expected one other record with a real '
                            f'account, found {len(twins)} — left alone'))
                continue
            twin = twins[0]
            account = db.session.get(Company, twin.account_id or twin.company_id)
            company, how = account.name, f'his other record #{twin.id}'
        else:
            account, how = find_account(company, domain)
            if account is None and how.startswith('several'):
                out.append(('contact', contact, {}, f'#{cid}: {how} — left alone'))
                continue
            if account is None:
                note = f'no account named "{company}" exists — one will be CREATED'
        new_company = account.name if account is not None else company
        changes = {}
        if (contact.company or '') != new_company:
            changes['company'] = (contact.company, new_company)
        new_id = account.id if account is not None else 'NEW'
        if contact.company_id != new_id:
            changes['company_id'] = (contact.company_id, new_id)
        if contact.account_id != new_id:
            changes['account_id'] = (contact.account_id, new_id)
        out.append(('contact', contact, changes,
                    note or (f'account #{account.id} "{account.name}" by {how}')))
    for cid, country in COUNTRY_CONTACTS:
        contact = db.session.get(Contact, cid)
        if contact is None:
            out.append(('contact', None, {}, f'#{cid}: no such contact'))
            continue
        if (contact.country or '') != country:
            out.append(('contact', contact,
                        {'country': (contact.country, country)}, 'country'))
    for lid, country in COUNTRY_LEADS:
        lead = db.session.get(Lead, lid)
        if lead is None:
            out.append(('lead', None, {}, f'#{lid}: no such lead'))
            continue
        if (lead.country or '') != country:
            out.append(('lead', lead, {'country': (lead.country, country)},
                        'the PIC\'s country'))
    return out


def placeholder_survey():
    """[(account, kind, row, email, suggested)] for every placeholder account."""
    from app import Company, Contact, Lead
    from app.services import account_match

    rows = []
    for account in Company.query.order_by(Company.id).all():
        if not (account.name or '').strip().lower() in account_match.PLACEHOLDER_NAMES:
            continue
        linked = [('contact', c, c.email) for c in Contact.query.filter(
            db.or_(Contact.company_id == account.id,
                   Contact.account_id == account.id)).all()]
        linked += [('lead', l, l.email) for l in Lead.query.filter(
            Lead.company_id == account.id).all()]
        if not linked:
            rows.append((account, '', None, '', ''))
        for kind, row, email in linked:
            suggestion = ''
            domain = _domain(email)
            if domain:
                found, how = find_account(domain.split('.')[0], domain)
                suggestion = (f'#{found.id} {found.name} ({how})' if found
                              else f'none — {how}')
            rows.append((account, kind, row, email or '', suggestion))
    return rows


def report(items, survey):
    print('\n══ Part 1 — the named fixes ══')
    for kind, row, changes, note in items:
        ident = f'{kind} #{row.id}' if row is not None else kind
        name = (getattr(row, 'name', None) or getattr(row, 'pic', None) or '') if row else ''
        print(f'\n  {ident} {name}')
        print(f'      {note}')
        for field, (old, new) in changes.items():
            print(f'      {field}: {old!r} → {new!r}')
        if row is not None and not changes and 'left alone' not in note:
            print('      already as asked — nothing to do')
    print('\n══ Part 2 — placeholder accounts and what is still on them ══')
    seen = None
    for account, kind, row, email, suggestion in survey:
        if account.id != seen:
            print(f'\n  #{account.id} "{account.name}"')
            seen = account.id
        if row is None:
            print('      nothing linked')
            continue
        name = getattr(row, 'name', None) or getattr(row, 'pic', None) or ''
        print(f'      {kind} #{row.id} {name[:28]:<28} {email:<34} → {suggestion}')
    print('\n  Part 2 is a list. Nothing on it is relinked by this script.')


def write_csv(survey, path):
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(['placeholder_account_id', 'placeholder_name', 'record',
                    'id', 'name', 'email', 'suggested_account'])
        for account, kind, row, email, suggestion in survey:
            w.writerow([account.id, account.name, kind,
                        row.id if row is not None else '',
                        (getattr(row, 'name', None) or getattr(row, 'pic', None)
                         or '') if row is not None else '',
                        email, suggestion])
    print(f'\nCSV: {path}')


def next_batch_id(now=None):
    from app.models.audit import AuditEvent

    now = now or datetime.utcnow()
    prefix = f'{BATCH_PREFIX}-{now.strftime("%Y%m%d")}-'
    used = set()
    for ev in AuditEvent.query.filter(AuditEvent.action == ACTION).all():
        b = (ev.new_value or {}).get('batch') if isinstance(ev.new_value, dict) else ''
        if str(b or '').startswith(prefix):
            used.add(b)
    return f'{prefix}{len(used) + 1}'


def apply(items, *, batch, actor):
    from app import Company
    from app.services import audit

    created, written = [], 0
    try:
        for kind, row, changes, note in items:
            if row is None or not changes:
                continue
            new_account = None
            if any(v == 'NEW' for _o, v in changes.values()):
                name = changes['company'][1]
                new_account = Company(name=name, is_active=True,
                                      created_by=actor,
                                      email_domains=[_domain(row.email)]
                                      if _domain(row.email) else [])
                db.session.add(new_account)
                db.session.flush()
                created.append(new_account)
                audit.record('account.created', 'account', new_account.id,
                             new={'name': name, 'batch': batch}, actor=actor,
                             reason=f'batch {batch}: no account existed for '
                                    f'contact #{row.id}', commit=False)
            old, new = {}, {}
            for field, (was, will) in changes.items():
                if will == 'NEW':
                    will = new_account.id
                old[field], new[field] = was, will
                setattr(row, field, will)
            audit.record(ACTION, kind, row.id, old=old,
                         new=dict(new, batch=batch,
                                  created_account=new_account.id
                                  if new_account is not None else None),
                         actor=actor, reason=f'batch {batch}: {note}',
                         commit=False)
            written += 1
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    print(f'\n{written} record(s) changed. Batch {batch}.')
    for a in created:
        print(f'  created account #{a.id} "{a.name}"')
    print(f'Undo: --undo --batch {batch} --actor {actor}')
    return written


def undo(batch, actor):
    from app import Company, Contact, Lead
    from app.models.audit import AuditEvent
    from app.services import audit

    models = {'contact': Contact, 'lead': Lead}
    back, left = 0, 0
    for ev in (AuditEvent.query.filter(AuditEvent.action == ACTION)
               .order_by(AuditEvent.id.desc()).all()):
        new = ev.new_value if isinstance(ev.new_value, dict) else {}
        if new.get('batch') != batch:
            continue
        row = db.session.get(models.get(ev.entity_type), int(ev.entity_id))
        if row is None:
            continue
        old = ev.old_value if isinstance(ev.old_value, dict) else {}
        if any(getattr(row, f) != new.get(f) for f in old):
            left += 1
            continue
        for field, value in old.items():
            setattr(row, field, value)
        if new.get('created_account'):
            account = db.session.get(Company, new['created_account'])
            if account is not None:
                # Never deleted: deactivated, with the reason on it.
                account.is_active = False
        audit.record(UNDONE, ev.entity_type, row.id, old=new, new=old,
                     actor=actor, reason=f'undo of batch {batch}',
                     commit=False)
        back += 1
    db.session.commit()
    print(f'\n{back} record(s) put back; {left} left because they were '
          f'edited after the batch.')
    return back


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--undo', action='store_true')
    ap.add_argument('--batch')
    ap.add_argument('--actor')
    ap.add_argument('--csv', nargs='?', const=DEFAULT_CSV)
    args = ap.parse_args(argv)
    with app.app_context():
        if args.undo:
            if not (args.batch and args.actor):
                ap.error('--undo needs --batch and --actor')
            return undo(args.batch, args.actor)
        items, survey = plan(), placeholder_survey()
        report(items, survey)
        if args.csv:
            write_csv(survey, args.csv)
        if args.apply:
            if not args.actor:
                ap.error('--apply needs --actor')
            return apply(items, batch=next_batch_id(), actor=args.actor)
        print('\nDry run. Nothing written. Add --apply --actor DIR12010.')
    return 0


if __name__ == '__main__':
    main()
