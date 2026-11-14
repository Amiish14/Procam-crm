"""The UAT of 14 November, on live.

Four findings, and the first is the one that matters: three leads were
filed under the account **"Procol"**. A procurement portal is not a
customer. The parser reads a company out of the sender's domain, and
on a portal's mail the sender is the portal, so the buyer of each RFQ
became the robot that delivered it and three different buyers' leads
pooled under one row.

The intake already prefers the buyer read out of the body (13 Nov).
What was missing is the other half: nothing stopped a lead being
LINKED to a portal's account, by the matcher or by hand. Now
`account_match.not_a_customer` refuses it, which is the one gate every
linking path goes through, and a script moves the leads already filed
that way.

The other three are small and each one costs somebody something:

  * the portal's tender number was readable only in the mail subjects
    on the trail, and it is the number the whole conversation with
    that buyer is conducted in;
  * text typed in the notes box and saved with **Save** was discarded
    without a word -- two buttons under one textarea, one of which
    threw it away;
  * Admin → Account link took 20-30 seconds to show its first page.
"""
import ast
import importlib
import os
import sys
import tempfile
import time
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.modules.setdefault('msal', types.ModuleType('msal'))

os.environ['URL_PREFIX'] = ''
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'uat01')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'Uat01-123456789')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'uat01.db')

_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Lead, Company, Employee = _main.Lead, _main.Company, _main.Employee

from app.models.audit import AuditEvent                      # noqa: E402
from app.services import (account_match, lead_account,        # noqa: E402
                          portal_rfq)

APP_HTML = os.path.join(_ROOT, 'templates', 'app.html')
SCRIPT = os.path.join(_ROOT, 'scripts', '2026_11_14_portal_accounts.py')
LINK_SCRIPT = os.path.join(_ROOT, 'scripts',
                           '2026_11_11_link_accounts.py')
PROCOL = 'no-reply@procol.in'

with flask_app.app_context():
    db.create_all()


def _load(path, name):
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _purge():
    """Everything these tests made, however it ended up named.

    Keyed on the SENDER as well as the company: a swept lead's company
    becomes the buyer's name, so purging by company alone left leads
    called "Amns" behind — and another suite counts open leads with no
    owner, which is how a leak here shows up as a failure three files
    away.
    """
    with flask_app.app_context():
        ids = [l.id for l in Lead.query.filter(
            Lead.original_email_from == PROCOL).all()]
        ids += [l.id for l in Lead.query.filter(
            Lead.company.like('ZV%')).all()]
        ids += [l.id for l in Lead.query.filter(
            Lead.company.in_(('Amns', 'ZU Cement Works'))).all()]
        # '' as well as the named ones: the stuck-lead test makes a
        # lead with an empty company, and a leftover open lead with
        # no owner is counted by other suites.
        ids += [l.id for l in Lead.query.filter(
            db.or_(Lead.company.in_(('Procol', 'To review', '')),
                   Lead.company.is_(None))).all()]
        if ids:
            AuditEvent.query.filter(
                AuditEvent.entity_type == 'lead',
                AuditEvent.entity_id.in_([str(i) for i in ids])).delete(
                    synchronize_session=False)
            # The children too. SQLite reuses row ids, so a lead
            # deleted without its mail trail hands that trail to the
            # next lead created — and the buyer then reads off a mail
            # belonging to a lead that no longer exists.
            from app import LeadEmail, LeadNote
            for model in (LeadEmail, LeadNote):
                model.query.filter(model.lead_id.in_(ids)).delete(
                    synchronize_session=False)
            Lead.query.filter(Lead.id.in_(ids)).delete(
                synchronize_session=False)
        Company.query.filter(Company.name.like('ZV%')).delete(
            synchronize_session=False)
        Company.query.filter(Company.name.in_((
            'Procol', 'Arcelormittal Nippon Steel India Ltd.',
            'Amns', 'Unknown'))).delete(synchronize_session=False)
        Company.query.filter(Company.name.like(
            '%ArcelorMittal%')).delete(synchronize_session=False)
        Company.query.filter(Company.name == 'ZU Cement Works').delete(
            synchronize_session=False)
        db.session.commit()


# ═══ item 1 · a portal is not a customer ═══════════════════════════
def test_a_portal_name_is_matched_whole_not_as_a_substring():
    """The distinction the whole guard rests on: "SAP Ariba" is a
    portal and "SAP India Pvt Ltd" is a customer, and a rule that
    cannot tell them apart would refuse a real account's leads."""
    for name in ('Procol', 'procol.in', 'PROCOL', 'Ariba', 'SAP Ariba',
                 'ansmtp', 'GEM'):
        assert portal_rfq.is_portal_name(name), name
    for name in ('SAP India Pvt Ltd', 'Procolink Steels',
                 'Gemini Logistics', 'Jindal Stainless Limited', '',
                 'Ariba Steel Traders Private Limited'):
        assert not portal_rfq.is_portal_name(name), name


def test_the_matcher_refuses_to_link_anything_to_a_portal():
    with flask_app.app_context():
        db.create_all()
        _purge()
        portal = Company(name='Procol', is_active=True)
        db.session.add(portal)
        db.session.commit()
        why = account_match.not_a_customer(portal)
        assert 'procurement portal' in why
        assert 'the buyer is named in the body' in why
        _purge()


def test_an_account_claiming_a_portals_domain_is_refused_too():
    """A row that collects every buyer that portal sends for."""
    with flask_app.app_context():
        db.create_all()
        _purge()
        account = Company(name='ZV Looks Real', is_active=True,
                          email_domains=['procol.in'])
        db.session.add(account)
        db.session.commit()
        why = account_match.not_a_customer(account)
        assert 'procurement portal' in why
        _purge()


def test_the_link_service_refuses_it_which_is_the_one_gate():
    """Every path that links a lead to an account goes through
    `lead_account.link` — the lead sheet, the picker, the intake, the
    account-link screen and the sweeps."""
    with flask_app.app_context():
        db.create_all()
        _purge()
        portal = Company(name='Procol', is_active=True)
        lead = Lead(company='ZV Buyer', stage='New Opportunity')
        db.session.add_all([portal, lead])
        db.session.commit()
        ok, why = lead_account.link(lead, portal, actor='PCM001')
        assert ok is False
        assert 'procurement portal' in why
        assert lead.company_id is None
        _purge()


def _three_leads_on_procol():
    portal = Company(name='Procol', is_active=True)
    buyer = Company(name='Arcelormittal Nippon Steel India Ltd.',
                    is_active=True)
    db.session.add_all([portal, buyer])
    db.session.commit()
    made = []
    for key, subject, body in (
            ('RFX-56172', 'Project Invitation from ArcelorMittal',
             'You have been invited to RFX-56172 by ArcelorMittal '
             'Nippon Steel India.'),
            ('RFQ-112165', 'RFQ-112165 invitation',
             'Buyer organisation: ZV Cement Works'),
            (None, 'Invitation to quote', 'Log in to the portal.')):
        lead = Lead(company='Procol', company_id=portal.id,
                    portal_key=key, stage='New Opportunity',
                    procam_vertical='Heavy Cargo',
                    original_email_from=PROCOL,
                    original_email_subject=subject,
                    original_email_body=body)
        db.session.add(lead)
        made.append(lead)
    # And one real customer, which must not be touched.
    db.session.add(Lead(company='ZV Real Customer', company_id=buyer.id,
                        stage='New Opportunity',
                        original_email_from='buyer@zvreal.example',
                        original_email_subject='An enquiry'))
    db.session.commit()
    return portal, buyer, made


def test_the_sweep_sorts_the_three_live_cases_three_ways():
    mod = _load(SCRIPT, 'zv_sweep')
    with flask_app.app_context():
        db.create_all()
        _purge()
        portal, buyer, made = _three_leads_on_procol()
        found, portal_accounts = mod.leads_on_a_portal()
        assert portal.id in portal_accounts
        ids = {lead.id for lead, _w in found}
        assert ids == {l.id for l in made}, 'the real customer was swept'

        planned = {row['lead'].id: row for row in mod.plan(found)}
        # the buyer already has an account, under a different spelling
        assert planned[made[0].id]['verdict'] == 'link'
        assert planned[made[0].id]['account'].id == buyer.id
        # a buyer nobody has on file
        assert planned[made[1].id]['verdict'] == 'create'
        assert planned[made[1].id]['buyer'] == 'ZV Cement Works'
        # and no buyer anywhere on the trail. Young and untouched, so
        # the review queue rather than the archive.
        assert planned[made[2].id]['verdict'] == 'to review'
        _purge()


def test_the_sweep_moves_them_and_can_be_undone():
    mod = _load(SCRIPT, 'zv_sweep2')
    with flask_app.app_context():
        db.create_all()
        _purge()
        portal, buyer, made = _three_leads_on_procol()
        ids = [l.id for l in made]
        found, _accounts = mod.leads_on_a_portal()
        batch = mod.next_batch_id()
        mod.apply(mod.plan(found), batch=batch, actor='PCM001')

        first = db.session.get(Lead, ids[0])
        assert first.company_id != portal.id
        assert first.company.lower().startswith('arcelormittal')
        second = db.session.get(Lead, ids[1])
        assert second.company == 'ZV Cement Works'
        assert second.company_id and second.company_id != portal.id
        third = db.session.get(Lead, ids[2])
        assert third.company == 'To review'
        assert third.company_id is None
        # Nothing on the portal row any more, and the row still exists.
        assert Lead.query.filter(Lead.company_id == portal.id).count() == 0
        assert db.session.get(Company, portal.id) is not None

        mod.undo(batch)
        for lead_id in ids:
            lead = db.session.get(Lead, lead_id)
            assert lead.company_id == portal.id, lead_id
            assert lead.company == 'Procol'
        _purge()


def test_two_leads_naming_one_buyer_get_one_account():
    """Answering "a row that is not a company" with "two rows for one
    company" would be a poor trade."""
    mod = _load(SCRIPT, 'zv_sweep3')
    with flask_app.app_context():
        db.create_all()
        _purge()
        portal = Company(name='Procol', is_active=True)
        db.session.add(portal)
        db.session.commit()
        for n in (1, 2):
            db.session.add(Lead(
                company='Procol', company_id=portal.id,
                stage='New Opportunity', original_email_from=PROCOL,
                original_email_subject=f'RFX 5000{n} invitation',
                original_email_body='Buyer organisation: ZV Twin Works'))
        db.session.commit()
        found, _a = mod.leads_on_a_portal()
        mod.apply(mod.plan(found), batch=mod.next_batch_id(),
                  actor='PCM001')
        rows = Company.query.filter(Company.name == 'ZV Twin Works').all()
        assert len(rows) == 1, [r.id for r in rows]
        assert Lead.query.filter(
            Lead.company_id == rows[0].id).count() == 2
        _purge()


def test_a_created_account_never_carries_the_portals_domain():
    """It would hand this account the next buyer the portal sends
    for. The same reason the matcher is not taught the sender."""
    source = open(SCRIPT).read()
    marker = source.index('def _create_account')
    body = source[marker:source.index('def apply(', marker)]
    assert 'email_domains' not in body
    assert 'learn=False' in source


def test_the_undo_deactivates_rather_than_deletes():
    source = open(SCRIPT).read()
    assert 'is_active = False' in source
    assert 'delete(' not in source


# ═══ item 2 · the tender number on the sheet ════════════════════════
def test_the_lead_sheet_renders_the_portal_number():
    html = open(APP_HTML).read()
    assert 'function ldPortalRow(l)' in html
    assert '${ldPortalRow(l)}' in html
    marker = html.index('function ldPortalRow(l)')
    body = html[marker:marker + 1400]
    assert 'portal_key' in body
    assert 'Tender / RFX number' in body
    # Read-only: it is the portal's number, and a box somebody can
    # retype is a box in which one auction becomes two.
    assert 'disabled' in body


def test_the_api_sends_the_portal_number():
    with flask_app.app_context():
        db.create_all()
        _purge()
        lead = Lead(company='ZV Keyed', stage='New Opportunity',
                    portal_key='RFX-56172')
        db.session.add(lead)
        db.session.commit()
        assert lead.to_dict()['portal_key'] == 'RFX-56172'
        _purge()


# ═══ item 3 · nothing typed is lost ════════════════════════════════
def test_save_posts_the_staged_note_before_saving_the_lead():
    html = open(APP_HTML).read()
    marker = html.index('async function saveLd()')
    body = html[marker:marker + 4000]
    staged = body.index("const staged")
    put = body.index("api(`/api/leads/${activeId}`,{method:'PUT'")
    assert staged < put, 'the note must be posted before the save'
    assert "'/api/leads/'+activeId+'/notes'" in body


def test_a_note_that_cannot_be_saved_stops_the_save_and_keeps_the_text():
    """The one outcome worse than a lost note is a lost note and a
    success message."""
    html = open(APP_HTML).read()
    marker = html.index('const staged')
    body = html[marker:marker + 900]
    assert 'still in the box' in body
    # and it returns rather than carrying on to the PUT
    assert body.index('return;') < body.index('api(`/api/leads/')


def test_the_toast_says_what_is_still_open():
    html = open(APP_HTML).read()
    assert '— the customer is still to be named' in html
    marker = html.index("toast('Lead saved ✓'")
    assert 'placeholder_company' in html[marker:marker + 300]


def test_the_server_says_whether_the_customer_is_still_a_placeholder():
    """`unnamed_prompt` answers nothing at all when the account-first
    feature is off, which is how "Lead saved ✓" came to be the whole
    message on a lead called "To review"."""
    source = open(os.path.join(_ROOT, 'app.py')).read()
    assert "_body['placeholder_company']" in source
    marker = source.index("_body['placeholder_company']")
    head = source[marker - 500:marker]
    assert 'is_placeholder' in head


def test_the_placeholder_flag_rides_on_a_real_save():
    with flask_app.app_context():
        db.create_all()
        _purge()
        Employee.query.filter_by(emp_code='ZV_ADM').delete()
        db.session.add(Employee(
            emp_code='ZV_ADM', name='Admin', password_hash='x',
            role='admin', email='zv.adm@procamlogistics.com',
            is_active=True))
        lead = Lead(company='To review', stage='New Opportunity')
        db.session.add(lead)
        db.session.commit()
        lead_id = lead.id

    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess['emp_code'] = 'ZV_ADM'
        sess['role'] = 'admin'
    token = ''
    for header in client.get('/my-work').headers.getlist('Set-Cookie'):
        if header.startswith('csrf_token='):
            token = header.split('=', 1)[1].split(';')[0]
    r = client.put(f'/api/leads/{lead_id}',
                   json={'pic': 'Somebody'},
                   headers={'X-CSRFToken': token})
    assert r.status_code == 200, r.status_code
    assert r.get_json().get('placeholder_company')

    with flask_app.app_context():
        Employee.query.filter_by(emp_code='ZV_ADM').delete()
        db.session.commit()
        _purge()


# ═══ item 4 · the screen that took half a minute ═══════════════════
def test_the_snapshot_reads_the_master_once_and_then_forgets_it():
    with flask_app.app_context():
        db.create_all()
        _purge()
        db.session.add(Company(name='ZV One', is_active=True))
        db.session.commit()

        assert account_match._held() is False
        with account_match.snapshot():
            assert account_match._held() is True
            first = account_match.all_accounts()
            assert account_match.all_accounts() is first
            index = account_match.active_index()
            assert account_match.active_index() is index
            with account_match.snapshot():       # nests
                assert account_match.all_accounts() is first
            assert account_match.all_accounts() is first
        # Cleared: nothing is cached between pages.
        assert account_match._held() is False
        assert account_match.all_accounts() is not first
        _purge()


def test_the_snapshot_changes_no_answer():
    """The whole requirement: faster, and the same proposals."""
    with flask_app.app_context():
        db.create_all()
        _purge()
        db.session.add_all([
            Company(name='ZV Alpha Steel Works', is_active=True,
                    email_domains=['zvalpha.example'],
                    website='www.zvalpha.example'),
            Company(name='ZV Beta Cement', is_active=True),
            Company(name='ZV Alpha Steel Workz', is_active=True),
        ])
        db.session.commit()

        asks = (
            lambda: account_match.not_a_customer(
                Company.query.filter_by(name='ZV Beta Cement').first()),
            lambda: (account_match.auto_link(
                'someone@zvalpha.example') or (None, ''))[0],
            lambda: account_match.auto_link_by_name(
                '', company_name='ZV Alpha Steel Works')[0],
            lambda: [(a.name, round(r, 3)) for a, r
                     in account_match._similar_names(
                         'ZV Alpha Steel Works')],
        )
        outside = [str(ask()) for ask in asks]
        with account_match.snapshot():
            inside = [str(ask()) for ask in asks]
        assert outside == inside
        _purge()


def test_the_resemblance_shortcut_keeps_the_same_scores():
    """`real_quick_ratio`/`quick_ratio` are upper bounds, so a
    candidate they drop could not have reached the floor."""
    from difflib import SequenceMatcher

    with flask_app.app_context():
        db.create_all()
        _purge()
        db.session.add_all([
            Company(name='ZV Hindustan Cables Limited', is_active=True),
            Company(name='ZV Hindusthan Cable Ltd', is_active=True),
            Company(name='ZV Completely Different Movers',
                    is_active=True),
        ])
        db.session.commit()
        want = 'ZV Hindustan Cables Limited'
        got = dict((a.name, round(r, 6)) for a, r
                   in account_match._similar_names(want))
        raw = account_match.strict_norm(want)
        for account in Company.query.filter(
                Company.name.like('ZV %')).all():
            theirs = account_match.strict_norm(account.name)
            full = SequenceMatcher(None, raw, theirs).ratio()
            if full >= 0.62 and not account_match._is_short(raw) \
                    and not account_match._is_short(theirs):
                assert account.name in got, account.name
                assert got[account.name] == round(full, 6)
        _purge()


def test_the_screen_takes_a_snapshot_and_asks_for_one_page():
    source = open(os.path.join(_ROOT, 'app', 'account_link',
                               'routes.py')).read()
    assert 'with account_match.snapshot():' in source
    assert 'script.stuck_leads(limit=' in source


def test_the_stuck_lead_query_cannot_hide_a_stuck_lead():
    """It narrows in SQL to the rows that COULD be stuck. A superset
    of what the gate refuses, or the screen stops showing work."""
    mod = _load(LINK_SCRIPT, 'zv_link')
    with flask_app.app_context():
        db.create_all()
        _purge()
        unknown = Company(name='Unknown', is_active=True)
        portal = Company(name='Procol', is_active=True)
        real = Company(name='ZV Named Account', is_active=True)
        db.session.add_all([unknown, portal, real])
        db.session.commit()
        # Four shapes of stuck, and one that is not stuck at all.
        a = Lead(company='To review', stage='New Opportunity')
        b = Lead(company='', stage='New Opportunity')
        # The company TEXT reads fine on these two: only the ACCOUNT
        # says otherwise, which a text clause cannot see.
        c = Lead(company='ZV Looks Fine', company_id=unknown.id,
                 stage='New Opportunity')
        d = Lead(company='ZV Also Fine', company_id=portal.id,
                 stage='New Opportunity')
        ok = Lead(company='ZV Named Account', company_id=real.id,
                  stage='New Opportunity')
        db.session.add_all([a, b, c, d, ok])
        db.session.commit()

        found = {lead.id for lead, _c, _w in mod.stuck_leads()}
        for lead in (a, b, c, d):
            assert lead.id in found, lead.company
        assert ok.id not in found

        # And the limit stops the walk without changing what it finds.
        assert len(mod.stuck_leads(limit=2)) == 2
        _purge()


def test_a_page_of_proposals_is_quick_on_a_book_of_this_size():
    """A number rather than an adjective. 1,200 accounts here; live
    has more, and the shape of the cost is what changed."""
    from flask import session

    with flask_app.app_context():
        db.create_all()
        _purge()
        db.session.add_all([
            Company(name=f'ZV Co {i} Industries Pvt Ltd', is_active=True,
                    email_domains=[f'zvco{i}.example'],
                    website=f'www.zvco{i}.example')
            for i in range(400)])
        db.session.add_all([
            Lead(company='To review', stage='New Opportunity',
                 procam_vertical='Heavy Cargo',
                 original_email_from=f'buyer{i}@zvunknown{i}.example',
                 original_email_subject=f'ZV enquiry {i}',
                 original_email_body='40 MT, Chennai to Hosur.')
            for i in range(80)])
        db.session.commit()

    with flask_app.test_request_context('/'):
        session['emp_code'] = 'PCM001'
        session['role'] = 'admin'
        from app.account_link import routes as al
        al.proposals(limit=5)                 # import, warm
        started = time.time()
        rows = al.proposals(limit=60)
        took = time.time() - started
    assert rows
    assert took < 4.0, f'a page of proposals took {took:.1f}s'
    _purge()


# ═══ the second review of the dry run, 14 Nov ══════════════════════
#
# "Don't apply as is." Four things the dry run showed:
#
#  1. the buyer was read from the lead's FIRST mail only, and on five
#     of these leads the mail that names it is further down the trail;
#  2. ArcelorMittal Nippon Steel India is the existing account "Amns"
#     (#652), and a fifth account for that customer is what the sweep
#     would have created;
#  3. "to review" was collecting two different things -- leads
#     somebody is working on, and portal notices nobody can ever name
#     a buyer for. The second are noise in a queue, and a queue with
#     noise in it stops being read;
#  4. the link and create rows were agreed; the split was not. They
#     should be appliable on their own.
def _sweep():
    return _load(SCRIPT, 'zv_sweep_' + str(time.time_ns()))


def _portal_lead(subject, *, key=None, days=5, body='', stage=None,
                 company_id=None):
    from datetime import datetime, timedelta

    when = datetime.utcnow() - timedelta(days=days)
    lead = Lead(company='Procol', company_id=company_id,
                portal_key=key, stage=stage or 'New Opportunity',
                procam_vertical='Heavy Transport',
                original_email_from=PROCOL,
                original_email_subject=subject,
                original_email_body=body,
                created_at=when, original_email_received_at=when)
    db.session.add(lead)
    db.session.commit()
    return lead


def _trail(lead, subject, body, *, direction='inbound'):
    from datetime import datetime

    from app import LeadEmail

    db.session.add(LeadEmail(
        lead_id=lead.id, direction=direction, from_addr=PROCOL,
        subject=subject, body=body, source='ingested',
        sent_or_received_at=datetime.utcnow()))
    db.session.commit()


# ── 1 · the whole trail ────────────────────────────────────────────
def test_the_buyer_is_read_from_anywhere_on_the_trail():
    """#12644 and four others: the lead was made by a bare "Technical
    is now live" notice and the invitation that names the buyer
    arrived after it."""
    mod = _sweep()
    with flask_app.app_context():
        db.create_all()
        _purge()
        lead = _portal_lead('RFX-56172 | Technical is now live',
                            key='RFX-56172',
                            body='Log in to the portal to respond.')
        assert mod.buyer_of(lead) == ('', '')
        _trail(lead, 'Project Invitation from ArcelorMittal Nippon '
                     'Steel India Limited',
               'You have been invited to RFX-56172 by ArcelorMittal '
               'Nippon Steel India Limited.')
        buyer, how = mod.buyer_of(lead)
        assert buyer == 'ArcelorMittal Nippon Steel India Limited'
        # And it says WHICH mail answered, because a buyer read off a
        # mail four down the trail is a thing somebody may want to
        # check.
        assert 'inbound' in how
        _purge()


def test_the_trail_is_read_oldest_first():
    mod = _sweep()
    with flask_app.app_context():
        db.create_all()
        _purge()
        lead = _portal_lead('RFX-56173 notice', key='RFX-56173')
        assert mod.trail_of(lead)[0][0] == "the lead's own mail"
        _trail(lead, 'Buyer organisation: ZV First Works', 'x')
        _trail(lead, 'Buyer organisation: ZV Second Works', 'x')
        buyer, _how = mod.buyer_of(lead)
        assert buyer == 'ZV First Works'
        _purge()


# ── 2 · one account for one customer ───────────────────────────────
def test_amns_is_arcelormittal_nippon_steel():
    """The whole of item 2: "Amns" (#652) is that customer, and the
    sweep must find it rather than create a fifth row."""
    mod = _sweep()
    with flask_app.app_context():
        db.create_all()
        _purge()
        amns = Company(name='Amns', is_active=True)
        db.session.add(amns)
        db.session.commit()
        for spelling in ('ArcelorMittal Nippon Steel India Limited',
                         'ArcelorMittal Nippon Steel India',
                         'ArcelorMittal Nippon Steel'):
            account, note = mod.account_for(spelling)
            assert account is not None, spelling
            assert account.id == amns.id, spelling
            assert 'acronym' in note, spelling
        _purge()


def test_the_acronym_rule_needs_more_than_two_letters():
    """"TS" for Tata Steel is a coincidence waiting to happen."""
    mod = _sweep()
    assert 'AMNS' in mod.acronyms_of('ArcelorMittal Nippon Steel India')
    assert 'JSL' in mod.acronyms_of('Jindal Stainless Limited')
    assert not any(len(a) < 3 for a in mod.acronyms_of('Tata Steel'))


def test_a_longer_name_matches_the_account_it_extends():
    mod = _sweep()
    with flask_app.app_context():
        db.create_all()
        _purge()
        held = Company(name='ZV Hindustan Cables', is_active=True)
        db.session.add(held)
        db.session.commit()
        # "Limited" alone normalises away, so use a name that really
        # is the account's plus more of it.
        account, note = mod.account_for(
            'ZV Hindustan Cables Projects Division')
        assert account is not None and account.id == held.id
        assert 'without the rest of it' in note
        _purge()


def test_a_short_name_is_never_matched_by_containment():
    """On four characters there is no reading that separates BHEL
    from BEML — `account_match` refuses to resemble a short name at
    all, and so does this."""
    mod = _sweep()
    with flask_app.app_context():
        db.create_all()
        _purge()
        db.session.add(Company(name='ZV Co', is_active=True))
        db.session.commit()
        account, _note = mod.account_for('ZV Completely Other Works')
        assert account is None
        _purge()


def test_a_matched_account_is_given_the_buyers_spelling():
    """So the NEXT RFQ matches on the name, and a person reading the
    account can see what its mail arrives under. The account keeps
    the name somebody gave it."""
    mod = _sweep()
    with flask_app.app_context():
        db.create_all()
        _purge()
        amns = Company(name='Amns', is_active=True)
        db.session.add(amns)
        db.session.commit()
        lead = _portal_lead('RFX-56174 | Technical is now live',
                            key='RFX-56174')
        _trail(lead, 'Project Invitation from ArcelorMittal',
               'invited to RFX-56174 by ArcelorMittal Nippon Steel '
               'India Limited.')
        found, _accounts = mod.leads_on_a_portal()
        planned = mod.plan(found)
        batch = mod.next_batch_id()
        mod.apply(planned, batch=batch, actor='PCM001')

        again = db.session.get(Company, amns.id)
        assert again.name == 'Amns'
        assert 'ArcelorMittal Nippon Steel India Limited' in \
            (again.aliases or [])
        assert db.session.get(Lead, lead.id).company_id == amns.id
        # And the alias now does the matching on its own.
        account, note = mod.account_for(
            'ArcelorMittal Nippon Steel India Limited')
        assert account.id == amns.id
        assert note == ''

        mod.undo(batch)
        assert (db.session.get(Company, amns.id).aliases or []) == []
        _purge()


def test_no_second_account_for_a_customer_that_has_one():
    mod = _sweep()
    with flask_app.app_context():
        db.create_all()
        _purge()
        db.session.add(Company(name='Amns', is_active=True))
        db.session.commit()
        for n, key in enumerate(('RFX-1001', 'RFX-1002', 'RFX-1003')):
            lead = _portal_lead(f'{key} | Technical is now live',
                                key=key)
            _trail(lead, 'Project Invitation',
                   f'invited to {key} by ArcelorMittal Nippon Steel '
                   f'India Limited.')
        found, _a = mod.leads_on_a_portal()
        mod.apply(mod.plan(found), batch=mod.next_batch_id(),
                  actor='PCM001')
        assert Company.query.filter(
            Company.name.like('%ArcelorMittal%')).count() == 0
        assert Company.query.filter(Company.name == 'Amns').count() == 1
        _purge()


# ── 3 · the review bucket, split ───────────────────────────────────
def test_a_lead_somebody_is_working_on_is_never_archived():
    mod = _sweep()
    with flask_app.app_context():
        db.create_all()
        _purge()
        from app import LeadNote

        worked = _portal_lead('Invitation to quote', days=90,
                              body='Log in.', stage='RFQ Generated')
        db.session.add(LeadNote(lead_id=worked.id,
                                note_text='Called the desk',
                                note_type='call'))
        db.session.commit()
        done = mod.work_on(worked)
        assert 'a note' in done
        assert any(d.startswith('stage') for d in done)

        row = [r for r in mod.plan(mod.leads_on_a_portal()[0])
               if r['lead'].id == worked.id][0]
        assert row['verdict'] == 'to review'
        assert 'worked on' in row['why']
        _purge()


def test_each_kind_of_work_counts():
    """Quote, RFQ, call, note, outgoing mail, stage — any one of them
    means a person has handled this enquiry."""
    mod = _sweep()
    from datetime import date

    with flask_app.app_context():
        db.create_all()
        _purge()
        for field, value, word in (
                ('quote_date', date(2026, 11, 1), 'quoted'),
                ('rfq_date', date(2026, 11, 1), 'RFQ'),
                ('phone_call_date', date(2026, 11, 1), 'called'),
                ('meeting_date', date(2026, 11, 1), 'met')):
            lead = _portal_lead(f'ZV {word} notice', days=90)
            setattr(lead, field, value)
            db.session.commit()
            assert word in mod.work_on(lead), word

        sent = _portal_lead('ZV outgoing notice', days=90)
        _trail(sent, 'Our quotation', 'Please find attached.',
               direction='outbound')
        assert 'mail sent' in mod.work_on(sent)
        _purge()


def test_an_untouched_notice_older_than_thirty_days_is_archived():
    mod = _sweep()
    with flask_app.app_context():
        db.create_all()
        _purge()
        old = _portal_lead('RFX-40011 | Event withdrawn', days=55,
                           key='RFX-40011', body='Withdrawn.')
        row = [r for r in mod.plan(mod.leads_on_a_portal()[0])
               if r['lead'].id == old.id][0]
        assert row['verdict'] == 'archive'
        assert mod.ARCHIVE_AFTER_DAYS == 30
        assert mod.ARCHIVE_REASON == 'portal notice — no buyer'
        _purge()


def test_a_young_untouched_notice_waits_in_the_review_queue():
    """The invitation that names the buyer may still arrive —
    `trail_of` reads the whole trail, so only time can decide that
    nothing more is coming."""
    mod = _sweep()
    with flask_app.app_context():
        db.create_all()
        _purge()
        young = _portal_lead('RFX-57001 | Technical is now live',
                             days=4, key='RFX-57001', body='No buyer.')
        row = [r for r in mod.plan(mod.leads_on_a_portal()[0])
               if r['lead'].id == young.id][0]
        assert row['verdict'] == 'to review'
        assert 'still arrive' in row['why']
        _purge()


def test_the_archive_goes_through_the_service_and_can_be_undone():
    """Not a column the sweep writes itself: `lead_archive` asks
    whether this actor may archive this lead, and that permission is
    why the column can be trusted."""
    mod = _sweep()
    with flask_app.app_context():
        db.create_all()
        _purge()
        old = _portal_lead('RFX-40012 | Technical is now live',
                           days=71, key='RFX-40012', body='No buyer.')
        lead_id = old.id
        batch = mod.next_batch_id()
        mod.apply(mod.plan(mod.leads_on_a_portal()[0]), batch=batch,
                  actor='PCM001')
        again = db.session.get(Lead, lead_id)
        assert again.is_archived is True
        assert again.archive_reason == 'portal notice — no buyer'

        mod.undo(batch)
        back = db.session.get(Lead, lead_id)
        assert not back.is_archived
        assert back.company == 'Procol'
        _purge()
    source = open(SCRIPT).read()
    assert 'lead_archive.archive(' in source
    assert 'lead_archive.restore(' in source


def test_the_counts_are_printed_per_bucket_and_per_portal(capsys):
    mod = _sweep()
    with flask_app.app_context():
        db.create_all()
        _purge()
        db.session.add(Company(name='Amns', is_active=True))
        db.session.commit()
        named = _portal_lead('RFX-2001 | live', key='RFX-2001')
        _trail(named, 'Project Invitation',
               'invited to RFX-2001 by ArcelorMittal Nippon Steel '
               'India Limited.')
        _portal_lead('RFX-2002 | live', key='RFX-2002', days=90,
                     body='No buyer.')
        found, accounts = mod.leads_on_a_portal()
        mod.report(mod.plan(found), accounts)
        out = capsys.readouterr().out
        _purge()
    assert '── by bucket ──' in out
    assert '── by bucket and portal ──' in out
    assert 'procol.in' in out
    assert 'link' in out and 'archive' in out


# ── 4 · the link rows on their own ─────────────────────────────────
def test_only_link_writes_the_links_and_leaves_the_rest():
    mod = _sweep()
    with flask_app.app_context():
        db.create_all()
        _purge()
        db.session.add(Company(name='Amns', is_active=True))
        db.session.commit()
        named = _portal_lead('RFX-3001 | live', key='RFX-3001')
        _trail(named, 'Project Invitation',
               'invited to RFX-3001 by ArcelorMittal Nippon Steel '
               'India Limited.')
        old = _portal_lead('RFX-3002 | live', key='RFX-3002', days=90,
                           body='No buyer.')
        named_id, old_id = named.id, old.id

        found, _a = mod.leads_on_a_portal()
        done = mod.apply(mod.plan(found), batch=mod.next_batch_id(),
                         actor='PCM001', only_link=True)
        assert done['linked'] == 1
        assert done['left for the second pass'] == 1
        assert db.session.get(Lead, named_id).company_id is not None
        left = db.session.get(Lead, old_id)
        assert left.company == 'Procol'
        assert not left.is_archived

        # The second pass finds exactly what the first left.
        found, _a = mod.leads_on_a_portal()
        rest = mod.plan(found)
        assert [r['lead'].id for r in rest] == [old_id]
        _purge()


def test_the_dry_run_offers_only_link():
    source = open(SCRIPT).read()
    assert "--only-link" in source
    assert 'only_link' in source
    assert 'does the ' in source      # the dry run's closing line


def test_an_alias_can_be_stated_outright():
    """"These two names are one customer" is a statement about the
    master that outlives this backlog, and it beats every reading the
    sweep makes on its own."""
    mod = _sweep()
    with flask_app.app_context():
        db.create_all()
        _purge()
        amns = Company(name='Amns', is_active=True)
        db.session.add(amns)
        db.session.commit()
        assert mod.add_aliases([f'{amns.id}=ArcelorMittal Nippon Steel'],
                               actor='PCM001')
        again = db.session.get(Company, amns.id)
        assert 'ArcelorMittal Nippon Steel' in (again.aliases or [])
        assert again.name == 'Amns'
        # Now matched on the name rather than on an acronym.
        account, note = mod.account_for('ArcelorMittal Nippon Steel')
        assert account.id == amns.id and note == ''
        # Said again, it changes nothing.
        mod.add_aliases([f'{amns.id}=ArcelorMittal Nippon Steel'],
                        actor='PCM001')
        assert len(db.session.get(Company, amns.id).aliases) == 1
        _purge()


def test_a_bad_alias_pair_is_refused_rather_than_guessed_at():
    mod = _sweep()
    with flask_app.app_context():
        db.create_all()
        assert mod.add_aliases(['no-equals-sign'], actor='PCM001') == 0
        assert mod.add_aliases(['abc=Name'], actor='PCM001') == 0
        assert mod.add_aliases(['99999=Name'], actor='PCM001') == 0


def test_the_alias_step_needs_an_actor_and_an_apply():
    source = open(SCRIPT).read()
    assert '--add-alias needs --actor' in source
    marker = source.index("if args.add_alias:")
    assert 'DRY RUN' in source[marker:marker + 900]
