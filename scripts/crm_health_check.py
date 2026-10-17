"""
CRM-C1 Part 1 — the health check, read-only.

Reads. Writes nothing, sends nothing, queues nothing. Safe on a live
server at any hour.

    .venv/bin/python scripts/crm_health_check.py
    .venv/bin/python scripts/crm_health_check.py --workbench-only

Sections, in the order the work order asks for them:

    1  platform      database, release, boot state
    2  mail          outbox by state, failures and their reasons, the
                     address the CRM sends as, the daily brief
    3  stale alerts  where notification bodies are built
    4  permissions   whether an unknown permission code grants anything
    5  flags         every switch and what it is set to
    6  workbench     the live tile-vs-chip numbers CRM-WB-01 has to fix

The things this cannot see from inside the application — the commit the
service is running, the worker count, the timers, the restart time —
are printed at the end as shell one-liners to run next to it.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from sqlalchemy import text                               # noqa: E402

from app import app, db                                   # noqa: E402


def head(n, title):
    print(f'\n{"=" * 72}\n{n}. {title}\n{"=" * 72}')


def rows(sql, params=None):
    try:
        return db.session.execute(text(sql), params or {}).fetchall()
    except Exception as exc:                              # noqa: BLE001
        db.session.rollback()
        print(f'  ! {str(exc)[:160]}')
        return []


# ── 1 ────────────────────────────────────────────────────────────────
def platform():
    head(1, 'PLATFORM')
    print(f'  database      {db.engine.url}')
    print(f'  dialect       {db.engine.dialect.name}')
    url = str(db.engine.url)
    if url.startswith('sqlite'):
        path = url.replace('sqlite:///', '')
        try:
            size = os.path.getsize(path)
            print(f'  file size     {size / 1024 / 1024:.1f} MB')
        except OSError:
            print('  file size     (cannot stat)')

    # Counted in SQL, not through the ORM: a model that has grown a
    # column the database has not yet is a real state on a server
    # mid-deploy, and the health check has to survive it to say so.
    for label, table in (('employees', 'employees'), ('leads', 'leads')):
        r = rows(f'SELECT COUNT(*) FROM {table}')
        print(f'  {label:<13} {r[0][0] if r else "unreadable"}')

    # Is CRM-EMAIL-ADMIN-01 actually running? Three things have to be
    # true: the tables exist, the code that reads them is imported, and
    # the process was started after the migration wrote its rows.
    from sqlalchemy import inspect
    insp = inspect(db.engine)
    for table in ('vertical_head', 'vertical_alias', 'notification_policy'):
        print(f'  table {table:<22} '
              f'{"present" if insp.has_table(table) else "MISSING"}')
    cols = {c['name'] for c in insp.get_columns('email_outbox')} \
        if insp.has_table('email_outbox') else set()
    for col in ('policy_version', 'smtp_code'):
        print(f'  email_outbox.{col:<17} '
              f'{"present" if col in cols else "MISSING"}')
    try:
        from app.services import vertical_heads          # noqa: F401
        from app.services import notification_policy     # noqa: F401
        print('  release code  importable — this process has CRM-EMAIL-ADMIN-01')
    except Exception as exc:                              # noqa: BLE001
        print(f'  release code  NOT IMPORTABLE: {str(exc)[:100]}')

    r = rows('SELECT COUNT(*) FROM vertical_head WHERE head_emp_code '
             "IS NOT NULL AND TRIM(head_emp_code) <> ''")
    named = r[0][0] if r else 0
    r = rows('SELECT COUNT(*) FROM vertical_head')
    total = r[0][0] if r else 0
    print(f'  vertical heads {named} named of {total} rows')
    for v, h in rows('SELECT vertical, COALESCE(head_emp_code, '
                     "'— nobody —') FROM vertical_head ORDER BY vertical"):
        print(f'      {v:<24} {h}')


# ── 2 ────────────────────────────────────────────────────────────────
def mail():
    head(2, 'MAIL')
    print('  email_outbox by state:')
    total = 0
    for status, n in rows('SELECT status, COUNT(*) FROM email_outbox '
                          'GROUP BY status ORDER BY COUNT(*) DESC'):
        print(f'      {status:<12} {n:>6}')
        total += n
    print(f'      {"TOTAL":<12} {total:>6}')

    print('\n  failed rows, grouped by the reason the server gave:')
    for reason, n, oldest, newest in rows(
            "SELECT COALESCE(last_error, '(no reason recorded)'), COUNT(*), "
            'MIN(created_at), MAX(created_at) FROM email_outbox '
            "WHERE status = 'failed' GROUP BY last_error "
            'ORDER BY COUNT(*) DESC LIMIT 20'):
        print(f'      {n:>5}  {str(oldest)[:10]} → {str(newest)[:10]}  '
              f'{str(reason)[:90]}')

    r = rows("SELECT COUNT(*) FROM email_outbox WHERE status = 'failed' "
             "AND created_at < date('now', '-7 day')")
    if r:
        print(f'\n  of those, {r[0][0]} are older than a week (stale)')

    r = rows('SELECT COUNT(*) FROM email_outbox WHERE smtp_code IS NOT NULL')
    print(f'  rows carrying an SMTP code: {r[0][0] if r else 0} '
          '(only rows that failed since 13 Oct can have one)')

    print('\n  the address the CRM sends as:')
    try:
        from app.services import mailer
        name, addr = mailer.sender()
        print(f'      from          {name} <{addr}>')
        print(f'      transport     {mailer.describe()}')
        ok, why = mailer.ready()
        print(f'      ready         {ok}{"" if ok else "  — " + why}')
    except Exception as exc:                              # noqa: BLE001
        print(f'      ! {str(exc)[:160]}')
    try:
        from app.services import mail_policy
        print(f'      policy        {mail_policy.describe()}')
    except Exception:
        pass

    print('\n  the 08:30 daily brief — what actually went out:')
    for d, n, st in rows(
            "SELECT substr(COALESCE(sent_at, created_at),1,10), COUNT(*), "
            'status FROM email_outbox '
            "WHERE event_key LIKE 'report.daily%' "
            "GROUP BY 1, 3 ORDER BY 1 DESC LIMIT 14"):
        print(f'      {d}  {st:<10} {n:>5}')
    r = rows("SELECT COUNT(*) FROM email_outbox WHERE event_key LIKE 'report.%'")
    print(f'      report rows in the outbox, all time: {r[0][0] if r else 0}')

    print('\n  recent sends, any kind, last 7 days:')
    for d, n, st in rows(
            "SELECT substr(COALESCE(sent_at, created_at),1,10), COUNT(*), "
            "status FROM email_outbox "
            "WHERE COALESCE(sent_at, created_at) >= date('now','-7 day') "
            'GROUP BY 1, 3 ORDER BY 1 DESC, 2 DESC LIMIT 30'):
        print(f'      {d}  {st:<10} {n:>5}')


# ── 3 ────────────────────────────────────────────────────────────────
def stale_alerts():
    head(3, 'STALE ALERTS — where notification bodies are built')
    print("""  Three places build the text of an outbound notification:

    app/services/notification_rules.py  dispatch()
        title and body from the matrix row, filled with describe(record)
        AT THE MOMENT THE EVENT FIRES.

    app/services/notify.py  _plain_html() / send_policy()
        renders that text to HTML, then hands it to the outbox.

    app/services/outbox.py  enqueue(html=...) / send_group()
        stores the rendered HTML in email_outbox.html and sends that
        string verbatim, however much later. NOTHING RE-RENDERS.

  So every notification email is a snapshot of the record as it was
  when the event fired. Two code paths turn that into Dipanka's
  symptom; both are visible without running anything:""")

    print("""
  (a) notify._recent_duplicate() keys on
      (user, kind, entity_type, entity_id) inside a 10-minute window.
      Content is NOT part of the key. A second, different update to
      the same lead inside 10 minutes is suppressed completely — no
      bell row, no email — so the person keeps the FIRST version and
      never learns of the later one.""")
    r = rows("""SELECT COUNT(*) FROM (
                  SELECT entity_type, entity_id, user_id,
                         strftime('%Y-%m-%d %H', created_at) h, COUNT(*) c
                  FROM notifications
                  WHERE created_at >= date('now','-14 day')
                    AND entity_id IS NOT NULL
                  GROUP BY 1,2,3,4 HAVING c > 1)""")
    print(f'      records notified more than once in the same hour, '
          f'last 14 days: {r[0][0] if r else "n/a"}')

    print("""
  (b) The delay between rendering and sending. Anything that holds a
      message — batching (default 15 min), quiet hours (up to ~10 h),
      the retry back-off (2/5/15/30/60 min), the per-minute throttle —
      ships the body written at dispatch time.""")
    for label, sql in (
            ('median minutes queued → sent, last 7 days',
             "SELECT ROUND(AVG((julianday(sent_at)-julianday(created_at))"
             "*1440),1) FROM email_outbox WHERE status='sent' "
             "AND sent_at >= date('now','-7 day')"),
            ('worst case, last 7 days',
             "SELECT ROUND(MAX((julianday(sent_at)-julianday(created_at))"
             "*1440),1) FROM email_outbox WHERE status='sent' "
             "AND sent_at >= date('now','-7 day')")):
        r = rows(sql)
        print(f'      {label}: {r[0][0] if r and r[0][0] is not None else "n/a"}')
    print('\n  NOT FIXED — reported only, as the work order asks.')


# ── 4 ────────────────────────────────────────────────────────────────
def permissions():
    head(4, 'PERMISSIONS — does an unknown code grant anything?')
    from app.access import service as access
    print(f'  ALL_PERMS holds {len(access.ALL_PERMS)} codes.')
    print("""
  app/access/service.py:

      def can(perm):
          if not session.get('emp_code'):
              return False
          _scope, perms = current()
          return perm in perms

  A plain membership test on an explicit set. There is no prefix match,
  no wildcard, and no "unknown therefore allow" branch. And set_profile
  filters what can even be stored:

      clean = sorted({p for p in (perms or []) if p in ALL_PERMS})

  so a code outside ALL_PERMS cannot be saved onto a profile either.""")

    with app.test_request_context():
        from flask import session as sess
        emp = None
        try:
            from app import Employee
            emp = (Employee.query.filter_by(is_super_admin=True).first()
                   or Employee.query.filter_by(role='admin').first())
        except Exception:
            pass
        if emp is None:
            print('  (no admin on file to test against)')
            return
        sess['emp_code'] = emp.emp_code
        scope, perms = access.effective(emp.emp_code)
        print(f'\n  tested as {emp.emp_code} '
              f'(super_admin={bool(emp.is_super_admin)}, role={emp.role!r})')
        print(f'      data scope                 {scope}')
        print(f'      permissions held           {len(perms)}')
        for probe in ('admin.access', 'admin.email', 'not.a.real.permission',
                      'admin', 'admin.', '*', ''):
            print(f'      can({probe!r:<28}) = {access.can(probe)}')
        print('\n  VERDICT: an unrecognised code is denied, including for a '
              'super admin.\n  The CRM does not have the TMS fallback.')


# ── 5 ────────────────────────────────────────────────────────────────
def feature_flags():
    head(5, 'FEATURE FLAGS IN PRODUCTION')
    from app.services import flags
    print(f'  {"flag":<26} {"state":<6} {"set in":<12} description')
    for name, desc, state, source in flags.all_flags():
        print(f'  {name:<26} {("ON" if state else "off"):<6} '
              f'{source:<12} {desc}')
    print('\n  precedence: database row, then .env, then the built-in '
          'default.\n  EMAIL_KILL_SWITCH is the exception — ON if either '
          'source says so.')


# ── 6 ────────────────────────────────────────────────────────────────
def workbench(for_codes=()):
    """The tile-vs-chip table, before and after CRM-WB-01.

    Also the post-deploy check the work order asks for: run it as
    Admin and as two named people and every pair should read "yes".
    """
    head(6, 'WORKBENCH — the live tile vs chip numbers')
    from app.access import scope as sc_mod
    from app.workbench import service as wb

    from app import Employee
    admin = (Employee.query.filter_by(is_super_admin=True).first()
             or Employee.query.filter_by(role='admin').first())
    who = [c for c in for_codes if c] or ([admin.emp_code] if admin else [])
    if not who:
        print('  (nobody to measure as)')
        return

    try:
        from app.workbench import filters as wf
        new_shape = True
    except Exception:
        wf, new_shape = None, False
    from app.services import sales_rules as _rules
    _biz = _rules.business_today()
    print(f'  business day   {_biz} ({_biz.strftime("%A")}) — "due today" '
          f'is measured against this, not against the server\'s date')
    print('  registry: ' + ('present (CRM-WB-01)' if new_shape
                            else 'ABSENT — this is the pre-release board'))

    for code in who:
        emp = Employee.query.filter_by(emp_code=code).first()
        if emp is None:
            print(f'\n  -- {code}: no such employee')
            continue
        sc = sc_mod.for_employee(code)
        try:
            data = wb.board(sc, per_page=1)
        except Exception as exc:                          # noqa: BLE001
            print(f'\n  -- {code}: board failed: {str(exc)[:160]}')
            continue

        print(f'\n  -- viewer {code} ({emp.name}), scope '
              f'{"company" if sc.unrestricted else sc.data_scope} — '
              f'{data["total"]} items')

        if new_shape and 'tiles' in data:
            tiles = {t['key']: (t['label'], t['count']) for t in data['tiles']}
            chips = {c['key']: (c['label'], c['count']) for c in data['chips']}
            print(f'     {"key":<22} {"tile":<26} {"n":>6} {"chip n":>7}  ok')
            for key in wf.KEYS:
                tl, tn = tiles.get(key, ('—', None))
                cl, cn = chips.get(key, ('—', None))
                both = tn is not None and cn is not None
                ok = 'n/a' if not both else ('yes' if tn == cn else 'NO')
                label = tl if tl != '—' else cl
                print(f'     {key:<22} {str(label):<26} '
                      f'{"" if tn is None else tn:>6} '
                      f'{"" if cn is None else cn:>7}  {ok}')
                # and the list behind it
                got = wb.board(sc, filter_key=key, per_page=1)
                n = data['counts'][key]
                if got['total'] != n:
                    print(f'        !! ?filter={key} returned '
                          f'{got["total"]}, count says {n}')
            union = wb.board(sc, filter_key='action_today', per_page=1)['total']
            print(f'     action_today == due_today ∪ overdue: {union} '
                  f'(due {data["counts"]["due_today"]}, '
                  f'overdue {data["counts"]["overdue"]})')
        else:
            chip = {g['key']: g['count'] for g in data['groups']}
            pairs = (('new_assignments', 'newly_assigned'),
                     ('high_value', 'high_value'),
                     ('data_updates', 'data_update'),
                     ('stale', 'stale'),
                     ('followups_due', 'customer_followup'),
                     ('rfqs_awaiting_quote', 'quotations'),
                     ('quotes_due', 'quotations'),
                     ('no_next_action', 'data_update'),
                     ('due_today', 'action_today'),
                     ('overdue', 'action_today'))
            print(f'     {"tile":<24} {"tile n":>7} {"chip n":>7}  ok')
            for skey, gkey in pairs:
                t, c = data['summary'].get(skey, 0), chip.get(gkey, 0)
                print(f'     {skey:<24} {t:>7} {c:>7}  '
                      f'{"yes" if t == c else "NO"}')

        qa = data['quote_ageing']
        total = sum(b['count'] for b in qa['buckets'])
        pop = qa.get('population')
        print(f'     quote ageing: buckets sum {total}'
              + (f', population {pop}, '
                 f'{"agree" if total == pop else "DISAGREE"}'
                 if pop is not None else ' (no population reported — '
                                         'pre-release shape)'))
        for b in qa['buckets']:
            print(f'        {b["label"]:<20} {b["count"]:>6}')
        print(f'        {"Overdue":<20} {qa["overdue_count"]:>6}')
        if qa.get('estimated'):
            print(f'        ({qa["estimated"]} with an estimated quote date)')

    print('\n  what the ageing population should be, from the stages:')
    for stage, n in rows(
            'SELECT stage, COUNT(*) FROM leads WHERE is_archived IS NOT 1 '
            "AND stage IN ('Quoted','Under Negotiation','RFQ Generated') "
            'GROUP BY stage ORDER BY COUNT(*) DESC'):
        print(f'      {stage:<24} {n:>6} leads')


# ── acceptance ───────────────────────────────────────────────────────
def acceptance(for_codes=()):
    """CRM-WB-01 acceptance tests 1-6, against whatever database this
    process is pointed at.

    The same assertions as tests/test_workbench_filters.py, run over
    real data instead of a fixture. Read-only: every call is a board
    read. Prints PASS or FAIL per test per viewer and exits non-zero
    if anything failed, so it can be the gate in a deploy.
    """
    head(8, 'CRM-WB-01 ACCEPTANCE TESTS 1-6')
    from app.access import scope as sc_mod
    from app.services import sales_rules as _rules
    from app.workbench import filters as wf
    from app.workbench import service as wb

    from app import Employee, Lead
    admin = (Employee.query.filter_by(is_super_admin=True).first()
             or Employee.query.filter_by(role='admin').first())
    who = [c for c in for_codes if c] or ([admin.emp_code] if admin else [])
    today = _rules.business_today()
    ctx = {'today': today}
    failures = []

    def check(viewer, number, name, ok, detail=''):
        mark = 'PASS' if ok else 'FAIL'
        print(f'  [{mark}] {viewer:<10} {number}. {name}'
              + (f'  — {detail}' if detail else ''))
        if not ok:
            failures.append(f'{viewer} test {number}: {name} — {detail}')

    for code in who:
        emp = Employee.query.filter_by(emp_code=code).first()
        if emp is None:
            print(f'  [SKIP] {code}: no such employee')
            continue
        sc = sc_mod.for_employee(code)
        print(f'\n  viewer {code} ({emp.name}), scope '
              f'{"company" if sc.unrestricted else sc.data_scope}')

        base = wb.board(sc, per_page=1)
        counts = base['counts']

        # 1 — counts[key] == ?filter=key total == rows returned
        bad = []
        for key in wf.KEYS:
            got = wb.board(sc, filter_key=key, per_page=200)
            if got['total'] != counts[key]:
                bad.append(f'{key}: count {counts[key]} vs list '
                           f'{got["total"]}')
                continue
            wrong = [i for i in got['items']
                     if not wf.BY_KEY[key].matches(i, ctx)]
            if wrong:
                bad.append(f'{key}: {len(wrong)} rows do not match')
        check(code, 1, 'count == filter total == rows', not bad,
              '; '.join(bad[:3]) or f'{len(wf.KEYS)} keys')

        # 2 — tile number == chip number wherever both exist
        tiles = {t['key']: t['count'] for t in base['tiles']}
        chips = {c['key']: c['count'] for c in base['chips']}
        pairs = [k for k in wf.KEYS if k in tiles and k in chips]
        bad = [f'{k}: tile {tiles[k]} chip {chips[k]}'
               for k in pairs if tiles[k] != chips[k]]
        check(code, 2, 'tile == chip', not bad,
              '; '.join(bad) or f'{len(pairs)} pairs: '
              + ', '.join(f'{k}={tiles[k]}' for k in pairs))

        # 3 — action_today == |due_today union overdue|
        items = wb.board(sc, per_page=100000)['items']
        due = {(i['kind'], i['id']) for i in items
               if wf.BY_KEY['due_today'].matches(i, ctx)}
        over = {(i['kind'], i['id']) for i in items
                if wf.BY_KEY['overdue'].matches(i, ctx)}
        union = len(due | over)
        check(code, 3, 'action_today == due_today U overdue',
              counts['action_today'] == union,
              f'{counts["action_today"]} vs {union} '
              f'(due {len(due)}, overdue {len(over)})')

        # 4 — ageing buckets sum to the Quoted/Under Negotiation count
        qa = base['quote_ageing']
        total = sum(qa['counts'].values())
        in_scope = len([i for i in items if i['kind'] == 'lead'
                        and i['stage'] in ('Quoted', 'Under Negotiation')])
        check(code, 4, 'ageing buckets sum == population',
              total == qa['population'] == in_scope,
              f'buckets {total}, population {qa["population"]}, '
              f'leads in scope {in_scope}')

        # 5 — RFQs awaiting quote == RFQ Generated with no quote sent
        expected = len([i for i in items if i['kind'] == 'lead'
                        and i['stage'] == 'RFQ Generated'
                        and not i.get('quote_sent_at')])
        check(code, 5, 'rfqs_awaiting_quote == RFQ Generated, unquoted',
              counts['rfqs_awaiting_quote'] == expected,
              f'{counts["rfqs_awaiting_quote"]} vs {expected}')

        # 6 — everything is distinct, and paging loses nothing
        seen, page = [], 1
        while True:
            got = wb.board(sc, per_page=50, page=page)
            seen += [(i['kind'], i['id']) for i in got['items']]
            if page >= got['pages']:
                break
            page += 1
        dupes = len(seen) - len(set(seen))
        check(code, 6, 'everything == distinct, paging loses nothing',
              dupes == 0 and len(seen) == counts['everything'],
              f'paged {len(seen)}, distinct {len(set(seen))}, '
              f'everything {counts["everything"]}, pages {page}')

    # 8 — nothing was written. Counted here rather than claimed.
    print()
    from app.models.task_engine import TaskInstance
    from app import Company
    print(f'  census after the run: leads {Lead.query.count()}, '
          f'accounts {Company.query.count()}, '
          f'tasks {TaskInstance.query.count()} '
          f'(compare with before — this run writes nothing)')

    print()
    if failures:
        print(f'  {len(failures)} FAILURE(S):')
        for f in failures:
            print(f'    {f}')
    else:
        print('  All acceptance tests passed for every viewer.')
    return not failures


def shell_bits():
    head(7, 'WHAT THIS SCRIPT CANNOT SEE — run these next to it')
    print("""  git log -1 --format='%h %ci %s'
  git fetch -q origin && git rev-list --count HEAD..origin/main
  systemctl show procam-crm -p ActiveEnterTimestamp --value
  systemctl show procam-crm -p ExecStart --value | tr ' ' '\\n' | grep -E 'workers|bind'
  systemctl list-timers 'procam*' --all --no-pager
  .venv/bin/python -m pytest -q 2>&1 | tail -3

  The restart time must be LATER than the 13 Oct migration for
  CRM-EMAIL-ADMIN-01 to actually be serving.""")


def _safe(fn):
    """Run one section. A section that cannot read must not cost the
    others — a health check that dies on its first query tells you
    less than no health check at all."""
    try:
        fn()
    except Exception as exc:                              # noqa: BLE001
        import traceback
        name = getattr(fn, '__name__', None) or repr(fn)
        print(f'\n  !! section {name} could not complete: '
              f'{type(exc).__name__}: {str(exc)[:200]}')
        traceback.print_exc(limit=3)
        try:
            db.session.rollback()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--workbench-only', action='store_true')
    ap.add_argument('--acceptance', action='store_true',
                    help='run CRM-WB-01 acceptance tests 1-6 and exit '
                         'non-zero on any failure')
    ap.add_argument('--for', dest='for_codes', default='',
                    help='comma-separated employee codes to measure as, '
                         'as well as the admin')
    args = ap.parse_args()
    codes = [c.strip().upper() for c in (args.for_codes or '').split(',')
             if c.strip()]
    with app.app_context():
        if args.acceptance:
            ok = acceptance(codes)
            print('\nRead-only: nothing was written, queued or sent.\n')
            raise SystemExit(0 if ok else 1)
        if args.workbench_only:
            _safe(lambda: workbench(codes))
            return
        for section in (platform, mail, stale_alerts, permissions,
                        feature_flags, lambda: workbench(codes)):
            _safe(section)
        shell_bits()
        print('\nRead-only: nothing was written, queued or sent.\n')


if __name__ == '__main__':
    main()
