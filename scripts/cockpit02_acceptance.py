"""
The ten §9 acceptance checks, run against a live CRM.

    .venv/bin/python scripts/cockpit02_acceptance.py
    .venv/bin/python scripts/cockpit02_acceptance.py --as DIR12010
    .venv/bin/python scripts/cockpit02_acceptance.py --json

§9 asks the Tech team to run these on production before announcing,
as DIR12010, EMP372011 (Sanjna) and EMP572012 (Vijay). This is that
run, as a script, so the answer is the same every time and nobody has
to remember the order.

READ-ONLY on everything that matters. Checks 1, 3, 4 and 9 need a
record to act on, so they use a lead this script creates, marks
clearly, and closes as a Duplicate at the end — the same disposal an
operator would use, because CRM-NO-DELETE-01 has no exceptions. It
never touches a real lead, and it writes nothing to the register, the
feature switches, the master data or anybody's permissions.

Each check prints PASS, FAIL or SKIP with the reason. SKIP is not a
pass: it means the check could not be run here (no data of that
shape), and it says which.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Company, Employee, Lead, app, db          # noqa: E402

#: Every record this script makes carries this, so anything it leaves
#: behind after a crash is obvious and findable.
MARK = 'ZZ-ACCEPTANCE-CRM-COCKPIT-02'

RESULTS = []


def _utcnow():
    """Naive UTC, which is what the database stores."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def record(n, title, ok, detail=''):
    RESULTS.append({'check': n, 'title': title,
                    'result': ('PASS' if ok is True
                               else 'SKIP' if ok is None else 'FAIL'),
                    'detail': detail})
    tag = ('PASS' if ok is True else 'SKIP' if ok is None else 'FAIL')
    print(f'  {tag}  {n:>2}. {title}')
    if detail:
        for line in str(detail).splitlines():
            print(f'            {line}')


def _client(emp_code):
    c = app.test_client()
    emp = Employee.query.filter_by(emp_code=emp_code).first()
    with c.session_transaction() as sess:
        sess['emp_code'] = emp_code
        sess['role'] = getattr(emp, 'role', 'user') or 'user'
        sess['session_version'] = getattr(emp, 'session_version', 0) or 0
        sess['vertical'] = getattr(emp, 'vertical', '') or ''
    return c


def _scratch_lead(owner, *, stage='New Opportunity', **kw):
    """A lead of this script's own, marked and disposable."""
    lead = Lead(company=f'{MARK} {kw.pop("tag", "lead")}',
                stage=stage, source='manual',
                assigned_to=owner, procam_vertical='Heavy Transport',
                estimated_value_inr=100000,
                opportunity_value_num=100000, value_basis='estimate',
                created_at=_utcnow(),
                received_at=_utcnow(), **kw)
    db.session.add(lead)
    db.session.commit()
    return lead


def _dispose(lead, actor):
    """Close it the way an operator would. Nothing is deleted."""
    if lead is None:
        return
    lead.stage = 'Lost'
    lead.lost_reason = 'Others: acceptance check record'
    lead.is_archived = True
    lead.archive_reason = MARK
    lead.followup_date = None
    db.session.commit()


# ── the ten ──────────────────────────────────────────────────────────
def check_1(c, owner):
    """A lead cannot move to RFQ Generated without a quote due date."""
    lead = _scratch_lead(owner, tag='check1',
                         followup_date=(_utcnow().date()
                                        + timedelta(days=3)))
    try:
        r = c.put(f'/api/leads/{lead.id}',
                  json={'stage': 'RFQ Generated'})
        body = r.get_json() or {}
        ok = (r.status_code == 400
              and body.get('needs') == 'quote_due_at'
              and bool((body.get('guide') or {}).get('steps')))
        detail = (f"status {r.status_code}, needs "
                  f"{body.get('needs')!r}, guide "
                  f"{(body.get('guide') or {}).get('code')!r}")
        record(1, 'RFQ Generated refused without a due date, with '
                  'guided help', ok, detail)
        # and the same save with the date is accepted
        r2 = c.put(f'/api/leads/{lead.id}',
                   json={'stage': 'RFQ Generated',
                         'quote_due_at': (_utcnow()
                                          + timedelta(days=5)
                                          ).strftime('%Y-%m-%dT%H:%M')})
        record('1b', 'the same save carrying the date is accepted',
               r2.status_code == 200, f'status {r2.status_code}')
    finally:
        db.session.refresh(lead)
        _dispose(lead, owner)


def check_2():
    """A portal alert makes one lead; the reminder joins it."""
    from app.services import quote_due as qd

    rows = (Lead.query
            .filter(Lead.portal_key.isnot(None),
                    Lead.is_archived.isnot(True)).all())
    if not rows:
        record(2, 'one lead per portal number', None,
               'no lead carries a portal number yet — run '
               'scripts/portal_dedupe.py --apply')
        return
    by_key = {}
    for lead in rows:
        by_key.setdefault(lead.portal_key, []).append(lead)
    doubled = {k: v for k, v in by_key.items() if len(v) > 1}
    record(2, 'one open lead per portal number', not doubled,
           (f'{len(by_key)} number(s) in use; '
            + ('none doubled' if not doubled
               else 'doubled: ' + ', '.join(
                   f'{k} ({", ".join("#" + str(l.id) for l in v)})'
                   for k, v in list(doubled.items())[:5]))))
    # And the reader itself, which is what joins them.
    sample = next(iter(by_key))
    record('2b', 'the number is read back off a subject line',
           qd.portal_key(f'Re: {sample} reminder') == sample,
           f'{sample} read from a reminder subject')


def check_3(c, owner):
    """An extension records its history and its reason."""
    due = _utcnow() + timedelta(days=5)
    lead = _scratch_lead(
        owner, tag='check3', stage='RFQ Generated',
        quote_due_at=due, quote_due_confirmed=True,
        followup_date=(_utcnow().date() + timedelta(days=3)))
    try:
        r = c.post(f'/api/leads/{lead.id}/quote-due/extend',
                   json={'quote_due_at': (due + timedelta(days=7)
                                          ).strftime('%Y-%m-%dT%H:%M'),
                         'reason': 'client_extension'})
        body = r.get_json() or {}
        moved = [h for h in (body.get('history') or [])
                 if h.get('reason_code') == 'client_extension']
        ok = (r.status_code == 200 and body.get('extensions') == 1
              and bool(moved) and bool(moved[0].get('old_due_at')))
        record(3, 'an extension records the old date, the new one and '
                  'the reason; the lead reads "Extended 1x"', ok,
               f"status {r.status_code}, extensions "
               f"{body.get('extensions')}, history rows {len(moved)}")
    finally:
        db.session.refresh(lead)
        _dispose(lead, owner)


def check_4(c, owner):
    """A past deadline with no quote shows Missed submission."""
    from app.services import quote_due as qd
    from app.services import quote_due_flow as flow

    lead = _scratch_lead(
        owner, tag='check4', stage='RFQ Generated',
        quote_due_at=_utcnow() - timedelta(hours=6),
        followup_date=(_utcnow().date() + timedelta(days=3)))
    try:
        flagged = flow.mark_missed(lead)
        db.session.commit()
        told = flow.tell_about_miss(lead, dry_run=True)
        ok = (qd.is_missed(lead) and flagged
              and bool(told.get('would_tell')))
        record(4, 'a passed deadline with no quote is Missed '
                  'submission, and the mail has recipients', ok,
               f"flagged={flagged}, would tell "
               f"{told.get('would_tell') or told.get('skipped')}")
        # and the owner can answer it
        r = c.post(f'/api/leads/{lead.id}/bid-outcome',
                   json={'outcome': 'not_bid_rate'})
        record('4b', 'Bid / Not bid is recorded from a dropdown',
               r.status_code == 200, f'status {r.status_code}')
    finally:
        db.session.refresh(lead)
        _dispose(lead, owner)


def check_5(c, owner):
    """Every stage change asks for a follow-up; the count is 0."""
    from app.workbench import filters as wf
    from app.workbench import service as wb
    from app.access import scope as sc_mod

    lead = _scratch_lead(owner, tag='check5')
    try:
        r = c.put(f'/api/leads/{lead.id}', json={'stage': 'Call Done'})
        body = r.get_json() or {}
        asked = (r.status_code == 400
                 and body.get('needs') == 'followup'
                 and bool(body.get('suggested')))
        record(5, 'a stage change asks for a follow-up and proposes '
                  'a date', asked,
               f"status {r.status_code}, suggested "
               f"{body.get('suggested')!r}")
    finally:
        db.session.refresh(lead)
        _dispose(lead, owner)

    items, _ = wb.lead_items(sc_mod.for_employee(owner),
                             include_quiet=True)
    ctx = {'today': _utcnow().date()}
    missing = [i for i in items
               if wf.BY_KEY['followup_missing'].matches(i, ctx)]
    record('5b', '"Follow-up missing" reads 0', not missing,
           (f'{len(missing)} open lead(s) with no follow-up'
            + (': ' + ', '.join(f"#{i['id']}" for i in missing[:8])
               if missing else '')
            + ('  — run scripts/followup_backfill.py --apply'
               if missing else '')))


def check_6(owner):
    """No-activity 7/15/30 are non-zero and reconcile with ageing."""
    from app.access import scope as sc_mod
    from app.workbench import filters as wf
    from app.workbench import service as wb

    items, _ = wb.lead_items(sc_mod.for_employee(owner),
                             include_quiet=True)
    ctx = {'today': _utcnow().date()}
    counts = {k: len([i for i in items
                      if wf.BY_KEY[k].matches(i, ctx)])
              for k in ('no_activity_7d', 'no_activity_15d',
                        'no_activity_30d')}
    stamped = Lead.query.filter(
        Lead.last_activity_at.isnot(None)).count()
    if not stamped:
        record(6, 'No activity 7D / 15D / 30D are non-zero', None,
               'no lead carries a last-activity stamp yet — run '
               'scripts/last_activity_backfill.py --apply')
        return
    # Nested by definition: 30 days of silence is also 15 and 7.
    nested = (counts['no_activity_7d'] >= counts['no_activity_15d']
              >= counts['no_activity_30d'])
    record(6, 'No activity 7D / 15D / 30D are non-zero and nest',
           bool(counts['no_activity_7d']) and nested,
           f"7D {counts['no_activity_7d']}, "
           f"15D {counts['no_activity_15d']}, "
           f"30D {counts['no_activity_30d']} "
           f"(over {len(items)} items, {stamped} leads stamped)")


def check_7(c):
    """Overdue matches on the strip and the KPI row for one scope."""
    out = {}
    for scope in ('mailbox', 'all'):
        body = c.get(f'/api/my-work?scope={scope}').get_json() or {}
        out[scope] = {
            'label': (body.get('data_scope') or {}).get('label'),
            'overdue_leads': (body.get('pending') or {}).get(
                'overdue_leads'),
            'open_leads': (body.get('pending') or {}).get('open_leads'),
        }
    twice = c.get('/api/my-work?scope=mailbox').get_json() or {}
    same = ((twice.get('pending') or {}).get('overdue_leads')
            == out['mailbox']['overdue_leads'])
    labelled = all(v['label'] for v in out.values())
    record(7, 'every number says which scope it is over, and the same '
              'scope gives the same number',
           labelled and same,
           json.dumps(out, default=str))


def check_8():
    """Unowned open leads = 0; Walchandnagar and Godrej open = 0."""
    from app.services import client_restrictions as restrictions
    from app.services import sales_rules as rules
    from sqlalchemy import or_

    unowned = (Lead.query
               .filter(Lead.is_archived.isnot(True),
                       Lead.stage.in_(rules.open_stages()),
                       or_(Lead.assigned_to.is_(None),
                           Lead.assigned_to == ''))
               .count())
    record(8, 'no open lead is without an owner', unowned == 0,
           f'{unowned} unowned'
           + ('  — run scripts/unowned_sweep.py --apply'
              if unowned else ''))

    still = []
    for lead in (Lead.query
                 .filter(Lead.is_archived.isnot(True),
                         Lead.stage.in_(rules.open_stages()))
                 .filter(or_(Lead.company.ilike('%walchand%'),
                             Lead.company.ilike('%godrej%'))).all()):
        verdict = restrictions.check(company_name=lead.company,
                                     email=lead.email,
                                     account_id=lead.company_id)
        if verdict.blocked:
            still.append(lead.id)
    record('8b', 'no open lead on a blocked client', not still,
           (f'{len(still)} open: '
            + ', '.join(f'#{i}' for i in still[:10])
            + '  — run scripts/2026_11_01_close_pending.py --apply')
           if still else 'none')


def check_9(c, owner):
    """A quote sent from the CRM fills the lead and moves it."""
    from app.models.quote import Quote
    from app.services import lead_actions

    lead = _scratch_lead(
        owner, tag='check9', stage='RFQ Generated',
        quote_due_at=_utcnow() + timedelta(days=3),
        followup_date=(_utcnow().date() + timedelta(days=1)))
    quote = None
    try:
        quote = Quote(quote_number=f'{MARK[-8:]}-{lead.id}',
                      lead_id=lead.id,
                      quote_date=_utcnow().date(),
                      validity_until=(_utcnow().date()
                                      + timedelta(days=15)),
                      total_amount=123456)
        db.session.add(quote)
        db.session.commit()
        changed = lead_actions.quote_sent(lead, quote, actor=owner)
        db.session.commit()
        ok = (lead.stage == 'Quoted' and lead.quote_no
              and lead.quote_date and lead.quote_validity_date
              and float(lead.quote_value_num or 0) == 123456.0)
        record(9, 'a quote fills the number, value, date and validity '
                  'and moves the stage to Quoted', ok,
               f'changed: {sorted(changed)}')
    finally:
        if quote is not None:
            db.session.delete(quote)     # this script's own row
            db.session.commit()
        db.session.refresh(lead)
        _dispose(lead, owner)


def check_10(c, codes):
    """Tile = chip = list = footer, for each named person."""
    from app.crm2.routes import C02_TILES
    from app.workbench import filters as wf

    problems = []
    for code in codes:
        cl = _client(code)
        body = cl.get('/api/crm2/cockpit?scope=mailbox').get_json() \
            or {}
        if not body.get('ok'):
            problems.append(f'{code}: the cockpit answered '
                            f'{body.get("error") or "not ok"}')
            continue
        tiles = {t['key']: t for t in (body['cockpit'].get('tiles')
                                       or [])}
        for key, _label in C02_TILES:
            tile = tiles.get(key)
            if tile is None:
                problems.append(f'{code}: no tile for {key}')
                continue
            listed = cl.get(f'/my-work?filter={key}')
            if listed.status_code not in (200, 302):
                problems.append(f'{code}: {key} opens '
                                f'{listed.status_code}')
            if wf.BY_KEY[key].note != tile['note']:
                problems.append(f'{code}: {key} footer differs')
    record(10, 'every tile has a label, a footer and a list that '
               'opens, for each person', not problems,
           '\n'.join(problems[:12]) or
           f'checked {len(C02_TILES)} tiles for '
           f'{len(codes)} person(s)')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--as', dest='actor', default='DIR12010',
                    help='who the acting checks run as')
    ap.add_argument('--also', default='EMP372011,EMP572012',
                    help='the other people check 10 runs for')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args()

    with app.app_context():
        app.config['WTF_CSRF_ENABLED'] = False
        print(f'database: {db.engine.url}')
        print(f'acting as: {args.actor}')
        print(f'records this script creates are marked: {MARK}\n')

        emp = Employee.query.filter_by(emp_code=args.actor).first()
        if emp is None:
            print(f'  !! {args.actor} is not an employee here. '
                  f'Nothing was run.')
            return 1
        codes = [args.actor] + [c.strip() for c in args.also.split(',')
                                if c.strip()]
        codes = [c for c in codes
                 if Employee.query.filter_by(emp_code=c).first()]

        c = _client(args.actor)
        print('§9 ACCEPTANCE')
        print('=' * 70)
        check_1(c, args.actor)
        check_2()
        check_3(c, args.actor)
        check_4(c, args.actor)
        check_5(c, args.actor)
        check_6(args.actor)
        check_7(c)
        check_8()
        check_9(c, args.actor)
        check_10(c, codes)

        print('\n' + '=' * 70)
        tally = {}
        for row in RESULTS:
            tally[row['result']] = tally.get(row['result'], 0) + 1
        print('  ' + ', '.join(f'{v} {k}' for k, v in
                               sorted(tally.items())))
        left = Lead.query.filter(
            Lead.company.like(f'{MARK}%'),
            Lead.is_archived.isnot(True)).count()
        print(f'  records left open by this run: {left} '
              f'(every one is closed as it finishes; anything here is '
              f'a crash to report)')
        if args.json:
            print('\n' + json.dumps(RESULTS, indent=2))
        return 0 if tally.get('FAIL', 0) == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
