"""
Does the CRM's mail transport actually work?

The presence of MAIL_* variables proves nothing — a password can be
stale, a mailbox can be blocked from sending, a firewall can sit in
front of port 587. This answers the four questions separately, so a
failure says which part failed:

    connection      can we reach the server at all
    STARTTLS        does it offer encryption and accept it
    authentication  do the credentials work
    send            will it accept a message from this sender

Nothing here prints a password, and nothing logs one. The only
credential that appears in the output is the username, because a
failure you cannot attribute to an account is not a diagnosis.

    .venv/bin/python scripts/check_smtp.py
    .venv/bin/python scripts/check_smtp.py --send-to you@procamgroup.in

Without --send-to it stops after authentication and sends nothing.
"""
import argparse
import os
import smtplib
import ssl
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))


def _mask(value):
    """Enough to tell two values apart, not enough to use."""
    if not value:
        return '(not set)'
    return f'set, {len(value)} characters'



def _load_env_file(path):
    """Read KEY=VALUE lines out of a .env, without running it.

    `set -a; . file` executes the file: a value containing a space, a
    `<`, or a `$(...)` is a command as far as the shell is concerned.
    The TMS's own sender line — `MAIL_DEFAULT_SENDER=Procam TMS
    <tms@procamgroup.in>` — is enough to break it, and a less harmless
    line would be enough to do something. So this parses.

    Only MAIL_* is taken. Nothing else in another application's
    configuration is any of this script's business.
    """
    taken = {}
    try:
        with open(path, encoding='utf-8', errors='replace') as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, _, value = line.partition('=')
                key = key.strip()
                if not key.startswith('MAIL_'):
                    continue
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] in ('"', "'"):
                    value = value[1:-1]
                os.environ[key] = value
                taken[key] = value
    except OSError as exc:
        print(f'could not read {path}: {exc}')
    return taken


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--send-to', dest='send_to',
                    help='address to send a real test message to')
    ap.add_argument('--timeout', type=int, default=20)
    ap.add_argument('--env-file',
                    help='read MAIL_* from this file for this run only. '
                         'Parsed, never executed — do not source another '
                         'application\'s .env into a shell.')
    args = ap.parse_args()

    if args.env_file:
        loaded = _load_env_file(args.env_file)
        print(f'read {len(loaded)} MAIL_* value(s) from {args.env_file}\n')

    host = (os.environ.get('MAIL_SERVER') or '').strip()
    port = int((os.environ.get('MAIL_PORT') or '587').strip() or 587)
    username = (os.environ.get('MAIL_USERNAME') or '').strip()
    password = os.environ.get('MAIL_PASSWORD') or ''
    sender_raw = ((os.environ.get('CRM_MAIL_FROM')
                   or os.environ.get('MAIL_DEFAULT_SENDER') or '').strip())
    use_ssl = (os.environ.get('MAIL_USE_SSL') or '').lower() in (
        '1', 'true', 'yes', 'on')
    use_tls = (os.environ.get('MAIL_USE_TLS') or 'true').lower() in (
        '1', 'true', 'yes', 'on')

    print('configuration')
    print(f'  MAIL_SERVER        {host or "(not set)"}')
    print(f'  MAIL_PORT          {port}')
    print(f'  MAIL_USE_TLS       {use_tls}')
    print(f'  MAIL_USE_SSL       {use_ssl}')
    print(f'  MAIL_USERNAME      {username or "(not set)"}')
    print(f'  MAIL_PASSWORD      {_mask(password)}')
    print(f'  sender             {sender_raw or "(not set)"}')
    print()

    if not host:
        print('RESULT: cannot test — MAIL_SERVER is not set in this '
              'environment.')
        print('  The CRM will fall back to Microsoft Graph, which is '
              'refused until Mail.Send is granted.')
        return 2

    results = {'connection': 'not reached', 'starttls': 'not reached',
               'authentication': 'not reached', 'send': 'not attempted'}
    server = None
    try:
        print(f'connecting to {host}:{port} …')
        if use_ssl:
            server = smtplib.SMTP_SSL(host, port, timeout=args.timeout,
                                      context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(host, port, timeout=args.timeout)
        server.ehlo()
        results['connection'] = 'ok'
        print('  connection ok')

        if use_tls and not use_ssl:
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
            results['starttls'] = 'ok'
            print('  STARTTLS ok')
        else:
            results['starttls'] = 'not applicable'

        if username:
            server.login(username, password)
            results['authentication'] = 'ok'
            print(f'  authenticated as {username}')
        else:
            results['authentication'] = 'skipped — no MAIL_USERNAME'
            print('  no username set; the server may still accept relay')

        if args.send_to:
            from app import app
            with app.app_context():
                from app.services import mailer
                name, addr = mailer.sender()
                print(f'  sending a test message from {addr} '
                      f'to {args.send_to} …')
                html = ('<p>Procam CRM SMTP connectivity test.</p>'
                        '<p>If you are reading this, CRM email works.</p>')
                msg_ok = _send_one(server, name, addr, args.send_to, html)
            results['send'] = 'ok' if msg_ok else 'refused'
            print(f'  send {results["send"]}')
    except smtplib.SMTPAuthenticationError as exc:
        results['authentication'] = f'REFUSED — {exc.smtp_code}'
        print(f'  authentication REFUSED: {exc.smtp_code} '
              f'{(exc.smtp_error or b"")[:200]!r}')
    except smtplib.SMTPException as exc:
        print(f'  SMTP error: {type(exc).__name__}: {str(exc)[:300]}')
        for key in results:
            if results[key] == 'not reached':
                results[key] = f'failed — {type(exc).__name__}'
                break
    except OSError as exc:
        print(f'  could not connect: {exc}')
        results['connection'] = f'failed — {exc}'
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:
                pass

    print('\nRESULT')
    for key in ('connection', 'starttls', 'authentication', 'send'):
        print(f'  {key:<16} {results[key]}')

    good = (results['connection'] == 'ok'
            and results['authentication'].startswith(('ok', 'skipped')))
    if good and not args.send_to:
        print('\nTransport reachable and credentials accepted. Re-run with '
              '--send-to <your address> to prove delivery end to end.')
    elif good:
        print('\nCRM email works over SMTP. Set the same MAIL_* variables '
              'in the CRM .env and restart.')
    else:
        print('\nCRM email will NOT work over SMTP with this configuration.')
    return 0 if good else 1


def _send_one(server, from_name, from_addr, to_addr, html):
    from email.message import EmailMessage
    from email.utils import formataddr

    # The internal-only rule applies to a test as much as to anything
    # else: this script must not be the way somebody emails a customer.
    from app.services import mail_policy
    allowed, refused = mail_policy.check([to_addr])
    if refused:
        print(f'  REFUSED: {to_addr} is outside Procam. {mail_policy.describe()}')
        return False

    msg = EmailMessage()
    msg['Subject'] = 'Procam CRM — SMTP connectivity test'
    msg['From'] = formataddr((from_name, from_addr))
    msg['To'] = allowed[0]
    msg['Auto-Submitted'] = 'auto-generated'
    msg.set_content('Procam CRM SMTP connectivity test.')
    msg.add_alternative(html, subtype='html')
    server.send_message(msg, to_addrs=allowed)
    return True


if __name__ == '__main__':
    sys.exit(main())
