"""
Copilot search still finds a passage when the index is large.

The candidate cap (4,000) used to apply to the whole scoped index in id
order, with the lead and the words filtered afterwards. On production
data an admin's search looked only at the oldest 4,000 passages, and a
search within one lead indexed later found nothing.
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'SearchScaleTestOnly123')
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'searchscale.db'))

from app import app as flask_app, db                           # noqa: E402
from app.access import scope as scope_mod                      # noqa: E402
from app.copilot import retrieval                              # noqa: E402
from app.models.copilot import CopilotChunk                    # noqa: E402

LEAD_BASE = 9_000_000          # ids no other module uses


@pytest.fixture()
def big_index():
    with flask_app.app_context():
        db.create_all()
        old = datetime.utcnow() - timedelta(days=900)
        rows = [dict(lead_id=LEAD_BASE, owner_emp_code='SSOWN',
                     source='note', occurred_at=old,
                     text='the zanzibar gantry lift needs a 600t crawler')]
        rows += [dict(lead_id=LEAD_BASE + 1 + i, owner_emp_code='SSOWN',
                      source='email:inbound',
                      occurred_at=datetime.utcnow() - timedelta(minutes=i),
                      text='please share your best rate for the shipment')
                 for i in range(4_100)]
        rows.append(dict(lead_id=LEAD_BASE + 99_999, owner_emp_code='SSOWN',
                         source='note', occurred_at=old,
                         text='a late-indexed lead about warehousing'))
        db.session.bulk_insert_mappings(CopilotChunk, rows)
        db.session.commit()
        yield
        CopilotChunk.query.filter(
            CopilotChunk.lead_id >= LEAD_BASE).delete(
            synchronize_session=False)
        db.session.commit()


def _everyone():
    sc = scope_mod.for_employee('SSOWN')
    sc.codes = None                    # an unrestricted viewer
    return sc


def test_an_old_passage_is_found_past_the_cap(big_index):
    with flask_app.app_context():
        found = retrieval.search(_everyone(), 'zanzibar gantry')
    assert [h['lead_id'] for h in found['hits']] == [LEAD_BASE]


def test_search_within_a_lead_reaches_it_past_the_cap(big_index):
    with flask_app.app_context():
        found = retrieval.search(_everyone(), 'warehousing',
                                 lead_id=LEAD_BASE + 99_999)
    assert [h['lead_id'] for h in found['hits']] == [LEAD_BASE + 99_999]


def test_the_scope_filter_still_applies_first(big_index):
    with flask_app.app_context():
        sc = scope_mod.for_employee('SSOWN')
        sc.codes = ['SOMEONE_ELSE']
        assert retrieval.search(sc, 'zanzibar gantry')['hits'] == []
