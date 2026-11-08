"""Nothing on a quote should still say DRAFT. DRY RUN BY DEFAULT.

CRM-FINAL-CLOSEOUT-01 §4: "Terms: T&C master v1 was published 2 Nov —
point quotes at it; nothing should still say DRAFT."

    .venv/bin/python scripts/2026_11_08_no_draft_terms.py --check
    .venv/bin/python scripts/2026_11_08_no_draft_terms.py --apply

TWO HALVES, AND ONLY ONE OF THEM NEEDS ANYTHING DONE

**Pointing quotes at the master** is already built and already
running. `quote_build` calls `quote_terms.attach_to`, which stamps
`terms_master_version_id` on every quote it builds, and
`quote_terms.for_print` — used by the quote screen, the PDF, the
emailed quotation and the job handover — prints the master, then the
vertical's annexure under it. This script checks that and reports it
rather than changing it: a release note that claims a thing is wired
is worth less than a command that says so from the database.

**The DRAFT markers** are the part with a decision in them. The 2
November migration replaced the October placeholders with real
annexure clauses, except one: Sea Freight has a term set that nobody
has given clauses for, and QUESTIONS 53 recorded it as deliberately
left alone, marker included, because an unmarked placeholder is worse
than a marked one.

"Nothing should still say DRAFT" closes that. The way to close it is
not to write Sea Freight's contract language — this script cannot and
should not — but to **retire the placeholder annexure**, so quotes in
that vertical print the Procam Standard T&C alone. The master is real,
approved wording: validity, payment, taxes, scope, liability. A quote
carrying it and nothing else is a correct quote. A quote carrying it
with a placeholder stapled underneath is not.

So: the master stays, the placeholder goes, and the vertical is named
in the output so the commercial team knows exactly which annexure
they still owe. Nothing is deleted — the row is superseded with no
successor, which is what "retired rather than replaced" looks like in
the data, and a quote printed under it can still say what it printed
under.

WHAT IT WILL NOT DO

It will not retire the master, whatever it says. A quote with no
master prints no standard terms at all.

It will not strip the word DRAFT and leave the prose. That is the one
change that makes a placeholder look approved, and it is the failure
the 2 November migration was careful to avoid.
"""
from __future__ import annotations

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from app import app, db                                    # noqa: E402

ACTOR = 'FINAL-CLOSEOUT-01'

#: How much of a term set to look at for the marker. The 2 November
#: migration used the same window: a placeholder announces itself in
#: its first line, and the word appearing deep in a real clause ("a
#: draft bill of lading") is not a placeholder.
HEAD = 400


def _is_draft(row):
    return 'draft' in (row.body or '')[:HEAD].lower()


def survey():
    """What the terms library holds, and what still says DRAFT."""
    from app.models.quote_terms import ANNEXURE, MASTER, QuoteTermSet
    from app.services import quote_terms as qt

    master = qt.master_current()
    rows = (QuoteTermSet.query
            .filter_by(is_current=True)
            .order_by(QuoteTermSet.vertical.asc())
            .all())
    annexures = [r for r in rows if (r.kind or ANNEXURE) != MASTER]
    return {
        'master': master,
        'master_draft': bool(master is not None and _is_draft(master)),
        'annexures': annexures,
        'drafts': [r for r in annexures if _is_draft(r)],
    }


def quote_coverage():
    """How many quotes carry a master version, and how many do not.

    The ones that do not are quotes built before the master existed.
    They are not broken: `master_on()` falls back to today's master
    for a quote with no stamp, so they print correctly — but the
    count is worth seeing, because it is the number of quotes whose
    printed terms cannot be reproduced exactly from the database.
    """
    from app.models.quote import Quote

    try:
        total = Quote.query.count()
        stamped = Quote.query.filter(
            Quote.terms_master_version_id.isnot(None)).count()
    except Exception as exc:                              # noqa: BLE001
        return {'error': f'{type(exc).__name__}: {exc}'}
    return {'total': total, 'stamped': stamped,
            'unstamped': total - stamped}


def wiring():
    """Whether quotes are actually pointed at the master, in code.

    Read off the source rather than asserted in prose. If somebody
    removes the `attach_to` call, this says so on the next run
    instead of the next argument with a customer.
    """
    out = {}
    for label, path, needle in (
            ('the builder stamps the master',
             'app/services/quote_build.py', 'attach_to'),
            ('the quote screen prints it',
             'app/quote/routes.py', 'for_print'),
            ('the emailed quotation prints it',
             'app/services/quote_send.py', 'for_print'),
            ('the job handover carries it',
             'app/services/job_handover.py', 'for_print')):
        try:
            body = open(os.path.join(_ROOT, path)).read()
        except OSError:
            out[label] = None
            continue
        out[label] = needle in body
    return out


def report(state, coverage, wired):
    master = state['master']
    print('\n── the Procam Standard T&C ──')
    if master is None:
        print('  !! THERE IS NO MASTER. Every quote prints its '
              'vertical\'s annexure alone, or nothing at all.')
        print('     Run scripts/2026_11_02_terms_master.py --apply '
              'first.')
    else:
        print(f'  v{master.version}, effective '
              f'{master.effective_from or "—"}: {master.title}')
        print(f'  {len(master.body.splitlines())} line(s), '
              f'{len(master.body)} characters')
        if state['master_draft']:
            print('  !! the master itself says DRAFT. That is the one '
                  'row this script will not touch.')

    print('\n── per-vertical annexures ──')
    if not state['annexures']:
        print('  none. Every quote prints the master alone, which is '
              'a correct quote.')
    for row in state['annexures']:
        mark = '!!' if _is_draft(row) else '  '
        print(f'  {mark} {row.vertical:<24} v{row.version}  '
              f'{len(row.body)} chars'
              + ('   — still says DRAFT' if _is_draft(row) else ''))

    print('\n── are quotes pointed at it? ──')
    for label, ok in wired.items():
        state_word = ('yes' if ok else
                      'NO' if ok is False else 'could not read')
        print(f'  {state_word:<16} {label}')

    if 'error' in coverage:
        print(f'\n  quotes: could not be counted ({coverage["error"]})')
    else:
        print(f'\n  {coverage["stamped"]} of {coverage["total"]} '
              f'quote(s) carry the master version they printed.')
        if coverage['unstamped']:
            print(f'  {coverage["unstamped"]} predate the master. They '
                  f'print today\'s version, correctly — but their '
                  f'printed terms cannot be reproduced exactly.')
    return state['drafts']


def retire_drafts(drafts, *, apply):
    from app.services import quote_terms as qt

    if not drafts:
        print('\nNothing says DRAFT. There is nothing to do.')
        return []
    print(f'\n{len(drafts)} annexure(s) still say DRAFT:')
    for row in drafts:
        print(f'  {row.vertical} v{row.version}')
        print(f'    first line: {(row.body or "").splitlines()[0][:72]!r}')
    print('\nRetiring an annexure does not delete it. The row is '
          'superseded with no successor, and quotes in that vertical '
          'print the Procam Standard T&C alone — real, approved '
          'wording, rather than a placeholder stapled under it.')
    print('The commercial team still owes clauses for: '
          + ', '.join(sorted(r.vertical for r in drafts)))

    if not apply:
        print('\nDRY RUN — nothing written. --apply to retire them.')
        return []

    done = []
    try:
        for row in list(drafts):
            qt.retire(row.vertical, actor=ACTOR,
                      reason=('retired under CRM-FINAL-CLOSEOUT-01 §4: '
                              'the clauses were placeholders, so quotes '
                              'in this vertical print the Procam '
                              'Standard T&C alone until real wording '
                              'exists'))
            done.append(row.vertical)
        db.session.commit()
    except Exception as exc:                              # noqa: BLE001
        db.session.rollback()
        print(f'\n  !! ROLLED BACK: {type(exc).__name__}: {exc}')
        raise SystemExit(1)
    print(f'\n{len(done)} retired: {", ".join(done)}. Each has an '
          f'audit row saying why.')
    return done


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--check', action='store_true',
                    help='report only. The default.')
    ap.add_argument('--apply', action='store_true',
                    help='retire the annexures that still say DRAFT')
    args = ap.parse_args(argv)

    with app.app_context():
        print(f'database: {db.engine.url}')
        print('CRM-FINAL-CLOSEOUT-01 §4 — the master, and the DRAFT '
              'markers.' + ('' if args.apply else ' REPORT ONLY.'))

        state = survey()
        drafts = report(state, quote_coverage(), wiring())
        retire_drafts(drafts, apply=args.apply)
        if not args.apply:
            db.session.rollback()
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
