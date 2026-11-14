"""Leads filed under the portal that delivered them, not the buyer.

CRM-UAT-01 item 1, from the UAT of 14 November on live. Leads #12642,
#12644 (RFX-56172, ArcelorMittal Nippon Steel India) and #12645
(RFQ-112165) carry the account **"Procol"** — which is not a company.
The parser reads a company out of the sender's domain, and on a
procurement portal's mail the sender is the portal, so the buyer of
each RFQ became the robot that delivered it and three different
buyers' enquiries pooled under one row.

The intake no longer does this: the buyer read out of the body wins,
and `account_match.not_a_customer` refuses a portal-named account, so
nothing can be linked to one again. This moves the leads that were
already filed that way.

    .venv/bin/python scripts/2026_11_14_portal_accounts.py --check
    .venv/bin/python scripts/2026_11_14_portal_accounts.py --check --csv
    .venv/bin/python scripts/2026_11_14_portal_accounts.py --apply --actor PCM001
    .venv/bin/python scripts/2026_11_14_portal_accounts.py --undo --batch PACC-20261114-1

WHERE EACH LEAD GOES

The buyer is read from the mail the lead was made from, by the same
`portal_rfq.describe` the intake uses — the buyer-organisation line,
the event owner, "X has invited you", "invited … by X".

  * **the buyer's existing account**, where a name matches one. Asked
    through `company_match.norm`, so "Arcelormittal Nippon Steel
    India Ltd." and "ArcelorMittal Nippon Steel India" are one
    customer.
  * **a new account**, where the buyer is named and no account
    answers to it. Name and vertical only: NO email domain, ever. The
    sender is `no-reply@procol.in`, and writing that domain onto a
    buyer's account would make the portal's next RFQ -- for a
    different buyer -- link straight to this one.
  * **"To review"**, with no account, where the body names no buyer.
    That is the account-check queue, which is where a lead whose
    customer is unknown is supposed to sit. It is not a loss: the
    mail trail is on the lead and the tender number is on the sheet.

The portal's own account rows are left exactly as they are. Nothing is
deleted and nothing is merged: an account with no leads on it is
harmless, and deciding what to do with a row somebody may have typed
notes on is not a sweep's business. They are listed at the end.

UNDO

Every write carries an audit row with the batch id. `--undo --batch`
puts each lead back on the account it was on and names the accounts
the batch created, which it deactivates rather than deletes -- a lead
somebody has since linked to one of them by hand keeps it, and is
reported.
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import sys
from collections import Counter
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from app import app, db                                    # noqa: E402

ACTOR = 'CRM-UAT-01'
ACTION = 'lead.portal_account_corrected'
UNDONE = 'lead.portal_account_restored'
BATCH_PREFIX = 'PACC'
DEFAULT_CSV = os.path.join(_ROOT, 'data', 'portal_accounts_14NOV.csv')

#: What a lead says when its customer is not known. The same word the
#: intake uses, so the account-check queue holds one spelling.
TO_REVIEW = 'To review'


def _rule(n=72):
    print('─' * n)


# ── finding them ────────────────────────────────────────────────────
def leads_on_a_portal():
    """[(lead, what)] — leads whose account or company is a portal.

    Both ends: the linked account's name, and the free-text company,
    because a lead can carry "Procol" with no account at all and it
    reads the same way on every screen.
    """
    from app import Company, Lead
    from app.services import portal_rfq

    portal_accounts = {}
    for account in Company.query.all():
        if portal_rfq.is_portal_name(account.name):
            portal_accounts[account.id] = account

    out = []
    rows = (Lead.query
            .filter(Lead.is_archived.isnot(True))
            .order_by(Lead.id.asc()).all())
    for lead in rows:
        if lead.company_id and lead.company_id in portal_accounts:
            out.append((lead, f'account "{lead.company}"'))
        elif portal_rfq.is_portal_name(lead.company or ''):
            out.append((lead, f'company "{lead.company}" (no account)'))
    return out, portal_accounts


def buyer_of(lead):
    """(name, how) read out of the mail this lead was made from."""
    from app.services import portal_rfq

    found = portal_rfq.describe(
        lead.original_email_from or '',
        lead.original_email_subject or '',
        lead.original_email_body or '')
    if found is None:
        # Not a portal sender, so the portal name came from somewhere
        # else. The body is still worth asking.
        buyer, how = portal_rfq.buyer_from(
            lead.original_email_subject or '',
            lead.original_email_body or '')
        return buyer, how
    return found['buyer'], found['how']


def account_for(name):
    """An existing account that answers to this name, or None."""
    from app import Company
    from app.services import company_match, portal_rfq

    want = company_match.norm(name or '')
    if not want or len(want) < 3:
        return None
    for account in Company.query.filter(
            Company.is_active.isnot(False)).all():
        if portal_rfq.is_portal_name(account.name):
            continue
        if company_match.norm(account.name or '') == want:
            return account
        for alias in (getattr(account, 'aliases', None) or []):
            if company_match.norm(str(alias)) == want:
                return account
    return None


def plan(found):
    """[(lead, what, verdict, buyer, account, how)].

    verdict: 'link' | 'create' | 'to review'
    """
    out = []
    for lead, what in found:
        buyer, how = buyer_of(lead)
        if not buyer:
            out.append((lead, what, 'to review', '', None, ''))
            continue
        account = account_for(buyer)
        if account is not None:
            out.append((lead, what, 'link', buyer, account, how))
        else:
            out.append((lead, what, 'create', buyer, None, how))
    return out


def report(planned, portal_accounts):
    print(f'\n══ {len(planned)} lead(s) filed under a procurement '
          f'portal ══')
    if not planned:
        print('  none. No lead carries a portal as its customer.')
        return
    counts = Counter(verdict for _l, _w, verdict, _b, _a, _h in planned)
    from app.services import company_match
    new_names = {company_match.norm(buyer)
                 for _l, _w, verdict, buyer, _a, _h in planned
                 if verdict == 'create'}
    for verdict in ('link', 'create', 'to review'):
        if counts.get(verdict):
            label = verdict
            if verdict == 'create':
                label = (f'create — {len(new_names)} new account(s) for '
                         f'{counts[verdict]} lead(s)')
            print(f'  {counts[verdict]:>5}  {label}')

    print(f'\n  {"lead":>7}  {"now":<22} {"becomes":<30} how')
    for lead, what, verdict, buyer, account, how in planned:
        if verdict == 'link':
            becomes = f'#{account.id} {account.name}'
        elif verdict == 'create':
            becomes = f'NEW account "{buyer}"'
        else:
            becomes = f'"{TO_REVIEW}", no account'
        print(f'  {lead.id:>7}  {what[:22]:<22} {becomes[:30]:<30} '
              f'{how or "no buyer in the body"}')
        print(f'  {"":>7}  {(lead.original_email_subject or "")[:62]!r}')
        if lead.portal_key:
            print(f'  {"":>7}  tender {lead.portal_key}')

    if portal_accounts:
        print(f'\n  the portal account row(s), left exactly as they '
              f'are:')
        for account in portal_accounts.values():
            from app import Lead
            held = Lead.query.filter(Lead.company_id == account.id).count()
            print(f'    #{account.id} {account.name!r} — {held} lead(s) '
                  f'on it now')
        print('    Nothing is deleted or merged here. Once the leads '
              'have moved, an empty one can be archived from '
              'Accounts → Merge if somebody wants it gone; the '
              'matcher already refuses to link to it.')


def write_csv(planned, path):
    buf = io.StringIO()
    out = csv.writer(buf)
    out.writerow(['lead_id', 'now', 'verdict', 'buyer', 'account_id',
                  'how', 'tender', 'subject'])
    for lead, what, verdict, buyer, account, how in planned:
        out.writerow([lead.id, what, verdict, buyer,
                      account.id if account is not None else '',
                      how, lead.portal_key or '',
                      (lead.original_email_subject or '')[:120]])
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', newline='') as fh:
        fh.write(buf.getvalue())
    print(f'\nCSV: {path}')


# ── writing ─────────────────────────────────────────────────────────
def next_batch_id(now=None):
    from app.models.audit import AuditEvent

    now = now or datetime.utcnow()
    prefix = f'{BATCH_PREFIX}-{now.strftime("%Y%m%d")}-'
    try:
        rows = (AuditEvent.query.filter(AuditEvent.action == ACTION)
                .order_by(AuditEvent.id.desc()).limit(500).all())
    except Exception:                                     # noqa: BLE001
        rows = []
    used = set()
    for row in rows:
        payload = row.new_value if isinstance(row.new_value, dict) else {}
        batch = str(payload.get('batch') or '')
        if batch.startswith(prefix):
            used.add(batch)
    return f'{prefix}{len(used) + 1}'


def _create_account(buyer, lead, actor):
    """A new account for a buyer nobody has on file.

    Name and vertical. **No email domain**: the sender is the
    portal's robot, and writing that domain here would hand this
    account the next buyer the portal sends for.
    """
    from app import Company
    from app.services import audit

    account = Company(name=buyer[:200], is_active=True)
    for column, value in (('vertical', lead.procam_vertical or None),
                          ('created_by', actor)):
        if hasattr(account, column) and value:
            setattr(account, column, value)
    db.session.add(account)
    db.session.flush()
    audit.record('account.created', 'company', account.id,
                 new={'name': account.name, 'from_lead': lead.id,
                      'read_from': 'the body of a portal RFQ'},
                 actor=actor,
                 reason='CRM-UAT-01 item 1: the buyer of a portal RFQ, '
                        'which had been filed under the portal',
                 commit=False)
    return account


def apply(planned, *, batch, actor):
    from app.services import audit, lead_account

    from app.services import company_match

    done = Counter()
    # Two leads can name the same buyer, and creating the account
    # twice would answer one problem with the other: a second row for
    # a customer who now has one. The first lead creates it and the
    # rest of the batch links to it.
    made = {}
    try:
        for lead, _what, verdict, buyer, account, how in planned:
            was = {'company_id': lead.company_id,
                   'company': lead.company}
            if verdict == 'create':
                key = company_match.norm(buyer or '')
                account = made.get(key)
                if account is None:
                    account = _create_account(buyer, lead, actor)
                    made[key] = account
                    done['account created'] += 1
                else:
                    done['linked to an account made in this batch'] += 1
                verdict = 'link'
            if verdict == 'link' and account is not None:
                # `learn=False`, and this is the whole reason the
                # matcher must not be taught here: the sender is
                # `no-reply@procol.in`, and learning it as this
                # buyer's address would send every later portal RFQ
                # -- for any buyer -- to this one account.
                ok, detail = lead_account.link(
                    lead, account, actor=actor, learn=False,
                    reason=(f'CRM-UAT-01 item 1, batch {batch}: the '
                            f'buyer read from the mail, which had '
                            f'been filed under the portal'))
                if not ok:
                    print(f'  #{lead.id}: refused — {detail}')
                    done['refused'] += 1
                    continue
                done['linked'] += 1
            else:
                lead.company_id = None
                lead.company = TO_REVIEW
                if hasattr(lead, 'needs_review'):
                    lead.needs_review = True
                done['to review'] += 1
            lead.updated_at = datetime.utcnow()
            audit.record(ACTION, 'lead', lead.id,
                         old=was,
                         new={'company_id': lead.company_id,
                              'company': lead.company,
                              'buyer_read_from': how or '',
                              'batch': batch},
                         actor=actor,
                         reason=(f'batch {batch}: a procurement portal '
                                 f'is not a customer'),
                         commit=False)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    print()
    for what, n in done.most_common():
        print(f'  {n:>5}  {what}')
    print(f'\nBatch {batch}. Undo: --undo --batch {batch}')
    return done


def undo(batch):
    from app import Company, Lead
    from app.models.audit import AuditEvent
    from app.services import audit

    rows = (AuditEvent.query
            .filter(AuditEvent.action == ACTION)
            .order_by(AuditEvent.id.asc()).all())
    mine = []
    for row in rows:
        payload = row.new_value if isinstance(row.new_value, dict) else {}
        if str(payload.get('batch') or '') == batch:
            mine.append((row, payload))
    if not mine:
        print(f'\nno batch {batch} on file.')
        return 0

    back, left, touched = 0, 0, set()
    for row, payload in mine:
        lead = db.session.get(Lead, int(row.entity_id))
        if lead is None:
            continue
        if (lead.company_id or 0) != (payload.get('company_id') or 0):
            left += 1
            print(f'  #{lead.id}: its account has changed since the '
                  f'batch — left alone')
            continue
        old = row.old_value if isinstance(row.old_value, dict) else {}
        if payload.get('company_id'):
            touched.add(int(payload['company_id']))
        lead.company_id = old.get('company_id')
        lead.company = old.get('company')
        audit.record(UNDONE, 'lead', lead.id,
                     old={'company_id': payload.get('company_id')},
                     new={'company_id': lead.company_id,
                          'company': lead.company, 'batch': batch},
                     actor=ACTOR, reason=f'undo of batch {batch}',
                     commit=False)
        back += 1

    # Accounts this batch created, deactivated rather than deleted --
    # and only where nothing is left on them.
    made = 0
    for row in (AuditEvent.query
                .filter(AuditEvent.action == 'account.created')
                .order_by(AuditEvent.id.asc()).all()):
        payload = row.new_value if isinstance(row.new_value, dict) else {}
        if payload.get('read_from') != 'the body of a portal RFQ':
            continue
        try:
            account_id = int(row.entity_id)
        except (TypeError, ValueError):
            continue
        if account_id not in touched:
            continue
        held = Lead.query.filter(Lead.company_id == account_id).count()
        account = db.session.get(Company, account_id)
        if account is None:
            continue
        if held:
            print(f'  account #{account_id} {account.name!r} still has '
                  f'{held} lead(s) — left active')
            continue
        account.is_active = False
        audit.record('account.deactivated', 'company', account_id,
                     new={'batch': batch}, actor=ACTOR,
                     reason=(f'undo of batch {batch}: created for a '
                             f'portal RFQ buyer and now empty. '
                             f'Deactivated, never deleted.'),
                     commit=False)
        made += 1
    db.session.commit()
    print(f'\n{back} lead(s) put back; {left} left alone; '
          f'{made} account(s) this batch created deactivated.')
    return back


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--check', action='store_true',
                    help='report only. The default.')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--undo', action='store_true')
    ap.add_argument('--batch', default=None, metavar='ID')
    ap.add_argument('--actor', default=None, metavar='EMP_CODE',
                    help='who is running it — required for --apply')
    ap.add_argument('--csv', nargs='?', const=DEFAULT_CSV, default=None,
                    metavar='PATH')
    args = ap.parse_args(argv)

    with app.app_context():
        print(f'database: {db.engine.url}')
        print('CRM-UAT-01 item 1 — a procurement portal is not a '
              'customer.'
              + ('' if (args.apply or args.undo) else ' REPORT ONLY.'))
        _rule()

        if args.undo:
            if not args.batch:
                print('\n--undo needs --batch PACC-YYYYMMDD-n')
                return 2
            undo(args.batch)
            return 0

        found, portal_accounts = leads_on_a_portal()
        planned = plan(found)
        report(planned, portal_accounts)
        if args.csv:
            write_csv(planned, args.csv)

        if not args.apply:
            print(f'\nDRY RUN — nothing written. --apply --actor '
                  f'<EMP_CODE> moves the {len(planned)} lead(s) above.')
            db.session.rollback()
            return 0
        if not args.actor:
            print('\n--apply needs --actor <EMP_CODE>: an audit row '
                  'with nobody on it is half a record.')
            return 2
        apply(planned, batch=next_batch_id(),
              actor=args.actor.strip().upper())
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
