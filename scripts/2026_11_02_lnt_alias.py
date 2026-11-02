"""Why "Lntecc" did not link to Larsen & Toubro, and the alias that fixes it.
DRY RUN BY DEFAULT.

    .venv/bin/python scripts/2026_11_02_lnt_alias.py
    .venv/bin/python scripts/2026_11_02_lnt_alias.py --apply --actor DIR12010

THE ANSWER, BEFORE THE SCRIPT RUNS

`accounts_module2.looks_like_existing` resolves "Lntecc" only through an
ALIAS, and only an alias:

    an alias prefix counts from three letters — "Lntecc" starts with
    "LNT";
    an account NAME prefix counts from eight, because proposing that
    "Global Calcium" is "Global" would be wrong often enough to teach
    people to ignore the suggestion.

"Larsen & Toubro" normalises to `larsentoubro`, and "Lntecc" to
`lntecc`. Neither is a prefix of the other and neither contains the
other, so without the alias there is nothing to match on. `aliases`
defaults to an empty list and is only filled when a merge happens —
a merge keeps the loser's spelling as an alias of the survivor — so an
account that has never been merged into has none.

That is the whole of why it did not link, and this script confirms it on
the live data rather than assuming it.

WHAT IT WILL NOT DO

It will not pick between two accounts. If the data holds more than one
account that could be L&T — and a company this size usually does, one
per division — it reports them and stops, because choosing which one
owns the alias decides where every future "Lntecc" lead lands.

It adds an alias; it never renames an account, never merges, and never
links a lead. Running `accounts_module2 --check` afterwards is what
shows the leads now proposing a link, for a person to confirm.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Company, Lead, app, db                     # noqa: E402

ACTOR = 'PROCAM-DECISIONS-02NOV'

#: The alias to add, and the account it belongs to. The account is
#: found by its normalised name, not by an id: an id is a different
#: number on staging and on production.
ALIAS = 'LNT'
ACCOUNT_KEYS = ('larsentoubro', 'lt', 'lnt', 'larsenandtoubro')

#: What is being matched, so the report can say what it looked for.
LEAD_KEYS = ('lntecc', 'lnt', 'larsentoubro')


def _squash(text):
    from app.services.accounts_module2 import squash
    return squash(text)


def candidates():
    """Accounts that could be L&T, by normalised name."""
    out = []
    for account in Company.query.filter(Company.is_active.is_(True)).all():
        key = _squash(account.name)
        if key in ACCOUNT_KEYS or key.startswith('larsentoubro'):
            out.append(account)
    return out


def leads_affected():
    """The leads whose typed name or sender looks like L&T."""
    from app.services import junk_delete as jd
    out = []
    for lead in (Lead.query
                 .filter(Lead.junk_deleted.isnot(True))
                 .order_by(Lead.id).all()):
        name = _squash(lead.company)
        sender = _squash(jd.sender_of(lead))
        if any(k in name or k in sender for k in LEAD_KEYS):
            out.append(lead)
    return out


def report():
    from app.services import accounts_module2 as m2

    print('\n── why it did not link ──')
    print(f'  "Lntecc" squashes to {_squash("Lntecc")!r}')
    print(f'  "Larsen & Toubro" squashes to '
          f'{_squash("Larsen & Toubro")!r}')
    print('  Neither is a prefix of the other, and neither contains the '
          'other, so\n  only an alias can connect them.')

    found = candidates()
    print(f'\n── {len(found)} account(s) that could be L&T ──')
    for account in found:
        aliases = list(account.aliases or [])
        has = ALIAS.lower() in {str(a).strip().lower() for a in aliases}
        print(f'  #{account.id:<6} {account.name[:38]:<38} '
              f'aliases={aliases or "(none)"}'
              f'{"   <- already has " + ALIAS if has else ""}')
    if not found:
        print('  (none) — there is no Larsen & Toubro account on this '
              'database, so the\n  alias has nowhere to go. Create the '
              'account on Account check first.')

    affected = leads_affected()
    print(f'\n── {len(affected)} lead(s) this would change the answer for ──')
    for lead in affected[:30]:
        print(f'  #{lead.id:<7} {(lead.company or "(no name)")[:28]:<28} '
              f'company_id={lead.company_id or "(none)"}')
    if len(affected) > 30:
        print(f'    … and {len(affected) - 30} more')

    if len(found) == 1:
        account = found[0]
        aliases = {str(a).strip().lower() for a in (account.aliases or [])}
        if ALIAS.lower() in aliases:
            print(f'\n  {ALIAS} is already an alias of #{account.id}. '
                  f'If "Lntecc" still did not\n  propose a link, the '
                  f'cause is elsewhere — send the --check output.')
        else:
            print(f'\n  [WOULD] add the alias {ALIAS!r} to #{account.id} '
                  f'{account.name!r}')
            print('  Then `accounts_module2 --check` proposes the link, '
                  'and a person\n  confirms it on Account check. This '
                  'script links no lead itself.')
    elif len(found) > 1:
        print(f'\n  [STOP] {len(found)} accounts could be L&T. This run '
              f'will not choose:\n  which one owns the alias decides '
              f'where every future "Lntecc" lead\n  lands. Merge them '
              f'first on /admin/account-duplicates, or run this with\n'
              f'  --account <id>.')
    return found, affected


def apply_it(account, actor):
    from app.services import audit

    aliases = list(account.aliases or [])
    if ALIAS.lower() in {str(a).strip().lower() for a in aliases}:
        print(f'  [skip] #{account.id} already has {ALIAS}.')
        return 0
    before = list(aliases)
    aliases.append(ALIAS)
    # Reassigned rather than appended in place: `aliases` is a JSON
    # column, and SQLAlchemy does not see a mutation of the list it
    # handed out. An append alone saves nothing and reports success.
    account.aliases = aliases
    audit.record('account.alias_added', 'company', str(account.id),
                 old={'aliases': before}, new={'aliases': aliases},
                 actor=actor,
                 reason=(f'{ACTOR}: "Lntecc" could not reach '
                         f'{account.name} without it'),
                 commit=False)
    db.session.commit()
    print(f'  [DONE] {ALIAS} added to #{account.id} {account.name!r}')
    print(f'  aliases now: {aliases}')
    return 1


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--actor')
    ap.add_argument('--account', type=int,
                    help='the account id, where more than one could be '
                         'L&T')
    args = ap.parse_args()

    with app.app_context():
        print(f'database: {db.engine.url}')
        found, _affected = report()

        if not args.apply:
            db.session.rollback()
            print('\n' + '─' * 64)
            print('DRY RUN ONLY. NOTHING CHANGED.')
            return 0

        if not args.actor:
            print('\n  --apply needs --actor <employee code>.')
            return 1

        if args.account:
            account = db.session.get(Company, args.account)
            if account is None:
                print(f'\n  [STOP] no account #{args.account}.')
                return 1
        elif len(found) == 1:
            account = found[0]
        else:
            print(f'\n  [STOP] {len(found)} candidate(s). Name one with '
                  f'--account <id>.')
            return 1

        apply_it(account, args.actor)
        print('\n  Now run:  scripts/2026_11_02_accounts_module2.py '
              '--check --csv')
        print('  The L&T leads should move from "create" to a proposed '
              'link.')
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
