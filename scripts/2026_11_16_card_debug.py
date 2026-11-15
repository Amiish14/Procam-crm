"""One card, one reader call, everything it did printed.

For the fault of 15 Nov on live: every specimen read back empty and
`journalctl` had NOT ONE LINE about it — no status, no exception, no
timeout. The reason existed as a string inside the extractor and
nobody printed it.

    .venv/bin/python scripts/2026_11_16_card_debug.py
    .venv/bin/python scripts/2026_11_16_card_debug.py --file /path/card.jpg
    .venv/bin/python scripts/2026_11_16_card_debug.py --raw 2000

AND IN THE SERVICE'S OWN ENVIRONMENT, WHICH IS THE POINT

A shell `.env` and a systemd `EnvironmentFile` are different
environments, and the key can be visible to one and not the other.
This is the same command run as the service sees the world:

    sudo systemd-run --uid=www-data --pipe \\
      --property=EnvironmentFile=/etc/procam-crm.env \\
      --working-directory=/var/www/procam-crm \\
      /var/www/procam-crm/.venv/bin/python \\
      scripts/2026_11_16_card_debug.py

(`systemctl show procam-crm -p EnvironmentFile -p Environment` names
the file this unit actually loads; use that path.)

WHAT IT PRINTS, AND WHAT IT WILL NOT

  * whether a key is visible to THIS process — yes or no, never the
    key, never a prefix of it, never its length
  * the endpoint host, and the model name asked for
  * the HTTP status, the size of the answer, the first 300 characters
    of it, how long it took, and the exception with its type
  * what the picture became before it was sent: type, size, pixels

READ-ONLY. It touches no table and writes no file.
"""
from __future__ import annotations

import argparse
import mimetypes
import os
import socket
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from app import app                                       # noqa: E402

KEY = 'ANTHROPIC_API_KEY'
DEFAULT_HOST = 'api.anthropic.com'


def _rule(n=76):
    print('─' * n)


def _host():
    base = (os.environ.get('ANTHROPIC_BASE_URL')
            or os.environ.get('ANTHROPIC_API_URL') or '')
    if base:
        return base.split('://')[-1].split('/')[0]
    return DEFAULT_HOST


def environment():
    from app.services import business_card_ocr as ocr

    print('\n══ the environment this process sees ══')
    print(f'  pid {os.getpid()}, user {os.environ.get("USER") or os.getuid()}')
    print(f'  key visible to this process: '
          f'{"yes" if os.environ.get(KEY) else "NO"}   ({KEY})')
    print(f'  endpoint host:  {_host()}')
    print(f'  model asked for: {ocr.service_model()}')
    where = ('BUSINESS_CARD_MODEL' if os.environ.get('BUSINESS_CARD_MODEL')
             else 'the default in business_card_ocr.py')
    print(f'    (from {where})')
    for name in ('HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'NO_PROXY'):
        if os.environ.get(name):
            print(f'  {name}={os.environ[name]}')
    try:
        import anthropic
        print(f'  client library: {anthropic.__version__}')
    except Exception as exc:                              # noqa: BLE001
        print(f'  client library: UNAVAILABLE ({exc})')
    ready, why = ocr.service_ready()
    if not ready:
        print(f'\n  !! the reader cannot run: {why}')
        if 'no key' in why:
            # The whole reason this command exists twice over: a shell
            # `.env` and a systemd EnvironmentFile are different
            # environments, and the key can be in one and not the other.
            print('     If the CRM itself can read cards and this '
                  'cannot, the')
            print('     service reads a different environment. Ask it '
                  'which:')
            print('       systemctl show procam-crm -p EnvironmentFile '
                  '-p Environment')
            print('     then run this command again inside it (the '
                  'docstring at the')
            print('     top of this file has the systemd-run line).')
    return ready


def reachable(host=None, port=443, timeout=6):
    """Can this box open a socket to the endpoint at all?

    The first suspect on the list, and the cheapest to rule out: a
    blocked egress looks exactly like a silent reader.
    """
    host = host or _host()
    print('\n══ can this box reach the endpoint ══')
    started = time.time()
    try:
        addresses = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        print(f'  DNS: {host} → '
              f'{", ".join(sorted({a[4][0] for a in addresses}))}')
    except Exception as exc:                              # noqa: BLE001
        print(f'  DNS FAILED for {host}: {type(exc).__name__}: {exc}')
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
        print(f'  TCP {host}:{port} open  '
              f'({int((time.time() - started) * 1000)} ms)')
        return True
    except Exception as exc:                              # noqa: BLE001
        print(f'  TCP {host}:{port} FAILED: {type(exc).__name__}: {exc}')
        print('  → outbound HTTPS looks blocked from this box '
              '(firewall, NSG or proxy).')
        return False


def one_card(path=None, raw_chars=300):
    from app.services import business_card_ocr as ocr, card_image, card_samples

    print('\n══ the picture ══')
    if path:
        with open(path, 'rb') as fh:
            data = fh.read()
        mime = mimetypes.guess_type(path)[0] or 'image/jpeg'
        print(f'  {path}')
    else:
        specimen = card_samples.by_key('plain')
        data, mime = card_samples.as_bytes(specimen)
        print(f'  specimen "{specimen["key"]}" — {specimen["what"]}')
    print(f'  as uploaded: {mime}, {len(data) // 1024} KB')

    ready = card_image.prepare(data, mime)
    for note in ready.notes:
        print(f'    · {note}')
    pictures = ([(png, 'image/png') for png in ready.pages]
                or [(data, mime)])
    for n, (blob, kind) in enumerate(pictures):
        size = ''
        try:
            import io
            from PIL import Image
            with Image.open(io.BytesIO(blob)) as img:
                size = f', {img.size[0]}×{img.size[1]} px'
        except Exception:                                 # noqa: BLE001
            pass
        print(f'  sent as #{n + 1}: {kind}, {len(blob) // 1024} KB{size}')

    print('\n══ the reader call ══')
    found = ocr.read_with_service(pictures)
    for note in found['notes']:
        print(f'  · {note}')
    print(f'  HTTP status:   {found["status"] or "(no response)"}')
    print(f'  elapsed:       {found["ms"]} ms')
    print(f'  answer size:   {len(found["raw"])} characters')
    if found['raw']:
        print(f'  first {raw_chars} characters of the answer:')
        print('    ' + found['raw'][:raw_chars].replace('\n', '\n    '))
    if found['error']:
        print(f'\n  !! FAILED: {found["error"]}')
        _blame(found)
    else:
        fields = found['fields'] or {}
        print(f'\n  read: name={fields.get("name")!r} '
              f'company={fields.get("company")!r} '
              f'emails={fields.get("emails")!r}')
    return found


def _blame(found):
    """The likely cause, named, with what to do about it."""
    text = (found.get('error') or '').lower()
    status = str(found.get('status') or '')
    if 'no key is visible' in text:
        print('  → the key is not in THIS process\'s environment. If the '
              'CRM works')
        print('    and this does not, the service reads a different '
              'EnvironmentFile:')
        print('    systemctl show procam-crm -p EnvironmentFile '
              '-p Environment')
    elif status == '404' or 'not_found' in text or 'model' in text:
        print('  → the model name is refused by this account. Try '
              'another for one')
        print('    run, without changing anything:')
        print('      .venv/bin/python scripts/2026_11_16_card_debug.py '
              '--model claude-haiku-5-5')
        print('    When one answers, set BUSINESS_CARD_MODEL to it in '
              'the service')
        print('    environment and restart.')
    elif status in ('401', '403'):
        print('  → the key is present but not accepted: wrong key, or no '
              'access.')
    elif status == '429':
        print('  → rate limited. Try again; if it persists, the account '
              'is out of')
        print('    credit or over its limit.')
    elif status.startswith('5'):
        print('  → the service itself failed. Try again before changing '
              'anything.')
    elif 'could not reach' in text:
        print('  → outbound HTTPS is blocked, or a proxy is required. '
              'Compare with')
        print('    the socket test above.')
    elif 'not a picture it accepts' in text:
        print('  → the picture never left the box: the type was refused '
              'before')
        print('    sending. A PDF must be rasterised first '
              '(card_image.prepare).')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--file', default=None, metavar='PATH',
                    help='a real card to try instead of the specimen')
    ap.add_argument('--raw', type=int, default=300, metavar='N',
                    help='how much of the answer to print (default 300)')
    ap.add_argument('--no-socket', action='store_true')
    ap.add_argument('--model', default=None, metavar='NAME',
                    help='try a different model name for this run only '
                         '(sets BUSINESS_CARD_MODEL in this process)')
    args = ap.parse_args(argv)

    if args.model:
        os.environ['BUSINESS_CARD_MODEL'] = args.model
    with app.app_context():
        print('business card reader — debug. READ ONLY.')
        _rule()
        ready = environment()
        if not args.no_socket:
            reachable()
        if not ready:
            print('\nStopped: the reader cannot run in this environment. '
                  'The lines')
            print('above say why. Nothing was sent.')
            return 2
        found = one_card(args.file, args.raw)
        return 0 if not found['error'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
