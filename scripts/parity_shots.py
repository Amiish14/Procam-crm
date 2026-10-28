"""
Photograph every live RFQ screen next to its page in the demo.

    .venv/bin/python scripts/parity_shots.py --stage before
    .venv/bin/python scripts/parity_shots.py --stage after

RFQ-DEMO-PARITY asks for the pairs, desktop and phone, saved under
`docs/reference/parity/`. The live half runs against a throwaway
SQLite file seeded by `scripts/parity_world.py`; the real database
is never opened, and the flags are switched on only inside that
temp file.

Playwright and its Chromium are a developer dependency. They are
deliberately not in `requirements.txt`: nothing the server runs
needs a browser.

    .venv/bin/pip install playwright
    .venv/bin/python -m playwright install chromium
"""
from __future__ import annotations

import argparse
import os
import pathlib
import socket
import sys
import threading
import time

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

DEMO = _ROOT / 'docs' / 'reference' / 'RFQ_Chain_Tracker_Demo.html'
OUT = _ROOT / 'docs' / 'reference' / 'parity'
DESKTOP = {'width': 1440, 'height': 1000}
PHONE = {'width': 390, 'height': 844}

#: slug, the demo's nav page (and the row to open), the live path
SHOTS = (
    ('account-check', 'accounts', '/accounts/check'),
    ('new-accounts', 'accounts', '/accounts/check#owners'),
    ('chain-board', 'rfqs', '/rfqs/chain'),
    ('quote-mail-queue', 'rfqs', '/rfqs/chain#unmatched'),
    ('lead-record', 'lead:journey', '/leads/{lead}/journey'),
    ('lead-steps', 'lead:steps', '/leads/{lead}/journey#steps'),
    ('lead-activity', 'lead:activity', '/leads/{lead}/journey#activity'),
    ('conditions', 'lead:conds', '/leads/{lead}/journey#conditions'),
    ('quotation-versions', 'lead:quotes',
     '/leads/{lead}/journey#quotes'),
    ('spend', 'lead:spend', '/leads/{lead}/journey#spend'),
    ('audit-trail', 'lead:audit', '/leads/{lead}/journey#audit'),
    ('department-steps', 'changes', '/admin/rfq-steps'),
    ('reports', 'reports', '/rfqs/reports'),
    # Last, and signed in as the salesperson: the chain bucket
    # exists for whoever holds a step and the admin holds none,
    # and a sign-in per shot trips the portal's login throttle.
    ('waiting-on-me', 'mywork', '/my-work/tasks', 'PAR_SALES'),
)


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def serve():
    """Seed a throwaway world and serve it. Returns (base, ids)."""
    # By file, not by path: putting scripts/ on sys.path
    # shadows the email_ingest package with scripts/email_ingest.py
    # and app.py stops importing.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'parity_world', _ROOT / 'scripts' / 'parity_world.py')
    world = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(world)

    world.fresh_database()
    import importlib
    main = importlib.import_module('app')
    importlib.import_module('app.models')
    importlib.import_module('app.models.rfq')
    importlib.import_module('app.models.quote')
    flask_app = main.app
    with flask_app.app_context():
        ids = world.build(main)

    port = free_port()
    thread = threading.Thread(
        target=lambda: flask_app.run(host='127.0.0.1', port=port,
                                     debug=False, use_reloader=False),
        daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{port}'
    for _ in range(60):
        try:
            import urllib.request
            urllib.request.urlopen(base + '/login', timeout=1)
            break
        except Exception:                                 # noqa: BLE001
            time.sleep(0.25)
    return base, ids, world


def sign_in(page, base, world, who=None):
    """Sign this page in, and say whether it worked.

    Signing in before every shot tripped the portal's own
    throttle part-way through the run and every later page was
    photographed as the login screen. The caller signs in only
    when the person it needs changes; this refuses quietly rather
    than leaving a silent login page in the folder.
    """
    page.goto(base + '/login')
    status = page.evaluate(
        """async ([code, pw]) => {
            const r = await fetch('/login', {method:'POST',
              headers:{'Content-Type':'application/json'},
              body: JSON.stringify({emp_code: code, password: pw})});
            return r.status;
        }""", [who or world.ADMIN, world.PASSWORD])
    if status != 200:
        raise SystemExit(
            f'sign-in for {who or world.ADMIN} answered {status} — '
            f'the shots would all be of the login page')
    return who or world.ADMIN


def shoot_live(page, base, path, out):
    page.goto(base + path.split('#')[0],
              wait_until='domcontentloaded')
    page.wait_for_timeout(1200)
    anchor = path.split('#')[1] if '#' in path else ''
    if anchor:
        page.evaluate(
            """a => {
                const t = document.querySelector('[data-ptab="'+a+'"]')
                  || document.querySelector('[data-k="'+a+'"]')
                  || document.getElementById(a);
                if (t) t.click ? t.click() : t.scrollIntoView();
            }""", anchor)
    page.wait_for_timeout(900)
    page.screenshot(path=str(out), full_page=True)


def shoot_demo(page, which, out):
    page.goto(DEMO.as_uri())
    page.wait_for_timeout(400)
    # The demo opens as a salesperson, who cannot see Reports or
    # the leads@ queue. Every card has to be in the photograph.
    page.evaluate("""() => {
        const v = document.getElementById('viewer');
        v.value = 'nilesh';
        v.dispatchEvent(new Event('change'));
    }""")
    page.wait_for_timeout(300)
    if which.startswith('lead:'):
        tab = which.split(':')[1]
        page.evaluate("""() => {
            document.querySelector('[data-go="rfqs"]').click();
        }""")
        page.wait_for_timeout(250)
        page.evaluate("""() => {
            const r = document.querySelector('tr.row[data-open]');
            if (r) r.click();
        }""")
        page.wait_for_timeout(250)
        page.evaluate("""t => {
            const b = document.querySelector('[data-ptab="'+t+'"]');
            if (b) b.click();
        }""", tab)
    else:
        page.evaluate("""p => {
            const b = document.querySelector('[data-go="'+p+'"]');
            if (b) b.click();
        }""", which)
    page.wait_for_timeout(500)
    page.screenshot(path=str(out), full_page=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--stage', default='before',
                    choices=('before', 'after'))
    ap.add_argument('--only', default='',
                    help='one slug, for a single re-take')
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright

    OUT.mkdir(parents=True, exist_ok=True)
    base, ids, world = serve()
    lead = ids['leads'][-1]
    wanted = [s for s in SHOTS
              if not args.only or s[0] == args.only]

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for name, size in (('desktop', DESKTOP), ('phone', PHONE)):
            page = browser.new_page(viewport=size)
            signed = sign_in(page, base, world)
            for shot in wanted:
                slug, demo_page, path = shot[0], shot[1], shot[2]
                # Only when the person changes: signing in for
                # every shot trips the portal's login throttle
                # part-way through, and every page after it is
                # photographed as the login screen.
                wants = (shot[3] if len(shot) > 3
                         else world.ADMIN)
                if wants != signed:
                    signed = sign_in(page, base, world, wants)
                live = OUT / f'{slug}--live-{name}-{args.stage}.png'
                shoot_live(page, base, path.format(lead=lead), live)
                print('live ', live.name)
            page.close()
            demo = browser.new_page(viewport=size)
            for shot in wanted:
                slug, demo_page = shot[0], shot[1]
                shot = OUT / f'{slug}--demo-{name}.png'
                if shot.exists() and args.stage == 'after':
                    continue
                shoot_demo(demo, demo_page, shot)
                print('demo ', shot.name)
            demo.close()
        browser.close()
    print('\nleads seeded:', ids['leads'])
    print('shots in', OUT)


if __name__ == '__main__':
    main()
