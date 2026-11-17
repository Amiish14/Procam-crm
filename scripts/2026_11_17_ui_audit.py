"""What the CRM looks like at five widths, and what is wrong with it.

CRM-UI-01 Stage 0. READ-ONLY: it starts a throwaway copy of the CRM
on a temporary database, drives it with Chromium, and writes pictures
and a table. It never touches the live database and never writes to
the repository outside the output folder.

    .venv/bin/python scripts/2026_11_17_ui_audit.py
    .venv/bin/python scripts/2026_11_17_ui_audit.py --out /tmp/ui_17nov
    .venv/bin/python scripts/2026_11_17_ui_audit.py --only leads,scan

WHAT IT MEASURES, AND WHY EACH ONE IS A FAULT AND NOT A TASTE

  * **horizontal scroll** — `scrollWidth > innerWidth + 1`. On a phone
    it means part of every row is unreachable; the user cannot know
    what they are not seeing.
  * **overlap of fixed things** — the bounding boxes of everything
    `position: fixed` or `sticky`, intersected. A chat button over the
    bottom bar is not a cosmetic problem: it eats the tap.
  * **tap targets under 44px** — the figure both platform guidelines
    use. Measured on what is actually clickable and actually visible.
  * **text under 14px on a phone** — computed, not declared, so
    inherited sizes count.
  * **a modal wider or taller than the viewport** — a dialog whose
    buttons are off-screen cannot be dismissed, which is the worst
    failure on this list: the page is stuck.

Each finding carries the element's own selector, so Stage 1 onwards
can be checked against this file rather than against an opinion.

THE SEEDED DATA IS INVENTED

Lists have to have rows in them or the audit measures empty states.
Everything seeded here is made up, prefixed ZU, and lives in a
temporary database that is deleted with the run.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import tempfile
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

#: The five widths asked for, with the names used in the report.
WIDTHS = ((360, 800, 'phone-360'), (390, 844, 'phone-390'),
          (768, 1024, 'tablet-768'), (1366, 768, 'laptop-1366'),
          (1920, 1080, 'desktop-1920'))

#: 44px on both platforms' own guidance.
TAP_MIN = 44

#: Under this on a phone, text is being read by people who cannot.
TEXT_MIN = 14

ADMIN = 'ZU_ADM'
PASSWORD = 'UiAudit-123456789'


def _free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


# ── the throwaway CRM ───────────────────────────────────────────────
def boot(port, home):
    """A copy of the CRM on a temporary database, in a thread."""
    os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(home, 'ui.db')
    os.environ['UPLOAD_ROOT'] = os.path.join(home, 'uploads')
    os.environ['SESSION_COOKIE_SECURE'] = 'false'
    os.environ.setdefault('SECRET_KEY', 'ui-audit-only')
    os.environ['ADMIN_INITIAL_PASSWORD'] = PASSWORD
    os.environ['URL_PREFIX'] = ''

    import app as _app

    _app.app.config['WTF_CSRF_ENABLED'] = False
    _app.app.config['UPLOAD_ROOT'] = os.environ['UPLOAD_ROOT']
    with _app.app.app_context():
        _app.db.create_all()
        seed(_app)
    thread = threading.Thread(
        target=lambda: _app.app.run(port=port, threaded=True,
                                    use_reloader=False),
        daemon=True)
    thread.start()
    for _ in range(100):
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.3):
                return _app
        except OSError:
            time.sleep(0.1)
    raise RuntimeError('the CRM did not start')


def seed(_app):
    """Enough invented rows that no list is empty."""
    from werkzeug.security import generate_password_hash

    db = _app.db
    emp = _app.Employee.query.filter_by(emp_code=ADMIN).first()
    if emp is None:
        emp = _app.Employee(emp_code=ADMIN, name='UI Audit',
                            email='ui.audit@example.com')
        db.session.add(emp)
    emp.password_hash = generate_password_hash(PASSWORD)
    emp.role, emp.is_active, emp.must_change_pw = 'admin', True, False
    emp.vertical, emp.is_super_admin = 'All', True
    db.session.commit()

    if _app.Company.query.filter(_app.Company.name.like('ZU%')).count():
        return
    accounts = []
    for n, (name, city, state) in enumerate((
            ('ZU Zenith Steel Works Limited', 'Mumbai', 'Maharashtra'),
            ('ZU Konkan Heavy Engineering', 'Chennai', 'Tamil Nadu'),
            ('ZU Saurashtra Cement Corporation', 'Rajkot', 'Gujarat'),
            ('ZU Vindhya Power Systems', 'Bengaluru', 'Karnataka'),
            ('ZU Malabar Chemicals', 'Kochi', 'Kerala'))):
        account = _app.Company(name=name, city=city, state=state,
                               country='India', is_active=True,
                               industry='Steel' if n % 2 else 'Cement')
        db.session.add(account)
        accounts.append(account)
    db.session.commit()

    for n in range(24):
        account = accounts[n % len(accounts)]
        db.session.add(_app.Lead(
            company=account.name, company_id=account.id,
            project=f'ZU Movement of a {140 + n}MT transformer, '
                    f'Hazira to Jamnagar',
            pic=f'ZU Person {n}', email=f'zu{n}@example.com',
            phone=f'+9198200{n:05d}', city=account.city,
            state=account.state, country='India',
            procam_vertical=('Heavy Transport' if n % 3 else
                             'Project Freight'),
            stage=('New Opportunity' if n % 4 else 'Quote Sent'),
            assigned_to=ADMIN))
    for n in range(12):
        account = accounts[n % len(accounts)]
        db.session.add(_app.Contact(
            name=f'ZU Contact {n}', company=account.name,
            account_id=account.id, company_id=account.id,
            designation='Senior Manager - Logistics',
            email=f'zu.contact{n}@example.com',
            mobile=f'+9198400{n:05d}', city=account.city,
            state=account.state, country='India',
            industry='Steel', assigned_to=ADMIN))
    db.session.commit()


# ── what each shot is ───────────────────────────────────────────────
#: (key, label, how to get there). `go` is a URL; `then` is JavaScript
#: run once the page is up, for the tabs and modals that live inside
#: the one-page app.
SHOTS = (
    {'key': 'login', 'label': 'Login', 'go': '/login', 'anon': True},
    {'key': 'home', 'label': 'App home (dashboard)', 'go': '/app'},
    {'key': 'leads', 'label': 'Leads list', 'go': '/app',
     'then': "nav('leads')", 'wait': 1200},
    {'key': 'lead-detail', 'label': 'Lead detail', 'go': '/app',
     'then': ("nav('leads'); await new Promise(r=>setTimeout(r,900));"
              "const first=document.querySelector("
              "'[onclick*=openLd]');"
              "if(first) first.click();"), 'wait': 1500},
    {'key': 'pipeline', 'label': 'Pipeline', 'go': '/app',
     'then': "nav('pipeline')", 'wait': 1200},
    {'key': 'accounts', 'label': 'Accounts', 'go': '/app',
     'then': "nav('all')", 'wait': 1200},
    {'key': 'contacts', 'label': 'Global CRM (contacts)', 'go': '/app',
     'then': "nav('contacts')", 'wait': 1200},
    {'key': 'find-person', 'label': 'Find a person',
     'go': '/contacts/search?name=ZU', 'wait': 1200},
    {'key': 'scan', 'label': 'Scan business card',
     'go': '/business-cards/scan'},
    {'key': 'admin-master', 'label': 'Admin — master data', 'go': '/app',
     'then': "nav('master')", 'wait': 1200},
    {'key': 'admin-access', 'label': 'Admin — access control',
     'go': '/app', 'then': "nav('access')", 'wait': 1200},
    {'key': 'modal-new-lead', 'label': 'Modal — new lead', 'go': '/app',
     'then': "nav('leads'); await new Promise(r=>setTimeout(r,600));"
             "newLead();", 'wait': 1200, 'modal': '#ldMob'},
    {'key': 'modal-new-contact', 'label': 'Modal — new contact',
     'go': '/app', 'then': "nav('contacts'); "
                           "await new Promise(r=>setTimeout(r,600));"
                           "openCt();", 'wait': 1000, 'modal': '#ctMob'},
    {'key': 'modal-duplicate', 'label': 'Modal — duplicate warning',
     'go': '/app',
     'then': "nav('contacts'); await new Promise(r=>setTimeout(r,600));"
             "openCt(); document.getElementById('ctN').value="
             "'ZU Contact 1'; document.getElementById('ctE').value="
             "'zu.contact1@example.com'; await saveCt();",
     'wait': 1600, 'modal': '#ctMob'},
)

#: Run in the page. Returns the findings for this width, each with the
#: element that carries it, so a fix can be checked rather than argued.
MEASURE = r"""
() => {
  const out = [];
  const vw = window.innerWidth, vh = window.innerHeight;
  const sel = (el) => {
    if (!el || !el.tagName) return '?';
    if (el.id) return '#' + el.id;
    const cls = (el.className && String(el.className).trim()
      .split(/\s+/).slice(0, 2).join('.')) || '';
    return el.tagName.toLowerCase() + (cls ? '.' + cls : '');
  };
  const seen = (el) => {
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden'
        || parseFloat(s.opacity || '1') < 0.05) return false;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    // A closed drawer is translated off to the left and its items are
    // not tappable. Measuring them would fill the report with
    // findings nobody can reach — so anything wholly outside the
    // viewport sideways is not on screen.
    if (r.right <= 0 || r.left >= vw) return false;
    return r.bottom > 0 && r.top < vh * 3;
  };

  // 1. horizontal scroll
  const doc = document.documentElement;
  if (doc.scrollWidth > vw + 1) {
    let worst = null, over = 0;
    document.querySelectorAll('*').forEach(el => {
      if (!seen(el)) return;
      const r = el.getBoundingClientRect();
      if (r.right - vw > over) { over = r.right - vw; worst = el; }
    });
    out.push({kind: 'horizontal scroll',
              detail: doc.scrollWidth + 'px of content in ' + vw + 'px'
                      + (worst ? ', widest past the edge: ' + sel(worst)
                         + ' (+' + Math.round(over) + 'px)' : ''),
              where: worst ? sel(worst) : 'document', weight: 100});
  }

  // 2. fixed and sticky things that overlap each other
  const stuck = [];
  document.querySelectorAll('*').forEach(el => {
    const s = getComputedStyle(el);
    if ((s.position === 'fixed' || s.position === 'sticky') && seen(el)) {
      const r = el.getBoundingClientRect();
      if (r.width >= 8 && r.height >= 8) stuck.push([el, r]);
    }
  });
  // An open dialog covers the page ON PURPOSE. Counting everything
  // underneath it as an overlap buries the one that matters — a chat
  // button sitting on the bottom bar, which eats the tap.
  const overlay = (el) => el.classList && (el.classList.contains('mob')
    || el.id === 'crmFormModal' || el.getAttribute('role') === 'dialog');
  const covers = (ra, rb) => ra.left <= rb.left + 1 && ra.top <= rb.top + 1
    && ra.right >= rb.right - 1 && ra.bottom >= rb.bottom - 1;
  for (let i = 0; i < stuck.length; i++) {
    for (let j = i + 1; j < stuck.length; j++) {
      const [a, ra] = stuck[i], [b, rb] = stuck[j];
      if (a.contains(b) || b.contains(a)) continue;
      // An open dialog is on top of the page by definition, and a
      // sticky header under it is not fighting it. Anything paired
      // WITH an overlay is skipped; what matters is two things in
      // the same layer, which is where the chat button and the bar
      // were.
      if (overlay(a) || overlay(b)) continue;
      const w = Math.min(ra.right, rb.right) - Math.max(ra.left, rb.left);
      const h = Math.min(ra.bottom, rb.bottom) - Math.max(ra.top, rb.top);
      if (w > 4 && h > 4) {
        out.push({kind: 'fixed elements overlap',
                  detail: sel(a) + ' over ' + sel(b) + ' by '
                          + Math.round(w) + '×' + Math.round(h) + 'px',
                  where: sel(a) + ' / ' + sel(b), weight: 90});
      }
    }
  }

  // 3. tap targets under 44px
  const tappable = 'a, button, [role=button], input[type=checkbox],'
    + ' input[type=radio], select, .tab, .sbi, .fab, .mx';
  const small = {};
  document.querySelectorAll(tappable).forEach(el => {
    if (!seen(el)) return;
    const r = el.getBoundingClientRect();
    if (r.top > vh || r.bottom < 0) return;        // not on this screen
    const w = Math.round(r.width), h = Math.round(r.height);
    if (Math.min(w, h) >= 44) return;
    const key = sel(el) + '|' + w + '×' + h;
    small[key] = (small[key] || 0) + 1;
  });
  Object.keys(small).forEach(key => {
    const [where, size] = key.split('|');
    out.push({kind: 'tap target under 44px',
              detail: size + 'px' + (small[key] > 1
                      ? ' (' + small[key] + ' of them)' : ''),
              where: where, weight: 55});
  });

  // 4. text under 14px — phones only, decided by the caller
  if (window.__PHONE__) {
    const tiny = {};
    document.querySelectorAll('body *').forEach(el => {
      if (!seen(el)) return;
      const own = Array.from(el.childNodes).some(
        n => n.nodeType === 3 && n.textContent.trim().length > 1);
      if (!own) return;
      const size = parseFloat(getComputedStyle(el).fontSize || '16');
      if (size >= 14) return;
      const key = sel(el) + '|' + size.toFixed(1);
      tiny[key] = (tiny[key] || 0) + 1;
    });
    Object.keys(tiny).forEach(key => {
      const [where, size] = key.split('|');
      out.push({kind: 'text under 14px',
                detail: size + 'px' + (tiny[key] > 1
                        ? ' (' + tiny[key] + ' elements)' : ''),
                where: where, weight: 45});
    });
  }

  // 5. a dialog bigger than the screen it is on
  document.querySelectorAll('.mob, .mod, dialog, [role=dialog]')
    .forEach(el => {
      if (!seen(el)) return;
      const r = el.getBoundingClientRect();
      if (r.width > vw + 1 || r.height > vh + 1) {
        out.push({kind: 'modal larger than the viewport',
                  detail: Math.round(r.width) + '×' + Math.round(r.height)
                          + ' in ' + vw + '×' + vh,
                  where: sel(el), weight: 95});
      }
      if (r.bottom > vh + 1 && getComputedStyle(el).overflowY !== 'auto'
          && getComputedStyle(el).overflowY !== 'scroll') {
        out.push({kind: 'modal runs off the bottom',
                  detail: Math.round(r.bottom - vh)
                          + 'px below the fold, and it does not scroll',
                  where: sel(el), weight: 92});
      }
    });
  return out;
}
"""


def run(out_dir, only=None):
    from playwright.sync_api import sync_playwright

    home = tempfile.mkdtemp(prefix='ui-audit-')
    port = _free_port()
    boot(port, home)
    base = f'http://127.0.0.1:{port}'
    os.makedirs(out_dir, exist_ok=True)

    wanted = [s for s in SHOTS
              if not only or s['key'] in only]
    findings, shots = [], {}

    with sync_playwright() as play:
        browser = play.chromium.launch()
        for width, height, name in WIDTHS:
            phone = width < 768
            context = browser.new_context(
                viewport={'width': width, 'height': height},
                device_scale_factor=1, is_mobile=phone,
                has_touch=phone)
            page = context.new_page()
            page.add_init_script(
                f'window.__PHONE__ = {"true" if phone else "false"};')
            # The app defaults to the mailbox scope, which on a
            # seeded database matches nothing — every list measured
            # EMPTY, and an empty list hides exactly the faults this
            # is looking for (the card view, the density, the
            # overflow). Widened in the page, after the app's own
            # `CRM` object exists.
            page.add_init_script(
                'window.addEventListener("DOMContentLoaded", () => {'
                ' try { CRM.dataScope = "all"; } catch (e) {} });')
            # Sign in once per width.
            page.goto(f'{base}/login', wait_until='domcontentloaded')
            page.evaluate(
                """async ({code, password}) => {
                    await fetch('/login', {method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({emp_code: code,
                                              password: password})});
                }""", {'code': ADMIN, 'password': PASSWORD})

            for shot in wanted:
                key = shot['key']
                try:
                    if shot.get('anon'):
                        page.context.clear_cookies()
                    page.goto(base + shot['go'],
                              wait_until='domcontentloaded')
                    page.wait_for_timeout(700)
                    page.evaluate('() => { try { CRM.dataScope = "all"; }'
                                  ' catch (e) {} }')
                    if shot.get('then'):
                        page.evaluate('async () => { %s }' % shot['then'])
                    page.wait_for_timeout(shot.get('wait', 400))
                    found = page.evaluate(MEASURE)
                    path = os.path.join(out_dir, f'{key}__{name}.png')
                    page.screenshot(path=path, full_page=False)
                    shots.setdefault(key, {})[name] = path
                    for item in found:
                        item.update({'page': shot['label'], 'key': key,
                                     'width': name})
                        findings.append(item)
                    if shot.get('anon'):
                        page.goto(f'{base}/login',
                                  wait_until='domcontentloaded')
                        page.evaluate(
                            """async ({code, password}) => {
                                await fetch('/login', {method: 'POST',
                                  headers: {'Content-Type':
                                            'application/json'},
                                  body: JSON.stringify({emp_code: code,
                                        password: password})});
                            }""", {'code': ADMIN, 'password': PASSWORD})
                except Exception as exc:                  # noqa: BLE001
                    findings.append({
                        'kind': 'the page could not be measured',
                        'detail': f'{type(exc).__name__}: {str(exc)[:160]}',
                        'where': shot['go'], 'weight': 10,
                        'page': shot['label'], 'key': key,
                        'width': name})
            context.close()
        browser.close()
    return findings, shots


# ── the report ──────────────────────────────────────────────────────
def report(findings):
    from collections import defaultdict

    by_page = defaultdict(lambda: defaultdict(list))
    for item in findings:
        by_page[item['page']][item['width']].append(item)

    def worst(page):
        return -max((i['weight'] for w in by_page[page].values()
                     for i in w), default=0), page

    print(f'\n══ {len(findings)} finding(s), worst page first ══\n')
    head = f'{"page":<26} {"width":<13} {"what":<34} where'
    print(head)
    print('─' * 110)
    for page in sorted(by_page, key=worst):
        for _width, _height, name in WIDTHS:
            items = sorted(by_page[page].get(name, []),
                           key=lambda i: -i['weight'])
            for n, item in enumerate(items):
                print(f'{page[:25]:<26} {name:<13} '
                      f'{item["kind"][:33]:<34} {item["where"][:36]}'
                      if n == 0 else
                      f'{"":<26} {"":<13} {item["kind"][:33]:<34} '
                      f'{item["where"][:36]}')
                if item['detail']:
                    print(f'{"":<26} {"":<13}   {item["detail"][:70]}')
    return by_page


def counts(findings):
    from collections import Counter

    print('\n══ by kind ══')
    for kind, n in Counter(i['kind'] for i in findings).most_common():
        print(f'  {n:>4}  {kind}')
    print('\n══ by width ══')
    for _w, _h, name in WIDTHS:
        n = sum(1 for i in findings if i['width'] == name)
        print(f'  {n:>4}  {name}')


def contact_sheet(shots, out_dir):
    """One image per page, the five widths side by side.

    Scaled to a common height so the widths are comparable at a
    glance, which is the whole point of a contact sheet: the eye
    finds the broken one before the table does.
    """
    from PIL import Image, ImageDraw

    made = []
    height = 900
    for key, widths in shots.items():
        tiles = []
        for _w, _h, name in WIDTHS:
            path = widths.get(name)
            if not path or not os.path.isfile(path):
                continue
            img = Image.open(path)
            scale = height / float(img.size[1])
            tile = img.resize((max(1, int(img.size[0] * scale)), height))
            tiles.append((name, tile))
        if not tiles:
            continue
        pad, label = 12, 26
        total = sum(t.size[0] for _n, t in tiles) + pad * (len(tiles) + 1)
        sheet = Image.new('RGB', (total, height + label + pad * 2),
                          (245, 245, 247))
        pen = ImageDraw.Draw(sheet)
        x = pad
        for name, tile in tiles:
            sheet.paste(tile, (x, label + pad))
            pen.text((x, pad), f'{name}  ({tile.size[0]}px shown)',
                     fill=(40, 40, 45))
            pen.rectangle([x, label + pad, x + tile.size[0] - 1,
                           label + pad + height - 1],
                          outline=(200, 200, 205))
            x += tile.size[0] + pad
        # JPEG, because these are committed for the before/after
        # comparison and a stage that adds 13MB of PNG to the
        # repository every time it runs is a stage nobody re-runs.
        path = os.path.join(out_dir, f'sheet__{key}.jpg')
        sheet.save(path, quality=78, optimize=True)
        made.append(path)
    return made


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--out', default=os.path.join(
        _ROOT, 'docs', 'ui_audit', '2026_11_17_before'), metavar='DIR')
    ap.add_argument('--only', default='', metavar='KEYS')
    ap.add_argument('--no-sheet', action='store_true')
    args = ap.parse_args(argv)

    only = {k.strip() for k in args.only.split(',') if k.strip()}
    out_dir = os.path.abspath(args.out)
    print('CRM-UI-01 Stage 0 — read-only audit.')
    print(f'  widths: {", ".join(n for _w, _h, n in WIDTHS)}')
    print(f'  output: {out_dir}')

    findings, shots = run(out_dir, only or None)
    report(findings)
    counts(findings)

    with open(os.path.join(out_dir, 'findings.json'), 'w') as fh:
        json.dump(findings, fh, indent=1)
    print(f'\nfindings: {os.path.join(out_dir, "findings.json")}')
    if not args.no_sheet:
        for path in contact_sheet(shots, out_dir):
            print(f'sheet: {path}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
