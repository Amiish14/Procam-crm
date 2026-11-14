"""Today's Chrome clean-up: three things found by using the CRM.

ONE · THE MATCHER WAS TAUGHT A ROBOT'S ADDRESS

Linking the ArcelorMittal leads to "Amns" from the lead sheet taught
it two rules:

    email   no-reply@procol.in  → Amns
    domain  procol.in           → Amns

Those are the two strongest rules the matcher has. Every later RFQ
that portal delivered -- for ANY buyer -- would have gone to Amns,
silently, with "learned domain" as the reason on the lead.
`not_a_customer` refuses to LINK a lead to a portal's account; nothing
refused to LEARN a portal's address as a customer's.

TWO · A DIALOG NOBODY ANSWERED MOVED THE LEAD

"Lead PIC is X, account owner is Y" was a browser `confirm()`: OK
kept, Cancel switched. Escape, a click outside, a dismissed dialog and
a mis-click all return false, and false meant SWITCH -- so the owner
of a lead, the one field that decides whose work it is, changed
because a window was closed.

THREE · THE PAIRS NOBODY COULD REACH

The duplicates screen showed the first hundred pairs and called that
the count. The same-tender-number pairs -- #11971/#11970,
#12124/#12123, #12247/#12212 -- were ranked past 700, behind hundreds
of GSTIN pairs.
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
os.environ.setdefault('SECRET_KEY', 'uat02')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'Uat02-123456789')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'uat02.db')

_main = importlib.import_module('app')
importlib.import_module('app.models')
importlib.import_module('app.models.assignment')
flask_app, db = _main.app, _main.db
Lead, Company = _main.Lead, _main.Company

from app.models.assignment import (BY_DOMAIN, BY_EMAIL,              # noqa: E402
                                   SenderAccountLink)
from app.models.audit import AuditEvent                              # noqa: E402
from app.services import account_match, portal_rfq                    # noqa: E402

APP_HTML = os.path.join(_ROOT, 'templates', 'app.html')
DUPES_HTML = os.path.join(_ROOT, 'templates', 'admin',
                          'lead_duplicates.html')
SCRIPT = os.path.join(_ROOT, 'scripts',
                      '2026_11_15_portal_learned_links.py')
PROCOL = 'no-reply@procol.in'

with flask_app.app_context():
    db.create_all()


def _load(path, name):
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _script():
    return _load(SCRIPT, 'zu_learn_' + str(time.time_ns()))


def _purge():
    with flask_app.app_context():
        ids = [l.id for l in Lead.query.filter(
            db.or_(Lead.company.like('ZU%'),
                   Lead.original_email_from == PROCOL)).all()]
        if ids:
            from app import LeadEmail, LeadNote
            for model in (LeadEmail, LeadNote):
                model.query.filter(model.lead_id.in_(ids)).delete(
                    synchronize_session=False)
            AuditEvent.query.filter(
                AuditEvent.entity_type == 'lead',
                AuditEvent.entity_id.in_(
                    [str(i) for i in ids])).delete(
                        synchronize_session=False)
            Lead.query.filter(Lead.id.in_(ids)).delete(
                synchronize_session=False)
        SenderAccountLink.query.delete(synchronize_session=False)
        Company.query.filter(db.or_(
            Company.name.like('ZU%'),
            Company.name == 'Amns')).delete(synchronize_session=False)
        db.session.commit()


# ═══ item 1 · a portal is never learned ════════════════════════════
def test_the_never_learn_rule_is_wider_than_the_intake_list():
    """`is_portal` decides how live mail is TREATED, so adding a
    domain there is a decision somebody makes. This one decides only
    whether a sender may be learned, where being wrong costs one
    unlearned sender and being wrong the other way sends another
    buyer's RFQ to the wrong account."""
    assert portal_rfq.is_portal('x@superprocure.in') == ''
    for address in ('no-reply@procol.in', 'x@ansmtp.ariba.com',
                    'x@eu.ansmtp.ariba.com', 'noreply@superprocure.in',
                    'x@procurengine.com', 'x@coupa.com'):
        assert portal_rfq.is_portal_sender(address), address
    for address in ('buyer@realclient.example', 'x@notprocol.in',
                    'x@gembuyer.example', '', 'nonsense'):
        assert not portal_rfq.is_portal_sender(address), address


def test_learning_a_portal_writes_nothing():
    with flask_app.app_context():
        db.create_all()
        _purge()
        amns = Company(name='Amns', is_active=True)
        db.session.add(amns)
        db.session.commit()

        for address in (PROCOL, 'x@ansmtp.ariba.com',
                        'noreply@superprocure.in',
                        'x@procurengine.com'):
            assert account_match.learn(
                address, amns.id, actor='DIR12010',
                reason='linked from the lead sheet') == [], address
        assert SenderAccountLink.query.count() == 0
        _purge()


def test_a_real_customer_is_still_learned():
    """The rule has to be narrow enough to leave the matcher
    working."""
    with flask_app.app_context():
        db.create_all()
        _purge()
        account = Company(name='ZU Real Customer', is_active=True)
        db.session.add(account)
        db.session.commit()
        written = account_match.learn(
            'buyer@zureal.example', account.id, actor='DIR12010')
        assert written
        kinds = {row.kind for row in SenderAccountLink.query.all()}
        assert kinds == {BY_EMAIL, BY_DOMAIN}
        _purge()


def test_the_rule_sits_where_every_teacher_goes_through():
    """Four paths teach: the lead sheet, Account check, the
    assignment service and a recovery script. The fifth will too."""
    source = open(os.path.join(_ROOT, 'app', 'services',
                               'account_match.py')).read()
    tree = ast.parse(source)
    fn = [n for n in ast.walk(tree)
          if isinstance(n, ast.FunctionDef) and n.name == 'learn'][0]
    body = ast.unparse(fn)
    assert 'is_portal_sender' in body
    # and it refuses BEFORE building the pairs to write
    assert body.index('is_portal_sender') < body.index('pairs = [')

    import subprocess
    out = subprocess.run(
        ['grep', '-rn', 'SenderAccountLink(', '--include=*.py', _ROOT],
        capture_output=True, text=True).stdout
    writers = [line for line in out.splitlines()
               if '/tests/' not in line and 'models/assignment' not in line]
    assert len(writers) == 2, writers   # learn(), and the undo script


def test_the_sweep_finds_the_rows_already_written():
    mod = _script()
    with flask_app.app_context():
        db.create_all()
        _purge()
        amns = Company(name='Amns', is_active=True)
        real = Company(name='ZU Real Customer', is_active=True)
        db.session.add_all([amns, real])
        db.session.commit()
        rows = [
            (BY_EMAIL, PROCOL, amns.id),
            (BY_DOMAIN, 'procol.in', amns.id),
            (BY_DOMAIN, 'ansmtp.ariba.com', amns.id),
            (BY_DOMAIN, 'superprocure.in', amns.id),
            (BY_DOMAIN, 'procurengine.com', real.id),
            (BY_DOMAIN, 'zureal.example', real.id),
        ]
        for kind, value, account_id in rows:
            db.session.add(SenderAccountLink(
                kind=kind, value=value, account_id=account_id,
                learned_by='DIR12010', hits=3))
        db.session.commit()

        found = mod.learned_portals()
        values = {row.value for row, _p, _a in found}
        assert values == {PROCOL, 'procol.in', 'ansmtp.ariba.com',
                          'superprocure.in', 'procurengine.com'}
        assert 'zureal.example' not in values
        _purge()


def test_the_sweep_removes_them_and_can_put_them_back():
    mod = _script()
    with flask_app.app_context():
        db.create_all()
        _purge()
        amns = Company(name='Amns', is_active=True)
        db.session.add(amns)
        db.session.commit()
        db.session.add_all([
            SenderAccountLink(kind=BY_EMAIL, value=PROCOL,
                              account_id=amns.id, learned_by='DIR12010',
                              learned_from_lead=12, hits=7,
                              reason='linked from the lead sheet'),
            SenderAccountLink(kind=BY_DOMAIN, value='procol.in',
                              account_id=amns.id, learned_by='DIR12010',
                              hits=31),
        ])
        db.session.commit()

        batch = mod.next_batch_id()
        assert mod.apply(mod.learned_portals(), batch=batch,
                         actor='DIR12010') == 2
        assert SenderAccountLink.query.count() == 0

        # Everything the row held is in the audit row.
        row = (AuditEvent.query
               .filter(AuditEvent.action == 'assignment.link_removed')
               .order_by(AuditEvent.id.asc()).first())
        held = row.old_value or {}
        assert held.get('value') == PROCOL
        assert held.get('learned_by') == 'DIR12010'
        assert held.get('learned_from_lead') == 12
        assert held.get('hits') == 7
        assert row.actor == 'DIR12010'

        assert mod.undo(batch) == 2
        back = {r.value: r for r in SenderAccountLink.query.all()}
        assert set(back) == {PROCOL, 'procol.in'}
        assert back[PROCOL].learned_by == 'DIR12010'
        assert back[PROCOL].hits == 7
        _purge()


def test_the_undo_leaves_a_rule_something_has_taught_again(capsys):
    mod = _script()
    with flask_app.app_context():
        db.create_all()
        _purge()
        amns = Company(name='Amns', is_active=True)
        other = Company(name='ZU Other', is_active=True)
        db.session.add_all([amns, other])
        db.session.commit()
        db.session.add(SenderAccountLink(
            kind=BY_DOMAIN, value='procol.in', account_id=amns.id,
            learned_by='DIR12010'))
        db.session.commit()
        batch = mod.next_batch_id()
        mod.apply(mod.learned_portals(), batch=batch, actor='DIR12010')
        # Somebody (or something) teaches it again, differently.
        db.session.add(SenderAccountLink(
            kind=BY_DOMAIN, value='procol.in', account_id=other.id,
            learned_by='SOMEBODY'))
        db.session.commit()
        mod.undo(batch)
        rows = SenderAccountLink.query.filter_by(value='procol.in').all()
        assert len(rows) == 1
        assert rows[0].account_id == other.id
        _purge()
    assert 'left alone' in capsys.readouterr().out


def test_the_sweep_needs_an_actor_and_says_why_it_deletes():
    source = open(SCRIPT).read()
    assert '--apply needs --actor' in source
    # The one place a hard delete is right, with the reason written
    # down beside it.
    assert 'db.session.delete(row)' in source
    assert 'rule the CRM applies to future mail' in source


# ═══ item 2 · the dialog that moved the owner ══════════════════════
def test_the_owner_question_is_not_a_browser_confirm_any_more():
    html = open(APP_HTML).read()
    start = html.index('function laAskOwner(q)')
    body = html[start:html.index('function ocClose()', start)]
    assert 'confirm(' not in body, 'the confirm() is still there'
    assert "g('ocMob').style.display = 'flex'" in body


def test_keep_is_what_every_dismissal_gives():
    html = open(APP_HTML).read()
    # the ✕, the click outside, and Escape
    marker = html.index('id="ocMob"')
    head = html[marker:marker + 700]
    assert 'onclick="if(event.target===this)ocKeep()"' in head
    assert '<button class="mx" onclick="ocKeep()">' in head
    assert "if (host && host.style.display === 'flex') ocKeep();" in html


def test_keep_posts_the_pic_and_switch_posts_the_owner():
    html = open(APP_HTML).read()
    keep = html[html.index('function ocKeep()'):
                html.index('function ocSwitch()')]
    assert 'ocChose(q.lead_pic, true)' in keep
    switch = html[html.index('function ocSwitch()'):
                  html.index('function ocChose(')]
    assert 'ocChose(q.account_owner, false)' in switch


def test_the_keep_button_is_the_one_that_takes_focus():
    html = open(APP_HTML).read()
    start = html.index('function laAskOwner(q)')
    body = html[start:html.index('function ocClose()', start)]
    assert "g('ocKeep'); if(b) b.focus()" in body
    # And the window says that closing it changes nothing.
    assert 'Closing this window keeps things as they are' in body


def test_an_unowned_lead_is_offered_not_asked():
    """"Keep nobody" is not a choice, so a dismissal there writes
    nothing at all."""
    html = open(APP_HTML).read()
    keep = html[html.index('function ocKeep()'):
                html.index('function ocSwitch()')]
    assert 'if (q.unowned) return;' in keep


# ═══ item 3 · the pairs nobody could reach ═════════════════════════
def _pair_leads(key, *, named_first=True):
    amns = Company.query.filter_by(name='Amns').first()
    if amns is None:
        amns = Company(name='Amns', is_active=True)
        db.session.add(amns)
        db.session.commit()
    first = Lead(company='ZU To review', stage='New Opportunity',
                 portal_key=key, original_email_from=PROCOL)
    second = Lead(company='Amns', company_id=amns.id,
                  stage='New Opportunity', portal_key=key,
                  original_email_from=PROCOL)
    db.session.add_all([first, second])
    db.session.commit()
    return first, second


def test_a_portal_pair_is_ranked_above_a_stronger_signal():
    """Not by inflating its score — a GSTIN really is a stronger
    statement about identity. By saying that this kind of pair is the
    one a person can act on in a second."""
    from app.services import lead_dupe_report as report

    with flask_app.app_context():
        db.create_all()
        _purge()
        # A GSTIN pair, which scores 95.
        # A real checksum: `norm_gstin` validates, so a made-up
        # number would not match another made-up number.
        account = Company(name='ZU Same Entity', is_active=True,
                          gstin='27AAPFU0939F1ZV')
        db.session.add(account)
        db.session.commit()
        for n in range(2):
            db.session.add(Lead(company='ZU Same Entity',
                                company_id=account.id,
                                stage='New Opportunity'))
        db.session.commit()
        # And a portal pair, which scores 90.
        one, two = _pair_leads('RFX-11971')

        pairs = report.pairs(limit=50)
        assert pairs
        top = pairs[0]
        assert top['top'] == 1
        assert {top['keep']['lead_id'], top['drop']['lead_id']} \
            == {one.id, two.id}
        from app.services import lead_duplicates as ld
        assert ld.PORTAL in top['matched']
        # The stronger signal is still scored higher, just not first.
        others = [p for p in pairs if p['top'] == 0]
        # The stronger signal still scores higher — it is simply not
        # the first thing on the screen any more.
        assert others
        assert max(p['score'] for p in others) > top['score']
        _purge()


def test_the_screen_can_reach_a_pair_past_the_first_hundred():
    from app.services import lead_dupe_report as report

    with flask_app.app_context():
        db.create_all()
        _purge()
        made = [_pair_leads(f'RFX-9{n:04d}') for n in range(12)]
        everything, total = report.pairs(count=True)
        assert total >= 12
        first = report.pairs(limit=5, offset=0)
        second = report.pairs(limit=5, offset=5)
        assert len(first) == 5 and len(second) == 5
        assert not ({p['keep']['lead_id'] for p in first}
                    & {p['keep']['lead_id'] for p in second})
        # The same list, walked: page two is page one's continuation.
        assert [p['keep']['lead_id'] for p in everything[5:10]] == \
            [p['keep']['lead_id'] for p in second]
        _purge()


def test_the_api_sends_the_total_not_the_page_size():
    """It returned the first hundred and called that the count, so
    nothing on the screen could say there was more."""
    from app import Employee

    with flask_app.app_context():
        db.create_all()
        _purge()
        Employee.query.filter_by(emp_code='ZU_ADM').delete()
        db.session.add(Employee(
            emp_code='ZU_ADM', name='Admin', password_hash='x',
            role='admin', email='zu.adm@procamlogistics.com',
            is_active=True))
        db.session.commit()
        for n in range(6):
            _pair_leads(f'RFX-8{n:04d}')

    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess['emp_code'] = 'ZU_ADM'
        sess['role'] = 'admin'
    r = client.get('/api/admin/lead-duplicates?limit=2&offset=0')
    assert r.status_code == 200, r.status_code
    body = r.get_json()
    assert body['ok'] is True
    assert len(body['pairs']) == 2
    assert body['total'] >= 6
    assert body['count'] == 2
    assert body['has_more'] is True
    assert body['offset'] == 0

    last = client.get(f'/api/admin/lead-duplicates'
                      f'?limit=200&offset=0').get_json()
    assert last['has_more'] is False

    with flask_app.app_context():
        Employee.query.filter_by(emp_code='ZU_ADM').delete()
        db.session.commit()
        _purge()


def test_the_page_walks_and_says_where_it_is():
    html = open(DUPES_HTML).read()
    assert 'const PAGE = 50' in html
    assert 'function step(by)' in html
    assert "'&offset=' + OFFSET" in html
    assert 'of ' in html
    # The way back as well as the way on.
    assert "document.getElementById('prev').disabled = OFFSET <= 0" in html
    assert 'TOTAL > PAGE' in html


def test_a_row_out_of_score_order_says_why():
    """A list that is not in score order and does not say so reads as
    a bug."""
    html = open(DUPES_HTML).read()
    assert 'p.top' in html
    assert 'one auction' in html


def test_the_lead_screen_still_renders_with_the_new_window():
    """A template that does not render is a blank CRM, and the JS in
    it is only ever exercised in a browser — so at least prove the
    page builds and the window is in it."""
    from app import Employee

    with flask_app.app_context():
        db.create_all()
        Employee.query.filter_by(emp_code='ZU_REN').delete()
        db.session.add(Employee(
            emp_code='ZU_REN', name='Somebody', password_hash='x',
            role='admin', email='zu.ren@procamlogistics.com',
            is_active=True, must_change_pw=False))
        db.session.commit()
    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess['emp_code'] = 'ZU_REN'
        sess['role'] = 'admin'
    r = client.get('/app')
    assert r.status_code == 200, r.status_code
    html = r.get_data(as_text=True)
    assert 'id="ocMob"' in html
    assert 'function ocKeep()' in html
    # And the confirm() that used to decide the owner is gone from
    # the page as a whole, not just from one function.
    assert 'Cancel — switch the lead to' not in html
    with flask_app.app_context():
        Employee.query.filter_by(emp_code='ZU_REN').delete()
        db.session.commit()
