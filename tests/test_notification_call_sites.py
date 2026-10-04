"""Every rule in the matrix is fired by something.

The matrix is shown to administrators on `/admin/email/rules`, where a
row says who hears about an event. A row that nothing fires is a
promise the software does not keep, so this module checks the claim two
ways: that no row is marked as wired without naming where, and that the
places it names still exist.
"""
import os
import re
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'callsites-test-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'CallSitesTest12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'callsites.db'))

from app.services import notification_rules as nrules     # noqa: E402

#: Events raised by a timer rather than by a route. Their call site is
#: a script, and the script names the report rather than the event.
SCHEDULED = tuple(r['event'] for r in nrules.matrix_rows()
                  if r['source'] == nrules.S_SCHEDULED)


def _sources():
    """Every .py under the parts of the tree that can raise an event."""
    text = []
    for folder in ('app', 'scripts', 'presales', 'email_ingest'):
        root = os.path.join(_ROOT, folder)
        for base, _dirs, files in os.walk(root):
            if '__pycache__' in base:
                continue
            for name in files:
                if name.endswith('.py') and name != 'notification_rules.py':
                    with open(os.path.join(base, name), encoding='utf-8',
                              errors='replace') as fh:
                        text.append(fh.read())
    with open(os.path.join(_ROOT, 'app.py'), encoding='utf-8') as fh:
        text.append(fh.read())
    return '\n'.join(text)


def test_a_rule_marked_wired_says_where():
    missing = [r['event'] for r in nrules.matrix_rows()
               if r['implemented'] and not (r['call_site'] or '').strip()]
    assert not missing, (
        'these rules claim to be wired but name no call site: ' +
        ', '.join(missing))


def test_every_event_rule_is_actually_dispatched_somewhere():
    """An event rule — one that fires at the moment something happens —
    must appear in a dispatch() call. Scheduled rules are raised by the
    report and escalation jobs, which address them by role instead."""
    body = _sources()
    # Two ways a rule is raised: through dispatch(), or by a caller that
    # addresses notify.send directly with the same event key. Both are
    # real call sites; only a rule that appears in neither is a promise
    # nothing keeps.
    fired = set(re.findall(r"dispatch\(\s*'([a-z_.]+)'", body))
    fired |= set(re.findall(r"event_key='([a-z_.]+)'", body))
    orphans = [r['event'] for r in nrules.matrix_rows()
               if r['source'] == nrules.S_EVENT
               and r['event'] not in fired
               and 'lead_assignment' not in (r['call_site'] or '')]
    assert not orphans, (
        'these event rules are in the matrix but nothing raises them: ' +
        ', '.join(orphans))


def test_the_named_file_exists():
    for row in nrules.matrix_rows():
        site = (row['call_site'] or '').split('::')[0].split(',')[0].strip()
        if not site.endswith('.py'):
            continue
        assert os.path.exists(os.path.join(_ROOT, site)), (
            f"{row['event']} names {site}, which is not there")


def test_only_the_rows_that_should_carry_the_original_do():
    """A client's document goes out only where the recipient's next job
    is to read the request. Anywhere else it is a leak with a reason."""
    carry = {r['event'] for r in nrules.matrix_rows()
             if r.get('attach_original')}
    assert carry == {'lead.assigned', 'lead.assigned_secondary'}, carry
