"""
The two 554 MIME rejections, and what was in them.

Read-only. Opens no connection to the mail server, sends nothing,
writes nothing. Safe on a live server at any hour.

    .venv/bin/python scripts/inspect_mail_failures.py
    .venv/bin/python scripts/inspect_mail_failures.py --id 1234
    .venv/bin/python scripts/inspect_mail_failures.py --rebuild

`--rebuild` reassembles each failed message through the real builder
(`mailer._send_smtp`'s message construction, without the send) and
prints what the MIME looks like: the parts, the filenames, the
encodings and the total size. That is where a 554 5.6.0 comes from,
and it is the only way to see it without asking Office 365 again.

Nothing printed here includes a password. Addresses and subjects are
shown because the point is to identify the message.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import app, db                                   # noqa: E402

#: What Office 365 says when it will not accept the message at all.
MIME_CODES = (554, 550)


def _rows(args):
    from app.models.mailops import EmailOutbox
    q = EmailOutbox.query
    if args.id:
        return q.filter(EmailOutbox.id.in_(args.id)).all()
    q = q.filter(EmailOutbox.status == 'failed')
    if not args.all:
        q = q.filter(db.or_(EmailOutbox.smtp_code.in_(MIME_CODES),
                            EmailOutbox.last_error.like('%MIME%'),
                            EmailOutbox.last_error.like('%5.6.0%')))
    return q.order_by(EmailOutbox.created_at.desc()).limit(args.limit).all()


def _describe_attachments(row):
    """What the row asks to be attached, and what is actually there."""
    import json

    from app.services import outbox
    try:
        spec = json.loads(row.attachments_json or '[]')
    except Exception:
        print('      attachments_json is not valid JSON')
        return []
    if not spec:
        print('      (no attachments requested)')
        return []
    print(f'      {len(spec)} attachment(s) requested: {spec}')
    try:
        resolved = outbox.resolve_attachments(spec)
    except Exception as exc:                              # noqa: BLE001
        print(f'      ! resolve_attachments raised: {str(exc)[:160]}')
        return []
    if not resolved:
        print('      none of them resolved to a file on disk')
    for att in resolved:
        name = att.get('filename') or ''
        content = att.get('content') or b''
        ctype = att.get('content_type') or ''
        flags = []
        if not name:
            flags.append('NO FILENAME')
        if any(ord(c) > 126 for c in name):
            flags.append('NON-ASCII FILENAME')
        if any(c in name for c in '\r\n\t'):
            flags.append('CONTROL CHARACTER IN FILENAME')
        if len(name) > 200:
            flags.append('FILENAME OVER 200 CHARS')
        if not ctype or '/' not in ctype:
            flags.append(f'ODD CONTENT TYPE {ctype!r}')
        if not content:
            flags.append('EMPTY')
        print(f'      - {name!r}  {len(content):,} bytes  {ctype}'
              + ('   <<< ' + ', '.join(flags) if flags else ''))
    return resolved


def _rebuild(row, resolved):
    """Build the message the way the mailer does, and describe it."""
    from email.message import EmailMessage
    from email.utils import formataddr, make_msgid

    from app.services import mailer

    name, addr = mailer.sender()
    msg = EmailMessage()
    msg['Subject'] = (row.subject or '')[:250]
    msg['From'] = formataddr((name, addr))
    msg['To'] = row.to_addr or ''
    if row.cc_addr:
        msg['Cc'] = row.cc_addr
    msg['Message-ID'] = make_msgid(domain=addr.rsplit('@', 1)[-1])
    msg['Auto-Submitted'] = 'auto-generated'
    msg.set_content(row.text_body or mailer._text_from(row.html))
    msg.add_alternative(row.html or '', subtype='html')
    mailer._attach(msg, resolved, row.subject or '')

    raw = bytes(msg)
    print(f'      rebuilt message: {len(raw):,} bytes, '
          f'{len(list(msg.walk()))} MIME parts')
    for part in msg.walk():
        ctype = part.get_content_type()
        disp = part.get_content_disposition() or '-'
        fname = part.get_filename() or ''
        cte = part.get('Content-Transfer-Encoding', '-')
        print(f'        {ctype:<34} {disp:<12} {cte:<18} {fname!r}')

    # The things Exchange refuses outright.
    problems = []
    for header, value in msg.items():
        if any(ord(c) > 126 for c in str(value)):
            problems.append(f'non-ASCII in header {header}: {value[:60]!r}')
        if '\n' in str(value) or '\r' in str(value):
            problems.append(f'line break inside header {header}')
    if not (row.html or '').strip():
        problems.append('the HTML part is empty')
    if len(raw) > 35 * 1024 * 1024:
        problems.append(f'{len(raw):,} bytes is over the Exchange limit')
    for p in problems:
        print(f'      !! {p}')
    if not problems:
        print('      (nothing obviously malformed in the rebuilt message)')
    return problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--id', type=int, nargs='*',
                    help='specific email_outbox ids')
    ap.add_argument('--all', action='store_true',
                    help='every failed row, not only the MIME ones')
    ap.add_argument('--limit', type=int, default=20)
    ap.add_argument('--rebuild', action='store_true',
                    help='reassemble each message and describe its MIME')
    args = ap.parse_args()

    with app.app_context():
        print(f'database: {db.engine.url}\n')
        rows = _rows(args)
        if not rows:
            print('No matching rows. Try --all.')
            return
        print(f'{len(rows)} row(s)\n' + '=' * 72)
        for row in rows:
            print(f'\n  id {row.id}   {row.status}   '
                  f'attempts {row.attempts or 0}')
            print(f'      queued     {row.created_at}')
            print(f'      event      {row.event_key or "-"}')
            print(f'      to         {row.to_addr}')
            if row.cc_addr:
                print(f'      cc         {row.cc_addr}')
            print(f'      subject    {(row.subject or "")[:120]!r}')
            print(f'      entity     {row.entity_type or "-"} '
                  f'{row.entity_id or "-"}')
            print(f'      smtp code  {row.smtp_code or "-"}')
            print(f'      error      {(row.last_error or "")[:200]}')
            print(f'      html       {len(row.html or ""):,} bytes')
            print(f'      text       {len(row.text_body or ""):,} bytes')
            resolved = _describe_attachments(row)
            if args.rebuild:
                _rebuild(row, resolved)

        print('\n' + '=' * 72)
        print('Read-only: nothing was written, queued or sent.')


if __name__ == '__main__':
    main()
