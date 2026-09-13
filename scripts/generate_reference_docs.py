"""
Generate the API and database reference from the code itself.

    .venv/bin/python scripts/generate_reference_docs.py
    .venv/bin/python scripts/generate_reference_docs.py --check   # CI: stale?

Writes
    docs/reference/API.md      every route: methods, path, the access gate
                               found in its code, and the first line of
                               its docstring
    docs/reference/SCHEMA.md   every table: columns, types, nullability,
                               indexes, foreign keys

Hand-written reference drifts from the code within a release; generated
reference cannot. The app is imported against an in-memory database, so
running this never touches a real one.
"""
import argparse
import inspect
import io
import contextlib
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

OUT_DIR = os.path.join(_ROOT, 'docs', 'reference')

_GATES = (
    (re.compile(r"require_super"), 'super admin'),
    (re.compile(r"require_permission\('([^']+)'\)"), 'permission {0}'),
    (re.compile(r"require\(\s*'([^']+)'"), 'permission {0}'),
    (re.compile(r"require\(\s*(PERM|_PERM|AUDIT_PERM)\b"), 'permission ({0})'),
    (re.compile(r"_require_perm\('([^']+)'\)"), 'permission {0}'),
    (re.compile(r"_require_lead_access"), 'lead access (scope)'),
    (re.compile(r"_rfq_or_404|may_view_rfq"), 'RFQ access (records)'),
    (re.compile(r"_quote_or_404|may_view_quote"), 'quote access (records)'),
    (re.compile(r"_handover_or_404|may_view_handover"),
     'handover access (records)'),
    (re.compile(r"_card_or_404"), 'uploader or company scope'),
    (re.compile(r"_matrix_can\('([^']+)'\)"), 'permission {0}'),
    (re.compile(r"@_require_admin"), 'administrator (matrix)'),
    (re.compile(r"@_action\b"), 'permission reports.action'),
    (re.compile(r"@_competitor\b"), 'permission reports.competitor'),
    (re.compile(r"@_accounts\b"), 'permission reports.accounts'),
    (re.compile(r"@_any_report\b"), 'any report permission'),
    (re.compile(r"@_require_manager\b"), 'manager (reports)'),
    (re.compile(r"_current_emp\(\)[\s\S]{0,90}(redirect\(url_for\('login'|, 401)"),
     'signed in (checked inline)'),
    (re.compile(r"session\.get\('emp_code'\)|_current_emp_code\(\)|"
                r"'emp_code' not in session"), 'signed in (checked inline)'),
    (re.compile(r"require_auth|_login_required|_require_login|_authed\(\)"),
     'signed in'),
)


def _gates(fn):
    """Access gates visible in the view and its decorators' names."""
    found = []
    target = inspect.unwrap(fn)
    try:
        src = inspect.getsource(target)
    except (OSError, TypeError):
        src = ''
    # decorators live just above the def in the module source
    try:
        lines, start = inspect.getsourcelines(target)
        mod_lines = inspect.getsourcelines(inspect.getmodule(target))[0]
        above = ''.join(mod_lines[max(0, start - 6):start - 1])
    except (OSError, TypeError):
        above = ''
    text = above + src
    for rx, label in _GATES:
        for m in rx.finditer(text):
            name = label.format(*m.groups()) if m.groups() else label
            if name not in found:
                found.append(name)
    return found


#: Routes that answer without signing in, on purpose.
PUBLIC_ROUTES = {
    '/login', '/healthz', '/api/email/webhook', '/api/csp-report', '/offline',
    '/sw.js', '/', '/leads/<int:lid>',
    # vocabularies with no customer data, used by the login-free pages
    '/api/config/stages', '/api/config/states', '/api/config/loss-reasons',
    # help text shown next to form fields; filtered by role visibility
    '/api/help/tooltips', '/help', '/help/<slug>',
}


def ungated(app):
    """Routes with no detected access gate that are not deliberately
    public — each one is either a missing check or a pattern this script
    should learn."""
    out = []
    for rule in app.url_map.iter_rules():
        if rule.endpoint == 'static' or rule.rule in PUBLIC_ROUTES:
            continue
        fn = app.view_functions.get(rule.endpoint)
        if fn is not None and not _gates(fn):
            out.append(rule.rule)
    return sorted(set(out))


def api_markdown(app):
    rows = []
    for rule in sorted(app.url_map.iter_rules(), key=lambda r: r.rule):
        if rule.endpoint == 'static':
            continue
        fn = app.view_functions.get(rule.endpoint)
        methods = sorted(m for m in rule.methods
                         if m not in ('HEAD', 'OPTIONS'))
        doc = (inspect.getdoc(inspect.unwrap(fn)) or '').strip() if fn else ''
        first = doc.splitlines()[0] if doc else ''
        gates = _gates(fn) if fn else []
        public = rule.rule in PUBLIC_ROUTES
        rows.append((rule.rule, ', '.join(methods),
                     'public' if public and not gates else
                     ('; '.join(gates) or '—'), first))
    api = [r for r in rows if r[0].startswith('/api/')]
    pages = [r for r in rows if not r[0].startswith('/api/')]

    def table(items):
        out = ['| Path | Methods | Access gate | Purpose |',
               '|---|---|---|---|']
        for path, methods, gate, first in items:
            out.append(f'| `{path}` | {methods} | {gate} | '
                       f'{first.replace("|", "/")} |')
        return '\n'.join(out)

    return '\n'.join([
        '# API and page reference', '',
        'Generated by `scripts/generate_reference_docs.py` from the running '
        'code — do not edit by hand. Paths are relative to the deployment '
        'prefix (`/CRM` in production).', '',
        'Access gates are the checks found in each view\'s own code and '
        'decorators. Record visibility inside a list is applied by '
        '`app/access/scope.py` and `app/access/records.py`; see '
        '[RBAC](../RBAC.md). Every `/api/` error answers JSON.', '',
        f'{len(api)} API routes, {len(pages)} pages.', '',
        '## API', '', table(api), '', '## Pages', '', table(pages), ''])


def schema_markdown(db):
    out = ['# Database schema', '',
           'Generated by `scripts/generate_reference_docs.py` from the '
           'SQLAlchemy models — do not edit by hand. Tables created only by '
           'raw SQL at boot (KPI tables) are not listed.', '']
    tables = sorted(db.metadata.sorted_tables, key=lambda t: t.name)
    out.append(f'{len(tables)} tables.')
    out.append('')
    out.append(' · '.join(f'[{t.name}](#{t.name})' for t in tables))
    for t in tables:
        out += ['', f'## {t.name}', '',
                '| Column | Type | Null | Key | Default |',
                '|---|---|---|---|---|']
        for c in t.columns:
            key = []
            if c.primary_key:
                key.append('PK')
            for fk in c.foreign_keys:
                key.append(f'FK → {fk.target_fullname}')
            if c.unique:
                key.append('unique')
            if c.index:
                key.append('indexed')
            default = ''
            if c.default is not None and getattr(c.default, 'arg', None) \
                    is not None and not callable(c.default.arg):
                default = repr(c.default.arg)
            out.append(f'| {c.name} | {c.type} | '
                       f'{"yes" if c.nullable else "no"} | '
                       f'{", ".join(key)} | {default} |')
        multi = [i for i in t.indexes if len(i.columns) > 1]
        if multi:
            out.append('')
            out.append('Composite indexes: ' + '; '.join(
                f'`{i.name}` ({", ".join(c.name for c in i.columns)})'
                for i in multi))
    out.append('')
    return '\n'.join(out)


def build():
    os.environ['DATABASE_URL'] = 'sqlite://'
    os.environ.setdefault('SECRET_KEY', 'reference-docs-' + 'x' * 32)
    os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'ReferenceDocs-123456')
    import logging
    logging.disable(logging.CRITICAL)
    with contextlib.redirect_stdout(io.StringIO()):
        import app as A
        for mod in ('app.models.audit', 'app.models.copilot',
                    'app.models.rbac', 'app.models.rfq', 'app.models.quote',
                    'app.models.tms_handover', 'app.models.business_card',
                    'app.models.access', 'app.models.master_data',
                    'app.models.training', 'app.models.task_engine',
                    'app.models.competitor', 'app.models.data_mapping',
                    'app.models.notification', 'app.models.help_content',
                    'app.models.public_source', 'app.models.data_quality',
                    'presales.models'):
            try:
                __import__(mod)
            except Exception:
                pass
    return {'API.md': api_markdown(A.app), 'SCHEMA.md': schema_markdown(A.db)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true',
                    help='exit 1 if the committed reference is out of date')
    args = ap.parse_args()
    docs = build()
    os.makedirs(OUT_DIR, exist_ok=True)
    stale = []
    for name, text in docs.items():
        path = os.path.join(OUT_DIR, name)
        current = open(path).read() if os.path.exists(path) else None
        if current != text:
            stale.append(name)
            if not args.check:
                with open(path, 'w') as fh:
                    fh.write(text)
    if args.check:
        print('stale: ' + ', '.join(stale) if stale else 'up to date')
        return 1 if stale else 0
    print('written: ' + (', '.join(stale) or 'nothing changed'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
