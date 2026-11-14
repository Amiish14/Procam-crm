"""One lead per portal event, not one per message.

14 Nov, after `084e1b2` went live. The denylist had held back 74
messages from two procurement portals, and re-processing them as they
stood would have made 74 leads: "~30 are 'withdrawn' notices and most
of the rest are reminders."

So the message is not the unit of work -- the EVENT is. Every mail a
portal sends about one auction carries its number, so the number holds
the set together: the invitation becomes the lead, and the reminder,
the corrigendum, the "about to end" and the withdrawal all land on
that lead's trail. Two of them do more than land:

  * **withdrawn** closes the lead as Lost, "withdrawn by buyer";
  * **about to end** states the minute, which is the deadline.

The mechanism is `Lead.portal_key`, which CRM-COCKPIT-02 §5.6 built
for the Indo Tech reverse auctions. What was missing was the two
reported portals' own numbering -- RFX-, RFQ-, an Ariba document or
event id -- and the notices.

AND THE ONE THAT IS DELIBERATELY NOT A LEAD

A withdrawal of an event the CRM never saw. Its invitation was dropped
by the denylist weeks ago; a lead made from the withdrawal would be
born Lost and nobody can act on it. It is written into the ingest log
with its number and no lead is created, which is a refusal with a
record and not a silent drop -- the distinction §4 of CRM-WA-01 was
about.
"""
import ast
import importlib
import os
import sys
import tempfile
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'portalev-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'PortalEv-12345678')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'portalev.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app = _main.app
db = _main.db
Lead = _main.Lead

from app.services import (portal_event, portal_rfq,          # noqa: E402
                          quote_due)

SINGLE = os.path.join(_ROOT, 'email_ingest', 'single_message.py')
SCRIPT = os.path.join(_ROOT, 'scripts', '2026_11_14_reprocess_mail.py')
PORTAL = 'no-reply@procol.in'
ARIBA = 's4system-prodeu+procam@ansmtp.ariba.com'


# ── the event key ────────────────────────────────────────────────────
def test_the_key_the_portals_actually_print():
    assert portal_rfq.event_key(PORTAL, 'RFX 44711 — invitation') == \
        'RFX-44711'
    assert portal_rfq.event_key(PORTAL, 'RFQ-10244 published') == \
        'RFQ-10244'
    assert portal_rfq.event_key(
        ARIBA, 'Doc1234567890 - Invitation to participate') == \
        'ARIBA-DOC1234567890'
    assert portal_rfq.event_key(
        ARIBA, 'An event you are invited to',
        'Event ID: WS-7781 has been published') == 'ARIBA-EVENTWS-7781'


def test_the_existing_key_is_untouched_and_still_wins():
    """§5.6's own numbers keep their own form, so a lead already keyed
    on RA-57103 is still found by it."""
    assert portal_rfq.event_key(PORTAL, 'RA- 57103: Indo Tech') == \
        'RA-57103'
    assert quote_due.portal_key('RA- 57103: Indo Tech') == 'RA-57103'
    # And the narrow reader is unchanged for everybody else.
    assert quote_due.portal_key('RFX 44711 — invitation') == ''


def test_the_wide_key_is_only_ever_read_for_a_portal():
    """The point of the whole design. "RFQ-001" in two customers' own
    mail is two customers, and keying on it would merge them."""
    assert portal_rfq.event_key('buyer@a-real-client.example',
                                'RFQ-001 for Nhava Sheva') == ''
    assert portal_rfq.event_key('', 'RFQ-001 for Nhava Sheva') == ''
    assert portal_rfq.event_key(PORTAL, 'RFQ-001 for Nhava Sheva') == \
        'RFQ-001'


def test_a_message_with_no_number_has_no_event():
    assert portal_rfq.event_key(PORTAL, 'Your response is due') == ''


# ── the kinds ────────────────────────────────────────────────────────
def test_the_five_kinds_are_told_apart():
    k = portal_rfq.notice_kind
    assert k('RFX 44711: invitation to quote') == portal_rfq.INVITE
    assert k('Reminder: RFX 44711 awaits your response') == \
        portal_rfq.REMINDER
    assert k('RFX 44711 technical bid live') == portal_rfq.TECHNICAL
    assert k('RFX 44711 is about to end') == portal_rfq.ENDING
    assert k('RFX 44711 has been withdrawn') == portal_rfq.WITHDRAWN


def test_a_withdrawal_of_an_ending_event_is_a_withdrawal():
    """Ordered deliberately: the strongest reading wins, because the
    one that closes the lead must not be read as the one that moves
    its deadline."""
    assert portal_rfq.notice_kind(
        'RFX 44711 was about to end and has been withdrawn') == \
        portal_rfq.WITHDRAWN


def test_the_kind_is_read_from_the_body_as_well():
    """Ariba puts the document number in the subject and the sentence
    in the first paragraph."""
    assert portal_rfq.notice_kind(
        'Doc1234567890', 'This event has been withdrawn by the buyer.'
        ) == portal_rfq.WITHDRAWN


def test_is_notice_excludes_the_invitation():
    assert not portal_rfq.is_notice(portal_rfq.INVITE)
    for kind in (portal_rfq.REMINDER, portal_rfq.TECHNICAL,
                 portal_rfq.ENDING, portal_rfq.WITHDRAWN):
        assert portal_rfq.is_notice(kind)


def test_describe_carries_the_event_and_the_kind():
    found = portal_rfq.describe(PORTAL, 'RFX 44711 has been withdrawn',
                                'Buyer organisation: ZP Buyer Works')
    assert found['event'] == 'RFX-44711'
    assert found['notice'] == portal_rfq.WITHDRAWN
    assert found['buyer'] == 'ZP Buyer Works'
    assert 'RFX-44711' in found['reason']
    assert 'withdrawn' in found['reason']


# ── what a notice does to the lead ───────────────────────────────────
def _lead(**kw):
    kw.setdefault('company', 'ZP Buyer Works')
    kw.setdefault('stage', 'RFQ Generated')
    kw.setdefault('portal_key', 'RFX-44711')
    lead = Lead(**kw)
    db.session.add(lead)
    db.session.commit()
    return lead


def _purge():
    from app.models.audit import AuditEvent

    ids = [l.id for l in Lead.query.filter(
        Lead.company.like('ZP%')).all()]
    if ids:
        AuditEvent.query.filter(
            AuditEvent.entity_type == 'lead',
            AuditEvent.entity_id.in_([str(i) for i in ids])).delete(
                synchronize_session=False)
        Lead.query.filter(Lead.id.in_(ids)).delete(
            synchronize_session=False)
    db.session.commit()


def test_a_withdrawal_closes_the_lead_in_the_words_asked_for():
    with flask_app.app_context():
        db.create_all()
        _purge()
        lead = _lead()
        said = portal_event.apply_notice(
            lead, portal_rfq.WITHDRAWN,
            subject='RFX 44711 has been withdrawn')
        db.session.commit()
        assert lead.stage == 'Lost'
        assert lead.lost_reason == 'withdrawn by buyer'
        assert 'Lost' in said
        _purge()


def test_the_close_keeps_the_stage_it_came_from_in_the_audit():
    """Reversible from the record: "it was at RFQ Generated when the
    buyer pulled the event" is the question asked afterwards."""
    from app.models.audit import AuditEvent

    with flask_app.app_context():
        db.create_all()
        _purge()
        lead = _lead(stage='Quoted')
        portal_event.apply_notice(lead, portal_rfq.WITHDRAWN)
        db.session.commit()
        row = (AuditEvent.query
               .filter(AuditEvent.entity_type == 'lead',
                       AuditEvent.entity_id == str(lead.id),
                       AuditEvent.action == 'lead.closed_lost')
               .order_by(AuditEvent.id.desc()).first())
        assert row is not None
        assert (row.old_value or {}).get('stage') == 'Quoted'
        assert (row.new_value or {}).get('lost_reason') == \
            portal_rfq.LOST_REASON
        assert row.actor == portal_event.ACTOR
        _purge()


def test_a_withdrawal_does_not_touch_a_lead_somebody_already_closed():
    """A machine must not overwrite a person's own lost reason."""
    with flask_app.app_context():
        db.create_all()
        _purge()
        lead = _lead(stage='Won', lost_reason=None)
        said = portal_event.apply_notice(lead, portal_rfq.WITHDRAWN)
        db.session.commit()
        assert lead.stage == 'Won'
        assert lead.lost_reason is None
        assert 'left alone' in said
        _purge()


def test_a_withdrawal_never_reopens_anything():
    with flask_app.app_context():
        db.create_all()
        _purge()
        lead = _lead(stage='Lost', lost_reason='Price too high')
        portal_event.apply_notice(lead, portal_rfq.WITHDRAWN)
        db.session.commit()
        assert lead.stage == 'Lost'
        assert lead.lost_reason == 'Price too high'
        _purge()


def test_about_to_end_sets_the_deadline_unconfirmed():
    with flask_app.app_context():
        db.create_all()
        _purge()
        lead = _lead()
        soon = datetime.utcnow() + timedelta(days=3)
        body = (f'The event RFX 44711 is about to end on '
                f'{soon.strftime("%d %b %Y")} 15:15:00 IST.')
        said = portal_event.apply_notice(
            lead, portal_rfq.ENDING,
            subject='RFX 44711 is about to end', body=body,
            received_at=datetime.utcnow())
        db.session.commit()
        assert lead.quote_due_at is not None
        assert lead.quote_due_confirmed is not True
        assert 'deadline' in said
        _purge()


def test_the_phrase_itself_reads_as_a_deadline():
    """The reader had every "closes on" phrase and not this one, which
    is the phrase the portals' own notice uses."""
    soon = datetime.utcnow() + timedelta(days=4)
    reading = quote_due.read(
        'RFX 44711',
        f'This event is about to end on {soon.strftime("%d %b %Y")} '
        f'18:00 IST.', received_at=datetime.utcnow())
    assert reading is not None
    assert reading.at.date() == (soon - timedelta(hours=0)).date()


def test_a_reminder_with_no_date_changes_nothing():
    with flask_app.app_context():
        db.create_all()
        _purge()
        lead = _lead()
        said = portal_event.apply_notice(
            lead, portal_rfq.REMINDER,
            subject='Reminder: your response is due',
            body='Please respond on the portal.')
        db.session.commit()
        assert said == ''
        assert lead.quote_due_at is None
        assert lead.stage == 'RFQ Generated'
        _purge()


def test_apply_notice_never_raises_on_a_lead_it_cannot_use():
    assert portal_event.apply_notice(None, portal_rfq.WITHDRAWN) == ''
    assert portal_event.apply_notice(object(), portal_rfq.WITHDRAWN) == ''


# ── the intake: one lead per event ───────────────────────────────────
def _fn(path, name):
    tree = ast.parse(open(path).read())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f'{name} not found in {path}')


def test_the_intake_keys_on_the_event_not_the_narrow_number():
    """The join and the lead it joins to must read the SAME key. They
    did not: the lookup was widened and the write was not, so a lead
    made from an RFX- mail was stored with no key and the next notice
    about it could only become a second lead."""
    source = open(SINGLE).read()
    assert source.count('event_key(') >= 2
    assert 'lead.portal_key = _prf.event_key(' in source
    # And the narrow reader is no longer the one writing the column.
    assert 'lead.portal_key = _qd.portal_key(' not in source


def test_the_notice_is_applied_where_the_mail_is_filed():
    source = open(SINGLE).read()
    assert '_pev.apply_notice(' in source
    marker = source.index('_pev.apply_notice(')
    before = source[:marker]
    assert '_file_against_lead' in before


def test_a_closed_event_lead_still_receives_its_notices():
    """The open-stages lookup alone would make a second lead out of
    the withdrawal of an auction somebody had already closed."""
    node = _fn(SINGLE, 'process_single_message')
    source = ast.unparse(node)
    assert 'Lead.portal_key == _pkey' in source
    # Twice: the open lead first, then any lead, for a portal.
    assert source.count('Lead.portal_key == _pkey') == 2
    assert 'order_by(Lead.id.desc())' in source


def test_a_withdrawal_with_no_lead_creates_none_and_says_so():
    """The refusal, with a record — not a silent drop."""
    node = _fn(SINGLE, 'process_single_message')
    found = False
    for branch in ast.walk(node):
        if not isinstance(branch, ast.If):
            continue
        text = ast.unparse(branch)
        if '_notice == _prf.WITHDRAWN' not in text:
            continue
        assert '_record_ingest' in text
        assert "'skipped'" in text
        assert 'withdrawn by the' in text
        assert 'Lead(' not in text
        found = True
    assert found, 'the withdrawn-with-no-lead branch is gone'


def test_every_branch_that_returns_still_writes_a_log_row():
    """CRM-USER-ISSUES-02NOV §4's guard, asked again here because
    this change adds a branch that returns."""
    import importlib.util

    path = os.path.join(_ROOT, 'tests', 'test_unprocessed_mail_log.py')
    spec = importlib.util.spec_from_file_location('zp_guard', path)
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    assert guard._returns_without_logging(
        SINGLE, 'process_single_message') == []


# ── the script: events, not messages ────────────────────────────────
def _script():
    import importlib.util

    spec = importlib.util.spec_from_file_location('zp_reproc', SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Row:
    def __init__(self, rid, subject, when, sender=PORTAL):
        self.id = rid
        self.subject = subject
        self.from_addr = sender
        self.received_at = when
        self.created_at = when
        self.internet_message_id = f'<zp-{rid}@portal>'
        self.reason = 'blocked sender domain: procol.in'


def _backlog():
    base = datetime(2026, 11, 1, 9, 0)
    return [
        (_Row(1, 'RFX 44711: invitation to quote', base), 'procol.in'),
        (_Row(2, 'Reminder: RFX 44711 awaits your response',
              base + timedelta(days=1)), 'procol.in'),
        (_Row(3, 'RFX 44711 is about to end',
              base + timedelta(days=2)), 'procol.in'),
        (_Row(4, 'RFX 44712 published', base + timedelta(days=1)),
         'procol.in'),
        (_Row(5, 'RFX 44712 has been withdrawn',
              base + timedelta(days=3)), 'procol.in'),
        (_Row(6, 'RFX 44713 has been withdrawn',
              base + timedelta(days=4)), 'procol.in'),
        (_Row(7, 'Your response is due', base + timedelta(days=5)),
         'procol.in'),
    ]


def test_seven_messages_are_four_events():
    mod = _script()
    with flask_app.app_context():
        groups = dict(mod.group_events(_backlog()))
    assert len(groups) == 4
    assert len(groups['RFX-44711']) == 3
    assert len(groups['RFX-44712']) == 2
    assert len(groups['RFX-44713']) == 1
    # The one with no number is its own event, keyed so it cannot
    # collide with another.
    assert any(k.startswith('(no number') for k in groups)


def test_the_events_are_ordered_by_their_first_message():
    mod = _script()
    with flask_app.app_context():
        keys = [key for key, _m in mod.group_events(_backlog())]
    assert keys[0] == 'RFX-44711'


def test_the_plan_says_how_many_leads_and_refuses_the_withdrawn_only():
    mod = _script()
    with flask_app.app_context():
        db.create_all()
        _purge()
        planned = mod.plan(mod.group_events(_backlog()))
        verdicts = {key: verdict for key, _m, verdict, _n in planned}
        assert verdicts['RFX-44711'] == 'create'
        assert verdicts['RFX-44712'] == 'create'
        # Nothing but a withdrawal, and no lead to close.
        assert verdicts['RFX-44713'] == 'no lead'
        creates = [k for k, v in verdicts.items() if v == 'create']
        assert len(creates) == 3          # 44711, 44712, the unnumbered
        notes = {key: note for key, _m, _v, note in planned}
        assert 'closed as Lost' in notes['RFX-44712']
        assert 'deadline' in notes['RFX-44711']
        assert 'body is read' in [n for k, n in notes.items()
                                  if k.startswith('(no number')][0]
        _purge()


def test_an_event_that_already_has_a_lead_only_attaches():
    mod = _script()
    with flask_app.app_context():
        db.create_all()
        _purge()
        lead = _lead(portal_key='RFX-44711')
        planned = mod.plan(mod.group_events(_backlog()))
        verdicts = {key: verdict for key, _m, verdict, _n in planned}
        assert verdicts['RFX-44711'] == 'attach'
        notes = {key: note for key, _m, _v, note in planned}
        assert f'#{lead.id}' in notes['RFX-44711']
        _purge()


def test_the_dry_run_prints_events_and_a_lead_count(capsys):
    mod = _script()
    with flask_app.app_context():
        db.create_all()
        _purge()
        mod.report_portals(_backlog())
        out = capsys.readouterr().out
        _purge()
    assert '7 blocked message(s)' in out
    assert '4 portal EVENT(S)' in out
    assert 'lead(s) would be CREATED' in out
    assert 'NO lead created' in out
    # The honest limit, printed rather than assumed away.
    assert 'upper bound' in mod.group_events.__doc__


def test_the_apply_runs_an_event_oldest_first():
    """The invitation makes the lead; a reminder processed first would
    make the lead and leave the cargo on a trail line.

    Asserted on the order itself, not on the shape of the code: an
    --apply cannot be run here without a Graph token, and this is the
    half of it that can be checked without one.
    """
    mod = _script()
    with flask_app.app_context():
        db.create_all()
        _purge()
        planned = mod.plan(mod.group_events(_backlog()))
        order = mod.apply_order(planned)
        _purge()
    # Every event's messages are contiguous and oldest-first.
    assert order[:3] == ['<zp-1@portal>', '<zp-2@portal>',
                         '<zp-3@portal>']
    assert order.index('<zp-4@portal>') < order.index('<zp-5@portal>')
    assert len(order) == 7
    assert len(set(order)) == 7


def test_the_apply_asks_for_every_message_including_the_refused_event():
    """A withdrawn-only event is still re-processed — it is the
    processor that refuses to make a lead, and it writes the ingest
    row that says why. Dropping it here instead would be the silent
    version of the same decision."""
    mod = _script()
    with flask_app.app_context():
        db.create_all()
        _purge()
        planned = mod.plan(mod.group_events(_backlog()))
        order = mod.apply_order(planned)
        _purge()
    assert '<zp-6@portal>' in order


def test_the_csv_carries_the_event_and_the_kind():
    source = open(SCRIPT).read()
    assert "'event', 'notice'" in source


# ── item 2: the two figures, on the CRM's own clock ─────────────────
def _status():
    import importlib.util

    path = os.path.join(_ROOT, 'scripts', 'mail_ingest_status.py')
    spec = importlib.util.spec_from_file_location('zp_status', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_landing_is_timed_by_the_decision_not_the_postmark():
    """Item 2, the half the first fix missed. Ordering was made
    consistent but on `received_at` -- the date the customer sent it.
    A mail received on 3 November and turned into a lead on the 12th
    reported 2026-11-03, which is the sentence live was still
    printing, and the portal backlog is nothing but old mail landing
    now."""
    mod = _status()
    row = type('Row', (), {
        'received_at': datetime(2026, 11, 3),
        'created_at': datetime(2026, 11, 3),
        'updated_at': datetime(2026, 11, 12),
        'outcome': 'created'})()
    assert mod._when(row) == (datetime(2026, 11, 3), 'received_at')
    assert mod._decided(row) == (datetime(2026, 11, 12), 'updated_at')


def test_a_row_never_updated_is_timed_by_when_it_was_written():
    mod = _status()
    row = type('Row', (), {'received_at': datetime(2026, 10, 1),
                           'created_at': datetime(2026, 11, 11),
                           'updated_at': None,
                           'outcome': 'created'})()
    assert mod._decided(row) == (datetime(2026, 11, 11), 'created_at')


def test_a_row_with_no_decision_time_falls_back_and_says_so():
    mod = _status()
    row = type('Row', (), {'received_at': datetime(2026, 11, 2),
                           'created_at': None, 'updated_at': None,
                           'outcome': 'created'})()
    value, column = mod._decided(row)
    assert value == datetime(2026, 11, 2)
    assert 'no decision time' in column
    # And one with nothing at all is counted nowhere, as before.
    empty = type('Row', (), {'received_at': None, 'created_at': None,
                             'updated_at': None, 'outcome': 'created'})()
    assert mod._decided(empty) == (None, '')


def test_the_two_figures_cannot_disagree(capsys):
    """The invariant item 2 is really about: if the window counts
    landings, the headline date must be inside the window."""
    from app.models.ingest_log import MailIngestLog

    mod = _status()
    with flask_app.app_context():
        db.create_all()
        MailIngestLog.query.filter(
            MailIngestLog.internet_message_id.like('<zp-st%')).delete(
                synchronize_session=False)
        db.session.commit()
        old = datetime.utcnow() - timedelta(days=40)
        row = MailIngestLog(internet_message_id='<zp-st1@x>',
                            outcome='created', received_at=old,
                            created_at=old, subject='Old mail, landed now')
        db.session.add(row)
        db.session.commit()
        # Landed today, received six weeks ago: the exact shape.
        row.updated_at = datetime.utcnow()
        db.session.commit()
        mod.report_last(14)
        out = capsys.readouterr().out
        MailIngestLog.query.filter(
            MailIngestLog.internet_message_id == '<zp-st1@x>').delete(
                synchronize_session=False)
        db.session.commit()
    assert 'should now be impossible' not in out
    assert '0 message(s) became something' not in out
    assert 'the mail itself was received' in out
    assert 'back-processed' in out


def test_the_status_script_still_only_reads():
    source = open(os.path.join(
        _ROOT, 'scripts', 'mail_ingest_status.py')).read()
    tree = ast.parse(source)
    options = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and getattr(node.func, 'attr', None) == 'add_argument'):
            for arg in node.args:
                if isinstance(arg, ast.Constant):
                    options.add(arg.value)
    assert '--apply' not in options
    called = {getattr(n.func, 'attr', None) for n in ast.walk(tree)
              if isinstance(n, ast.Call)}
    assert 'commit' not in called and 'delete' not in called


# ── the fault that made both of the above do nothing ────────────────
def _read_before_import(path):
    """Names a function imports further down and reads further up.

    Both of 13 Nov's promotional rule and 14 Nov's portal rule were
    written correctly and did nothing at all on live, for the same
    reason: `lead_intake as _li` is imported two-thirds of the way
    down `process_single_message`, which makes `_li` local to the
    whole function, so the two rules above it raised
    UnboundLocalError. Both sat inside a `try` that logs and carries
    on, and every test asserting on the rules themselves passed.

    So the rule is checked where the fault was: a name bound by an
    import inside a function must not be read above that import.
    """
    bad = []
    tree = ast.parse(open(path).read())
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        binds = {}
        for node in ast.walk(fn):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    name = alias.asname or alias.name.split('.')[0]
                    binds[name] = min(binds.get(name, node.lineno),
                                      node.lineno)
        for node in ast.walk(fn):
            if (isinstance(node, ast.Name)
                    and isinstance(node.ctx, ast.Load)
                    and node.id in binds
                    and node.lineno < binds[node.id]):
                bad.append((fn.name, node.id, node.lineno,
                            binds[node.id]))
    return sorted(set(bad))


def test_nothing_in_the_intake_reads_an_import_from_below_it():
    assert _read_before_import(SINGLE) == []


def test_nor_anywhere_else_mail_is_decided():
    for folder in ('email_ingest', 'app/services'):
        base = os.path.join(_ROOT, *folder.split('/'))
        for here, _dirs, files in os.walk(base):
            for name in sorted(files):
                if not name.endswith('.py'):
                    continue
                path = os.path.join(here, name)
                assert _read_before_import(path) == [], path


def test_the_portal_is_bound_once_and_at_the_top():
    """Not inside a `try` that something below it reads. It was
    imported inside two different `try` blocks and read outside both,
    which is the same fault a second time."""
    source = open(SINGLE).read()
    assert source.count('import portal_rfq as') == 1
    head = source[source.index('def process_single_message'):]
    head = head[:head.index('imid = _internet_message_id(msg)')]
    assert 'import portal_rfq as _prf' in head
    assert 'import lead_intake as _li' in head


def test_the_classifier_cannot_drop_a_portal_rfq_either():
    """The second half of the same fault: the denylist was taught that
    a portal wins and the classifier was not, so a portal RFQ was
    skipped as `I_non_business` -- "no cargo, RFQ, route or contact
    signal", which is what a template with the cargo behind a login
    looks like."""
    node = _fn(SINGLE, 'process_single_message')
    source = ast.unparse(node)
    assert 'not decision.creates_lead' in source
    marker = source.index('if _portal and decision is not None')
    skip = source.index('if decision is not None and '
                        '(not decision.creates_lead):')
    # The portal's exemption is read BEFORE the branch that skips.
    assert marker < skip


def test_the_portals_own_name_is_never_the_company():
    """The parser reads a company out of the sender domain, so the
    first portal lead was filed under "Procol" — not empty and not a
    placeholder, so the buyer read out of the body was never used."""
    assert portal_rfq.names_the_portal('Procol', 'procol.in')
    assert portal_rfq.names_the_portal('Ariba', 'ansmtp.ariba.com')
    assert portal_rfq.names_the_portal('SAP Ariba Support')
    assert not portal_rfq.names_the_portal('ZQ Cement Works',
                                           'procol.in')
    assert not portal_rfq.names_the_portal('', 'procol.in')
    source = open(SINGLE).read()
    assert '_prf.names_the_portal(_named' in source


def test_the_same_event_written_two_ways_is_one_event():
    """"RFX 44711" in the invitation and "RFx no. 44711" in the
    reminder are the same auction, and reading them as two would make
    the second lead this change exists to prevent."""
    assert portal_rfq.event_key(PORTAL, 'RFX 44711: invitation') == \
        portal_rfq.event_key(PORTAL, 'Reminder: RFx no. 44711')
    assert portal_rfq.event_key(PORTAL, 'RFQ #10244 published') == \
        'RFQ-10244'
    assert portal_rfq.event_key(PORTAL, 'RFX id 44711 ends today') == \
        'RFX-44711'


def test_the_desk_branch_assigns_rather_than_only_deciding():
    """It returned the desk's code and never wrote it, and its caller
    ignores the return value — so item 11's routing chose the right
    seat and the lead arrived with nobody on it. Every sibling path in
    that function goes through `lead_assignment.assign`."""
    node = _fn(SINGLE, 'auto_assign_owners')
    source = ast.unparse(node)
    marker = source.index('_desk_for_a_guessed_vertical(lead)')
    tail = source[marker:marker + 900]
    assert 'lead_assignment.assign(' in tail
    assert 'primary_code=_desk' in tail
    # And a refusal falls through rather than leaving the lead empty.
    assert 'if _ok:' in tail
