"""Why this person cannot sign in. Read-only, and never the password.

CRM-WA-01 §5: one user gets "Invalid Employee Code or Password" and
nobody can tell which half is wrong.

WHY THAT MESSAGE IS DELIBERATELY USELESS

`/login` answers the same sentence for every failure, on purpose:
telling an anonymous caller "that code exists but the password is
wrong" is telling them which codes exist. That stays. What was
missing is the other side of the desk — an **administrator**, already
signed in, asking why a named colleague cannot get in. They can see
the employee master anyway; what they could not see is which of the
five conditions the sign-in actually tripped.

THE FIVE, AND WHICH ONE THE SCREEN NEVER SAYS

    no such code       the code is not in the CRM's employee table at
                       all. Often a TMS code: the two systems share
                       the code *format* and nothing else.
    inactive           `is_active` is false. `/login` filters on it
                       before the password is even checked, so a
                       deactivated person gets the identical message
                       to somebody typing a wrong password — and
                       resetting their password will not help, which
                       is the trap this is for.
    locked             too many failed attempts. This one the screen
                       does say, with the minutes left.
    temp expired       an administrator issued a temporary password
                       and it aged out. The screen says this too.
    no password set    a row created by an import or a seed with no
                       usable hash. `check_password` raises or
                       refuses, and the person has nothing to type
                       that would work.

Anything else and the account is fine: the password is wrong, and the
answer is the reset that already exists.

CRM LOGIN IS NOT TMS LOGIN

The CRM reads its own `employees` table, with its own
`password_hash`, in its own database. There is no shared session, no
SSO and no password sync. The codes look the same because both
systems use the same prefixes for the same people, which is exactly
why "it works in the TMS" is not evidence about the CRM.

WHAT THIS WILL NOT DO

It never reads, compares, returns or logs a password or a hash, and
it never says whether a given password would work. It reports the
state of the account, which is what an administrator can already see
on the employee screen — gathered into one answer instead of four
columns nobody thinks to check together.
"""
from __future__ import annotations

from datetime import datetime

#: The verdicts, worst first. The order matters: a locked *and*
#: inactive account is reported as inactive, because reactivating is
#: the thing that has to happen and the lock will have expired by the
#: time anybody gets to it.
NO_SUCH_CODE = 'no_such_code'
INACTIVE = 'inactive'
NO_PASSWORD = 'no_password_set'
LOCKED = 'locked'
TEMP_EXPIRED = 'temp_password_expired'
MUST_CHANGE = 'must_change_password'
FINE = 'account_is_fine'

#: What each verdict means, and what to do about it. Written for an
#: administrator reading it at the end of a day, not for a log.
ADVICE = {
    NO_SUCH_CODE:
        'There is no such employee code in the CRM. Check the spelling, '
        'and check whether they are thinking of their TMS code — the '
        'two systems share the code format and nothing else. If they '
        'have never had a CRM account, create one on Admin → '
        'Employees.',
    INACTIVE:
        'The account is deactivated. Sign-in is refused before the '
        'password is even checked, which is why they see the same '
        'message as a wrong password — and why resetting the password '
        'will not help. Reactivate them on Admin → Employees.',
    NO_PASSWORD:
        'The account has no usable password — it was created by an '
        'import or a seed and never given one. Use Reset password on '
        'Admin → Employees and give them the temporary password.',
    LOCKED:
        'Locked by failed attempts. It clears itself at the time '
        'below, or Reset password clears it immediately.',
    TEMP_EXPIRED:
        'The temporary password an administrator issued has expired. '
        'Issue a new one with Reset password on Admin → Employees.',
    MUST_CHANGE:
        'They have a temporary password that still works and must '
        'change it at first sign-in. If they cannot, re-issue it with '
        'Reset password — the old one may have been mistyped when it '
        'was passed on.',
    FINE:
        'Nothing is wrong with the account: it exists, it is active, '
        'it is not locked and it has a password. So the password being '
        'typed is wrong. Use Reset password on Admin → Employees and '
        'read them the temporary one.',
}


def _looks_unusable(employee):
    """Whether the stored hash could never match anything.

    The hash itself is never returned, compared or logged — only
    whether there is one and whether it has the shape Werkzeug
    writes. A row imported with an empty string, or with a plaintext
    password somebody pasted into the column, is unusable, and the
    person has nothing they could type that would work.
    """
    stored = getattr(employee, 'password_hash', None) or ''
    if not stored.strip():
        return True
    # Werkzeug writes 'method$salt$hash'. Anything without the
    # separators was not written by `set_password`.
    return stored.count('$') < 2


def diagnose(emp_code, *, now=None):
    """One verdict for one code. Never raises.

    Takes the code as typed — `/login` upper-cases and strips, so
    this does too, and reports when that was what made the difference.
    """
    from app import Employee

    now = now or datetime.utcnow()
    typed = str(emp_code or '')
    code = typed.strip().upper()

    out = {
        'typed': typed,
        'code': code,
        'verdict': NO_SUCH_CODE,
        'exists': False,
        'is_active': None,
        'name': '',
        'role': '',
        'vertical': '',
        'email': '',
        'locked_until': '',
        'failed_logins': 0,
        'must_change_pw': False,
        'temp_expires_at': '',
        'last_login': '',
        'detail': '',
    }
    if not code:
        out['detail'] = 'No employee code was given.'
        out['advice'] = ADVICE[NO_SUCH_CODE]
        return out

    try:
        employee = Employee.query.filter_by(emp_code=code).first()
    except Exception as exc:                              # noqa: BLE001
        out['verdict'] = NO_SUCH_CODE
        out['detail'] = (f'The employee table could not be read '
                         f'({type(exc).__name__}).')
        out['advice'] = ADVICE[NO_SUCH_CODE]
        return out

    if employee is None:
        out['detail'] = f'No employee in the CRM has the code {code}.'
        if code != typed:
            out['detail'] += (f' (Looked up as {code}; the sign-in form '
                              f'trims and upper-cases what is typed, so '
                              f'{typed!r} and {code!r} are the same '
                              f'code to it.)')
        out['advice'] = ADVICE[NO_SUCH_CODE]
        return out

    out.update({
        'exists': True,
        'is_active': bool(getattr(employee, 'is_active', False)),
        'name': getattr(employee, 'name', '') or '',
        'role': getattr(employee, 'role', '') or '',
        'vertical': getattr(employee, 'vertical', '') or '',
        'email': getattr(employee, 'email', '') or '',
        'failed_logins': int(getattr(employee, 'failed_logins', 0) or 0),
        'must_change_pw': bool(getattr(employee, 'must_change_pw', False)),
    })
    locked_until = getattr(employee, 'locked_until', None)
    temp_expires = getattr(employee, 'temp_password_expires_at', None)
    out['locked_until'] = str(locked_until)[:19] if locked_until else ''
    out['temp_expires_at'] = str(temp_expires)[:19] if temp_expires else ''
    out['last_login'] = _last_login(code)

    # Worst first. See the note on ordering above.
    if not out['is_active']:
        out['verdict'] = INACTIVE
        out['detail'] = (f'{out["name"] or code} is deactivated in the '
                         f'CRM. `/login` filters on is_active before it '
                         f'checks the password, so the message they see '
                         f'is the same one a wrong password gives.')
    elif _looks_unusable(employee):
        out['verdict'] = NO_PASSWORD
        out['detail'] = ('The account has no usable password hash, so '
                         'there is nothing they could type that would '
                         'be accepted.')
    elif locked_until and locked_until > now:
        out['verdict'] = LOCKED
        minutes = max(1, int((locked_until - now).total_seconds() // 60) + 1)
        out['detail'] = (f'Locked for another {minutes} minute(s) after '
                         f'repeated failed attempts.')
    elif (out['must_change_pw'] and temp_expires and temp_expires < now):
        out['verdict'] = TEMP_EXPIRED
        out['detail'] = (f'The temporary password expired at '
                         f'{out["temp_expires_at"]}.')
    elif out['must_change_pw']:
        out['verdict'] = MUST_CHANGE
        out['detail'] = ('They hold a temporary password that still '
                         'works and must be changed at first sign-in.')
    else:
        out['verdict'] = FINE
        out['detail'] = ('The account exists, is active, is not locked '
                         'and has a password.')
    out['advice'] = ADVICE[out['verdict']]
    return out


def _last_login(code):
    """When this code last signed in successfully, from the audit log.

    From `auth.login_success`, which `/login` writes on every
    successful sign-in. A blank means never — and "never, on an
    account that is otherwise fine" is the strongest single hint that
    the password was never successfully passed on to them.
    """
    try:
        from app.models.audit import AuditEvent
        row = (AuditEvent.query
               .filter(AuditEvent.action == 'auth.login_success',
                       AuditEvent.actor == code)
               .order_by(AuditEvent.occurred_at.desc())
               .first())
    except Exception:                                     # noqa: BLE001
        return ''
    return str(row.occurred_at)[:19] if row is not None else ''


def recent_failures(code, *, limit=10):
    """The last failed attempts on this code, for the admin screen.

    `/login` records `auth.login_failure` with
    `known_active_account`, which is the one thing the generic
    message hides: whether the code was even recognised.
    """
    try:
        from app.models.audit import AuditEvent
        rows = (AuditEvent.query
                .filter(AuditEvent.action == 'auth.login_failure',
                        AuditEvent.actor == (code or '').strip().upper())
                .order_by(AuditEvent.occurred_at.desc())
                .limit(limit).all())
    except Exception:                                     # noqa: BLE001
        return []
    out = []
    for row in rows:
        payload = row.new_value if isinstance(row.new_value, dict) else {}
        out.append({
            'at': str(row.occurred_at)[:19],
            'known_active_account': bool(
                payload.get('known_active_account')),
            'ip': row.ip or '',
        })
    return out
