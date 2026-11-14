"""The event number on leads that already exist, so the notices find them.

13 Nov, after the first `--apply --portals`. A portal lead is held to
its event by `Lead.portal_key`, and a lead created without one cannot
be found by the reminder, the "technical is now live" or the
withdrawal that follows it -- each of those then has nowhere to go and
becomes a second lead, or nothing at all.

Two reasons a live lead can be missing it:

  * it was created before `portal_rfq.event_key` existed, or before
    the key was written with the same reader the join uses;
  * its number is one the reader could not see until today --
    `RFQ_CHA_TPT_JSL_6500005154` in an Ariba subject, or the event
    named in the body of a mail whose subject carries only a document
    id.

    .venv/bin/python scripts/2026_11_13_portal_keys.py --check
    .venv/bin/python scripts/2026_11_13_portal_keys.py --check --csv
    .venv/bin/python scripts/2026_11_13_portal_keys.py --apply --actor PCM001
    .venv/bin/python scripts/2026_11_13_portal_keys.py --undo --batch PKEY-20261113-1

WHAT IT WILL AND WILL NOT TOUCH

Only a lead whose mail came from a listed procurement portal, and only
where `portal_key` is **empty**. A key already on a lead is left alone
even when the reader now proposes a different one: two leads were
joined under the old key and changing it would strand whichever mail
arrives next. Those are listed under "a different key is proposed" so
somebody can look, and nothing is done about them here.

Where two leads come out with the SAME key, the older one keeps it and
the younger is listed rather than written: one lead per event means
one lead holds the key, and deciding which of two live leads is the
event is a merge, not a backfill.

Nothing is deleted and nothing is merged. Every write carries an audit
row with the batch id, and `--undo --batch` puts back exactly the keys
that batch set, leaving any lead somebody has edited since.
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from app import app, db                                    # noqa: E402

ACTOR = 'CRM-PORTAL-KEYS'
ACTION = 'lead.portal_key_set'
BATCH_PREFIX = 'PKEY'
DEFAULT_CSV = os.path.join(_ROOT, 'data', 'portal_keys_13NOV.csv')


def _rule(n=72):
    print('─' * n)


# ── finding them ────────────────────────────────────────────────────
def portal_leads():
    """Leads whose mail came from a procurement portal.

    `lead_platform` is the column the intake writes the portal into,
    and the sender is checked as well: a lead created before that
    column was filled carries the portal only in
    `original_email_from`.
    """
    from app import Lead
    from app.services import portal_rfq

    out = []
    rows = (Lead.query
            .filter(Lead.is_archived.isnot(True))
            .order_by(Lead.id.asc()).all())
    for lead in rows:
        portal = portal_rfq.is_portal(lead.original_email_from or '')
        if not portal:
            platform = (lead.lead_platform or '').strip().lower()
            if not platform:
                continue
            if not portal_rfq.is_portal('x@' + platform):
                continue
            portal = platform
        out.append((lead, portal))
    return out


def proposals(leads):
    """(to_write, already, different, clashing).

    `to_write` is the answer; the other three are what a person has to
    see in order to trust it.
    """
    from app.services import portal_rfq

    to_write, already, different = [], [], []
    for lead, portal in leads:
        key = portal_rfq.event_key(
            lead.original_email_from or ('x@' + portal),
            lead.original_email_subject or '',
            lead.original_email_body or '')
        if not key:
            continue
        held = (lead.portal_key or '').strip()
        if held == key:
            already.append((lead, portal, key))
        elif held:
            different.append((lead, portal, held, key))
        else:
            to_write.append((lead, portal, key))

    # One lead per event: the oldest keeps the key, the rest are
    # reported. A backfill must not create the thing it prevents.
    taken = {}
    for lead, _portal, key in already:
        taken.setdefault(key, lead.id)
    clean, clashing = [], []
    for lead, portal, key in to_write:
        owner = taken.get(key)
        if owner is None:
            taken[key] = lead.id
            clean.append((lead, portal, key))
        else:
            clashing.append((lead, portal, key, owner))
    return clean, already, different, clashing


def report(clean, already, different, clashing):
    print(f'\n══ {len(clean)} lead(s) would be given their event '
          f'number ══')
    if clean:
        print(f'  {"lead":>7}  {"event":<40} company')
        for lead, _portal, key in clean:
            print(f'  {lead.id:>7}  {key[:40]:<40} '
                  f'{(lead.company or "")[:28]}')
            print(f'  {"":>7}  {(lead.original_email_subject or "")[:66]!r}')
    else:
        print('  none — every portal lead that has a readable number '
              'already carries it.')

    print(f'\n  {len(already)} lead(s) already carry the key the '
          f'reader proposes.')

    if different:
        print(f'\n  !! {len(different)} lead(s) carry a DIFFERENT key '
              f'from the one the reader now proposes. Left alone — '
              f'changing a key that mail has already joined on would '
              f'strand whatever arrives next. Listed so somebody can '
              f'look:')
        for lead, _portal, held, key in different:
            print(f'    #{lead.id}: has {held!r}, reader says {key!r} '
                  f'— {(lead.original_email_subject or "")[:44]!r}')

    if clashing:
        print(f'\n  !! {len(clashing)} lead(s) would take a key another '
              f'lead already holds. NOT written: one lead per event '
              f'means one lead holds the key, and choosing between '
              f'two live leads is a merge and not a backfill.')
        for lead, _portal, key, owner in clashing:
            print(f'    #{lead.id} wants {key} — lead #{owner} has it. '
                  f'Merge them on Admin → Duplicates if they are the '
                  f'same enquiry.')

    counts = Counter(portal for _lead, portal, _key in clean)
    if counts:
        print('\n  by portal: ' + ', '.join(
            f'{n} {portal}' for portal, n in counts.most_common()))


def write_csv(clean, different, clashing, path):
    buf = io.StringIO()
    out = csv.writer(buf)
    out.writerow(['what', 'lead_id', 'company', 'event_key',
                  'held_key', 'other_lead', 'subject'])
    for lead, _portal, key in clean:
        out.writerow(['would set', lead.id, lead.company or '', key, '',
                      '', (lead.original_email_subject or '')[:120]])
    for lead, _portal, held, key in different:
        out.writerow(['left alone', lead.id, lead.company or '', key,
                      held, '', (lead.original_email_subject or '')[:120]])
    for lead, _portal, key, owner in clashing:
        out.writerow(['clash', lead.id, lead.company or '', key, '',
                      owner, (lead.original_email_subject or '')[:120]])
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', newline='') as fh:
        fh.write(buf.getvalue())
    print(f'\nCSV: {path}')


# ── writing them ────────────────────────────────────────────────────
def next_batch_id(now=None):
    from app.models.audit import AuditEvent

    now = now or datetime.utcnow()
    prefix = f'{BATCH_PREFIX}-{now.strftime("%Y%m%d")}-'
    try:
        rows = (AuditEvent.query.filter(AuditEvent.actor == ACTOR)
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


def apply(clean, *, batch, actor):
    from app.services import audit

    done = 0
    try:
        for lead, _portal, key in clean:
            if (lead.portal_key or '').strip():
                continue                  # somebody set one since
            lead.portal_key = key[:60]
            lead.updated_at = datetime.utcnow()
            audit.record(ACTION, 'lead', lead.id,
                         old={'portal_key': None},
                         new={'portal_key': lead.portal_key,
                              'batch': batch},
                         actor=actor,
                         reason=(f'batch {batch}: the event number read '
                                 f'from the mail this lead was made '
                                 f'from, so its notices can find it'),
                         commit=False)
            done += 1
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    print(f'\n{done} lead(s) given their event number. Batch {batch}.')
    print(f'Undo: --undo --batch {batch}')
    return done


def undo(batch):
    from app import Lead
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

    undone, left = 0, 0
    for row, payload in mine:
        lead = db.session.get(Lead, int(row.entity_id))
        if lead is None:
            continue
        if (lead.portal_key or '') != (payload.get('portal_key') or ''):
            left += 1
            print(f'  #{lead.id}: the key has changed since the batch '
                  f'— left alone')
            continue
        lead.portal_key = None
        audit.record('lead.portal_key_undone', 'lead', lead.id,
                     old={'portal_key': payload.get('portal_key')},
                     new={'batch': batch}, actor=ACTOR,
                     reason=f'undo of batch {batch}', commit=False)
        undone += 1
    db.session.commit()
    print(f'\n{undone} key(s) removed; {left} left alone.')
    return undone


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
        print('the event number on portal leads that have none.'
              + ('' if (args.apply or args.undo) else ' REPORT ONLY.'))
        _rule()

        if args.undo:
            if not args.batch:
                print('\n--undo needs --batch PKEY-YYYYMMDD-n')
                return 2
            undo(args.batch)
            return 0

        leads = portal_leads()
        print(f'\n{len(leads)} lead(s) came in through a procurement '
              f'portal.')
        clean, already, different, clashing = proposals(leads)
        report(clean, already, different, clashing)
        if args.csv:
            write_csv(clean, different, clashing, args.csv)

        if not args.apply:
            print(f'\nDRY RUN — nothing written. --apply --actor '
                  f'<EMP_CODE> sets the {len(clean)} key(s) above.')
            db.session.rollback()
            return 0
        if not args.actor:
            print('\n--apply needs --actor <EMP_CODE>: an audit row '
                  'with nobody on it is half a record.')
            return 2
        apply(clean, batch=next_batch_id(), actor=args.actor.strip().upper())
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
