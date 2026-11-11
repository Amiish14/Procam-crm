"""The 384 leads nobody's account owns: propose, tick, apply, undo.

CRM-MAIL-2026-11-11 10. `2026_11_11_account_block.py --check --stuck`
counts 384 open leads the account rule still refuses: 277 with no
account at all and 107 filed under a placeholder ("Unknown",
"New Lead"). Clearing them one at a time through Account check is a
week of somebody's life; clearing them automatically is how 254 leads
ended up filed under nobody in the first place.

So: the CRM proposes, a person ticks, and only ticked rows are
applied.

    .venv/bin/python scripts/2026_11_11_link_accounts.py --check
    .venv/bin/python scripts/2026_11_11_link_accounts.py --check \
          --csv data/link_accounts_11NOV
    .venv/bin/python scripts/2026_11_11_link_accounts.py \
          --apply --csv data/link_accounts_11NOV-freight-forwarding.csv
    .venv/bin/python scripts/2026_11_11_link_accounts.py \
          --undo --batch LINK-20261111-1

WHY A CSV PER VERTICAL

Because the person who can answer "is this Sinofreight or Sinocan"
is the head of that vertical, and a single file of 384 rows is a file
nobody opens. One file each, named after the vertical, with the
proposal, the rule that found it and a confidence.

    tick column   put `y` (or `yes`, `1`, `x`) in `apply` to accept
    account_id    the proposal. Change it to link a different
                  account — the apply reads this column, not the
                  proposal it printed.
    archive       `junk` or `informational` instead, for a
                  placeholder lead that is not an enquiry at all.

Anything else in `apply` is left alone, which is the default: a row
nobody ticked is a row nobody decided.

CONFIDENCE IS THE RULE, NOT A NUMBER WE INVENTED

    certain    a learned sender, a contact on file, the account's own
               email or website domain, or a GSTIN. One account, no
               disagreement. `account_match.auto_link` would have
               taken this one on its own at intake.
    likely     the normalised company name is exactly an account's,
               and it is the only candidate of any kind.
    possible   several candidates, or a name that only resembles one.
               Shown with every candidate so a person can choose.
    none       nothing matched. For a placeholder lead that usually
               means it was never an enquiry; `archive` is the column
               for those.

REVERSIBLE, AND NOTHING IS DELETED

Every apply writes an audit row carrying the batch id and the
account the lead was on before (usually none). `--undo --batch`
reads those rows back and puts every lead in the batch where it was.
Archiving goes through `lead_archive`, which is reversible by an
administrator and already audited.

The apply deliberately does NOT teach the matcher the sender's
address (`learn=False`). A bulk tick is a decision about one lead; a
learned mapping is a decision about every future mail from that
address, and it would be made silently, in bulk, by whoever happened
to tick the row.
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

from app import Company, Lead, app, db                      # noqa: E402

ACTOR = 'CRM-MAIL-2026-11-11'
ACTION = 'lead.account_linked'
BATCH_PREFIX = 'LINK'

#: What a tick looks like. Anything else is "not decided".
YES = ('y', 'yes', '1', 'x', 'true', 'ok', 'tick')
ARCHIVE_WORDS = {'junk': 'junk', 'informational': 'junk',
                 'info': 'junk', 'archive': 'junk',
                 'test': 'test', 'duplicate': 'duplicate'}

CERTAIN, LIKELY, POSSIBLE, NONE = 'certain', 'likely', 'possible', 'none'

FIELDS = ('apply', 'archive', 'lead_id', 'confidence', 'company_on_lead',
          'placeholder', 'sender', 'subject', 'stage', 'owner', 'vertical',
          'created', 'account_id', 'account_name', 'rule',
          'other_candidates')


def _rule(n=70):
    print('─' * n)


# ── the population ────────────────────────────────────────
def stuck_leads():
    """Open leads the account rule refuses: no account, or a placeholder.

    The same two reasons `account_gate.reason_for` names, asked of the
    whole open book rather than one lead.
    """
    from app.services import account_gate, sales_rules

    rows = (Lead.query
            .filter(Lead.is_archived.isnot(True),
                    Lead.junk_deleted.isnot(True),
                    Lead.merged_into_id.is_(None),
                    Lead.stage.in_(sales_rules.open_stages()))
            .order_by(Lead.id.asc())
            .all())
    out = []
    for lead in rows:
        code, why = account_gate.reason_for(lead)
        if code in (account_gate.WHY_NO_ACCOUNT,
                    account_gate.WHY_PLACEHOLDER):
            out.append((lead, code, why))
    return out


def propose(lead):
    """(confidence, best, others) for one lead.

    `lead_account.suggest` is the matcher the lead sheet and the
    intake both use, so a proposal here is the same answer a person
    would be shown if they opened the lead.
    """
    from app.services import account_match, lead_account

    sender = (lead.email or lead.original_email_from or '')
    name = (lead.company or '')
    text = ' '.join(filter(None, (
        lead.original_email_subject or '',
        (lead.original_email_body or '')[:2000])))

    account, how = account_match.auto_link(
        sender, company_name=name, text=text)
    if account is not None:
        return CERTAIN, {'account': account, 'rule': how}, []

    account, how = account_match.auto_link_by_name(
        sender, company_name=name, text=text)
    if account is not None:
        return LIKELY, {'account': account, 'rule': how}, []

    try:
        rows = lead_account.suggest(lead, limit=5) or []
    except Exception:                                     # noqa: BLE001
        rows = []
    if not rows:
        return NONE, None, []
    best = {'account': db.session.get(Company, rows[0]['account_id']),
            'rule': rows[0].get('rule') or 'suggested'}
    if best['account'] is None:
        return NONE, None, []
    return POSSIBLE, best, rows[1:]


def _vertical_of(lead):
    from app.services import account_gate
    return account_gate.vertical_of(lead) or 'no vertical'


def row_for(lead, code, confidence, best, others):
    return {
        'apply': '',
        'archive': '',
        'lead_id': lead.id,
        'confidence': confidence,
        'company_on_lead': (lead.company or '')[:70],
        'placeholder': ('yes' if code == 'placeholder_account' else ''),
        'sender': (lead.email or lead.original_email_from or '')[:70],
        'subject': (lead.original_email_subject
                    or lead.forwarded_subject or '')[:90],
        'stage': lead.stage or '',
        'owner': lead.assigned_to or '',
        'vertical': _vertical_of(lead),
        'created': str(lead.created_at or '')[:10],
        'account_id': (best['account'].id if best else ''),
        'account_name': (best['account'].name if best else ''),
        'rule': (best['rule'] if best else ''),
        'other_candidates': '; '.join(
            f"#{r['account_id']} {r.get('name') or ''}" for r in others)[:160],
    }


def survey():
    rows = []
    for lead, code, _why in stuck_leads():
        confidence, best, others = propose(lead)
        rows.append(row_for(lead, code, confidence, best, others))
    return rows


# ── the report ───────────────────────────────────────────
def report(rows):
    print(f'\n── {len(rows)} open lead(s) the account rule refuses ──')
    by_conf = Counter(r['confidence'] for r in rows)
    for conf in (CERTAIN, LIKELY, POSSIBLE, NONE):
        if by_conf.get(conf):
            print(f'  {by_conf[conf]:>5}  {conf}')
    placeholders = [r for r in rows if r['placeholder']]
    print(f'\n  {len(placeholders)} of them are filed under a placeholder '
          f'name. Those are the ones that are often not enquiries at '
          f'all — the `archive` column is for them.')

    print('\n── by vertical, which is how the files are split ──')
    by_vert = Counter(r['vertical'] for r in rows)
    for vert, n in by_vert.most_common():
        certain = len([r for r in rows if r['vertical'] == vert
                       and r['confidence'] in (CERTAIN, LIKELY)])
        print(f'  {n:>5}  {vert:<24} {certain} proposed with a '
              f'name or a domain')

    print('\n── the strongest proposals ──')
    strong = [r for r in rows if r['confidence'] in (CERTAIN, LIKELY)]
    for r in strong[:25]:
        print(f'  #{r["lead_id"]:<7} {r["company_on_lead"][:28]:<28} '
              f'→ {r["account_name"][:28]:<28} {r["confidence"]:<9} '
              f'{r["rule"][:34]}')
    if len(strong) > 25:
        print(f'  … and {len(strong) - 25} more in the CSV')

    print('\n── nothing matched ──')
    none = [r for r in rows if r['confidence'] == NONE]
    print(f'  {len(none)} lead(s). Sender and subject are in the CSV so '
          f'a person can tell an enquiry from a circular.')
    for r in none[:12]:
        print(f'  #{r["lead_id"]:<7} {r["sender"][:34]:<34} '
              f'{r["subject"][:40]}')
    if len(none) > 12:
        print(f'  … and {len(none) - 12} more')


def write_csvs(rows, base):
    """One file per vertical, plus one with everything."""
    base = os.path.abspath(base)
    os.makedirs(os.path.dirname(base) or '.', exist_ok=True)
    groups = defaultdict(list)
    for row in rows:
        groups[row['vertical']].append(row)

    written = []
    for vertical, group in sorted(groups.items()):
        slug = ''.join(c if c.isalnum() else '-'
                       for c in vertical.lower()).strip('-') or 'none'
        path = f'{base}-{slug}.csv'
        _write_one(path, group)
        written.append((path, len(group)))
    _write_one(f'{base}-all.csv', rows)
    written.append((f'{base}-all.csv', len(rows)))

    print('\n── files ──')
    for path, n in written:
        print(f'  {n:>5}  {path}')
    print('  Tick `apply` with y, or put junk / informational in '
          '`archive`, then run --apply --csv <file>.')
    return written


def _write_one(path, rows):
    buf = io.StringIO()
    out = csv.DictWriter(buf, fieldnames=list(FIELDS))
    out.writeheader()
    for row in rows:
        out.writerow({k: row.get(k, '') for k in FIELDS})
    with open(path, 'w', newline='') as fh:
        fh.write(buf.getvalue())


# ── applying what was ticked ──────────────────────────────────
def next_batch_id(now=None):
    """LINK-YYYYMMDD-n, counting today's batches already on file."""
    from app.models.audit import AuditEvent

    now = now or datetime.utcnow()
    stamp = now.strftime('%Y%m%d')
    prefix = f'{BATCH_PREFIX}-{stamp}-'
    try:
        rows = (AuditEvent.query
                .filter(AuditEvent.action == ACTION,
                        AuditEvent.actor == ACTOR)
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


def read_ticks(path):
    """(to_link, to_archive, skipped) out of a ticked CSV."""
    with open(path, newline='') as fh:
        rows = list(csv.DictReader(fh))
    to_link, to_archive, skipped = [], [], []
    for row in rows:
        lead_id = (row.get('lead_id') or '').strip()
        if not lead_id.isdigit():
            continue
        lead_id = int(lead_id)
        archive = (row.get('archive') or '').strip().lower()
        ticked = (row.get('apply') or '').strip().lower() in YES
        account_id = (row.get('account_id') or '').strip()
        if archive:
            category = ARCHIVE_WORDS.get(archive)
            if category is None:
                skipped.append((lead_id, f'archive says {archive!r}, '
                                         f'which is not one of '
                                         f'{", ".join(sorted(ARCHIVE_WORDS))}'))
                continue
            to_archive.append((lead_id, category, archive))
            continue
        if not ticked:
            continue
        if not account_id.isdigit():
            skipped.append((lead_id, 'ticked with no account_id'))
            continue
        to_link.append((lead_id, int(account_id),
                        (row.get('rule') or '').strip()))
    return to_link, to_archive, skipped


def plan(to_link, to_archive):
    """Re-check every ticked row against the database as it is now."""
    from app.services import account_match

    linkable, refused = [], []
    for lead_id, account_id, rule in to_link:
        lead = db.session.get(Lead, lead_id)
        account = db.session.get(Company, account_id)
        if lead is None:
            refused.append((lead_id, 'no such lead'))
            continue
        if account is None:
            refused.append((lead_id, f'no account #{account_id}'))
            continue
        if lead.company_id:
            refused.append((lead_id, f'already on account '
                                     f'#{lead.company_id} — somebody '
                                     f'placed it since the CSV'))
            continue
        why = account_match.not_a_customer(account)
        if why:
            refused.append((lead_id, f'{account.name}: {why}'))
            continue
        linkable.append((lead, account, rule))

    archivable = []
    for lead_id, category, word in to_archive:
        lead = db.session.get(Lead, lead_id)
        if lead is None:
            refused.append((lead_id, 'no such lead'))
            continue
        if getattr(lead, 'is_archived', False):
            refused.append((lead_id, 'already archived'))
            continue
        archivable.append((lead, category, word))
    return linkable, archivable, refused


def apply(linkable, archivable, *, batch):
    from app.services import lead_account, lead_archive

    linked = archived = 0
    try:
        for lead, account, rule in linkable:
            ok, detail = lead_account.link(
                lead, account, actor=ACTOR, learn=False,
                reason=(f'CRM-MAIL-2026-11-11 §10, batch {batch}: '
                        f'{rule or "ticked by the vertical"}')[:400])
            if not ok:
                print(f'  !! #{lead.id}: {detail}')
                continue
            # The batch id goes on its own row: `link` writes the
            # audit, and an undo has to find this batch among every
            # other link ever made.
            from app.services import audit
            audit.record(ACTION, 'lead', lead.id,
                         old={'company_id': None},
                         new={'company_id': account.id,
                              'account': account.name,
                              'batch': batch, 'rule': rule},
                         actor=ACTOR,
                         reason=f'batch {batch}')
            linked += 1
        for lead, category, word in archivable:
            lead_archive.archive(
                lead, actor=ACTOR, category=category,
                note=(f'CRM-MAIL-2026-11-11 §10, batch {batch}: '
                      f'marked {word} by the vertical')[:170])
            archived += 1
        db.session.commit()
    except Exception as exc:                              # noqa: BLE001
        db.session.rollback()
        print(f'\n  !! ROLLED BACK: {type(exc).__name__}: {str(exc)[:200]}')
        raise SystemExit(1)
    print(f'\n{linked} lead(s) linked, {archived} archived, batch {batch}.')
    print(f'Undo: .venv/bin/python scripts/2026_11_11_link_accounts.py '
          f'--undo --batch {batch}')
    return linked, archived


def undo(batch):
    """Put every lead in a batch back where it was."""
    from app.models.audit import AuditEvent
    from app.services import audit

    rows = (AuditEvent.query
            .filter(AuditEvent.action == ACTION,
                    AuditEvent.actor == ACTOR)
            .order_by(AuditEvent.id.asc()).all())
    mine = []
    for row in rows:
        payload = row.new_value if isinstance(row.new_value, dict) else {}
        if str(payload.get('batch') or '') == batch:
            mine.append((row, payload))
    if not mine:
        print(f'\nno batch {batch} on file. Nothing undone.')
        return 0
    print(f'\n{len(mine)} lead(s) in batch {batch}.')
    moved = 0
    try:
        for row, payload in mine:
            lead = db.session.get(Lead, int(row.entity_id))
            if lead is None:
                continue
            if lead.company_id != payload.get('company_id'):
                print(f'  #{lead.id} is on account '
                      f'#{lead.company_id} now, not the one this batch '
                      f'set — left alone')
                continue
            old = row.old_value if isinstance(row.old_value, dict) else {}
            lead.company_id = old.get('company_id') or None
            audit.record('lead.account_unlinked', 'lead', lead.id,
                         old={'company_id': payload.get('company_id')},
                         new={'company_id': lead.company_id,
                              'batch': batch},
                         actor=ACTOR,
                         reason=f'undo of batch {batch}')
            moved += 1
        db.session.commit()
    except Exception as exc:                              # noqa: BLE001
        db.session.rollback()
        print(f'\n  !! ROLLED BACK: {type(exc).__name__}: {str(exc)[:200]}')
        raise SystemExit(1)
    print(f'{moved} lead(s) put back. Archived leads are restored from '
          f'Admin → Deleted / archived leads, which is where a person '
          f'can see what they are restoring.')
    return moved


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--check', action='store_true',
                    help='survey and propose. The default.')
    ap.add_argument('--csv', default=None, metavar='PATH',
                    help='with --check, where to write the files '
                         '(a base name); with --apply, the ticked file')
    ap.add_argument('--apply', action='store_true',
                    help='apply the ticked rows of --csv')
    ap.add_argument('--undo', action='store_true')
    ap.add_argument('--batch', default=None, metavar='ID')
    args = ap.parse_args(argv)

    with app.app_context():
        print(f'database: {db.engine.url}')
        print('CRM-MAIL-2026-11-11 §10 — the leads on nobody\'s account.')
        _rule()

        if args.undo:
            if not args.batch:
                print('\n--undo needs --batch LINK-YYYYMMDD-n')
                return 2
            undo(args.batch)
            return 0

        if args.apply:
            if not args.csv:
                print('\n--apply needs --csv <the ticked file>')
                return 2
            to_link, to_archive, bad = read_ticks(args.csv)
            linkable, archivable, refused = plan(to_link, to_archive)
            print(f'\nticked: {len(to_link)} to link, '
                  f'{len(to_archive)} to archive')
            for lead, account, rule in linkable[:40]:
                print(f'  #{lead.id:<7} {(lead.company or "")[:28]:<28} '
                      f'→ {account.name[:34]}')
            if len(linkable) > 40:
                print(f'  … and {len(linkable) - 40} more')
            for lead, category, _word in archivable[:20]:
                print(f'  #{lead.id:<7} → archive ({category})')
            for lead_id, why in (bad + refused):
                print(f'  !! #{lead_id}: {why}')
            batch = next_batch_id()
            apply(linkable, archivable, batch=batch)
            return 0

        rows = survey()
        report(rows)
        if args.csv:
            write_csvs(rows, args.csv)
        else:
            print('\nNo CSV written. --csv data/link_accounts_11NOV '
                  'writes one file per vertical.')
        print('\nDRY RUN — nothing written. The apply reads a ticked '
              'CSV and nothing else.')
        db.session.rollback()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
