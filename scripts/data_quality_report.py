"""
Data-quality audit. Read-only: writes CSV files, never the database.

Thirteen reports, each a CSV of the rows that need a person's attention,
plus summary.csv with the count per report. Nothing here fixes anything —
every row is a decision (who owns this account, which of these two is
the real company) that the software must not guess.

    python scripts/data_quality_report.py
    python scripts/data_quality_report.py --out /var/www/procam-crm/reports/dq
    python scripts/data_quality_report.py --only duplicate_accounts

The database is opened read-only (SQLite mode=ro), so the script cannot
write to it even by mistake, and it does not import app.py — importing
the app runs the boot autoheal, which is a write.

The CSVs hold customer names, emails and phone numbers. They are written
owner-read-only (0600) into reports/, which git ignores. Do not attach
them to tickets or email.
"""
import argparse
import csv
import os
import re
import sys
from collections import defaultdict
from datetime import date, datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_ROOT, '.env'))
except ImportError:
    pass

from sqlalchemy import create_engine, text          # noqa: E402

WON = ('Won', 'Closed Won')
LOST = ('Lost', 'Closed Lost')
#: Opportunities past these are not "overdue to close" — they closed.
CLOSED = WON + LOST + ('On Hold', 'Not Interested')


def _db_url():
    return os.environ.get('DATABASE_URL') or (
        'sqlite:///' + os.path.join(_ROOT, 'procam_crm.db'))


def read_only_engine(url=None):
    """An engine that cannot write. For SQLite this is enforced by the
    driver, not by our good behaviour."""
    url = url or _db_url()
    if url.startswith('sqlite:///'):
        path = url[len('sqlite:///'):]
        if not os.path.exists(path):
            raise SystemExit(f'database not found: {path}')
        return create_engine(f'sqlite:///file:{path}?mode=ro&uri=true')
    return create_engine(url)


def _blank(v):
    return v is None or str(v).strip() == ''


def _norm_name(name):
    """Loose company-name key. Same idea as audit_crm_state._norm, kept
    here so this script does not import the app."""
    s = ' ' + (name or '').lower() + ' '
    s = re.sub(r'[.,&/()\-]', ' ', s)
    for junk in ('pvt', 'private', 'ltd', 'limited', 'llp', 'inc', 'llc',
                 'gmbh', 'co', 'corporation', 'corp', 'company', 'the',
                 'india'):
        s = re.sub(rf'\s{junk}\s', ' ', s)
        s = re.sub(rf'\s{junk}\s', ' ', s)
    return ' '.join(s.split())


def _norm_phone(p):
    digits = re.sub(r'\D', '', p or '')
    return digits[-10:] if len(digits) >= 10 else ''


def _rows(conn, sql, **params):
    return [dict(r._mapping) for r in conn.execute(text(sql), params)]


def _active_employees(conn):
    return {r['emp_code'] for r in _rows(
        conn, 'SELECT emp_code FROM employees WHERE is_active = 1')}


def _owner_problem(code, active):
    if _blank(code):
        return 'no owner'
    if code not in active:
        return 'owner is not an active employee'
    return None


# ── the reports ──────────────────────────────────────────────────────
# Each takes a connection and returns (columns, rows).

def accounts_without_owner(conn):
    active = _active_employees(conn)
    out = []
    for r in _rows(conn, """
            SELECT c.id, c.name, c.vertical, c.pic_emp_code, c.created_at,
                   (SELECT COUNT(*) FROM leads l
                     WHERE l.company_id = c.id
                       AND COALESCE(l.is_archived, 0) = 0) AS leads
              FROM companies c
             WHERE COALESCE(c.is_active, 1) = 1
             ORDER BY leads DESC, c.id"""):
        problem = _owner_problem(r['pic_emp_code'], active)
        if problem:
            out.append({**r, 'problem': problem})
    return (['id', 'name', 'vertical', 'pic_emp_code', 'problem', 'leads',
             'created_at'], out)


def leads_without_owner(conn):
    active = _active_employees(conn)
    out = []
    for r in _rows(conn, """
            SELECT id, company, source, stage, procam_vertical,
                   classification, assigned_to, created_at
              FROM leads
             WHERE COALESCE(is_archived, 0) = 0
             ORDER BY created_at DESC"""):
        problem = _owner_problem(r['assigned_to'], active)
        if problem:
            out.append({**r, 'problem': problem})
    return (['id', 'company', 'source', 'stage', 'procam_vertical',
             'classification', 'assigned_to', 'problem', 'created_at'], out)


def opportunities_without_owner(conn):
    active = _active_employees(conn)
    out = []
    for r in _rows(conn, """
            SELECT o.id, o.opp_number, o.title, o.stage, o.value_inr,
                   o.owner_emp_code, c.name AS account
              FROM opportunities o
              LEFT JOIN companies c ON c.id = o.company_id
             ORDER BY o.id"""):
        problem = _owner_problem(r['owner_emp_code'], active)
        if problem:
            out.append({**r, 'problem': problem})
    return (['id', 'opp_number', 'title', 'account', 'stage', 'value_inr',
             'owner_emp_code', 'problem'], out)


def opportunities_overdue_close(conn, today=None):
    today = today or date.today()
    marks = ', '.join(f"'{s}'" for s in CLOSED)
    out = []
    for r in _rows(conn, f"""
            SELECT o.id, o.opp_number, o.title, o.stage, o.value_inr,
                   o.owner_emp_code, o.expected_close_date,
                   c.name AS account
              FROM opportunities o
              LEFT JOIN companies c ON c.id = o.company_id
             WHERE o.expected_close_date IS NOT NULL
               AND COALESCE(o.stage, '') NOT IN ({marks})
               AND o.won_at IS NULL AND o.lost_at IS NULL
             ORDER BY o.expected_close_date"""):
        d = r['expected_close_date']
        if isinstance(d, str):
            d = datetime.fromisoformat(d[:10]).date()
        elif isinstance(d, datetime):
            d = d.date()
        if d < today:
            out.append({**r, 'days_overdue': (today - d).days})
    return (['id', 'opp_number', 'title', 'account', 'stage', 'value_inr',
             'owner_emp_code', 'expected_close_date', 'days_overdue'], out)


def _won(conn):
    marks = ', '.join(f"'{s}'" for s in WON)
    return _rows(conn, f"""
        SELECT o.id, o.opp_number, o.title, o.value_inr, o.owner_emp_code,
               o.won_at, c.name AS account
          FROM opportunities o
          LEFT JOIN companies c ON c.id = o.company_id
         WHERE (o.stage IN ({marks}) OR o.won_at IS NOT NULL)
           AND o.lost_at IS NULL
         ORDER BY o.won_at DESC, o.id""")


_WON_COLS = ['id', 'opp_number', 'title', 'account', 'value_inr',
             'owner_emp_code', 'won_at']


def won_without_handover(conn):
    has = {r['opportunity_id'] for r in _rows(conn, """
        SELECT opportunity_id FROM won_handovers
         WHERE opportunity_id IS NOT NULL
           AND COALESCE(status, '') != 'Cancelled'""")}
    return (_WON_COLS, [r for r in _won(conn) if r['id'] not in has])


def won_without_po(conn):
    """No PO recorded on any live handover. Includes the won deals with no
    handover at all — they cannot have a PO either."""
    has = {r['opportunity_id'] for r in _rows(conn, """
        SELECT opportunity_id FROM won_handovers
         WHERE opportunity_id IS NOT NULL
           AND COALESCE(status, '') != 'Cancelled'
           AND COALESCE(po_ref, '') != ''""")}
    handed = {r['opportunity_id'] for r in _rows(conn, """
        SELECT opportunity_id FROM won_handovers
         WHERE opportunity_id IS NOT NULL
           AND COALESCE(status, '') != 'Cancelled'""")}
    out = [{**r, 'has_handover': 'yes' if r['id'] in handed else 'no'}
           for r in _won(conn) if r['id'] not in has]
    return (_WON_COLS + ['has_handover'], out)


def accounts_without_vertical(conn):
    return (['id', 'name', 'pic_emp_code', 'created_at'], _rows(conn, """
        SELECT id, name, pic_emp_code, created_at FROM companies
         WHERE COALESCE(is_active, 1) = 1 AND COALESCE(TRIM(vertical), '') = ''
         ORDER BY id"""))


def accounts_without_service(conn):
    """companies has no service column. A service is known for an account
    when any lead (products), quote line or handover names one."""
    known = set()
    for r in _rows(conn, """SELECT DISTINCT company_id FROM leads
                            WHERE company_id IS NOT NULL
                              AND COALESCE(TRIM(products), '') != ''"""):
        known.add(r['company_id'])
    for r in _rows(conn, """SELECT DISTINCT q.account_id FROM quote_lines ql
                              JOIN quotes q ON q.id = ql.quote_id
                             WHERE q.account_id IS NOT NULL
                               AND COALESCE(TRIM(ql.service), '') != ''"""):
        known.add(r['account_id'])
    for r in _rows(conn, """SELECT DISTINCT account_id, services
                              FROM won_handovers
                             WHERE account_id IS NOT NULL"""):
        if not _blank(r['services']) and str(r['services']).strip() not in (
                '[]', 'null'):
            known.add(r['account_id'])
    rows = [r for r in _rows(conn, """
        SELECT id, name, vertical, pic_emp_code FROM companies
         WHERE COALESCE(is_active, 1) = 1 ORDER BY id""")
            if r['id'] not in known]
    return (['id', 'name', 'vertical', 'pic_emp_code'], rows)


def duplicate_accounts(conn):
    companies = _rows(conn, """
        SELECT id, name, gstin, email_domains, pic_emp_code, vertical
          FROM companies WHERE COALESCE(is_active, 1) = 1""")
    groups = defaultdict(list)
    for c in companies:
        key = _norm_name(c['name'])
        if key:
            groups[('name', key)].append(c)
        if not _blank(c['gstin']):
            groups[('gstin', c['gstin'].strip().upper())].append(c)
        for d in re.split(r'[,;\s]+', c['email_domains'] or ''):
            d = d.strip().lower().lstrip('@')
            if d:
                groups[('email_domain', d)].append(c)
    out = []
    for (kind, key), members in sorted(groups.items()):
        if len({m['id'] for m in members}) < 2:
            continue
        for m in members:
            out.append({'match_type': kind, 'match_key': key,
                        'group_size': len(members), **m})
    return (['match_type', 'match_key', 'group_size', 'id', 'name', 'gstin',
             'email_domains', 'pic_emp_code', 'vertical'], out)


def duplicate_contacts(conn):
    contacts = _rows(conn, """
        SELECT id, name, company, company_id, email, phone, mobile
          FROM contacts WHERE COALESCE(is_active, 1) = 1""")
    groups = defaultdict(list)
    for c in contacts:
        if not _blank(c['email']):
            groups[('email', c['email'].strip().lower())].append(c)
        for p in {_norm_phone(c['phone']), _norm_phone(c['mobile'])} - {''}:
            groups[('phone', p)].append(c)
    out = []
    for (kind, key), members in sorted(groups.items()):
        if len({m['id'] for m in members}) < 2:
            continue
        for m in members:
            out.append({'match_type': kind, 'match_key': key,
                        'group_size': len(members), **m})
    return (['match_type', 'match_key', 'group_size', 'id', 'name', 'company',
             'company_id', 'email', 'phone', 'mobile'], out)


def accounts_missing_gstin(conn):
    """GSTIN only exists for Indian entities, so an account with a foreign
    country is not listed. A blank country is listed — it is probably
    Indian, and that is itself worth fixing."""
    return (['id', 'name', 'country', 'state', 'pic_emp_code'], _rows(conn, """
        SELECT id, name, country, state, pic_emp_code FROM companies
         WHERE COALESCE(is_active, 1) = 1
           AND COALESCE(TRIM(gstin), '') = ''
           AND LOWER(COALESCE(TRIM(country), 'india')) IN ('india', '', 'in')
         ORDER BY id"""))


def incomplete_contacts(conn):
    out = []
    for r in _rows(conn, """
            SELECT id, name, company, company_id, email, phone, mobile,
                   designation
              FROM contacts WHERE COALESCE(is_active, 1) = 1 ORDER BY id"""):
        problems = []
        if _blank(r['name']):
            problems.append('no name')
        if _blank(r['email']) and _blank(r['phone']) and _blank(r['mobile']):
            problems.append('no email or phone')
        if r['company_id'] is None:
            problems.append('not linked to an account')
        if _blank(r['designation']):
            problems.append('no designation')
        # designation alone is not "incomplete" enough to list
        if problems and problems != ['no designation']:
            out.append({**r, 'problems': '; '.join(problems)})
    return (['id', 'name', 'company', 'company_id', 'email', 'phone',
             'mobile', 'designation', 'problems'], out)


def quotes_received_filed_as_sent(conn):
    """Emails from outside Procam filed as if Procam sent them.

    Until 2026-09 an agent's quotation to Procam, or a supplier's reply,
    was filed as outbound; a quotation also moved the lead to Quoted and
    wrote the agent's price into quoted_amount_inr. The code is fixed;
    this lists the leads it already touched, for a person to check the
    stage and amount. Nothing is corrected automatically — some of those
    leads have since been quoted for real."""
    internal = {'procamlogistics.com', 'procamgroup.in'} | {
        d.strip().lower() for d in
        (os.environ.get('INTERNAL_EMAIL_DOMAINS') or '').split(',')
        if d.strip()}
    cols = {r[1] for r in conn.execute(text('PRAGMA table_info(lead_emails)'))}
    if 'intake_class' not in cols:
        return (['lead_id'], [])
    out = []
    for r in _rows(conn, """
            SELECT e.lead_id, e.id AS email_id, e.intake_class, e.from_addr,
                   e.subject, e.sent_or_received_at, l.company, l.stage,
                   l.quoted_amount_inr, l.assigned_to
              FROM lead_emails e JOIN leads l ON l.id = e.lead_id
             WHERE e.direction = 'outbound'
               AND e.intake_class IN ('H_quote_submission', 'G_rate_sourcing')
             ORDER BY e.lead_id, e.id"""):
        domain = (r['from_addr'] or '').rsplit('@', 1)[-1].lower().strip('> ')
        if domain and domain not in internal:
            out.append({**r, 'stage_possibly_wrong':
                        'yes' if r['intake_class'] == 'H_quote_submission'
                        and r['stage'] == 'Quoted' else ''})
    return (['lead_id', 'company', 'stage', 'stage_possibly_wrong',
             'quoted_amount_inr', 'assigned_to', 'email_id', 'intake_class',
             'from_addr', 'subject', 'sent_or_received_at'], out)


REPORTS = {
    'accounts_without_owner': accounts_without_owner,
    'leads_without_owner': leads_without_owner,
    'opportunities_without_owner': opportunities_without_owner,
    'opportunities_overdue_close': opportunities_overdue_close,
    'won_without_po': won_without_po,
    'won_without_handover': won_without_handover,
    'accounts_without_vertical': accounts_without_vertical,
    'accounts_without_service': accounts_without_service,
    'duplicate_accounts': duplicate_accounts,
    'duplicate_contacts': duplicate_contacts,
    'accounts_missing_gstin': accounts_missing_gstin,
    'incomplete_contacts': incomplete_contacts,
    'quotes_received_filed_as_sent': quotes_received_filed_as_sent,
}


def _write(path, columns, rows):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)


def run(conn, out_dir, only=None):
    os.makedirs(out_dir, mode=0o700, exist_ok=True)
    summary = []
    for name, fn in REPORTS.items():
        if only and name not in only:
            continue
        columns, rows = fn(conn)
        _write(os.path.join(out_dir, f'{name}.csv'), columns, rows)
        summary.append({'report': name, 'rows': len(rows)})
    _write(os.path.join(out_dir, 'summary.csv'), ['report', 'rows'], summary)
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=os.path.join(
        _ROOT, 'reports', 'data_quality',
        datetime.now().strftime('%Y-%m-%d-%H%M')))
    ap.add_argument('--only', nargs='*', choices=sorted(REPORTS))
    args = ap.parse_args()

    with read_only_engine().connect() as conn:
        summary = run(conn, args.out, args.only)
    width = max(len(s['report']) for s in summary)
    print()
    for s in summary:
        print(f'  {s["report"]:<{width}}  {s["rows"]:>6}')
    print(f'\n  CSVs written to {args.out} (owner-read-only; contains '
          f'customer data). Nothing in the database was changed.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
