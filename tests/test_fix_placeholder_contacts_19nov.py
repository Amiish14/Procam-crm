"""UAT 19 Nov: the seven placeholder contacts, two countries, the
placeholder account list — dry run, apply, undo."""
import importlib.util
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_contact_search_15nov import (  # noqa: E402,F401
    Company, Contact, Lead, _account, _clean, _contact, _lead, db)

_spec = importlib.util.spec_from_file_location(
    'phfix', os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'scripts',
        '2026_11_19_fix_placeholder_contacts.py'))
phfix = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(phfix)


def _setup(monkeypatch):
    holder = _account(name='New Lead')
    real = _account(name='ZT Luminous Power Technologies Pvt Ltd',
                    email_domains=['ztluminous.example'])
    facor = _account(name='ZT Vedanta Ltd - Ferro Alloys Business (FACOR)')
    a = _contact(name='ZT Soumya', company='New Lead', company_id=holder.id,
                 email='s@ztluminous.example')
    b = _contact(name='ZT Joachim', company='New Lead', company_id=holder.id,
                 email='j@ztfried.example')
    p1 = _contact(name='ZT Pratap', company='New Lead', company_id=holder.id,
                  email='p@ztvedanta.example')
    _contact(name='ZT Pratap', company=facor.name, account_id=facor.id,
             company_id=facor.id, email='p2@ztvedanta.example')
    jen = _contact(name='ZT Jenna', country='India')
    lead = _lead(pic='ZT Jenna', country='India')
    monkeypatch.setattr(phfix, 'TARGETS', (
        (a.id, 'ZT Luminous Power Technologies', 'ztluminous.example'),
        (b.id, 'ZT Fried Spedition', 'ztfried.example'),
        (p1.id, None, 'ztvedanta.example')))
    monkeypatch.setattr(phfix, 'COUNTRY_CONTACTS', ((jen.id, 'Finland'),))
    monkeypatch.setattr(phfix, 'COUNTRY_LEADS', ((lead.id, 'Finland'),))
    return holder, real, facor, a, b, p1, jen, lead


def test_dry_run_apply_and_undo(monkeypatch, capsys):
    holder, real, facor, a, b, p1, jen, lead = _setup(monkeypatch)
    items = phfix.plan()
    notes = {row.id: (changes, note) for k, row, changes, note in items
             if row is not None and k == 'contact' and 'country' != note}
    assert notes[a.id][1].startswith(f'account #{real.id}')
    assert 'CREATED' in notes[b.id][1]
    assert notes[p1.id][1].endswith(f'his other record #{p1.id + 1}') or \
        f'#{facor.id}' in notes[p1.id][1]
    survey = phfix.placeholder_survey()
    assert any(acc.id == holder.id and row is not None and row.id == a.id
               for acc, _k, row, _e, _s in survey)
    phfix.report(items, survey)
    assert 'Nothing on it is relinked' in capsys.readouterr().out

    phfix.apply(items, batch='PHFIX-TEST-1', actor='DIR12010')
    a2, b2, p2 = (db.session.get(Contact, x.id) for x in (a, b, p1))
    assert (a2.company, a2.company_id, a2.account_id) == (real.name, real.id, real.id)
    created = Company.query.filter_by(name='ZT Fried Spedition').one()
    assert b2.company_id == created.id and b2.account_id == created.id
    assert p2.account_id == facor.id
    assert db.session.get(Contact, jen.id).country == 'Finland'
    assert db.session.get(Lead, lead.id).country == 'Finland'

    phfix.undo('PHFIX-TEST-1', 'DIR12010')
    a3 = db.session.get(Contact, a.id)
    assert (a3.company, a3.company_id) == ('New Lead', holder.id)
    assert db.session.get(Lead, lead.id).country == 'India'
    assert db.session.get(Company, created.id).is_active is False   # never deleted


def test_a_placeholder_account_is_never_the_answer():
    _account(name='Unknown', email_domains=['ztnowhere.example'])
    found, why = phfix.find_account('Unknown', 'ztnowhere.example')
    assert found is None
