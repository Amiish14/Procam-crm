"""
The world the parity screenshots are taken in.

RFQ-DEMO-PARITY needs the live screens photographed next to
`docs/reference/RFQ_Chain_Tracker_Demo.html`, and an empty screen
photographs nothing: the demo shows thirteen leads at twelve
different steps, and a board with no rows cannot be compared with
it.

So this module builds that world — in a throwaway SQLite file of
its own, never the real database. It is a developer tool. Nothing
imports it at run time, and `scripts/parity_shots.py` is its only
caller.

The names are companies that do not exist and people from the
employee master are not used: the sample lives for the length of
one screenshot run and is deleted with its temp directory.
"""
from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, timedelta

PEOPLE = (
    # emp_code, name, role, vertical
    ('PAR_ADMIN', 'Parity Director', 'admin', 'Management'),
    ('PAR_HEAD',  'Parity Head',     'manager', 'Heavy Transport'),
    ('PAR_SALES', 'Parity Sales',    'user', 'Heavy Transport'),
    ('PAR_COORD', 'Parity Coord',    'user', 'Management'),
    ('PAR_SCM',   'Parity SCM',      'user', 'Heavy Transport'),
    ('PAR_OPS',   'Parity Ops',      'user', 'Heavy Transport'),
)
ADMIN = 'PAR_ADMIN'
PASSWORD = 'ParityShots-12345'

FLAGS = ('FEATURE_ACCOUNT_FIRST', 'FEATURE_RFQ_CHAIN',
         'FEATURE_QUOTE_REV_V2', 'FEATURE_PRE_RFQ_FUNNEL',
         'FEATURE_LEAD_JOURNEY', 'FEATURE_RFQ_REPORTS')

#: name, vertical, requirement, step the chain has reached, priority
WORK = (
    ('Arka Steel Ltd', 'Heavy Transport',
     '140 T reformer, Paradip to Angul', 2, 'Critical'),
    ('Velor Wind Pvt Ltd', 'Heavy Transport',
     '78 m blades x 9, Tuticorin to Kayathar', 3, 'High'),
    ('Kaveri Power Ltd', 'Installation',
     'Turbine module erection, 3 x 60 T', 5, 'High'),
    ('Sagar Ports Pvt Ltd', 'Project Freight',
     'Breakbulk 2,400 FRT, Hamburg to Chennai', 6, 'Medium'),
    ('Delta Pharma Ltd', 'Project Freight',
     'Air export 4 PMC, BOM to FRA', 8, 'Medium'),
    ('Zenith Cement Pvt Ltd', 'Installation',
     'Kiln shell rigging, 220 T', 9, 'Low'),
    ('Orion Chemicals Ltd', 'Warehousing',
     '6,000 sq ft bonded storage, Chakan', 6, 'Critical'),
    ('Meridian Rail Pvt Ltd', 'Heavy Transport',
     'Bogie frames FTL x 12, Kolkata to Pune', 13, 'Medium'),
)
#: leads before any RFQ: name, vertical, account development stage
EARLY = (
    ('Tarang Solar Ltd', 'Warehousing', 'Meeting Completed'),
    ('Indus Refinery Pvt Ltd', 'Project Freight',
     'Requirement Identified'),
    ('Shakti Forgings Ltd', 'Installation', 'Lead Generated'),
)
#: leads with no account at all — the Account check queue
UNPLACED = (
    ('Prakash Hydro Projects Ltd', 'Installation',
     'amit@prakashhydro.example',
     'Turbine runner lifting & erection at site'),
    ('ARKA STEELS PVT LTD', 'Heavy Transport',
     'rajesh.n@arkasteel.example',
     'RFQ: shifting of 2 x 90 T reactors within plant'),
    ('Velor Wind Pvt Ltd', 'Heavy Transport',
     'velor.procurement@gmail.com',
     'Blade transport, 12 sets - budgetary quote'),
)


def fresh_database():
    """A temp sqlite path, exported before `app` is imported."""
    path = os.path.join(tempfile.mkdtemp(prefix='parity-'),
                        'parity.db')
    os.environ['DATABASE_URL'] = 'sqlite:///' + path
    os.environ['URL_PREFIX'] = ''
    os.environ['SESSION_COOKIE_SECURE'] = 'false'
    os.environ.setdefault('SECRET_KEY', 'parity-shots')
    os.environ.setdefault('ADMIN_INITIAL_PASSWORD', PASSWORD)
    return path


def flags_on(db, state=True):
    import json

    from app.models.integration import AppSetting
    from app.services import flags
    for name in FLAGS:
        key = 'flag.' + name
        row = AppSetting.query.filter_by(key=key).first()
        if row is None:
            row = AppSetting(key=key)
            db.session.add(row)
        row.value_json = json.dumps(bool(state))
    db.session.commit()
    flags.cache_clear()


def build(main):
    """Seed the world. Returns the ids the shot list needs."""
    db = main.db
    Company, Employee, Lead = main.Company, main.Employee, main.Lead
    from app.models.quote import Quote, UnmatchedQuoteMail
    from app.models.rfq import RFQ, RfqCondition
    from app.services import rfq_chain as rc

    db.create_all()
    now = datetime.utcnow()

    for code, name, role, vertical in PEOPLE:
        emp = Employee(emp_code=code, name=name, role=role,
                       vertical=vertical, is_active=True,
                       email=code.lower() + '@procamlogistics.com')
        emp.set_password(PASSWORD)
        # Without this every /api call answers 403
        # 'change your password before continuing'.
        _set(emp, must_change_pw=False)
        db.session.add(emp)
    db.session.commit()
    flags_on(db)
    _verticals(db)
    # Production has these rows; a screenshot of an
    # unseeded catalogue would misreport the live page.
    rc.seed_definitions(actor='parity')
    db.session.commit()

    ids = {'leads': [], 'rfqs': []}

    for name, vertical, stage in EARLY:
        account = Company(name=name, is_active=True)
        _set(account, dev_stage=stage, pic_emp_code='PAR_SALES')
        db.session.add(account)
        db.session.flush()
        _owns(db, account.id, vertical)
        lead = Lead(company=name, stage='New Opportunity',
                    source='manual', procam_vertical=vertical,
                    assigned_to='PAR_SALES', company_id=account.id,
                    created_at=now - timedelta(days=12))
        _set(lead, lead_channel='Customer Referrals',
             expected_rfq_timing='Short-term (1-3 months)')
        db.session.add(lead)
        db.session.flush()
        ids['leads'].append(lead.id)

    for company, vertical, sender, subject in UNPLACED:
        lead = Lead(company=company, stage='New Opportunity',
                    source='email', procam_vertical=vertical,
                    assigned_to='PAR_SALES',
                    created_at=now - timedelta(hours=5))
        _set(lead, contact_email=sender, notes=subject,
             lead_channel='Inbound Enquiries', lead_channel_auto=True)
        db.session.add(lead)
    db.session.commit()

    team = {'sales': 'PAR_SALES', 'coordinator': 'PAR_COORD',
            'scm': 'PAR_SCM', 'ops': 'PAR_OPS', 'head': 'PAR_HEAD'}
    number = 0
    for name, vertical, requirement, reached, priority in WORK:
        number += 1
        account = Company(name=name, is_active=True)
        _set(account, dev_stage='RFQ Expected',
             pic_emp_code='PAR_SALES')
        db.session.add(account)
        db.session.flush()
        _owns(db, account.id, vertical)
        age = timedelta(days=4 + number)
        lead = Lead(company=name, stage='RFQ Generated',
                    source='email', procam_vertical=vertical,
                    assigned_to='PAR_SALES', company_id=account.id,
                    created_at=now - age)
        _set(lead, lead_channel='Inbound Enquiries',
             lead_channel_auto=True, notes=requirement)
        db.session.add(lead)
        db.session.flush()
        rfq = RFQ(rfq_number='PAR-2026-%04d' % number,
                  subject=requirement, lead_id=lead.id,
                  account_id=account.id, priority=priority,
                  lead_driver='PAR_SALES', created_by_id='PAR_SALES',
                  needs_technical=True,
                  needs_survey=vertical != 'Project Freight',
                  received_date=(now - age).date(),
                  customer_deadline=(now + timedelta(days=3)).date(),
                  registered_at=now - age + timedelta(hours=3),
                  registered_by='PAR_COORD',
                  created_at=now - age)
        db.session.add(rfq)
        db.session.flush()
        ids['rfqs'].append(rfq.id)
        ids['leads'].append(lead.id)

        _run_chain(rc, rfq, team, reached, now, age)

        if reached >= 4:
            for step_no, kind, text, visibility in (
                    (4, 'Assumption',
                     'Route clear of overhead obstructions above '
                     '5.5 m; line lifting by the customer at actuals',
                     'customer'),
                    (4, 'Limitation',
                     'Movement in daylight hours only, as per police '
                     'permission', 'customer'),
                    (5, 'Condition',
                     'Bridge at km 42 needs a structural check - '
                     'cost not yet included', 'internal')):
                db.session.add(RfqCondition(
                    rfq_id=rfq.id, step_no=step_no, type=kind,
                    text=text, visibility=visibility,
                    created_by=team['ops']))

        if reached >= 7:
            sell = 2850000 + number * 190000
            quote = Quote(quote_number='PAR-QUO-%04d' % number,
                          rfq_id=rfq.id, lead_id=lead.id,
                          account_id=account.id, subject=requirement,
                          currency='INR', total_amount=sell,
                          status='Submitted',
                          prepared_by_id=team['scm'],
                          approved_by_id=team['head'],
                          approved_at=now - timedelta(days=2),
                          submitted_by_id=team['sales'],
                          submitted_at=now - timedelta(days=1),
                          quote_date=date.today())
            _set(quote, cost_amount=round(sell * 0.74),
                 cost_saved_by=team['scm'], cost_saved_at=now,
                 revision_reason='Initial offer')
            db.session.add(quote)
            if reached >= 9:
                db.session.flush()
                rev = Quote(quote_number='PAR-QUO-%04d-R1' % number,
                            rfq_id=rfq.id, lead_id=lead.id,
                            account_id=account.id,
                            subject=requirement, currency='INR',
                            total_amount=round(sell * 0.96),
                            status='Draft',
                            prepared_by_id=team['scm'],
                            quote_date=date.today())
                _set(rev, cost_amount=round(sell * 0.74),
                     revision_reason='Customer asked for a revised '
                                     'price',
                     parent_quote_id=quote.id, rev_no=1,
                     revision_number=2)
                db.session.add(rev)

        if number == 3:
            rfq.held_at = now - timedelta(hours=30)
            rfq.held_by = team['head']
            rfq.hold_reason = 'Postponed by customer'
            rfq.hold_remarks = ('Customer moved project start to Q1; '
                                'keep the file ready')
        if number == 8:
            lead.stage = 'Won'
    db.session.commit()

    for subject, sender, why, lead_id in (
            ('Our offer for the reformer movement',
             'parity.sales@procamlogistics.com',
             'No RFQ or quote number in the subject',
             ids['leads'][-1]),
            ('Fwd: trailer availability Hazira',
             'parity.scm@procamlogistics.com',
             'No number; does not look like a quotation', None)):
        row = UnmatchedQuoteMail(
            subject=subject, from_addr=sender,
            to_addr='leads@procamgroup.in',
            sent_at=now - timedelta(hours=4),
            status=UnmatchedQuoteMail.OPEN, why=why)
        _set(row, suggested_lead_id=lead_id,
             message_id='parity-%s' % abs(hash(subject)))
        db.session.add(row)
    db.session.commit()
    return ids


def _verticals(db):
    """Name the four verticals and their heads.

    Without these rows `vertical_heads.master_vertical` answers
    Management for every lead, so every seeded lead lands in the
    Account check queue and the queue stops resembling the demo's.
    """
    from app.models.email_admin import VerticalHead
    from app.services import vertical_heads

    for name in ('Heavy Transport', 'Project Freight',
                 'Installation', 'Warehousing', 'Management'):
        db.session.add(VerticalHead(vertical=name,
                                    head_emp_code='PAR_HEAD',
                                    active=True))
    db.session.commit()
    vertical_heads._CACHE.update({'heads': None, 'at': 0})


def _owns(db, account_id, vertical):
    from app.models.assignment import AccountVerticalOwner
    db.session.add(AccountVerticalOwner(
        account_id=account_id, vertical=vertical,
        emp_code='PAR_SALES'))
    db.session.flush()


def _run_chain(rc, rfq, team, reached, now, age):
    """Close steps 1..reached-1 and leave `reached` open.

    `close_step` stamps `completed_at` with the clock, as it must,
    and `result_for` reads `started_at` at that moment to decide
    Early or Delayed. So each step is started at the distance that
    produces the verdict wanted, closed, and only then moved back
    onto the RFQ's own timeline — otherwise every sample step
    closes in the same second and the time chart is flat.
    """
    from app import db

    cursor = now - age
    for step in rc.applicable(rfq):
        no = step['no']
        if no > reached:
            break
        owner = team.get(step['who'], 'PAR_SALES')
        task = rc.open_step(rfq, no, owner=owner, actor='parity')
        if task is None:
            continue
        if no >= reached:
            # The open step is dated from now, not from the RFQ's
            # own timeline, so the board shows green, orange and
            # red rather than one wall of overdue.
            standard = rc.standard_hours(no, rfq.priority) or 24.0
            share = (0.4, 0.85, 1.6)[reached % 3]
            task.started_at = (datetime.utcnow()
                               - timedelta(hours=standard * share))
            task.owner_user_id = owner
            break
        standard = rc.standard_hours(no, rfq.priority) or 12.0
        took = standard * (0.45 if no % 3 else 1.6)
        task.started_at = datetime.utcnow() - timedelta(hours=took)
        db.session.flush()
        rc.close_step(
            rfq, no, actor=owner, event=step['event'],
            reason=('Awaiting vendor rate' if took > standard
                    else 'Historical rate / database used'))
        task.started_at = cursor
        task.completed_at = cursor + timedelta(hours=took)
        task.completed_by_id = owner
        cursor = task.completed_at
    db.session.flush()


def _set(obj, **kw):
    """Set only the columns this build of the model actually has."""
    for key, value in kw.items():
        if hasattr(obj, key):
            setattr(obj, key, value)
