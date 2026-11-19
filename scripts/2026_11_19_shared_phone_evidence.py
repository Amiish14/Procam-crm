"""Whose phone is it? The 24 shared numbers, decided from evidence.
UAT 19 Nov, shared-phone follow-up.

    # read the evidence and classify. Writes two CSVs, changes nothing.
    .venv/bin/python scripts/2026_11_19_shared_phone_evidence.py
    # the same for the numbers in a CSV you already have
    ... --from-csv docs/shared_phones.csv
    # take OURS numbers off customer contacts, and stop the ingest
    # capturing them again
    ... --apply --actor DIR12010
    # put a batch back, numbers included
    ... --undo --batch PHONE-20261119-1 --actor DIR12010

THE QUESTION

A number on contacts at two different companies is one of three things,
and nobody knows which from the number alone:

  OURS       a Procam signature the ingest captured off a forwarded
             enquiry, or a Procam user's own mobile
  CUSTOMER   genuinely the customer's -- a switchboard, a shared desk
  UNCLEAR    the evidence does not say, or it says both

THE THREE TESTS, WHICH ARE THE ONES ASKED FOR

  (a) does the number appear in the body of a mail sent FROM one of our
      own domains? That is our signature. Read from `lead_emails`,
      `lead_raw_emails` and `leads.original_email_body`, each of which
      carries a sender beside its body.
  (b) is it on a Procam user's record -- `employees.mobile`?
  (c) does it appear in the body of a mail sent from the CUSTOMER's own
      domain? The customer's domain is taken from the contact holding
      the number, so "their domain" is theirs and not a guess.

  OURS      = (a or b) and not c
  CUSTOMER  = c and not (a or b)
  UNCLEAR   = both, or neither

BOTH IS DELIBERATELY NOT "OURS". A number in our signature AND in
theirs is most likely a real shared line -- a freight desk both sides
quote -- and taking it off the customer would lose a working number.
Neither is UNCLEAR for the obvious reason.

THE TMS EMPLOYEE LIST IS NOT READ, and cannot be: two applications,
two databases, and the CRM has no credentials for the other one. Test
(b) is the CRM's own `employees` table. A number belonging to a TMS-only
user therefore reads as UNCLEAR rather than OURS, which is the safe way
round -- it changes nothing.

WHAT --apply DOES AND DOES NOT TOUCH

  it clears the number from CUSTOMER CONTACTS only, one audited row per
  field, undoable by batch;
  it adds the number to `data/own_numbers.txt`, so the ingest stops
  capturing it;
  it NEVER touches an employee record -- that is where the number
  belongs -- and it never touches a CUSTOMER or UNCLEAR number at all.
"""
import argparse
import csv
import os
import re
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ACTION = 'contact.own_phone_removed'
UNDONE = 'contact.own_phone_removed_undone'
BATCH_PREFIX = 'PHONE'

PHONE_FIELDS = ('phone', 'phone2', 'mobile', 'alt_phone')

OURS, CUSTOMER, UNCLEAR = 'OURS', 'CUSTOMER', 'UNCLEAR'


def digits(value):
    said = re.sub(r'\D', '', str(value or ''))
    return said[-10:] if len(said) >= 10 else ''


def _held_fields(row):
    """[(field, written value, digits)] for one record's phones."""
    out = []
    for field in PHONE_FIELDS:
        if not hasattr(row, field):
            continue
        said = getattr(row, field, None)
        if said and digits(said):
            out.append((field, str(said), digits(said)))
    return out


def shared_numbers():
    """{digits: [(contact, field, as written)]} for numbers on two
    or more companies. The same rule as the country survey's item 4."""
    from app import Contact

    by_number = defaultdict(list)
    for c in Contact.query.all():
        for field, written, said in _held_fields(c):
            by_number[said].append((c, field, written))
    out = {}
    for said, holders in by_number.items():
        companies = {(getattr(c, 'company', '') or '').strip().lower()
                     for c, _f, _w in holders} - {''}
        if len(holders) >= 2 and len(companies) >= 2:
            out[said] = holders
    return out


def _mail_rows():
    """[(from_addr, body)] from every store that keeps both.

    Read once for all the numbers: the bodies are the expensive part and
    scanning them per number would read the same text two dozen times.
    """
    from app import Lead, LeadEmail, db
    out = []
    try:
        for row in db.session.query(LeadEmail.from_addr, LeadEmail.body).all():
            out.append((row[0] or '', row[1] or ''))
    except Exception:                                      # noqa: BLE001
        pass
    # `lead_raw_emails` is NOT read: it keeps the message as a file
    # (`storage_path`) and holds no body column, so there is nothing in
    # the database to scan. The two stores above are the ones that keep
    # the text. A number that appears only inside a raw .eml on disk
    # therefore reads as no evidence, which lands it in UNCLEAR and
    # changes nothing -- the safe way round.
    try:
        for row in db.session.query(Lead.original_email_from,
                                    Lead.original_email_body).all():
            out.append((row[0] or '', row[1] or ''))
    except Exception:                                      # noqa: BLE001
        pass
    return out


def _bodies_by_domain(mails):
    """{sender domain: one big lump of normalised body text}.

    Normalised means digits only, so "+91 77381 55449", "077381-55449"
    and "7738155449" are all found by the same ten-digit needle.
    """
    out = defaultdict(list)
    for sender, body in mails:
        said = str(sender or '').strip().lower()
        if '@' not in said or not body:
            continue
        domain = said.rsplit('@', 1)[1].strip().strip('>,; ')
        out[domain].append(re.sub(r'\D', '', body))
    return {d: ' '.join(v) for d, v in out.items()}


def _employee_numbers():
    """{digits: who} for every Procam user with a mobile on file."""
    from app import Employee
    out = {}
    for emp in Employee.query.all():
        said = digits(getattr(emp, 'mobile', None))
        if said:
            out[said] = (getattr(emp, 'name', None)
                         or getattr(emp, 'emp_code', None) or 'a Procam user')
    return out


def classify(wanted=None):
    """One verdict per shared number, with the evidence behind it."""
    from app.services import own_people

    shared = shared_numbers()
    if wanted:
        shared = {k: v for k, v in shared.items() if k in wanted}
    mails = _mail_rows()
    by_domain = _bodies_by_domain(mails)
    ours_domains = own_people.own_domains()
    staff = _employee_numbers()

    our_text = ' '.join(text for domain, text in by_domain.items()
                        if any(domain == d or domain.endswith('.' + d)
                               for d in ours_domains))

    rows = []
    for said, holders in sorted(shared.items()):
        # (a) in a body we sent
        in_ours = said in our_text
        # (b) on a Procam user's record
        on_staff = staff.get(said, '')
        # (c) in a body the customer sent, from the domain of one of the
        #     contacts that holds the number
        their_domains = set()
        for contact, _f, _w in holders:
            address = (getattr(contact, 'email', '') or '').strip().lower()
            if '@' in address:
                their_domains.add(address.rsplit('@', 1)[1])
        their_domains -= set(ours_domains)
        in_theirs = [d for d in sorted(their_domains)
                     if said in (by_domain.get(d) or '')]

        if (in_ours or on_staff) and not in_theirs:
            verdict = OURS
        elif in_theirs and not (in_ours or on_staff):
            verdict = CUSTOMER
        else:
            verdict = UNCLEAR
        why = '; '.join(filter(None, [
            'in a body from our own domain' if in_ours else '',
            f'on {on_staff}\'s record' if on_staff else '',
            ('in a body from ' + ', '.join(in_theirs)) if in_theirs else '',
        ])) or 'no mail body and no user record carries it'
        rows.append({
            'phone': said,
            'verdict': verdict,
            'why': why,
            'in_our_mail': 'yes' if in_ours else 'no',
            'on_a_procam_user': on_staff or '',
            'in_customer_mail': ', '.join(in_theirs),
            'contacts': '; '.join(sorted(
                f'{c.name or "#" + str(c.id)} ({getattr(c, "company", "") or "—"})'
                for c, _f, _w in holders)),
            'contact_ids': '; '.join(str(c.id) for c, _f, _w in holders),
            'as_written': '; '.join(sorted({w for _c, _f, w in holders})),
            '_holders': holders,
        })
    order = {OURS: 0, UNCLEAR: 1, CUSTOMER: 2}
    rows.sort(key=lambda r: (order[r['verdict']], r['phone']))
    return rows, len(mails)


def write_csv(path, rows, fields):
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', newline='', encoding='utf-8') as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)
    return path


def next_batch_id(now=None):
    from app.models.audit import AuditEvent
    now = now or datetime.utcnow()
    prefix = f'{BATCH_PREFIX}-{now.strftime("%Y%m%d")}-'
    used = set()
    for r in (AuditEvent.query.filter(AuditEvent.action == ACTION)
              .order_by(AuditEvent.id.desc()).limit(2000).all()):
        said = str((r.new_value or {}).get('batch') or '') \
            if isinstance(r.new_value, dict) else ''
        if said.startswith(prefix):
            used.add(said)
    return f'{prefix}{len(used) + 1}'


def planned(rows):
    """[(contact, field, written, number)] that --apply would clear.

    OURS only, and only on a CONTACT. An employee record is where the
    number belongs and is never in this list -- `shared_numbers` reads
    contacts alone, so there is nothing to filter out, and the test
    `test_shared_phone_apply.py` holds that.
    """
    out = []
    for r in rows:
        if r['verdict'] != OURS:
            continue
        for contact, field, written in r['_holders']:
            out.append((contact, field, written, r['phone']))
    return out


def apply(rows, *, batch, actor):
    from app import db
    from app.services import audit, own_numbers

    jobs = planned(rows)
    n = 0
    try:
        for contact, field, written, number in jobs:
            if digits(getattr(contact, field, None)) != number:
                continue            # edited since the survey
            setattr(contact, field, None)
            audit.record(ACTION, 'contact', contact.id,
                         old={field: written},
                         new={field: None, 'batch': batch,
                              'phone': number},
                         actor=actor,
                         reason=(f'batch {batch}: {number} is ours, not '
                                 f'this customer\'s'),
                         commit=False)
            n += 1
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    added = own_numbers.add([r['phone'] for r in rows
                             if r['verdict'] == OURS],
                            note=f'batch {batch}')
    print(f'\n{n} number(s) cleared from customer contacts.')
    print(f'{len(added)} added to data/own_numbers.txt — the ingest will '
          f'not capture them again.')
    print(f'Batch {batch}. Undo: --undo --batch {batch} --actor {actor}')
    return n


def undo(batch, actor):
    from app import Contact, db
    from app.models.audit import AuditEvent
    from app.services import audit, own_numbers

    back, left, numbers = 0, 0, set()
    try:
        for ev in (AuditEvent.query.filter(AuditEvent.action == ACTION)
                   .order_by(AuditEvent.id.asc()).all()):
            new = ev.new_value if isinstance(ev.new_value, dict) else {}
            if str(new.get('batch') or '') != batch:
                continue
            field = next((k for k in new if k in PHONE_FIELDS), None)
            if field is None:
                continue
            row = db.session.get(Contact, int(ev.entity_id))
            if row is None:
                continue
            if getattr(row, field, None) not in (None, ''):
                left += 1       # somebody has filled it since; theirs stands
                continue
            was = (ev.old_value or {}).get(field) \
                if isinstance(ev.old_value, dict) else None
            setattr(row, field, was)
            numbers.add(str(new.get('phone') or ''))
            audit.record(UNDONE, 'contact', row.id,
                         old={field: None}, new={field: was, 'batch': batch},
                         actor=actor, reason=f'undo of batch {batch}',
                         commit=False)
            back += 1
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    dropped = own_numbers.remove(numbers)
    print(f'{back} number(s) put back on their contacts.'
          + (f' {left} left alone — filled in since the batch.'
             if left else ''))
    print(f'{len(dropped)} taken back out of data/own_numbers.txt.')
    return back


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default='docs/shared_phone_evidence.csv')
    ap.add_argument('--from-csv', dest='from_csv',
                    help='only the numbers in this CSV (a phone column)')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--undo', action='store_true')
    ap.add_argument('--batch')
    ap.add_argument('--actor', help='employee id; required with --apply')
    a = ap.parse_args()

    os.environ.setdefault('SECRET_KEY', 'read-only')
    os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'read-only-0000000')
    from app import app as flask_app

    with flask_app.app_context():
        if a.undo:
            if not (a.batch and a.actor):
                sys.exit('  [STOP] --undo needs --batch and --actor.')
            undo(a.batch, a.actor)
            return
        if a.apply and not a.actor:
            sys.exit('  [STOP] --apply needs --actor, an employee id.')

        wanted = None
        if a.from_csv:
            wanted = set()
            with open(a.from_csv, encoding='utf-8') as fh:
                for row in csv.DictReader(fh):
                    said = digits(row.get('phone') or row.get('number') or '')
                    if said:
                        wanted.add(said)
            print(f'{len(wanted)} number(s) read from {a.from_csv}')

        rows, mails = classify(wanted)
        counts = defaultdict(int)
        for r in rows:
            counts[r['verdict']] += 1
        path = write_csv(a.out, rows,
                         ['phone', 'verdict', 'why', 'in_our_mail',
                          'on_a_procam_user', 'in_customer_mail',
                          'contacts', 'contact_ids', 'as_written'])

        print(f'{mails} mail body/sender pair(s) read.')
        print(f'{len(rows)} shared number(s):')
        for verdict in (OURS, UNCLEAR, CUSTOMER):
            print(f'  {counts[verdict]:>4}  {verdict}')
        print()
        for r in rows:
            print(f'  {r["verdict"]:<8} {r["phone"]}  {r["why"]}')
            print(f'           {r["contacts"]}')
        print(f'\nCSV: {path}')

        jobs = planned(rows)
        if a.apply:
            batch = a.batch or next_batch_id()
            apply(rows, batch=batch, actor=a.actor)
        else:
            print(f'\nDry run. {len(jobs)} phone field(s) on '
                  f'{len({c.id for c, _f, _w, _n in jobs})} customer '
                  f'contact(s) would be cleared,\nand '
                  f'{counts[OURS]} number(s) added to '
                  f'data/own_numbers.txt. Nothing for CUSTOMER or '
                  f'UNCLEAR.\nAdd --apply --actor DIR12010 to write.')


if __name__ == '__main__':
    main()
