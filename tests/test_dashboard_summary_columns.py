"""The dashboard summary reads only the columns it needs, and says exactly
what it said when it loaded whole Lead objects."""
import os
import random
import sys
import tempfile
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'DashColsTestOnly12345')
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'dashcols.db'))

import app as app_module                                         # noqa: E402
from app import app as flask_app, db, Lead                       # noqa: E402

TAG = 'DashCols'


def test_column_rows_give_the_same_summary_as_whole_objects():
    rng = random.Random(3)
    stages = ['New', 'Call Done', 'Profile Sent', 'Appointment', 'Visit Done',
              'RFQ Generated', 'Quoted', 'Under Negotiation', 'Won', 'Lost']
    today = date.today()
    # The payload reads the request's filters and the session's scope.
    with flask_app.test_request_context('/api/dashboard/summary'):
        db.create_all()
        made = []
        for i in range(120):
            d = lambda: (today - timedelta(days=rng.randint(-5, 90))
                         if rng.random() < .6 else None)
            l = Lead(company=f'{TAG} {i}', source=rng.choice(['email', 'manual']),
                     stage=rng.choice(stages),
                     industry=rng.choice(['Steel', 'Power', None]),
                     procam_vertical=rng.choice(['Project Freight', None]),
                     state=rng.choice(['Gujarat', 'Maharashtra', None]),
                     assigned_to=rng.choice(['DC1', 'DC2', None]),
                     assigned_name=rng.choice(['One', 'Two', None]),
                     cost_million=rng.choice([None, 1.5, 12.0]),
                     intro_mail_date=d(), phone_call_date=d(),
                     meeting_date=d(), rfq_date=d(), followup_date=d(),
                     opp_number=rng.choice([None, f'O-{i}']),
                     lost_reason=rng.choice([None, 'Price']),
                     updated_at=datetime.utcnow() - timedelta(
                         days=rng.randint(0, 60)),
                     stage_entered_at=rng.choice([None, datetime.utcnow()
                                                  - timedelta(days=20)]))
            made.append(l)
        db.session.add_all(made)
        db.session.commit()
        try:
            q = Lead.query.filter(Lead.company.like(f'{TAG} %'))
            codes = {'DC1', 'DC2'}
            whole = app_module._dashboard_summary_payload(q.all(), codes, today)
            rows = q.with_entities(*[getattr(Lead, c) for c in
                                     app_module._SUMMARY_COLUMNS]).all()
            slim = app_module._dashboard_summary_payload(rows, codes, today)
            for key in ('kpis', 'funnel', 'ageing', 'pic_board'):
                assert whole[key] == slim[key], key
            assert whole['distributions'] == slim['distributions']
            assert whole['options']['industries'] == slim['options']['industries']
        finally:
            Lead.query.filter(Lead.company.like(f'{TAG} %')).delete(
                synchronize_session=False)
            db.session.commit()
