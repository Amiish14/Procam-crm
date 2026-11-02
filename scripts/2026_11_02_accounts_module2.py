"""
PROCAM-DECISIONS-02NOV Module 2 — accounts, ownership and merges.

    .venv/bin/python scripts/2026_11_02_accounts_module2.py --check
    .venv/bin/python scripts/2026_11_02_accounts_module2.py --check \
        --only owners
    .venv/bin/python scripts/2026_11_02_accounts_module2.py --apply
    .venv/bin/python scripts/2026_11_02_accounts_module2.py --undo <batch>

Rule 0.3 of the pack: "Before any bulk save, send Nilesh a preview
with counts per rule. Save after his OK." So --check prints the
counts per rule and writes nothing, and --apply is a separate
command. The 2 November pack reinstates that step, which
CRM-COCKPIT-02 §0.3 had removed; the later decision wins and it is
the safer of the two.

Every write goes through `bulk_log`, so one command reverses any of
it, and every changed record carries a dated audit remark (rule 0.2).

Three sections, run in this order and for a reason:

  owners   first. An account's owner decides who the queue hands a
           lead to, so clearing the queue before fixing ownership
           hands leads to the person who is about to be replaced.
  queue    second. Links and creates accounts, assigns the owner.
  merges   last, and this is the one to read twice. A merge changes
           what "the account" means for every lead the first two
           sections just placed, so it goes after them — and only
           where the evidence is strong enough to act without a
           person. The rest go to the in-CRM queue.
"""
import argparse
import os
import sys
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Company, Employee, Lead, app, db           # noqa: E402

ACTOR = 'PROCAM-DECISIONS-02NOV'
SECTIONS = ('owners', 'queue', 'merges')

#: Where --csv writes when no path is given. In the repo rather than
#: /tmp, because the file is the evidence for rule 0.3's approval and
#: somebody will want it a week later.
DEFAULT_CSV = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'docs', 'reference', 'module2_accounts_to_create.csv')


def _name(code):
    if not code:
        return '(nobody)'
    emp = Employee.query.filter_by(emp_code=code).first()
    if emp is None:
        return f'{code} (NOT AN EMPLOYEE ON FILE)'
    return f'{emp.name}' + ('' if emp.is_active else ' [inactive]')


def _dupes():
    """scripts/account_duplicates.py, as a module.

    Imported by path rather than by name: scripts/ holds an
    `email_ingest.py` that shadows the package of the same name if
    the directory goes on sys.path.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        'account_duplicates',
        os.path.join(_HERE, 'account_duplicates.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── 2.2 the owners ──────────────────────────────────────────────────
def report_owners(plan, problems, notes):
    from collections import Counter

    print('\n── 2.2  owner mismatches ' + '─' * 38)
    if notes:
        print('\n  read these first:')
        for note in notes:
            print(f'    • {note}')
    by_rule = Counter(row['rule'] for row in plan)
    print(f'\n  {len(plan)} owner row(s) would change, per rule:')
    for rule, count in sorted(by_rule.items()):
        print(f'    {count:>4}  {rule}')
    if not plan:
        print('    (none)')
    for row in plan[:40]:
        print(f'    {row["account"][:34]:<34} {row["vertical"][:16]:<16}'
              f' {_name(row["was"])[:20]:<20} -> '
              f'{_name(row["now"])[:20]:<20} [{row["rule"]}]')
    if len(plan) > 40:
        print(f'    … and {len(plan) - 40} more')
    if problems:
        print(f'\n  {len(problems)} thing(s) this cannot do:')
        for problem in problems:
            print(f'    !! {problem}')


def apply_owners(plan, log):
    from app.models.assignment import AccountVerticalOwner
    from app.services import audit

    for row in plan:
        owner = db.session.get(AccountVerticalOwner, row['row_id'])
        if owner is None:
            continue
        log.set(owner, 'emp_code', row['now'])
        owner.source = 'manual'
        owner.updated_by = ACTOR
        owner.updated_at = datetime.utcnow()
        owner.note = (f'2 Nov 2026: {row["rule"]} — '
                      f'{row["why"]}')[:200]
        audit.record('account.owner_changed', 'company',
                     str(row['account_id']),
                     old={'emp_code': row['was'],
                          'vertical': row['vertical']},
                     new={'emp_code': row['now'],
                          'vertical': row['vertical']},
                     actor=ACTOR,
                     reason=f'Module 2.2 {row["rule"]}: {row["why"]}',
                     commit=False)
    return len(plan)


# ── 2.1 the queue ───────────────────────────────────────────────────
def write_creates_csv(rows, path):
    """The full set of accounts that would be created, for review.

    Every row, not a sample: the report prints thirty and the whole
    point of this file is that the thirty-first is the one that
    creates an account called Unknown.

    One line per LEAD, not per account, because two leads creating one
    account is the case worth seeing — it is either the same customer
    twice, which is right, or two customers whose typed names
    normalise together, which is not.
    """
    import csv
    import os

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, 'w', newline='', encoding='utf-8') as fh:
        out = csv.writer(fh)
        out.writerow(['lead', 'typed name', 'proposed account', 'owner',
                      'owner code', 'why the owner', 'vertical', 'rule'])
        for row in sorted(rows,
                          key=lambda r: (r['account_name'].lower(),
                                         r['lead'].id)):
            out.writerow([
                row['lead'].id,
                (row['lead'].company or '').strip(),
                row['account_name'],
                _name(row['owner']),
                row['owner'],
                row['owner_why'],
                row['vertical'],
                row['rule'],
            ])
    return path


def report_queue(rows, review, stays=()):
    from collections import Counter

    print('\n── 2.1  the account check queue ' + '─' * 31)
    by_rule = Counter(row['rule'] for row in rows)
    print(f'\n  {len(rows)} lead(s) the rules can clear, per rule:')
    for rule, count in sorted(by_rule.items()):
        print(f'    {count:>4}  {rule}')
    if not rows:
        print('    (none)')
    creates = [r for r in rows if r['create']]
    print(f'\n  accounts that would be created: '
          f'{len({r["account_name"].lower() for r in creates})}')
    by_owner = Counter(row['owner_why'] for row in rows)
    for why, count in sorted(by_owner.items()):
        print(f'    {count:>4}  owner: {why}')
    for row in rows[:30]:
        mark = '+' if row['create'] else ' '
        print(f'  {mark} #{row["lead"].id:<7}'
              f' {(row["lead"].company or "(no name)")[:26]:<26}'
              f' -> {row["account_name"][:26]:<26}'
              f' {_name(row["owner"])[:18]:<18} [{row["rule"]}]')
    if len(rows) > 30:
        print(f'    … and {len(rows) - 30} more')
    print(f'\n  {len(review)} lead(s) to Nilesh\'s Thursday review:')
    for item in review[:20]:
        print(f'    #{item["lead"].id:<7} {item["why"]}')
    if len(review) > 20:
        print(f'    … and {len(review) - 20} more')

    stays = list(stays or [])
    print(f'\n  {len(stays)} lead(s) STAY IN ACCOUNT CHECK — no account '
          f'is created')
    if stays:
        print('    A person types the real customer\'s name on '
              '/CRM/admin/account-check.')
        print('    This run will not name an account after a '
              'placeholder or after us:')
        reasons = Counter()
        for item in stays:
            first = item['why'].split(' — ')[0].split(' is ')[-1]
            reasons[first] += 1
        for reason, count in reasons.most_common():
            print(f'    {count:>4}  {reason}')
        for item in stays[:30]:
            print(f'    #{item["lead"].id:<7}'
                  f' {item["candidate"][:26]:<26} {item["why"]}')
        if len(stays) > 30:
            print(f'    … and {len(stays) - 30} more')


def m2_why_not(name):
    from app.services.accounts_module2 import why_not_an_account_name
    return why_not_an_account_name(name)


def apply_queue(rows, log):
    from app.services import audit

    made = {}
    linked = created = 0
    for row in rows:
        account_id = row['account_id']
        if account_id is None:
            # Asked again at the moment of writing, not only when the
            # plan was built. The plan is the only caller today; the
            # creation is the irreversible half, and an account called
            # Unknown cannot be undone by deleting it once leads,
            # notes and quotes point at it.
            refused = m2_why_not(row['account_name'])
            if refused:
                print(f'    !! #{row["lead"].id} not created: {refused}')
                continue
            key = row['account_name'].strip().lower()
            if key in made:
                account_id = made[key]
            else:
                account = Company(
                    name=row['account_name'].strip(),
                    is_active=True,
                    vertical=(row['vertical'] or None),
                    pic_emp_code=row['owner'],
                    created_by=ACTOR,
                    notes=(f'Created 2 Nov 2026 by Module 2.1: '
                           f'{row["why"]}')[:500])
                db.session.add(account)
                db.session.flush()
                made[key] = account_id = account.id
                created += 1
                audit.record('account.created', 'company',
                             str(account.id),
                             new={'name': account.name,
                                  'vertical': row['vertical'],
                                  'owner': row['owner']},
                             actor=ACTOR,
                             reason=f'Module 2.1 {row["rule"]}',
                             commit=False)
        lead = row['lead']
        log.set(lead, 'company_id', account_id)
        if not (lead.assigned_to or '').strip():
            log.set(lead, 'assigned_to', row['owner'])
            log.set(lead, 'assigned_name', _name(row['owner']))
        linked += 1
        audit.record('lead.account_linked', 'lead', str(lead.id),
                     new={'company_id': account_id,
                          'owner': row['owner']},
                     actor=ACTOR,
                     reason=(f'Module 2.1 {row["rule"]}: {row["why"]}; '
                             f'owner is {row["owner_why"]}'),
                     commit=False)
    return linked, created


# ── 2.3 the merges ──────────────────────────────────────────────────
def report_merges(auto, ask):
    print('\n── 2.3  duplicate accounts ' + '─' * 36)
    print(f'\n  {len(auto)} cluster(s) the evidence settles — '
          f'these would be merged:')
    for cluster in auto:
        names = ', '.join(a['name'][:28] for a in cluster['accounts'])
        print(f'    {cluster["survivor"]["name"][:30]:<30} '
              f'<- {len(cluster["losers"])} other(s), '
              f'{cluster["leads"]} lead(s)')
        print(f'      {cluster["verdict_why"]}')
        print(f'      {names}')
    if not auto:
        print('    (none)')
    print(f'\n  {len(ask)} cluster(s) for the in-CRM queue '
          f'(/admin/account-duplicates — Seema):')
    for cluster in ask:
        print(f'    {cluster["survivor"]["name"][:30]:<30} '
              f'{len(cluster["accounts"])} account(s), '
              f'{cluster["leads"]} lead(s)')
        print(f'      {cluster["verdict_why"]}')
    if not ask:
        print('    (none)')


def apply_merges(dupes, auto):
    done = 0
    for cluster in auto:
        survivor = cluster['survivor']['id']
        losers = [row['id'] for row in cluster['losers']]
        if not losers:
            continue
        dupes.merge(survivor, losers, actor=ACTOR)
        done += len(losers)
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--undo', metavar='BATCH',
                    help='reverse a batch this script wrote')
    ap.add_argument('--only', choices=SECTIONS, action='append',
                    help='one section; repeatable')
    ap.add_argument('--csv', metavar='PATH', nargs='?',
                    const=DEFAULT_CSV, default=None,
                    help='write every account that would be created to '
                         'a CSV — lead, typed name, proposed account, '
                         f'owner. Defaults to {DEFAULT_CSV}')
    args = ap.parse_args()
    if len([a for a in (args.check, args.apply, args.undo) if a]) != 1:
        raise SystemExit('Choose one: --check, --apply or --undo.')
    wanted = tuple(args.only or SECTIONS)

    with app.app_context():
        from app.services import accounts_module2 as m2, bulk_log

        print(f'database: {db.engine.url}\n')

        if args.undo:
            restored, skipped, problems = bulk_log.undo(
                args.undo, actor=ACTOR)
            db.session.commit()
            print(f'  restored: {restored}')
            print(f'  skipped:  {skipped} (changed since, or already '
                  f'undone)')
            for problem in problems:
                print(f'    !! {problem}')
            print('\n  A merge is NOT reversed by this. Merges move '
                  'foreign keys across tables; undoing one is '
                  '`account_duplicates.py` with the survivor and the '
                  'loser the other way round, which is a decision, '
                  'not a rollback.')
            raise SystemExit(1 if problems else 0)

        owners = queue = merges = None
        if 'owners' in wanted:
            owners = m2.owner_plan()
            report_owners(*owners)
        if 'queue' in wanted:
            queue = m2.autoclear_plan()
            report_queue(*queue)
            if args.csv:
                creates = [r for r in queue[0] if r['create']]
                where = write_creates_csv(creates, args.csv)
                print(f'\n  {len(creates)} proposed account(s), every '
                      f'one of them, written to:\n    {where}')
        if 'merges' in wanted:
            dupes = _dupes()
            found = dupes.clusters()
            merges = m2.merge_plan(found)
            report_merges(*merges)

        if args.check:
            print('\n' + '─' * 64)
            print('CHECK ONLY — nothing written.')
            print('Send these counts to Nilesh (rule 0.3), then '
                  '--apply.')
            return

        print('\n' + '─' * 64)
        print('applying\n')
        batch = f'module2.{datetime.utcnow():%Y%m%d%H%M%S}'
        with bulk_log.batch(batch, actor=ACTOR,
                            note='PROCAM-DECISIONS-02NOV Module 2') \
                as log:
            if owners is not None:
                print(f'  owners changed: '
                      f'{apply_owners(owners[0], log)}')
            if queue is not None:
                linked, created = apply_queue(queue[0], log)
                print(f'  leads linked:   {linked}')
                print(f'  accounts made:  {created}')
            name = log.name
        db.session.commit()

        if merges is not None:
            # Outside the bulk log on purpose: a merge re-points
            # foreign keys across every table with an account id, and
            # a log of old and new values cannot put that back. Its
            # own audit trail is in account_duplicates.
            merged = apply_merges(_dupes(), merges[0])
            db.session.commit()
            print(f'  accounts merged: {merged} '
                  f'(into {len(merges[0])} survivor(s))')
            print(f'  left for a person: {len(merges[1])} cluster(s) '
                  f'on /admin/account-duplicates')

        print(f'\n  batch: {name}')
        print(f'  undo:  scripts/2026_11_02_accounts_module2.py '
              f'--undo {name}')
        print('  (the merges are not in that batch — see the note '
              'the undo prints)')


if __name__ == '__main__':
    main()
