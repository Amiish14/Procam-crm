"""Forward-as-attachment: read the message that is attached, not the note.

CRM-USER-ISSUES-02NOV §4, second pass. QUESTIONS.md 68 recorded this as
the one piece of §4 not built, because it needs a Graph call whose
behaviour I could not test from the laptop.

TWO KINDS OF FORWARD

Outlook's "Forward" puts the original message into the body of the new
one as text, under `---------- Forwarded message ----------`. The
parser unwraps that already, and `analyze_forward` promotes the
customer over the colleague who relayed it.

Outlook's "Forward as attachment" — and dragging a message onto a new
one, which is the default on Outlook for Mac — does something else
entirely. The enquiry becomes an `itemAttachment`: a whole Outlook
object hanging off the outer mail. The outer body is then the covering
note, or "FYI", or nothing at all.

`graph_client.list_attachments` filtered those out and said in its own
docstring that they "need different handling". So the CRM created a
lead from the covering note: right sender if a colleague had typed
one, no cargo, no route, no weights, no customer — and the enquiry
itself sitting in an attachment nobody read. The lead existed, which is
why this never showed up as a missing lead; it was simply empty.

WHY THIS RENDERS TEXT INSTEAD OF PARSING THE OBJECT

The attached message arrives as a Graph message object, which is the
same shape the ingest already handles. The tempting thing is to hand
that object to the parser instead of the outer one. This does not do
that, because the covering note matters: who forwarded it, what they
said about it, and the fact that it was relayed at all are all things
the CRM records, and swapping the object would lose them.

Instead the attached message is rendered into the exact text shape a
normal forward has, and spliced onto the end of the outer body. Then
`split_forwarded_body`, `analyze_forward`, the sender promotion, the
signals, the AI extraction and `forwarded_by` all work unchanged,
because from their point of view this is now an ordinary forward. One
code path for both kinds, and the one that was already proven on 501
real messages.

WHAT IT COSTS, AND THE GATE

One extra Graph call per message, so it is gated three ways before any
call is made:

    * `hasAttachments` must be true — free, already on the message;
    * the outer body must not *already* carry a resolvable forward,
      because then the enquiry is in hand and fetching is waste;
    * the message must not have been handled already.

Messages with only file attachments cost one call and find nothing.
That is the price of the ones that matter, and it is paid on the
create path, not on every refusal.
"""
from __future__ import annotations

import logging

from . import parser as email_parser

log = logging.getLogger(__name__)

#: The header the parser's `_FORWARD_BLOCK_RE` looks for. Written here
#: exactly as that pattern expects it, because the whole design is that
#: the rendered text is indistinguishable from a real forward.
FORWARD_HEADER = '---------- Forwarded message ----------'

#: More than this many attached messages on one mail and only the
#: first is spliced. Several attached enquiries are a different thing
#: — a batch somebody relayed — and guessing which one the lead is
#: about would be worse than saying so and letting a person look.
MAX_SPLICED = 1


def _addr(item, key='from'):
    """(address, display name) from a Graph message's from/sender."""
    holder = ((item.get(key) or {}).get('emailAddress') or {})
    return ((holder.get('address') or '').strip(),
            (holder.get('name') or '').strip())


def _recipients(item, key='toRecipients'):
    out = []
    for entry in (item.get(key) or []):
        addr = ((entry or {}).get('emailAddress') or {})
        name = (addr.get('name') or '').strip()
        mail = (addr.get('address') or '').strip()
        if mail and name and name.lower() != mail.lower():
            out.append(f'{name} <{mail}>')
        elif mail:
            out.append(mail)
    return out


def render(item):
    """One attached message as the text a forward would have carried.

    Deliberately plain: the parser reads `From:`, `Sent:`, `To:` and
    `Subject:` lines under the forward header, and anything cleverer
    here is something it has to learn.
    """
    address, name = _addr(item)
    from_line = (f'{name} <{address}>' if name and address
                 else address or name or 'unknown sender')
    sent = ((item.get('sentDateTime') or item.get('receivedDateTime')
             or '') or '').strip()

    lines = [FORWARD_HEADER, f'From: {from_line}']
    if sent:
        lines.append(f'Sent: {sent}')
    to_all = _recipients(item, 'toRecipients')
    if to_all:
        lines.append('To: ' + ', '.join(to_all))
    cc_all = _recipients(item, 'ccRecipients')
    if cc_all:
        lines.append('Cc: ' + ', '.join(cc_all))
    lines.append(f'Subject: {(item.get("subject") or "").strip()}')
    lines.append('')
    lines.append(email_parser._get_body_text(item) or '')
    return '\n'.join(lines)


def describe(item):
    """What one attached message is, for a log line or a dry run."""
    address, name = _addr(item)
    body = email_parser._get_body_text(item) or ''
    return {
        'from': address,
        'from_name': name,
        'subject': (item.get('subject') or '').strip(),
        'sent': ((item.get('sentDateTime')
                  or item.get('receivedDateTime') or '') or '').strip(),
        'to': _recipients(item, 'toRecipients'),
        'body_chars': len(body),
        'body_head': body[:400],
        'attachment_name': item.get('_attachment_name') or '',
    }


def already_a_forward(msg):
    """Whether the outer body already carries the original.

    An ordinary forward needs nothing from this module, and splicing
    on top of one would give the parser the same message twice.
    """
    try:
        fwd = email_parser.analyze_forward(dict(msg or {}))
        return bool(fwd.get('promoted'))
    except Exception:                                     # noqa: BLE001
        return False


def find(msg, graph, mailbox):
    """The messages attached to this one. [] when there are none.

    Never raises: a Graph failure here must not lose the mail. The
    outer message still becomes a lead, exactly as it did before this
    module existed.
    """
    if not msg or not msg.get('hasAttachments'):
        return []
    message_id = (msg.get('id') or '').strip()
    if not message_id or graph is None or not mailbox:
        return []
    try:
        return graph.list_item_attachments(mailbox, message_id) or []
    except Exception:                                     # noqa: BLE001
        log.exception('could not read the attached messages on %s',
                      msg.get('internetMessageId') or message_id)
        return []


def unwrap(msg, graph, mailbox):
    """Splice an attached message into `msg`'s body, in place.

    Returns {'spliced': bool, 'found': int, 'skipped': str,
             'messages': [describe(...)]} — a report, so the caller can
    say what happened in the ingest log rather than only in a debug
    line.

    `msg` is mutated on purpose. Everything downstream — the parser,
    the classifier, the AI extraction, `original_email_body` — reads
    the body off this dict, and the point is that all of them see the
    enquiry rather than the covering note.
    """
    report = {'spliced': False, 'found': 0, 'skipped': '', 'messages': []}
    if not msg:
        report['skipped'] = 'no message'
        return report
    if not msg.get('hasAttachments'):
        report['skipped'] = 'no attachments'
        return report
    if already_a_forward(msg):
        # The enquiry is in the body already. Nothing to fetch, and
        # splicing would hand the parser two copies of it.
        report['skipped'] = 'the body already carries the forward'
        return report

    items = find(msg, graph, mailbox)
    report['found'] = len(items)
    if not items:
        report['skipped'] = 'no attached messages'
        return report

    report['messages'] = [describe(i) for i in items]
    if len(items) > MAX_SPLICED:
        # Said, not guessed. The outer mail still becomes a lead and
        # the count is on the record.
        report['skipped'] = (f'{len(items)} messages are attached — only '
                             f'the first is read')

    rendered = render(items[0])
    if not rendered.strip():
        report['skipped'] = 'the attached message was empty'
        return report

    # The outer body is converted to text first. Appending plain text
    # to an HTML body would leave `_get_body_text` running an HTML
    # conversion over the rendered headers, which eats anything with
    # an angle bracket in it — including every address.
    outer = email_parser._get_body_text(msg) or ''
    msg['body'] = {
        'contentType': 'text',
        'content': (outer + '\n\n' + rendered) if outer.strip() else rendered,
    }
    # bodyPreview is what several skip checks read when the body is
    # empty; leaving the old one would keep saying "FYI".
    msg['bodyPreview'] = (msg.get('bodyPreview') or '')[:255] or \
        rendered[:255]
    # `analyze_forward` caches its verdict on `msg['_forward']` and
    # returns it unconditionally on the next call. Anything that asked
    # before the splice cached "not a forward" against the covering
    # note, and that stale answer would outlive the body we just
    # replaced — the splice would land and change nothing.
    msg.pop('_forward', None)
    msg['_attached_message_spliced'] = True
    report['spliced'] = True
    return report
