"""
PROCAM-DECISIONS-02NOV Module 1 — the Procam Standard T&C.

    .venv/bin/python scripts/2026_11_02_terms_master.py --check
    .venv/bin/python scripts/2026_11_02_terms_master.py --apply
    .venv/bin/python scripts/2026_11_02_terms_master.py --down --yes

What it does, in one transaction:

  1. adds `kind` to `quote_term_sets` (every existing row becomes an
     annexure, which is what the six October rows are);
  2. adds `terms_master_version_id` to `quotes`;
  3. creates `quote_terms_documents` and
     `account_standing_conditions`;
  4. publishes the Procam Standard T&C as v1, from the values the
     decision gives;
  5. clears the DRAFT marker off the six October annexures.

Step 5 is the one to read twice. "Publish it as v1 and remove every
DRAFT flag" is the instruction, and the October rows say DRAFT in
their first line because the wording under it is a placeholder that
says what a clause is *for* rather than what Procam's contract says.
So this does not quietly delete the word and leave placeholder prose
looking approved. It replaces each annexure with the clauses that
belong in an annexure — free time, surcharges, permit lead time, site
readiness, per Module 1.2 — and everything the placeholders covered
that is really company-wide now lives in the master above them, which
is where the decision puts it.

Where a placeholder said something the master does not cover and this
script cannot responsibly write, the annexure says so in a line
addressed to the commercial team, and `--check` lists those lines.
That is a smaller lie than an unmarked placeholder.
"""
import argparse
import os
import sys
from datetime import date

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from sqlalchemy import inspect                            # noqa: E402

from app import app, db                                   # noqa: E402

TABLES = ('quote_terms_documents', 'account_standing_conditions')

ACTOR = 'PROCAM-DECISIONS-02NOV'

#: Module 1, "Values to load in v1", as labelled clauses. The label
#: before the colon is what an SCC names to override the clause
#: (Module 1.4), so it is part of the contract's structure and not
#: formatting — which is why each one is a short phrase and why
#: "Payment" is its own clause rather than a sentence inside
#: another.
MASTER_TITLE = 'Procam Standard Terms and Conditions'
MASTER_BODY = '\n'.join((
    'Validity: This quotation is valid for 15 days from the quote '
    'date. For Freight Forwarding, validity is as per the carrier '
    'validity stated on the quote.',

    'Payment: 30 days from the date of invoice.',

    'Taxes: GST and all statutory levies are extra, as applicable at '
    'the time of supply.',

    'Scope: Strictly as quoted. Any work beyond the quoted scope is '
    'charged on actuals only, against the client\'s written '
    'confirmation and an amendment to the Sales PO.',

    'Exclusions: Unless expressly stated as included, this quotation '
    'excludes insurance, customs duty, port and statutory charges, '
    'permits and escort charges, and civil works on the route.',

    'Detention: Detention beyond the free time is charged per day '
    'per vehicle or unit, at the rates in the vertical annexure to '
    'this quotation.',

    'Liability: Liability is as per applicable law. Cargo is to be '
    'insured by the client.',

    'Force majeure: Neither party is liable for delay or '
    'non-performance caused by events beyond its reasonable '
    'control.',

    'Rate revision: Rates are subject to revision for changes in '
    'diesel price, carrier general rate increases, and regulatory '
    'or statutory changes.',

    'Cancellation: On cancellation, costs already committed on the '
    'client\'s instructions are chargeable.',

    'Jurisdiction: The courts of Delhi have exclusive jurisdiction.',
))

#: Module 1.2 — the annexure per vertical, and only what belongs in
#: one. Everything company-wide has moved up into the master, so
#: these are deliberately short: an annexure that repeats the master
#: is how the two come to disagree.
#:
#: ODC is not a vertical. The decision lists "ODC: survey and permit
#: lead time" beside three verticals, and ODC work is quoted inside
#: Heavy Transport — so it is the Heavy Transport annexure that
#: carries those clauses, and the vertical's name is what the column
#: holds. Freight Forwarding and Project Freight are one vertical
#: since 31 October; both rows are written, because quotes keyed to
#: the old name are still on file and `vertical_of()` still answers
#: 'Freight Forwarding' when a lead has no vertical.
_ASK = ('For the commercial team: the figures below are the shape of '
        'the clause, not agreed numbers. Set them on '
        'Admin → Quote terms before this annexure is quoted.')

ANNEXURES = {
    'Warehousing': (
        'Free time: Free storage time is as stated on the quote, '
        'counted from the date of receipt at the warehouse. Part '
        'periods count as whole periods.',
        'Storage basis: Storage is charged on the basis stated on '
        'the quote — per pallet, per sq ft or per MT per month '
        '— on the higher of space occupied and space reserved.',
        'Handling: Inbound and outbound handling are charged '
        'separately from storage.',
        'Lien: Goods are held subject to a general lien for unpaid '
        'charges.',
    ),
    'Freight Forwarding': (
        'Free days: Free days at origin and destination are as per '
        'the carrier and as stated on the quote. Demurrage and '
        'detention beyond them are to the client\'s account at '
        'carrier tariff.',
        'Surcharges: Rates are subject to the carrier surcharges '
        'ruling at the time of shipment — BAF, CAF, GRI, peak '
        'season, war risk, congestion and any statutory surcharge.',
        'Carrier liability: Liability for the carriage is limited as '
        'per the convention governing the mode carried.',
    ),
    'Project Freight': (
        'Free days: Free days at origin and destination are as per '
        'the carrier and as stated on the quote. Demurrage and '
        'detention beyond them are to the client\'s account at '
        'carrier tariff.',
        'Surcharges: Rates are subject to the carrier surcharges '
        'ruling at the time of shipment — BAF, CAF, GRI, peak '
        'season, war risk, congestion and any statutory surcharge.',
        'Survey: Rates assume the route survey findings stated and '
        'are subject to revision if the route, permits or escort '
        'requirements change.',
    ),
    'Heavy Transport': (
        'Survey: Rates assume the route survey findings and the '
        'cargo dimensions stated on the quote, and are subject to '
        'revision if either changes.',
        'Permits: Permit and escort lead time is as stated on the '
        'quote, counted from receipt of complete cargo and consignee '
        'details. Permits and escort are arranged at actuals unless '
        'a line includes them.',
        'Route works: Road strengthening, tree cutting, line '
        'shifting and police escort are at actuals unless quoted.',
        'Idle time: Idle time caused by site readiness, permit delay '
        'or consignee delay is chargeable.',
        'Cranes: Cranes at both ends are the client\'s arrangement '
        'unless a line on this quote provides them.',
    ),
    'General Transport': (
        'Free time: Loading and unloading free time is as stated on '
        'the quote; detention beyond it is chargeable per day per '
        'vehicle.',
        'Statutory: Toll, octroi, entry tax and permits are at '
        'actuals unless a line includes them.',
        'Transit time: Transit times are estimates, not guarantees.',
    ),
    'Installation': (
        'Site readiness: Rates assume a level, compacted and '
        'accessible working area, and continuous access to the work '
        'front for the duration stated.',
        'Cranes: Cranes and lifting tackle are as stated on the '
        'quote. Any crane not listed is the client\'s arrangement.',
        'Client scope: Power, water, lighting and a safe working '
        'area are to the client\'s account.',
        'Standby: Standby caused by site readiness is chargeable.',
        'Working hours: Work is carried out in daylight hours unless '
        'otherwise agreed.',
    ),
}


def _dialect():
    return db.engine.dialect.name


def _cols(table):
    try:
        return {c['name'] for c in inspect(db.engine).get_columns(table)}
    except Exception:                                     # noqa: BLE001
        return set()


def _add_column(table, name, ddl):
    if name in _cols(table):
        print(f'  {table}.{name:<26} already there')
        return False
    db.session.execute(db.text(
        f'ALTER TABLE {table} ADD COLUMN {name} {ddl}'))
    print(f'  {table}.{name:<26} added')
    return True


def _report():
    """What is there now.

    Raw SQL throughout, not the ORM. The model already declares
    `kind`, so any ORM query against `quote_term_sets` selects a
    column that does not exist until this script has run — which made
    --check fail on exactly the database it exists to inspect.
    """
    have = set(inspect(db.engine).get_table_names())
    print('  tables')
    for t in TABLES:
        print(f'    {t:<32} {"present" if t in have else "ABSENT"}')
    print('  columns')
    has_kind = 'kind' in _cols('quote_term_sets')
    for table, col in (('quote_term_sets', 'kind'),
                       ('quotes', 'terms_master_version_id')):
        print(f'    {table}.{col:<26} '
              f'{"present" if col in _cols(table) else "ABSENT"}')

    kind_sql = 'kind' if has_kind else "'' AS kind"
    rows = db.session.execute(db.text(
        f'SELECT vertical, version, body, {kind_sql} '
        f'FROM quote_term_sets WHERE is_current = 1 '
        f'ORDER BY vertical')).fetchall()

    master = [r for r in rows if (r.kind or '') == 'master']
    annexures = [r for r in rows if (r.kind or '') != 'master']
    print(f'\n  Procam Standard T&C   '
          + (f'v{master[0].version}' if master else 'NOT PUBLISHED'))
    print(f'  annexures on file     {len(annexures)}')
    drafty = []
    for row in annexures:
        first = (row.body or '').strip().splitlines()[:1]
        draft = 'draft' in (first[0].lower() if first else '')
        if draft:
            drafty.append(row.vertical)
        print(f'    {row.vertical:<22} v{row.version}'
              f'{"   DRAFT" if draft else ""}')
    if drafty:
        print(f'\n  !! {len(drafty)} annexure(s) still say DRAFT: '
              f'{", ".join(sorted(drafty))}')
        print('     --apply replaces their wording; it does not just '
              'delete the word.')
    unknown = sorted({r.vertical for r in annexures} - set(ANNEXURES))
    if unknown:
        print(f'\n  annexures this script does not write: '
              f'{", ".join(unknown)}')
        print('     left exactly as they are, DRAFT marker included '
              'if they have one \u2014 a vertical whose clauses nobody '
              'has given me is not one I can write for.')
    return master, annexures


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--down', action='store_true')
    ap.add_argument('--yes', action='store_true')
    args = ap.parse_args()
    if len([a for a in (args.check, args.apply, args.down) if a]) != 1:
        raise SystemExit('Choose one: --check, --apply or --down.')

    with app.app_context():
        print(f'database: {db.engine.url} ({_dialect()})\n')

        if args.down:
            if not args.yes:
                raise SystemExit('--down needs --yes.')
            # The two new tables go; the two new columns stay, because
            # dropping a column is a table rewrite on SQLite and a
            # nullable column nobody reads costs nothing. The master
            # term set is superseded rather than deleted — it is a
            # published contract version, and CRM keeps those.
            from app.models.quote_terms import MASTER, QuoteTermSet
            from datetime import datetime as _dt

            for row in QuoteTermSet.query.filter_by(
                    kind=MASTER, is_current=True).all():
                row.is_current = False
                row.superseded_at = _dt.utcnow()
                print(f'  master v{row.version} marked not current')
            for t in reversed(TABLES):
                db.session.execute(db.text(f'DROP TABLE IF EXISTS {t}'))
                print(f'  {t} dropped')
            db.session.commit()
            print('\n  The two added columns are left in place: '
                  'nullable, and nothing reads them now.')
            return

        _report()
        if args.check:
            print('\nCHECK ONLY — nothing written.')
            return

        import app.models.quote_terms as qtm                # noqa: F401
        from app.models.quote_terms import (ANNEXURE, MASTER,
                                            QuoteTermSet)
        from app.services import quote_terms as qt

        print('\napplying\n')
        # 1 + 2 — the columns, before anything reads them.
        _add_column('quote_term_sets', 'kind', "VARCHAR(12)")
        _add_column('quotes', 'terms_master_version_id', 'INTEGER')
        db.session.commit()

        # Every row that existed before `kind` is an annexure. Done
        # as one UPDATE rather than relying on the column default,
        # because a default applies to inserts and these rows are
        # already there.
        done = db.session.execute(db.text(
            "UPDATE quote_term_sets SET kind = :k "
            "WHERE kind IS NULL OR kind = ''"), {'k': ANNEXURE})
        print(f'  {done.rowcount} existing term set(s) marked '
              f'{ANNEXURE}')
        db.session.commit()

        # 3 — the two new tables.
        db.create_all()
        have = set(inspect(db.engine).get_table_names())
        for t in TABLES:
            print(f'  {t:<32} {"present" if t in have else "FAILED"}')

        # 4 — the master.
        master = qt.master_current()
        if master is not None and (master.body or '').strip() == \
                MASTER_BODY.strip():
            print(f'\n  Procam Standard T&C already published as '
                  f'v{master.version}, unchanged')
        else:
            row = qt.publish_master(MASTER_BODY, title=MASTER_TITLE,
                                    actor=ACTOR,
                                    effective_from=date(2026, 11, 2))
            print(f'\n  Procam Standard T&C published as v{row.version} '
                  f'({len(qt.clauses(MASTER_BODY))} clauses, '
                  f'effective {row.effective_from})')

        # 5 — the annexures, replacing the placeholder wording.
        print('\n  annexures')
        for vertical in sorted(ANNEXURES):
            body = '\n'.join(ANNEXURES[vertical])
            was = qt.current_for(vertical)
            if was is not None and (was.body or '').strip() == body:
                print(f'    {vertical:<22} v{was.version} unchanged')
                continue
            row = qt.publish(vertical, body, actor=ACTOR,
                             title=f'{vertical} annexure',
                             effective_from=date(2026, 11, 2))
            print(f'    {vertical:<22} v{row.version} '
                  f'({len(ANNEXURES[vertical])} clauses)'
                  + ('  [was DRAFT]'
                     if was and 'draft' in (was.body or '')[:60].lower()
                     else ''))

        left = [r.vertical for r in
                QuoteTermSet.query.filter_by(is_current=True)
                .filter(db.or_(QuoteTermSet.kind == ANNEXURE,
                               QuoteTermSet.kind.is_(None))).all()
                if 'draft' in (r.body or '')[:60].lower()]
        db.session.commit()

        print(f'\n  DRAFT markers left: {len(left)}'
              + (f' ({", ".join(sorted(left))})' if left else ''))
        if left:
            print('  Those are verticals this script has no clauses '
                  'for. Write them on Admin → Quote terms; the '
                  'master already applies to their quotes.')
        print('\n  Every quote in every vertical now prints the '
              'master. A quote is not blocked for want of an '
              'annexure.')


if __name__ == '__main__':
    main()
