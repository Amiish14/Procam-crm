"""No route answers without an access check unless it is deliberately
public. Guards against the next endpoint that forgets one."""
import io
import contextlib
import os
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_every_route_has_a_detected_gate_or_is_listed_public():
    code = (
        "import sys, io, contextlib; sys.path.insert(0, 'scripts')\n"
        "import generate_reference_docs as g\n"
        "g.build()\n"
        "import app as A\n"
        "print('UNGATED', g.ungated(A.app))\n")
    env = dict(os.environ, DATABASE_URL='sqlite://', SECRET_KEY='x' * 40,
               ADMIN_INITIAL_PASSWORD='RouteGateTest-12345')
    out = subprocess.run([sys.executable, '-c', code], cwd=_ROOT, env=env,
                         capture_output=True, text=True, timeout=180)
    line = [l for l in out.stdout.splitlines() if l.startswith('UNGATED')]
    assert line, out.stderr[-600:]
    assert line[0] == 'UNGATED []', (
        line[0] + ' — add an access check, or list the route in '
        'PUBLIC_ROUTES in scripts/generate_reference_docs.py with a reason')
