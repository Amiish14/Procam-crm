"""
Photographs of what CRM-COCKPIT-02 adds.

§9 asks for screenshots with the acceptance results. These are them:
the cockpit's new tiles with the scope label under them, the lead
pop-up's deadline card in each of its states (unconfirmed, a later
mail's offer, missed), the follow-up quick picks, and the guided-help
refusals a save gets when either date is missing.

A throwaway database and Playwright. Nothing touches the live system,
and the browser is deliberately not in requirements.txt — the test
suite must not need one.

    .venv/bin/python scripts/cockpit02_shots.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(_ROOT, 'docs', 'reference', 'cockpit02')
WIDTHS = (('desktop', 1280, 1000), ('phone', 390, 844))

ADMIN = 'C02_SHOTS'
VERTICAL = 'Heavy Transport'

#: Everything these screens sit behind. CRM-COCKPIT-02's own code is
#: not behind a switch (§0.1); these are the older ones the cockpit
#: and the lead screens already needed.
FLAGS = ('FEATURE_CRM2', 'FEATURE_LEAD_JOURNEY', 'FEATURE_RFQ_CHAIN',
         'FEATURE_RFQ_REPORTS', 'FEATURE_LEAD_POPUP_V2',
         'FEATURE_GUIDED_HELP')


def _world():
    from app import Company, Employee, Lead, app, db
    from app.models.access import AccessProfile, DataScope
    from app.models.integration import AppSetting
    from app.services import flags

    with app.app_context():
        db.create_all()
        db.session.add(Employee(
            emp_code=ADMIN, name='James Francis Xavier',
            password_hash='x', role='admin', vertical=VERTICAL,
            is_active=True, must_change_pw=False, is_super_admin=True,
            email='shots@procamlogistics.com'))
        db.session.add(AccessProfile(emp_code=ADMIN,
                                     data_scope=DataScope.ALL,
                                     perms=[]))
        for name in FLAGS:
            db.session.add(AppSetting(key='flag.' + name,
                                      value_json=json.dumps(True)))
        account = Company(name='INDO TECH TRANSFORMERS LIMITED',
                          is_active=True, pic_emp_code=ADMIN,
                          vertical=VERTICAL, industry='Power',
                          state='Tamil Nadu', city='Chennai')
        db.session.add(account)
        db.session.flush()

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        made = {}
        #: (key, stage, due, confirmed, quiet days, subject)
        rows = (
            ('due_soon', 'RFQ Generated', now + timedelta(hours=20),
             False, 2,
             'RA- 57103: Indo Tech Transformers Reverse Auction '
             + (now + timedelta(hours=20) + timedelta(hours=5, minutes=30)
                ).strftime('%d %b %Y %H:%M:00')),
            ('due_later', 'RFQ Generated', now + timedelta(days=6),
             True, 1, 'eRFQ- 41688: Sterlite Power — Transportation'),
            ('missed', 'RFQ Generated', now - timedelta(days=3),
             True, 9, 'RA- 56609: auction closed'),
            ('no_due', 'Quoted', None, False, 22,
             'Enquiry for ODC movement — Hazira to Jamnagar'),
            ('quiet', 'Business Discussion', None, False, 34,
             'Transport requirement'),
        )
        for key, stage, due, confirmed, quiet, subject in rows:
            lead = Lead(
                company=account.name, company_id=account.id,
                stage=stage, source='email', assigned_to=ADMIN,
                email_message_id=f'<c02-{key}@example>',
                assigned_name='James Francis Xavier',
                procam_vertical=VERTICAL, pic='R Venkatesh',
                designation_pic='Logistics Manager',
                email='rfq@indotech.example',
                project='2 x 40 MVA transformers',
                origin='Vadodara', destination='Lapanga',
                cargo_type='Transformer', cargo_weight_mt=94.5,
                cargo_dimensions='7.2 x 3.1 x 3.4 m',
                urgency='High',
                auto_filled_json=json.dumps(
                    ['cargo_type', 'cargo_weight_mt',
                     'cargo_dimensions', 'urgency']),
                opportunity_value_num=2500000,
                estimated_value_inr=2500000,
                value_basis='Client budget',
                quote_due_at=due,
                quote_due_source=('portal_subject' if due else None),
                quote_due_confirmed=confirmed,
                quote_due_missed_at=(now - timedelta(days=2)
                                     if key == 'missed' else None),
                portal_key=('RA-57103' if key == 'due_soon'
                            else 'ERFQ-41688' if key == 'due_later'
                            else 'RA-56609' if key == 'missed'
                            else None),
                followup_date=(date.today() + timedelta(days=2)
                               if key != 'quiet' else None),
                original_email_subject=subject,
                original_email_body='Please submit your best offer.',
                last_activity_at=now - timedelta(days=quiet),
                last_activity_kind='mail_in',
                stage_entered_at=now - timedelta(days=quiet + 2),
                created_at=now - timedelta(days=quiet + 10),
                received_at=now - timedelta(days=quiet + 10))
            db.session.add(lead)
            db.session.flush()
            made[key] = lead.id

        # §2.5 — an offer from a later mail, so the banner has
        # something to show.
        from app.models.cockpit02 import LeadQuoteDueHistory
        _offer = now + timedelta(days=9)
        db.session.add(LeadQuoteDueHistory(
            lead_id=made['due_soon'],
            old_due_at=now + timedelta(hours=20),
            new_due_at=_offer,
            reason_code='proposed', source='mail_body',
            # The quoted evidence has to be the sentence that date
            # was read from, or the picture shows the CRM misquoting
            # a mail.
            reason_text=('the auction now closes '
                         + (_offer + timedelta(hours=5, minutes=30)
                            ).strftime('%d %b %Y %H:%M')),
            actor='system'))
        db.session.commit()
        flags.cache_clear()
        return made


def main():
    tmp = tempfile.mkdtemp()
    os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(tmp,
                                                             'c02.db')
    os.environ.setdefault('SECRET_KEY', 'c02shots')
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
    server = make_server('127.0.0.1', 5099, flask_app)
    Thread(target=server.serve_forever, daemon=True).start()

    from playwright.sync_api import sync_playwright
    base = 'http://127.0.0.1:5099'
    written, notes = [], []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for label, width, height in WIDTHS:
            ctx = browser.new_context(
                viewport={'width': width, 'height': height})
            page = ctx.new_page()
            page.goto(base + '/_shotlogin',
                      wait_until='domcontentloaded')

            def shot(name, *, full=True):
                path = os.path.join(OUT, f'{name}_{label}.png')
                page.screenshot(path=path, full_page=full)
                written.append(path)

            # 1. the cockpit: the new tiles and the scope label
            page.goto(base + '/crm2', wait_until='domcontentloaded')
            try:
                page.wait_for_selector('#c02tiles .ck-tile',
                                       timeout=8000)
            except Exception:                             # noqa: BLE001
                page.wait_for_timeout(1500)
            shot('01_cockpit_tiles')
            tiles = page.evaluate(
                "Array.from(document.querySelectorAll("
                "'#c02tiles .ck-tile')).map(t => t.innerText"
                ".replace(/\\n/g,' '))")
            notes.append(f'{label}: tiles = {tiles}')
            scope = page.evaluate(
                "(document.getElementById('scope')||{}).innerText||''")
            notes.append(f'{label}: scope label = {scope.strip()!r}')

            # 2. the lead pop-up — a deadline inside a day, unconfirmed,
            #    with a later mail's offer under it
            page.goto(base + f'/app?lead={made["due_soon"]}',
                      wait_until='domcontentloaded')
            try:
                page.wait_for_selector('#ldDue .ld-due-head',
                                       timeout=8000)
            except Exception:                             # noqa: BLE001
                page.wait_for_timeout(2000)
            shot('02_popup_due_red_and_offer')
            card = page.evaluate(
                "(document.getElementById('ldDue')||{}).innerText||''")
            notes.append(f'{label}: due card = '
                         f'{card.strip()[:160]!r}')

            # 3. the extend form, with its reason dropdown
            try:
                page.click('#ldDue button:has-text("Extend")',
                           timeout=4000)
                page.wait_for_timeout(400)
                shot('03_popup_extend')
            except Exception as exc:                      # noqa: BLE001
                notes.append(f'{label}: extend form not shown ({exc})')

            # 4. a missed submission, and "Did we bid?"
            page.goto(base + f'/app?lead={made["missed"]}',
                      wait_until='domcontentloaded')
            try:
                page.wait_for_selector('#ldDue .ld-due-head',
                                       timeout=8000)
            except Exception:                             # noqa: BLE001
                page.wait_for_timeout(2000)
            shot('04_popup_missed')

            # 5. the follow-up quick picks
            page.goto(base + f'/app?lead={made["quiet"]}',
                      wait_until='domcontentloaded')
            try:
                page.wait_for_selector('#lFuPicks button', timeout=8000)
            except Exception:                             # noqa: BLE001
                page.wait_for_timeout(2000)
            shot('05_popup_followup_picks')
            picks = page.evaluate(
                "Array.from(document.querySelectorAll("
                "'#lFuPicks button')).map(b => b.innerText)")
            notes.append(f'{label}: quick picks = {picks}')

            # 6. the refusal a save gets with no deadline (§2.2)
            page.goto(base + f'/app?lead={made["no_due"]}',
                      wait_until='domcontentloaded')
            page.wait_for_timeout(1500)
            refusal = page.evaluate(
                """async () => {
                    const r = await fetch('/api/leads/"""
                + str(made['no_due'])
                + """', {method:'PUT',
                        headers:{'Content-Type':'application/json'},
                        body: JSON.stringify({stage:'Under Negotiation'})});
                    const b = await r.json();
                    return [r.status, (b.guide||{}).code,
                            (b.guide||{}).title, b.suggested];
                }""")
            notes.append(f'{label}: refusal = {refusal}')

            # 7. the Workbench, where every tile's list opens
            page.goto(base + '/my-work?filter=due_date_missing',
                      wait_until='domcontentloaded')
            page.wait_for_timeout(1500)
            shot('06_list_due_date_missing')
            ctx.close()
        browser.close()
    server.shutdown()

    print(f'\n  {len(written)} screenshot(s) in {OUT}')
    for path in written:
        print(f'    {os.path.relpath(path, _ROOT)}')
    print('\n  what the pages actually said:')
    for line in notes:
        print(f'    {line}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
