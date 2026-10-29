"""
Open leads nobody holds, and who the account says should hold them.

CRM-LEAD-ACCOUNT-01 stopped new leads arriving unowned once they are
linked to an account. It does nothing for the ones already here: on
29 October 2026 there were 132 open leads with no PIC, most of them
sitting under an account that *does* have an owner. Nobody is working
them and nobody is told to.

This reads that backlog and says, for each lead, who the account
would give it to:

    OBVIOUS         exactly one active owner — safe to assign
    CHOOSE          several active owners, a person must pick
    OWNER INACTIVE  the owner has left; the standing owner map names
                    their replacement
    NO OWNER        the account has nobody at all — fix the account
    NO ACCOUNT      the lead is not linked — link it first, which is
                    what scripts/link_unplaced_leads.py is for

Read-only by default. It prints and changes nothing:

    .venv/bin/python scripts/unowned_open_leads.py
    .venv/bin/python scripts/unowned_open_leads.py --ids 12397

Assigning needs the leads named, an actor, and --apply together — no
"assign everything it worked out", because a wrong owner is worse
than no owner: the lead now looks worked:

    .venv/bin/python scripts/unowned_open_leads.py \\
        --ids 12200 11607 --actor DIR12010 --apply

Only OBVIOUS leads are assigned, only open ones — a Won or Lost lead
is never touched — and the assignment goes through the same
`lead_assignment.assign()` the lead sheet uses, so the history row,
the audit row and the notification all happen exactly as they would
if somebody had done it on the screen.

--history-only writes the missing history row for a lead whose owner
was set outside the CRM (#12397, set by hand in SQL on 29 October).
It documents what is already true rather than changing anything, and
it is idempotent: running it twice writes one row.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

OBVIOUS = 'OBVIOUS'
CHOOSE = 'CHOOSE'
INACTIVE = 'OWNER INACTIVE'
NO_OWNER = 'NO OWNER'
NO_ACCOUNT = 'NO ACCOUNT'

#: Print order: what can be fixed now first, what needs a decision
#: next, what needs somebody else's work last.
VERDICTS = (OBVIOUS, CHOOSE, INACTIVE, NO_OWNER, NO_ACCOUNT)

#: On the history row and the audit row, so a backfilled owner is
#: never mistaken for somebody's decision.
BACKFILL_REASON = 'Backfill: owner taken from account'

#: The default for --history-only: the #12397 correction.
HISTORY_REASON = 'Manual correction 29 Oct'


# ── people ───────────────────────────────────────────────────────────
def _employee(code):
    from app import Employee
    code = (code or '').strip().upper()
    if not code:
        return None
    return Employee.query.filter_by(emp_code=code).first()


def _active(code):
    emp = _employee(code)
    return emp is not None and bool(emp.is_active)


def _name(code):
    emp = _employee(code)
    return emp.name if emp is not None else (code or '')


def _who(code):
    """"Name (CODE)", or '' — the form every line below prints."""
    code = (code or '').strip().upper()
    if not code:
        return ''
    name = _name(code)
    return f'{name} ({code})' if name and name != code else code


# ── what the account says ────────────────────────────────────────────
def owners_of(account, vertical):
    """The owner codes this account offers for this vertical.

    The vertical's own owner when there is one, because that is the
    specific answer. Only when the vertical names nobody do the
    account's other verticals get a say, and only when they are all
    silent does the account PIC.
    """
    from app.models.assignment import AccountVerticalOwner
    from app.services import account_gate

    if account is None:
        return []
    found = []
    named = (account_gate.owner_for(account, vertical) or '').strip().upper()
    if named:
        found.append(named)
    else:
        try:
            rows = (AccountVerticalOwner.query
                    .filter_by(account_id=account.id).all())
        except Exception:
            from app import db
            db.session.rollback()
            rows = []
        for row in rows:
            code = (row.emp_code or '').strip().upper()
            if code and code not in found:
                found.append(code)
    if not found:
        pic = (getattr(account, 'pic_emp_code', '') or '').strip().upper()
        if pic:
            found.append(pic)
    return found


def read(lead):
    """One lead, one verdict."""
    from app import Company, db
    from app.services import account_gate, auto_assign, sales_rules

    account = (db.session.get(Company, lead.company_id)
               if lead.company_id else None)
    # Two readings of the same thing. The lead's own word is what the
    # count is grouped by, because that is what somebody reading the
    # report recognises; `vertical_of` resolves aliases and falls back
    # to a master name, which is what the owner lookup needs.
    written = (getattr(lead, 'procam_vertical', '') or '').strip()
    resolved = account_gate.vertical_of(lead) or ''
    candidates = owners_of(account, written or resolved)
    active = [c for c in candidates if _active(c)]

    owner, replacement = '', ''
    if account is None:
        verdict = NO_ACCOUNT
    elif not candidates:
        verdict = NO_OWNER
    elif len(active) == 1:
        verdict, owner = OBVIOUS, active[0]
    elif len(active) > 1:
        verdict = CHOOSE
    else:
        verdict = INACTIVE
        replacement = (auto_assign.replacement_for(candidates[0])
                       or auto_assign.backstop())

    return {
        'lead': lead,
        'lead_id': lead.id,
        'company': lead.company or '',
        'stage': lead.stage or '',
        'open': sales_rules.is_open(lead.stage),
        'owned': bool((lead.assigned_to or '').strip()),
        'vertical': written or '(none)',
        'account': account,
        'account_id': account.id if account is not None else None,
        'account_name': account.name if account is not None else '',
        'candidates': candidates,
        'active': active,
        'owner': owner,
        'replacement': replacement,
        'verdict': verdict,
    }


def unowned(lead_ids=()):
    """Every open lead with no PIC, read. Or just the ones named."""
    from app import Lead, db
    from app.services import sales_rules

    if lead_ids:
        leads = [db.session.get(Lead, int(x)) for x in lead_ids]
        leads = [x for x in leads if x is not None]
    else:
        leads = (Lead.query
                 .filter(db.or_(Lead.assigned_to.is_(None),
                                Lead.assigned_to == ''),
                         Lead.is_archived.isnot(True),
                         Lead.stage.in_(sales_rules.open_stages()))
                 .order_by(Lead.id.desc()).all())
    return [read(lead) for lead in leads]


def counts(rows):
    """(per verdict, per vertical) — both in print order."""
    by_verdict = {name: 0 for name in VERDICTS}
    by_vertical = {}
    for row in rows:
        by_verdict[row['verdict']] = by_verdict.get(row['verdict'], 0) + 1
        by_vertical[row['vertical']] = by_vertical.get(
            row['vertical'], 0) + 1
    return by_verdict, by_vertical


def report(rows):
    if not rows:
        return 'Nothing unowned. Every open lead has a PIC.'

    out = [f'{len(rows)} open lead(s) with no owner:', '']
    for verdict in VERDICTS:
        group = [r for r in rows if r['verdict'] == verdict]
        if not group:
            continue
        out.append(f'── {verdict} ── {len(group)}')
        for row in group:
            out.append(f'  #{row["lead_id"]}  '
                       f'{row["company"] or "(blank)"}  ·  {row["stage"]}'
                       f'  ·  {row["vertical"]}')
            if row['account'] is None:
                out.append('      no account — link it first '
                           '(scripts/link_unplaced_leads.py)')
            else:
                out.append(f'      account: {row["account_name"]} '
                           f'(id {row["account_id"]})')
            if verdict == OBVIOUS:
                out.append(f'      owner:   {_who(row["owner"])}')
            elif verdict == CHOOSE:
                names = ', '.join(_who(c) for c in row['active'])
                out.append(f'      owners:  {names} — somebody must pick')
            elif verdict == INACTIVE:
                gone = ', '.join(_who(c) for c in row['candidates'])
                out.append(f'      owner:   {gone} — no longer active')
                out.append(f'      goes to: {_who(row["replacement"])} '
                           f'(standing owner map)')
            elif verdict == NO_OWNER:
                out.append('      the account has no owner either — '
                           'fix the account, not the lead')
        out.append('')

    by_verdict, by_vertical = counts(rows)
    out.append('By verdict:')
    for name in VERDICTS:
        out.append(f'  {name:<16} {by_verdict.get(name, 0):>4}')
    out.append('')
    out.append('By vertical:')
    for name in sorted(by_vertical, key=lambda k: (-by_vertical[k], k)):
        out.append(f'  {name:<16} {by_vertical[name]:>4}')
    out.append('')
    out.append('Nothing was changed. To assign the OBVIOUS ones, name '
               'them:')
    out.append('  --ids <lead ids> --actor <EMP> --apply')
    return '\n'.join(out)


# ── the writes ───────────────────────────────────────────────────────
def apply(lead_ids, *, actor, notify=True):
    """Assign the named OBVIOUS leads. Returns (done, refused)."""
    from app import Lead, db
    from app.services import audit, lead_assignment, sales_rules

    done, refused = [], []
    for raw in lead_ids:
        lead = db.session.get(Lead, int(raw))
        if lead is None:
            refused.append((raw, 'no such lead'))
            continue
        # Said twice on purpose: the default query cannot return a
        # closed lead, but --ids can name one, and rewriting the owner
        # of a lead somebody has already reported on is the one
        # mistake here that cannot be undone quietly.
        if (lead.stage or '') in sales_rules.terminal_stages():
            refused.append((lead.id, f'{lead.stage} — closed leads are '
                                     f'never touched'))
            continue
        if not sales_rules.is_open(lead.stage):
            refused.append((lead.id, f'{lead.stage} is not an open stage'))
            continue
        if (lead.assigned_to or '').strip():
            refused.append((lead.id, f'already owned by '
                                     f'{_who(lead.assigned_to)}'))
            continue

        row = read(lead)
        if row['verdict'] != OBVIOUS:
            refused.append((lead.id, f'{row["verdict"]} — not assigned '
                                     f'without a person deciding'))
            continue

        ok, err = lead_assignment.assign(
            lead, primary_code=row['owner'], actor=actor,
            note=BACKFILL_REASON, notify=notify)
        if not ok:
            refused.append((lead.id, err))
            continue
        audit.record('lead.owner_backfilled', 'lead', lead.id,
                     old={'assigned_to': ''},
                     new={'assigned_to': row['owner'],
                          'from_account': row['account_id']},
                     reason=BACKFILL_REASON, actor=actor)
        done.append((lead.id, row['owner']))
    db.session.commit()
    return done, refused


def write_history(lead_ids, *, actor, owner='', reason=HISTORY_REASON):
    """Record an owner that was set outside the CRM.

    Writes nothing to the lead: the owner is already there. It fills
    in the trail that the out-of-band change skipped, so the lead's
    history answers "who put them there, and why".

    Idempotent — a second run finds its own row and skips.
    """
    from app import Lead, LeadAssignmentHistory, db
    from app.services import audit

    written, skipped = [], []
    for raw in lead_ids:
        lead_id = int(raw)
        lead = db.session.get(Lead, lead_id)
        if lead is None:
            skipped.append((raw, 'no such lead'))
            continue
        current = (lead.assigned_to or '').strip().upper()
        if not current:
            skipped.append((lead_id, 'the lead has no owner — this '
                                     'records one, it does not set one'))
            continue
        wanted = (owner or '').strip().upper()
        if wanted and wanted != current:
            skipped.append((lead_id, f'owned by {current}, not {wanted} '
                                     f'— nothing written'))
            continue
        already = (LeadAssignmentHistory.query
                   .filter_by(lead_id=lead_id, to_primary=current,
                              note=reason).first())
        if already is not None:
            skipped.append((lead_id, 'already recorded'))
            continue
        db.session.add(LeadAssignmentHistory(
            lead_id=lead_id, from_primary=None, to_primary=current,
            from_secondary=None,
            to_secondary=(lead.secondary_owner or None),
            changed_at=datetime.utcnow(), changed_by=actor, note=reason))
        audit.record('lead.assignment_history_backfilled', 'lead', lead_id,
                     new={'to_primary': current}, reason=reason,
                     actor=actor)
        written.append((lead_id, current))
    db.session.commit()
    return written, skipped


def history_preview(lead_ids, *, owner='', reason=HISTORY_REASON):
    """What --history-only would write, written down instead."""
    from app import Lead, LeadAssignmentHistory, db

    out = []
    for raw in lead_ids:
        lead_id = int(raw)
        lead = db.session.get(Lead, lead_id)
        if lead is None:
            out.append(f'#{raw}: no such lead')
            continue
        current = (lead.assigned_to or '').strip().upper()
        if not current:
            out.append(f'#{lead_id}: no owner on the lead — nothing to '
                       f'record')
            continue
        wanted = (owner or '').strip().upper()
        if wanted and wanted != current:
            out.append(f'#{lead_id}: owned by {current}, not {wanted} — '
                       f'nothing would be written')
            continue
        already = (LeadAssignmentHistory.query
                   .filter_by(lead_id=lead_id, to_primary=current,
                              note=reason).first())
        if already is not None:
            out.append(f'#{lead_id}: already recorded — nothing to do')
            continue
        out.append(f'#{lead_id}: would record owner {_who(current)} '
                   f'with reason "{reason}"')
    out.append('')
    out.append('Nothing was changed. Add --actor <EMP> --apply to write '
               'it.')
    return '\n'.join(out)


# ── the command line ─────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--ids', nargs='*', default=[],
                    help='the leads to look at, or to write')
    ap.add_argument('--actor', help='who is doing this (emp code)')
    ap.add_argument('--apply', action='store_true',
                    help='write the assignments')
    ap.add_argument('--history-only', action='store_true',
                    help='record an owner set outside the CRM; '
                         'changes no owner')
    ap.add_argument('--owner', default='',
                    help='with --history-only, the owner you expect '
                         'to find (a safety check)')
    ap.add_argument('--reason', default=HISTORY_REASON,
                    help='with --history-only, the note on the row')
    ap.add_argument('--quiet', action='store_true',
                    help='assign without notifying the new owners')
    args = ap.parse_args()

    import app as main_app
    with main_app.app.app_context():
        if args.history_only:
            if not args.ids:
                print('--history-only needs --ids. Nothing was changed.')
                return 2
            if not args.apply or not args.actor:
                print(history_preview(args.ids, owner=args.owner,
                                      reason=args.reason))
                return 0
            written, skipped = write_history(
                args.ids, actor=args.actor, owner=args.owner,
                reason=args.reason)
            for lead_id, code in written:
                print(f'recorded #{lead_id} -> {_who(code)}')
            for lead_id, why in skipped:
                print(f'skipped  #{lead_id}: {why}')
            print(f'\n{len(written)} recorded, {len(skipped)} skipped.')
            return 0

        if not args.apply:
            print(report(unowned(args.ids)))
            return 0

        if not args.ids or not args.actor:
            print('--apply needs --ids and --actor. Nothing was changed.')
            return 2
        done, refused = apply(args.ids, actor=args.actor,
                              notify=not args.quiet)
        for lead_id, code in done:
            print(f'assigned #{lead_id} -> {_who(code)}')
        for lead_id, why in refused:
            print(f'refused  #{lead_id}: {why}')
        print(f'\n{len(done)} assigned, {len(refused)} refused.')
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
