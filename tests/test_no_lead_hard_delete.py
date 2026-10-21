"""
Nothing destroys a lead any more.

830 leads were removed between September and October. 710 left a
snapshot in `deletion_audit`; 120 left nothing at all, so no one can
say who took them or why. All 830 are restorable only because a
backup happened to exist.

A lead that did not happen is a lead that did not happen, not an
absence. It closes as Lost with a reason, and the record stays.

This test is the thing that keeps that true. It reads the source
rather than exercising the routes, because the failure it guards
against is somebody adding a new delete in six months — which no
behavioural test would catch, since the route would not exist yet.
"""
import ast
import os
import re

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Where application code lives. Tests may delete their own rows;
#: fixtures that did not clean up are their own problem.
TREES = ('app', 'presales', 'scripts')
SINGLE = ('app.py',)

#: A migration's `--down` is allowed to drop what its `--up` made,
#: and the restore script is allowed to remove exactly the leads it
#: created, identified by its own audit rows. Both are named here
#: so an addition to this list is a decision somebody makes on
#: purpose, in a diff, with a reason.
ALLOWED = {
    'scripts/restore_deleted_leads.py':
        'its --down removes only the leads its --apply restored',
}
MIGRATION = re.compile(r'scripts/\d{4}_\d{2}_\d{2}_.*\.py$')


def _files():
    out = [os.path.join(_ROOT, f) for f in SINGLE]
    for tree in TREES:
        for base, _dirs, names in os.walk(os.path.join(_ROOT, tree)):
            if '__pycache__' in base:
                continue
            out += [os.path.join(base, n) for n in names
                    if n.endswith('.py')]
    return sorted(out)


def _rel(path):
    return os.path.relpath(path, _ROOT)


LEADISH = re.compile(r'\bLead\b')


def _lead_names(func):
    """Names inside one function that hold a Lead.

    Matching on the variable being called `lead` is too loose and
    too tight at once: `db.session.delete(row)` is a competitor
    contact in one file and would be a lead in the next. So the
    assignments are read — `row = db.session.get(Lead, lid)` makes
    `row` a lead, and `row = Contact.query...` does not.
    """
    names = set()
    for node in ast.walk(func):
        targets = []
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)):
            targets, value = [node.target], node.value
        elif isinstance(node, ast.For):
            targets, value = [node.target], node.iter
        else:
            continue
        if value is None:
            continue
        try:
            text = ast.unparse(value)
        except Exception:                                 # noqa: BLE001
            continue
        if not LEADISH.search(text):
            continue
        for t in targets:
            if isinstance(t, ast.Name):
                names.add(t.id)
    return names


def _deletes_leads(tree, src):
    """Calls that would remove a row from `leads`."""
    found = []
    scopes = [n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                ast.Module))]
    for scope in scopes:
        names = _lead_names(scope)
        for node in ast.walk(scope):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) \
                    or func.attr != 'delete':
                continue
            if node.args:
                arg = node.args[0]
                text = ast.unparse(arg)
                if (isinstance(arg, ast.Name) and arg.id in names) \
                        or LEADISH.search(text):
                    found.append(ast.unparse(node)[:90])
            else:
                # Lead.query.filter(...).delete()
                owner = ast.unparse(func.value)
                if LEADISH.search(owner):
                    found.append(ast.unparse(node)[:90])

    for hit in re.findall(r'DELETE\s+FROM\s+leads\b', src, re.I):
        found.append(hit)
    return sorted(set(found))


def test_no_code_path_can_hard_delete_a_lead():
    offenders = {}
    for path in _files():
        rel = _rel(path)
        if rel in ALLOWED or MIGRATION.match(rel):
            continue
        src = open(path).read()
        if 'Lead' not in src and 'leads' not in src:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:                               # pragma: no cover
            continue
        hits = _deletes_leads(tree, src)
        if hits:
            offenders[rel] = hits
    assert not offenders, (
        'These destroy leads. Close them as Lost instead — '
        '/api/leads/<id>/close-lost, or duplicate-of:\n  '
        + '\n  '.join(f'{k}: {v}' for k, v in offenders.items()))


def test_the_allowlist_names_only_what_it_should():
    """An entry here is a code path that can destroy a lead. Each
    one is a decision, so each one carries its reason."""
    for rel, why in ALLOWED.items():
        assert os.path.exists(os.path.join(_ROOT, rel)), rel
        assert len(why) > 20, f'{rel} has no reason recorded'
    assert len(ALLOWED) == 1, \
        'something new was allowed to delete leads — was that meant?'


def test_a_migration_down_is_still_allowed():
    """A dated migration may drop what it created; that is what
    reversible means."""
    assert MIGRATION.match('scripts/2026_10_20_ff_shipment.py')
    assert not MIGRATION.match('scripts/purge_everything.py')


def test_the_delete_endpoint_is_gone_and_says_where_to_go():
    src = open(os.path.join(_ROOT, 'app.py')).read()
    assert 'close_as_lost' in src
    assert '410' in src.split('def api_delete_lead')[1][:1200]
    body = src.split('def api_delete_lead')[1].split('@app.route')[0]
    assert 'db.session.delete' not in body
    assert 'DeletionAudit' not in body


def test_the_purge_script_closes_rather_than_deletes():
    src = open(os.path.join(_ROOT, 'scripts',
                            '2026_09_02_purge_irrelevant_leads.py')).read()
    assert 'Lost' in src
    assert 'Lead.query.filter(Lead.id.in_(ids)).delete' not in src


def test_bulk_delete_closes_and_reports_what_it_did():
    src = open(os.path.join(_ROOT, 'app', 'bulk_admin',
                            'service.py')).read()
    body = src.split('def delete(')[1].split('\ndef ')[0]
    assert 'Lead.query.filter(Lead.id.in_(ids)).delete' not in body
    assert "'closed'" in body


# ── the detector, tested against code that should trip it ────────────
def _detect(src):
    return _deletes_leads(ast.parse(src), src)


@pytest.mark.parametrize('src,why', [
    ('def f(lid):\n'
     '    lead = db.session.get(Lead, lid)\n'
     '    db.session.delete(lead)\n',
     'the obvious one'),
    ('def f(ids):\n'
     '    Lead.query.filter(Lead.id.in_(ids)).delete()\n',
     'a bulk query delete'),
    ('def f(lid):\n'
     '    row = db.session.get(Lead, lid)\n'
     '    db.session.delete(row)\n',
     'named row, but assigned from Lead'),
    ('def f(lid):\n'
     '    for row in Lead.query.filter_by(stage="Lost"):\n'
     '        db.session.delete(row)\n',
     'a loop variable'),
    ('def f():\n'
     '    db.session.execute(text("DELETE FROM leads WHERE id = 1"))\n',
     'raw SQL'),
])
def test_the_detector_catches_a_reintroduction(src, why):
    assert _detect(src), f'missed: {why}'


@pytest.mark.parametrize('src,why', [
    ('def f(cid):\n'
     '    row = db.session.get(Contact, cid)\n'
     '    db.session.delete(row)\n',
     'a contact is not a lead'),
    ('def f(q):\n'
     '    QuoteLine.query.filter_by(quote_id=q).delete()\n',
     'quote lines are not leads'),
    ('def f(lid):\n'
     '    LeadNote.query.filter_by(lead_id=lid).delete()\n',
     'a note hanging off a lead is not the lead'),
])
def test_the_detector_leaves_everything_else_alone(src, why):
    assert not _detect(src), f'false positive: {why}'
