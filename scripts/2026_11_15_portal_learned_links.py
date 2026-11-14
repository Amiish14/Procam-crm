"""Learned senders that are procurement portals, not customers.

15 Nov, from the Chrome clean-up. Linking the ArcelorMittal leads to
"Amns" from the lead sheet taught the matcher two rules:

    email   no-reply@procol.in  → Amns
    domain  procol.in           → Amns

Those are the two strongest rules it has. Every later RFQ that portal
delivered -- for **any** buyer -- would have gone straight to Amns,
silently, with "learned domain" as the reason on the lead. The rule
`not_a_customer` refuses to LINK a lead to a portal's account; nothing
refused to LEARN a portal's address as a customer's.

`account_match.learn` now refuses, at the one function all four
teaching paths go through. This removes the rows already written.

    .venv/bin/python scripts/2026_11_15_portal_learned_links.py --check
    .venv/bin/python scripts/2026_11_15_portal_learned_links.py --check --csv
    .venv/bin/python scripts/2026_11_15_portal_learned_links.py --apply --actor DIR12010
    .venv/bin/python scripts/2026_11_15_portal_learned_links.py --undo --batch PLRN-20261115-1

WHICH ROWS

Every `sender_account_link` whose value is a portal address or a
portal domain, by `portal_rfq.is_portal_sender` -- the listed portals
and the portal brands, so `superprocure.in` and `procurengine.com`
count whether or not the intake has been told about them.

DELETED, AND THIS IS THE EXCEPTION TO "NEVER HARD-DELETE"

A learned link is not a record of anything that happened; it is a
rule the CRM applies to future mail, and leaving a wrong rule in place
marked "inactive" would either keep applying it or require every
reader to know about the flag. So the row goes -- and the audit row
that replaces it carries the kind, the value, the account and who
taught it, which is everything the row held. `--undo --batch` writes
it back from that audit row.

The leads these rules have already been applied to are NOT changed.
Where a lead is on the wrong account because of one, it is
`scripts/2026_11_14_portal_accounts.py` that moves it, and this says
how many leads each rule has been used for so somebody can judge.
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

ACTOR = 'CRM-PORTAL-LEARN'
ACTION = 'assignment.link_removed'
RESTORED = 'assignment.link_restored'
BATCH_PREFIX = 'PLRN'
DEFAULT_CSV = os.path.join(_ROOT, 'data', 'portal_learned_15NOV.csv')


def _rule(n=72):
    print('─' * n)


def learned_portals():
    """[(row, portal, account)] — learned links that are portals."""
    from app import Company
    from app.models.assignment import SenderAccountLink
    from app.services import portal_rfq

    out = []
    for row in SenderAccountLink.query.order_by(
            SenderAccountLink.id.asc()).all():
        value = (row.value or '').strip().lower()
        if not value:
            continue
        # A domain row holds a bare domain; an email row holds an
        # address. `is_portal_sender` reads the part after the @ when
        # there is one, so a bare domain is asked as "x@domain".
        probe = value if '@' in value else 'x@' + value
        portal = portal_rfq.is_portal_sender(probe)
        if not portal:
            continue
        account = db.session.get(Company, row.account_id)
        out.append((row, portal, account))
    return out


def report(found):
    print(f'\n══ {len(found)} learned sender link(s) that are a '
          f'procurement portal ══')
    if not found:
        print('  none. No portal address or domain is taught to any '
              'account.')
        return
    print(f'\n  {"kind":<7} {"value":<30} {"→ account":<28} used')
    for row, portal, account in found:
        name = (f'#{row.account_id} '
                + ((account.name if account is not None else '(gone)')
                   or ''))
        print(f'  {row.kind:<7} {(row.value or "")[:30]:<30} '
              f'{name[:28]:<28} {row.hits or 0}')
        print(f'  {"":<7} taught by {row.learned_by or "—"}'
              + (f' from lead #{row.learned_from_lead}'
                 if row.learned_from_lead else '')
              + (f'; last used {str(row.last_hit_at)[:16]}'
                 if row.last_hit_at else '; never used')
              + f'; {portal}')
        if row.reason:
            print(f'  {"":<7} reason: {row.reason[:60]}')

    print('\n  Each of these is a rule applied to FUTURE mail: an '
          'email row routes one')
    print('  address, a domain row routes everything that portal '
          'sends, whoever the')
    print('  buyer is. That is why they go rather than being marked '
          'inactive.')

    by_kind = Counter(row.kind for row, _p, _a in found)
    for kind, n in by_kind.most_common():
        print(f'  {n:>5}  {kind}')
    leads = _leads_on_those_accounts(found)
    if leads:
        print(f'\n  The accounts named above carry {leads} open '
              f'lead(s) between them. This script does not touch '
              f'them: scripts/2026_11_14_portal_accounts.py is what '
              f'moves a lead off an account it should not be on.')


def _leads_on_those_accounts(found):
    from app import Lead

    ids = {row.account_id for row, _p, _a in found if row.account_id}
    if not ids:
        return 0
    try:
        return (Lead.query
                .filter(Lead.company_id.in_(ids),
                        Lead.is_archived.isnot(True)).count())
    except Exception:                                     # noqa: BLE001
        return 0


def write_csv(found, path):
    buf = io.StringIO()
    out = csv.writer(buf)
    out.writerow(['kind', 'value', 'portal', 'account_id', 'account',
                  'taught_by', 'from_lead', 'hits', 'last_hit_at',
                  'reason'])
    for row, portal, account in found:
        out.writerow([row.kind, row.value, portal, row.account_id,
                      account.name if account is not None else '',
                      row.learned_by or '', row.learned_from_lead or '',
                      row.hits or 0, str(row.last_hit_at or '')[:19],
                      (row.reason or '')[:120]])
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', newline='') as fh:
        fh.write(buf.getvalue())
    print(f'\nCSV: {path}')


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


def apply(found, *, batch, actor):
    """Remove them, with everything the row held in the audit row."""
    from app.services import audit

    gone = 0
    try:
        for row, portal, account in found:
            audit.record(ACTION, 'company', row.account_id,
                         old={'kind': row.kind, 'value': row.value,
                              'account_id': row.account_id,
                              'learned_by': row.learned_by,
                              'learned_from_lead': row.learned_from_lead,
                              'reason': row.reason,
                              'hits': row.hits or 0},
                         new={'batch': batch, 'portal': portal},
                         actor=actor,
                         reason=(f'batch {batch}: {row.value} is the '
                                 f'{portal} portal, not this '
                                 f'customer\'s address — its next RFQ '
                                 f'is a different buyer'),
                         commit=False)
            db.session.delete(row)
            gone += 1
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    print(f'\n{gone} learned link(s) removed. Batch {batch}.')
    print(f'Undo: --undo --batch {batch}')
    return gone


def undo(batch):
    from app.models.assignment import SenderAccountLink
    from app.models.audit import AuditEvent
    from app.services import audit

    rows = (AuditEvent.query
            .filter(AuditEvent.action == ACTION)
            .order_by(AuditEvent.id.asc()).all())
    mine = []
    for row in rows:
        payload = row.new_value if isinstance(row.new_value, dict) else {}
        if str(payload.get('batch') or '') == batch:
            mine.append(row)
    if not mine:
        print(f'\nno batch {batch} on file.')
        return 0

    back, left = 0, 0
    for row in mine:
        held = row.old_value if isinstance(row.old_value, dict) else {}
        kind, value = held.get('kind'), held.get('value')
        if not kind or not value:
            continue
        if SenderAccountLink.query.filter_by(kind=kind,
                                             value=value).first():
            left += 1
            print(f'  {kind} {value}: something has taught it again '
                  f'— left alone')
            continue
        db.session.add(SenderAccountLink(
            kind=kind, value=value,
            account_id=held.get('account_id'),
            learned_from_lead=held.get('learned_from_lead'),
            learned_by=held.get('learned_by'),
            reason=held.get('reason'),
            hits=held.get('hits') or 0))
        audit.record(RESTORED, 'company', held.get('account_id'),
                     new={'kind': kind, 'value': value,
                          'batch': batch},
                     actor=ACTOR, reason=f'undo of batch {batch}',
                     commit=False)
        back += 1
    db.session.commit()
    print(f'\n{back} link(s) put back; {left} left alone.')
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
        print('learned senders that are procurement portals.'
              + ('' if (args.apply or args.undo) else ' REPORT ONLY.'))
        _rule()

        if args.undo:
            if not args.batch:
                print('\n--undo needs --batch PLRN-YYYYMMDD-n')
                return 2
            undo(args.batch)
            return 0

        found = learned_portals()
        report(found)
        if args.csv:
            write_csv(found, args.csv)

        if not args.apply:
            print(f'\nDRY RUN — nothing removed. --apply --actor '
                  f'<EMP_CODE> removes the {len(found)} link(s) above.')
            db.session.rollback()
            return 0
        if not args.actor:
            print('\n--apply needs --actor <EMP_CODE>: an audit row '
                  'with nobody on it is half a record.')
            return 2
        apply(found, batch=next_batch_id(),
              actor=args.actor.strip().upper())
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
