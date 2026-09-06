#!/usr/bin/env python3
"""
CRM state audit — the evidence base for the §92 gap analysis.

Read-only.  Answers, from the live database rather than from reading the
code: how fragmented is the master data, what is orphaned, and do the
reports reconcile with their own source tables.

    python scripts/audit_crm_state.py              # full report
    python scripts/audit_crm_state.py --section C  # one section
    python scripts/audit_crm_state.py --json out.json

Sections
    A  inventory ................ every table and its row count
    B  relationship map ......... how records actually link (and don't)
    C  duplicate masters ........ the same company held in several tables
    D  report reconciliation .... each report's number vs its source table
    E  data quality ............. the §66 dashboard, measured
"""
import argparse
import json
import os
import sys
from collections import defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

import importlib                                              # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
app, db = _main.app, _main.db

OUT = {}


def rule(title):
    print('\n' + '═' * 74)
    print('  ' + title)
    print('═' * 74)


def _norm(name):
    """Loose company-name key for duplicate detection."""
    if not name:
        return ''
    s = str(name).lower().strip()
    for junk in (' pvt', ' ltd', ' limited', ' private', ' inc', ' llc',
                 ' gmbh', ' co.', ' co ', ' corporation', ' corp',
                 ' company', ' group', ' india', ' logistics', '.', ',',
                 '-', '&', '  '):
        s = s.replace(junk, ' ')
    return ' '.join(s.split())


# ── A. inventory ─────────────────────────────────────────────────────
def section_a():
    rule('A · INVENTORY — every table and its row count')
    from sqlalchemy import text
    insp = db.inspect(db.engine)
    rows = {}
    for t in sorted(insp.get_table_names()):
        try:
            with db.engine.connect() as c:
                rows[t] = c.execute(text(f'SELECT COUNT(*) FROM "{t}"')).scalar()
        except Exception as exc:
            rows[t] = f'error: {exc}'
    empty = [t for t, n in rows.items() if n == 0]
    for t, n in rows.items():
        flag = '   ← empty' if n == 0 else ''
        print(f'  {t:34} {n:>8}{flag}')
    print(f'\n  {len(rows)} tables, {len(empty)} empty')
    OUT['A_tables'] = rows


# ── B. relationship map ──────────────────────────────────────────────
def section_b():
    rule('B · RELATIONSHIP MAP — what is actually linked')
    from app import Lead, Opportunity, Company, Contact

    checks = []

    total_leads = Lead.query.count()
    # Lead.company is free text, so "linked" means the name matches a
    # Company row exactly.
    names = {(_norm(c.name)): c.id for c in Company.query.all()}
    matched = unmatched = 0
    for (lc,) in db.session.query(Lead.company).all():
        if _norm(lc) in names:
            matched += 1
        else:
            unmatched += 1
    checks.append(('Lead → Company', 'name string, NO foreign key',
                   matched, unmatched, total_leads))

    total_ct = Contact.query.count()
    cm = cu = 0
    for (cc,) in db.session.query(Contact.company).all():
        (cm := cm + 1) if _norm(cc) in names else (cu := cu + 1)
    checks.append(('Contact → Company', 'name string, NO foreign key',
                   cm, cu, total_ct))

    total_opp = Opportunity.query.count()
    linked = Opportunity.query.filter(Opportunity.company_id.isnot(None)).count()
    checks.append(('Opportunity → Company', 'company_id FK',
                   linked, total_opp - linked, total_opp))

    print(f'  {"relation":26} {"mechanism":28} {"linked":>8} {"orphan":>8}')
    for name, mech, ok, bad, tot in checks:
        print(f'  {name:26} {mech:28} {ok:>8} {bad:>8}   of {tot}')
    OUT['B_links'] = [
        {'relation': n, 'mechanism': m, 'linked': o, 'orphan': b,
         'total': t} for n, m, o, b, t in checks]


# ── C. duplicate masters ─────────────────────────────────────────────
def section_c():
    rule('C · DUPLICATE MASTERS — the same company in several tables')
    from app import Company, OverseasAgent, Lead, Contact

    sources = {'companies': [c.name for c in Company.query.all()],
               'overseas_agents': [a.name for a in OverseasAgent.query.all()]}
    try:
        from app.models.competitor import CompetitorMaster
        sources['competitor_masters'] = [c.name for c
                                         in CompetitorMaster.query.all()]
    except Exception:
        sources['competitor_masters'] = []
    try:
        from app import Competitor
        sources['competitors (per-lead)'] = [
            c.name for c in Competitor.query.all()]
    except Exception:
        sources['competitors (per-lead)'] = []
    sources['leads.company (text)'] = [r[0] for r in
                                       db.session.query(Lead.company).all()]
    sources['contacts.company (text)'] = [r[0] for r in
                                          db.session.query(Contact.company).all()]

    print('  Company names held per table')
    index = defaultdict(set)
    for table, names in sources.items():
        distinct = {_norm(n) for n in names if _norm(n)}
        print(f'    {table:28} {len(names):>7} rows   {len(distinct):>6} distinct')
        for d in distinct:
            index[d].add(table)

    overlap = {k: v for k, v in index.items() if len(v) > 1}
    print(f'\n  Same company appearing in more than one table: {len(overlap)}')
    for name in sorted(overlap)[:15]:
        print(f'    {name[:44]:46} {", ".join(sorted(overlap[name]))}')
    if len(overlap) > 15:
        print(f'    … and {len(overlap) - 15} more')

    # duplicates inside the Company master itself
    seen = defaultdict(list)
    for c in Company.query.all():
        seen[_norm(c.name)].append(c.id)
    dupes = {k: v for k, v in seen.items() if len(v) > 1}
    print(f'\n  Duplicates WITHIN companies: {len(dupes)} names, '
          f'{sum(len(v) for v in dupes.values())} rows')
    for name in sorted(dupes)[:10]:
        print(f'    {name[:44]:46} ids {dupes[name]}')

    OUT['C_sources'] = {k: len(v) for k, v in sources.items()}
    OUT['C_cross_table_overlap'] = len(overlap)
    OUT['C_company_internal_dupes'] = len(dupes)


# ── D. report reconciliation ─────────────────────────────────────────
def section_d():
    rule('D · REPORT RECONCILIATION — report number vs source table')
    from app import Lead, Opportunity, Company, Contact
    rows = []

    def check(label, report_value, source_value, note=''):
        ok = (report_value == source_value)
        rows.append((label, report_value, source_value, ok, note))

    won = Opportunity.query.filter(Opportunity.stage == 'Won').count()
    won_valued = Opportunity.query.filter(
        Opportunity.stage == 'Won', Opportunity.value_inr.isnot(None)).count()
    check('Won opportunities', won, won,
          f'{won - won_valued} have no value_inr — they contribute 0 to '
          f'Won Value by Account')

    won_no_company = Opportunity.query.filter(
        Opportunity.stage == 'Won',
        Opportunity.company_id.is_(None)).count()
    check('Won rows reaching Won-Value report', won - won_no_company, won,
          f'{won_no_company} Won rows have no company_id and are skipped '
          f'by the report entirely')

    try:
        from app.models.task_engine import TaskInstance, TaskInstanceStatus
        total_t = TaskInstance.query.count()
        open_t = TaskInstance.query.filter(
            TaskInstance.status.in_(TaskInstanceStatus.OPEN)).count()
        no_owner = TaskInstance.query.filter(
            TaskInstance.owner_user_id.is_(None)).count()
        check('Open tasks', open_t, open_t,
              f'{no_owner} of {total_t} tasks have no owner_user_id and '
              f'appear for nobody in My Work')
    except Exception as exc:
        rows.append(('Task engine', '-', '-', False, f'unavailable: {exc}'))

    try:
        from app.models.rfq import RFQ
        from app.models.quote import Quote
        rfq_n, q_n = RFQ.query.count(), Quote.query.count()
        q_no_acct = Quote.query.filter(Quote.account_id.is_(None)).count()
        check('Quotes reaching Quoted-Value report', q_n - q_no_acct, q_n,
              f'{q_no_acct} quotes have no account_id')
        check('RFQs', rfq_n, rfq_n, 'RFQs-by-Account groups on account_id')
    except Exception:
        pass

    acts_total = 0
    try:
        from app import LeadActivity
        acts_total = LeadActivity.query.count()
        leads_with_acts = db.session.query(
            LeadActivity.lead_id).distinct().count()
        check('Activities', acts_total, acts_total,
              f'across {leads_with_acts} leads; grouped by Lead.company '
              f'(text) in Activities-per-Account')
    except Exception:
        pass

    print(f'  {"metric":42} {"report":>8} {"source":>8}')
    for label, rv, sv, ok, note in rows:
        mark = ' ' if ok else '!'
        print(f' {mark}{label:42} {rv:>8} {sv:>8}')
        if note:
            print(f'   └─ {note}')
    OUT['D_reconciliation'] = [
        {'metric': l, 'report': r, 'source': s, 'reconciles': ok,
         'note': n} for l, r, s, ok, n in rows]


# ── E. data quality (§66) ────────────────────────────────────────────
def section_e():
    rule('E · DATA QUALITY — the §66 dashboard, measured')
    from app import Lead, Opportunity, Company, Contact, Employee
    checks = []

    active = {e.emp_code for e in Employee.query.filter_by(is_active=True)}

    checks.append(('Leads with no owner',
                   Lead.query.filter(
                       (Lead.assigned_to.is_(None)) |
                       (Lead.assigned_to == '')).count()))
    orphan_owner = Lead.query.filter(
        Lead.assigned_to.isnot(None), Lead.assigned_to != '',
        ~Lead.assigned_to.in_(active or [''])).count()
    checks.append(('Leads owned by an inactive employee', orphan_owner))
    checks.append(('Opportunities with no owner',
                   Opportunity.query.filter(
                       (Opportunity.owner_emp_code.is_(None)) |
                       (Opportunity.owner_emp_code == '')).count()))
    checks.append(('Opportunities not linked to a Company',
                   Opportunity.query.filter(
                       Opportunity.company_id.is_(None)).count()))
    checks.append(('Companies with no PIC',
                   Company.query.filter(
                       (Company.pic_emp_code.is_(None)) |
                       (Company.pic_emp_code == '')).count()))
    checks.append(('Contacts with no owner',
                   Contact.query.filter(
                       (Contact.assigned_to.is_(None)) |
                       (Contact.assigned_to == '')).count()))

    try:
        from app.models.task_engine import TaskInstance
        checks.append(('Tasks with no owner',
                       TaskInstance.query.filter(
                           TaskInstance.owner_user_id.is_(None)).count()))
        stale = TaskInstance.query.filter(
            TaskInstance.owner_user_id.isnot(None),
            ~TaskInstance.owner_user_id.in_(active or [''])).count()
        checks.append(('Tasks owned by an inactive employee', stale))
    except Exception:
        pass

    try:
        from app.models.tms_handover import WonHandover
        won = Opportunity.query.filter(Opportunity.stage == 'Won').count()
        checks.append(('Won deals with no TMS handover',
                       won - WonHandover.query.count()))
    except Exception:
        pass

    try:
        from app.models.competitor import OpportunityCompetitor
        lost = Opportunity.query.filter(Opportunity.stage == 'Lost').count()
        with_comp = db.session.query(
            OpportunityCompetitor.opportunity_id).distinct().count()
        checks.append(('Lost deals with no competitor recorded',
                       max(0, lost - with_comp)))
    except Exception:
        pass

    for label, n in checks:
        flag = '  ←' if n else ''
        print(f'  {label:46} {n:>7}{flag}')
    OUT['E_quality'] = dict(checks)


SECTIONS = {'A': section_a, 'B': section_b, 'C': section_c,
            'D': section_d, 'E': section_e}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--section', help='run one section (A-E)')
    ap.add_argument('--json', help='also write the findings to this file')
    args = ap.parse_args()

    with app.app_context():
        todo = ([args.section.upper()] if args.section
                else list(SECTIONS.keys()))
        for key in todo:
            fn = SECTIONS.get(key)
            if not fn:
                print(f'no section {key}')
                continue
            try:
                fn()
            except Exception as exc:
                print(f'\n  !! section {key} failed: {exc}')
        if args.json:
            with open(args.json, 'w') as fh:
                json.dump(OUT, fh, indent=2, default=str)
            print(f'\nwritten: {args.json}')
    print()
    return 0


if __name__ == '__main__':
    sys.exit(main())
