"""The account action buttons open CRM.Form. The <head> script that
defines it once called crmUrl() — declared later, in the body — so it
threw at load, CRM.Form and CRMToast were never defined, and "+ Log
Activity", "Change Stage", "Reassign PIC" and "Convert → Opportunity"
all did nothing.

The boot check runs that head script under node as a browser would at
that moment: no crmUrl, and a fetch that fails."""
import json
import os
import re
import shutil
import subprocess

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _html():
    with open(os.path.join(_ROOT, 'templates', 'app.html')) as fh:
        return fh.read()


def _head_helpers_script(html):
    head = html[:html.index('</head>')]
    scripts = re.findall(r'<script>(.*?)</script>', head, re.S)
    return next(s for s in scripts if 'CRM.Form = {' in s)


_RUNNER = r'''
const vm = require('vm');
const src = require('fs').readFileSync(0, 'utf8');
const rejections = [];
process.on('unhandledRejection', r => rejections.push(String(r)));
const ctx = {
  console: {error() {}, warn() {}, log() {}},
  location: {pathname: '/CRM/app'},
  document: {getElementById() { return null; }, addEventListener() {},
             createElement() { return {style: {}, addEventListener() {}}; },
             body: {appendChild() {}}},
  fetch: () => Promise.reject(new Error('blocked')),
  setTimeout,
};
ctx.window = ctx;
let thrown = null;
try { vm.runInNewContext(src, ctx); } catch (e) { thrown = String(e); }
setTimeout(() => {
  console.log(JSON.stringify({
    thrown, rejections,
    form: typeof (ctx.CRM && ctx.CRM.Form && ctx.CRM.Form.open),
    toast: typeof (ctx.CRMToast && ctx.CRMToast.error),
    url: ctx.CRM && ctx.CRM._headUrl ? ctx.CRM._headUrl('/api/employees') : null,
    retry: ctx.CRM ? ctx.CRM._empsPromise : 'no CRM',
  }));
}, 50);
'''


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_the_form_helpers_exist_even_when_the_head_script_runs_first():
    src = _head_helpers_script(_html())
    out = subprocess.run(['node', '-e', _RUNNER], input=src, text=True,
                         capture_output=True, timeout=30)
    assert out.returncode == 0, out.stderr[-800:]
    r = json.loads(out.stdout.strip().splitlines()[-1])
    assert r['thrown'] is None, r['thrown']
    assert r['form'] == 'function' and r['toast'] == 'function', r
    assert r['rejections'] == [], r['rejections']
    assert r['url'] == '/CRM/api/employees'
    assert r['retry'] is None, 'a failed employee load must allow a retry'


def test_the_head_script_does_not_call_body_helpers_at_load():
    src = _head_helpers_script(_html())
    code = re.sub(r'/\*.*?\*/', '', src, flags=re.S)
    code = re.sub(r'(?m)^\s*//.*$', '', code)
    assert 'crmUrl(' not in code.replace(
        "typeof crmUrl === 'function') return crmUrl(", ''), \
        'crmUrl is declared in the body; the head script must not need it'
    assert src.index('CRM.Form = {') < src.index('try { CRM.ensureEmployees(); }'), \
        'data loading starts only after the form helpers are defined'


def test_each_account_action_checks_the_form_first():
    html = _html()
    for fn in ('acctLogActivity', 'acctChangeStage', 'acctReassign',
               'acctConvert', 'acctBulkAssign'):
        body = html[html.index(f'async function {fn}('):]
        body = body[:body.index('\n}\n')]
        assert 'acctFormReady()' in body, fn


def test_bulk_selection_is_limited_to_rows_on_screen():
    html = _html()
    select_all = html[html.index('function acctSelectAll('):]
    select_all = select_all[:select_all.index('\n}\n')]
    assert 'acctVisible()' in select_all and 'ACCT.list' not in select_all


def test_bulk_assign_does_not_send_account_notes():
    html = _html()
    run = html[html.index('async function acctRunBulkAssign('):]
    run = run[:run.index('\n}\n')]
    assert "/api/accounts/assign-bulk" in run
    assert 'notes' not in run
