"""
CRM-TIDY-01 — four small things.

  1. The 156 stale failed outbox rows are cancelled, not resent.
  2. The Overview panel's pendency tiles and the Workbench's tiles are
     one set of numbers, from one registry, over one population.
  3. Freight Forwarding leads carry a Mode: Sea, Air or Multimodal.
  4. There is no "check Vertical before saving" warning to remove.

Item 2 is the one with teeth. The two screens each held their own SQL
for the same five words. "Overdue" meant `followup_date < today` on
the Overview and "any date gone by on anything still open" on the
Workbench, so a quote the customer was sitting on past its validity
counted on one screen and not the other. The tests below assert the
agreement directly: for every tile, the number the panel shows equals
the number of rows the Workbench lists when you click it — for an
admin and for two individual people, because a count that is right
for an admin and wrong for a rep is still wrong.
"""
import os
import sys
import tempfile
from datetime import date, datetime, timedelta

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'tidy-test-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'TidyTest123456')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'tidy.db'))

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Lead, Employee, Company = _main.Lead, _main.Employee, _main.Company
LeadActivity = _main.LeadActivity
flask_app.config['WTF_CSRF_ENABLED'] = False

from app.access import scope as sc_mod                    # noqa: E402
from app.models.access import AccessProfile, DataScope    # noqa: E402
from app.models.audit import AuditEvent                   # noqa: E402
from app.models.mailops import EmailOutbox                # noqa: E402
from app.services import lead_mode                        # noqa: E402
from app.workbench import filters as wf                   # noqa: E402
from app.workbench import service as wb                   # noqa: E402

import scripts.cancel_stale_failed_mail as canceller      # noqa: E402

ADMIN, REP, MATE = 'TDADMIN', 'TDREP', 'TDMATE'
TAG = 'TD '
TODAY = date.today()
FF = lead_mode.VERTICAL
OTHER = 'Heavy Transport'


def _emp(code, name, vertical=FF, scope=DataScope.OWN, admin=False):
    e = (Employee.query.filter_by(emp_code=code).first()
         or Employee(emp_code=code))
    e.name, e.vertical, e.is_active, e.must_change_pw = name, vertical, True, False
    e.role = 'admin' if admin else 'user'
    e.is_super_admin, e.is_vertical_head, e.session_version = admin, False, 0
    e.email = f'{code.lower()}@procamgroup.in'
    db.session.add(e)
    db.session.flush()
    AccessProfile.query.filter_by(emp_code=code).delete()
    db.session.add(AccessProfile(emp_code=code, data_scope=scope, perms=[]))
    return e


def _lead(name, owner, **kw):
    l = Lead(company=TAG + name, assigned_to=owner, assigned_name=owner,
             stage=kw.pop('stage', 'Quoted'),
             procam_vertical=kw.pop('vertical', FF),
             created_at=kw.pop('created_at',
                               datetime.utcnow() - timedelta(days=40)))
    for k, v in kw.items():
        setattr(l, k, v)
    db.session.add(l)
    return l


@pytest.fixture()
def world():
    with flask_app.app_context():
        db.create_all()
        mine = [l.id for l in Lead.query.filter(Lead.company.like(TAG + '%'))]
        if mine:
            LeadActivity.query.filter(LeadActivity.lead_id.in_(mine)).delete(
                synchronize_session=False)
            AuditEvent.query.filter(
                AuditEvent.entity_type == 'lead',
                AuditEvent.entity_id.in_([str(i) for i in mine])).delete(
                    synchronize_session=False)
            Lead.query.filter(Lead.id.in_(mine)).delete(
                synchronize_session=False)
        EmailOutbox.query.filter(
            EmailOutbox.dedupe_key.like(TAG + '%')).delete(
                synchronize_session=False)
        _emp(ADMIN, 'Tidy Admin', scope=DataScope.ALL, admin=True)
        _emp(REP, 'Tidy Rep')
        _emp(MATE, 'Tidy Mate', vertical=OTHER)
        # A real account, so 'Healthy Ltd' is genuinely clean: an
        # unlinked lead trips the hygiene check and would never be
        # the condition-free lead this fixture is for.
        # Named for the lead that links to it: the hygiene checks
        # flag a lead whose company name does not match its account,
        # and that flag would make 'Healthy Ltd' not healthy.
        house = (Company.query.filter_by(name=TAG + 'Healthy Ltd').first()
                 or Company(name=TAG + 'Healthy Ltd'))
        db.session.add(house)
        db.session.flush()

        rows = {
            # Overdue, and nothing else going for it.
            'late': _lead('Late Ltd', REP, vertical=OTHER,
                          followup_date=TODAY - timedelta(days=5)),
            # Quoted, perfectly healthy: contacted today, follow-up
            # booked. It trips no condition at all, so the board used
            # to drop it — and the Overview's "In negotiation" counted
            # it. That is the disagreement these tests close.
            'healthy': _lead('Healthy Ltd', REP, stage='Under Negotiation',
                             followup_date=TODAY + timedelta(days=6),
                             quoted_amount_inr=500000,
                             quote_date=TODAY - timedelta(days=3),
                             company_id=house.id,
                             phone_call_date=TODAY),
            'awaiting': _lead('Awaiting Ltd', REP, stage='RFQ Generated',
                              followup_date=TODAY + timedelta(days=6),
                              phone_call_date=TODAY),
            # No call, no mail, no meeting, ever.
            'virgin': _lead('Virgin Ltd', REP, stage='New',
                            followup_date=TODAY + timedelta(days=3)),
            'mates': _lead('Mates Ltd', MATE, vertical=OTHER,
                           followup_date=TODAY - timedelta(days=2),
                           phone_call_date=TODAY),
            'won': _lead('Closed Ltd', REP, stage='Won',
                         followup_date=TODAY - timedelta(days=4)),
        }
        db.session.flush()
        for key in ('healthy', 'awaiting', 'mates'):
            db.session.add(LeadActivity(
                lead_id=rows[key].id, kind='call', subject='spoke',
                occurred_at=datetime.utcnow(),
                performed_by=rows[key].assigned_to))
        db.session.commit()
        wb.hygiene_cache_clear()
        yield {k: v.id for k, v in rows.items()}


def _client(code):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code=code, name=code, role='user', sv=0)
    return c


# ── item 2: one registry, two screens ────────────────────────────────
def test_every_overview_key_is_a_real_filter_with_a_label(world):
    for key in wf.OVERVIEW_KEYS:
        f = wf.get(key)
        assert f is not None, f'{key} is on the Overview and in no registry'
        assert f.overview, f'{key} has no label for the Overview panel'


def test_the_overview_panel_has_no_tile_list_of_its_own(world):
    # The labels the panel shows come from the server. If this list
    # is ever re-typed into the template, the two can drift and the
    # drift is invisible until somebody compares two screens.
    with open(os.path.join(_ROOT, 'templates', 'app.html')) as fh:
        page = fh.read()
    for stale in ("mwTile('Overdue'", "mwTile('Idle 3+ days'",
                  "mwTile('Never actioned'", "mwTile('Awaiting quote'",
                  "mwTile('In negotiation'"):
        assert stale not in page, f'{stale} is a second copy of a label'


@pytest.mark.parametrize('code', [ADMIN, REP, MATE])
def test_overview_tiles_equal_the_workbench_counts(world, code):
    with flask_app.app_context():
        counts = wb.board(sc_mod.for_employee(code), per_page=1)['counts']
    body = _client(code).get('/api/my-work').get_json()
    assert body['ok']
    p = body['pending']
    assert p['overdue'] == counts['overdue']
    assert p['due_today'] == counts['due_today']
    assert p['untouched_3d'] == counts['quiet_3d']
    assert p['never_actioned'] == counts['never_actioned']
    assert p['awaiting_quote'] == counts['rfqs_awaiting_quote']
    assert p['in_negotiation'] == counts['in_negotiation']


@pytest.mark.parametrize('code', [ADMIN, REP, MATE])
def test_clicking_a_tile_lists_exactly_that_many_rows(world, code):
    """The number on the tile is the number of rows behind it.

    This is the whole point of the item. A tile that says 12 and
    opens a list of 4 is worse than no tile.
    """
    body = _client(code).get('/api/my-work').get_json()
    by_key = {t['key']: t['count'] for t in body['tiles']}
    assert set(by_key) == set(wf.OVERVIEW_KEYS)
    with flask_app.app_context():
        sc = sc_mod.for_employee(code)
        for key, n in by_key.items():
            got = wb.board(sc, filter_key=key, per_page=200)
            assert got['total'] == n, f'{key}: tile {n}, list {got["total"]}'


def test_a_healthy_lead_is_on_the_board_and_counted(world, monkeypatch):
    """The population is every open lead, not only the troubled ones.

    'Healthy Ltd' is under negotiation, was called today and has a
    follow-up booked. It trips nothing. The board used to drop it and
    the Overview's "In negotiation" counted it, which is two screens
    disagreeing about whether a lead exists.

    The hygiene checks are silenced here on purpose. They are ten
    scoped queries over the whole lead table, and in a suite that
    shares one database another module's rows can make any lead
    "untidy" — which would test the hygiene checks, not the rule this
    test is about.
    """
    monkeypatch.setattr(wb, '_hygiene_ids', lambda sc: {})
    with flask_app.app_context():
        sc = sc_mod.for_employee(REP)
        loud, _ = wb.lead_items(sc)
        all_of_them, _ = wb.lead_items(sc, include_quiet=True)
        d = wb.board(sc, per_page=200)

    assert world['healthy'] not in {i['id'] for i in loud}
    assert world['healthy'] in {i['id'] for i in all_of_them}
    assert world['healthy'] in {i['id'] for i in d['items']}
    assert d['counts']['in_negotiation'] >= 1

    quiet = [i for i in all_of_them if i['id'] == world['healthy']][0]
    assert quiet['conditions'] == []
    assert quiet['group'] == wb.G_NOTHING_OUTSTANDING
    # A lead with nothing wrong matches no chip but Everything, so
    # including it changes exactly one count — the one that was wrong
    # to leave it out.
    ctx = {'today': wb._today()}
    assert [k for k in wf.KEYS if wf.get(k).matches(quiet, ctx)
            and k not in ('everything', 'in_negotiation', 'quotations')] == []


def test_never_actioned_means_no_call_no_mail_no_meeting(world):
    with flask_app.app_context():
        d = wb.board(sc_mod.for_employee(REP), filter_key='never_actioned',
                     per_page=200)
    ids = {i['id'] for i in d['items']}
    assert world['virgin'] in ids
    assert world['healthy'] not in ids     # called today


def test_in_negotiation_excludes_leads_merely_awaiting_a_quote(world):
    """`quotations` includes RFQ Generated; `in_negotiation` does not.

    They are different questions — "is a quote in play" and "has the
    customer got our number" — and the Overview has always asked the
    second one.
    """
    with flask_app.app_context():
        sc = sc_mod.for_employee(REP)
        neg = {i['id'] for i in wb.board(
            sc, filter_key='in_negotiation', per_page=200)['items']}
        quo = {i['id'] for i in wb.board(
            sc, filter_key='quotations', per_page=200)['items']}
    assert world['awaiting'] in quo
    assert world['awaiting'] not in neg
    assert world['healthy'] in neg


def test_quiet_three_days_is_three_days_everywhere(world):
    assert wf.QUIET_DAYS == 3
    ctx = {'today': TODAY}
    fresh = {'kind': 'lead', 'last_activity_days': 2}
    old = {'kind': 'lead', 'last_activity_days': 3}
    never = {'kind': 'lead', 'last_activity_days': None}
    assert not wf.get('quiet_3d').matches(fresh, ctx)
    assert wf.get('quiet_3d').matches(old, ctx)
    # Never contacted is not "contacted nought days ago".
    assert wf.get('quiet_3d').matches(never, ctx)


def test_a_closed_lead_is_on_neither_screen(world):
    body = _client(REP).get('/api/my-work').get_json()
    with flask_app.app_context():
        d = wb.board(sc_mod.for_employee(REP), per_page=200)
    assert world['won'] not in {i['id'] for i in d['items']}
    assert body['pending']['in_negotiation'] == d['counts']['in_negotiation']


def test_another_persons_lead_is_on_neither_screen(world):
    body = _client(REP).get('/api/my-work').get_json()
    assert body['pending']['overdue'] >= 1
    with flask_app.app_context():
        d = wb.board(sc_mod.for_employee(REP), filter_key='overdue',
                     per_page=200)
    assert world['mates'] not in {i['id'] for i in d['items']}


# ── item 3: Mode ─────────────────────────────────────────────────────
def test_the_vocabulary_is_three_words():
    assert lead_mode.MODES == ('Sea', 'Air', 'Multimodal')


@pytest.mark.parametrize('given,want', [
    ('Sea', 'Sea'), ('sea', 'Sea'), ('  AIR ', 'Air'),
    ('multimodal', 'Multimodal'), ('Sea Freight', 'Sea'),
    ('Air Freight', 'Air'), ('ocean', 'Sea'), ('sea & air', 'Multimodal'),
    ('', None), (None, None), ('Rail', None), ('yes', None),
])
def test_normalise_takes_three_answers_and_refuses_the_rest(given, want):
    assert lead_mode.normalise(given) == want


def test_only_freight_forwarding_is_asked():
    assert lead_mode.applies_to('Freight Forwarding')
    assert lead_mode.applies_to('  freight forwarding ')
    assert not lead_mode.applies_to('Heavy Transport')
    assert not lead_mode.applies_to('')


def test_the_old_verticals_map_onto_two_of_the_three():
    assert lead_mode.from_vertical('Sea Freight') == 'Sea'
    assert lead_mode.from_vertical('air freight') == 'Air'
    # Multimodal has no old equivalent: the vertical pair could not
    # say it. Nothing backfills into it.
    assert 'Multimodal' not in lead_mode.FROM_VERTICAL.values()
    assert lead_mode.from_vertical('Heavy Transport') is None


def test_mode_saves_on_a_forwarding_lead(world):
    c = _client(ADMIN)
    r = c.put(f'/api/leads/{world["awaiting"]}',
              json={'procam_vertical': FF, 'procam_vertical_was': FF,
                    'mode': 'Air'})
    assert r.status_code == 200
    with flask_app.app_context():
        assert db.session.get(Lead, world['awaiting']).mode == 'Air'


def test_mode_is_refused_on_any_other_vertical(world):
    c = _client(ADMIN)
    c.put(f'/api/leads/{world["late"]}',
          json={'procam_vertical': OTHER, 'procam_vertical_was': OTHER,
                'mode': 'Sea'})
    with flask_app.app_context():
        assert db.session.get(Lead, world['late']).mode is None


def test_moving_a_lead_out_of_forwarding_clears_its_mode(world):
    c = _client(ADMIN)
    lid = world['awaiting']
    c.put(f'/api/leads/{lid}', json={'procam_vertical': FF,
                                     'procam_vertical_was': FF,
                                     'mode': 'Sea'})
    with flask_app.app_context():
        assert db.session.get(Lead, lid).mode == 'Sea'
    # The form always sends what it was rendered with, and the save
    # is refused without it — see `_drop_untouched`. A reclassify is
    # a genuine vertical change, so it carries one.
    c.put(f'/api/leads/{lid}', json={'procam_vertical': OTHER,
                                     'procam_vertical_was': FF})
    with flask_app.app_context():
        assert db.session.get(Lead, lid).mode is None


def test_an_unrecognised_mode_stores_nothing_rather_than_itself(world):
    c = _client(ADMIN)
    c.put(f'/api/leads/{world["awaiting"]}',
          json={'procam_vertical': FF, 'procam_vertical_was': FF,
                'mode': 'Camel'})
    with flask_app.app_context():
        assert db.session.get(Lead, world['awaiting']).mode is None


def test_a_save_that_mentions_neither_field_leaves_mode_alone(world):
    c, lid = _client(ADMIN), world['awaiting']
    c.put(f'/api/leads/{lid}', json={'procam_vertical': FF,
                                     'procam_vertical_was': FF,
                                     'mode': 'Sea'})
    c.put(f'/api/leads/{lid}', json={'city': 'Mumbai'})
    with flask_app.app_context():
        assert db.session.get(Lead, lid).mode == 'Sea'


def test_a_change_of_mode_is_audited(world):
    c, lid = _client(ADMIN), world['awaiting']
    c.put(f'/api/leads/{lid}', json={'procam_vertical': FF,
                                     'procam_vertical_was': FF,
                                     'mode': 'Sea'})
    with flask_app.app_context():
        rows = AuditEvent.query.filter(
            AuditEvent.entity_type == 'lead',
            AuditEvent.entity_id == str(lid)).all()
    assert any('mode' in (r.new_value or {}) for r in rows), \
        'a mode change left no audit row'


def test_mode_is_on_the_record_the_screen_reads(world):
    body = _client(ADMIN).get(f'/api/leads/{world["awaiting"]}').get_json()
    assert 'mode' in body


def test_the_board_filters_by_mode(world):
    c = _client(ADMIN)
    c.put(f'/api/leads/{world["awaiting"]}',
          json={'procam_vertical': FF, 'procam_vertical_was': FF,
                'mode': 'Air'})
    with flask_app.app_context():
        wb.hygiene_cache_clear()
        sc = sc_mod.for_employee(ADMIN)
        air = wb.board(sc, mode='Air', per_page=200)
        sea = wb.board(sc, mode='Sea', per_page=200)
    assert world['awaiting'] in {i['id'] for i in air['items']}
    assert world['awaiting'] not in {i['id'] for i in sea['items']}
    # Narrowing by mode narrows the counts with it, or the tiles and
    # the list disagree the moment somebody uses the filter.
    assert air['counts']['everything'] == air['total']


def test_the_vocabulary_is_served_not_hard_coded_in_the_page(world):
    body = _client(ADMIN).get('/api/workbench/filters').get_json()
    assert body['modes'] == list(lead_mode.MODES)
    assert body['mode_vertical'] == FF
    opts = _client(ADMIN).get('/api/dashboard/options').get_json()
    assert opts['modes'] == list(lead_mode.MODES)


def test_reports_filter_and_break_down_by_mode(world):
    c = _client(ADMIN)
    c.put(f'/api/leads/{world["awaiting"]}',
          json={'procam_vertical': FF, 'procam_vertical_was': FF,
                'mode': 'Air'})
    body = c.get('/api/dashboard/summary?mode=Air').get_json()
    assert body['distributions']['modes'].get('Air') == 1
    assert 'Sea' not in body['distributions']['modes']


# ── item 1: the stale outbox rows ────────────────────────────────────
def _queued(key, **kw):
    row = EmailOutbox(dedupe_key=TAG + key, event_key='stale.alert',
                      to_addr='somebody@procamgroup.in',
                      subject='Stale alert', html='<p>x</p>',
                      status=kw.pop('status', 'failed'),
                      created_at=kw.pop('created_at',
                                        datetime(2026, 10, 6, 9, 0)))
    for k, v in kw.items():
        setattr(row, k, v)
    db.session.add(row)
    return row


def test_only_the_rows_the_release_note_describes_are_cancelled(world):
    with flask_app.app_context():
        stale = _queued('stale', last_error='the mail server refused it')
        graded = _queued('graded', smtp_code=550,
                         last_error='mailbox does not exist')
        recent = _queued('recent', created_at=datetime(2026, 10, 15, 9, 0))
        queued = _queued('queued', status='queued')
        sent = _queued('sent', status='sent')
        db.session.commit()
        picked = {r.dedupe_key for r in canceller.stale_rows()}
        assert picked == {TAG + 'stale'}
        for row in (graded, recent, queued, sent):
            assert row.dedupe_key not in picked
        assert stale.dedupe_key in picked


def test_cancelling_never_deletes_and_never_requeues(world):
    with flask_app.app_context():
        _queued('tocancel', last_error='the mail server refused it')
        db.session.commit()
        before = EmailOutbox.query.filter(
            EmailOutbox.dedupe_key.like(TAG + '%')).count()
        rows = canceller.stale_rows()
        assert canceller.cancel(rows) == len(rows)
        db.session.commit()
        after = EmailOutbox.query.filter(
            EmailOutbox.dedupe_key.like(TAG + '%')).all()
        assert len(after) == before, 'a row was deleted'
        row = [r for r in after if r.dedupe_key == TAG + 'tocancel'][0]
        assert row.status == 'cancelled'
        assert 'Not resent' in row.last_error
        # Cancelled is not a status the worker picks up.
        assert row.status not in ('queued', 'sending')


def test_running_it_twice_cancels_nothing_the_second_time(world):
    with flask_app.app_context():
        _queued('twice', last_error='the mail server refused it')
        db.session.commit()
        assert canceller.cancel(canceller.stale_rows()) >= 1
        db.session.commit()
        assert canceller.stale_rows() == []


def test_the_note_fits_the_column(world):
    assert len(canceller.NOTE) <= 500


# ── item 4: the warning that is not there ────────────────────────────
def test_no_check_the_vertical_before_saving_warning_exists():
    """Item 4 asked for a warning to be removed. There is none.

    This is the test that says so, and that keeps saying so: if
    somebody adds one later, it fails and they find out here rather
    than from a user asking what the CRM wants them to check.

    Templates and application code only — the places user-facing help
    text actually comes from. The release note discusses the warning
    in order to record that it does not exist, and a search that
    cannot tell the two apart would fail on its own evidence. The
    `help_articles` and `help_tooltips` tables were searched on
    production by hand and hold nothing either; no unit test can
    reach them.
    """
    import glob
    import re
    pat = re.compile(r'vertical[^.<>\n]{0,60}before\s+(you\s+)?sav',
                     re.IGNORECASE)
    hits = []
    for pattern in ('templates/**/*.html', 'app/**/*.py', '*.py'):
        for path in glob.glob(os.path.join(_ROOT, pattern), recursive=True):
            with open(path, encoding='utf-8', errors='ignore') as fh:
                text = fh.read()
            for m in pat.finditer(text):
                hits.append(f'{os.path.relpath(path, _ROOT)}: '
                            f'{text[max(0, m.start()-40):m.end()+20]!r}')
    assert not hits, 'a "check the vertical before saving" warning:\n  ' \
                     + '\n  '.join(hits)


# ── the cap that was reading as a count ──────────────────────────────
def test_the_task_ceiling_is_a_safety_stop_not_a_page_size():
    """It used to be 500, and the board counts what it reads.

    Every open task on production is overdue and tasks sort due-date
    first, so the Overdue tile read exactly 500 for every viewer: an
    admin with 7,318 overdue tasks and a rep with 587 saw the same
    number, and it belonged to neither of them. The ceiling has to sit
    well above any real workload, and the board has to say when it is
    reached rather than present a limit as an answer.
    """
    assert wb.TASK_CEILING >= 20000


def test_the_board_says_when_it_truncated(world):
    with flask_app.app_context():
        d = wb.board(sc_mod.for_employee(ADMIN), per_page=1)
    assert d['truncated'] is False, \
        'nothing here is near the ceiling, so nothing should be flagged'


def test_a_truncated_board_is_flagged_rather_than_quietly_short(
        world, monkeypatch):
    monkeypatch.setattr(wb, 'TASK_CEILING', 0)
    monkeypatch.setattr(
        wb, 'task_items',
        lambda sc, today=None: [{'kind': 'task', 'id': -1, 'group': 'x',
                                 'priority': 4, 'due': '', 'account': '',
                                 'account_id': None, 'title': 't',
                                 'vertical': '', 'stage': '',
                                 'value_inr': None, 'ageing_days': 0,
                                 'assigned_to': '', 'conditions': [],
                                 'last_activity_days': None}])
    with flask_app.app_context():
        d = wb.board(sc_mod.for_employee(ADMIN), per_page=1)
    assert d['truncated'] is True
