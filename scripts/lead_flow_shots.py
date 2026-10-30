"""
Photographs of the new lead screen, at both widths.

CRM-LEAD-FLOW-01 §3. A throwaway database, a seeded lead at each
stage, and Playwright. Nothing touches the live system and the
pictures are the evidence that the screen holds together at 1280px
and at 390px — where the old pop-up's 25 fields became a scroll.

    .venv/bin/python scripts/lead_flow_shots.py

Playwright is installed in the virtualenv and deliberately not in
requirements.txt: the suite must not need a browser.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(_ROOT, 'docs', 'reference', 'lead_flow')
WIDTHS = (('desktop', 1280, 900), ('phone', 390, 844))

ADMIN = 'SHOT_ADMIN'
VERTICAL = 'Heavy Transport'


def _world():
    """A lead at each stage, in a database that is thrown away."""
    from app import (Company, Employee, Lead, LeadEmail, app, db)
    from app.models.integration import AppSetting
    from app.services import flags

    with app.app_context():
        db.create_all()
        db.session.add(Employee(
            emp_code=ADMIN, name='James Francis Xavier',
            password_hash='x', role='admin', vertical=VERTICAL,
            is_active=True, must_change_pw=False,
            email='shot@procamlogistics.com'))
        for name in ('FEATURE_LEAD_POPUP_V2', 'FEATURE_GUIDED_HELP'):
            db.session.add(AppSetting(key='flag.' + name,
                                      value_json=json.dumps(True)))
        account = Company(name='HITACHI ENERGY INDIA LIMITED',
                          is_active=True, pic_emp_code=ADMIN,
                          vertical=VERTICAL)
        db.session.add(account)
        db.session.flush()

        made = []
        for stage, extra in (
                ('New', {}),
                ('Business Discussion', {'sub_status': 'Visit done'}),
                ('RFQ Generated', {}),
                ('Quoted', {'quote_no': 'PWL/Q/2026/0412',
                            'quote_value_num': 2450000,
                            'quote_date': date.today(),
                            'quote_validity_date':
                                date.today() + timedelta(days=30)}),
        ):
            lead = Lead(company=account.name, company_id=account.id,
                        stage=stage, source='email',
                        assigned_to=ADMIN,
                        assigned_name='James Francis Xavier',
                        procam_vertical=VERTICAL,
                        pic='R Venkatesh',
                        designation_pic='Logistics Manager',
                        email='rfq@hitachienergy.example',
                        phone='+91 98200 00000',
                        project='2 x 40 MVA transformers',
                        origin='Vadodara', destination='Lapanga',
                        opportunity_value_num=2500000,
                        value_basis='Client budget',
                        followup_date=date.today() + timedelta(days=3),
                        created_at=datetime.utcnow(), **extra)
            db.session.add(lead)
            db.session.flush()
            db.session.add(LeadEmail(
                lead_id=lead.id, direction='in',
                from_addr='rfq@hitachienergy.example',
                subject='RFQ — 2 x 40 MVA transformers',
                sent_or_received_at=datetime.utcnow()))
            made.append((stage, lead.id))
        db.session.commit()
        flags.cache_clear()
        return made


def main():
    tmp = tempfile.mkdtemp()
    os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(tmp,
                                                             'shots.db')
    os.environ.setdefault('SECRET_KEY', 'shots')
    os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'ShotsOnly-123456')
    os.environ['URL_PREFIX'] = ''
    sys.path.insert(0, _ROOT)

    made = _world()
    os.makedirs(OUT, exist_ok=True)

    from threading import Thread

    from flask import session as _session
    from werkzeug.serving import make_server

    from app import app as flask_app

    # A sign-in route that exists only in this process. The real
    # login throttles, and photographing the throttle page instead
    # of the screen is a mistake this harness has made before.
    def _shotlogin():
        _session['emp_code'] = ADMIN
        _session['role'] = 'admin'
        return 'ok'

    flask_app.add_url_rule('/_shotlogin', '_shotlogin', _shotlogin)
    server = make_server('127.0.0.1', 5099, flask_app)
    Thread(target=server.serve_forever, daemon=True).start()

    from playwright.sync_api import sync_playwright
    base = 'http://127.0.0.1:5099'
    written = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for label, width, height in WIDTHS:
            ctx = browser.new_context(
                viewport={'width': width, 'height': height})
            page = ctx.new_page()
            page.goto(base + '/_shotlogin',
                      wait_until='domcontentloaded')
            for stage, lead_id in made:
                page.goto(f'{base}/leads/{lead_id}/panel',
                          wait_until='domcontentloaded')
                page.wait_for_timeout(250)
                name = (f'panel_{stage.lower().replace(" ", "_")}'
                        f'_{label}.png')
                path = os.path.join(OUT, name)
                page.screenshot(path=path, full_page=True)
                written.append(path)
            page.goto(f'{base}/leads/new',
                      wait_until='domcontentloaded')
            page.wait_for_timeout(200)
            path = os.path.join(OUT, f'new_lead_{label}.png')
            page.screenshot(path=path, full_page=True)
            written.append(path)
            # The page must not scroll sideways at either width.
            # The RFQ screens shipped at 536px on a 390px phone
            # because nobody measured.
            wide = page.evaluate(
                'document.documentElement.scrollWidth')
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
