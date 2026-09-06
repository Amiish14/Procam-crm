"""Excel import — build templates, validate uploads, preview, commit.

§47 is the rule that shapes this module: bulk data is NEVER inserted on
upload.  Parsing and validating produce a preview; a person confirms; only
then is anything written.

§48 modes decide what a confirmed import may do:
    create   create new records only, skip anything that exists
    upsert   create new and update existing
    update   update existing only, skip anything new

§51 — the same company arriving as "Customer" in one file and "Competitor"
in another updates one record's classifications. It never becomes two.
"""
import io
import json
import os
import tempfile
from datetime import datetime

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

from app import db
from app.excel_io.templates import TEMPLATES, lookups_for

MODE_CREATE = 'create'
MODE_UPSERT = 'upsert'
MODE_UPDATE = 'update'
MODES = (MODE_CREATE, MODE_UPSERT, MODE_UPDATE)

_HEAD_FILL = PatternFill('solid', fgColor='0A0B0F')
_HEAD_FONT = Font(color='FFFFFF', bold=True, size=10)
_REQ_FILL = PatternFill('solid', fgColor='FFF1F2')
_NOTE_FONT = Font(italic=True, color='5C616D', size=9)


# ── §43/§44: the downloadable workbook ───────────────────────────────
def build_template(kind):
    spec = TEMPLATES[kind]
    wb = Workbook()

    ws = wb.active
    ws.title = 'DATA'
    headers = [c[0] for c in spec['columns']]
    ws.append(headers)
    for i, (header, _f, required, _lk, example, _h) in enumerate(
            spec['columns'], start=1):
        cell = ws.cell(row=1, column=i)
        cell.fill = _HEAD_FILL
        cell.font = _HEAD_FONT
        cell.alignment = Alignment(vertical='center')
        if required:
            ws.cell(row=2, column=i).fill = _REQ_FILL
        ws.column_dimensions[get_column_letter(i)].width = max(
            14, min(34, len(header) + 6))
    # one example row, clearly labelled so it is deleted rather than
    # imported by accident
    ws.append([c[4] for c in spec['columns']])
    ws.cell(row=2, column=1).comment = None
    ws.freeze_panes = 'A2'

    info = wb.create_sheet('INSTRUCTIONS')
    info.column_dimensions['A'].width = 26
    info.column_dimensions['B'].width = 14
    info.column_dimensions['C'].width = 74
    info.append([spec['label']])
    info['A1'].font = Font(bold=True, size=13)
    info.append([spec['description']])
    info.append([])
    info.append(['DELETE ROW 2 OF THE DATA SHEET BEFORE UPLOADING — '
                 'it is an example, not data.'])
    info['A4'].font = Font(bold=True, color='CC1E2E')
    info.append([])
    info.append(['Column', 'Required', 'Notes'])
    for c in info[info.max_row]:
        c.fill, c.font = _HEAD_FILL, _HEAD_FONT
    for header, _f, required, lookup, _e, help_text in spec['columns']:
        note = help_text or ''
        if lookup:
            note = (note + ' ' if note else '') + \
                   f'Allowed values are on the LOOKUPS sheet ({lookup}).'
        info.append([header, 'Required' if required else 'Optional', note])
    if spec['notes']:
        info.append([])
        info.append(['How this import behaves'])
        info[info.max_row][0].font = Font(bold=True)
        for note in spec['notes']:
            info.append(['', '', note])
            info[info.max_row][2].font = _NOTE_FONT

    lookups = lookups_for(kind)
    if lookups:
        sheet = wb.create_sheet('LOOKUPS')
        col = 1
        for name, values in lookups.items():
            cell = sheet.cell(row=1, column=col, value=name)
            cell.fill, cell.font = _HEAD_FILL, _HEAD_FONT
            sheet.column_dimensions[get_column_letter(col)].width = 30
            for r, v in enumerate(values, start=2):
                sheet.cell(row=r, column=col, value=v)
            col += 1

    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    return out


# ── parsing and validation ───────────────────────────────────────────
def _norm_header(h):
    return (str(h or '')).strip().lower().replace('_', ' ')


def parse(file_storage, kind):
    """Read the DATA sheet into dicts keyed by template field."""
    spec = TEMPLATES[kind]
    wb = load_workbook(file_storage, data_only=True, read_only=True)
    ws = wb['DATA'] if 'DATA' in wb.sheetnames else wb.worksheets[0]

    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return [], ['The sheet is empty.']

    header_row = [_norm_header(h) for h in rows[0]]
    wanted = {_norm_header(c[0]): c[1] for c in spec['columns']}

    missing = [c[0] for c in spec['columns']
               if c[2] and _norm_header(c[0]) not in header_row]
    if missing:
        return [], [f'Required column(s) missing: {", ".join(missing)}. '
                    f'Download a fresh template.']

    index = {}
    for pos, head in enumerate(header_row):
        if head in wanted:
            index[wanted[head]] = pos

    out = []
    for n, raw in enumerate(rows[1:], start=2):
        record = {}
        for field, pos in index.items():
            value = raw[pos] if pos < len(raw) else None
            record[field] = (str(value).strip()
                             if value is not None else '')
        if not any(record.values()):
            continue
        record['_row'] = n
        out.append(record)
    return out, []


def validate(records, kind, mode):
    """Check every row without writing anything (§46, §47)."""
    from app import Company, Contact, Employee
    from app.services.company_match import build_index, match

    spec = TEMPLATES[kind]
    lookups = {name: {v.lower() for v in values}
               for name, values in lookups_for(kind).items()}
    required = [(c[0], c[1]) for c in spec['columns'] if c[2]]
    lookup_cols = [(c[0], c[1], c[3]) for c in spec['columns'] if c[3]]

    company_index = build_index(
        Company.query.filter(Company.is_active.is_(True)).all())
    known_emp = {e.emp_code for e in Employee.query.all()}
    emails = {c.email.lower() for c in Contact.query.filter(
        Contact.email.isnot(None)).all() if c.email}

    seen_in_file = {}
    results = []
    for record in records:
        errors, warnings = [], []

        for header, field in required:
            if not record.get(field):
                errors.append(f'{header} is required')

        for header, field, lookup in lookup_cols:
            raw = record.get(field) or ''
            if not raw:
                continue
            allowed = lookups.get(lookup, set())
            for part in [p.strip() for p in raw.split(',') if p.strip()]:
                if allowed and part.lower() not in allowed:
                    errors.append(
                        f'{header} "{part}" is not a known {lookup} — '
                        f'add it under Master Data first, or correct it')

        for field, label in (('pic_emp_code', 'Account Owner'),
                             ('assigned_to', 'Assigned To')):
            code = record.get(field)
            if code and code not in known_emp:
                errors.append(f'{label} "{code}" is not an employee code')

        # what this row will do
        action, existing_id = 'create', None
        name = record.get('name') or record.get('company') or ''
        if name:
            key = name.strip().lower()
            if key in seen_in_file:
                warnings.append(
                    f'Same company appears on row {seen_in_file[key]} — '
                    f'they will be merged into one record')
            else:
                seen_in_file[key] = record['_row']

            found, reason, _c = match(name, company_index)
            if found is not None:
                existing_id, action = found.id, 'update'
                if found.name.strip().lower() != key:
                    warnings.append(
                        f'Matches existing company "{found.name}"')

        email = (record.get('email') or record.get('_person_email') or '')
        if kind in ('person', 'company_contact') and email.lower() in emails:
            action = 'update'
            warnings.append(f'A contact with {email} already exists')

        if mode == MODE_CREATE and action == 'update':
            action = 'skip'
            warnings.append('Exists already — skipped in "create new only"')
        if mode == MODE_UPDATE and action == 'create':
            action = 'skip'
            warnings.append('Not found — skipped in "update existing only"')

        results.append({
            'row': record['_row'], 'data': record,
            'errors': errors, 'warnings': warnings,
            'action': 'error' if errors else action,
            'existing_id': existing_id,
        })
    return results


def summarise(results):
    """§47 — the numbers a person confirms against."""
    return {
        'total':      len(results),
        'valid':      sum(1 for r in results if not r['errors']),
        'errors':     sum(1 for r in results if r['errors']),
        'warnings':   sum(1 for r in results
                          if r['warnings'] and not r['errors']),
        'create':     sum(1 for r in results if r['action'] == 'create'),
        'update':     sum(1 for r in results if r['action'] == 'update'),
        'skip':       sum(1 for r in results if r['action'] == 'skip'),
    }


# ── §49: the error report ────────────────────────────────────────────
def build_error_report(results, kind):
    spec = TEMPLATES[kind]
    wb = Workbook()
    ws = wb.active
    ws.title = 'ERRORS'
    headers = ['Row', 'Record', 'Problem', 'Suggested correction']
    ws.append(headers)
    for c in ws[1]:
        c.fill, c.font = _HEAD_FILL, _HEAD_FONT
    for width, letter in ((8, 'A'), (38, 'B'), (52, 'C'), (52, 'D')):
        ws.column_dimensions[letter].width = width

    for r in results:
        if not r['errors']:
            continue
        label = (r['data'].get('name') or r['data'].get('company')
                 or r['data'].get('_company') or '(blank)')
        for err in r['errors']:
            ws.append([r['row'], label, err, _suggest(err)])

    info = wb.create_sheet('WHAT TO DO')
    info.column_dimensions['A'].width = 100
    for line in (
        f'{spec["label"]} — rows that could not be imported',
        '',
        'Fix the rows listed on the ERRORS sheet in your original file, '
        'then upload it again.',
        'Rows that were fine have already been imported; re-uploading them '
        'will not create duplicates — matching companies are updated, not '
        'copied.',
    ):
        info.append([line])
    info['A1'].font = Font(bold=True, size=12)

    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    return out


def _suggest(error):
    if 'is required' in error:
        return 'Fill this cell in.'
    if 'not a known' in error:
        return ('Use a value from the LOOKUPS sheet, or add it under '
                'Admin → Master Data first.')
    if 'not an employee code' in error:
        return 'Use the employee code exactly as it appears in Employees.'
    return 'Correct the value and upload again.'


# ── staging between preview and commit ───────────────────────────────
def _stage_dir():
    path = os.path.join(tempfile.gettempdir(), 'procam_crm_imports')
    os.makedirs(path, exist_ok=True)
    return path


def stage(file_storage, kind, actor):
    """Save the upload so the preview can be confirmed without re-uploading."""
    from app import ImportBatch
    batch = ImportBatch(kind=kind, filename=file_storage.filename or '',
                        created_by=actor or '', committed=False)
    db.session.add(batch)
    db.session.commit()

    path = os.path.join(_stage_dir(), f'{batch.id}.xlsx')
    file_storage.stream.seek(0)
    with open(path, 'wb') as fh:
        fh.write(file_storage.stream.read())
    return batch


def staged_path(batch_id):
    return os.path.join(_stage_dir(), f'{batch_id}.xlsx')


# ── §48/§50/§51: applying a confirmed import ─────────────────────────
def _set_classifications(company_id, raw):
    """Add relationship types without removing any already held (§51).

    A company arriving as "Customer" in one file and "Competitor" in
    another ends with both on one record — that is the whole point of the
    unified master.
    """
    if not raw:
        return
    from presales.models import AccountRelationshipTag
    have = {t.tag for t in AccountRelationshipTag.query.filter_by(
        account_id=company_id).all()}
    for tag in [p.strip() for p in str(raw).split(',') if p.strip()]:
        if tag not in have:
            db.session.add(AccountRelationshipTag(account_id=company_id,
                                                  tag=tag))
            have.add(tag)


_COMPANY_FIELDS = ('industry', 'website', 'country', 'state', 'city',
                   'address', 'phone', 'email', 'linkedin', 'pic_emp_code',
                   'dev_stage', 'priority', 'notes')


def _upsert_company(record, existing_id, mode):
    from app import Company

    name = (record.get('name') or record.get('company') or '').strip()
    if not name:
        return None, None

    if existing_id:
        company = Company.query.get(existing_id)
        action = 'update'
    else:
        if mode == MODE_UPDATE:
            return None, 'skip'
        company = Company(name=name, is_active=True)
        db.session.add(company)
        action = 'create'

    if company is None:
        return None, 'skip'

    for field in _COMPANY_FIELDS:
        value = record.get(field)
        # Never blank an existing value with an empty cell — an import
        # that erases data people typed is worse than one that skips it.
        if value:
            setattr(company, field, value)
    db.session.flush()
    _set_classifications(company.id, record.get('_relationship')
                         or record.get('_network'))
    return company, action


def _upsert_contact(record, company_id, mode):
    from app import Contact

    name = (record.get('name') or record.get('_person_name') or '').strip()
    if not name:
        return None
    email = (record.get('email') or record.get('_person_email') or '').strip()

    contact = None
    if email:
        contact = Contact.query.filter(Contact.email == email).first()
    if contact is None and company_id:
        contact = Contact.query.filter(Contact.name == name,
                                       Contact.company_id == company_id).first()
    if contact is None:
        if mode == MODE_UPDATE:
            return None
        contact = Contact(name=name)
        db.session.add(contact)

    contact.name = name
    if company_id:
        contact.company_id = company_id
    for src, dest in (('designation', 'designation'),
                      ('_designation', 'designation'),
                      ('department', 'department'),
                      ('_person_email', 'email'), ('email', 'email'),
                      ('_person_mobile', 'mobile'), ('mobile', 'mobile'),
                      ('phone', 'phone'), ('linkedin', 'linkedin'),
                      ('city', 'city'), ('country', 'country'),
                      ('assigned_to', 'assigned_to')):
        value = record.get(src)
        if value and hasattr(contact, dest):
            setattr(contact, dest, value)
    return contact


def _create_lead(record, company_id, mode):
    from app import Lead
    if mode == MODE_UPDATE:
        return None
    lead = Lead(company=(record.get('company') or '').strip() or 'Unknown',
                company_id=company_id)
    for field in ('project', 'industry', 'procam_vertical', 'source',
                  'stage', 'pic', 'email', 'phone', 'state', 'city',
                  'assigned_to', 'notes'):
        value = record.get(field)
        if value:
            setattr(lead, field, value)
    db.session.add(lead)
    return lead


def commit(results, kind, mode, batch, actor):
    """Write a confirmed import. Rows with errors are never applied."""
    counts = {'created': 0, 'updated': 0, 'skipped': 0, 'failed': 0}

    for r in results:
        if r['errors']:
            counts['failed'] += 1
            continue
        if r['action'] == 'skip':
            counts['skipped'] += 1
            continue

        record = r['data']
        try:
            company = None
            if kind in ('company', 'company_contact', 'network', 'lead'):
                if kind == 'lead':
                    from app.services.company_match import build_index, match
                    from app import Company
                    index = build_index(Company.query.filter(
                        Company.is_active.is_(True)).all())
                    company, _reason, _c = match(record.get('company'), index)
                else:
                    company, action = _upsert_company(
                        record, r['existing_id'], mode)
                    if action == 'skip' or company is None:
                        counts['skipped'] += 1
                        continue

            if kind in ('person', 'company_contact'):
                if kind == 'person' and company is None:
                    from app.services.company_match import build_index, match
                    from app import Company
                    index = build_index(Company.query.filter(
                        Company.is_active.is_(True)).all())
                    company, _reason, _c = match(record.get('_company'), index)
                _upsert_contact(record, company.id if company else None, mode)

            if kind == 'lead':
                _create_lead(record, company.id if company else None, mode)

            counts['created' if r['action'] == 'create' else 'updated'] += 1
        except Exception:
            db.session.rollback()
            counts['failed'] += 1

    summary = summarise(results)
    batch.total_rows = summary['total']
    batch.valid_rows = summary['valid']
    batch.error_rows = summary['errors']
    batch.duplicate_rows = summary['update']
    batch.committed = True
    batch.committed_at = datetime.utcnow()
    batch.preview_data = json.dumps(counts)
    db.session.commit()
    return counts
