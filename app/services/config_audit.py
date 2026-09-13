"""Record changes to runtime configuration in the audit trail.

AI providers, feature flags and the ingest mode are set in .env, not on a
screen, so no request ever changes them and nothing would audit them. At
each boot the settings below are compared with the last recorded set; if
anything differs, one 'config.runtime_change' event names what changed.

Secrets are recorded only as "set" / "not set" — whether a key exists is
the configuration fact that matters; its value never is.
"""
import hashlib
import json
import os

#: Settings whose values are recorded.
TRACKED = (
    'PROCAM_AI_ACTIONS', 'PROCAM_AI_BASE_URL', 'PROCAM_AI_MODEL',
    'PROCAM_AI_CLASSIFIER_MODEL', 'PROCAM_AI_EMBED_URL',
    'PROCAM_AI_EMBED_MODEL', 'LEAD_INTAKE_AI', 'LEAD_INTAKE_MODE',
    'LEAD_INTAKE_AI_MODEL', 'EMAIL_AI_MODEL_GROQ', 'ANTHROPIC_MODEL',
    'EMAIL_AI_FALLBACK_THRESHOLD', 'AI_DAILY_TOKEN_BUDGET',
    'EMAIL_INGESTION_MODE', 'CRM_INBOX_EMAIL', 'NOTIFY_ENABLED',
    'NOTIFY_FROM', 'URL_PREFIX', 'SESSION_COOKIE_SECURE', 'CSP_MODE',
    'RATELIMIT_STORAGE_URI', 'LOG_LEVEL', 'DEBUG',
)
#: Settings recorded only as present or absent, under plain labels: a
#: label that looked like "SECRET" or "API_KEY" would be redacted by the
#: audit module itself, and every boot would then look like a change.
SECRETS = {
    'SECRET_KEY': 'session_signing',
    'GROQ_API_KEY': 'groq_provider',
    'ANTHROPIC_API_KEY': 'anthropic_provider',
    'MS_CLIENT_SECRET': 'graph_client',
    'MS_TENANT_ID': 'graph_tenant',
    'MS_CLIENT_ID': 'graph_app',
    'EMAIL_WEBHOOK_SECRET': 'webhook_verification',
    'PROCAM_AI_KEY': 'private_model_auth',
}


def current(environ=None):
    env = os.environ if environ is None else environ
    snap = {k: (env.get(k) or '') for k in TRACKED}
    for k, label in SECRETS.items():
        snap[label] = 'set' if env.get(k) else 'not set'
    return snap


def fingerprint(snap):
    return hashlib.sha256(json.dumps(snap, sort_keys=True).encode()) \
        .hexdigest()[:16]


def record_if_changed(db, environ=None):
    """Write one audit event when the configuration differs from the last
    recorded one. Returns the event or None."""
    from app.models.audit import AuditEvent
    from app.services import audit

    snap = current(environ)
    print_ = fingerprint(snap)
    last = (AuditEvent.query.filter_by(action='config.runtime_change')
            .order_by(AuditEvent.id.desc()).first())
    if last is not None and last.entity_id == print_:
        return None
    before = (last.new_value or {}).get('settings') if last else None
    # Compared as stored: the audit module redacts any name that looks
    # secret (AI_DAILY_TOKEN_BUDGET, for one), and a raw-vs-stored
    # comparison would report those as changed on every boot.
    stored = audit._clean(snap)
    changed = sorted(k for k in stored if (before or {}).get(k) != stored[k])
    ev = audit.record(
        'config.runtime_change', 'configuration', print_,
        old={'settings': before} if before else None,
        new={'settings': snap, 'changed': changed},
        actor='system',
        reason='first recorded configuration' if before is None
        else 'configuration differs from the last boot')
    db.session.commit()
    return ev
