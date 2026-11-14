"""The backlog run that left ten leads behind, and why.

`--apply --portals` reported "9 created, 65 skipped" against a dry run
that said up to 19 leads, and that was the whole of the report: the
loop printed the status and the lead id and threw the REASON away.
"Previously purged", "already classified", "joined lead 12644" and
"withdrawn, with no lead" are four entirely different situations and
every one of them prints as `skipped`.

Three faults, and the first is the reporting itself.

ONE · THE REASON WAS NEVER PRINTED

It was in the ingest log the whole time, because `_record_ingest`
writes it. `--check --why` reads the rows and prints the reason per
message without re-processing anything.

TWO · ARIBA NUMBERS ITS INVITATION DIFFERENTLY FROM ITS NOTICES

The invitation arrives as `Doc2472440490`, and every mail after it
names the event: `RFQ_CHA_TPT_JSL_6500005154`. Keying the first on the
document id and the rest on the name turns one auction into three
leads, which is the thing the key exists to prevent. The event's NAME
is now read first, from the subject or the body, and the document id
only when there is no name anywhere -- the name is in all three mails
and the document id is in one.

THREE · `force` DOES NOT OVERRIDE A PURGE

A message somebody deleted as irrelevant is refused whatever `force`
says -- rightly, because a re-poll must not undo a person's decision.
But an operator re-processing a backlog of procurement-portal mail has
made the opposite decision deliberately and about these messages.
`--include-purged` is how they say so, and every override writes an
audit row: overriding somebody's deletion silently would be worse than
refusing to do it.

AND THE BUYER, WHICH WAS BEING THROWN AWAY

The parser reads a company out of the subject of a robot's mail and
produced "participate in an event" and "quote" as customer names,
while the body said "Jindal Stainless Limited has invited you". The
body's buyer now wins.
"""
import ast
import importlib
import os
import sys
import tempfile
import types
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.modules.setdefault('msal', types.ModuleType('msal'))

os.environ['URL_PREFIX'] = ''
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'portalbacklog')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'PortalBacklog12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'portalbacklog.db')
os.environ['LEAD_INTAKE_MODE'] = 'enforce'
os.environ['LEAD_INTAKE_AI'] = 'off'

_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Lead, EmailEvent = _main.Lead, _main.EmailEvent

from app.models.audit import AuditEvent                      # noqa: E402
from app.models.ingest_log import MailIngestLog              # noqa: E402
from app.services import portal_rfq                          # noqa: E402
from email_ingest import single_message as SM                 # noqa: E402

MAILBOX = 'leads@procamgroup.in'
PROCOL = 'no-reply@procol.in'
ARIBA = 's4system-prodeu+jindalstainless@ansmtp.ariba.com'
SCRIPT = os.path.join(_ROOT, 'scripts', '2026_11_14_reprocess_mail.py')
KEYS = os.path.join(_ROOT, 'scripts', '2026_11_13_portal_keys.py')

with flask_app.app_context():
    db.create_all()


def _load(path, name):
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _msg(mid, sender, subject, body, when='2026-11-10T09:00:00Z'):
    return {'id': 'AAMk' + mid.strip('<>@.').replace('-', ''),
            'internetMessageId': mid, 'subject': subject,
            'body': {'contentType': 'text', 'content': body},
            'receivedDateTime': when,
            'from': {'emailAddress': {'address': sender}},
            'toRecipients': [{'emailAddress': {'address': MAILBOX}}]}


def _leads(key):
    with flask_app.app_context():
        return Lead.query.filter_by(portal_key=key).all()


# ── two · the Ariba event name ──────────────────────────────────────
JINDAL = (
    ('<j1@ariba>', 'Doc2472440490 - Jindal Stainless Limited has invited '
                   'you to participate in an event',
     'Jindal Stainless Limited has invited you to participate in the '
     'following event: RFQ_CHA_TPT_JSL_6500005154.\nThe event starts '
     'on 10 Nov 2026.\n'),
    ('<j2@ariba>', 'Event RFQ_CHA_TPT_JSL_6500005154 has been published',
     'The event RFQ_CHA_TPT_JSL_6500005154 has been published.\n'),
    ('<j3@ariba>', '[Reminder] Event RFQ_CHA_TPT_JSL_6500005154 is about '
                   'to end',
     'The event RFQ_CHA_TPT_JSL_6500005154 is about to end on '
     '30 Nov 2026 17:00.\n'),
)
JINDAL_KEY = 'ARIBA-EVENT-RFQ_CHA_TPT_JSL_6500005154'


def test_all_three_ariba_mails_name_one_event():
    for _mid, subject, body in JINDAL:
        assert portal_rfq.event_key(ARIBA, subject, body) == JINDAL_KEY


def test_the_event_name_beats_the_document_id():
    """The document id identifies the MAIL. Only the invitation has
    one, so keying on it splits the set."""
    assert portal_rfq.event_key(
        ARIBA, 'Doc2472440490 - invitation',
        'event: RFQ_CHA_TPT_JSL_6500005154') == JINDAL_KEY
    # With no name anywhere it is still better than nothing.
    assert portal_rfq.event_key(
        ARIBA, 'Doc999888777 - invitation', 'A sourcing event.') == \
        'ARIBA-DOC999888777'


def test_an_english_sentence_is_not_an_event_name():
    """"The event starts on Monday" must not read as an event called
    "starts". The name has to carry a digit."""
    assert portal_rfq.event_key(
        ARIBA, 'Notice', 'The event starts on Monday.') == ''
    assert portal_rfq.event_key(
        ARIBA, 'Notice', 'The event closes soon.') == ''


def test_the_explicit_event_id_still_reads():
    assert portal_rfq.event_key(
        ARIBA, 'An event', 'Event ID: WS-7781') == 'ARIBA-EVENT-WS-7781'


def test_the_three_become_one_lead_through_the_real_intake():
    for mid, subject, body in JINDAL:
        SM.process_single_message(None, MAILBOX,
                                  _msg(mid, ARIBA, subject, body))
    made = _leads(JINDAL_KEY)
    assert len(made) == 1, [l.id for l in made]
    with flask_app.app_context():
        lead = db.session.get(Lead, made[0].id)
        # And named after the buyer, not after a fragment of the
        # subject line.
        assert lead.company == 'Jindal Stainless Limited'
        # The "about to end" notice carried the deadline.
        assert lead.quote_due_at is not None


# ── the buyer, which the parser was overruling ──────────────────────
def test_the_buyer_is_read_from_the_sentence_ariba_writes():
    buyer, how = portal_rfq.buyer_from(
        'Doc2472440490 - Jindal Stainless Limited has invited you to '
        'participate in an event', '')
    assert buyer == 'Jindal Stainless Limited', how


def test_a_document_id_in_front_of_the_name_is_not_the_name():
    """The bare hyphen was the whole bug: the run of words before "has
    invited you" began at the document id."""
    buyer, _how = portal_rfq.buyer_from(
        'Doc2472440490 - Jindal Stainless Limited has invited you', '')
    assert buyer == 'Jindal Stainless Limited'


def test_invited_to_a_number_by_a_company():
    buyer, _how = portal_rfq.buyer_from(
        'Project Invitation',
        'You have been invited to RFX-56172 by ArcelorMittal Nippon '
        'Steel India.')
    assert buyer == 'ArcelorMittal Nippon Steel India'


def test_a_sentence_fragment_is_still_refused_as_a_company():
    for fragment in ('participate in an event', 'the following event',
                     'quote'):
        assert portal_rfq.names_the_portal(fragment)


def test_the_portal_itself_is_still_never_the_buyer():
    buyer, _how = portal_rfq.buyer_from(
        'Procol has invited you to participate', '')
    assert buyer == ''


def test_the_bodys_buyer_beats_the_parsers_guess():
    source = open(os.path.join(_ROOT, 'email_ingest',
                               'single_message.py')).read()
    marker = source.index("if _portal['buyer']:")
    assert "lead_kwargs['company'] = _portal['company']" in \
        source[marker:marker + 700]


# ── three · the purge ───────────────────────────────────────────────
def _purge_the_message(imid):
    with flask_app.app_context():
        db.session.add(EmailEvent(
            internet_message_id=imid, mailbox=MAILBOX,
            status='lead_deleted', subject='purged',
            received_at=datetime.utcnow()))
        db.session.commit()


def test_force_alone_does_not_undo_a_purge():
    """And must not: a re-poll undoing somebody's deletion is the
    fault this rule exists for."""
    mid = '<purged-1@procol.in>'
    _purge_the_message(mid)
    out = SM.process_single_message(None, MAILBOX, _msg(
        mid, PROCOL, 'RFX 70001: invitation to quote',
        'Buyer organisation: ZP Purged Works'), force=True)
    assert out['status'] == 'skipped'
    assert 'purged' in out['reason']
    assert out['lead_id'] is None
    assert not _leads('RFX-70001')


def test_the_operator_can_say_not_these_and_the_lead_lands():
    mid = '<purged-2@procol.in>'
    _purge_the_message(mid)
    out = SM.process_single_message(None, MAILBOX, _msg(
        mid, PROCOL, 'RFX 70002: invitation to quote',
        'Buyer organisation: ZP Recovered Works'),
        force=True, override_purge=True)
    assert out['status'] not in ('skipped',), out
    made = _leads('RFX-70002')
    assert len(made) == 1
    with flask_app.app_context():
        lead = db.session.get(Lead, made[0].id)
        assert lead.company == 'ZP Recovered Works'


def test_overriding_a_purge_is_written_down():
    """Somebody deleted that message. Undoing it silently would be
    worse than refusing to undo it."""
    with flask_app.app_context():
        row = (AuditEvent.query
               .filter(AuditEvent.action == 'mail.purge_overridden',
                       AuditEvent.entity_id == '<purged-2@procol.in>')
               .first())
        assert row is not None
        assert 'purged as irrelevant' in (row.reason or '')


def test_the_override_is_off_by_default_everywhere():
    """The re-process button on the Mail ingest screen passes `force`
    and nothing else, and must keep meaning what it meant."""
    node = None
    tree = ast.parse(open(os.path.join(
        _ROOT, 'email_ingest', 'single_message.py')).read())
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef) and \
                fn.name == 'process_single_message':
            node = fn
    assert node is not None
    names = [a.arg for a in node.args.kwonlyargs]
    assert 'override_purge' in names
    default = node.args.kw_defaults[names.index('override_purge')]
    assert isinstance(default, ast.Constant) and default.value is False
    source = open(os.path.join(_ROOT, 'app.py')).read()
    assert 'override_purge' not in source


# ── one · the reason, printed ───────────────────────────────────────
def test_the_loop_prints_the_reason_it_used_to_discard():
    source = open(SCRIPT).read()
    marker = source.index('out = process_single_message(')
    tail = source[marker:marker + 1200]
    assert "(out or {}).get('reason')" in tail
    assert 'print(f\'      → {why' in tail


def test_the_apply_passes_the_override_through():
    source = open(SCRIPT).read()
    assert 'override_purge=include_purged' in source


class _Row:
    def __init__(self, rid, subject, outcome, reason, lead_id=None,
                 sender=PROCOL):
        self.id = rid
        self.subject = subject
        self.from_addr = sender
        self.outcome = outcome
        self.reason = reason
        self.lead_id = lead_id
        self.received_at = datetime(2026, 11, 10) + timedelta(days=rid)
        self.created_at = self.received_at
        self.internet_message_id = f'<w{rid}@portal>'


def _mixed():
    return [
        (_Row(1, 'RFX 80001: invitation', 'created', '', 991), 'procol.in'),
        (_Row(2, 'Reminder: RFX 80001', 'attached',
              'portal number RFX-80001', 991), 'procol.in'),
        (_Row(3, 'RFX 80002: invitation', 'skipped',
              'previously purged as irrelevant — the verdict is '
              'permanent'), 'procol.in'),
        (_Row(4, 'RFX 80003 has been withdrawn', 'skipped',
              'portal event RFX-80003 was withdrawn by the buyer and '
              'no lead exists for it'), 'procol.in'),
    ]


def test_the_why_report_names_the_reason_per_message(capsys):
    mod = _load(SCRIPT, 'zw_reproc')
    with flask_app.app_context():
        mod.report_outcomes(_mixed())
    out = capsys.readouterr().out
    assert 'previously purged as irrelevant' in out
    assert 'withdrawn by the buyer' in out
    # The ones with a lead are not in the no-lead list.
    assert '2 message(s) carry no lead' in out
    assert '--include-purged' in out


def test_the_why_report_groups_by_reason(capsys):
    mod = _load(SCRIPT, 'zw_reproc2')
    with flask_app.app_context():
        mod.report_outcomes(_mixed())
    out = capsys.readouterr().out
    head = out[:out.index('carry no lead')]
    assert 'created' in head and 'attached' in head
    assert head.count('skipped') >= 2


def test_the_selector_looks_past_blocked():
    """After a run the rows say what happened the SECOND time, so
    asking only for `blocked` hides every message the run touched —
    which is exactly the set somebody then wants to ask about."""
    mod = _load(SCRIPT, 'zw_reproc3')
    assert set(mod.SEEN) >= {'blocked', 'skipped', 'created', 'attached'}
    tree = ast.parse(open(SCRIPT).read())
    fn = [n for n in ast.walk(tree)
          if isinstance(n, ast.FunctionDef)
          and n.name == 'blocked_portal_rows'][0]
    assert 'outcomes' in [a.arg for a in fn.args.args]
    assert 'outcome.in_(outcomes)' in ast.unparse(fn)


def test_missing_keeps_only_the_ones_with_no_lead():
    source = open(SCRIPT).read()
    marker = source.index('if args.missing:')
    tail = source[marker:marker + 500]
    assert 'not row.lead_id' in tail
    assert "row.outcome != 'created'" in tail


# ── the keys on leads that already exist ────────────────────────────
def test_the_backfill_proposes_only_where_there_is_none():
    mod = _load(KEYS, 'zw_keys')
    with flask_app.app_context():
        db.create_all()
        held = Lead(company='ZK Held', stage='New Opportunity',
                    original_email_from=PROCOL,
                    original_email_subject='RFX 90011: invitation',
                    original_email_body='x', portal_key='RFX-90011')
        empty = Lead(company='ZK Empty', stage='New Opportunity',
                     original_email_from=PROCOL,
                     original_email_subject='RFX 90012: invitation',
                     original_email_body='x')
        other = Lead(company='ZK Other', stage='New Opportunity',
                     original_email_from=PROCOL,
                     original_email_subject='RFX 90013: invitation',
                     original_email_body='x', portal_key='RFX-OLD')
        twin = Lead(company='ZK Twin', stage='New Opportunity',
                    original_email_from=PROCOL,
                    original_email_subject='RFX 90011 again',
                    original_email_body='x')
        notportal = Lead(company='ZK Client', stage='New Opportunity',
                         original_email_from='buyer@client.example',
                         original_email_subject='RFQ-001',
                         original_email_body='x')
        db.session.add_all([held, empty, other, twin, notportal])
        db.session.commit()

        leads = mod.portal_leads()
        ids = {lead.id for lead, _p in leads}
        assert notportal.id not in ids, 'a customer is not a portal'
        clean, already, different, clashing = mod.proposals(leads)
        assert empty.id in {lead.id for lead, _p, _k in clean}
        assert held.id in {lead.id for lead, _p, _k in already}
        # A key already set is never overwritten.
        assert other.id in {lead.id for lead, _p, _h, _k in different}
        # And two leads cannot end up holding one event.
        assert twin.id in {lead.id for lead, _p, _k, _o in clashing}
        assert empty.id not in {lead.id for lead, _p, _k, _o in clashing}


def test_the_backfill_writes_undoes_and_audits():
    mod = _load(KEYS, 'zw_keys2')
    with flask_app.app_context():
        db.create_all()
        lead = Lead(company='ZK Undo', stage='New Opportunity',
                    original_email_from=PROCOL,
                    original_email_subject='RFX 90021: invitation',
                    original_email_body='x')
        db.session.add(lead)
        db.session.commit()
        lead_id = lead.id

        clean, _a, _d, _c = mod.proposals(mod.portal_leads())
        mine = [item for item in clean if item[0].id == lead_id]
        assert mine
        batch = mod.next_batch_id()
        mod.apply(mine, batch=batch, actor='PCM001')
        assert db.session.get(Lead, lead_id).portal_key == 'RFX-90021'

        row = (AuditEvent.query
               .filter(AuditEvent.action == 'lead.portal_key_set',
                       AuditEvent.entity_id == str(lead_id))
               .order_by(AuditEvent.id.desc()).first())
        assert row is not None
        assert (row.new_value or {}).get('batch') == batch
        assert row.actor == 'PCM001'

        mod.undo(batch)
        assert db.session.get(Lead, lead_id).portal_key is None


def test_the_undo_leaves_a_key_somebody_changed_since(capsys):
    mod = _load(KEYS, 'zw_keys3')
    with flask_app.app_context():
        db.create_all()
        lead = Lead(company='ZK Edited', stage='New Opportunity',
                    original_email_from=PROCOL,
                    original_email_subject='RFX 90031: invitation',
                    original_email_body='x')
        db.session.add(lead)
        db.session.commit()
        lead_id = lead.id
        clean, _a, _d, _c = mod.proposals(mod.portal_leads())
        mine = [item for item in clean if item[0].id == lead_id]
        batch = mod.next_batch_id()
        mod.apply(mine, batch=batch, actor='PCM001')

        changed = db.session.get(Lead, lead_id)
        changed.portal_key = 'RFX-SOMETHING-ELSE'
        db.session.commit()

        mod.undo(batch)
        assert db.session.get(Lead, lead_id).portal_key == \
            'RFX-SOMETHING-ELSE'
    assert 'left alone' in capsys.readouterr().out


def test_the_backfill_refuses_to_write_without_an_actor():
    source = open(KEYS).read()
    assert '--apply needs --actor' in source
    assert 'commit=False' in source      # one transaction, not many
