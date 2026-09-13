"""A change to runtime configuration is recorded, and secrets are not."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'ConfigAuditTestOnly12345')
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'configaudit.db'))

from app import app as flask_app, db                            # noqa: E402
from app.models.audit import AuditEvent                         # noqa: E402
from app.services import config_audit                           # noqa: E402


def test_only_a_changed_configuration_is_recorded_and_secrets_stay_secret():
    env = {'PROCAM_AI_ACTIONS': 'off', 'GROQ_API_KEY': 'gsk_live_secret_1',
           'LEAD_INTAKE_MODE': 'enforce'}
    with flask_app.app_context():
        db.create_all()
        AuditEvent.query.filter_by(action='config.runtime_change').delete()
        db.session.commit()
        first = config_audit.record_if_changed(db, env)
        assert first is not None
        assert config_audit.record_if_changed(db, env) is None   # unchanged
        env2 = dict(env, PROCAM_AI_ACTIONS='on', GROQ_API_KEY='gsk_other_2')
        second = config_audit.record_if_changed(db, env2)
        assert second is not None
        # the key's value changed, but only its presence is recorded, so
        # that is not a difference; the flag is.
        assert second.new_value['changed'] == ['PROCAM_AI_ACTIONS']
        assert second.old_value['settings']['PROCAM_AI_ACTIONS'] == 'off'
        text = str(first.new_value) + str(second.new_value)
        assert 'gsk_' not in text
        assert second.new_value['settings']['groq_provider'] == 'set'
        AuditEvent.query.filter_by(action='config.runtime_change').delete()
        db.session.commit()
