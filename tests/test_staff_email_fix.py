"""Every employee's real address, found from evidence or not at all.

88 of 132 employee rows on the server carry `emp<code>@procamgroup.in`
and 13 carry nothing. Those are not mailboxes: the first install
seeded employees from the PRERNA master and filled a missing address
from the employee code, so every notification the CRM sent to those
people went nowhere, and nothing ever complained because the value
looked like an address and was unique.

THE EVIDENCE IS ONLY ON THE SERVER. Every mail table on a development
machine is empty -- `lead_emails`, `lead_raw_emails`,
`mail_ingest_log`, `email_classifications`, all zero rows -- so the
dry run here reports 112 unresolved and nothing else, and says almost
nothing about whether the script works. So this file seeds the
evidence itself and exercises the whole path: what is found, what is
refused, what `--apply` writes, and what `--undo` puts back.
"""
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'staff-email-test-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'StaffEmailTest12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'staff_email.db'))

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Employee = _main.Employee

# Loaded BY PATH, not by putting scripts/ on sys.path: there is a
# `scripts/email_ingest.py`, and on the path it shadows the
# `email_ingest` PACKAGE that app imports -- the whole app fails to
# import with "'email_ingest' is not a package".
import importlib.util                                      # noqa: E402
_spec = importlib.util.spec_from_file_location(
    'staff_email_fix',
    os.path.join(_ROOT, 'scripts', '2026_11_19_staff_email_fix.py'))
fix = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fix)

DOMAINS = {'procamgroup.in', 'procamlogistics.com'}

#: Nobody real. The rule is no employee names or codes in code, seeds
#: or tests, so these are invented people with invented codes.
STAFF = [
    ('ZZ001', 'Asha Venkatraman'),     # found by a forwarded mail
    ('ZZ002', 'Bikram Sanyal'),        # found by a display name on a header
    ('ZZ003', 'Chandni Rao'),          # two addresses: a conflict
    ('ZZ004', 'Devdas Pillai'),        # no evidence at all
    ('ZZ005', 'Esha Fernandes'),       # evidence, but the address is taken
    ('ZZ006', 'Farhan Qureshi'),       # already real: must be left alone
]


@pytest.fixture()
def world():
    with flask_app.app_context():
        db.create_all()
        Employee.query.filter(Employee.emp_code.in_(
            [c for c, _ in STAFF] + ['ZZ999'])).delete()
        db.session.commit()
        for code, name in STAFF:
            e = Employee(emp_code=code, name=name, is_active=True,
                         role='user', vertical='All', industries='[]')
            if code == 'ZZ006':
                e.email = 'farhan.q@procamlogistics.com'
            elif code == 'ZZ004':
                e.email = None                       # no address at all
            else:
                e.email = f'emp{code[2:]}2020@procamgroup.in'
            e.set_password('x' * 20)
            db.session.add(e)
        # The row that already owns the address ZZ005's evidence points
        # at. `employees.email` is unique, so writing it twice would
        # fail the transaction and take the whole batch with it.
        holder = Employee(emp_code='ZZ999', name='Esha F Duplicate',
                          email='esha.f@procamgroup.in', is_active=True,
                          role='user', vertical='All', industries='[]')
        holder.set_password('x' * 20)
        db.session.add(holder)
        db.session.commit()
        _seed_evidence()
        yield
        with flask_app.app_context():
            pass


def _seed_evidence():
    """The evidence, written straight into the tables the script reads."""
    from sqlalchemy import text

    # `lead_emails.lead_id` is NOT NULL, so the mail has to hang on a
    # lead. One is enough: the script reads the headers, not the lead.
    Lead = _main.Lead
    lead = Lead.query.filter_by(company='ZZ Test Cargo').first()
    if lead is None:
        lead = Lead(company='ZZ Test Cargo', stage='New Opportunity',
                    source='email')
        db.session.add(lead)
        db.session.commit()
    lid = lead.id

    # Idempotent: this fixture runs per test, and the evidence tables
    # have unique keys of their own -- a second insert of the same
    # message id fails and takes the fixture with it.
    db.session.execute(text(f'DELETE FROM lead_emails WHERE lead_id = {lid}'))
    db.session.execute(text(
        "DELETE FROM mail_ingest_log WHERE internet_message_id = '<a@t>'"))
    db.session.commit()

    rows = [
        # ZZ001: the ingest paired a CODE with an address. The strongest
        # kind, and it needs no name matching.
        ("INSERT INTO mail_ingest_log (internet_message_id, from_addr, "
         "outcome, forwarded_by, forwarded_by_emp) "
         "VALUES ('<a@t>', 'customer@acme.example', 'created', "
         "'asha.v@procamgroup.in', 'ZZ001')"),
        # ZZ002: a display name on a header, on our own domain.
        ("INSERT INTO lead_emails (lead_id, direction, from_addr, to_addr, subject) "
         f"VALUES ({lid}, 'in', 'Bikram Sanyal <bikram.s@procamlogistics.com>', "
         "'leads@procamgroup.in', 'Rate request')"),
        ("INSERT INTO lead_emails (lead_id, direction, from_addr, to_addr, subject) "
         f"VALUES ({lid}, 'in', 'someone@acme.example', "
         "'\"Bikram Sanyal\" <bikram.s@procamlogistics.com>', 'Re: rates')"),
        # ZZ003: two different addresses under the same name.
        ("INSERT INTO lead_emails (lead_id, direction, from_addr, to_addr, subject) "
         f"VALUES ({lid}, 'in', 'Chandni Rao <chandni.rao@procamgroup.in>', "
         "'leads@procamgroup.in', 'One')"),
        ("INSERT INTO lead_emails (lead_id, direction, from_addr, to_addr, subject) "
         f"VALUES ({lid}, 'in', 'Chandni Rao <c.rao@procamlogistics.com>', "
         "'leads@procamgroup.in', 'Two')"),
        # ZZ005: evidence for an address another row already holds.
        ("INSERT INTO lead_emails (lead_id, direction, from_addr, to_addr, subject) "
         f"VALUES ({lid}, 'in', 'Esha Fernandes <esha.f@procamgroup.in>', "
         "'leads@procamgroup.in', 'Three')"),
        # A customer with a display name, to prove nothing is matched
        # to an employee by accident.
        ("INSERT INTO lead_emails (lead_id, direction, from_addr, to_addr, subject) "
         f"VALUES ({lid}, 'in', 'Asha Venkatraman <asha@acme.example>', "
         "'leads@procamgroup.in', 'Not ours')"),
    ]
    for sql in rows:
        try:
            db.session.execute(text(sql))
        except Exception:
            db.session.rollback()
            raise
    db.session.commit()


def _survey(everyone=False):
    with flask_app.app_context():
        return fix.survey(DOMAINS, tms=None, everyone=everyone)


def _row(rows, code):
    for r in rows:
        if r['emp_code'] == code:
            return r
    return None


# ── what counts as a made-up address ─────────────────────────────────
@pytest.mark.parametrize('addr', [
    'emp2622020@procamgroup.in', 'EMP472012@procamgroup.in',
    'emp1@procamlogistics.com', 'emp999@anything.example',
])
def test_the_seeded_shape_is_recognised(addr):
    assert fix.looks_generated(addr)


@pytest.mark.parametrize('addr', [
    'asha.v@procamgroup.in', 'employment@procamgroup.in',
    'emp.loyee@procamgroup.in', 'empire@procamlogistics.com', '',
])
def test_a_real_address_is_not_mistaken_for_one(addr):
    """`emp` followed by DIGITS and then `@`. "employment@" and
    "empire@" start with the same three letters and are addresses
    somebody uses."""
    assert not fix.looks_generated(addr)


# ── matching a person to a written name ──────────────────────────────
@pytest.mark.parametrize('written,name', [
    ('Asha Venkatraman', 'Asha Venkatraman'),
    ('ASHA VENKATRAMAN', 'Asha Venkatraman'),
    ('Venkatraman, Asha', 'Asha Venkatraman'),
    ('Mr Asha Venkatraman', 'Asha Venkatraman'),
    ('Bala Murugan', 'Bala Murugan T'),
])
def test_the_same_person_written_differently(written, name):
    assert fix.name_matches(written, name)


@pytest.mark.parametrize('written,name', [
    ('Asha', 'Asha Venkatraman'),          # one word is not enough
    ('Asha Pillai', 'Asha Venkatraman'),
    ('Devdas Pillai', 'Asha Venkatraman'),
    ('', 'Asha Venkatraman'),
    ('Asha Venkatraman', ''),
])
def test_two_different_people_are_not_matched(written, name):
    """One word in common is not a person. This book has several of
    every first name, and a wrong address is worse than none: mail
    meant for one colleague would go to another."""
    assert not fix.name_matches(written, name)


# ── the survey ───────────────────────────────────────────────────────
def test_only_the_broken_rows_are_surveyed(world):
    rows, _ = _survey()
    codes = {r['emp_code'] for r in rows}
    assert 'ZZ006' not in codes, 'a real address was surveyed for replacement'
    for code in ('ZZ001', 'ZZ002', 'ZZ003', 'ZZ004', 'ZZ005'):
        assert code in codes, code


def test_a_row_with_no_address_at_all_is_included(world):
    rows, _ = _survey()
    assert _row(rows, 'ZZ004')['current_kind'] == 'empty'


def test_the_code_keyed_evidence_wins_with_no_name_matching(world):
    """`mail_ingest_log.forwarded_by_emp` is an employee code and
    `forwarded_by` the address they forwarded from -- paired by the
    ingest itself, nothing inferred."""
    r = _row(_survey()[0], 'ZZ001')
    assert r['proposed'] == 'asha.v@procamgroup.in'
    assert r['source'] == 'forwarded'
    assert not r['conflict']


def test_a_display_name_on_a_header_is_evidence(world):
    r = _row(_survey()[0], 'ZZ002')
    assert r['proposed'] == 'bikram.s@procamlogistics.com'
    assert r['source'] == 'header'
    assert r['evidence_count'] >= 2, 'both the From and the To should count'


def test_an_address_off_our_domains_is_not_evidence(world):
    """"Asha Venkatraman <asha@acme.example>" is a customer who shares
    a name, and acme.example is not ours."""
    r = _row(_survey()[0], 'ZZ001')
    assert 'acme.example' not in r['all_candidates']


def test_two_addresses_for_one_person_is_a_conflict(world):
    rows, unresolved = _survey()
    r = _row(rows, 'ZZ003')
    assert r['conflict'], 'two addresses were silently reconciled'
    assert r['proposed'] == '', 'a conflict must propose nothing'
    assert 'chandni.rao@procamgroup.in' in r['all_candidates']
    assert 'c.rao@procamlogistics.com' in r['all_candidates']
    assert 'ZZ003' in [u['emp_code'] for u in unresolved]


def test_an_address_another_row_holds_is_a_conflict(world):
    """`employees.email` is unique. Writing it twice would fail the
    transaction and take the whole batch with it."""
    r = _row(_survey()[0], 'ZZ005')
    assert r['proposed'] == ''
    assert 'already on ZZ999' in r['conflict']


def test_a_row_with_no_evidence_says_so(world):
    rows, unresolved = _survey()
    r = _row(rows, 'ZZ004')
    assert r['proposed'] == ''
    said = [u for u in unresolved if u['emp_code'] == 'ZZ004'][0]
    assert 'no evidence' in said['why']


def test_everything_unproposed_reaches_the_hr_list(world):
    """Nobody is dropped between the two files: a row either has a
    proposal or is on the list for HR."""
    rows, unresolved = _survey()
    proposed = {r['emp_code'] for r in rows if r['proposed']}
    listed = {u['emp_code'] for u in unresolved}
    assert proposed | listed == {r['emp_code'] for r in rows}
    assert not (proposed & listed)


# ── --apply, which is the half a dry run says nothing about ──────────
def _emails():
    with flask_app.app_context():
        return {e.emp_code: (e.email or '')
                for e in Employee.query.filter(
                    Employee.emp_code.like('ZZ%')).all()}


def _apply(rows=None, batch='SEML-TEST-1', actor='DIR12010'):
    with flask_app.app_context():
        if rows is None:
            rows, _ = fix.survey(DOMAINS, tms=None)
        return fix.apply(rows, batch=batch, actor=actor)


def test_apply_writes_the_unconflicted_proposals(world):
    assert _apply() == 2, 'the two resolvable rows should be written'
    now = _emails()
    assert now['ZZ001'] == 'asha.v@procamgroup.in'
    assert now['ZZ002'] == 'bikram.s@procamlogistics.com'


def test_apply_leaves_a_conflict_exactly_as_it_was(world):
    before = _emails()
    _apply()
    after = _emails()
    for code in ('ZZ003', 'ZZ004', 'ZZ005'):
        assert after[code] == before[code], code


def test_apply_never_touches_a_real_address(world):
    _apply()
    assert _emails()['ZZ006'] == 'farhan.q@procamlogistics.com'


def test_apply_writes_an_audit_row_for_each_change(world):
    from app.models.audit import AuditEvent
    _apply(batch='SEML-TEST-AUD')
    with flask_app.app_context():
        rows = [e for e in AuditEvent.query.filter(
            AuditEvent.action == fix.ACTION).all()
            if isinstance(e.new_value, dict)
            and e.new_value.get('batch') == 'SEML-TEST-AUD']
    assert len(rows) == 2
    for ev in rows:
        assert ev.actor == 'DIR12010'
        assert ev.old_value.get('email') is not None, 'the old value is kept'
        assert ev.new_value.get('source'), 'the audit says WHY'
        assert 'batch SEML-TEST-AUD' in (ev.reason or '')


def test_apply_refuses_a_row_somebody_edited_since_the_survey(world):
    """A person's address is a decision; this script's is a guess from
    evidence, and the guess must not overwrite the decision."""
    with flask_app.app_context():
        rows, _ = fix.survey(DOMAINS, tms=None)
        emp = Employee.query.filter_by(emp_code='ZZ001').first()
        emp.email = 'asha.venkatraman@procamlogistics.com'
        db.session.commit()
        written = fix.apply(rows, batch='SEML-TEST-EDIT', actor='DIR12010')
    assert written == 1, 'only ZZ002 should have been written'
    assert _emails()['ZZ001'] == 'asha.venkatraman@procamlogistics.com'


def test_apply_refuses_an_address_taken_since_the_survey(world):
    """`employees.email` is unique. Writing a duplicate would fail the
    transaction and lose the whole batch, so it is checked again at
    the moment of writing and not only in the survey."""
    with flask_app.app_context():
        rows, _ = fix.survey(DOMAINS, tms=None)
        holder = Employee.query.filter_by(emp_code='ZZ999').first()
        holder.email = 'asha.v@procamgroup.in'
        db.session.commit()
        written = fix.apply(rows, batch='SEML-TEST-TAKEN', actor='DIR12010')
    assert written == 1
    assert _emails()['ZZ001'].startswith('emp')


# ── --undo --batch ───────────────────────────────────────────────────
def test_undo_puts_every_address_back(world):
    before = _emails()
    _apply(batch='SEML-TEST-UNDO')
    assert _emails() != before
    with flask_app.app_context():
        back = fix.undo('SEML-TEST-UNDO', 'DIR12010')
    assert back == 2
    assert _emails() == before, 'the undo did not restore what was there'


def test_undo_restores_an_empty_address_as_empty_not_as_a_blank(world):
    """ZZ004 has no address and must still have none. A row that held
    nothing must come back holding nothing, not an empty string:
    `email` is unique, and two empty strings are a duplicate while two
    NULLs are not."""
    with flask_app.app_context():
        emp = Employee.query.filter_by(emp_code='ZZ004').first()
        emp.email = None
        db.session.commit()
        rows, _ = fix.survey(DOMAINS, tms=None)
        for r in rows:                      # give ZZ004 an answer
            if r['emp_code'] == 'ZZ004':
                r['_addr'] = r['proposed'] = 'devdas.p@procamgroup.in'
                r['source'], r['evidence_count'] = 'forwarded', 1
                r['conflict'] = ''
        fix.apply(rows, batch='SEML-TEST-NULL', actor='DIR12010')
        assert Employee.query.filter_by(
            emp_code='ZZ004').first().email == 'devdas.p@procamgroup.in'
        fix.undo('SEML-TEST-NULL', 'DIR12010')
        assert Employee.query.filter_by(emp_code='ZZ004').first().email is None


def test_undo_leaves_alone_what_was_changed_after_the_batch(world):
    _apply(batch='SEML-TEST-LATER')
    with flask_app.app_context():
        emp = Employee.query.filter_by(emp_code='ZZ001').first()
        emp.email = 'someone.else@procamgroup.in'
        db.session.commit()
        back = fix.undo('SEML-TEST-LATER', 'DIR12010')
    assert back == 1, 'only the untouched one should be reverted'
    assert _emails()['ZZ001'] == 'someone.else@procamgroup.in'


def test_undo_writes_its_own_audit_row(world):
    from app.models.audit import AuditEvent
    _apply(batch='SEML-TEST-AUD2')
    with flask_app.app_context():
        fix.undo('SEML-TEST-AUD2', 'DIR12010')
        rows = AuditEvent.query.filter(
            AuditEvent.action == f'{fix.ACTION}_undo').all()
    assert len(rows) >= 2
    assert all(e.actor == 'DIR12010' for e in rows)


def test_undo_of_an_unknown_batch_changes_nothing(world):
    before = _emails()
    with flask_app.app_context():
        assert fix.undo('SEML-NOT-A-BATCH', 'DIR12010') == 0
    assert _emails() == before


# ── nothing generates one of these any more ──────────────────────────
def test_the_seeder_leaves_a_missing_address_missing():
    """The line that made the mess: `email=em or
    f"{ec.lower()}@procamgroup.in"` in the employee seed. A missing
    address is now left missing, which is at least true."""
    body = open(os.path.join(_ROOT, 'app.py')).read()
    block = body.split('EMPLOYEES = _load_seed_employees')[1][:3000]
    assert "email=(em or '').strip() or None" in block
    assert 'lower()}@procamgroup.in"' not in block.replace(
        '# `em or f"{ec.lower()}@procamgroup.in"`, which gave', '')


def test_no_product_code_builds_an_address_from_an_employee_code():
    """Across app.py and app/: anything that creates an Employee must
    leave the address empty rather than invent one. Comments are
    stripped first -- the one in the seeder quotes the old line on
    purpose, and a test nobody can edit a comment past is a test that
    gets edited instead of the code."""
    import glob
    import re
    bad = []
    files = [os.path.join(_ROOT, 'app.py')]
    files += glob.glob(os.path.join(_ROOT, 'app', '**', '*.py'),
                       recursive=True)
    pattern = re.compile(
        r"""(?:emp_?code|\bec\b|\bcode\b)[^\n]{0,40}\.lower\(\)[^\n]{0,20}"""
        r"""[+}][^\n]{0,8}['"]?@""")
    for path in files:
        for n, line in enumerate(open(path).read().splitlines(), 1):
            said = line.split('#', 1)[0]
            if pattern.search(said):
                bad.append(f'{os.path.relpath(path, _ROOT)}:{n}: '
                           f'{said.strip()[:70]}')
    assert bad == [], ('these build an address out of an employee code:\n  '
                       + '\n  '.join(bad))


def test_every_employee_creation_site_stores_none_for_empty():
    """`email` is unique, so '' twice is a duplicate and NULL twice is
    not -- which is how the second employee without an address used to
    fail with a server error."""
    body = open(os.path.join(_ROOT, 'app.py')).read()
    sites = body.count('emp = Employee(') + body.count('e = Employee(')
    assert sites >= 2, 'the creation sites moved; check this test'
    assert body.count("or '').strip() or None") >= 2


def test_a_generated_address_is_never_proposed_as_an_answer(world):
    """The script reads addresses out of mail headers, and the mailbox
    has seen plenty of mail FROM these made-up addresses -- the CRM
    sent it. Proposing one as the fix would be a loop."""
    from sqlalchemy import text
    with flask_app.app_context():
        lead = _main.Lead.query.filter_by(company='ZZ Test Cargo').first()
        db.session.execute(text(
            "INSERT INTO lead_emails (lead_id, direction, from_addr, "
            "to_addr, subject) VALUES "
            f"({lead.id}, 'out', "
            "'Devdas Pillai <emp0042020@procamgroup.in>', "
            "'customer@acme.example', 'Quotation')"))
        db.session.commit()
        rows, unresolved = fix.survey(DOMAINS, tms=None)
    r = _row(rows, 'ZZ004')
    assert r['proposed'] == ''
    assert 'emp0042020' not in r['all_candidates']
    assert 'ZZ004' in [u['emp_code'] for u in unresolved]


# ── the TMS source, which runs the other way ─────────────────────────
def test_the_tms_is_reported_unreachable_rather_than_skipped(monkeypatch):
    """The integration API is INBOUND: the TMS calls the CRM. There is
    no outbound base URL, token or client here, so "if reachable" can
    only mean "when a deployment has configured one"."""
    monkeypatch.delenv('TMS_BASE_URL', raising=False)
    monkeypatch.delenv('TMS_TOKEN', raising=False)
    rows, note = fix.tms_employees()
    assert rows is None
    assert 'not configured' in note
    assert 'inbound' in note


def test_a_tms_answer_is_used_when_one_is_given(world):
    """The source itself works; only the reaching of it is in doubt."""
    with flask_app.app_context():
        rows, _ = fix.survey(DOMAINS, tms={'ZZ004': 'd.pillai@procamgroup.in'})
    r = _row(rows, 'ZZ004')
    assert r['proposed'] == 'd.pillai@procamgroup.in'
    assert r['source'] == 'tms'


def test_a_tms_answer_does_not_beat_the_ingest(world):
    """A code the ingest itself paired with an address outranks a list
    from another application, which may be as stale as this one was."""
    with flask_app.app_context():
        rows, _ = fix.survey(DOMAINS, tms={'ZZ001': 'stale@procamgroup.in'})
    r = _row(rows, 'ZZ001')
    assert r['proposed'] == 'asha.v@procamgroup.in'
    assert r['source'] == 'forwarded'


def test_the_seeders_own_expression_keeps_nothing_as_nothing():
    """The seed CSV is not on a development machine, so the expression
    itself is exercised rather than a fresh boot. `None`, not `''`:
    `email` is unique, and two empty strings are a duplicate while two
    NULLs are not."""
    def seeded(em):
        return (em or '').strip() or None

    assert seeded('') is None
    assert seeded(None) is None
    assert seeded('   ') is None
    assert seeded(' asha.v@procamgroup.in ') == 'asha.v@procamgroup.in'
