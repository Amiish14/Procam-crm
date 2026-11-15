"""CRM-SEARCH-SCAN-01 Step 0 — what is there now. READ ONLY.

Everything the spec's first step asks for, from the live database and
the live configuration, in one pass:

  1. the fields that exist on Contact / Company / Lead for name,
     company, designation, country, city/state, industry/vertical,
     phones and emails -- and HOW MANY ROWS HAVE EACH FILLED;
  2. where industry and vertical actually live, and the values in
     use, counted;
  3. what the global search can reach today, with the query plan of
     the query it runs;
  4. the card scanner: how many cards, what came of them, whether
     the image service is configured, and the field hit-rate of the
     extractions already on file.

    .venv/bin/python scripts/2026_11_15_search_scan_survey.py --check

It writes nothing. No `--apply` exists. Secrets are never printed:
the configuration questions are answered yes or no.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from app import app, db                                    # noqa: E402

#: (model, column) per question the spec asks about.
WANTED = {
    'name': [('Contact', 'name'), ('Lead', 'pic')],
    'company (text)': [('Contact', 'company'), ('Lead', 'company')],
    'company (account link)': [('Contact', 'company_id'),
                               ('Contact', 'account_id'),
                               ('Lead', 'company_id')],
    'designation': [('Contact', 'designation'), ('Lead', 'designation')],
    'country': [('Contact', 'country'), ('Company', 'country')],
    'city': [('Contact', 'city'), ('Company', 'city')],
    'state': [('Contact', 'state'), ('Company', 'state')],
    'industry': [('Contact', 'industry'), ('Company', 'industry')],
    'vertical': [('Company', 'vertical'), ('Lead', 'procam_vertical')],
    'phone': [('Contact', 'phone'), ('Contact', 'mobile'),
              ('Company', 'phone'), ('Lead', 'phone'),
              ('Lead', 'phone2')],
    'email': [('Contact', 'email'), ('Company', 'email'),
              ('Lead', 'email')],
    'owner': [('Contact', 'assigned_to'), ('Lead', 'assigned_to')],
    'website / social': [('Contact', 'website'), ('Contact', 'linkedin'),
                         ('Company', 'website'), ('Company', 'linkedin')],
}


def _rule(n=76):
    print('─' * n)


def _model(name):
    import app as _app

    return getattr(_app, name, None)


def _filled(model, column):
    """(rows, filled) — non-null and non-blank."""
    col = getattr(model, column, None)
    if col is None:
        return None, None
    total = model.query.count()
    try:
        filled = (model.query
                  .filter(col.isnot(None))
                  .filter(db.func.trim(db.cast(col, db.String)) != '')
                  .count())
    except Exception:                                     # noqa: BLE001
        db.session.rollback()
        filled = model.query.filter(col.isnot(None)).count()
    return total, filled


def report_fields():
    print('\n══ 1 · the fields, and how many rows carry one ══')
    print(f'\n  {"what":<24} {"model.column":<28} {"filled":>9} '
          f'{"of":>8}   %')
    for what, pairs in WANTED.items():
        for model_name, column in pairs:
            model = _model(model_name)
            if model is None:
                print(f'  {what:<24} {model_name + "." + column:<28} '
                      f'{"— no such model":>9}')
                continue
            total, filled = _filled(model, column)
            if total is None:
                print(f'  {what:<24} {model_name + "." + column:<28} '
                      f'{"— no such column":>18}')
                continue
            pct = (100.0 * filled / total) if total else 0.0
            print(f'  {what:<24} {model_name + "." + column:<28} '
                  f'{filled:>9} {total:>8}  {pct:5.1f}%')
        print()


def report_gaps():
    """The three the spec wants a fill script for."""
    from app import Contact

    print('\n══ 1b · contacts missing the search fields ══')
    total = Contact.query.count()
    if not total:
        print('  no contacts on file.')
        return
    blank = {}
    for column in ('country', 'city', 'state', 'industry'):
        col = getattr(Contact, column, None)
        if col is None:
            continue
        blank[column] = (Contact.query
                         .filter(db.or_(col.is_(None),
                                        db.func.trim(col) == ''))
                         .count())
    for column, n in blank.items():
        print(f'  {n:>8} of {total} contacts have no {column} '
              f'({100.0 * n / total:.1f}%)')

    # What could be filled, and from where.
    fillable_country = (Contact.query
                        .filter(db.or_(Contact.country.is_(None),
                                       db.func.trim(Contact.country) == ''))
                        .filter(db.or_(
                            db.and_(Contact.mobile.isnot(None),
                                    Contact.mobile.like('+%')),
                            db.and_(Contact.phone.isnot(None),
                                    Contact.phone.like('+%'))))
                        .count())
    print(f'\n  {fillable_country} of them have a phone number starting '
          f'"+", so a country could be read off the dialling code.')
    linked = (Contact.query
              .filter(db.or_(Contact.industry.is_(None),
                             db.func.trim(Contact.industry) == ''))
              .filter(db.or_(Contact.account_id.isnot(None),
                             Contact.company_id.isnot(None)))
              .count())
    print(f'  {linked} with no industry are linked to an account, '
          f'which may have one.')


def report_vocabulary():
    from app import Company, Contact, Lead

    print('\n══ 2 · where industry and vertical live, and their values ══')
    for model, column, what in (
            (Contact, 'industry', 'Contact.industry (free text)'),
            (Company, 'industry', 'Company.industry (free text)'),
            (Company, 'vertical', 'Company.vertical (the desk)'),
            (Lead, 'procam_vertical', 'Lead.procam_vertical (the desk)')):
        col = getattr(model, column, None)
        if col is None:
            continue
        rows = Counter()
        for (value,) in db.session.query(col).all():
            text = (value or '').strip()
            if text:
                rows[text] += 1
        print(f'\n  {what} — {len(rows)} distinct value(s)')
        for value, n in rows.most_common(15):
            print(f'    {n:>7}  {value[:56]}')
        if len(rows) > 15:
            print(f'    … and {len(rows) - 15} more')

    try:
        from app.master_data import service as md
        items = md.items('vertical')
        print(f'\n  The master list (Admin → Master data → '
              f'procam_vertical): {len(items)} entries')
        for item in items:
            print(f'    {item.code}')
    except Exception as exc:                              # noqa: BLE001
        print(f'\n  could not read the vertical master list: '
              f'{type(exc).__name__}')


def report_search():
    print('\n══ 3 · what the search reaches today ══')
    print('  route     GET /api/crm2/search?q=…  (behind the CRM2 flag)')
    print('  minimum   2 characters; 8 rows per kind; no paging')
    print('  contacts  matched on name and email ONLY — not company,')
    print('            designation, country, city, state or industry')
    print('  leads     company, pic, email, crn, opp_number')
    print('  accounts  name, city')
    print('  match     LIKE %q% on each, case-insensitive')

    from app import Contact

    print('\n  the plan the contact half of that query runs:')
    like = '%steel%'
    query = (Contact.query
             .filter(db.or_(Contact.name.ilike(like),
                            Contact.email.ilike(like)))
             .order_by(Contact.name.asc()).limit(8))
    _explain(query)

    print('\n  and the plan the new search would need (name, company,')
    print('  designation, country, city, state, industry at once):')
    wide = (Contact.query
            .filter(db.or_(Contact.name.ilike(like),
                           Contact.company.ilike(like),
                           Contact.designation.ilike(like),
                           Contact.country.ilike(like),
                           Contact.city.ilike(like),
                           Contact.state.ilike(like),
                           Contact.industry.ilike(like)))
            .order_by(Contact.name.asc()).limit(50))
    _explain(wide)

    print('\n  indexes on contacts today:')
    try:
        rows = db.session.execute(
            db.text("SELECT name, sql FROM sqlite_master "
                    "WHERE type='index' AND tbl_name='contacts'")).all()
        if not rows:
            print('    none beyond the primary key.')
        for name, sql in rows:
            print(f'    {name}: {(sql or "implicit")[:70]}')
    except Exception as exc:                              # noqa: BLE001
        print(f'    not SQLite, or unreadable: {type(exc).__name__}')


def _explain(query):
    dialect = db.engine.dialect.name
    statement = query.statement.compile(
        db.engine, compile_kwargs={'literal_binds': True})
    verb = ('EXPLAIN QUERY PLAN' if dialect == 'sqlite' else 'EXPLAIN')
    try:
        rows = db.session.execute(db.text(f'{verb} {statement}')).all()
        for row in rows:
            print(f'    {" | ".join(str(x) for x in row)[:110]}')
    except Exception as exc:                              # noqa: BLE001
        print(f'    could not explain: {type(exc).__name__}: '
              f'{str(exc)[:90]}')


def report_scanner():
    from app.models.business_card import BusinessCardImport

    print('\n══ 4 · the card scanner ══')
    print('  engine    app/services/business_card_ocr.py calls an '
          'outside')
    print('            image service over HTTPS with the card image, '
          'and falls')
    print('            back to app/services/card_parse.py when that is '
          'unavailable.')
    print('            card_parse reads TEXT only: it cannot see an '
          'image.')
    key = bool(os.environ.get('ANTHROPIC_API_KEY'))
    print(f'  key set   {"YES — images ARE being sent off this server" if key else "no — the image path is dead; only typed text is parsed"}')
    print(f'  model     {os.environ.get("BUSINESS_CARD_MODEL") or "(default)"}')

    for module, what in (('PIL', 'resize / rotate'),
                         ('cv2', 'deskew, crop, contrast'),
                         ('pytesseract', 'on-server OCR'),
                         ('pdf2image', 'PDF → image'),
                         ('fitz', 'PDF → image (PyMuPDF)'),
                         ('phonenumbers', 'E.164 phone normalisation')):
        try:
            __import__(module)
            state = 'present'
        except Exception:                                 # noqa: BLE001
            state = 'MISSING'
        print(f'  {module:<14}{state:<9} {what}')

    try:
        total = BusinessCardImport.query.count()
    except Exception:                                     # noqa: BLE001
        db.session.rollback()
        print('\n  the card table does not exist in this database — '
              'the scanner has')
        print('  never been used here, or the table was never created.')
        return
    print(f'\n  {total} card(s) on file')
    if not total:
        print('  Nothing stored, so nothing to re-test against. The '
              'spec\'s 10-card set has to be supplied or photographed.')
        return
    by_status = Counter(
        row.status or '(none)'
        for row in BusinessCardImport.query.all())
    for status, n in by_status.most_common():
        print(f'    {n:>6}  {status}')

    with_image = (BusinessCardImport.query
                  .filter(BusinessCardImport.image_path.isnot(None))
                  .count())
    print(f'    {with_image:>6}  have the image kept')

    fields = Counter()
    cards = 0
    for row in BusinessCardImport.query.all():
        found = row.extracted_json or {}
        if not isinstance(found, dict) or not found:
            continue
        cards += 1
        for key_name, value in found.items():
            if isinstance(value, str) and value.strip():
                fields[key_name] += 1
    if cards:
        print(f'\n  field hit-rate over {cards} extraction(s) on file:')
        for key_name, n in fields.most_common():
            print(f'    {100.0 * n / cards:5.1f}%  {key_name}')
        print('  (what the extractor FILLED, not whether it was right '
              '— correctness')
        print('   needs the card beside it, which is the 10-card test '
              'set.)')


def report_engine():
    print('\n══ 0 · the database this all has to run on ══')
    url = str(db.engine.url)
    print(f'  {url}')
    print(f'  dialect: {db.engine.dialect.name}')
    if db.engine.dialect.name != 'postgresql':
        print('  !! The spec asks for Postgres pg_trgm similarity and '
              'indexes.')
        print('     This is not Postgres, so pg_trgm does not exist '
              'here. See the')
        print('     report: the same tolerance is reachable another '
              'way, or the')
        print('     database moves — and that is a decision, not a '
              'detail.')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--check', action='store_true', required=True,
                    help='required: this script only ever reads')
    ap.parse_args(argv)

    with app.app_context():
        print('CRM-SEARCH-SCAN-01 — Step 0 survey. READ ONLY.')
        _rule()
        # Each section on its own: a survey that dies half way through
        # on one missing table tells you less than one that says which
        # part it could not read.
        for section in (report_engine, report_fields, report_gaps,
                        report_vocabulary, report_search,
                        report_scanner):
            try:
                section()
            except Exception as exc:                      # noqa: BLE001
                db.session.rollback()
                print(f'\n  !! {section.__name__} could not run: '
                      f'{type(exc).__name__}: {str(exc)[:120]}')
        print()
        db.session.rollback()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
