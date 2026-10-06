"""
The decisions the leads mailbox makes, written down.

Four things live here, and they share a theme: an email that reaches
Procam must not disappear without a record of why.

  * **the log** — one row per message, whatever happened to it, so
    "did our enquiry reach you?" and "why is it not a lead?" are
    answerable. See `record`.
  * **reopening** — an enquiry against a lead somebody closed is new
    business, not a footnote on a dead record. See `reopen_for`.
  * **internal-only forwards** — a colleague forwarding a customer's
    enquiry sends mail whose visible addresses are all ours. Dropping
    those on the "no external sender" rule loses exactly the enquiries
    somebody cared enough to pass on. See `client_in_forward`.
  * **"Dear Suranjan, please take this up"** — a forward usually says
    who it is for, in the first line, in English. See
    `assignee_from_forward`.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

#: Stages that mean somebody decided this was over. An enquiry
#: arriving against one of these is the customer disagreeing.
CLOSED_STAGES = ('Lost', 'Not Interested', 'On Hold', 'Won')

#: What a reopened lead becomes. Configurable through Master Data —
#: a company that wants reopened leads to land in triage rather than
#: at RFQ Generated changes it there, not here.
DEFAULT_REOPEN_STAGE = 'RFQ Generated'
REOPEN_STAGE_SETTING = 'reopen_stage'

#: Classifications that count as new business arriving.
REOPENING_CLASSES = ('rfq', 'rfi', 'enquiry', 'new_lead', 'lead')

#: Company placeholder for a forward nobody could identify a client in.
NEEDS_REVIEW_COMPANY = 'To review'


# ── the log ──────────────────────────────────────────────────────────
def record(outcome, msg=None, *, lead_id=None, reason='', classifier_label='',
           confidence=None, raw_eml_path=None, commit=False, received_at=None):
    """Write down what happened to one message. Returns the row.

    Keyed on the message id, so the webhook and the five-minute poll
    seeing the same mail update one row rather than writing two. Never
    raises: the log is a record of the work, not the work.
    """
    from app import db
    from app.models.ingest_log import MailIngestLog
    from email_ingest import parser as email_parser

    msg = msg or {}
    imid = (msg.get('internetMessageId') or '').strip() or None
    try:
        row = (MailIngestLog.query.filter_by(internet_message_id=imid).first()
               if imid else None)
        if row is None:
            row = MailIngestLog(internet_message_id=imid)
            db.session.add(row)
        row.received_at = (received_at
                           or email_parser.received_datetime(msg)
                           or row.received_at)
        row.conversation_id = (msg.get('conversationId')
                               or row.conversation_id)
        sender = (((msg.get('from') or {}).get('emailAddress') or {})
                  .get('address') or '')
        row.from_addr = (sender or row.from_addr or '')[:320] or None
        row.subject = (msg.get('subject') or row.subject or '')[:500] or None
        row.outcome = outcome
        if lead_id is not None:
            row.lead_id = lead_id
        if reason:
            row.reason = reason[:500]
        if classifier_label:
            row.classifier_label = classifier_label[:40]
        if confidence is not None:
            try:
                row.confidence = int(confidence)
            except (TypeError, ValueError):
                pass
        if raw_eml_path:
            row.raw_eml_path = raw_eml_path[:600]
        if commit:
            db.session.commit()
        return row
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        log.exception('could not record the ingest outcome for %s', imid)
        return None


def already_ingested(internet_message_id):
    """Have we handled this exact message before?

    **The message id, and nothing else.** Not the conversation, not the
    thread, not the subject: a second RFQ in a running conversation is
    a second RFQ, and deduplicating on the thread is how a real enquiry
    gets swallowed by the one before it.
    """
    imid = (internet_message_id or '').strip()
    if not imid:
        return False
    try:
        from app import Lead
        from app.models.ingest_log import MailIngestLog
        if Lead.query.filter_by(email_message_id=imid).first():
            return True
        row = MailIngestLog.query.filter_by(internet_message_id=imid).first()
        return bool(row and row.outcome in ('created', 'attached', 'reopened'))
    except Exception:
        return False


# ── reopening ────────────────────────────────────────────────────────
def reopen_stage():
    try:
        from app.master_data import service as md
        items = md.items(REOPEN_STAGE_SETTING)
        if items:
            return items[0].code
    except Exception:
        pass
    return DEFAULT_REOPEN_STAGE


def should_reopen(lead, classification):
    if lead is None:
        return False
    if (lead.stage or '') not in CLOSED_STAGES:
        return False
    label = (classification or '').strip().lower()
    return any(key in label for key in REOPENING_CLASSES)


def reopen_for(lead, msg, *, classification='', actor='system'):
    """Bring a closed lead back, and say so where somebody will see it.

    Three records, deliberately: the stage moves, the lead's own
    history gains a line, and a notification goes to whoever owns it.
    A new RFQ silently attached to a dead lead is an enquiry nobody
    answers.
    """
    from datetime import datetime

    from app import db
    from email_ingest import parser as email_parser

    if not should_reopen(lead, classification):
        return False

    was = lead.stage
    subject = (msg or {}).get('subject') or '(no subject)'
    when = email_parser.received_datetime(msg) or datetime.utcnow()
    note = (f'Reopened by inbound email "{subject[:160]}" on '
            f'{str(when)[:16]}')

    lead.stage = reopen_stage()
    lead.stage_entered_at = datetime.utcnow()
    lead.updated_at = datetime.utcnow()
    lead.received_at = when
    if hasattr(lead, 'lost_reason'):
        lead.lost_reason = None
    existing = (lead.history or '').strip()
    lead.history = f'{note}\n{existing}'.strip()[:6000]

    try:
        db.session.add(_history_row(lead, was, lead.stage, note, actor))
    except Exception:
        pass

    try:
        from app.services import audit
        audit.record('lead.reopened', 'Lead', lead.id,
                     old={'stage': was}, new={'stage': lead.stage},
                     actor=actor, reason=note[:200])
    except Exception:
        pass

    try:
        from app.services import notification_rules as rules
        rules.dispatch('lead.reopened', lead, actor=actor, detail=note)
    except Exception:
        log.exception('could not announce the reopening of lead %s', lead.id)
    return True


def _history_row(lead, from_stage, to_stage, note, actor):
    from app import LeadStageHistory
    return LeadStageHistory(lead_id=lead.id, from_stage=from_stage,
                            to_stage=to_stage, changed_by=actor,
                            note=note[:400])


# ── internal-only forwards ───────────────────────────────────────────
_LABEL = re.compile(
    r'(?im)^[\s>]*(?:company|client|customer|account|organisation|'
    r'organization|m/s\.?|messrs\.?)\s*[:\-]\s*(.{2,120})$')

#: A signature block line that looks like a company: "Acme Projects
#: Pvt Ltd", "XYZ Engineering Limited". Deliberately requires a legal
#: suffix — without one this matches somebody's job title.
_SUFFIXED = re.compile(
    r'(?im)^[\s>]*([A-Z][\w&.,\'\- ]{2,80}?\s+'
    r'(?:pvt\.?\s*)?(?:private\s+)?(?:ltd|limited|llp|inc|corp|'
    r'corporation|co\.?|company|industries|engineering|projects|'
    r'logistics|enterprises|technologies)\.?)\s*$')


def client_in_forward(body_text, *, internal_domains=()):
    """Who the customer is, from inside a forwarded message.

    Looks at the quoted original's own From:, then at an explicit
    "Company:" line, then at a signature line with a legal suffix.
    Returns {'company', 'email', 'how'} — any of which may be empty.

    None of this is clever, and it does not need to be: it only has to
    beat throwing the email away.
    """
    from email_ingest import parser as email_parser

    out = {'company': '', 'email': '', 'how': ''}
    body = body_text or ''
    internal = tuple(d.lower() for d in (internal_domains or ()))

    def _external(addr):
        addr = (addr or '').lower()
        if '@' not in addr:
            return False
        domain = addr.rsplit('@', 1)[1]
        return not any(domain == d or domain.endswith('.' + d)
                       for d in internal)

    try:
        split = email_parser.split_forwarded_body(body)
        headers = split.get('headers') or {}
        raw_from = headers.get('from') or ''
        found = re.search(r'[\w.+\-]+@[\w.\-]+\.\w+', raw_from)
        if found and _external(found.group(0)):
            out['email'] = found.group(0).lower()
            out['how'] = 'forwarded From: header'
            name = raw_from.split('<')[0].strip(' "\'')
            if name and '@' not in name:
                out['company'] = name[:120]
    except Exception:
        pass

    if not out['company']:
        match = _LABEL.search(body)
        if match:
            out['company'] = match.group(1).strip(' .*_-')[:120]
            out['how'] = out['how'] or 'a "Company:" line in the body'

    if not out['company']:
        match = _SUFFIXED.search(body)
        if match:
            out['company'] = match.group(1).strip()[:120]
            out['how'] = out['how'] or 'a signature line'

    if not out['email']:
        for addr in re.findall(r'[\w.+\-]+@[\w.\-]+\.\w+', body):
            if _external(addr):
                out['email'] = addr.lower()
                out['how'] = out['how'] or 'an address in the quoted body'
                break
    return out


def match_account(company_name, email=None):
    """The CRM account this names, if the CRM already knows it."""
    if not (company_name or email):
        return None
    try:
        from app import Company
        from app.services.company_match import build_index, match
        index = build_index(Company.query.filter(
            Company.is_active.is_(True)).all())
        found, _reason, _cands = match(company_name or '', index)
        if found is not None:
            return found
    except Exception:
        pass
    if email and '@' in email:
        try:
            from app import Company
            domain = email.rsplit('@', 1)[1]
            return (Company.query
                    .filter(Company.website.ilike(f'%{domain}%'))
                    .first())
        except Exception:
            pass
    return None


# ── who it is for ────────────────────────────────────────────────────
_ADDRESSED = (
    re.compile(r'(?im)^\s*(?:dear|hi|hello|hey)\s+([A-Za-z][A-Za-z.\'\- ]{1,40})'
               r'\s*[,:\-]?\s*$'),
    re.compile(r'(?im)@([A-Za-z][A-Za-z.\'\-]{2,40})\b'),
    re.compile(r'(?im)\b([A-Za-z][A-Za-z.\'\- ]{2,40}?)\s*[,–\-]?\s*'
               r'(?:please|pls|kindly)\s+(?:take this up|handle|look into|'
               r'attend|action|quote)'),
)


def assignee_from_forward(body_text):
    """The employee a forward names, when it names exactly one.

    "Dear Suranjan", "@Suranjan", "Suranjan please take this up". The
    rule is deliberately strict — exactly one active employee matching
    exactly one name — because assigning a lead to the wrong person is
    worse than leaving it unassigned, which at least shows up on the
    triage screen.
    """
    from app import Employee

    body = (body_text or '')[:4000]
    names = []
    for pattern in _ADDRESSED:
        for found in pattern.findall(body):
            candidate = (found or '').strip(' .,:-').strip()
            if 2 < len(candidate) <= 40 and candidate.lower() not in names:
                names.append(candidate.lower())
    if not names:
        return None, ''

    try:
        people = Employee.query.filter(Employee.is_active.isnot(False)).all()
    except Exception:
        return None, ''

    for candidate in names:
        hits = []
        for emp in people:
            full = (emp.name or '').strip().lower()
            if not full:
                continue
            parts = full.split()
            if candidate == full or (parts and candidate == parts[0]) \
                    or (len(parts) > 1 and candidate == parts[-1]):
                hits.append(emp)
        # Exactly one, or it is a guess and we do not guess.
        unique = {e.emp_code: e for e in hits}
        if len(unique) == 1:
            emp = list(unique.values())[0]
            return emp, candidate
        if len(unique) > 1:
            log.info('forward names %r, which matches %d employees — '
                     'leaving it unassigned', candidate, len(unique))
    return None, ''


# ── rescuing an internal-only forward ────────────────────────────────
def rescue_internal_forward(msg, body_text, *, internal_domains=()):
    """A colleague forwards a customer's enquiry. Now what?

    Every visible address on that message is ours, so the classifier
    calls it internal and nothing is created — which loses exactly the
    enquiries somebody cared enough to pass on. This looks inside the
    quoted original instead.

    Returns one of:

        {'action': 'create', 'company', 'email', 'account_id', 'how'}
            a customer was identified; make the lead for them
        {'action': 'review', 'company': 'To review'}
            something came in that nobody can attribute. It still
            becomes a lead, flagged, because an enquiry sitting
            unexamined on a review screen is recoverable and one that
            was never recorded is not.

    It never returns "drop it". That is the whole point.
    """
    found = client_in_forward(body_text, internal_domains=internal_domains)
    company = (found.get('company') or '').strip()
    email = (found.get('email') or '').strip()

    if company or email:
        account = match_account(company, email)
        return {
            'action': 'create',
            'company': (getattr(account, 'name', None) or company
                        or (email.split('@')[1] if email else '')),
            'email': email,
            'account_id': getattr(account, 'id', None),
            'how': found.get('how') or 'the quoted message',
        }
    return {'action': 'review', 'company': NEEDS_REVIEW_COMPANY,
            'email': '', 'account_id': None,
            'how': 'nothing in the message identified a client'}


def looks_like_a_forwarded_quotation(msg, body_text):
    """Is this a quotation somebody forwarded in?

    A forwarded commercial offer means the enquiry has already been
    quoted, and a lead that sits at "New" while the customer is
    holding our price is a lead nobody chases.
    """
    text = f"{(msg or {}).get('subject') or ''}\n{body_text or ''}".lower()
    strong = ('quotation', 'quote no', 'quotation no', 'our offer',
              'commercial offer', 'price offer', 'proforma')
    return any(term in text for term in strong)


def notify_admin_review(lead, reason):
    """Tell the administrators a lead arrived that nobody could place."""
    try:
        from app.services import notification_rules as rules
        rules.dispatch('lead.needs_review', lead, actor='system',
                       detail=reason)
    except Exception:
        log.exception('could not raise the review notice for lead %s',
                      getattr(lead, 'id', '?'))
