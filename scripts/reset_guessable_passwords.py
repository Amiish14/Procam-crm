"""
Reset the accounts whose password is still their employee code.

scripts/audit_default_passwords.py finds them and stops there, because a
reset is a person's decision. This is that decision carried out: every
account whose password anyone in the company could guess gets a fresh
random temporary password, is forced to change it at the next sign-in,
and has its open sessions ended.

    # see what would happen — writes nothing at all
    .venv/bin/python scripts/reset_guessable_passwords.py

    # do it
    .venv/bin/python scripts/reset_guessable_passwords.py --apply --yes \
        --actor PCM001

The preview opens the database read-only and does not import the app, so
it cannot change anything even by mistake. Only --apply imports the app.

The new passwords are never printed. They are written to one CSV, owner
read-only (0600), under backups/ which git ignores; the run prints the
path and the count and nothing else about them. Hand them over privately
— each holder must change theirs at first sign-in, and the temporary one
stops working after the same 72 hours an administrator-issued password
has always had.

Safety rails
  * --apply without --yes is refused: the file of live credentials this
    produces should never be created by a mistyped command.
  * a database with no employees is refused — that is the wrong
    DATABASE_URL, not a clean estate.
  * the super admin is left alone unless its code is named in --only. It
    owns the Access Control matrix and cannot be reset through the
    portal, so locking it out by accident has no easy way back.
  * the file is created exclusively; an existing one is never overwritten.
  * nothing is committed until the file is safely on disk, so a failed
    write cannot lock anybody out of an account whose new password was
    lost with it.

Exit status: 0 when the run completed, 2 when it refused to run.
"""
import argparse
import csv
import os
import secrets
import sys
from datetime import datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)
sys.path.insert(0, _HERE)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_ROOT, '.env'))
except ImportError:
    pass

from audit_default_passwords import PUBLISHED               # noqa: E402
from data_quality_report import read_only_engine            # noqa: E402
from sqlalchemy import text                                 # noqa: E402
from werkzeug.security import check_password_hash           # noqa: E402

# Those two put scripts/ at the front of sys.path when they load, and
# scripts/email_ingest.py then shadows the email_ingest *package* that
# app.py imports — which would break --apply with a bewildering
# "email_ingest is not a package". The project root goes back in front.
if _ROOT in sys.path:
    sys.path.remove(_ROOT)
sys.path.insert(0, _ROOT)

#: Mirrors TEMP_PASSWORD_VALID_FOR in app.py. Used only to describe the
#: rule in the preview, which must not import the app; --apply reads the
#: real value from app.py, so what is written can never drift from it.
TEMP_PASSWORD_HOURS = 72

#: Roles that carry administrative power, so the preview can say plainly
#: which of the exposed accounts are the dangerous ones.
ADMIN_ROLES = ('admin', 'procam_admin')

SUPER_ADMIN_NOTE = ('the super admin — it owns Access Control and cannot '
                    'be reset from the portal; name it in --only to '
                    'include it')


def why_guessable(emp_code, password_hash):
    """Why this password is guessable, or None.

    The same rule as scripts/audit_default_passwords.py, so the tool that
    finds the problem and the tool that fixes it can never disagree about
    which accounts are affected: anyone in the company knows a
    colleague's employee code, and this repository's history still holds
    the bootstrap password its deployment notes once printed in full.
    """
    if not emp_code or not password_hash:
        return None
    if any(check_password_hash(password_hash, guess)
           for guess in (emp_code.lower(), emp_code)):
        return 'employee code'
    if any(check_password_hash(password_hash, guess) for guess in PUBLISHED):
        return 'published in DEPLOY.md'
    return None


def _codes(value):
    """--only / --exclude: comma-separated codes, matched case-blind."""
    return {c.strip().upper() for c in (value or '').split(',') if c.strip()}


def select(rows, only=(), exclude=(), force=False):
    """(chosen, skipped) from (emp_code, name, email, role, is_super, hash).

    Scope filters are applied before the hashes are checked: verifying a
    password hash is deliberately slow, and --only should not pay for the
    whole directory.

    ``force`` resets the named accounts whether or not their password is
    guessable. That is the case where a temporary password has leaked —
    read aloud, pasted into a chat, left in a sent folder — and the
    account needs a new one even though nothing about it looks weak.
    """
    only, exclude = set(only), set(exclude)
    chosen, skipped = [], []
    for emp_code, name, email, role, is_super, password_hash in rows:
        code = (emp_code or '').upper()
        if only and code not in only:
            continue
        why = why_guessable(emp_code, password_hash)
        if not why and force and code in only:
            why = 'named with --force'
        if not why:
            continue
        account = {'emp_code': emp_code, 'name': name or '',
                   'email': email or '', 'role': role or '',
                   'is_super_admin': bool(is_super),
                   'is_admin': (role or '') in ADMIN_ROLES, 'why': why}
        if code in exclude:
            skipped.append((account, 'named in --exclude'))
        elif is_super and code not in only:
            skipped.append((account, SUPER_ADMIN_NOTE))
        else:
            chosen.append(account)
    return chosen, skipped


def _read_only_rows():
    """Every active account, straight from the database, no app import."""
    engine = read_only_engine()
    with engine.connect() as conn:
        total = conn.execute(
            text('SELECT COUNT(*) FROM employees')).scalar() or 0
        rows = conn.execute(text(
            'SELECT emp_code, name, email, role, '
            '       COALESCE(is_super_admin, 0), password_hash '
            'FROM employees WHERE is_active = 1')).fetchall()
    return [tuple(r) for r in rows], total


def _print_table(chosen, skipped):
    if chosen:
        print(f'  {"CODE":<12} {"ROLE":<12} {"ADMIN":<6} WHY')
        for a in sorted(chosen, key=lambda a: (not a['is_admin'],
                                               a['emp_code'])):
            print(f'  {a["emp_code"]:<12} {a["role"]:<12} '
                  f'{"yes" if a["is_admin"] else "no":<6} {a["why"]}')
    for account, reason in sorted(skipped, key=lambda s: s[0]['emp_code']):
        print(f'  skipped  {account["emp_code"]:<12} {reason}')


def _email_password(employee_email, name, password, expires_at):
    """Send one person their own temporary password.

    Returns (sent, reason). Uses the CRM's existing Graph sender, so it
    needs the same Mail.Send grant as every other message the CRM sends;
    without it this returns False and the caller falls back to the file.
    """
    from email_ingest import notifier
    if not (employee_email or '').strip():
        return False, 'no email address on file'
    if not notifier.is_enabled():
        return False, 'NOTIFY_ENABLED is off'
    base = notifier.base_url()
    link = f'{base}/login' if base else '/login'
    esc = notifier._esc
    html = (
        f'<div style="font-family:Arial,sans-serif;font-size:14px;color:#111">'
        f'<p>Hello {esc(name or "")},</p>'
        f'<p>Your Procam CRM password has been reset. Sign in with this '
        f'temporary password and choose your own:</p>'
        f'<p style="font-size:20px;font-weight:700;letter-spacing:1px;'
        f'font-family:monospace">{esc(password)}</p>'
        f'<p>It stops working after <strong>{esc(str(expires_at)[:16])} '
        f'UTC</strong>, and you will be asked to set your own password the '
        f'first time you sign in.</p>'
        f'<p><a href="{esc(link)}" style="background:#BC1D2F;color:#fff;'
        f'padding:10px 18px;border-radius:6px;text-decoration:none;'
        f'font-weight:600">Sign in</a></p>'
        f'<p style="color:#6b7280;font-size:12.5px">If you did not expect '
        f'this, tell your CRM administrator — somebody reset your account.'
        f'</p></div>')
    try:
        sent = notifier.send(employee_email, 'Your Procam CRM password', html)
    except Exception as exc:                            # pragma: no cover
        return False, f'{exc.__class__.__name__}'
    return bool(sent), ('' if sent else 'the mail server refused it')


def _new_password_file(path, issued):
    """Write the credentials file 0600, refusing to overwrite.

    os.open with the mode set at creation, rather than a chmod after the
    fact, so the passwords are never readable by anyone else — not even
    for the instant between the write and the chmod.
    """
    path = os.path.abspath(path)
    # backups/ is absent on a fresh checkout and was absent on the server
    # until it was first needed.
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SystemExit(f'  refused: {path} already exists. A file of live '
                         f'credentials is never overwritten — choose '
                         f'another --out.')
    with os.fdopen(fd, 'w', newline='', encoding='utf-8') as fh:
        writer = csv.writer(fh)
        writer.writerow(['emp_code', 'name', 'email', 'temporary_password',
                         'expires_at'])
        for item in issued:
            expires = item['expires_at'].strftime('%Y-%m-%d %H:%M UTC')
            writer.writerow([item['emp_code'], item['name'], item['email'],
                             item['password'], expires])
    return path


def _fallback_password():
    """Only reached if app.py ever loses its generator. Same shape: no
    characters that are misread when a password is read out or copied by
    hand (0/O, 1/l/I)."""
    alphabet = 'abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789'
    # token_urlsafe seeds nothing here — secrets.choice is the same CSPRNG
    # and lets us keep the unambiguous alphabet.
    return ''.join(secrets.choice(alphabet) for _ in range(14))


def apply_resets(only, exclude, out_path, actor, force=False, email=False):
    """Reset the chosen accounts.

    Returns (issued, skipped, path). With ``email``, each person is sent
    their own password and only those who could not be reached are
    written to the file — the common case then leaves no credentials
    file on disk at all.
    """
    # Imported here and nowhere else: importing the app runs the boot
    # autoheal, which writes, and the preview promises to write nothing.
    import app as main                                      # noqa: E402
    from app.services import audit                          # noqa: E402

    db, Employee = main.db, main.Employee
    valid_for = getattr(main, 'TEMP_PASSWORD_VALID_FOR',
                        timedelta(hours=TEMP_PASSWORD_HOURS))
    generate = getattr(main, '_temporary_password', _fallback_password)

    with main.app.app_context():
        if Employee.query.count() == 0:
            raise SystemExit('  refused: no employees in this database. '
                             'Check DATABASE_URL — an empty database is the '
                             'wrong database, not a clean estate.')
        employees = (Employee.query.filter_by(is_active=True)
                     .order_by(Employee.emp_code).all())
        rows = [(e.emp_code, e.name, e.email, e.role, bool(e.is_super_admin),
                 e.password_hash) for e in employees]
        chosen, skipped = select(rows, only, exclude, force=force)
        if not chosen:
            return [], skipped, None

        by_code = {e.emp_code: e for e in employees}
        now = datetime.utcnow()
        expires_at = now + valid_for
        issued = []
        session_info = db.session.info
        previous = session_info.get('audit_disabled')
        # The automatic listener records this change as 'system', because
        # a flush cannot know who ran a command line. We want the operator
        # named, and exactly one row per account, so the listener stands
        # down for this flush and each reset is recorded explicitly below.
        session_info['audit_disabled'] = True
        try:
            for account in chosen:
                emp = by_code[account['emp_code']]
                password = generate()
                before_version = emp.session_version or 0
                before_forced = bool(emp.must_change_pw)
                emp.set_password(password)
                emp.must_change_pw = True
                emp.temp_password_expires_at = expires_at
                # An account locked out by someone guessing at it must be
                # able to use the password we are about to hand over.
                emp.failed_logins, emp.locked_until = 0, None
                # Whoever held the guessable password is signed out
                # everywhere, immediately.
                emp.session_version = before_version + 1
                audit.record(
                    'employee.password_change', 'employee', emp.emp_code,
                    old={'sign_in_details': 'previous',
                         'must_change_pw': before_forced,
                         'session_version': before_version},
                    new={'sign_in_details': 'changed',
                         'must_change_pw': True,
                         'session_version': emp.session_version,
                         'expires_at': expires_at},
                    reason=f'temporary password issued by '
                           f'reset_guessable_passwords.py '
                           f'(was the {account["why"]})',
                    actor=actor)
                issued.append({'emp_code': emp.emp_code,
                               'name': emp.name or '',
                               'email': emp.email or '',
                               'password': password,
                               'expires_at': expires_at})
            db.session.flush()
        finally:
            # Restore it: this session is long-lived under the test suite,
            # and leaving the trail switched off would be silent.
            if previous is None:
                session_info.pop('audit_disabled', None)
            else:
                session_info['audit_disabled'] = previous

        to_write = issued
        if email:
            # Send first, then write only what could not be sent: a
            # password that reached its owner should not also be sitting
            # in a file somebody has to remember to destroy.
            to_write = []
            for row in issued:
                sent, why = _email_password(row['email'], row['name'],
                                            row['password'],
                                            row['expires_at'])
                row['emailed'], row['not_emailed_because'] = sent, why
                if not sent:
                    to_write.append(row)
        try:
            # The file first. If it cannot be written, nothing is
            # committed and nobody is locked out of an account whose new
            # password went nowhere.
            path = _new_password_file(out_path, to_write) if to_write else None
        except BaseException:
            db.session.rollback()
            raise
        try:
            db.session.commit()
        except BaseException:
            db.session.rollback()
            # The passwords in that file never took effect; it is only a
            # secret lying about.
            if path:
                os.unlink(path)
            raise
        return issued, skipped, path


def main(argv=None):
    ap = argparse.ArgumentParser(
        description='Reset the accounts whose password is still their '
                    'employee code. Previews by default.')
    ap.add_argument('--apply', action='store_true',
                    help='actually reset them (requires --yes)')
    ap.add_argument('--yes', action='store_true',
                    help='confirm: --apply does nothing without it')
    ap.add_argument('--only', default='',
                    help='only these employee codes (comma separated)')
    ap.add_argument('--exclude', default='',
                    help='never these employee codes (comma separated)')
    ap.add_argument('--out', default=None,
                    help='where the new passwords are written '
                         '(default backups/password-reset-<timestamp>.csv)')
    ap.add_argument('--force', action='store_true',
                    help='reset the accounts named in --only even if their '
                         'password is not guessable — for a credential that '
                         'has leaked')
    ap.add_argument('--email', action='store_true',
                    help='send each person their own password instead of '
                         'writing it to a file; only those who cannot be '
                         'reached are written')
    ap.add_argument('--actor', default='system',
                    help='who is running this, for the audit trail')
    args = ap.parse_args(argv)

    only, exclude = _codes(args.only), _codes(args.exclude)
    out_path = args.out or os.path.join(
        _ROOT, 'backups',
        f'password-reset-{datetime.utcnow():%Y%m%d-%H%M%S}.csv')

    if args.force and not only:
        print('\n  refused: --force needs --only as well.\n'
              '  Forcing without naming anybody would reset every active '
              'account in the\n  CRM — every person signed out at once. '
              'Name the accounts whose\n  password has leaked.\n')
        return 2

    if args.apply and not args.yes:
        print('\n  refused: --apply needs --yes as well.\n'
              '  It resets live passwords, ends the sessions of everyone '
              'affected and\n'
              '  writes a file of credentials. Run the preview first, then '
              'add --yes.\n')
        return 2

    if not args.apply:
        try:
            rows, total = _read_only_rows()
        except Exception as exc:
            print(f'\n  refused: could not read the employees table '
                  f'({exc.__class__.__name__}). Check DATABASE_URL.\n')
            return 2
        if total == 0:
            print('\n  refused: no employees in this database. Check '
                  'DATABASE_URL — an empty\n  database is the wrong '
                  'database, not a clean estate.\n')
            return 2
        chosen, skipped = select(rows, only, exclude, force=args.force)
        print('\n  PREVIEW — nothing is written.\n')
        print(f'  active accounts checked   {len(rows)}')
        print(f'  would be reset            {len(chosen)}')
        admins = sum(a['is_admin'] for a in chosen)
        print(f'    of which administrators {admins}')
        print(f'  left alone                {len(skipped)}\n')
        _print_table(chosen, skipped)
        if chosen:
            print(f'\n  --apply --yes would, for each account above: set a '
                  f'fresh random temporary\n'
                  f'  password, force a change at the next sign-in, expire '
                  f'it after '
                  f'{TEMP_PASSWORD_HOURS} hours,\n'
                  f'  end every open session, and record the change in the '
                  f'audit trail.\n'
                  f'  The new passwords would be written to\n'
                  f'    {os.path.abspath(out_path)}  (0600)\n')
        else:
            print('\n  Nothing to do: no account in scope opens with a '
                  'guessable password.\n')
        return 0

    issued, skipped, path = apply_resets(only, exclude, out_path,
                                         args.actor, force=args.force,
                                         email=args.email)
    print('')
    if not issued:
        _print_table([], skipped)
        print('  Nothing to do: no account in scope opens with a guessable '
              'password.\n')
        return 0
    _print_table([], skipped)
    print(f'  reset     {len(issued)} account(s)')
    emailed = [r for r in issued if r.get('emailed')]
    unreached = [r for r in issued if args.email and not r.get('emailed')]
    if args.email:
        print(f'  emailed   {len(emailed)} — their password went straight '
              f'to them')
        for row in unreached:
            print(f'    not sent to {row["emp_code"]}: '
                  f'{row.get("not_emailed_because") or "unknown"}')
    if path:
        print(f'  file      {path}  (0600)'
              + ('  — only those who could not be emailed' if args.email
                 else ''))
    print()
    if path:
        print('  Hand each person their password privately — one to one, '
              'never in a group\n'
              '  chat or a shared sheet. It expires in '
              f'{TEMP_PASSWORD_HOURS} hours and they must set their\n'
              '  own password at first sign-in. Delete the file once they '
              'all have.\n')
    else:
        print('  No credentials file was written: everybody was emailed '
              'their own\n  password, so there is nothing on disk to '
              'destroy. Each expires in\n'
              f'  {TEMP_PASSWORD_HOURS} hours and must be changed at first '
              'sign-in.\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
