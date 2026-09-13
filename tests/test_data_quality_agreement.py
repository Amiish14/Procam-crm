"""The dashboard and the read-only report give the same answer.

They cannot share code: the report must not import the app (its boot
autoheal writes), and the dashboard must apply the Access Matrix through
the ORM. They share definitions.py instead — and this holds the two
engines to the same records on one seeded database, so a change to either
that makes them disagree fails here rather than in a meeting.

The database is the one the whole run shares, so only records created
here are compared, and they are removed afterwards.
"""
import os
import sys
import tempfile
from datetime import date, datetime, timedelta

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'DqAgreeTestOnly12345')
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'dqagree.db'))

from app import (app as flask_app, db, Company, Contact, Employee,  # noqa: E402
                 Lead, LeadEmail, Opportunity)
import app.models                                                    # noqa: E402,F401
from app.data_quality import service as dq                           # noqa: E402
from app.models.tms_handover import WonHandover                      # noqa: E402

# Appended, not prepended, and only after the app is imported: scripts/
# holds an email_ingest.py that would shadow the email_ingest package.
sys.path.append(os.path.join(_ROOT, 'scripts'))
import data_quality_report as report                                 # noqa: E402

PAST = date.today() - timedelta(days=9)
FUTURE = date.today() + timedelta(days=9)


@pytest.fixture()
def seeded():
    made = {'company': [], 'lead': [], 'opp': [], 'handover': [],
            'contact': [], 'email': []}

    def add(kind, obj):
        db.session.add(obj)
        db.session.flush()
        made[kind].append(obj.id)
        return obj

    with flask_app.app_context():
        db.create_all()
        if db.engine.url.get_backend_name() != 'sqlite' or \
                not db.engine.url.database:
            pytest.skip('the report engine needs a SQLite file')
        for code, active in (('DQAOK', True), ('DQAGONE', False)):
            e = Employee.query.filter_by(emp_code=code).first()
            if e is None:
                e = Employee(emp_code=code, name=f'Placeholder {code}')
                db.session.add(e)
            e.is_active, e.role, e.must_change_pw = active, 'user', False
        db.session.commit()

        ok = add('company', Company(name='Zq Agree Alpha', pic_emp_code='DQAOK',
                                    is_active=True, gstin='27ZQAGR0000A1Z5'))
        add('company', Company(name='Zq Agree Alpha Pvt Ltd', is_active=True))
        add('company', Company(name='Zq Agree Beta', pic_emp_code='DQAGONE',
                               is_active=True, gstin='27ZQAGR0000A1Z5'))
        add('company', Company(name='Zq Agree Gamma', pic_emp_code='DQAOK',
                               is_active=True,
                               email_domains=['zq-agree.example']))
        add('company', Company(name='Zq Agree Delta', pic_emp_code='DQAOK',
                               is_active=True,
                               email_domains=['zq-agree.example', 'x.example']))

        for owner, archived in (('DQAOK', False), ('', False),
                                ('DQAGONE', False), ('', True)):
            add('lead', Lead(company='Zq Agree Lead', company_id=ok.id,
                             assigned_to=owner, stage='New',
                             is_archived=archived))
        quoted = add('lead', Lead(company='Zq Agree Lead', assigned_to='DQAOK',
                                  stage='Quoted'))

        def opp(n, **kw):
            base = dict(opp_number=f'ZQA-{n}', stage='Proposal',
                        owner_emp_code='DQAOK', company_id=ok.id,
                        expected_close_date=FUTURE)
            base.update(kw)
            return add('opp', Opportunity(**base))

        opp(1)
        opp(2, owner_emp_code=None, expected_close_date=PAST)
        opp(3, owner_emp_code='DQAGONE')
        opp(4, stage='Won', won_at=datetime.utcnow(), expected_close_date=PAST)
        with_po = opp(5, stage='Won', won_at=datetime.utcnow())
        without_po = opp(6, stage='Won', won_at=datetime.utcnow())
        opp(7, stage='Negotiation', won_at=datetime.utcnow())
        cancelled = opp(8, stage='Won', won_at=datetime.utcnow())
        add('handover', WonHandover(opportunity_id=with_po.id, po_ref='ZQ-PO',
                                    status='Handover Pending'))
        add('handover', WonHandover(opportunity_id=without_po.id,
                                    status='Awaiting PO'))
        add('handover', WonHandover(opportunity_id=cancelled.id,
                                    status='Cancelled'))

        add('contact', Contact(name='Zq One', email='one@zq-agree.example',
                               phone='98700 11111', is_active=True))
        add('contact', Contact(name='Zq Two', email='ONE@zq-agree.example',
                               is_active=True))
        add('contact', Contact(name='Zq Three', email='three@zq-agree.example',
                               mobile='+91 98700 11111', is_active=True))
        add('contact', Contact(name='Zq Four', email='four@zq-agree.example',
                               is_active=True))

        add('email', LeadEmail(lead_id=quoted.id, direction='outbound',
                               intake_class='H_quote_submission',
                               from_addr='rates@zq-agent.example'))
        add('email', LeadEmail(lead_id=quoted.id, direction='outbound',
                               intake_class='H_quote_submission',
                               from_addr='sales@procamlogistics.com'))
        db.session.commit()
        made['path'] = db.engine.url.database
    yield made
    with flask_app.app_context():
        for model, kind in ((LeadEmail, 'email'), (Contact, 'contact'),
                            (WonHandover, 'handover'), (Opportunity, 'opp'),
                            (Lead, 'lead'), (Company, 'company')):
            if made[kind]:
                model.query.filter(model.id.in_(made[kind])).delete(
                    synchronize_session=False)
        db.session.commit()


def _live(keys, kind_ids):
    out = set()
    for key in keys:
        data = dq.records_for(key, sc=dq.system_scope(), per_page=100000)
        recs = ([r for g in data['groups'] for r in g['records']]
                if data['grouped'] else data['records'])
        out |= {r['id'] for r in recs}
    return out & set(kind_ids)


def _cli(conn, name, kind_ids, field='id'):
    return {r[field] for r in report.REPORTS[name](conn)[1]} & set(kind_ids)


def test_the_dashboard_and_the_report_agree(seeded):
    with flask_app.app_context(), \
            report.read_only_engine('sqlite:///' + seeded['path']) \
            .connect() as conn:
        c, l, o = seeded['company'], seeded['lead'], seeded['opp']

        pairs = [
            ('accounts_without_owner', ('no_pic', 'accounts_of_leavers'), c),
            ('leads_without_owner', ('unowned_leads', 'leads_of_leavers'), l),
            ('opportunities_without_owner',
             ('unowned_opps', 'opps_of_leavers'), o),
            ('won_without_handover', ('won_no_handover',), o),
            ('duplicate_accounts', ('dupe_companies',), c),
            ('duplicate_contacts', ('dupe_contacts',), seeded['contact']),
        ]
        for name, keys, ids in pairs:
            cli, live = _cli(conn, name, ids), _live(keys, ids)
            assert cli, f'{name}: the seed exercises nothing'
            assert cli == live, f'{name}: report {cli} ≠ dashboard {live}'

        # Overdue to close is one of the ways a deal is stale.
        overdue = _cli(conn, 'opportunities_overdue_close', o)
        assert overdue and overdue <= _live(('stale_opps',), o)

        # The report lists won deals with no PO whether or not a handover
        # exists; the dashboard splits them into "no PO on the handover"
        # and "no handover at all".
        cli_po = _cli(conn, 'won_without_po', o)
        with_handover = {h.opportunity_id for h in dq.run(
            dq.find('won_no_po'), dq.system_scope())[1].all()}
        assert cli_po == (_live(('won_no_handover',), o)
                          | (with_handover & set(o)))

        cli_sent = _cli(conn, 'quotes_received_filed_as_sent',
                        seeded['email'], field='email_id')
        assert cli_sent and cli_sent == _live(('quotes_filed_as_sent',),
                                              seeded['email'])
