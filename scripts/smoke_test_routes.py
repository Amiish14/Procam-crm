#!/usr/bin/env python3
"""
Walk every GET route in the CRM in-process and report what breaks.

Read-only: only GET, and anything whose endpoint looks mutating is skipped.
Impersonates a user by writing the Flask session directly, so no password
and no logged-in human is required.

    python scripts/smoke_test_routes.py                    # as an admin
    python scripts/smoke_test_routes.py --as EMP372011     # as a salesperson
    python scripts/smoke_test_routes.py --as EMP372011 --verbose

Exit code is the number of routes that returned 5xx.
"""
import argparse
import os
import re
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Never let a smoke test think it is the real server.
os.environ['URL_PREFIX'] = ''
# The test client speaks plain http, so a Secure-only session cookie would be
# dropped and every page would look like an anonymous redirect to /login.
os.environ['SESSION_COOKIE_SECURE'] = 'false'
# Talisman would 302 every http request to https for the same reason.
os.environ['TALISMAN_FORCE_HTTPS'] = 'false'

import app as app_module                      # noqa: E402
from app import app, db, Employee             # noqa: E402
from flask import got_request_exception       # noqa: E402

# ── routes we must not touch ────────────────────────────────────────────
SKIP_ENDPOINT_RE = re.compile(
    r'(?i)(logout|delete|purge|remove|send|dispatch|sync|reset|revoke|'
    r'unsubscribe|renew|poll|sweep|seed|migrate|backfill|import|export_all)'
)
SKIP_RULE_RE = re.compile(r'(?i)/(logout|sw\.js|static/)')

# param name → how to find a real value in the DB
_MODELS = {}


def _load_models():
    """Map class name → model, across app.py and the app/models package."""
    if _MODELS:
        return _MODELS
    # importing the blueprints registers every model on the shared db
    try:
        import app.models  # noqa: F401
    except Exception:
        pass
    try:
        for mapper in db.Model.registry.mappers:
            _MODELS[mapper.class_.__name__] = mapper.class_
    except Exception:
        pass
    return _MODELS


def _sample(model_name, attr='id', **filters):
    """Return one real value for <attr> from <model_name>, or None."""
    model = _load_models().get(model_name)
    if model is None:
        return None
    try:
        q = model.query
        if filters:
            q = q.filter_by(**filters)
        row = q.first()
        return getattr(row, attr, None) if row else None
    except Exception:
        return None


def build_param_values():
    """Resolve real ids for URL params, so detail pages get tested too."""
    return {
        'lead_id':        lambda: _sample('Lead'),
        'opp_id':         lambda: _sample('Opportunity'),
        'company_id':     lambda: _sample('Company'),
        'contact_id':     lambda: _sample('Contact'),
        'task_id':        lambda: _sample('TaskInstance'),
        'rfq_id':         lambda: _sample('RFQ'),
        'quote_id':       lambda: _sample('Quote'),
        'handover_id':    lambda: _sample('WonHandover'),
        'competitor_id':  lambda: _sample('CompetitorMaster'),
        'intel_id':       lambda: _sample('CompetitorIntelligence'),
        'notification_id':lambda: _sample('Notification'),
        'role_id':        lambda: _sample('Role'),
        'card_id':        lambda: _sample('BusinessCardImport'),
        'emp_code':       lambda: _sample('Employee', 'emp_code', is_active=True),
        'slug':           lambda: _sample('HelpArticle', 'slug'),
        'id':             lambda: _sample('Lead'),
    }


def resolve(param, converter, cache, resolvers):
    if param in cache:
        return cache[param]
    val = None
    fn = resolvers.get(param)
    if fn:
        try:
            val = fn()
        except Exception:
            val = None
    if val is None:                       # last resort by converter type
        val = {'int': 1, 'string': 'x', 'path': 'x'}.get(converter)
    cache[param] = val
    return val


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--as', dest='emp_code', default=None,
                    help='emp_code to impersonate (default: first active admin)')
    ap.add_argument('--verbose', action='store_true',
                    help='print the full traceback for every failure')
    ap.add_argument('--only', default=None,
                    help='regex; test only rules matching it')
    args = ap.parse_args()

    app.config['TESTING'] = False          # we want 500s, not raised exceptions
    app.config['PROPAGATE_EXCEPTIONS'] = False
    app.config['WTF_CSRF_ENABLED'] = False

    # capture real tracebacks instead of the generic 500 page
    errors = {}

    def _record(sender, exception, **extra):
        from flask import request
        errors[request.path] = traceback.format_exc()

    got_request_exception.connect(_record, app)

    with app.app_context():
        if args.emp_code:
            emp = Employee.query.filter_by(emp_code=args.emp_code.upper()).first()
            if not emp:
                sys.exit(f'no employee {args.emp_code}')
        else:
            emp = (Employee.query.filter_by(role='admin', is_active=True).first()
                   or Employee.query.filter_by(is_active=True).first())
            if not emp:
                sys.exit('no employees in the database')

        resolvers = build_param_values()
        cache = {}

        rules = []
        for rule in app.url_map.iter_rules():
            if 'GET' not in (rule.methods or set()):
                continue
            if rule.endpoint == 'static':
                continue
            if SKIP_ENDPOINT_RE.search(rule.endpoint) or SKIP_RULE_RE.search(str(rule)):
                continue
            if args.only and not re.search(args.only, str(rule)):
                continue
            rules.append(rule)

        rules.sort(key=lambda r: str(r))

        print(f'Impersonating {emp.emp_code} — {emp.name} (role={emp.role})')
        print(f'Testing {len(rules)} GET routes\n')

        client = app.test_client()
        with client.session_transaction() as sess:
            sess['emp_code'] = emp.emp_code
            sess['name']     = emp.name
            sess['role']     = emp.role
            sess['vertical'] = emp.vertical or ''

        ok = skipped = failed = 0
        failures = []

        for rule in rules:
            # fill in URL params with real ids
            values, missing = {}, False
            for param in rule.arguments:
                conv = 'string'
                m = re.search(r'<(?:(\w+):)?%s>' % re.escape(param), str(rule))
                if m and m.group(1):
                    conv = m.group(1)
                val = resolve(param, conv, cache, resolvers)
                if val is None:
                    missing = True
                    break
                values[param] = val

            if missing:
                skipped += 1
                print(f'  SKIP  {rule}  (no sample data for its id)')
                continue

            try:
                url = rule.build(values, append_unknown=False)[1]
            except Exception:
                skipped += 1
                continue

            try:
                resp = client.get(url, follow_redirects=False)
                code = resp.status_code
            except Exception:
                code = 500
                errors[url] = traceback.format_exc()

            if code >= 500 and code != 503:
                failed += 1
                failures.append((url, code, rule.endpoint))
                print(f'  FAIL  {code}  {url}   [{rule.endpoint}]')
                tb = errors.get(url, '')
                if tb:
                    last = [l for l in tb.strip().splitlines() if l.strip()][-1]
                    print(f'        {last}')
                    if args.verbose:
                        print('\n'.join('        ' + l for l in tb.splitlines()))
            elif code in (301, 302, 303, 307, 308):
                ok += 1
                print(f'  OK    {code}  {url}  → {resp.headers.get("Location", "")}')
            elif code in (401, 403):
                ok += 1
                print(f'  AUTH  {code}  {url}   (permission check fired)')
            elif code == 503:
                skipped += 1
                print(f'  CFG   503  {url}   (dependency not configured here)')
            elif code == 404:
                skipped += 1
                print(f'  404   {url}   (no such row — not a crash)')
            else:
                ok += 1
                print(f'  OK    {code}  {url}')

        print(f'\n{"="*64}')
        print(f'  passed {ok}   skipped {skipped}   FAILED {failed}')
        if failures:
            print(f'\n  Broken pages:')
            for url, code, endpoint in failures:
                print(f'    {code}  {url}   [{endpoint}]')
            print(f'\n  Re-run with --verbose for full tracebacks.')
        print(f'{"="*64}')

        return failed


if __name__ == '__main__':
    sys.exit(min(main(), 120))
