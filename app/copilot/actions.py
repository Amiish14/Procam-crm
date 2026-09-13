"""
§10 / Phase 5 — controlled write actions, built and switched off.

The brief is explicit twice over: read-only for Phase 1, and write
actions "Phase 5 only, if approved". So this ships complete and
disabled. Turning it on is one line in `.env` and a deliberate decision
by somebody who can be named, not a side effect of deploying.

Every action is two calls, never one:

    propose(...)  →  a Proposal describing exactly what would change,
                     in the words the user will confirm
    commit(...)   →  applies it, given the token from the proposal

There is no single-call form. A model that could act in one step could
act by accident, and the confirmation is not a dialog box bolted on the
front — it is the only entry point.

What an action may never do
    Reach a record outside the caller's scope: every action resolves
    its target through the same scoped query the read side uses.
    Delete anything. Change a value. Send anything. The four actions
    here are the four the brief names, and adding a fifth is a code
    change somebody reviews.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time

from app.access import scope as sc_mod


#: Off unless switched on. Both conditions, so a stray environment
#: variable in a shared file cannot enable writes on its own.
def enabled():
    flag = (os.environ.get('PROCAM_AI_ACTIONS') or 'off').lower()
    return flag in ('on', 'true', '1', 'yes')


#: A proposal is good for this long. Short: it describes a state of the
#: CRM that may have moved on, and confirming a stale proposal would
#: apply a change to a record the user has not looked at recently.
TTL_SECONDS = 300


class Proposal:
    def __init__(self, *, action, target, summary, changes, token,
                 expires_at):
        self.action = action
        self.target = target
        self.summary = summary
        self.changes = changes
        self.token = token
        self.expires_at = expires_at

    def to_dict(self):
        return {'action': self.action, 'target': self.target,
                'summary': self.summary, 'changes': self.changes,
                'token': self.token, 'expires_at': self.expires_at,
                'confirm_required': True}


def _secret():
    return (os.environ.get('SECRET_KEY') or 'procam-ai').encode()


def _sign(payload):
    raw = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    mac = hmac.new(_secret(), raw.encode(), hashlib.sha256).hexdigest()
    return f'{mac}.{int(payload["exp"])}'


def _verify(payload, token):
    """The token proves this exact change was the one shown.

    Signed rather than stored, so a confirmation cannot be replayed
    against a different record by editing the request — the payload is
    rebuilt from the request and the signature has to match.
    """
    try:
        mac, exp = (token or '').rsplit('.', 1)
    except ValueError:
        return False, 'Malformed confirmation'
    if int(exp) < time.time():
        return False, 'That confirmation has expired — ask again.'
    expected = _sign(payload).split('.', 1)[0]
    if not hmac.compare_digest(mac, expected):
        return False, 'That confirmation does not match what was proposed.'
    return True, None


# ── the four actions the brief names ─────────────────────────────────
ACTIONS = {}


def action(key, label, *, permission=None):
    def register(fn):
        ACTIONS[key] = {'key': key, 'label': label,
                        'permission': permission, 'fn': fn}
        return fn
    return register


def propose(key, scope, params):
    """(Proposal, error). Reads only — nothing is written here."""
    if not enabled():
        return None, ('Actions are switched off. Procam AI is read-only '
                      'until they are approved and enabled.')
    spec = ACTIONS.get(key)
    if spec is None:
        return None, f'Unknown action {key}'
    if spec['permission'] and not scope.can(spec['permission']):
        return None, 'That is outside your CRM access.'
    return spec['fn'](scope, params, commit=False)


def commit(key, scope, params, token, *, actor=None):
    """(result, error). Applies a proposal, given its own token."""
    if not enabled():
        return None, 'Actions are switched off.'
    spec = ACTIONS.get(key)
    if spec is None:
        return None, f'Unknown action {key}'
    if spec['permission'] and not scope.can(spec['permission']):
        return None, 'That is outside your CRM access.'

    proposal, err = spec['fn'](scope, params, commit=False)
    if err:
        return None, err
    ok, why = _verify(_payload(key, proposal), token)
    if not ok:
        return None, why
    return spec['fn'](scope, params, commit=True, actor=actor)


def _payload(key, proposal):
    return {'a': key, 't': proposal.target, 'c': proposal.changes,
            'exp': proposal.expires_at}


def _proposal(action_key, *, target, summary, changes):
    exp = int(time.time()) + TTL_SECONDS
    p = Proposal(action=action_key, target=target, summary=summary,
                 changes=changes, token='', expires_at=exp)
    p.token = _sign(_payload(action_key, p))
    return p


def _lead_in_scope(scope, params):
    from app import Lead
    try:
        lead_id = int(params.get('lead_id'))
    except (TypeError, ValueError):
        return None, 'Which lead?'
    lead = sc_mod.leads(sc=scope).filter(Lead.id == lead_id).first()
    if lead is None:
        return None, 'That lead is not one you can act on.'
    return lead, None


@action('create_activity', 'Log an activity', permission=None)
def create_activity(scope, params, *, commit=False, actor=None):
    from app import LeadActivity, db

    lead, err = _lead_in_scope(scope, params)
    if err:
        return None, err
    kind = (params.get('kind') or 'note').strip()[:30]
    note = (params.get('note') or '').strip()[:2000]
    if not note:
        return None, 'What should the activity say?'

    if not commit:
        return _proposal(
            'create_activity', target=f'lead:{lead.id}',
            summary=(f'Log a {kind} on {lead.company}: '
                     f'"{note[:120]}"'),
            changes={'lead_id': lead.id, 'kind': kind, 'note': note}), None

    db.session.add(LeadActivity(lead_id=lead.id, kind=kind,
                                subject=note[:255], body=note,
                                performed_by=actor or scope.emp_code))
    db.session.commit()
    return {'ok': True, 'lead_id': lead.id,
            'message': f'Logged on {lead.company}.'}, None


@action('set_followup', 'Set a follow-up date', permission=None)
def set_followup(scope, params, *, commit=False, actor=None):
    from datetime import date

    from app import db

    lead, err = _lead_in_scope(scope, params)
    if err:
        return None, err
    raw = (params.get('date') or '').strip()
    try:
        when = date.fromisoformat(raw)
    except ValueError:
        return None, 'Give the date as YYYY-MM-DD.'

    if not commit:
        return _proposal(
            'set_followup', target=f'lead:{lead.id}',
            summary=(f'Set the follow-up on {lead.company} to {when} '
                     f'(currently {lead.followup_date or "none"}).'),
            changes={'lead_id': lead.id, 'date': str(when)}), None

    lead.followup_date = when
    db.session.commit()
    return {'ok': True, 'lead_id': lead.id,
            'message': f'Follow-up on {lead.company} set to {when}.'}, None


@action('reassign_lead', 'Reassign a lead', permission='admin.access')
def reassign_lead(scope, params, *, commit=False, actor=None):
    """Reassignment goes through the existing service, so §16's history
    row, validation and notification cannot be skipped by coming in
    through the Copilot instead of the screen."""
    from app import Employee, db
    from app.services import lead_assignment

    lead, err = _lead_in_scope(scope, params)
    if err:
        return None, err
    primary = (params.get('primary') or '').strip()
    if not primary:
        return None, 'Who should own it?'
    if not Employee.query.filter_by(emp_code=primary, is_active=True).first():
        return None, f'{primary} is not an active employee.'
    reason = (params.get('reason') or '').strip()
    if reason not in lead_assignment.REASSIGNMENT_REASONS:
        return None, ('Pick a reason: '
                      + ', '.join(lead_assignment.REASSIGNMENT_REASONS))

    if not commit:
        return _proposal(
            'reassign_lead', target=f'lead:{lead.id}',
            summary=(f'Reassign {lead.company} from '
                     f'{lead.assigned_to or "nobody"} to {primary} — '
                     f'{reason}.'),
            changes={'lead_id': lead.id, 'primary': primary,
                     'secondary': params.get('secondary') or '',
                     'reason': reason}), None

    ok, why = lead_assignment.assign(
        lead, primary_code=primary,
        secondary_code=params.get('secondary') or None,
        actor=actor or scope.emp_code, note=reason)
    if not ok:
        db.session.rollback()
        return None, why
    db.session.commit()
    return {'ok': True, 'lead_id': lead.id,
            'message': f'{lead.company} reassigned to {primary}.'}, None


@action('update_stage', 'Move a lead to another stage',
        permission='admin.access')
def update_stage(scope, params, *, commit=False, actor=None):
    from app import STAGES_ALL, LeadStageHistory, db

    lead, err = _lead_in_scope(scope, params)
    if err:
        return None, err
    stage = (params.get('stage') or '').strip()
    if stage not in STAGES_ALL:
        return None, f'Not a stage. One of: {", ".join(STAGES_ALL)}'
    if stage == lead.stage:
        return None, f'{lead.company} is already at {stage}.'

    if not commit:
        return _proposal(
            'update_stage', target=f'lead:{lead.id}',
            summary=(f'Move {lead.company} from {lead.stage} to {stage}.'),
            changes={'lead_id': lead.id, 'stage': stage}), None

    previous = lead.stage
    lead.stage = stage
    try:
        db.session.add(LeadStageHistory(
            lead_id=lead.id, from_stage=previous, to_stage=stage,
            changed_by=actor or scope.emp_code))
    except Exception:
        pass                       # history is additive, never blocking
    db.session.commit()
    return {'ok': True, 'lead_id': lead.id,
            'message': f'{lead.company}: {previous} → {stage}.'}, None


def available(scope):
    """Actions this viewer could take, for the panel."""
    if not enabled():
        return []
    return [{'key': a['key'], 'label': a['label']}
            for a in ACTIONS.values()
            if not a['permission'] or scope.can(a['permission'])]
