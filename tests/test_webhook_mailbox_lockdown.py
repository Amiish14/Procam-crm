"""Tests for the webhook's mailbox-lockdown resource parsing.

Regression guard for the 2026-09-02 outage: Graph names the mailbox in a
notification's `resource` by directory object id, not by UPN, so a
substring match on the UPN rejected every real notification and no leads
were ingested at all.

Stubs `msal` so the module imports without the Graph SDK installed.
Run with:  python3 tests/test_webhook_mailbox_lockdown.py
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.modules.setdefault('msal', types.ModuleType('msal'))

from email_ingest import webhook as W        # noqa: E402

UPN = 'leads@procamgroup.in'
OBJ_ID = '9a0b6a4e-0423-4e71-94b3-a77b849f2d05'


class FakeGraph:
    """Stands in for GraphClient; answers the object-id lookup only."""
    def __init__(self, obj_id=OBJ_ID, fail=False):
        self.obj_id, self.fail, self.calls = obj_id, fail, 0

    def _request(self, method, path, **kw):
        self.calls += 1
        r = types.SimpleNamespace()
        if self.fail:
            r.status_code = 404
            r.json = lambda: {}
        else:
            r.status_code = 200
            r.json = lambda: {'id': self.obj_id}
        return r


def setup_function(*_):
    W._MAILBOX_ID_CACHE.clear()


# ── resource parsing ──────────────────────────────────────────────────
def test_parses_object_id_form_graph_actually_sends():
    res = f"Users/{OBJ_ID}/Messages/AAMkAGVjNDlmMmU3LWQ5OWMtNDU0MS1hZ"
    assert W._resource_user_segment(res) == OBJ_ID


def test_parses_upn_form_the_subscription_was_created_with():
    res = f"/users/{UPN}/mailFolders('Inbox')/messages"
    assert W._resource_user_segment(res) == UPN


def test_parses_regardless_of_case_and_leading_slash():
    for res in (f"/Users/{UPN}/Messages/AAA",
                f"users/{UPN}/messages/AAA",
                f"/USERS/{UPN.upper()}/MESSAGES/AAA"):
        assert W._resource_user_segment(res) == UPN, res


def test_unparseable_resource_yields_empty_segment():
    for res in ('', None, 'garbage', '/groups/abc/messages/1'):
        assert W._resource_user_segment(res) == ''


# ── identifier set ────────────────────────────────────────────────────
def test_identifiers_include_both_upn_and_object_id():
    W._MAILBOX_ID_CACHE.clear()
    ids = W._mailbox_identifiers(FakeGraph(), UPN)
    assert ids == {UPN, OBJ_ID}


def test_object_id_is_resolved_once_and_cached():
    W._MAILBOX_ID_CACHE.clear()
    g = FakeGraph()
    W._mailbox_identifiers(g, UPN)
    W._mailbox_identifiers(g, UPN)
    W._mailbox_identifiers(g, UPN)
    assert g.calls == 1


def test_falls_back_to_upn_when_object_id_cannot_be_resolved():
    W._MAILBOX_ID_CACHE.clear()
    ids = W._mailbox_identifiers(FakeGraph(fail=True), UPN)
    assert ids == {UPN}


# ── the actual regression ─────────────────────────────────────────────
def test_real_rejected_resource_now_matches_the_sanctioned_mailbox():
    """The exact resource string that was being rejected in production."""
    W._MAILBOX_ID_CACHE.clear()
    res = f"Users/{OBJ_ID}/Messages/AAMkAGVjNDlmMmU3LWQ5OWMtNDU0MS1hZ"
    seg = W._resource_user_segment(res)
    assert seg in W._mailbox_identifiers(FakeGraph(), UPN)


def test_a_genuinely_different_mailbox_does_not_match():
    W._MAILBOX_ID_CACHE.clear()
    res = "Users/00000000-1111-2222-3333-444444444444/Messages/AAA"
    seg = W._resource_user_segment(res)
    assert seg not in W._mailbox_identifiers(FakeGraph(), UPN)


def test_another_upn_does_not_match():
    W._MAILBOX_ID_CACHE.clear()
    res = "/users/someone.else@procamgroup.in/mailFolders('Inbox')/messages"
    seg = W._resource_user_segment(res)
    assert seg not in W._mailbox_identifiers(FakeGraph(), UPN)


if __name__ == '__main__':
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            setup_function()
            try:
                fn()
                print(f'  PASS  {name}')
            except AssertionError as e:
                fails += 1
                print(f'  FAIL  {name}: {e}')
            except Exception as e:                       # noqa: BLE001
                fails += 1
                print(f'  ERROR {name}: {type(e).__name__}: {e}')
    print(f"\n{'FAILURES: %d' % fails if fails else 'all tests passed'}")
    sys.exit(1 if fails else 0)
