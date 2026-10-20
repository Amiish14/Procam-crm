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
