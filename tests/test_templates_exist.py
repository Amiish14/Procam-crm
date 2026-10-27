"""
Every template a route renders must exist.

Two pages shipped to five pilot users returning 500: the routes
were written, the flags were switched on, and
`render_template('lead_journey.html')` named a file nobody had
created. Every test passed throughout, because no test asked for
the page — the route tests all checked the 404 that a flag-off
state produces, which is the one case that never reaches the
template.

So this reads the source for render_template(...) and checks the
file is on disk. It costs nothing and it closes the exact gap
that let a 500 reach people.
"""
import ast
import os
import re

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(_ROOT, 'templates')

#: Names built at runtime rather than written out, which this
#: check cannot resolve and must not guess at.
DYNAMIC = re.compile(r'[{%+]|\bf[\'"]')


def _render_calls():
    """[(file, template_name)] for every literal render_template."""
    found = []
    for base, _dirs, files in os.walk(_ROOT):
        if any(skip in base for skip in
               ('/.venv', '/node_modules', '/.git', '/tests')):
            continue
        for name in files:
            if not name.endswith('.py'):
                continue
            path = os.path.join(base, name)
            try:
                tree = ast.parse(open(path, encoding='utf-8').read())
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                fname = (getattr(fn, 'id', None)
                         or getattr(fn, 'attr', None))
                if fname not in ('render_template',
                                 'render_template_string'):
                    continue
                if fname == 'render_template_string' or not node.args:
                    continue
                first = node.args[0]
                if isinstance(first, ast.Constant) and \
                        isinstance(first.value, str):
                    found.append((os.path.relpath(path, _ROOT),
                                  first.value))
    return found


def test_every_rendered_template_is_on_disk():
    missing = []
    for source, template in _render_calls():
        if DYNAMIC.search(template):
            continue
        if not os.path.isfile(os.path.join(TEMPLATES, template)):
            missing.append(f'{source} renders {template!r}')
    assert not missing, (
        'these routes render a template that does not exist, which '
        'is a 500 for whoever opens the page:\n  '
        + '\n  '.join(sorted(missing)))


def test_the_check_would_catch_a_missing_one():
    """A detector that cannot fail proves nothing."""
    assert not os.path.isfile(
        os.path.join(TEMPLATES, 'definitely_not_a_template.html'))


def test_the_pilot_pages_are_all_present():
    for name in ('account_check.html', 'rfq_chain.html',
                 'lead_journey.html', 'rfq_reports.html'):
        assert os.path.isfile(os.path.join(TEMPLATES, name)), name
