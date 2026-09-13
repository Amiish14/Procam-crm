"""
The list intents count in SQL and load only the rows they show.

They used to load every matching lead as a full ORM object to render
fifty rows and a count — most of a quarter-second answer on a 10,000-
lead desk. The rewrite must change the cost and nothing else, so each
test here computes the answer the old way, over the same seeded rows,
and requires the new handler to give the same headline, figures, rows
and order.
"""
import os
import sys
import tempfile
from datetime import date, datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'EfficiencyTestOnly123')
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'efficiency.db'))
os.environ.pop('PROCAM_AI_BASE_URL', None)

from app import (app as flask_app, db, Company, Lead, LeadEmail,  # noqa: E402
                 LeadActivity)
from app.access import scope as scope_mod                        # noqa: E402
from app.copilot import intents as catalogue                     # noqa: E402
from app.copilot import queries as Q                             # noqa: E402
from app.services import contact                                 # noqa: E402

CODE = 'EFREP'
TERMINAL = ('Won', 'Lost', 'On Hold', 'Not Interested')


@pytest.fixture(scope='module')
def seeded():
    """More rows than ROW_CAP, with ties in every ordering column."""
    with flask_app.app_context():
        db.create_all()
        today = date.today()
        base = datetime.utcnow().replace(microsecond=0) - timedelta(days=3)
        leads = []
        for i in range(130):
            stage = ('Lost' if i % 9 == 0 else
                     'Won' if i % 13 == 0 else
                     ('Quoted', 'Under Negotiation', 'New')[i % 3])
            l = Lead(company=f'Efficiency Co {i:03d}', source='manual',
                     assigned_to=CODE, stage=stage,
                     estimated_value_inr=(0, 250000, 4800000)[i % 3],
                     # every fourth lead shares a timestamp with the last
                     created_at=base - timedelta(hours=(i // 4) * 5),
                     updated_at=base - timedelta(days=i % 7),
                     followup_date=(None if i % 5 == 0 else
                                    today - timedelta(days=i % 11)),
                     lost_reason=(('Price', 'Timeline', '', None)[i % 4]
                                  if stage == 'Lost' else None))
            leads.append(l)
        db.session.add_all(leads)
        comps = [Company(name=f'Efficiency Account {i}', is_active=True,
                         pic_emp_code=CODE,
                         last_activity_at=(None if i % 3 == 0 else
                                           base - timedelta(days=200 + i % 2)))
                 for i in range(70)]
        db.session.add_all(comps)
        db.session.commit()
        mails = []
        for l in leads[::6]:
            mails.append(LeadEmail(lead_id=l.id, direction='inbound',
                                   subject='s', body='b',
                                   sent_or_received_at=base - timedelta(
                                       days=l.id % 30)))
        acts = [LeadActivity(lead_id=leads[7].id, kind='call',
                             occurred_at=base - timedelta(days=40))]
        db.session.add_all(mails + acts)
        db.session.commit()
        ids = [l.id for l in leads]
        yield ids
        LeadEmail.query.filter(LeadEmail.lead_id.in_(ids)).delete(
            synchronize_session=False)
        LeadActivity.query.filter(LeadActivity.lead_id.in_(ids)).delete(
            synchronize_session=False)
        Lead.query.filter(Lead.id.in_(ids)).delete(synchronize_session=False)
        Company.query.filter(Company.name.like('Efficiency Account %')
                             ).delete(synchronize_session=False)
        db.session.commit()


def _sc():
    sc = scope_mod.for_employee(CODE)
    sc.codes = {CODE}
    sc.perms = {'admin.master', 'reports.accounts', 'reports.competitor'}
    return sc


def _lead_rows():
    """The old candidate order: created_at, then the table's own order."""
    rows = Lead.query.filter(Lead.assigned_to == CODE).all()
    return rows


def _same(result, headline, figures, rows):
    assert result.headline == headline
    for k, v in figures.items():
        assert result.figures[k] == v, k
    got = [{k: v for k, v in r.items()} for r in result.rows]
    assert got == rows


# ── the old algorithms, as the specification ─────────────────────────
def test_open_leads_match_the_full_load(seeded):
    with flask_app.app_context():
        found = sorted([l for l in _lead_rows() if l.stage not in TERMINAL],
                       key=lambda l: (-l.created_at.timestamp(), l.id))
        rows = [{'Company': l.company, 'Stage': l.stage,
                 'Value': Q._money(l.estimated_value_inr),
                 'Owner': l.assigned_to or '—',
                 'Age': Q._age(l.created_at) or 0,
                 '_chip': Q._lead_chip(l)} for l in found][:Q.ROW_CAP]
        assert len(found) > Q.ROW_CAP
        r = catalogue.get('leads_open').handler(_sc(), {})
        _same(r, f'{len(found)} open lead(s).', {'count': len(found)}, rows)
        assert r.notes == [f'Showing the first {Q.ROW_CAP} of {len(found)}.']


def test_no_next_action_matches_the_full_load(seeded):
    with flask_app.app_context():
        found = sorted([l for l in _lead_rows() if l.stage not in TERMINAL
                        and l.followup_date is None],
                       key=lambda l: (-l.created_at.timestamp(), l.id))
        rows = [{'Company': l.company, 'Stage': l.stage,
                 'Owner': l.assigned_to or '—',
                 'Value': Q._money(l.estimated_value_inr),
                 '_chip': Q._lead_chip(l)} for l in found][:Q.ROW_CAP]
        r = catalogue.get('leads_no_next_action').handler(_sc(), {})
        _same(r, f'{len(found)} open lead(s) with no next action recorded.',
              {'count': len(found)}, rows)


def test_followups_match_the_full_load(seeded):
    with flask_app.app_context():
        today = date.today()
        found = sorted([l for l in _lead_rows() if l.stage not in TERMINAL
                        and l.followup_date is not None
                        and l.followup_date <= today],
                       key=lambda l: (l.followup_date, l.id))
        overdue = sum(1 for l in found if l.followup_date < today)
        r = catalogue.get('followups_due').handler(_sc(), {})
        assert r.headline == f'{len(found)} follow-up(s) due — {overdue} ' \
                             f'overdue.'
        assert r.figures == {'count': len(found), 'overdue': overdue}
        assert [row['_chip']['id'] for row in r.rows] == \
            [l.id for l in found][:Q.ROW_CAP]


def test_stale_matches_the_full_load(seeded):
    with flask_app.app_context():
        touched = contact.contacted_since(Q._days_ago(7))
        open_leads = sorted([l for l in _lead_rows()
                             if l.stage not in TERMINAL],
                            key=lambda l: (l.created_at.timestamp(), l.id))
        found = [l for l in open_leads if l.id not in touched]
        expected = []
        for l in found[:Q.ROW_CAP]:
            last = contact.last_contact(l)
            expected.append({
                'Company': l.company, 'Stage': l.stage,
                'Value': Q._money(l.estimated_value_inr),
                'Last contact': (str(last)[:10] if last else 'never'),
                'Idle days': (Q._age(last) if last
                              else Q._age(l.created_at) or 0),
                'Owner': l.assigned_to or '—', '_chip': Q._lead_chip(l)})
        r = catalogue.get('leads_stale').handler(_sc(), {})
        _same(r, f'{len(found)} lead(s) with no contact for 7+ days.',
              {'count': len(found), 'open_total': len(open_leads)}, expected)


def test_lost_without_reason_matches_the_full_load(seeded):
    with flask_app.app_context():
        found = sorted([l for l in _lead_rows() if l.stage == 'Lost'
                        and not l.lost_reason],
                       key=lambda l: (-l.updated_at.timestamp(), l.id))
        rows = [{'Company': l.company, 'Owner': l.assigned_to or '—',
                 'Lost': str(l.updated_at or '')[:10],
                 '_chip': Q._lead_chip(l)} for l in found][:Q.ROW_CAP]
        r = catalogue.get('dq_lost_no_reason').handler(_sc(), {})
        _same(r, f'{len(found)} lost lead(s) with no reason.',
              {'count': len(found)}, rows)


def test_last_contacts_agrees_with_last_contact(seeded):
    """One definition, read two ways — they must never differ."""
    with flask_app.app_context():
        leads = _lead_rows()
        bulk = contact.last_contacts([l.id for l in leads])
        for l in leads:
            assert bulk.get(l.id) == contact.last_contact(l), l.id
        everyone = contact.last_contacts()
        for l in leads:
            assert everyone.get(l.id) == contact.last_contact(l), l.id


def test_next_best_action_matches_the_per_lead_reads(seeded, monkeypatch):
    """Recomputed with the old one-query-pair-per-lead contact read."""
    with flask_app.app_context():
        fast = catalogue.get('next_best_action').handler(_sc(), {})
        monkeypatch.setattr(
            contact, 'last_contacts',
            lambda ids=None: {l.id: contact.last_contact(l)
                              for l in Lead.query.all()
                              if contact.last_contact(l)})
        slow = catalogue.get('next_best_action').handler(_sc(), {})
        assert fast.to_dict() == slow.to_dict()


def test_inactive_accounts_match_the_full_load(seeded):
    with flask_app.app_context():
        cutoff = Q._days_ago(90)
        found = [c for c in Company.query.filter(
            Company.pic_emp_code == CODE, Company.is_active.is_(True)).all()
            if c.last_activity_at is None or c.last_activity_at < cutoff]
        found.sort(key=lambda c: (c.last_activity_at is not None,
                                  c.last_activity_at or datetime.min, c.id))
        r = catalogue.get('accounts_inactive').handler(_sc(), {})
        assert r.figures['count'] == len(found)
        assert [row['_chip']['id'] for row in r.rows] == \
            [c.id for c in found][:Q.ROW_CAP]


def test_only_the_displayed_slice_is_loaded(seeded):
    """The row query carries a LIMIT; the total comes from a COUNT."""
    from sqlalchemy import event

    seen = []

    def spy(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    with flask_app.app_context():
        engine = db.engine
        event.listen(engine, 'before_cursor_execute', spy)
        try:
            catalogue.get('leads_open').handler(_sc(), {})
        finally:
            event.remove(engine, 'before_cursor_execute', spy)
    lead_selects = [s for s in seen if 'FROM leads' in s]
    assert any('count(' in s.lower() for s in lead_selects)
    rows_query = [s for s in lead_selects if 'count(' not in s.lower()]
    assert rows_query and all('LIMIT' in s for s in rows_query)
    assert all('original_email_body' not in s for s in rows_query)
