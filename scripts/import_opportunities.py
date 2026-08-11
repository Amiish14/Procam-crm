"""
Bulk import historical Opportunity data from Excel into the CRM.

Reads an Excel file with the schema:
    Date | Opportunity No. | Opportunity Description | Amount | End Date |
    Start Date | Stage.Name | Vertical.Name | Branch | BU | Link | Owner |
    Assignee | Customer Name

Creates:
    - Company records (dedup by lower(name))
    - Opportunity records (unique by opp_number)

Usage:
    cd /var/www/procam-crm
    .venv/bin/python scripts/import_opportunities.py /path/to/Opportunity.xlsx

Idempotent: re-running skips existing opp_numbers.
"""
import os
import sys
import re
from datetime import datetime, date
from decimal import Decimal
import pandas as pd

# Make sure we import from the CRM app
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import app, db, Company, Opportunity, Employee  # noqa: E402


# ─── Value mappings ──────────────────────────────────────────────────────────
STAGE_MAP = {
    'WON':              ('Won',              100),
    'LOST':             ('Lost',               0),
    'CLOSED':           ('Won',              100),
    'CANCELLED':        ('Lost',               0),
    'NOT PARTICIPATED': ('Not Participated',   0),
    'PROPOSAL SENT':    ('Proposal Sent',     60),
    'BUDGETARY':        ('Budgetary',         30),
    'QUALIFIED':        ('Qualified',         40),
}

VERTICAL_MAP = {
    'PFM':          'Project Freight',
    'PTM-M':        'Heavy Transport',
    'PTM-H':        'Heavy Transport',
    'INSTALLATION': 'Installation',
    'WAREHOUSE':    'Warehousing',
    'BLADE':        'Project Freight',
}


def clean(v):
    if v is None:
        return None
    if isinstance(v, float) and pd.isna(v):
        return None
    s = str(v).strip()
    return s if s and s.lower() != 'nan' else None


def to_date(v):
    try:
        if v is None or pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(v, pd.Timestamp):
        if pd.isna(v):
            return None
        return v.to_pydatetime().date()
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return None


def to_amount(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        d = Decimal(str(v))
        return d if d > 0 else None
    except Exception:
        return None


def build_employee_index():
    """name-upper → emp_code"""
    idx = {}
    for e in Employee.query.all():
        if e.name:
            key = re.sub(r'\s+', ' ', e.name.strip().upper())
            idx[key] = e.emp_code
    return idx


def resolve_owner(name, emp_idx):
    n = clean(name)
    if not n:
        return None
    key = re.sub(r'\s+', ' ', n.upper())
    if key in emp_idx:
        return emp_idx[key]
    # try last-name-first swap and partial
    parts = key.split()
    if len(parts) >= 2:
        alt = ' '.join([parts[-1]] + parts[:-1])
        if alt in emp_idx:
            return emp_idx[alt]
    # substring fallback
    for full, code in emp_idx.items():
        if key in full or full in key:
            return code
    return None


def main(xlsx_path):
    print(f"Reading {xlsx_path}")
    df = pd.read_excel(xlsx_path, sheet_name='Sheet2', header=1)
    print(f"  {len(df)} rows loaded")

    with app.app_context():
        # ── 1. Companies ────────────────────────────────────────────────────
        print("\n[1/2] Companies")
        existing = {c.name.strip().lower(): c
                    for c in Company.query.all()}
        print(f"  {len(existing)} companies already in DB")

        unique_names = {}
        for cust in df['Customer Name'].dropna().unique():
            name = clean(cust)
            if name:
                unique_names[name.lower()] = name

        new_companies = []
        for lname, name in unique_names.items():
            if lname not in existing:
                new_companies.append(Company(name=name, is_active=True,
                                             created_by='PCM001'))

        if new_companies:
            db.session.bulk_save_objects(new_companies)
            db.session.commit()
            print(f"  + {len(new_companies)} new companies inserted")
        else:
            print("  no new companies to insert")

        # refresh index
        company_idx = {c.name.strip().lower(): c.id
                       for c in Company.query.all()}
        print(f"  total companies now: {len(company_idx)}")

        # ── 2. Opportunities ────────────────────────────────────────────────
        print("\n[2/2] Opportunities")
        existing_opp = {o.opp_number
                        for o in Opportunity.query.with_entities(
                            Opportunity.opp_number).all()}
        print(f"  {len(existing_opp)} opportunities already in DB")

        emp_idx = build_employee_index()
        print(f"  {len(emp_idx)} employees available for owner mapping")

        new_opps = []
        skipped_dup = 0
        skipped_bad = 0
        matched_owners = 0

        for _, r in df.iterrows():
            opp_no = clean(r['Opportunity No.'])
            if not opp_no:
                skipped_bad += 1
                continue
            if opp_no in existing_opp:
                skipped_dup += 1
                continue

            cust = clean(r['Customer Name'])
            cid = company_idx.get(cust.lower()) if cust else None

            stage_raw = (clean(r['Stage.Name']) or '').upper()
            stage, prob = STAGE_MAP.get(stage_raw, (stage_raw.title() or 'RFQ', 50))

            vertical = VERTICAL_MAP.get((clean(r['Vertical.Name']) or '').upper(),
                                        clean(r['Vertical.Name']))

            owner = resolve_owner(r['Owner'], emp_idx)
            if owner:
                matched_owners += 1

            created = to_date(r['Date']) or date.today()
            close_date = to_date(r['End Date']) or to_date(r['Start Date'])
            amount = to_amount(r['Amount'])

            # notes: preserve source metadata
            note_bits = []
            if clean(r['BU']):
                note_bits.append(f"BU: {clean(r['BU'])}")
            if vertical:
                note_bits.append(f"Vertical: {vertical}")
            if clean(r['Branch']):
                note_bits.append(f"Branch: {clean(r['Branch'])}")
            if clean(r['Assignee']) and clean(r['Assignee']) != clean(r['Owner']):
                note_bits.append(f"Assignee: {clean(r['Assignee'])}")
            if clean(r['Owner']) and not owner:
                note_bits.append(f"Owner (unmatched): {clean(r['Owner'])}")
            if clean(r['Link']):
                link_clean = re.sub(r'\s+', '', str(r['Link']))
                note_bits.append(f"ERP: {link_clean}")
            notes = ' | '.join(note_bits) if note_bits else None

            title = clean(r['Opportunity Description']) or 'Transport'
            if cust:
                title = f"{cust} — {title}"

            opp = Opportunity(
                opp_number=opp_no,
                company_id=cid,
                title=title[:255],
                stage=stage,
                value_inr=amount,
                currency='INR',
                probability=prob,
                expected_close_date=close_date,
                owner_emp_code=owner,
                notes=notes,
                created_at=datetime.combine(created, datetime.min.time()),
                updated_at=datetime.combine(created, datetime.min.time()),
                won_at=datetime.combine(created, datetime.min.time())
                    if stage == 'Won' else None,
                lost_at=datetime.combine(created, datetime.min.time())
                    if stage in ('Lost', 'Not Participated') else None,
                lost_reason=stage_raw.title()
                    if stage in ('Lost', 'Not Participated') else None,
            )
            new_opps.append(opp)
            existing_opp.add(opp_no)

            # batch flush every 1000 to keep memory + tx size sane
            if len(new_opps) >= 1000:
                db.session.bulk_save_objects(new_opps)
                db.session.commit()
                print(f"  + committed batch, running total inserted: "
                      f"{len(new_opps)} (this batch), "
                      f"skipped_dup={skipped_dup}")
                new_opps = []

        if new_opps:
            db.session.bulk_save_objects(new_opps)
            db.session.commit()

        # ── Summary ─────────────────────────────────────────────────────────
        total_opp = Opportunity.query.count()
        total_co = Company.query.count()
        print("\n─── SUMMARY ──────────────────────────────────────────")
        print(f"  Companies in DB:     {total_co}")
        print(f"  Opportunities in DB: {total_opp}")
        print(f"  Skipped (dup):       {skipped_dup}")
        print(f"  Skipped (bad row):   {skipped_bad}")
        print(f"  Owner name matched:  {matched_owners}")

        # stage breakdown
        from sqlalchemy import func
        rows = db.session.query(Opportunity.stage,
                                func.count(Opportunity.id)).group_by(
                                    Opportunity.stage).all()
        print("  Stage breakdown:")
        for s, n in sorted(rows, key=lambda x: -x[1]):
            print(f"    {s:20s} {n:>6d}")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("usage: python scripts/import_opportunities.py <xlsx_path>")
        sys.exit(1)
    main(sys.argv[1])
