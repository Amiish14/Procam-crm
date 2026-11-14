"""
v2026-08 — Process a single email → Lead. Reused by both the poll pipeline
and the webhook handler so both routes end at the same DB state.

v2026-09 — CAPTURE EVERYTHING. leads@procamgroup.in exists for one purpose:
employees forward leads into it. So the mailbox itself is the filter, and
every message that arrives becomes a Lead — no content filtering, no
confidence threshold, no junk detection. What the pipeline DOES do is
unwrap the forward, so the Lead is the ORIGINAL prospect inside it rather
than the employee who pressed Forward. See docs/LEAD_INGESTION_POLICY.md.

Public function:

    process_single_message(graph, mailbox, msg) -> dict

The message dict is a Microsoft Graph "message" resource (as returned by
list_messages / get_message). Returns a status dict:

    {'status': 'created' | 'skipped' | 'failed',
     'reason': str,                            # only when skipped / failed
     'lead_id': int | None,
     'internet_message_id': str | None}
"""
from __future__ import annotations
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

from . import parser as email_parser
from . import ai_extractor
from . import ai_router                                              # noqa
from . import attachments as attachments_mod
from . import enrich as enrich_mod
from . import blocklist

log = logging.getLogger(__name__)

DEDUP_DOMAIN_WINDOW_DAYS = 30


def _internet_message_id(msg: dict) -> Optional[str]:
    return msg.get('internetMessageId') or msg.get('id')


class _IntakeDisabled(Exception):
    """Raised to skip classification when LEAD_INTAKE_MODE=off."""


def _stamp_quote(lead, mail_row, got, log):
    """Write the quote facts onto the lead's opp_notes.

    opp_notes is already the lead's JSON side-channel, so this adds to it
    rather than claiming new columns for something only quote emails
    carry.
    """
    import json as _json
    try:
        current = _json.loads(lead.opp_notes or '{}')
        if not isinstance(current, dict):
            current = {}
    except Exception:
        current = {}
    quote = {k: (str(v) if v is not None else None) for k, v in got.items()}
    quote['recorded_from'] = mail_row.message_id if mail_row else None
    current['last_quote'] = quote
    lead.opp_notes = _json.dumps(current, default=str)
    # Offered to the PIC on the lead as a suggestion, not written into the
    # quote value: an amount read from an email counts only once a person
    # confirms it (app/services/lead_value.quote_suggestion).
    log.info('quote recorded on lead %s: %s', lead.id, quote)


def _thread_key_for(msg, sender=None):
    """The stored thread key, keyed on the customer's own address."""
    try:
        from app.services import lead_thread
        return lead_thread.key_for_message(msg, sender=sender) or None
    except Exception:
        return None


def _thread_keys(msg):
    """Conversation and RFC-822 threading headers, in one shape."""
    try:
        from app.services import lead_intake as _li
        return _li.thread_keys(msg)
    except Exception:
        return {}



def _log_blocked_intake(db, msg, verdict, sender_email, log):
    """Record an enquiry that was refused because its client is blocked.

    Written to the same EmailEvent log the rest of the intake uses, with
    status 'blocked_client', so the Intake screens can show it under
    "Intake — Blocked" beside everything else that arrived. The mail
    itself is not deleted or hidden — somebody has to be able to see
    that a blocked client is still writing in.
    """
    try:
        from app import EmailEvent
        db.session.add(EmailEvent(
            received_at=email_parser.received_datetime(msg),
            mailbox=(msg.get('_mailbox') or ''),
            internet_message_id=(msg.get('internetMessageId') or '')[:400],
            subject=(msg.get('subject') or '')[:500],
            status='blocked_client',
            reason=(f'{verdict.company_name} is blocked — '
                    f'{verdict.reason_summary}')[:500],
            classification='blocked_client',
            decided_by='restriction-register',
            payload_json=None,
            lead_id=None))
    except Exception:
        log.exception('could not write the blocked-intake event')



def _record_ingest(outcome, msg, decision=None, *, lead_id=None, reason=''):
    """One row per message in mail_ingest_log, whatever happened.

    Wrapped rather than called directly so a failure to write the log
    can never be the thing that loses an email.
    """
    try:
        from app.services import mail_ingest as _mi
        _mi.record(outcome, msg, lead_id=lead_id, reason=reason,
                   classifier_label=getattr(decision, 'klass', '') or '',
                   confidence=getattr(decision, 'confidence', None),
                   commit=True)
    except Exception:
        log.exception('could not record the ingest outcome')


def _reopen_if_closed(db, decision, msg, log):
    """Reopen the lead this belongs to, if it was closed. Returns the
    note written, or None."""
    from app import Lead
    from app.services import mail_ingest as _mi

    lead = db.session.get(Lead, decision.lead_id)
    if lead is None:
        return None
    if not _mi.should_reopen(lead, decision.klass, msg=msg):
        return None
    if _mi.reopen_for(lead, msg, classification=decision.klass):
        return (f'Reopened from {lead.stage!r}: '
                f'{decision.klass} on a closed lead')
    return None


def _retry_once_after_a_clash(db, error, msg, imid, lead, decision, log):
    """One retry where the clash was a reference, not a duplicate.

    Returns the LEAD when the retry landed one, else None.

    The lead rather than a finished result dict, and the ingest row is
    written by the caller: `tests/test_unprocessed_mail_log.py` walks
    this file and fails any branch that returns without logging, and
    it verifies the helper a branch delegates to rather than assuming
    it -- which it cannot do through a variable. Logging at the return
    site keeps that guard able to check this path instead of having to
    trust it.

    Deliberately once: a second collision on a fresh number is not a
    race any more, it is a broken sequence, and retrying a broken
    sequence in a loop is how an intake stops answering.
    """
    from app import Lead

    text = str(error or '')
    if 'UNIQUE constraint failed' not in text \
            and 'duplicate key value' not in text.lower():
        return None
    # An already-ingested message is not a loss and not a retry.
    if imid and Lead.query.filter_by(email_message_id=imid).first():
        log.info('%s already has a lead; the clash was the webhook and '
                 'the poll racing, not a lost enquiry', imid)
        return None
    if 'crn' not in text and 'email_message_id' in text:
        return None
    if lead is None:
        return None
    try:
        # The SAME object, with the clashing reference cleared, rather
        # than a lead rebuilt from `lead_kwargs`: the real insert adds
        # a dozen fields of its own beside those kwargs -- the company,
        # the sender, the thread key, the classification -- and a
        # rebuild from the kwargs alone would land a near-empty lead
        # that looked like a successful recovery.
        #
        # Its INSERT never completed, so after the caller's rollback it
        # is transient again and `add` makes it pending with a fresh
        # reference taken on flush.
        lead.crn = None
        db.session.add(lead)
        db.session.commit()
    except Exception:
        db.session.rollback()
        log.exception('the retry after the reference clash failed too '
                      'for %s', imid)
        return None
    log.info('lead %s landed on the retry after a reference clash (%s)',
             lead.id, imid)
    return lead


def _is_placeholder_company(name):
    """Whether this company name identifies nobody.

    `lead_account.is_placeholder` is the one list of these -- "To
    review", "Unknown", a subject line -- read from Master Data on top
    of its seeded defaults, so a portal lead and the lead sheet agree
    about what counts as unnamed.
    """
    try:
        from app.services import lead_account
        return bool(lead_account.is_placeholder(name or ''))
    except Exception:                                     # noqa: BLE001
        return not (name or '').strip()


def _desk_for_a_guessed_vertical(lead):
    """The RFQ desk's code, where this lead must not be routed on a guess.

    CRM-FINAL-13NOV item 11. Two things have to hold: the lead is
    already flagged for review, and its vertical is a guess rather
    than something stated, taken from the customer's own record, or
    read off an attachment. Both, because a confident guess on a lead
    nobody has questioned is how most leads get their vertical and
    routing those normally is right.

    The desk is an Admin setting, read at the time, so naming the
    person is a screen rather than a release. With nobody named it
    returns '' and the lead goes to the ordinary rules -- a lead with
    no owner at all would be worse than one with an owner to correct.
    """
    try:
        from app.services import lead_vertical
        if not getattr(lead, 'needs_review', False):
            return ''
        # CRM-FINAL-13NOV follow-up item 1. A portal RFQ goes to the
        # desk whatever its vertical reads as: the buyer was read out
        # of a robot's mail, so the one thing certain about it is that
        # nobody has looked at it yet. That is the desk's job.
        if not (getattr(lead, 'lead_platform', '') or '').strip():
            if lead_vertical.is_firm(
                    getattr(lead, 'vertical_confidence', None),
                    getattr(lead, 'vertical_source', None)):
                return ''
        from app.services import admin_settings
        code = (admin_settings.get('rfq_coordinator') or '').strip().upper()
        if not code:
            return ''
        from app import Employee
        person = Employee.query.filter_by(emp_code=code).first()
        if person is None or not getattr(person, 'is_active', False):
            return ''
        return code
    except Exception:                                     # noqa: BLE001
        return ''


def _file_against_lead(db, decision, msg, extracted, log,
                       graph=None, mailbox=None):
    """File an email that is not a new lead against the lead it belongs to.

    This is the difference between a classifier and a shredder. A reply,
    a forward, a quotation or a rate request all carry information about
    a live enquiry; declining to create a *lead* is not a reason to lose
    the *email*. One with no lead to attach to is recorded and left.
    """
    if not decision.lead_id:
        return None
    try:
        from app import LeadEmail, Lead
        from app.services import lead_intake as _li

        message_id = (msg.get('internetMessageId') or '').strip() or None
        if message_id and LeadEmail.query.filter_by(
                lead_id=decision.lead_id, message_id=message_id).first():
            return None                       # already on the trail

        keys = _thread_keys(msg)
        # Only what our own people send is outbound. The classifier gives
        # the same two classes to mail coming IN: a known supplier's reply
        # (rule 6) and an agent quoting Procam (rule 7). Those were filed
        # as sent by us, moved the lead to Quoted before Procam had quoted
        # anyone, and wrote the agent's buying price into
        # quoted_amount_inr. Rule 4 is the "sender is one of ours" branch.
        from_us = decision.step == 4
        outbound = from_us and decision.klass in (_li.Klass.QUOTE,
                                                  _li.Klass.RATE_SOURCING)
        row = LeadEmail(
            lead_id=decision.lead_id,
            direction='outbound' if outbound else 'inbound',
            from_addr=_li.sender(msg) or None,
            to_addr=', '.join(_li.recipients(msg, 'toRecipients')) or None,
            cc=', '.join(_li.recipients(msg, 'ccRecipients')) or None,
            subject=(msg.get('subject') or '')[:500] or None,
            body=(_li.body_text(msg) or '')[:8000] or None,
            sent_or_received_at=email_parser.received_datetime(msg),
            source='ingested',
            status='received',
            message_id=message_id,
            conversation_id=keys.get('conversation_id'),
            in_reply_to=keys.get('in_reply_to'),
            references_header=keys.get('references'),
            intake_class=decision.klass,
        )
        db.session.add(row)
        db.session.flush()

        # A reply can carry the thing that matters — a revised BOQ, the
        # drawings that were missing, the client's answer to a query —
        # and until now only its text was kept. Behind
        # FEATURE_REPLY_CAPTURE the reply's own attachments are recorded
        # against the lead and the message itself is kept, the same way
        # the first email in the thread is.
        try:
            from app.services import flags
            if flags.on('FEATURE_REPLY_CAPTURE') and decision.lead_id \
                    and graph is not None and mailbox:
                from app.services import rfq_capture
                _lead = db.session.get(Lead, decision.lead_id)
                if _lead is not None:
                    _cap = rfq_capture.capture_for_lead(
                        _lead, graph=graph, mailbox=mailbox, msg=msg,
                        lead_email_id=row.id)
                    if _cap['attachments'] or _cap['raw'] == 'stored':
                        log.info('reply on lead %s kept %s file(s), '
                                 'original: %s', decision.lead_id,
                                 _cap['attachments'], _cap['raw'])
        except Exception:
            log.exception('could not capture the reply on lead %s',
                          decision.lead_id)

        # CRM-MAIL-2026-11-11 §3+§5 — "flag it customer chasing". The
        # reminder that asked for the D&D estimate as of 15 Nov instead
        # of 5 Nov is information about us, not about the enquiry: the
        # customer has asked twice and is still waiting. On the trail
        # it was one more row. Outbound mail clears it, because that is
        # what answering a chase is.
        try:
            from app.services import mail_chase
            _lead_row = db.session.get(Lead, decision.lead_id)
            if _lead_row is not None:
                if outbound:
                    mail_chase.answered(_lead_row)
                else:
                    _phrase = mail_chase.is_a_chase(
                        _lead_row,
                        subject=(msg.get('subject') or ''),
                        body=(_li.body_text(msg) or ''),
                        from_addr=(_li.sender(msg) or ''))
                    if _phrase and mail_chase.flag(
                            _lead_row, phrase=_phrase,
                            when=email_parser.received_datetime(msg),
                            message_id=message_id):
                        log.info('lead %s: the customer is chasing '
                                 '(%r, %s so far)', _lead_row.id,
                                 _phrase, _lead_row.chase_count)
        except Exception:
            log.exception('could not record the chase on lead %s',
                          decision.lead_id)

        # A quotation going out moves the enquiry on. Nothing else here
        # changes a stage: an inbound reply is information, not progress,
        # and advancing a lead on one would corrupt the pipeline.
        if decision.klass == _li.Klass.QUOTE and outbound:
            lead = db.session.get(Lead, decision.lead_id)
            if lead is not None:
                if (lead.stage or '') not in ('Quoted', 'Won', 'Lost',
                                              'Not Interested'):
                    lead.stage = 'Quoted'
                # §6 — record what was quoted, not just that something
                # was. A stage change with no reference or amount tells
                # nobody anything a month later.
                try:
                    from app.services import quote_details
                    got = quote_details.extract(
                        f"{msg.get('subject') or ''}\n{_li.body_text(msg)}",
                        received=email_parser.received_datetime(msg),
                        attachments=_li.attachment_names(msg))
                    _stamp_quote(lead, row, got, log)
                except Exception:
                    log.exception('quote detail extraction failed for %s',
                                  decision.lead_id)
        return row
    except Exception:
        log.exception('could not file %s against lead %s',
                      msg.get('internetMessageId'), decision.lead_id)
        return None



def auto_assign_owners(lead, sender_email, *, subject='', body=''):
    """§12-§15 — give a new lead its two PICs, with no admin.

    The whole point of the engine: a genuine enquiry from a known
    account reaches its owners on arrival. Until this existed only the
    review-accept path assigned anyone, so every lead the classifier was
    confident enough to create straight through — the case §26 says must
    need no admin at all — arrived with no owner.

    Returns (primary, secondary), either of which may be None. An
    unmapped account is not a failure: §14 sends it to an admin queue,
    and the lead is created either way. Never raises — assignment is not
    worth losing a lead over.
    """
    try:
        from app.services import lead_assignment
        from app.services import lead_intake_db as _asdb

        # The better matcher first, where it is switched on: five
        # rules against two, free mail excluded from domain matching,
        # and whatever a person's corrections have already taught it.
        account, _how = None, ''
        try:
            from app.services import account_match, flags
            if flags.on('FEATURE_AUTO_ASSIGN'):
                account, _how = account_match.resolve(
                    sender_email, company_name=getattr(lead, 'company', ''),
                    text=f'{subject}\n{body}')
        except Exception:
            log.exception('the account matcher failed — falling back')
            account = None
        if account is None:
            account, _how = _asdb.resolve_account(
                sender_email, text_for_gstin=f'{subject}\n{body}')

        # CRM-ACCOUNT-FIRST-01. Until now the account was resolved
        # and then thrown away: it picked an owner and `company_id`
        # stayed null, so a third of the open pipeline belongs to
        # nobody's account and no account-level view can see it.
        # Written only where linking needs no judgement — a learned
        # sender, or one exact rule with nothing disagreeing. The
        # rest go to Account check, which is the point of the queue.
        #
        # CRM-MAIL-2026-11-11 §7, two corrections. It only ran while
        # FEATURE_ACCOUNT_FIRST was on, so every lead ingested before
        # 10 November landed on no account however obvious the
        # customer was — which is most of the 277 the stuck report
        # counts. Linking a lead to its account is right whether or
        # not a gate is reading the answer, so the flag no longer
        # decides it.
        #
        # And an exact normalised-name match now links too, through
        # `auto_link_by_name`: ANDRITZ HYDRO wrote in, the account
        # existed under a name that normalises identically, and
        # nothing linked because the account did not carry their
        # domain.
        try:
            if not getattr(lead, 'company_id', None):
                from app.services import account_match as _am
                linked, how = _am.auto_link(
                    sender_email,
                    company_name=getattr(lead, 'company', ''),
                    text=f'{subject}\n{body}')
                if linked is None:
                    linked, how = _am.auto_link_by_name(
                        sender_email,
                        company_name=getattr(lead, 'company', ''),
                        text=f'{subject}\n{body}')
                if linked is not None:
                    lead.company_id = linked.id
                    log.info('lead %s linked to account %s (%s)',
                             getattr(lead, 'id', '?'), linked.id, how)
                    try:
                        from app.services import audit
                        audit.record(
                            'lead.account_linked', 'lead',
                            getattr(lead, 'id', None),
                            new={'company_id': linked.id,
                                 'account': linked.name, 'rule': how},
                            actor='system',
                            reason=f'linked on intake by {how}')
                    except Exception:                     # noqa: BLE001
                        pass
        except Exception:                                 # noqa: BLE001
            log.exception('account linking failed for lead %s — the '
                          'lead still lands, in Account check',
                          getattr(lead, 'id', '?'))

        primary, secondary = _asdb.owners_for(account)
        if not primary:
            # No owner on the account, or no account at all. Rather
            # than the unmapped queue — where a lead sat with nobody
            # — the rules give it a real owner and ask the head to
            # confirm it in the morning.
            #
            # CRM-FINAL-13NOV item 11, FIRST. A heavy-transport tender
            # was filed under Warehousing and then routed to the
            # Warehousing owner, who is not the person who should have
            # seen it. The vertical was a guess; the routing treated
            # it as a fact. On a lead already flagged for review,
            # whose vertical is a guess, the owner is the RFQ desk --
            # the one seat whose job is to look at an enquiry nobody
            # has placed yet. Anything else (stated, the account's
            # own, or a confident call) routes exactly as before.
            _desk = _desk_for_a_guessed_vertical(lead)
            if _desk:
                # AND WRITTEN ONTO THE LEAD, which it was not. This
                # branch returned the desk's code and never assigned
                # it, and its caller ignores the return value -- so
                # item 11's routing picked the right seat and the lead
                # arrived with no owner at all, which is the state the
                # branch above it exists to prevent. Every sibling
                # path here goes through `lead_assignment.assign`;
                # this one has to as well.
                _ok, _err = lead_assignment.assign(
                    lead, primary_code=_desk, actor=None,
                    note='routed to the RFQ desk: the vertical is a '
                         'guess on a lead flagged for review',
                    _defer_commit=True)
                if _ok:
                    log.info('lead %s: the vertical is a guess on a '
                             'review lead — routed to the RFQ desk '
                             "rather than to %s's owner", lead.id,
                             getattr(lead, 'procam_vertical', '') or '?')
                    return _desk, None
                # Refused: fall through to the ordinary rules rather
                # than leave the lead with nobody.
                log.warning('lead %s could not be routed to the RFQ '
                            'desk (%s) — falling back to the rules',
                            lead.id, _err)
            try:
                from app.services import auto_assign
                decided = auto_assign.assign_for(
                    lead, account=account, sender_email=sender_email,
                    actor='system', commit=False)
                if decided:
                    log.info('lead %s auto-assigned to %s (%s)', lead.id,
                             decided['emp_code'], decided['rule'])
                    return decided['emp_code'], None
            except Exception:
                log.exception('auto-assign failed for lead %s', lead.id)
            log.info('lead %s has no configured owner (%s) — unmapped '
                     'account queue', lead.id,
                     account.name if account else 'no account')
            return None, None

        ok, err = lead_assignment.assign(
            lead, primary_code=primary, secondary_code=secondary,
            actor=None, note='assigned automatically on intake',
            _defer_commit=True)
        if not ok:
            log.warning('auto-assign refused for lead %s: %s', lead.id, err)
            return None, None
        log.info('lead %s auto-assigned to %s / %s',
                 lead.id, primary, secondary or '-')
        return primary, secondary
    except Exception:
        log.exception('auto-assignment failed for lead %s',
                      getattr(lead, 'id', '?'))
        return None, None

def _attach_outbound(lead, msg, body, account, to_addr, imid):
    """File our own outbound mail on the customer's open lead.

    Returns the same shape the ingest returns for anything it
    attaches rather than creates, so the caller and the admin
    screens see no special case.

    The lead's account is filled in if it was missing — the
    recipient told us who the customer is, which is the one thing
    the old path threw away.
    """
    from app import LeadEmail, db
    from app.services import audit
    from app.services import lead_intake as _li

    try:
        message_id = (msg.get('internetMessageId') or '').strip() or None
        if message_id and LeadEmail.query.filter_by(
                lead_id=lead.id, message_id=message_id).first():
            return {'status': 'skipped', 'reason': 'already on the trail',
                    'lead_id': lead.id, 'internet_message_id': imid}

        db.session.add(LeadEmail(
            lead_id=lead.id, direction='outbound',
            from_addr=_li.sender(msg) or None,
            to_addr=', '.join(_li.recipients(msg, 'toRecipients'))
            or to_addr,
            cc=', '.join(_li.recipients(msg, 'ccRecipients')) or None,
            subject=(msg.get('subject') or '')[:500] or None,
            body=(body or '')[:8000] or None,
            source='ingested', status='received',
            message_id=message_id,
            intake_class='outbound_to_customer'))

        if not lead.company_id:
            lead.company_id = account.id
            audit.record('lead.account_linked', 'lead', lead.id,
                         new={'company_id': account.id,
                              'account': account.name},
                         actor='system',
                         reason=f'our own mail to {to_addr} named the '
                                f'customer')
        db.session.commit()
        _record_ingest('attached', msg, None, lead_id=lead.id,
                       reason=f'our own mail to {to_addr}, filed on '
                              f'{account.name}')
        return {'status': 'attached',
                'reason': f'outbound mail filed on lead {lead.id}',
                'lead_id': lead.id,
                'classification': 'outbound_to_customer',
                'internet_message_id': imid}
    except Exception:
        db.session.rollback()
        log.exception('could not file outbound mail %s on lead %s',
                      imid, getattr(lead, 'id', '?'))
        return {'status': 'error', 'reason': 'outbound filing failed',
                'lead_id': getattr(lead, 'id', None),
                'internet_message_id': imid}


def process_single_message(graph, mailbox: str, msg: dict, *,
                           force: bool = False,
                           override_classifier: bool = False,
                           override_purge: bool = False) -> dict:
    """Process ONE Graph message into a Lead. Idempotent — safe to call
    multiple times for the same message; only the first call creates a
    Lead. Any subsequent call returns 'skipped' with reason='already ingested'.

    A message the classifier already decided against (internal, reply,
    review …) is not classified again either: Graph re-delivers a
    notification whenever the webhook answers slowly, and each pass
    repeated the AI call, filed another review-queue row and could reach
    a different verdict. force=True re-runs it — for an administrator's
    explicit retry.
    """
    # Local imports keep this module import-safe under all circumstances.
    from app import app, db, Lead, LeadAttachment, EmailEvent  # type: ignore
    from app import EmailClassification  # type: ignore
    # BOUND HERE, ONCE, AND BEFORE ANYTHING READS IT.
    #
    # 14 Nov, and it had already cost two releases. `_li` was imported
    # at the classifier, two-thirds of the way down this function, and
    # read twice above it: by the promotional rule (13 Nov, item 13)
    # and by the portal rule (14 Nov, item 1). A name a function
    # imports anywhere is local to the whole of it, so both reads
    # raised UnboundLocalError -- and both sit inside a `try` that
    # logs and carries on, so neither feature did anything at all on
    # live while every test that asserted on the rule itself passed.
    # The rules were right; they were never reached.
    from app.services import lead_intake as _li
    # Same reason, and the same mistake twice over: `portal_rfq` was
    # imported inside two separate `try` blocks and read outside both.
    from app.services import portal_rfq as _prf

    imid = _internet_message_id(msg)

    with app.app_context():
        # ── Idempotency (kept — same physical email must not double-insert) ──
        if imid:
            hit = (db.session.query(Lead.id)
                   .filter(Lead.email_message_id == imid).first())
            if hit:
                # CRM-USER-ISSUES-02NOV §4. _record_ingest's own
                # docstring says "one row per message, whatever
                # happened"; four paths in this function returned
                # without writing one, and this was the first. A
                # message the CRM deliberately dropped and a message
                # that never arrived looked identical, which is what
                # "forwarded mail never reaches the CRM" looks like
                # from the outside.
                _record_ingest('skipped', msg, lead_id=hit[0],
                               reason='already ingested')
                return {'status': 'skipped', 'reason': 'already ingested',
                        'lead_id': hit[0], 'internet_message_id': imid}

            # ── Respect a purge ───────────────────────────────────────
            # Deleting a Lead removes the email_message_id that dedup keys
            # on, so without this a re-poll or replay resurrects every
            # newsletter the team just cleared out. The purge marks the
            # EmailEvent 'lead_deleted'; that verdict is permanent.
            purged = (db.session.query(EmailEvent.id)
                      .filter(EmailEvent.internet_message_id == imid,
                              EmailEvent.status == 'lead_deleted').first())
            if purged and override_purge:
                # 13 Nov. `force` overrides the denylist, the
                # system-sender block and the classifier's memo, and
                # not this -- rightly, because a purge is a person
                # saying "this was not business", and a re-poll must
                # not undo that. But an operator re-processing a
                # backlog of procurement-portal mail HAS made the
                # opposite decision, deliberately and about these
                # messages, and without a way to say so the enquiries
                # among them can never be recovered.
                #
                # So: only when asked for in those words, never by
                # the re-process button, and written down twice --
                # an audit row and the ingest reason -- because
                # overriding somebody's deletion silently would be
                # worse than refusing to do it at all.
                log.warning('%s was purged as irrelevant; the purge is '
                            'being overridden deliberately', imid)
                try:
                    from app.services import audit
                    audit.record(
                        'mail.purge_overridden', 'email', imid,
                        old={'status': 'lead_deleted'},
                        new={'status': 're-processed'},
                        actor='portal backlog',
                        reason='re-processed from the portal backlog: '
                               'the message was purged as irrelevant '
                               'and an operator asked for it back',
                        commit=True)
                except Exception:                         # noqa: BLE001
                    log.exception('could not record the override of '
                                  'the purge on %s', imid)
                purged = None
            if purged:
                # §4 — a permanent verdict, and until now a silent
                # one. Worth a row precisely because it cannot be
                # undone by a re-poll: somebody asking "where did our
                # enquiry go?" needs to be told it was purged.
                _record_ingest('skipped', msg,
                               reason='previously purged as irrelevant '
                                      '— the verdict is permanent')
                return {'status': 'skipped',
                        'reason': 'previously purged as irrelevant',
                        'lead_id': None, 'internet_message_id': imid}

        # ── Mail from a machine ───────────────────────────────────
        # Before the parse and before the AI call: a quarantine
        # digest costs a model call and becomes a lead called
        # "Microsoft" that somebody has to close. Held, logged and
        # visible on the Mail ingest screen under `blocked`, never
        # deleted — and `force` overrides it, because a person
        # replaying a message by hand has already decided.
        if not force:
            try:
                from app.services import system_senders
                if system_senders.enabled():
                    sender = (((msg.get('from') or {})
                               .get('emailAddress') or {})
                              .get('address') or '')
                    hit, pattern, why = system_senders.blocks(sender)
                    if hit:
                        told = (f'{system_senders.REASON}: {sender} '
                                f'matches {pattern} — {why}')
                        _record_ingest('blocked', msg, reason=told)
                        return {'status': 'skipped', 'reason': told,
                                'lead_id': None,
                                'internet_message_id': imid}
            except Exception:
                # A failure here must not lose an email: fall
                # through and treat it as ordinary mail.
                log.exception('sender block check failed')

        rfc_id = (msg.get('internetMessageId') or '')[:400]
        if rfc_id and not force:
            decided = (db.session.query(EmailClassification.classification,
                                        EmailClassification.matched_lead_id)
                       .filter(EmailClassification.message_id == rfc_id,
                               EmailClassification.created_lead_id.is_(None))
                       .order_by(EmailClassification.id.desc()).first())
            if decided:
                _record_ingest('skipped', msg, lead_id=decided[1],
                               reason=f'{decided[0]}: already classified')
                return {'status': 'skipped',
                        'reason': f'{decided[0]}: already classified',
                        'lead_id': decided[1],
                        'classification': decided[0],
                        'internet_message_id': imid}

        # ── Parse + AI-extract ────────────────────────────────────────
        # CAPTURE EVERYTHING. Every message in the leads inbox becomes a
        # Lead — the inbox is the filter. parser.extract_lead unwraps the
        # forward so `extracted` describes the ORIGINAL customer message
        # rather than the employee who forwarded it; anything the parser
        # would have wanted to skip is kept as a 'triage_tag' in opp_notes
        # so the sales team can sort in the UI. Silently dropping mail =
        # losing business.
        # CRM-USER-ISSUES-02NOV §4 — "forward as attachment". The
        # enquiry is an itemAttachment hanging off this mail and the
        # body is the covering note, so the lead used to be built from
        # "FYI" with no cargo, no route and no customer. Rendered into
        # the text shape an ordinary forward has and spliced onto the
        # body, so the parser, the classifier and the AI extraction all
        # see a forward they already know how to read. Before the
        # parse, because everything after this reads the body.
        _attached = {'spliced': False, 'found': 0, 'skipped': 'not run'}
        try:
            from . import attached_message as _att
            _attached = _att.unwrap(msg, graph, mailbox)
            if _attached.get('spliced'):
                log.info('%s carried an attached message from %s — '
                         'spliced into the body', imid,
                         (_attached.get('messages') or [{}])[0].get('from'))
        except Exception:
            log.exception('could not read the attached message on %s', imid)

        # CRM-FINAL-13NOV item 13. A circular is not an enquiry, and it
        # was becoming a lead on somebody's board. Judged here, before
        # anything is extracted or created: the headers say the mail
        # was sent by a mailing system, and nothing in it reads as an
        # enquiry. Both halves have to hold -- a shipping line's
        # circular and its quotation come from the same address
        # through the same marketing system, and only one is business.
        #
        # It goes to Mail ingest with the reason, where an
        # administrator can turn it into a lead with one button. No
        # sender is blocked: the address that sends a circular this
        # week sends a real quotation next week.
        try:
            from app.services import bulk_mail as _bulk
            _promo, _promo_why = _bulk.classify(msg, _li.body_text(msg))
            if _promo:
                _record_ingest('skipped', msg, None, reason=_promo_why)
                db.session.commit()
                log.info('%s is promotional: %s', imid, _promo_why)
                return {'status': 'skipped', 'reason': _promo_why,
                        'lead_id': None,
                        'classification': 'promotional',
                        'internet_message_id': imid}
        except Exception:
            db.session.rollback()
            log.exception('could not judge whether %s is promotional; '
                          'the lead still lands', imid)

        try:
            extracted = email_parser.extract_lead(msg)
        except Exception as e:
            log.exception('parser exception for %s: %s', imid, e)
            extracted = None

        if not extracted:
            # Even if the parser completely bailed, build a minimal payload
            # from the raw Graph message so we still get a lead row.
            from_addr = ((msg.get('from') or {}).get('emailAddress') or {})
            extracted = {
                'company': '',
                'contact_name': from_addr.get('name') or '',
                'email': from_addr.get('address') or '',
                'phone': None,
                'subject': msg.get('subject') or '',
                'body_text': msg.get('bodyPreview') or '',
                'signals': {},
                'confidence': 0.0,
                'skip_reason': 'parser bailed',
            }

        # A parser skip reason is a LABEL, not a rejection — it rides along
        # on the Lead so the sales team can sort, and nothing is dropped.
        triage_tag = extracted.get('skip_reason')       # None if clean

        forwarded_by = extracted.get('forwarded_by') or msg.get('_forwarded_by')
        sender_email  = (extracted.get('email') or '').strip().lower()

        # ── Sender denylist ───────────────────────────────────────────
        # v2026-09-02 — promotions, advertisements and newsletters never
        # become leads. Checked against the ORIGINAL sender (the forward is
        # already unwrapped by this point), so a newsletter relayed in by
        # the auto-forward is blocked on its true origin, not on whoever
        # forwarded it. Editable in data/blocked_senders.txt.
        # `force` overrides the denylist, exactly as it overrides the
        # system-sender block above and for the same reason: a person
        # replaying a message from the Mail ingest screen has already
        # decided. Without this the rows §4 adds to that screen could
        # be read and never acted on — the "re-process one" button
        # would come straight back here and skip again.
        blocked = blocklist.check(sender_email) if not force else None
        # CRM-FINAL-13NOV follow-up item 1. A procurement portal wins
        # over the denylist. `procol.in` and `ansmtp.ariba.com` were
        # added to it on 30 October as notification noise, and in the
        # next fortnight 72 messages were dropped at the door --
        # including the RFQs and tender invitations among them, which
        # is what those portals exist to deliver. `portal_senders.txt`
        # is the more specific statement, so it is read first.
        _portal = None
        try:
            _portal = _prf.describe(sender_email,
                                    msg.get('subject') or '',
                                    _li.body_text(msg) or '')
        except Exception:
            log.exception('could not judge whether %s is a portal', imid)
        if _portal and blocked:
            log.info('%s is from the %s portal, not blocked: %s', imid,
                     _portal['portal'], blocked)
            blocked = None
        if blocked:
            log.info('Blocked %s [%s]', imid, blocked)
            # §4, and the one that matters most. This is the sender
            # denylist: data/blocked_senders.txt by domain, plus
            # _BULK_LOCAL_RE on any domain, which matches mailer@,
            # bulk@, broadcast@, digest@, campaign@, offers@ and
            # deals@. A real enquiry relayed from an address shaped
            # like that was dropped here with no trace anywhere —
            # not a lead, not a timeline entry, not a log row. The
            # outcome is 'blocked' rather than 'skipped' so the
            # denylist's work is readable on its own, and the row
            # carries the pattern that matched so the rule can be
            # corrected rather than guessed at.
            _record_ingest('blocked', msg, reason=blocked)
            return {'status': 'skipped', 'reason': blocked,
                    'lead_id': None, 'internet_message_id': imid}
        sender_domain = sender_email.split('@', 1)[1] if '@' in sender_email else ''

        # ── The client block register ─────────────────────────────────
        # A blocked client's enquiry must not become a lead at all: the
        # whole point is that nobody starts work on it, and a lead with
        # a badge on it is still something somebody picks up. It is
        # recorded as "Intake — Blocked" instead, so the enquiry is not
        # silently lost and an administrator can see it arrived.
        #
        # A caution does create the lead: business is allowed, and the
        # badge plus the acknowledgement on first touch is how the
        # person learns about the dispute.
        #
        # CRM-LEAD-FLOW-01 §2.1: the account is resolved first and
        # passed in. Without it the check saw only the name the
        # parser read and the sender's domain, so a blocked client
        # writing from a second domain, or under a spelling the
        # account carries as an alias, was not matched — which is
        # why Walchandnagar and Godrej kept making leads on 22, 26,
        # 28 and 29 October while both were on the register.
        try:
            from app.services import client_restrictions as _restrictions
            _typed = (extracted.get('company')
                      or msg.get('_company') or '')
            _account_id, _account_name = _resolve_account_for_block(
                sender_email, _typed, log)
            _verdict = _restrictions.check(
                company_name=_account_name or _typed,
                email=sender_email, domain=sender_domain,
                account_id=_account_id)
            if (not _verdict.blocked and _account_name
                    and _typed and _account_name != _typed):
                # Both readings of who this is, because either can
                # be the one on the register.
                _second = _restrictions.check(
                    company_name=_typed, email=sender_email,
                    domain=sender_domain)
                if _second.blocked:
                    _verdict = _second
        except Exception:
            log.exception('restriction check failed for %s', imid)
            _verdict = None

        if _verdict is not None and _verdict.blocked:
            log.info('Blocked client %s — %s not turned into a lead',
                     _verdict.company_name, imid)
            try:
                _restrictions.record_attempt(
                    _verdict, what='inbound email', user_id='system',
                    detail={'from': sender_email, 'subject':
                            (msg.get('subject') or '')[:160],
                            'internet_message_id': imid})
                _log_blocked_intake(db, msg, _verdict, sender_email, log)
                db.session.commit()
            except Exception:
                db.session.rollback()
                log.exception('could not record the blocked intake %s', imid)
            # §4 — _log_blocked_intake writes an EmailEvent, which the
            # Intake screens show. It does not write a mail_ingest_log
            # row, so this enquiry was absent from /admin/mail-ingest —
            # the screen somebody actually opens to ask "where did our
            # mail go?". Both, now: the EmailEvent for the Intake
            # boards and this row for that question.
            _record_ingest('blocked', msg,
                           reason=f'{_verdict.company_name} is on the '
                                  f'client block register — '
                                  f'{_verdict.reason_summary}')
            return {'status': 'skipped',
                    'reason': f'client blocked: {_verdict.company_name}',
                    'classification': 'blocked_client',
                    'lead_id': None, 'internet_message_id': imid}

        # ── Intake classification ─────────────────────────────────────
        # This path was deliberately zero-skip: a parser reason was a
        # label, DB dedup was removed, and a second email from the same
        # customer made a second lead. That is what filled the CRM with
        # replies, forwards, our own quotations and rate requests to
        # shipping lines.
        #
        # The classifier answers a different question from the parser:
        # not "is this logistics?" but "is this a NEW enquiry?". Only one
        # of ten classes creates a lead — and nothing is dropped, because
        # everything else is filed against the lead it belongs to.
        # ── Kill switch ───────────────────────────────────────────
        # LEAD_INTAKE_MODE:
        #   enforce  (default) classification gates lead creation
        #   observe  classify and record, but create the lead anyway
        #   off      do not classify at all
        #
        # An untuned classifier suppressing a real RFQ is far more
        # expensive than a duplicate lead, so there has to be a way to
        # stand it down in seconds without a code rollback.
        _mode = (os.environ.get('LEAD_INTAKE_MODE') or 'enforce').lower()

        decision = None
        try:
            if _mode == 'off':
                raise _IntakeDisabled
            from app.services import lead_intake_db as _lidb
            msg_for_class = dict(msg)
            msg_for_class['_forward_resolved'] = bool(
                extracted.get('forward_resolved'))
            # The customer's own address, not the colleague who relayed
            # it. Everything downstream reasons about the sender.
            msg_for_class['_resolved_sender'] = (
                extracted.get('email') or '')
            # §20 — Graph gives us names here, not content; the files
            # themselves are fetched after the lead exists. What is
            # available pre-lead is the names, so anything richer waits
            # for the attachment pass below and is applied then.
            msg_for_class['_attachment_text'] = (
                msg.get('_attachment_text') or '')
            decision = _li.classify(msg_for_class, _lidb.build_context())
            log.info('intake %s → %s (step %s, conf %s) %s',
                     imid, decision.klass, decision.step,
                     decision.confidence, decision.reason)
        except _IntakeDisabled:
            pass
        except Exception:
            log.exception('intake classification failed for %s — '
                          'creating the lead rather than losing it', imid)

        # An administrator pressing "create a lead from this email" on
        # the ingestion log has overruled the classifier deliberately.
        # `force` alone only skips the duplicate check — the
        # classification would decline it a second time, and the
        # button would appear to do nothing.
        if override_classifier and decision is not None \
                and not decision.creates_lead:
            log.info('intake OVERRIDDEN for %s — was %s (%s)',
                     imid, decision.klass, decision.reason)
            try:
                _lidb.record(decision, msg)
                db.session.commit()
            except Exception:
                db.session.rollback()
            decision = None

        if _mode == 'observe' and decision is not None \
                and not decision.creates_lead:
            # Record what it would have done, then create the lead
            # anyway. This is how the classifier earns trust: it is
            # measured against real mail before it is allowed to
            # suppress any of it.
            try:
                _lidb.record(decision, msg)
                db.session.commit()
            except Exception:
                db.session.rollback()
            log.info('intake OBSERVE %s → would have been %s; creating the '
                     'lead anyway', imid, decision.klass)
            decision = None

        # ── An enquiry against a lead somebody closed ────────────────
        # A new RFQ on a dead lead is the customer disagreeing with the
        # decision to close it. Attaching it silently is how an enquiry
        # arrives and nobody answers.
        if decision is not None and decision.lead_id:
            try:
                _reopened = _reopen_if_closed(db, decision, msg, log)
                if _reopened:
                    _file_against_lead(db, decision, msg, extracted, log,
                                       graph=graph, mailbox=mailbox)
                    db.session.commit()
                    _record_ingest('reopened', msg, decision,
                                   lead_id=decision.lead_id,
                                   reason=_reopened)
                    return {'status': 'reopened', 'reason': _reopened,
                            'lead_id': decision.lead_id,
                            'classification': decision.klass,
                            'internet_message_id': imid}
            except Exception:
                db.session.rollback()
                log.exception('could not reopen lead %s for %s',
                              decision.lead_id, imid)

        # ── A colleague's forward with no external sender ────────────
        # Every visible address is ours, so the classifier calls it
        # internal and nothing is created — losing exactly the
        # enquiries somebody cared enough to pass on. Look inside the
        # quoted original before giving up on it.
        _rescue = None
        if decision is not None and not decision.creates_lead \
                and decision.klass == _li.Klass.INTERNAL \
                and not decision.lead_id:
            # CRM-FINAL-13NOV item 10, BEFORE the rescue. The rescue
            # never returns "drop it" -- deliberately, because it
            # exists for a colleague's forward of a customer's
            # enquiry. Our own tender digest is not that: it has no
            # external customer because there is no customer in it,
            # and it became a lead called "To review" carrying
            # "could not identify external customer", which is a true
            # sentence about a mail that never had one.
            #
            # So a mail that is from us AND reads as a notice AND
            # carries no customer in its body goes to the lead it is
            # about, or to Mail ingest. Any one of those three
            # failing leaves the rescue to run exactly as before.
            try:
                from app.services import internal_mail as _im
                _what, _lead_id, _why = _im.decide(
                    msg, _li.body_text(msg),
                    internal_domains=email_parser._skip_domains())
            except Exception:
                log.exception('could not judge the internal mail %s', imid)
                _what, _lead_id, _why = _im.NOTHING, None, ''

            if _what == _im.ATTACH and _lead_id:
                try:
                    _notice = _li.Decision(
                        _li.Klass.EXISTING, step=10, lead_id=_lead_id,
                        reason=_why)
                    _file_against_lead(db, _notice, msg, extracted, log,
                                       graph=graph, mailbox=mailbox)
                    db.session.commit()
                    _record_ingest('attached', msg, _notice,
                                   lead_id=_lead_id, reason=_why)
                    log.info('%s is our own notice about lead %s', imid,
                             _lead_id)
                    return {'status': 'skipped', 'reason': _why,
                            'lead_id': _lead_id,
                            'classification': 'internal_notice',
                            'internet_message_id': imid}
                except Exception:
                    db.session.rollback()
                    log.exception('could not file the notice %s against '
                                  'lead %s', imid, _lead_id)
            elif _what == _im.LOG:
                try:
                    _record_ingest('skipped', msg, None, reason=_why)
                    db.session.commit()
                    log.info('%s is our own notice and names no lead: '
                             'logged, not created', imid)
                    return {'status': 'skipped', 'reason': _why,
                            'lead_id': None,
                            'classification': 'internal_notice',
                            'internet_message_id': imid}
                except Exception:
                    db.session.rollback()
                    log.exception('could not log the notice %s', imid)

            try:
                from app.services import mail_ingest as _mi
                _rescue = _mi.rescue_internal_forward(
                    msg, _li.body_text(msg),
                    internal_domains=email_parser._skip_domains())
                log.info('internal forward %s → %s (%s)', imid,
                         _rescue['action'], _rescue['how'])
                decision = None          # fall through and make the lead
            except Exception:
                log.exception('could not examine the internal forward %s',
                              imid)
                _rescue = None

        # ── A PORTAL MESSAGE IS NOT DROPPED BY THE CLASSIFIER ───────
        #
        # 14 Nov. The second half of the same fault as `_li` above: the
        # denylist was taught that a procurement portal wins, and the
        # classifier was not. A portal RFQ reads as `I_non_business`
        # -- "no cargo, RFQ, route or contact signal" -- because the
        # mail is a template with a number in it and the cargo is
        # behind a login, so every one of these was still being
        # skipped after the denylist let it through.
        #
        # The whole decision of item 1 is that an enquiry on a review
        # screen is recoverable and one that was never recorded is
        # not. So the classifier's refusal is set aside for a portal
        # message with no lead of its own, exactly as the internal
        # forward's rescue does it, and the message falls through to
        # the block below: it joins its event's lead if there is one,
        # it is refused as a withdrawal of an event nobody has, or it
        # becomes one lead, flagged for review.
        if (_portal and decision is not None
                and not decision.creates_lead and not decision.lead_id):
            log.info('%s is from the %s portal; the classifier said %s '
                     'and a portal RFQ is not dropped on that', imid,
                     _portal['portal'], decision.klass)
            try:
                _lidb.record(decision, msg)
                db.session.commit()
            except Exception:
                db.session.rollback()
            decision = None          # fall through and make the lead

        if decision is not None and not decision.creates_lead:
            try:
                _lidb.record(decision, msg)
                _file_against_lead(db, decision, msg, extracted, log,
                                   graph=graph, mailbox=mailbox)
                db.session.commit()
                _record_ingest(
                    'attached' if decision.lead_id else 'skipped',
                    msg, decision, lead_id=decision.lead_id,
                    reason=f'{decision.klass}: {decision.reason}')
            except Exception:
                db.session.rollback()
                log.exception('could not file %s against lead %s',
                              imid, decision.lead_id)
            return {'status': 'skipped',
                    'reason': f'{decision.klass}: {decision.reason}',
                    'lead_id': decision.lead_id,
                    'classification': decision.klass,
                    'internet_message_id': imid}

        # ── CRM-COCKPIT-02 §5.6 · one lead per portal number ─────────
        # A reverse auction sends the alert, a reminder and often a
        # corrigendum — two or three mails, each on its own thread
        # with its own message id, so `thread_key` and the
        # conversation id cannot hold them together. Eight duplicates
        # were merged by hand on 01 November for this reason alone.
        #
        # The number can hold them: RA-57103, eRFQ-41688, a GeM bid
        # number and a tender number all appear in every mail of the
        # set. So before a new lead is made, look for an open lead
        # already carrying this one — and if there is one, file the
        # message against it and let it update the deadline.
        #
        # ONE LEAD PER PORTAL EVENT, AND THE NOTICES ON IT
        #
        # Of 74 portal messages the denylist had held back, ~30 were
        # "withdrawn" notices and most of the rest reminders. The key
        # below is the §5.6 key widened to the numbers the two
        # reported portals use (RFX-, RFQ-, an Ariba document or event
        # id) -- only for a listed portal sender, so nothing outside
        # them changes -- and the notice that arrives on an event
        # already in the CRM now does what it says: a withdrawal
        # closes the lead, an "about to end" sets the deadline.
        _pkey = None
        _notice = None
        try:
            _subject_text = msg.get('subject') or ''
            _body_text = _li.body_text(msg) or ''
            _pkey = _prf.event_key(sender_email, _subject_text,
                                   _body_text)
            _notice = _prf.notice_kind(_subject_text, _body_text)
        except Exception:
            log.exception('could not read a portal number on %s', imid)
        if _pkey:
            _held = None
            try:
                from app.services import sales_rules as _rules
                _held = (Lead.query
                         .filter(Lead.portal_key == _pkey,
                                 Lead.is_archived.isnot(True),
                                 Lead.stage.in_(_rules.open_stages()))
                         .order_by(Lead.id.asc()).first())
                if _held is None and _portal:
                    # A withdrawal or a reminder about an event whose
                    # lead somebody has already closed belongs on that
                    # lead's trail, not on a second lead. Attached
                    # only: nothing here re-opens anything.
                    _held = (Lead.query
                             .filter(Lead.portal_key == _pkey,
                                     Lead.is_archived.isnot(True))
                             .order_by(Lead.id.desc()).first())
            except Exception:
                log.exception('could not look up portal number %s', _pkey)
                _held = None
            if _held is not None:
                try:
                    from app.services import portal_event as _pev
                    _joined = _li.Decision(
                        _li.Klass.REPLY, step=5.6, lead_id=_held.id,
                        reason=f'the same portal number ({_pkey}) as '
                               f'lead #{_held.id}')
                    _file_against_lead(db, _joined, msg, extracted, log,
                                       graph=graph, mailbox=mailbox)
                    # What the notice itself says: a withdrawal closes
                    # the lead, an "about to end" carries the
                    # deadline. Proposed, never applied (§2.5).
                    _did = _pev.apply_notice(
                        _held, _notice,
                        subject=msg.get('subject') or '',
                        body=_li.body_text(msg) or '',
                        email_message_id=imid,
                        received_at=email_parser.received_datetime(msg))
                    db.session.commit()
                    _why = (f'portal number {_pkey}'
                            + (f' ({_notice})'
                               if _prf.is_notice(_notice) else '')
                            + (f' — {_did}' if _did else ''))
                    _record_ingest('attached', msg, _joined,
                                   lead_id=_held.id, reason=_why)
                    log.info('%s joined lead %s on portal number %s: %s',
                             imid, _held.id, _pkey, _did or 'filed')
                    return {'status': 'skipped',
                            'reason': f'portal number {_pkey}: joined '
                                      f'lead {_held.id}'
                                      + (f' — {_did}' if _did else ''),
                            'lead_id': _held.id,
                            'classification': 'portal_same_number',
                            'internet_message_id': imid}
                except Exception:
                    db.session.rollback()
                    log.exception('could not join %s to lead %s by '
                                  'portal number', imid, _held.id)
            elif _portal and _notice == _prf.WITHDRAWN:
                # The buyer withdrew an event we never saw -- its
                # invitation was dropped by the denylist weeks ago, or
                # never arrived. A lead created now would be born Lost
                # and nobody can act on it, so the event is written
                # down where the other refusals are and no lead is
                # made. It is in the ingest log with its number: a
                # person who wants it can still press re-process.
                _record_ingest(
                    'skipped', msg, None,
                    reason=f'portal event {_pkey} was withdrawn by the '
                           f'buyer and no lead exists for it — nothing '
                           f'to close, so no lead was created')
                db.session.commit()
                log.info('%s is a withdrawal of %s, which has no lead',
                         imid, _pkey)
                return {'status': 'skipped',
                        'reason': f'portal event {_pkey} withdrawn; no '
                                  f'lead existed',
                        'lead_id': None,
                        'classification': 'portal_withdrawn_unknown',
                        'internet_message_id': imid}

        # ── Build Lead payload — shared enricher, same summary card poll uses.
        try:
            lead_kwargs = enrich_mod.build_enriched_lead_kwargs(
                msg, extracted,
                sender_email=sender_email,
                sender_domain=sender_domain,
                forwarded_by=forwarded_by,
            )
        except Exception as e:
            log.exception('enricher failed for %s', imid)
            return {'status': 'failed', 'reason': f'enricher: {e}',
                    'lead_id': None, 'internet_message_id': imid}

        # If the parser flagged a triage reason, surface it in opp_notes so
        # the sales team can filter (e.g. hide 'auto-reply' rows).
        if triage_tag:
            try:
                import json as _json
                cur = _json.loads(lead_kwargs.get('opp_notes') or '{}')
                cur['triage_tag'] = triage_tag
                lead_kwargs['opp_notes'] = _json.dumps(cur, default=str)
            except Exception:
                pass

        try:
            # v2026-09-02 — date the Lead by when the email ARRIVED in the
            # leads inbox, not when we happened to process it. The portal
            # sorts on created_at, so a backfill or a replay would otherwise
            # bunch old mail at the top under today's timestamp.
            received = email_parser.received_datetime(msg) or datetime.utcnow()
            # §17 — recommend a vertical where the Account Master has
            # not settled it. A recommendation, recorded with its
            # confidence so a person can overrule it.
            _vertical = lead_kwargs.get('procam_vertical')
            try:
                from app.services import lead_vertical
                from app.services import lead_intake_db as _lvdb
                _acct, _ = _lvdb.resolve_account(
                    extracted.get('email') or sender_email,
                    text_for_gstin=f"{extracted.get('subject') or ''}\n"
                                   f"{extracted.get('body_text') or ''}")
                _guess, _conf, _why = lead_vertical.recommend(
                    f"{extracted.get('subject') or ''}\n"
                    f"{extracted.get('body_text') or ''}",
                    account_vertical=(_acct.vertical if _acct else None))
                if _guess and (not _vertical or _conf >= 80):
                    lead_kwargs['procam_vertical'] = _guess
                    # Kept, not just logged: a person looking at the lead
                    # can now see whether the vertical was a strong call or
                    # a guess, and why.
                    lead_kwargs['vertical_confidence'] = int(_conf or 0)
                    lead_kwargs['vertical_reason'] = (_why or '')[:200]
                    # item 11: the same answer as a word, so the owner
                    # rule below can refuse to route on a guess.
                    lead_kwargs['vertical_source'] = \
                        lead_vertical.source_for(_why, _conf)
                    log.info('vertical %s (%s%%) for %s — %s',
                             _guess, _conf, imid, _why)
            except Exception:
                log.exception('vertical recommendation failed for %s', imid)

            # item 1. The From is a robot, so the buyer is whatever
            # the body says, and the lead announces itself as unread:
            # flagged for review, and carrying the portal in
            # `lead_platform` -- which is the column's own purpose
            # ("which platform it came through — superprocure,
            # ariba") and what the owner rule below reads to route it
            # to the RFQ desk.
            if _portal:
                lead_kwargs['needs_review'] = True
                lead_kwargs['lead_platform'] = _portal['portal'][:40]
                _named = (lead_kwargs.get('company') or '').strip()
                if _portal['buyer']:
                    # THE BODY'S BUYER WINS. The parser guesses a
                    # company out of the subject of a robot's mail and
                    # produced "participate in an event" and "quote"
                    # as customer names; the buyer line is a statement
                    # by the portal about whose event this is. Where
                    # the body names one, it is the answer.
                    lead_kwargs['company'] = _portal['company']
                elif (not _named or _is_placeholder_company(_named)
                        or _prf.names_the_portal(_named,
                                                 _portal['portal'])):
                    # Nothing readable in the body either, and what
                    # the parser read is unusable or is the portal's
                    # own name: "To review", flagged -- an enquiry on
                    # a review screen is recoverable and one that was
                    # never recorded is not.
                    lead_kwargs['company'] = _portal['company']

            _keys = _thread_keys(msg)
            # The client's own date, when it can be read. Different
            # from `received` for a forward, and that difference is
            # the point: an enquiry from the 1st forwarded on the 14th
            # is work that arrived on the 14th, and losing the
            # customer's date is how it then looks new.
            _client_sent = None
            try:
                _client_sent = email_parser.client_sent_datetime(
                    msg, _li.body_text(msg))
            except Exception:
                log.exception('could not read the client date on %s', imid)

            # CRM-ACCOUNT-FIRST-01 — our own mail going out.
            #
            # Every other rule reads the sender, and on outbound mail
            # the sender is us, so the customer in the To: line was
            # invisible: 167 such messages became 167 orphan leads
            # under "Unknown" on 1-3 September. Read the recipient,
            # attach to that customer's open lead, and create one
            # only when they have none — with the account on it.
            _outbound_note = ''
            try:
                from app.services import account_gate as _ag
                from app.services import outbound_mail as _om
                if _ag.enabled():
                    _dir, _from, _ext = _om.direction(
                        msg, body=body)
                    if _dir == 'outbound' and _ext:
                        _acct, _rule, _addr = _om.customer_of(_ext)
                        if _acct is not None:
                            _target = _om.open_lead_for(
                                _acct, subject=subject)
                            if _target is not None:
                                log.info(
                                    'outbound mail %s belongs on lead '
                                    '%s (%s, by %s)', imid, _target.id,
                                    _acct.name, _rule)
                                return _attach_outbound(
                                    _target, msg, body, _acct, _addr,
                                    imid)
                            # No open lead for this customer: a new
                            # one, but never an orphan.
                            lead_kwargs['company_id'] = _acct.id
                            lead_kwargs.setdefault('company',
                                                   _acct.name)
                            _outbound_note = (
                                f'our own mail to {_addr}; no open '
                                f'lead for {_acct.name}, so this is '
                                f'a new one')
                        else:
                            # The recipient could not be placed. The
                            # lead still lands, and Account check
                            # gets the address to work from.
                            lead_kwargs.setdefault('email', _addr)
                            lead_kwargs['needs_review'] = True
                            _outbound_note = (
                                f'our own mail to {_addr}, who is on '
                                f'no account yet')
            except Exception:                             # noqa: BLE001
                log.exception('outbound handling failed for %s — the '
                              'lead still lands', imid)

            # A colleague's forward that named a customer we could not
            # otherwise see.
            if _rescue:
                if _rescue['action'] == 'review':
                    lead_kwargs['company'] = _rescue['company']
                    lead_kwargs['needs_review'] = True
                else:
                    lead_kwargs.setdefault('company', _rescue['company'])
                    if not lead_kwargs.get('company'):
                        lead_kwargs['company'] = _rescue['company']
                    if _rescue.get('email'):
                        lead_kwargs.setdefault('email', _rescue['email'])
                    if _rescue.get('account_id'):
                        lead_kwargs['company_id'] = _rescue['account_id']

            # A lead created from a forwarded offer starts at Quoted:
            # the price is already with the customer, and a lead at
            # New Opportunity would be chased as if it were not.
            _stage = (decision.create_stage if decision is not None
                      else _li.DEFAULT_NEW_STAGE)
            if decision is not None:
                _vert = (decision.extra or {}).get('vertical')
                if _vert and not lead_kwargs.get('procam_vertical'):
                    lead_kwargs['procam_vertical'] = _vert

            lead = Lead(
                source            = 'email',
                stage             = _stage,
                email_message_id  = imid,
                created_at        = received,
                received_at       = received,
                client_sent_at    = _client_sent,
                onboarded_date    = received.date(),
                conversation_id   = _keys.get('conversation_id'),
                in_reply_to       = _keys.get('in_reply_to'),
                references_header = _keys.get('references'),
                # CRM-USER-ISSUES-02NOV §2 — see the note in
                # pipeline.py. Both intake paths fill it or neither
                # does, and the webhook is the live one. Keyed on the
                # resolved sender for the same reason: `_keys` comes
                # from the untouched message, and a forward's From is
                # the colleague who relayed it.
                thread_key        = _thread_key_for(
                    msg, extracted.get('email')),
                classification    = (decision.klass if decision else None),
                lead_confidence   = (decision.confidence if decision else None),
                duplicate_score   = (decision.duplicate_score if decision else None),
                **lead_kwargs,
            )
            # Who passed it on, and what kind of enquiry it is.
            # Both read off the same message at the same moment,
            # and both are facts the CRM could not answer before:
            # 226 forwarded load postings, no record of the
            # forwarder, and nothing marking them as time-critical
            # when a bulk review deleted them.
            try:
                from app.services import lead_intake as _lik
                from app.services import lead_kinds

                fwd_email, fwd_name, fwd_subject = \
                    lead_kinds.forwarder(msg, _lik.body_text(msg))
                if fwd_email:
                    lead.forwarded_by_email = fwd_email
                    lead.forwarded_by_name = fwd_name
                    lead.forwarded_subject = fwd_subject

                kind, platform, _why = lead_kinds.classify(
                    subject=(msg.get('subject') or ''),
                    sender=(lead.original_email_from or lead.email
                            or ''))
                if kind:
                    lead.lead_type = kind
                    lead.lead_platform = platform or None
                    owner = lead_kinds.owner()
                    if owner and not (lead.assigned_to or '').strip():
                        lead.assigned_to = owner
            except Exception:
                # Never the reason an email is lost.
                log.exception('could not record forwarder or kind')

            db.session.add(lead)
            db.session.flush()

            # "Dear Suranjan, please take this up" — a forward usually
            # says who it is for, in the first line, in English. That
            # beats the routing rules, because it is a person telling
            # us rather than us inferring.
            _named = None
            try:
                from app.services import mail_ingest as _mi
                _named, _which = _mi.assignee_from_forward(
                    _li.body_text(msg))
                if _named is not None:
                    from app.services import lead_assignment
                    lead_assignment.assign(
                        lead, primary_code=_named.emp_code, actor='system',
                        note=f'Assigned by forward from {sender_email}',
                        _defer_commit=True)
                    log.info('lead %s assigned to %s — named in the forward',
                             lead.id, _named.emp_code)
            except Exception:
                log.exception('could not act on the name in the forward %s',
                              imid)

            if _named is None:
                auto_assign_owners(
                    lead,
                    extracted.get('email') or sender_email,
                    subject=extracted.get('subject') or '',
                    body=extracted.get('body_text') or '')

            # A forward whose quoted message is itself a quotation means
            # the enquiry has already been priced; a lead sitting at
            # "New" while the customer holds our offer is one nobody
            # chases.
            try:
                if _rescue and _mi.looks_like_a_forwarded_quotation(
                        msg, _li.body_text(msg)):
                    lead.stage = 'Quoted'
                    if _client_sent:
                        lead.quote_date = _client_sent.date()
            except Exception:
                pass

            # Same for a lead the classifier placed at Quoted: date
            # the quote from the message that carried it, so ageing
            # and the quote reports can see it at all.
            if _stage == 'Quoted' and not lead.quote_date:
                lead.quote_date = (_client_sent or received).date()

            # ── CRM-COCKPIT-02 §5.1, §5.6, §2 ────────────────────────
            # Everything the message already told us, written onto the
            # lead's own fields instead of left inside the extracted
            # JSON: origin, destination, cargo, weight, dimensions,
            # urgency — plus the portal number this enquiry belongs to
            # and the deadline its subject line states. All marked
            # "auto — confirm"; none of it overwrites anything.
            try:
                from app.services import autofill as _af
                from app.services import quote_due_flow as _qdf

                _af.apply(lead)
                if not lead.portal_key:
                    # The SAME key the join above looked for. It was
                    # `quote_due.portal_key` on its own here, so a
                    # lead made from an RFX- or Ariba-numbered mail
                    # was stored with no key at all and the next
                    # notice about that event could only become a
                    # second lead.
                    lead.portal_key = _prf.event_key(
                        sender_email,
                        lead.original_email_subject or '',
                        lead.original_email_body or '') or None
                _qdf.read_from_message(
                    lead,
                    subject=lead.original_email_subject or '',
                    body=lead.original_email_body or '',
                    email_message_id=imid,
                    received_at=received)
            except Exception:
                log.exception('could not autofill from the mail %s', imid)

            if _rescue and _rescue['action'] == 'review':
                try:
                    from app.services import mail_ingest as _mi2
                    _mi2.notify_admin_review(
                        lead, 'An internal forward arrived that named no '
                              'client the CRM could identify.')
                except Exception:
                    pass

            # Row 1 of the email trail.
            try:
                from email_ingest.trail import record_inbound
                record_inbound(db, lead, received_at=received,
                               message_id=imid)
            except Exception:
                log.exception('email trail seed failed for lead %s', lead.id)

            # ── Attachments and the original message ────────────────────
            # This used to call save_attachments_for_lead and throw the
            # result away, so the files landed on disk with no row — and
            # with no row there is no download route, so nobody could
            # reach them. The poll path recorded them; this, the path
            # production actually runs, did not. capture_for_lead writes
            # the rows and, behind FEATURE_RFQ_CAPTURE, keeps the .eml
            # so the original can be forwarded as the client sent it.
            try:
                from app.services import rfq_capture
                _cap = rfq_capture.capture_for_lead(
                    lead, graph=graph, mailbox=mailbox, msg=msg)
                log.info('lead %s captured %s attachment(s), original: %s',
                         lead.id, _cap['attachments'], _cap['raw'])
            except Exception:
                log.exception('attachments save failed for lead %s', lead.id)

            # §20 — now the files are on disk, read them. The lead
            # already exists, so this cannot change whether it was
            # created; what it can do is fill in a vertical and a
            # confidence that the body alone could not support, which is
            # exactly the "please find attached" case.
            try:
                from app.services import attachment_text
                inside = attachment_text.for_lead(lead.id)
                if inside:
                    from app.services import lead_vertical
                    guess, conf, why = lead_vertical.recommend(
                        f"{extracted.get('subject') or ''}\n{inside}",
                        account_vertical=lead.procam_vertical)
                    if guess and conf >= 80 and guess != lead.procam_vertical:
                        log.info('vertical revised to %s (%s%%) for lead %s '
                                 'from its attachments — %s',
                                 guess, conf, lead.id, why)
                        lead.procam_vertical = guess
                        lead.vertical_confidence = int(conf or 0)
                        lead.vertical_reason = (
                            f'from attachments — {why}' if why
                            else 'from attachments')[:200]
                        lead.vertical_source = lead_vertical.source_for(
                            why, conf, from_attachment=True)
                    if not (lead.original_email_body or '').strip():
                        # The body really was just "please find attached".
                        lead.original_email_body = inside[:8000]
            except Exception:
                log.exception('attachment text pass failed for lead %s',
                              lead.id)

            # The created leads are the labels most worth having: they
            # are the ones a human might reject.
            if decision is not None:
                try:
                    from app.services import lead_intake_db as _lidb2
                    _lidb2.record(decision, msg, created_lead_id=lead.id)
                except Exception:
                    log.exception('could not record the classification for '
                                  'lead %s', lead.id)

            db.session.commit()
            # §4 — say when the enquiry came out of an attached
            # message. "The lead looks empty" and "the lead was built
            # from the covering note" are the same symptom, and this
            # is what tells them apart on the Mail ingest screen.
            _why = (('rescued from an internal forward — ' + _rescue['how'])
                    if _rescue else 'new enquiry')
            if _attached.get('spliced'):
                _why += (' · read from the message attached to it'
                         + (f' ({_attached["found"]} attached)'
                            if _attached.get('found', 0) > 1 else ''))
            _record_ingest('created', msg, decision, lead_id=lead.id,
                           reason=_why)
            return {'status': 'created', 'lead_id': lead.id,
                    'internet_message_id': imid, 'reason': None}
        except Exception as e:
            db.session.rollback()
            log.exception('lead insert failed for %s', imid)
            # CRM-FINAL-13NOV follow-up item 3. One notification died
            # here with "UNIQUE constraint failed: leads." and the
            # enquiry was lost: the insert rolled back, no lead
            # existed, and the only trace was an EmailEvent row.
            #
            # `leads` has two unique columns and they mean opposite
            # things:
            #
            #   email_message_id  the webhook and the poll saw the
            #                     same mail at once. The other one
            #                     won, a lead EXISTS, and there is
            #                     nothing to recover.
            #   crn               two leads allocated the same
            #                     customer reference in the same
            #                     moment. Nothing exists, and the
            #                     enquiry is gone until somebody
            #                     re-processes the message.
            #
            # The second is worth one retry: the CRN is taken from a
            # sequence, so asking again gives a different number and
            # the lead lands. The first is not retried -- the lead is
            # already there, and a retry would be a second attempt at
            # the thing that just told us so.
            _landed = _retry_once_after_a_clash(
                db, e, msg, imid, locals().get('lead'), decision, log)
            if _landed is not None:
                _record_ingest(
                    'created', msg, decision, lead_id=_landed.id,
                    reason='new enquiry; the first insert clashed on a '
                           'reference and the retry took a fresh one')
                return {'status': 'created', 'lead_id': _landed.id,
                        'internet_message_id': imid,
                        'reason': 'retried after a reference clash'}
            _record_ingest('error', msg, decision,
                           reason=f'lead insert: {e}'[:500])
            return {'status': 'failed', 'reason': f'lead insert: {e}',
                    'lead_id': None, 'internet_message_id': imid}


def _resolve_account_for_block(sender_email, typed_name, log):
    """(account_id, account name) for the block check, or (None, '').

    Uses the matcher the rest of the CRM uses, so an account found
    by a learned sender, a domain or an alias is the one the block
    register is asked about. Best-effort: a matcher that cannot run
    must not stop the mail being read.
    """
    try:
        from app.services import account_match
        found = account_match.candidates(
            sender_email, company_name=typed_name or None) or []
        for item in found:
            account = item.get('account')
            if account is None:
                continue
            if item.get('kind') == 'resemblance':
                continue
            return account.id, (account.name or '')
    except Exception:                                     # noqa: BLE001
        log.debug('could not resolve the account for the block check',
                  exc_info=True)
    return None, ''
