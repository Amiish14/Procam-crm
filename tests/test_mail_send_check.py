"""
The CRM can tell you whether it is allowed to send email — without
sending any, without a database, and without ever putting the access
token where a person or a log file could see it.

Three callers share one answer: the ops check (`graph_mail_send`), the
production preflight's CONFIG section, and scripts/check_mail_send.py.
These tests hold all three to the same wording, and hold the JWT
decoder to never raising on rubbish.

The checks library is loaded by path, the way scripts/ops_status.py
loads it, so nothing here imports app.py.
"""
import base64
import importlib.util
import json
import logging
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, 'scripts'))

import ops_status                                               # noqa: E402
import production_preflight as pf                               # noqa: E402

C = ops_status.load_checks()
OK, WARN, FAIL, UNKNOWN = C.OK, C.WARN, C.FAIL, C.UNKNOWN

_CREDS = {'MS_TENANT_ID': 'tenant-guid', 'MS_CLIENT_ID': 'client-guid',
          'MS_CLIENT_SECRET': 'the-client-secret-value'}

#: Long enough to be unmistakable if it ever leaks into a report or log.
_SECRET_LOOKING = 'tok-' + 'z' * 40


def _b64(raw: bytes) -> str:
    """base64url with the padding stripped, exactly as a JWT carries it."""
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()


def _jwt(claims, *, signature='not-a-real-signature') -> str:
    return '.'.join((_b64(b'{"alg":"RS256","typ":"JWT"}'),
                     _b64(json.dumps(claims).encode()), signature))


def _token(roles, **claims) -> str:
    base = {'appid': 'bd542cf9-f851-4bcb-b731-70e3e0b2ae42',
            'tid': 'tenant-guid', 'aud': 'https://graph.microsoft.com',
            'nonce': _SECRET_LOOKING}
    if roles is not None:
        base['roles'] = list(roles)
    base.update(claims)
    return _jwt(base)


@pytest.fixture()
def ctx(tmp_path):
    """A context with credentials and the network allowed. No database:
    the check reads a token's claims and touches nothing else."""
    return C.Context(root=str(tmp_path), db_path=str(tmp_path / 'none.db'),
                     backups_dir=str(tmp_path), env_path=str(tmp_path /
                     '.env'), base_url='', network=True,
                     schema=False, environ=dict(_CREDS))


def _issue(monkeypatch, token, status=200):
    """Answer the token endpoint with `token`; any other URL is a bug."""
    def http(url, data=None, headers=None, timeout=None):
        assert 'login.microsoftonline.com' in url, f'unexpected call: {url}'
        body = {'access_token': token, 'expires_in': 3599} if token \
            else {'error': 'invalid_client',
                  'error_description': 'AADSTS7000222: secret expired'}
        return status, json.dumps(body).encode()
    monkeypatch.setattr(C, '_http', http)


# ── the decoder ──────────────────────────────────────────────────────
def test_the_payload_is_read_whatever_its_padding():
    # A JWT drops the base64 '=' padding, so a payload needing one, two
    # or three of them back must still decode. Varying the claim length
    # by a byte at a time walks through every case.
    for pad in range(4):
        claims = {'roles': ['Mail.Send'], 'pad': 'x' * pad}
        assert pf.decode_jwt_claims(_jwt(claims))['pad'] == 'x' * pad


@pytest.mark.parametrize('rubbish', [
    None, '', 'not-a-jwt', 'two.parts', 'a.!!!!.c', 'a..c', 123, b'bytes',
    'a.' + _b64(b'[1,2]') + '.c',          # valid JSON, not an object
    'a.' + _b64(b'not json') + '.c',
])
def test_anything_that_is_not_a_readable_jwt_decodes_to_nothing(rubbish):
    assert pf.decode_jwt_claims(rubbish) == {}
    assert pf.token_roles(rubbish) == []


def test_roles_are_sorted_deduplicated_and_survive_a_wrong_shape():
    assert pf.token_roles(_token(['Mail.Send', 'Mail.Read', 'Mail.Send'])) \
        == ['Mail.Read', 'Mail.Send']
    assert pf.token_roles(_token(None)) == []              # no roles claim
    assert pf.token_roles(_jwt({'roles': 'Mail.Send'})) == []   # not a list


def test_the_verdict_is_worded_once_for_every_caller():
    granted, detail = pf.mail_send_verdict(['Mail.Read', 'Mail.Send'])
    assert granted and 'granted' in detail
    granted, detail = pf.mail_send_verdict(['Mail.Read'])
    assert not granted
    for consequence in pf.MAIL_SEND_BREAKS:
        assert consequence in detail
    assert pf.MAIL_SEND_RUNBOOK in detail


# ── the ops check ────────────────────────────────────────────────────
def test_mail_send_granted_is_ok(ctx, monkeypatch):
    _issue(monkeypatch, _token(['Mail.Read', 'Mail.Send', 'User.Read.All']))
    r = C.check_mail_send(ctx)
    assert r['status'] == OK
    assert r['value'] == {'roles': ['Mail.Read', 'Mail.Send',
                                    'User.Read.All'], 'mail_send': True}


def test_mail_send_missing_warns_and_names_what_breaks(ctx, monkeypatch):
    _issue(monkeypatch, _token(['Mail.Read']))
    r = C.check_mail_send(ctx)
    assert r['status'] == WARN
    assert 'Mail.Read' in r['detail']           # what we do have
    for consequence in pf.MAIL_SEND_BREAKS:     # ... and what it costs
        assert consequence in r['detail']
    assert 'GRAPH_MAIL_SEND.md' in r['detail']
    assert r['value']['mail_send'] is False


def test_a_token_with_no_permissions_at_all_is_unknown(ctx, monkeypatch):
    _issue(monkeypatch, _token(None))
    r = C.check_mail_send(ctx)
    assert r['status'] == UNKNOWN and 'roles' in r['detail']


def test_a_malformed_token_is_unknown_not_a_traceback(ctx, monkeypatch):
    _issue(monkeypatch, 'this-is-not-a-jwt')
    [r] = C.run_checks(ctx, checks=[('graph_mail_send', C.check_mail_send)])
    assert r['status'] == UNKNOWN
    assert 'crashed' not in r['detail'] and 'Traceback' not in r['detail']


def test_without_a_token_it_repeats_the_credential_wording(
        ctx, monkeypatch):
    # no credentials
    ctx.environ, ctx._graph = {}, None
    bare = C.check_mail_send(ctx)
    assert bare['status'] == UNKNOWN
    assert bare['detail'] == C.check_graph_token(ctx)['detail']

    # --no-network
    ctx.environ, ctx._graph, ctx.network = dict(_CREDS), None, False
    off = C.check_mail_send(ctx)
    assert off['status'] == UNKNOWN and 'no-network' in off['detail']

    # credentials refused by Microsoft
    ctx.network, ctx._graph = True, None
    _issue(monkeypatch, None, status=401)
    bad = C.check_mail_send(ctx)
    assert bad['status'] == FAIL
    assert bad['detail'] == C.check_graph_token(ctx)['detail']
    assert _CREDS['MS_CLIENT_SECRET'] not in json.dumps(bad)


def test_the_check_is_part_of_the_report_and_needs_no_database(ctx,
                                                               monkeypatch):
    assert 'graph_mail_send' in C.CHECK_KEYS
    _issue(monkeypatch, _token(['Mail.Read']))
    assert not os.path.exists(ctx.db_path)      # nothing opened a database
    r = C.check_mail_send(ctx)
    assert set(r) == {'key', 'label', 'status', 'detail', 'measured_at',
                      'value'}
    assert not os.path.exists(ctx.db_path)


# ── the token never escapes ──────────────────────────────────────────
def test_no_path_ever_prints_or_logs_the_token(ctx, monkeypatch, caplog,
                                               capsys):
    """The token is a bearer credential: anyone holding it can read the
    mailbox. It may be decoded, never emitted."""
    token = _token(['Mail.Read', 'Mail.Send'])
    _issue(monkeypatch, token)
    caplog.set_level(logging.DEBUG)

    r = C.check_mail_send(ctx)
    rep = pf.Report()
    monkeypatch.setattr(pf, 'graph_access_token', lambda *a, **k: ('ok',
                                                                   token))
    pf.check_graph_permissions(rep)
    cli = _cli()
    monkeypatch.setattr(cli, 'fetch_token', lambda: ('ok', token))
    monkeypatch.setattr(cli, 'mailbox', lambda: 'leads@procamgroup.in')
    assert cli.main([]) == 0

    printed = capsys.readouterr().out
    for haystack in (json.dumps(r), json.dumps(rep.rows), caplog.text,
                     printed):
        assert token not in haystack
        assert _SECRET_LOOKING not in haystack     # nor any claim of it
    assert 'Mail.Send' in printed                  # it did do the work


# ── the preflight's CONFIG section ───────────────────────────────────
def _rows(rep, name='Mail.Send permission'):
    return [r for r in rep.rows if r['check'] == name]


def test_the_preflight_reports_the_grant_in_config(monkeypatch):
    monkeypatch.setattr(pf, 'graph_access_token',
                        lambda *a, **k: ('ok', _token(['Mail.Read'])))
    rep = pf.Report()
    pf.check_graph_permissions(rep)
    [row] = _rows(rep)
    assert row['area'] == 'config' and row['status'] == pf.WARN
    assert pf.MAIL_SEND_RUNBOOK in row['detail']

    monkeypatch.setattr(pf, 'graph_access_token', lambda *a, **k: (
        'ok', _token(['Mail.Read', 'Mail.Send'])))
    rep = pf.Report()
    pf.check_graph_permissions(rep)
    assert _rows(rep)[0]['status'] == pf.PASS
    assert _rows(rep, 'Graph application permissions')[0]['detail'] == \
        'Mail.Read, Mail.Send'


@pytest.mark.parametrize('state,info,status', [
    ('missing', 'MS_CLIENT_SECRET', 'INFO'),
    ('unreachable', 'URLError: timed out', 'INFO'),
    ('refused', 'HTTP 401 invalid_client AADSTS7000222', 'WARN'),
])
def test_the_preflight_never_fails_the_deploy_over_an_unreadable_grant(
        monkeypatch, state, info, status):
    monkeypatch.setattr(pf, 'graph_access_token', lambda *a, **k: (state,
                                                                   info))
    rep = pf.Report()
    pf.check_graph_permissions(rep)
    assert _rows(rep)[0]['status'] == getattr(pf, status)
    assert not rep.failed


def test_the_preflight_asks_nothing_of_the_network_when_told_not_to(
        monkeypatch):
    def boom(*a, **k):
        raise AssertionError('a token was fetched despite --no-network')
    monkeypatch.setattr(pf, 'graph_access_token', boom)
    rep = pf.Report()
    pf.check_graph_permissions(rep, network=False)
    assert _rows(rep)[0]['status'] == pf.INFO


# ── the administrator's CLI ──────────────────────────────────────────
def _cli():
    """scripts/check_mail_send.py, loaded by path like any other script."""
    name = 'check_mail_send'
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_ROOT, 'scripts', 'check_mail_send.py'))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_the_cli_says_plainly_whether_mail_can_be_sent(monkeypatch, capsys):
    cli = _cli()
    monkeypatch.setattr(cli, 'mailbox', lambda: 'leads@procamgroup.in')

    monkeypatch.setattr(cli, 'fetch_token',
                        lambda: ('ok', _token(['Mail.Read', 'Mail.Send'])))
    assert cli.main([]) == 0
    assert 'CAN SEND' in capsys.readouterr().out

    monkeypatch.setattr(cli, 'fetch_token',
                        lambda: ('ok', _token(['Mail.Read'])))
    assert cli.main([]) == 1
    out = capsys.readouterr().out
    assert 'CANNOT SEND' in out and 'GRAPH_MAIL_SEND.md' in out


def test_the_cli_refuses_to_send_a_test_without_the_grant(monkeypatch,
                                                          capsys):
    cli = _cli()
    monkeypatch.setattr(cli, 'mailbox', lambda: 'leads@procamgroup.in')
    monkeypatch.setattr(cli, 'fetch_token',
                        lambda: ('ok', _token(['Mail.Read'])))

    def never(*a, **k):
        raise AssertionError('a test message was attempted without the grant')
    monkeypatch.setattr(cli, '_send_test', never)

    assert cli.main(['--send-test', 'someone@procamgroup.in']) == 1
    out = capsys.readouterr().out
    assert 'refused' in out and '403' in out     # and why


def test_the_cli_sends_the_test_once_the_grant_is_there(monkeypatch,
                                                        capsys):
    cli = _cli()
    sent = []
    monkeypatch.setattr(cli, 'mailbox', lambda: 'leads@procamgroup.in')
    monkeypatch.setattr(cli, 'fetch_token',
                        lambda: ('ok', _token(['Mail.Read', 'Mail.Send'])))
    monkeypatch.setattr(cli, '_send_test',
                        lambda addr, roles: sent.append(addr) or True)
    monkeypatch.setattr(cli, 'notifications_enabled', lambda: True)
    assert cli.main(['--send-test', 'someone@procamgroup.in']) == 0
    assert sent == ['someone@procamgroup.in']
    assert 'SENT' in capsys.readouterr().out

    monkeypatch.setattr(cli, '_send_test', lambda addr, roles: False)
    assert cli.main(['--send-test', 'someone@procamgroup.in']) == 1
    assert 'FAILED' in capsys.readouterr().out


def test_the_cli_will_not_blame_the_grant_for_notifications_being_off(
        monkeypatch, capsys):
    cli = _cli()
    monkeypatch.setattr(cli, 'mailbox', lambda: 'leads@procamgroup.in')
    monkeypatch.setattr(cli, 'fetch_token',
                        lambda: ('ok', _token(['Mail.Read', 'Mail.Send'])))
    monkeypatch.setattr(cli, 'notifications_enabled', lambda: False)

    def never(*a, **k):
        raise AssertionError('sent while NOTIFY_ENABLED is off')
    monkeypatch.setattr(cli, '_send_test', never)

    assert cli.main(['--send-test', 'someone@procamgroup.in']) == 1
    out = capsys.readouterr().out
    assert 'NOTIFY_ENABLED' in out and 'CAN SEND' in out


def test_the_cli_cannot_tell_without_a_token(monkeypatch, capsys):
    cli = _cli()
    monkeypatch.setattr(cli, 'fetch_token',
                        lambda: ('missing', 'MS_CLIENT_SECRET'))
    assert cli.main([]) == 2
    out = capsys.readouterr().out
    assert 'CANNOT TELL' in out and 'MS_CLIENT_SECRET' in out
