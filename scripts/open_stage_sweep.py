"""
Everything that asks "is this lead still open", and what each one
answered before and after the alias stages were counted.

The bug
    `sales_rules.open_stages()` returned the journey's open stages
    and nothing else. Three stage names are not on the journey and
    are still carried by live leads — Profile Sent, Appointment and
    Visit Done — because `LEGACY_STAGE_MAP` only rewrites a stage
    when somebody saves the lead, and nobody has saved these since
    the names changed. Every screen, report and count built on the
    helper read those leads as closed: not in the Workbench, not in
    the review funnel, not in the pipeline value, not offered to the
    account gate, not swept by any of the backlog scripts.

    It is the same shape of bug as the 156 "New Opportunity" leads
    that `app.py`'s stage-list comment describes, one layer down:
    the open set was derived from one list, and the stage a lead
    actually carries came from another.

The fix
    `STAGES_OPEN` in `app.py` — the journey's open stages plus the
    retired names that fold onto them — and `open_stages()` returns
    it. The vocabulary list a dropdown or a funnel needs is now
    `sales_rules.pipeline_stages()`, which is the journey alone, so
    the two readings of "the stage list" can no longer be confused
    for one another.

What this prints
    Every place in the codebase that decides openness, with what it
    feeds, and then each probe's count under the old set and the new
    one. The probes are the real query shapes those screens use.

Read-only. It opens the database, counts, and writes nothing:

    .venv/bin/python scripts/open_stage_sweep.py
    .venv/bin/python scripts/open_stage_sweep.py --sites
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


#: Every site that decides whether a lead is open, what it feeds, and
#: which probe below measures it. Kept by hand rather than grepped at
#: runtime, because the point of the list is the "feeds" column: a
#: file and line number does not tell anybody which screen went wrong.
SITES = (
    # (file:line, what it feeds, probe)
    ('app/services/sales_rules.py:147 open_leads()',
     'Workbench, cockpit, every caller of open_leads()', 'open'),
    ('app/services/sales_rules.py:143 is_open()',
     'the per-lead open/closed test', 'open'),
    ('app.py:3972 active_stages',
     'dashboard stats — "active"', 'open'),
    ('app.py:4063 active_stages',
     'lead summary KPIs — active_pipeline, profile_sent, visits',
     'open'),
    ('app.py:4302 stage-wise open pipeline',
     'cockpit stage list', 'stagewise'),
    ('app.py:5860 kpi active_pipeline',
     'KPI engine — active_pipeline', 'open'),
    ('app.py:5889 kpi pipeline_value',
     'KPI engine — pipeline_value (₹ mn)', 'value'),
    ('app/review/service.py:340 _funnel()',
     'review pack — funnel, open count, open value', 'open'),
    ('app/review/service.py:428 _discipline()',
     'review pack — follow-up discipline, stale, overdue', 'open'),
    ('app/review/service.py:755 _desk()',
     'review pack — person by person', 'open'),
    ('app/review/service.py:865 _accounts()',
     'review pack — account by account', 'open'),
    ('app/review/service.py:975 meeting()',
     'review meeting agenda — negotiations, expected, stale', 'open'),
    ('app/review/service.py:1315 command_view()',
     'management view — actions today, overdue, stuck', 'open'),
    ('app/services/account_gate.py:206',
     'the account gate — does this account have open leads', 'account'),
    ('app/services/account_suggest.py:187',
     'account suggestions on an unlinked lead', 'unlinked'),
    ('app/services/lead_account.py:447, 601, 766',
     'lead → account linking, the queue and the teach-back', 'unlinked'),
    ('app/services/digests.py:482',
     'the daily digest mail', 'open'),
    ('app/services/outbound_mail.py:154',
     'outbound mail — which leads may be mailed', 'open'),
    ('app/services/quote_mail.py:149',
     'quote mail — which leads may be quoted', 'open'),
    ('app/account_check/routes.py:665',
     'the account-check screen', 'account'),
    ('app/lead_v2/routes.py:287, 395',
     'the new lead pop-up and the All Leads list (v2)', 'open'),
    ('scripts/account_check_backlog.py:46',
     'the account-check backlog report', 'account'),
    ('scripts/assignment_survey.py:35',
     'the assignment survey', 'open'),
    ('scripts/close_unidentifiable_leads.py:113',
     'the unidentifiable-lead sweep', 'open'),
    ('scripts/fix_unowned_leads.py:40',
     'the unowned-lead fix', 'unowned'),
    ('scripts/lead_flow_cleanup.py:69',
     'the lead-flow cleanup reports', 'open'),
    ('scripts/link_unplaced_leads.py:56, 214',
     'the unlinked-lead sweep', 'unlinked'),
    ('scripts/seed_account_vertical_owners.py:88',
     'the account owner seed', 'open'),
    ('scripts/split_new_lead_account.py:64',
     'the new-lead account split', 'unlinked'),
    ('scripts/unowned_open_leads.py:212',
     'the unowned open-lead sweep', 'unowned'),
    ('scripts/vertical_master_survey.py:53',
     'the vertical master survey', 'open'),
)


# ── probes ───────────────────────────────────────────────────────────
def _base(stages):
    from app import Lead
    return Lead.query.filter(Lead.is_archived.isnot(True),
                             Lead.stage.in_(stages))


def p_open(stages):
    """Open, unarchived leads — the shape `open_leads()` uses."""
    return _base(stages).count()


def p_unowned(stages):
    from app import Lead
    return _base(stages).filter(
        (Lead.assigned_to.is_(None)) | (Lead.assigned_to == '')).count()


def p_unlinked(stages):
    from app import Lead
    return _base(stages).filter(Lead.company_id.is_(None)).count()


def p_account(stages):
    from app import Lead
    return _base(stages).filter(Lead.company_id.isnot(None)).count()


def p_value(stages):
    """Open pipeline value, in ₹ millions, the way the KPI reads it."""
    from app import Lead, db
    from app.services.lead_value import value_inr_sql
    total = db.session.query(
        db.func.coalesce(db.func.sum(value_inr_sql()), 0)
    ).filter(Lead.stage.in_(stages)).scalar() or 0
    return round(float(total) / 1000000.0, 2)


def p_stagewise(stages):
    """Leads the cockpit's stage list accounts for.

    The list draws one row per journey stage, so what the fix
    changes here is not the number of rows but how many leads those
    rows add up to: an alias lead now lands on the row of the stage
    it means instead of on no row at all.
    """
    from app import Lead, db
    from app.services import stage_map
    rows = db.session.query(Lead.stage, db.func.count(Lead.id)).filter(
        Lead.is_archived.isnot(True), Lead.stage.in_(stages)
    ).group_by(Lead.stage).all()
    drawn = [s for s in stage_map.JOURNEY if s not in stage_map.TERMINAL]
    return sum(n for stage, n in rows
               if stage_map.canonical(stage) in drawn)


PROBES = {
    'open': ('open leads', p_open),
    'unowned': ('open leads with no owner', p_unowned),
    'unlinked': ('open leads with no account', p_unlinked),
    'account': ('open leads on an account', p_account),
    'value': ('open pipeline value, ₹ mn', p_value),
    'stagewise': ('leads on the cockpit stage list', p_stagewise),
}


# ── the two stage sets ───────────────────────────────────────────────
def old_set():
    """What `open_stages()` returned before 30 October 2026."""
    from app.services import stage_map
    return tuple(s for s in stage_map.JOURNEY
                 if s not in stage_map.TERMINAL)


def new_set():
    from app.services import sales_rules
    return tuple(sales_rules.open_stages())


def alias_rows():
    """Leads sitting on an alias stage, by stage."""
    from app import Lead, db
    from app.services import stage_map
    rows = db.session.query(Lead.stage, db.func.count(Lead.id)).filter(
        Lead.stage.in_(stage_map.OPEN_ALIASES)
    ).group_by(Lead.stage).all()
    return {r[0]: r[1] for r in rows}


def counts():
    """Every probe under both sets."""
    before, after = old_set(), new_set()
    out = []
    for key, (label, fn) in PROBES.items():
        out.append({'probe': key, 'label': label,
                    'before': fn(before), 'after': fn(after)})
    return out


# ── printing ─────────────────────────────────────────────────────────
def _rule(n=72):
    print('─' * n)


def report(*, sites=False):
    from app.services import stage_map

    print()
    print('The open-stage sweep')
    _rule()
    print('journey open stages : ' + ', '.join(old_set()))
    print('alias open stages   : ' + ', '.join(stage_map.OPEN_ALIASES))
    print()

    rows = alias_rows()
    if rows:
        print('Leads on an alias stage, all stages of life:')
        for stage in stage_map.OPEN_ALIASES:
            print(f'   {stage:<20} {rows.get(stage, 0):>6}')
        print(f'   {"total":<20} {sum(rows.values()):>6}')
    else:
        print('No lead in this database carries an alias stage, so '
              'every count below is unchanged.')
    print()

    print('Counts, before and after')
    _rule()
    print(f'{"":<36}{"before":>10}{"after":>10}{"change":>10}')
    for row in counts():
        delta = row['after'] - row['before']
        mark = f'+{delta}' if delta > 0 else str(delta)
        print(f'{row["label"]:<36}{row["before"]:>10}'
              f'{row["after"]:>10}{mark:>10}')
    print()

    by_probe = {}
    for where, feeds, probe in SITES:
        by_probe.setdefault(probe, []).append((where, feeds))
    print(f'{len(SITES)} places decide openness, in '
          f'{len(by_probe)} query shapes')
    _rule()
    if sites:
        for probe, items in by_probe.items():
            print(f'\n  {PROBES[probe][0]}')
            for where, feeds in items:
                print(f'    {where}')
                print(f'        {feeds}')
    else:
        for probe, items in by_probe.items():
            word = 'place' if len(items) == 1 else 'places'
            print(f'  {PROBES[probe][0]:<36}{len(items):>3} {word}')
        print('\n  --sites lists each one with what it feeds.')
    print()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--sites', action='store_true',
                    help='list every place, with what it feeds')
    args = ap.parse_args(argv)

    from app import app as flask_app
    with flask_app.app_context():
        report(sites=args.sites)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
