"""
Does the handover pack send fields the TMS actually has?

    .venv/bin/python scripts/handover_field_audit.py
    .venv/bin/python scripts/handover_field_audit.py --tms /var/www/procam-lr
    .venv/bin/python scripts/handover_field_audit.py --strict

PROCAM-DECISIONS-02NOV Module 1: "replace the invented handover
fields (9 in FF-COMMS-01, 21 in CRM2) with the fields the TMS Job /
FF Job create screens actually require, read from the TMS code.
Invent no fields."

A one-off correction would be true on the day it was made. This reads
both repositories and says what is wrong today, so the answer stays
true — the CRM's field lists on one side, and on the other the TMS's
FF pack reader (`app/services/ff_handover.py`) and the three models a
handover lands in (Job, Project, ClientPO).

It reads the TMS **source**, not a copy of its field names kept here.
A vendored list is a list that drifts, and drift is the whole problem:
the CRM sends a field, the TMS ignores it silently, and the figure
shows as blank on a screen nobody connects back to the pack.

Two kinds of finding, and they are not equally bad:

  INVENTED  the CRM sends a field the TMS has nowhere to put. The
            data is dropped without a word. `--strict` exits 1 on
            any of these, so it can run in a pre-deploy check.

  OMITTED   the TMS has a field the pack never fills. Not always a
            fault — plenty are not known at quotation — so these
            are listed, never failed on, and the ones that are
            genuinely unknowable are named in the CRM's own
            CANNOT_SUPPLY and skipped here.

Read-only. Touches no database.
"""
import argparse
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

#: Where the TMS checkout is, in the order worth trying.
TMS_GUESSES = (
    '/var/www/procam-lr',
    os.path.join(os.path.expanduser('~'), 'Desktop', 'Procam-lr-main'),
    os.path.join(os.path.dirname(_ROOT), 'Procam-lr-main'),
    os.path.join(os.path.dirname(_ROOT), 'procam-lr'),
)

#: Columns no handover ever fills: the TMS's own keys, its audit
#: trail, and the numbers it issues itself. Sending any of these
#: would be inventing an identifier in somebody else's series.
NEVER_OURS = {
    'id', 'business_id', 'is_active', 'created_at', 'updated_at',
    'created_by_id', 'project_id', 'job_id', 'client_id',
    'branch_id', 'project_head_id', 'job_owner_id',
    'pcn_job_no', 'full_job_code', 'splendin_job_no',
    'project_job_no', 'job_reference', 'pro_number', 'code',
    'fiscal_year', 'team_access_enabled', 'is_have_dsr_shipment',
    'splendid_project_name', 'closure_override_reason',
    'proforma_cancel_reason', 'crm_lead_id',
}


def _columns(src, class_name):
    """{column: declaration} for one SQLAlchemy model class.

    Written against the file rather than by importing it: the TMS has
    its own dependencies and its own database URL, and importing its
    models from here would need its virtualenv. The cost is a regex,
    and the regex has to cope with both styles the TMS uses —
    `name = db.Column(` and the aligned `name           = db.Column(`.
    """
    start = src.find(f'class {class_name}(')
    if start < 0:
        return {}
    end = src.find('\nclass ', start + 10)
    body = src[start:(end if end > 0 else len(src))]
    out = {}
    for match in re.finditer(
            r'^\s{4}(\w+)\s*=\s*db\.Column\(', body, re.M):
        name = match.group(1)
        # The declaration runs to the matching close paren, which may
        # be several lines down.
        i, depth = match.end() - 1, 0
        while i < len(body):
            if body[i] == '(':
                depth += 1
            elif body[i] == ')':
                depth -= 1
                if depth == 0:
                    break
            i += 1
        out[name] = body[match.end():i]
    return out


def _tuple(src, name):
    match = re.search(name + r'\s*=\s*\(', src)
    if not match:
        return ()
    i, depth = match.end() - 1, 0
    while i < len(src):
        if src[i] == '(':
            depth += 1
        elif src[i] == ')':
            depth -= 1
            if depth == 0:
                break
        i += 1
    return tuple(re.findall(r"'([a-z0-9_]+)'", src[match.end():i]))


def _required(columns):
    return sorted(n for n, decl in columns.items()
                  if 'nullable=False' in decl and 'default' not in decl
                  and n not in NEVER_OURS)


def _find_tms(given):
    for path in ([given] if given else []) + list(TMS_GUESSES):
        if path and os.path.isdir(os.path.join(path, 'app', 'models')):
            return path
    return None


def _report(label, ours, theirs, *, cannot=(), required=(),
            lookups=()):
    """One section. Returns (invented, required-and-not-sent).

    `lookups` are match keys rather than fields — `client_name` finds
    the client whose id the TMS actually stores. They are in the pack
    on purpose, so counting them as invented would make the honest
    fix look like deleting the column that makes the pack usable.

    `cannot` is subtracted from the required list as well as the
    omitted one. It was not, and the first run reported
    `ClientPO.po_number` as required and missing — a number the TMS
    issues itself, and one the CRM's own THEIRS_TO_NUMBER already
    said not to send.
    """
    ours, theirs = set(ours), set(theirs)
    lookups = set(lookups)
    invented = sorted(ours - theirs - lookups)
    used_as_key = sorted((ours & lookups) - theirs)
    omitted = sorted(theirs - ours - set(cannot) - NEVER_OURS)
    unfilled_required = sorted(
        set(required) - ours - set(cannot) - NEVER_OURS)

    print(f'\n{label}')
    print(f'  the pack sends          {len(ours)}')
    print(f'  the TMS has a place for {len(theirs)}')
    if invented:
        print(f'  INVENTED ({len(invented)}) — sent, and dropped '
              f'without a word:')
        for name in invented:
            print(f'    {name}')
    else:
        print('  INVENTED (0) — every field the pack sends has a '
              'place at the far end')
    if used_as_key:
        print(f'  lookup keys ({len(used_as_key)}) — in the pack so '
              f'the person importing can find the record; the TMS '
              f'stores an id, not these:')
        for name in used_as_key:
            print(f'    {name}')
    if unfilled_required:
        print(f'  REQUIRED AND NOT SENT ({len(unfilled_required)}) — '
              f'the far end refuses without these:')
        for name in unfilled_required:
            print(f'    {name}')
    if omitted:
        print(f'  omitted ({len(omitted)}) — the TMS has these and '
              f'the pack does not fill them:')
        for name in omitted:
            print(f'    {name}')
    if cannot:
        print(f'  not knowable at quotation ({len(cannot)}), named in '
              f'the CRM\'s own CANNOT_SUPPLY — skipped above')
    return invented, unfilled_required


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tms', default=None,
                    help='path to the procam-lr checkout')
    ap.add_argument('--strict', action='store_true',
                    help='exit 1 if the pack sends an invented field')
    args = ap.parse_args()

    tms = _find_tms(args.tms)
    if tms is None:
        print('The TMS repository is not here, so nothing can be '
              'read from it.')
        print('Tried:')
        for path in ([args.tms] if args.tms else []) + list(TMS_GUESSES):
            if path:
                print(f'  {path}')
        print('\nPass --tms <path>. This check deliberately does not '
              'fall back to a list of TMS field names kept in this '
              'repo: a vendored list is a list that drifts, and '
              'drift is what it exists to find.')
        raise SystemExit(2)

    print(f'CRM: {_ROOT}')
    print(f'TMS: {tms}')

    from app.services import tms_intake as ti

    ff_src = open(os.path.join(
        tms, 'app', 'services', 'ff_handover.py')).read()
    job_src = open(os.path.join(tms, 'app', 'models', 'job.py')).read()
    master_src = open(os.path.join(
        tms, 'app', 'models', 'master.py')).read()
    po_src = open(os.path.join(
        tms, 'app', 'models', 'commercials.py')).read()

    bad, missing_required = [], []

    # ── the FF pack: a reader, not a model ──────────────────────────
    known = _tuple(ff_src, 'KNOWN')
    needs = _tuple(ff_src, 'REQUIRED')
    if not known:
        raise SystemExit('Could not read KNOWN from the TMS '
                         'ff_handover. The format has changed; this '
                         'script has to be read before it is '
                         'trusted.')
    inv, req = _report(
        'FF Job — app/services/ff_handover.py (the TMS reader)',
        tuple(ti.PART_A_FIELDS) + ('gst_treatment',), known,
        cannot=ti.CANNOT_SUPPLY, required=needs,
        lookups=ti.LOOKUP_KEYS)
    bad += [('FF Job', n) for n in inv]
    missing_required += [('FF Job', n) for n in req]

    # ── the three the Part B pack lands in ──────────────────────────
    for label, ours, src, cls in (
            ('Project — app/models/master.py', ti.PART_B_PROJECT,
             master_src, 'Project'),
            ('Job — app/models/job.py', ti.PART_B_JOB, job_src, 'Job'),
            ('Client PO — app/models/commercials.py',
             ti.PART_B_ORDER, po_src, 'ClientPO')):
        columns = _columns(src, cls)
        if not columns:
            raise SystemExit(f'Could not read the {cls} columns from '
                             f'the TMS. Read this script before '
                             f'trusting it.')
        inv, req = _report(label, ours, columns.keys(),
                           cannot=(tuple(ti.THEIRS_TO_NUMBER)
                                   + tuple(ti.THEIRS_TO_DECIDE)),
                           required=_required(columns),
                           lookups=ti.LOOKUP_KEYS)
        bad += [(cls, n) for n in inv]
        missing_required += [(cls, n) for n in req]

    print('\n' + '─' * 64)
    if bad:
        print(f'{len(bad)} invented field(s): '
              + ', '.join(f'{where}.{name}' for where, name in bad))
    else:
        print('No invented fields. Every field the handover sends has '
              'a place at the far end.')
    if missing_required:
        print(f'{len(missing_required)} field(s) the far end requires '
              f'and the pack does not send: '
              + ', '.join(f'{where}.{name}'
                          for where, name in missing_required))

    if args.strict and (bad or missing_required):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
