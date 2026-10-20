"""
FF-LINK-01 parts 3 and 5 — accounts for the unmatched, aliases for
the unmaskable.

`scripts/ff_account_hygiene.py` had no tests at all. It is the
second script this week whose write path was handed over before
anything ran it, so these tests invoke it as a subprocess, the way
Nilesh does, and read the database afterwards.

The alias rule is the part worth guarding. A mail signature is
whatever sat at the bottom of a thread, and in production two of
them name the wrong company: #886 Ril's signature is PROCAM's own,
and #990 Vie's is Siemens Healthcare, the shipper. Writing either in
as an alias would point the vendor masking at the wrong company and
leave the real one exposed — a failure that reads as success.
"""
import os
import subprocess
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'ffhyg-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'FfHygTest12345')
_DB = os.path.join(tempfile.mkdtemp(), 'ffhyg.db')
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + _DB)

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app = _main.app
db = _main.db
Company, Lead = _main.Company, _main.Lead

import scripts.ff_account_hygiene as hyg                  # noqa: E402


# ── the rule, in isolation ───────────────────────────────────────────
@pytest.mark.parametrize('account,signature', [
    ('Efl', 'EXPO FREIGHT PRIVATE LIMITED (EFL GLOBAL - INDIA)'),
    ('Ggl', 'GGL LINE PRIVATE LIMITED'),
    ('Mkt', 'Mkt Shipping Co., Ltd'),
])
def test_a_signature_that_accounts_for_the_name_is_taken(account,
                                                         signature):
    assert hyg.corroborates(account, signature)


@pytest.mark.parametrize('account,signature,why', [
    ('Ril', 'PROCAM LOGISTICS PVT. LTD.', 'our own signature'),
    ('Vie', 'Siemens Healthcare Private Limited', 'the shipper'),
    ('Uht', 'Hira Electro Smelters limited', 'a company in the thread'),
    ('Efl', 'Voith Hydro Private Limited', 'a different company'),
    ('Qd', 'QINGDAO ORD INTERNATIONAL LOGISTICS CO.,LTD',
     'plausible — QD is Qingdao — but nothing on the record says so'),
])
def test_a_signature_naming_somebody_else_is_refused(account, signature,
                                                     why):
    assert hyg.corroborates(account, signature) is None, why


def test_initials_read_through_an_ampersand():
    """Dun & Bradstreet is DnB to everyone who deals with it."""
    assert hyg.corroborates('Dnb', 'Dun & Bradstreet Information '
                                   'Services India Pvt Ltd')
    assert hyg.corroborates('Db', 'Dun & Bradstreet')


def test_the_legal_form_is_not_an_initial():
    """Pvt and Ltd are not part of anybody's initialism."""
    sig = 'Zenith (Tianjin) International Logistics Co., Ltd'
    assert hyg.corroborates('Ztil', sig), 'the four real words'
    assert hyg.corroborates('Zti', sig), 'the leading run'
    assert hyg.corroborates('Ztilcl', sig) is None, \
        'Co and Ltd must not contribute initials'


def test_two_initials_off_the_front_are_a_coincidence():
    """Any two-word name opens with some two letters."""
    assert hyg.corroborates(
        'Ze', 'Zenith Tianjin International Logistics') is None


# ── the script, as a script ──────────────────────────────────────────
def _run(*flags):
    """The script, in its own process, against *this* database.

    Not `os.environ['DATABASE_URL']`: the suite shares one app, and
    whichever module imported it first decided where it points. Ask
    the engine instead, or the subprocess writes to one database
    while the assertions read another — which is a pass alone and a
    failure in the suite.
    """
    with flask_app.app_context():
        url = str(db.engine.url)
    return subprocess.run(
        [sys.executable,
         os.path.join(_ROOT, 'scripts', 'ff_account_hygiene.py'), *flags],
        capture_output=True, text=True, cwd=_ROOT,
        env={**os.environ, 'PYTHONPATH': _ROOT, 'DATABASE_URL': url})


@pytest.fixture
def seeded():
    with flask_app.app_context():
        for l in Lead.query.filter(Lead.source == 'ffhyg').all():
            db.session.delete(l)
        for c in Company.query.filter(
                Company.created_by == 'ffhyg').all():
            db.session.delete(c)
        db.session.commit()

        short = Company(name='Efl', is_active=True, created_by='ffhyg',
                        vertical='Freight Forwarding',
                        email_domains=['efl.global'])
        db.session.add(short)
        db.session.flush()
        db.session.add(Lead(
            source='ffhyg', company='Efl', company_id=short.id,
            stage='New', procam_vertical='Freight Forwarding',
            email='ops@efl.global',
            original_email_body=(
                'Regards\n'
                'EXPO FREIGHT PRIVATE LIMITED (EFL GLOBAL - INDIA)\n'
                'Voith Hydro Private Limited\n')))
        # Unmatched, for part 3.
        db.session.add(Lead(
            source='ffhyg', company='Daleel Alsahraa', company_id=None,
            stage='New', procam_vertical='Freight Forwarding',
            email='operation@daleelalsahraa.example'))
        db.session.commit()
        yield short.id


def test_check_writes_nothing(seeded):
    out = _run('--check')
    assert out.returncode == 0, out.stderr[-2000:]
    assert 'DRY RUN' in out.stdout
    with flask_app.app_context():
        assert not (db.session.get(Company, seeded).aliases or [])
        assert Lead.query.filter(
            Lead.company == 'Daleel Alsahraa',
            Lead.company_id.isnot(None)).count() == 0


def test_apply_creates_the_account_and_links_the_lead(seeded):
    out = _run('--apply')
    assert out.returncode == 0, out.stderr[-3000:]
    assert 'APPLY (part 3 only)' in out.stdout
    assert 'DRY RUN' not in out.stdout
    with flask_app.app_context():
        lead = Lead.query.filter_by(company='Daleel Alsahraa').first()
        assert lead.company_id, 'the lead was never linked'
        acct = db.session.get(Company, lead.company_id)
        assert acct.name == 'Daleel Alsahraa'
        from app.models.audit import AuditEvent
        assert AuditEvent.query.filter_by(
            action='company.created',
            entity_id=str(acct.id)).count() == 1
        # Aliases are a different flag and must not have run.
        assert not (db.session.get(Company, seeded).aliases or [])
    again = _run('--apply')
    assert again.returncode == 0
    assert '0 account(s) created' in again.stdout, 'not idempotent'


def test_apply_aliases_writes_the_corroborated_name(seeded):
    out = _run('--apply-aliases')
    assert out.returncode == 0, out.stderr[-3000:]
    assert 'APPLY (aliases only)' in out.stdout
    with flask_app.app_context():
        acct = db.session.get(Company, seeded)
        assert any('EXPO FREIGHT' in a for a in (acct.aliases or [])), \
            'the corroborated name was not written'
        assert not any('Voith' in a for a in acct.aliases), \
            'a different company in the same thread'
        from app.models.audit import AuditEvent
        assert AuditEvent.query.filter_by(
            action='company.alias_added',
            entity_id=str(acct.id)).count() == 1
    again = _run('--apply-aliases')
    assert '0 alias(es) written' in again.stdout, 'not idempotent'


def test_the_alias_is_what_makes_the_masking_work(seeded):
    """The whole point: before, the client's name reaches a carrier."""
    from app.services import ff_privacy
    with flask_app.app_context():
        acct = db.session.get(Company, seeded)
        body = ('Please quote for EXPO FREIGHT PRIVATE LIMITED '
                '(EFL GLOBAL - INDIA).')
        secrets = ff_privacy.secrets_for(account=acct,
                                         include_contacts=False)
        before = ff_privacy.redact(body, *secrets)
        assert 'EXPO FREIGHT' in before, 'nothing to redact it with yet'

        acct.aliases = ['EXPO FREIGHT PRIVATE LIMITED '
                        '(EFL GLOBAL - INDIA)']
        db.session.flush()
        secrets = ff_privacy.secrets_for(account=acct,
                                         include_contacts=False)
        after = ff_privacy.redact(body, *secrets)
        assert 'EXPO FREIGHT' not in after
        db.session.rollback()


def test_two_write_flags_at_once_are_refused():
    out = _run('--apply', '--apply-aliases')
    assert out.returncode != 0
    assert 'exactly one' in (out.stderr + out.stdout)


# ── names a person supplies ──────────────────────────────────────────
def test_set_alias_writes_what_a_person_typed(seeded):
    """No corroboration: "Ril" is Reliance because somebody knew."""
    out = _run('--set-alias', f'{seeded}=Reliance Industries Limited',
               '--actor', 'DIR12010')
    assert out.returncode == 0, out.stderr[-3000:]
    assert 'APPLY (aliases by hand)' in out.stdout
    with flask_app.app_context():
        acct = db.session.get(Company, seeded)
        assert 'Reliance Industries Limited' in (acct.aliases or [])
        from app.models.audit import AuditEvent
        row = AuditEvent.query.filter_by(
            action='company.alias_added',
            entity_id=str(acct.id)).order_by(
                AuditEvent.id.desc()).first()
        assert row.actor == 'DIR12010'
        assert 'by hand' in (row.reason or ''), \
            'the audit row must say nobody corroborated this'
    again = _run('--set-alias', f'{seeded}=Reliance Industries Limited')
    assert '0 alias(es) written' in again.stdout
    assert 'already had that alias' in again.stdout


def test_a_hand_alias_does_not_disturb_the_corroborated_one(seeded):
    _run('--apply-aliases')
    _run('--set-alias', f'{seeded}=Some Other Name')
    with flask_app.app_context():
        aliases = db.session.get(Company, seeded).aliases or []
        assert any('EXPO FREIGHT' in a for a in aliases)
        assert 'Some Other Name' in aliases


def test_an_unknown_account_is_an_error_not_a_silent_skip(seeded):
    out = _run('--set-alias', '99999=Nobody Ltd')
    assert out.returncode != 0
    assert 'do not exist' in out.stdout


@pytest.mark.parametrize('bad', ['886', '886=', 'eight=Name'])
def test_a_malformed_pair_is_refused(bad):
    out = _run('--set-alias', bad)
    assert out.returncode != 0, f'{bad!r} was accepted'


# ── part 4: one action per junk account ──────────────────────────────
@pytest.fixture
def junk():
    """A junk account beside the real one its domain belongs to."""
    with flask_app.app_context():
        for l in Lead.query.filter(Lead.source == 'ffjunk').all():
            db.session.delete(l)
        for c in Company.query.filter(
                Company.created_by == 'ffjunk').all():
            db.session.delete(c)
        db.session.commit()

        real = Company(name='Oyaso Shipping Limited', is_active=True,
                       created_by='ffjunk', email_domains=['oyaso.com'])
        mail = Company(name='Mail', is_active=True, created_by='ffjunk',
                       email_domains=['mail.oyaso.com'])
        lone = Company(name='Sales', is_active=True, created_by='ffjunk',
                       email_domains=[])
        db.session.add_all([real, mail, lone])
        db.session.flush()
        db.session.add(Lead(
            source='ffjunk', company='Oyaso', company_id=mail.id,
            stage='New', procam_vertical='Freight Forwarding',
            email='shipper@mail.oyaso.com'))
        db.session.commit()
        yield {'real': real.id, 'mail': mail.id, 'lone': lone.id}


def _row(rows, cid):
    return next(r for r in rows if r['account'].id == cid)


def test_a_subdomain_account_merges_into_the_domain_it_belongs_to(junk):
    with flask_app.app_context():
        rows = hyg.junk_plan()
        r = _row(rows, junk['mail'])
        assert r['action'] == 'merge'
        assert r['target'].id == junk['real']


def test_an_account_with_nothing_to_go_on_is_left_alone(junk):
    with flask_app.app_context():
        r = _row(hyg.junk_plan(), junk['lone'])
        assert r['action'] == 'leave'
        assert r['to'] == ''


def test_propose_writes_nothing(junk):
    out = _run('--propose-junk')
    assert out.returncode == 0, out.stderr[-2000:]
    assert 'DRY RUN' in out.stdout
    with flask_app.app_context():
        assert db.session.get(Company, junk['mail']).is_active
        assert Lead.query.filter_by(
            source='ffjunk').first().company_id == junk['mail']


def test_apply_junk_moves_the_leads_and_keeps_the_old_account(junk):
    out = _run('--apply-junk', '--actor', 'DIR12010')
    assert out.returncode == 0, out.stderr[-3000:]
    with flask_app.app_context():
        lead = Lead.query.filter_by(source='ffjunk').first()
        assert lead.company_id == junk['real'], 'the lead did not move'
        old = db.session.get(Company, junk['mail'])
        assert old is not None, 'audit rows point at this id'
        assert old.is_active is False
        real = db.session.get(Company, junk['real'])
        assert 'mail.oyaso.com' in (real.email_domains or []), \
            'the domain must follow the leads or the next mail '\
            'rebuilds the junk account'
        from app.models.audit import AuditEvent
        assert AuditEvent.query.filter_by(
            action='company.merged',
            entity_id=str(junk['mail'])).count() == 1
    again = _run('--apply-junk')
    assert '0 renamed, 0 merged' in again.stdout, 'not idempotent'


@pytest.mark.parametrize('line,is_signature', [
    ('TRT International LTD.', True),
    ('Toshiba Logistics India Pvt Ltd', True),
    ('Mitsubishi Electric Corporation', True),
    ('View our company presentation', False),
    ('We are eager to have a long term cooperation with your '
     'esteemed company', False),
    ('Company Overview and Business Activities', False),
])
def test_a_legal_form_ends_a_signature_it_does_not_decorate_prose(
        line, is_signature):
    """"company" appears in sentences; "Pvt Ltd" ends a sign-off."""
    assert bool(hyg.SIG_LEGAL.search(line)) is is_signature


def test_a_mail_platform_never_names_the_client():
    """usatrt.ccsend.com would otherwise make an account "Ccsend"."""
    assert 'ccsend.com' in hyg.MAIL_PLATFORMS
    assert 'onmicrosoft.com' in hyg.MAIL_PLATFORMS
    assert hyg.registrable('usatrt.ccsend.com') == 'ccsend.com'
