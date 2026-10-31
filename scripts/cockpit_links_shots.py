"""
Photographs of the links CRM-COCKPIT-LINKS-01 adds.

The pages existed and nothing led to them, so the evidence that this
is fixed is a picture of each way in: the Cockpit as the landing
page, the menu it is first in, the Journey button and stage line on
the lead pop-up, the journey icon on a list row, and the chain board
and RFQ reports in the menu.

A throwaway database and Playwright. Nothing touches the live
system.

    .venv/bin/python scripts/cockpit_links_shots.py

Playwright is in the virtualenv and deliberately not in
requirements.txt: the suite must not need a browser.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(_ROOT, 'docs', 'reference', 'cockpit_links')
WIDTHS = (('desktop', 1280, 900), ('phone', 390, 844))

ADMIN = 'CKS_ADMIN'
VERTICAL = 'Heavy Transport'

#: Everything these links depend on, on for everybody.
FLAGS = ('FEATURE_CRM2', 'FEATURE_LEAD_JOURNEY', 'FEATURE_RFQ_CHAIN',
         'FEATURE_RFQ_REPORTS', 'FEATURE_PRE_RFQ_FUNNEL')


def _world():
    from app import Company, Employee, Lead, app, db
    from app.models.integration import AppSetting
    from app.services import flags

    with app.app_context():
        db.create_all()
        db.session.add(Employee(
            emp_code=ADMIN, name='James Francis Xavier',
            password_hash='x', role='admin', vertical=VERTICAL,
            is_active=True, must_change_pw=False, is_super_admin=True,
            email='shots@procamlogistics.com'))
        # Without a profile the scope is empty and every list reads
        # "0 leads", which photographs as a working screen with
        # nothing on it.
        from app.models.access import AccessProfile, DataScope
        db.session.add(AccessProfile(emp_code=ADMIN,
                                     data_scope=DataScope.ALL,
                                     perms=[]))
        for name in FLAGS:
            db.session.add(AppSetting(key='flag.' + name,
                                      value_json=json.dumps(True)))
        account = Company(name='HITACHI ENERGY INDIA LIMITED',
                          is_active=True, pic_emp_code=ADMIN,
                          vertical=VERTICAL)
        db.session.add(account)
        db.session.flush()

        made = []
        for stage, days in (('New Opportunity', 14),
                            ('RFQ Generated', 41),
                            ('Quoted', 3)):
            lead = Lead(company=account.name, company_id=account.id,
                        stage=stage, source='email', assigned_to=ADMIN,
                        # The lead lists are pinned to the mailbox
                        # scope (CRM.dataScope), which wants both a
                        # source of 'email' and a message id.
                        email_message_id=f'<shot-{stage}@example>',
                        assigned_name='James Francis Xavier',
                        procam_vertical=VERTICAL, pic='R Venkatesh',
                        designation_pic='Logistics Manager',
                        email='rfq@hitachienergy.example',
                        project='2 x 40 MVA transformers',
                        origin='Vadodara', destination='Lapanga',
                        opportunity_value_num=2500000,
                        estimated_value_inr=2500000,
                        value_basis='Client budget',
                        followup_date=date.today() + timedelta(days=3),
                        stage_entered_at=datetime.utcnow()
                        - timedelta(days=days),
                        created_at=datetime.utcnow() - timedelta(days=60))
            db.session.add(lead)
            db.session.flush()
            made.append((stage, lead.id))
        db.session.commit()
        flags.cache_clear()
        return made


def main():
    tmp = tempfile.mkdtemp()
    os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(tmp,
                                                             'cks.db')
    os.environ.setdefault('SECRET_KEY', 'cks')
    os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'ShotsOnly-123456')
    os.environ['URL_PREFIX'] = ''
    sys.path.insert(0, _ROOT)

    made = _world()
    os.makedirs(OUT, exist_ok=True)

    from threading import Thread

    from flask import session as _session
    from werkzeug.serving import make_server

    from app import app as flask_app

    def _shotlogin():
        _session['emp_code'] = ADMIN
        _session['role'] = 'admin'
        _session['must_change_pw'] = False
        return 'ok'

    flask_app.add_url_rule('/_shotlogin', '_shotlogin', _shotlogin)
    server = make_server('127.0.0.1', 5098, flask_app)
    Thread(target=server.serve_forever, daemon=True).start()

    from playwright.sync_api import sync_playwright
    base = 'http://127.0.0.1:5098'
    lead_id = made[1][1]
    written = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for label, width, height in WIDTHS:
            ctx = browser.new_context(
                viewport={'width': width, 'height': height})
            page = ctx.new_page()
            page.goto(base + '/_shotlogin', wait_until='domcontentloaded')

            def shot(name, *, full=True):
                path = os.path.join(OUT, f'{name}_{label}.png')
                page.screenshot(path=path, full_page=full)
                written.append(path)

            # 1. the landing page is the cockpit
            page.goto(base + '/', wait_until='domcontentloaded')
            page.wait_for_timeout(700)
            print(f'  {label}: / lands on {page.url.replace(base, "")}')
            shot('01_landing_cockpit')

            # 2. the menu, with the cockpit first and the RFQ pages in it
            page.goto(base + '/app', wait_until='domcontentloaded')
            page.wait_for_timeout(900)
            shot('02_menu', full=False)
            # The topnav is desktop-only — at phone width the same
            # groups live behind the hamburger, so there is no
            # dropdown to open and nothing to photograph.
            if label == 'desktop':
                page.click('.topnav-group .topnav-head', timeout=4000)
                page.wait_for_timeout(300)
                shot('03_menu_open', full=False)

            # 3. the lead pop-up: Journey button and the stage line
            page.goto(base + f'/app?lead={lead_id}',
                      wait_until='domcontentloaded')
            page.wait_for_timeout(1600)
            shot('04_popup_journey')

            # 4. a list row's journey icon
            page.goto(base + '/app', wait_until='domcontentloaded')
            page.wait_for_timeout(900)
            try:
                page.evaluate("nav('all')")
                # Wait for the rows, not for a guessed number of
                # milliseconds: the list fetches, and photographing
                # the empty state is how this harness lies.
                try:
                    page.wait_for_selector('#aTb .jny-ic', timeout=8000)
                except Exception:                  # noqa: BLE001
                    page.wait_for_timeout(1500)
                shot('05_list_row_icon')
                has = page.evaluate(
                    "document.querySelectorAll('.jny-ic').length")
                seen = page.evaluate(
                    "fetch('/api/leads?limit=300')"
                    ".then(r=>r.json()).then(d=>d.length)")
                print(f'  {label}: journey icons on the list: {has} '
                      f'(api says {seen} leads, '
                      f'links={page.evaluate("JSON.stringify(window.CRM_LINKS)")})')
            except Exception as exc:               # noqa: BLE001
                print(f'  {label}: list not reached ({exc})')

            # 5. the pages themselves
            for name, path in (
                    ('06_journey', f'/leads/{lead_id}/journey'),
                    ('07_rfq_chain', '/rfqs/chain'),
                    ('08_rfq_reports', '/rfqs/reports')):
                page.goto(base + path, wait_until='domcontentloaded')
                page.wait_for_timeout(900)
                shot(name)

            wide = page.evaluate('document.documentElement.scrollWidth')
            print(f'  {label}: scrollWidth {wide} at {width}px')
            ctx.close()
        browser.close()
    server.shutdown()
    for path in written:
        print('wrote', os.path.relpath(path, _ROOT))
    print(f'\n{len(written)} picture(s) in '
          f'{os.path.relpath(OUT, _ROOT)}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
