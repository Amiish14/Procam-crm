"""Leads that were never enquiries: our own notices, and circulars.

CRM-FINAL-13NOV items 10 and 13. Both faults made the same thing:
a lead on somebody's board out of a mail that was never an enquiry.

    .venv/bin/python scripts/2026_11_13_internal_and_promo_leads.py --check
    .venv/bin/python scripts/2026_11_13_internal_and_promo_leads.py --check --csv
    .venv/bin/python scripts/2026_11_13_internal_and_promo_leads.py --apply
    .venv/bin/python scripts/2026_11_13_internal_and_promo_leads.py \
          --undo --batch INTPROMO-20261113-1

WHAT IT LOOKS FOR

    internal      the lead was made from a mail sent by us, which
                  reads as a notice rather than an enquiry, and names
                  no external customer. The tender digest -- "Procam
                  Logistics owner: ... / Current bid status ... /
                  please create / confirm the CRM lead ...", signed
                  "Procam Tech team" -- is the one reported.
    promotional   the lead was made from mail a mailing system sent:
                  List-Unsubscribe, Precedence: bulk, a campaign
                  header, or a sender already marked for it, with
                  nothing in it reading as an enquiry.

`internal_mail` and `bulk_mail` are the same two modules the intake
now asks, so a lead archived here is one that would not be created
again -- which is the only reason to archive it rather than leave it.

WHAT IT DOES TO ONE

Archives it, reversibly, with a reason: `internal` for the notices
and `informational` for the circulars (item 13's own word). Both leave
the boards, the dashboards and the reports; neither counts as Lost;
both are restorable by an administrator, and `lead_archive` writes the
audit row. **No hard delete, and no sender is blocked** -- the address
that sends a circular this week sends a real quotation next week.

It never archives a lead somebody has worked. A quote, an RFQ, a
mail sent from the CRM, a call, a meeting, a note a person wrote, an
opportunity, a merge: any one of them and the lead is reported and
left, because a lead with history is a judgement and not a sweep.
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

from app import Lead, app, db                              # noqa: E402

#: Set from --actor. `lead_archive.archive` asks the same permission
#: the Archive button asks -- the lead's owner, the head of its
#: vertical, or an administrator -- so the sweep has to act AS
#: somebody rather than as a script name. A batch that could archive
#: without anybody's rights would be a way round the one control on
#: this path.
ACTOR = 'CRM-FINAL-13NOV'
ACTION = 'lead.archived'
BATCH_PREFIX = 'INTPROMO'
DEFAULT_CSV = os.path.join(_ROOT, 'data', 'internal_and_promo_13NOV.csv')

#: (kind, the archive category it is filed under).
INTERNAL = ('internal', 'junk')
PROMO = ('promotional', 'junk')


def _rule(n=70):
    print('─' * n)


def _msg_of(lead):
    """The mail this lead was made from, as the classifiers read it.

    Rebuilt from the columns rather than re-fetched from Graph: the
    message may be months old and the question is what the CRM holds.
    Headers are not among them, so a promotional lead is recognised
    here by its SENDER and its wording, with the header evidence named
    only where the ingest log still carries it.
    """
    return {
        'subject': lead.original_email_subject or '',
        'from': {'emailAddress': {
            'address': (lead.original_email_from or lead.email or '')}},
        'body': {'content': lead.original_email_body or ''},
        'internetMessageHeaders': [],
    }


def worked(lead):
    """Why this lead must not be swept, or ''."""
    from app.services import junk_delete

    try:
        blockers = junk_delete.blockers(lead) or []
    except Exception:                                     # noqa: BLE001
        return ''
    # `blockers` answers [(key, sentence)].
    for item in blockers:
        if isinstance(item, (tuple, list)) and len(item) >= 2:
            return str(item[1])
        if isinstance(item, dict):
            return str(item.get('why') or item.get('sentence') or '')
        if item:
            return str(item)
    return ''


def survey():
    from app.services import bulk_mail, internal_mail

    rows = (Lead.query
            .filter(Lead.is_archived.isnot(True),
                    Lead.junk_deleted.isnot(True),
                    Lead.merged_into_id.is_(None))
            .order_by(Lead.id.asc())
            .all())
    found = {'internal': [], 'promotional': [], 'left': []}
    for lead in rows:
        sender = (lead.original_email_from or lead.email or '')
        body = lead.original_email_body or ''
        subject = lead.original_email_subject or ''
        msg = _msg_of(lead)

        kind, why = '', ''
        if internal_mail.from_us(sender):
            phrase = internal_mail.reads_as_a_notice(subject, body)
            if phrase and not internal_mail.carries_an_external_customer(body):
                kind, why = 'internal', f'our own notice: "{phrase}"'
        if not kind:
            promo, promo_why = bulk_mail.classify(msg, body, sender=sender)
            if promo:
                kind, why = 'promotional', promo_why
        if not kind:
            continue

        blocker = worked(lead)
        if blocker:
            found['left'].append((lead, kind, f'{why}; LEFT — {blocker}'))
            continue
        found[kind].append((lead, kind, why))
    return found


def report(found):
    print(f'\n══ leads made from mail that was never an enquiry ══')
    print(f'  {len(found["internal"])} from our own notices (item 10)')
    print(f'  {len(found["promotional"])} from circulars (item 13)')
    print(f'  {len(found["left"])} matched and are LEFT because somebody '
          f'has worked them')

    for kind, label in (('internal', 'our own notices'),
                        ('promotional', 'circulars')):
        rows = found[kind]
        if not rows:
            continue
        print(f'\n  ── {label} ──')
        for lead, _kind, why in rows[:25]:
            print(f'    #{lead.id:<7} {(lead.company or "")[:26]:<26} '
                  f'{(lead.original_email_from or "")[:30]:<30} '
                  f'{(lead.original_email_subject or "")[:34]!r}')
            print(f'    {"":<7} {why[:96]}')
        if len(rows) > 25:
            print(f'    … and {len(rows) - 25} more')
        senders = Counter((l.original_email_from or '(none)').lower()
                          for l, _k, _w in rows)
        print(f'    by sender:')
        for sender, n in senders.most_common(8):
            print(f'      {n:>5}  {sender}')

    if found['left']:
        print('\n  ── matched, and left alone ──')
        for lead, kind, why in found['left'][:15]:
            print(f'    #{lead.id:<7} {kind:<12} {why[:80]}')
        print('    These have a quote, an RFQ, a call, a note or a mail '
              'sent from the CRM on them. A lead with history is a '
              'judgement, not a sweep.')


def write_csv(found, path):
    buf = io.StringIO()
    out = csv.writer(buf)
    out.writerow(['kind', 'lead_id', 'company', 'sender', 'subject',
                  'stage', 'owner', 'created', 'why', 'action'])
    for key in ('internal', 'promotional', 'left'):
        for lead, kind, why in found[key]:
            out.writerow([
                kind, lead.id, (lead.company or '')[:60],
                (lead.original_email_from or lead.email or '')[:70],
                (lead.original_email_subject or '')[:90],
                lead.stage or '', lead.assigned_to or '',
                str(lead.created_at or '')[:10], why[:160],
                'left' if key == 'left' else 'would archive'])
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', newline='') as fh:
        fh.write(buf.getvalue())
    print(f'\nCSV: {path}')
    return path


def next_batch_id(now=None):
    from app.models.audit import AuditEvent

    now = now or datetime.utcnow()
    prefix = f'{BATCH_PREFIX}-{now.strftime("%Y%m%d")}-'
    try:
        rows = (AuditEvent.query
                .filter(AuditEvent.action
                        == 'lead.archived_as_not_an_enquiry')
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


def apply(found, *, batch, actor=None):
    from app.services import audit, lead_archive

    who = actor or ACTOR
    done, refused = Counter(), []
    try:
        for key, (kind, category) in (('internal', INTERNAL),
                                      ('promotional', PROMO)):
            for lead, _kind, why in found[key]:
                try:
                    lead_archive.archive(
                        lead, actor=who, category=category,
                        note=(f'{kind}: {why}')[:170])
                except lead_archive.Refused as refusal:
                    # The same door the Archive button goes through.
                    # Named and left, never forced.
                    refused.append((lead.id, str(refusal)))
                    continue
                audit.record('lead.archived_as_not_an_enquiry', 'lead',
                             lead.id,
                             new={'kind': kind, 'batch': batch,
                                  'why': why[:200]},
                             actor=who,
                             reason=f'batch {batch}: {kind}')
                done[kind] += 1
        db.session.commit()
    except Exception as exc:                              # noqa: BLE001
        db.session.rollback()
        print(f'\n  !! ROLLED BACK: {type(exc).__name__}: {str(exc)[:200]}')
        raise SystemExit(1)
    if refused:
        print(f'\n  {len(refused)} refused by the archive permission:')
        for lead_id, why in refused[:10]:
            print(f'    #{lead_id}: {why}')
        print('    Run --apply --actor <CODE> as somebody who may '
              'archive these: an administrator, or the head of their '
              'vertical.')
    print(f'\narchived: {done["internal"]} internal, '
          f'{done["promotional"]} promotional. Batch {batch}.')
    print('Nothing was deleted, no sender was blocked, and none of them '
          'counts as Lost.')
    print(f'Undo: --undo --batch {batch}')
    return done


def undo(batch, actor=None):
    from app.models.audit import AuditEvent
    from app.services import lead_archive

    _actor = actor

    # Found by the ACTION and the BATCH, not by the actor: `apply`
    # writes the audit row as the person whose rights the archive was
    # taken under, so filtering on this script's own name found
    # nothing and the undo quietly restored none of them.
    rows = (AuditEvent.query
            .filter(AuditEvent.action == 'lead.archived_as_not_an_enquiry')
            .order_by(AuditEvent.id.asc()).all())
    mine = []
    for row in rows:
        payload = row.new_value if isinstance(row.new_value, dict) else {}
        if str(payload.get('batch') or '') == batch:
            mine.append(row)
    if not mine:
        print(f'\nno batch {batch} on file.')
        return 0
    back = 0
    try:
        for row in mine:
            lead = db.session.get(Lead, int(row.entity_id))
            if lead is None or not getattr(lead, 'is_archived', False):
                continue
            try:
                lead_archive.restore(lead, actor=(_actor or ACTOR))
            except lead_archive.Refused as refusal:
                # The restore asks the same permission the Archive
                # screen asks. A script actor is not a person, so if
                # it is refused the lead is named and left rather than
                # forced: Admin -> Archived leads is the way back.
                print(f'  #{lead.id}: {refusal} \u2014 restore it from '
                      f'Admin \u2192 Archived leads')
                continue
            back += 1
        db.session.commit()
    except Exception as exc:                              # noqa: BLE001
        db.session.rollback()
        print(f'\n  !! ROLLED BACK: {type(exc).__name__}: {str(exc)[:200]}')
        raise SystemExit(1)
    print(f'\n{back} lead(s) put back.')
    return back


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--check', action='store_true',
                    help='report only. The default.')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--undo', action='store_true')
    ap.add_argument('--batch', default=None, metavar='ID')
    ap.add_argument('--actor', default=None, metavar='EMP-CODE',
                    help='whose rights the archive is taken under. '
                         'Required for --apply: the archive asks the '
                         'same permission the button asks.')
    ap.add_argument('--csv', nargs='?', const=DEFAULT_CSV, default=None,
                    metavar='PATH')
    args = ap.parse_args(argv)

    with app.app_context():
        print(f'database: {db.engine.url}')
        print('CRM-FINAL-13NOV items 10 and 13 — leads that were never '
              'enquiries.' + ('' if args.apply else ' REPORT ONLY.'))
        _rule()

        if args.undo:
            if not args.batch:
                print('\n--undo needs --batch INTPROMO-YYYYMMDD-n')
                return 2
            undo(args.batch,
                 actor=(args.actor or '').strip().upper() or None)
            return 0

        found = survey()
        report(found)
        if args.csv:
            write_csv(found, args.csv)
        if not args.apply:
            total = len(found['internal']) + len(found['promotional'])
            print(f'\nDRY RUN — nothing written. --apply archives the '
                  f'{total} lead(s) above, reversibly.')
            db.session.rollback()
            return 0
        if not args.actor:
            print('\n--apply needs --actor <EMP-CODE>. The archive asks '
                  'the same permission the Archive button asks \u2014 the '
                  "lead's owner, the head of its vertical, or an "
                  'administrator \u2014 so the sweep has to act as '
                  'somebody. Nothing was written.')
            return 2
        apply(found, batch=next_batch_id(), actor=args.actor.strip().upper())
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
