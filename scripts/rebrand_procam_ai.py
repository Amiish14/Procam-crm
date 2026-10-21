"""
The assistant is called Procam AI. Say so everywhere a user reads.

    .venv/bin/python scripts/rebrand_procam_ai.py --check
    .venv/bin/python scripts/rebrand_procam_ai.py --apply
    .venv/bin/python scripts/rebrand_procam_ai.py --down --yes

The copilot panel has said "Ask Procam AI" since it was built. The
name leaked in two places a user sees — a textarea placeholder and
a help article — and into 591 stored strings the system wrote
about its own work.

What this deliberately does not touch

  Model identifiers. `claude-haiku-4-5-20251001` is an API value,
  not a brand: renaming it stops the call working.

  `outreach_drafts.model`, which records which model drafted a
  given email. Rewriting it would make the record say something
  untrue about work already done.

  Lead notes, email bodies, copilot chunks. That is correspondence
  and customer text — ours to store, not to edit.

  Code comments and docstrings. They name the vendor whose API
  this calls, which is a fact about the code. A comment saying
  "sends the email to Procam AI" would be wrong, and the next
  person debugging a 401 from Anthropic deserves better.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from sqlalchemy import text                               # noqa: E402

from app import app, db                                   # noqa: E402

OLD, NEW = 'Claude', 'Procam AI'

#: (table, column, what it is). Only text a person reads.
TARGETS = (
    ('help_articles', 'what_it_is', 'the Business Card Scan article'),
    ('account_assignments', 'reason',
     'the reason shown against an auto-assigned account'),
)

#: Phrases that must survive, checked before anything is written.
PROTECTED = ('claude-haiku', 'claude-sonnet', 'claude-opus',
             'anthropic/claude')


def _rows(table, column):
    return db.session.execute(text(
        f'SELECT COUNT(*) FROM {table} '
        f'WHERE {column} LIKE :p'), {'p': f'%{OLD}%'}).scalar()


def _protected_hits(table, column):
    """Rows that also carry a model id, which must not be rewritten."""
    clause = ' OR '.join(f"{column} LIKE :p{i}"
                         for i in range(len(PROTECTED)))
    params = {f'p{i}': f'%{v}%' for i, v in enumerate(PROTECTED)}
    return db.session.execute(text(
        f'SELECT COUNT(*) FROM {table} '
        f'WHERE ({column} LIKE :needle) AND ({clause})'),
        {'needle': f'%{OLD}%', **params}).scalar()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--down', action='store_true')
    ap.add_argument('--yes', action='store_true')
    args = ap.parse_args()
    if len([a for a in (args.check, args.apply, args.down) if a]) != 1:
        raise SystemExit('Choose one: --check, --apply or --down.')

    frm, to = (NEW, OLD) if args.down else (OLD, NEW)

    with app.app_context():
        print(f'database: {db.engine.url}')
        mode = ('APPLY' if args.apply else 'DOWN' if args.down
                else 'DRY RUN — nothing is written')
        print(f'MODE:     {mode}\n')
        print(f'  "{frm}" -> "{to}"\n')

        total = 0
        for table, column, what in TARGETS:
            try:
                n = db.session.execute(text(
                    f'SELECT COUNT(*) FROM {table} '
                    f'WHERE {column} LIKE :p'),
                    {'p': f'%{frm}%'}).scalar()
            except Exception as exc:                      # noqa: BLE001
                print(f'  {table}.{column}: skipped ({str(exc)[:60]})')
                continue
            guarded = _protected_hits(table, column) if not args.down \
                else 0
            print(f'  {table}.{column:<12} {n:>5}  {what}')
            if guarded:
                print(f'    !! {guarded} of them also carry a model '
                      f'id — left alone')
            total += n

            if args.check or not n:
                continue
            guard = ''.join(
                f' AND {column} NOT LIKE :g{i}'
                for i in range(len(PROTECTED)))
            db.session.execute(
                text(f'UPDATE {table} SET {column} = '
                     f'REPLACE({column}, :frm, :to) '
                     f'WHERE {column} LIKE :p' + guard),
                {'frm': frm, 'to': to, 'p': f'%{frm}%',
                 **{f'g{i}': f'%{v}%'
                    for i, v in enumerate(PROTECTED)}})

        if args.check:
            print(f'\n  {total} row(s) would change.')
            print('\nDRY RUN — nothing written. Model ids, draft '
                  'model records, lead notes and email bodies are '
                  'never touched; see the docstring.')
            return

        db.session.commit()
        print(f'\n  {total} row(s) updated.')
        left = sum(_rows(t, c) for t, c, _w in TARGETS)
        print(f'  "{OLD}" now appears in {left} of them.')


if __name__ == '__main__':
    main()
