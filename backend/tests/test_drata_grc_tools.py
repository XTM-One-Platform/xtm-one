"""Tests for the Drata GRC read tools.

These tools previously returned wrong / unusable data and shipped without
automated coverage. Each test pins one of the corrected behaviours against a
documented v2 payload shape:

  * ``list_drata_users`` resolves an exact email via the ``users/email:<addr>``
    path form (stripped), and a blank / whitespace email falls through to the
    list endpoint instead of issuing a malformed ``/users/email:`` lookup.
  * ``list_drata_personnel`` counts a compliance check as an issue only when it
    FAILs or is MISCONFIGURED (the old code compared against ``"PASSING"`` and
    wrongly flagged every healthy ``PASS``).
  * ``list_drata_user_assigned_policies`` reads the nested ``policy.name`` /
    ``policyVersion.version`` / ``acceptedAt`` fields (requesting them via
    ``expand[]``) and prints an ACCEPTED / NOT ACCEPTED verdict.
  * ``acknowledge_drata_assigned_policy`` POSTs to ``action-acknowledge`` with
    an ``acceptedAt`` body.
"""

from __future__ import annotations

import json

import pytest

from app.integrations.providers import drata
from app.integrations.providers.drata import _common as drata_common
from app.tools.base import CallableToolExecutor, ToolCall

_TOKEN = "https://public-api.drata.com/public/v2||test-token"


class _Resp:
    """Minimal ``httpx.Response`` stand-in for the captured tool calls."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.content = b"{}"

    def json(self) -> dict:
        return self._payload


def _patch(monkeypatch, payload: dict) -> list[dict]:
    """Patch ``safe_request`` to capture each call and return a fixed payload."""
    calls: list[dict] = []

    async def _fake_safe_request(
        client, method, url, provider, action, *, expected_statuses=None, **kwargs
    ):
        calls.append({"method": method, "url": url, "kwargs": kwargs})
        return _Resp(payload), None

    monkeypatch.setattr(drata_common, "safe_request", _fake_safe_request)
    return calls


def _build_executor() -> CallableToolExecutor:
    executor = CallableToolExecutor()
    drata.build_tools(_TOKEN, executor)
    return executor


async def _call(executor, name, args):
    return await executor.execute_tool(ToolCall(id="c1", name=name, arguments=args))


@pytest.mark.asyncio
async def test_list_users_email_resolves_single_account_stripped(monkeypatch):
    calls = _patch(
        monkeypatch,
        {
            "id": 7,
            "firstName": "Jo",
            "lastName": "Smith",
            "email": "jo@example.com",
            "jobTitle": "Engineer",
            "roles": [],
        },
    )
    executor = _build_executor()
    result = await _call(executor, "list_drata_users", {"email": "  jo@example.com  "})

    assert result.is_error is False
    # Exact-email path form, with the address stripped (no surrounding spaces).
    assert calls[0]["method"] == "get"
    assert calls[0]["url"].endswith("/users/email:jo@example.com")
    assert "jo@example.com" in result.content


@pytest.mark.asyncio
async def test_list_users_whitespace_email_falls_through_to_list(monkeypatch):
    calls = _patch(monkeypatch, {"data": [], "pagination": {}})
    executor = _build_executor()
    result = await _call(executor, "list_drata_users", {"email": "   "})

    assert result.is_error is False
    # A whitespace-only email must NOT issue a malformed ``/users/email:`` lookup;
    # it falls through to the list endpoint.
    assert all("/users/email:" not in c["url"] for c in calls)
    assert any(c["url"].endswith("/users") for c in calls)


@pytest.mark.asyncio
async def test_personnel_counts_only_fail_and_misconfigured_as_issues(monkeypatch):
    _patch(
        monkeypatch,
        {
            "data": [
                {
                    "id": 1,
                    "employmentStatus": "CURRENT_EMPLOYEE",
                    "user": {
                        "firstName": "Ada",
                        "lastName": "Pass",
                        "email": "ada@example.com",
                    },
                    "complianceChecks": [
                        {"status": "PASS"},
                        {"status": "EXCLUDED"},
                    ],
                },
                {
                    "id": 2,
                    "employmentStatus": "CURRENT_EMPLOYEE",
                    "user": {
                        "firstName": "Bo",
                        "lastName": "Fail",
                        "email": "bo@example.com",
                    },
                    "complianceChecks": [
                        {"status": "FAIL"},
                        {"status": "MISCONFIGURED"},
                        {"status": "PASS"},
                    ],
                },
            ],
            "pagination": {},
        },
    )
    executor = _build_executor()
    result = await _call(executor, "list_drata_personnel", {})

    assert result.is_error is False
    # All-PASS/EXCLUDED person is compliant (0 issues); the FAIL+MISCONFIGURED
    # person has exactly 2 issues. The old logic wrongly reported PASS as an issue.
    assert "ada@example.com" in result.content
    assert "compliant" in result.content
    assert "2 issue(s)" in result.content


@pytest.mark.asyncio
async def test_assigned_policies_use_nested_fields_and_expand(monkeypatch):
    calls = _patch(
        monkeypatch,
        {
            "data": [
                {
                    "policyId": 10,
                    "acceptedAt": "2026-01-02T00:00:00Z",
                    "policy": {
                        "id": 10,
                        "name": "Security Policy",
                        "policyStatus": "PUBLISHED",
                    },
                    "policyVersion": {"version": 1, "subVersion": 2},
                },
                {
                    "policyId": 11,
                    "policy": {"id": 11, "name": "Privacy Policy"},
                    "policyVersion": {"version": 3, "subVersion": 0},
                },
            ],
            "pagination": {},
        },
    )
    executor = _build_executor()
    result = await _call(
        executor, "list_drata_user_assigned_policies", {"user_id": 5}
    )

    assert result.is_error is False
    # Nested policy.name + acceptance verdict + policyVersion.version.
    assert "Security Policy" in result.content
    assert "ACCEPTED (2026-01-02" in result.content
    assert "v1.2" in result.content
    assert "Privacy Policy" in result.content
    assert "NOT ACCEPTED" in result.content
    # The nested objects are only returned when expanded.
    assert any("expand" in str(c["kwargs"].get("params")) for c in calls)


@pytest.mark.asyncio
async def test_acknowledge_uses_action_acknowledge_post(monkeypatch):
    calls = _patch(monkeypatch, {"acceptedAt": "2026-01-02T00:00:00Z"})
    executor = _build_executor()
    result = await _call(
        executor,
        "acknowledge_drata_assigned_policy",
        {"user_id": 5, "policy_id": 10},
    )

    assert result.is_error is False
    call = calls[0]
    assert call["method"] == "post"
    assert call["url"].endswith("/users/5/assigned-policies/10/action-acknowledge")
    # Body carries the required acceptedAt (defaulted to now when omitted).
    assert "acceptedAt" in call["kwargs"]["json"]


def _paged_safe_request(state: dict, sizes: list[int]):
    """A safe_request fake that serves a finite dataset across cursor pages,
    honouring the requested ``size`` and recording each requested size."""

    async def _fake(client, method, url, provider, action, *, expected_statuses=None, **kwargs):
        size = kwargs["params"]["size"]
        sizes.append(size)
        start = state["served"]
        n = min(size, state["total"] - start)
        state["served"] += n
        batch = [{"id": start + i} for i in range(n)]
        more = state["served"] < state["total"]
        return _Resp({"data": batch, "pagination": {"cursor": "c" if more else None}}), None

    return _fake


@pytest.mark.asyncio
async def test_paginated_fetch_caps_page_size_to_remaining_budget(monkeypatch):
    """Each page is sized from the REMAINING budget, so a max_items that isn't a
    multiple of page_size never over-fetches and the truncation flag stays
    reliable (the old fixed-size logic would over-fetch the last page and could
    silently drop records with truncated=False)."""
    sizes: list[int] = []
    monkeypatch.setattr(
        drata_common, "safe_request", _paged_safe_request({"served": 0, "total": 300}, sizes)
    )

    items, err, truncated = await drata_common._paginated_fetch_all(
        None, "http://x/users", {}, {}, max_items=250, page_size=100
    )

    assert err is None
    assert len(items) == 250  # exactly the cap, nothing silently dropped
    assert truncated is True  # more existed beyond the cap -> warned
    # The final page is capped to the 50-record remainder, never another 100.
    assert sizes == [100, 100, 50]


@pytest.mark.asyncio
async def test_paginated_fetch_no_truncation_when_dataset_fits(monkeypatch):
    """When the dataset fits within max_items, truncated is False (all rows
    returned) - no false-positive truncation warning."""
    sizes: list[int] = []
    monkeypatch.setattr(
        drata_common, "safe_request", _paged_safe_request({"served": 0, "total": 150}, sizes)
    )

    items, err, truncated = await drata_common._paginated_fetch_all(
        None, "http://x/users", {}, {}, max_items=500, page_size=100
    )

    assert err is None
    assert len(items) == 150
    assert truncated is False


# ---------------------------------------------------------------------------
# Assigned-policy acknowledgment masking + dedup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assigned_policy_pending_not_masked_when_requires_ack_false(monkeypatch):
    """A current, unaccepted policy whose version reports
    ``requiresAcknowledgment: False`` must render NOT ACCEPTED - never the old
    reassuring "no acknowledgment required" string that hid real non-compliance
    (the Caroline / Alexandre DPP incident)."""
    _patch(
        monkeypatch,
        {
            "data": [
                {
                    "policyId": 9,
                    "acceptedAt": None,
                    "policy": {"id": 9, "name": "Data Protection Policy"},
                    "policyVersion": {
                        "version": 1,
                        "subVersion": 2,
                        "current": True,
                        "requiresAcknowledgment": False,
                    },
                }
            ],
            "pagination": {},
        },
    )
    executor = _build_executor()
    result = await _call(
        executor, "list_drata_user_assigned_policies", {"user_id": 288}
    )

    assert result.is_error is False
    assert "Data Protection Policy" in result.content
    assert "NOT ACCEPTED" in result.content
    # The bug: the old code emitted this for unaccepted-but-flagged rows.
    assert "no acknowledgment required" not in result.content
    # The raw flag is still surfaced (with a verify caveat), and the version is
    # shown as current.
    assert "Acknowledgment required: no" in result.content
    assert "(current)" in result.content


@pytest.mark.asyncio
async def test_assigned_policies_collapse_archived_duplicate_rows(monkeypatch):
    """Multiple (policy, version) rows for the same policy collapse to one row -
    the current published version - so a policy is never double-counted."""
    _patch(
        monkeypatch,
        {
            "data": [
                {
                    "policyId": 9,
                    "acceptedAt": None,
                    "policy": {"id": 9, "name": "Data Protection Policy"},
                    "policyVersion": {
                        "version": 1,
                        "subVersion": 0,
                        "current": False,
                        "id": 100,
                    },
                },
                {
                    "policyId": 9,
                    "acceptedAt": None,
                    "policy": {"id": 9, "name": "Data Protection Policy"},
                    "policyVersion": {
                        "version": 1,
                        "subVersion": 2,
                        "current": True,
                        "id": 120,
                        "requiresAcknowledgment": True,
                    },
                },
                {
                    "policyId": 9,
                    "acceptedAt": None,
                    "policy": {"id": 9, "name": "Data Protection Policy"},
                    "policyVersion": {
                        "version": 1,
                        "subVersion": 1,
                        "current": False,
                        "id": 110,
                    },
                },
            ],
            "pagination": {},
        },
    )
    executor = _build_executor()
    result = await _call(
        executor, "list_drata_user_assigned_policies", {"user_id": 288}
    )

    assert result.is_error is False
    # Exactly one row for the policy, kept at the current version (v1.2).
    assert result.content.count("[policy 9]") == 1
    assert "v1.2 (current)" in result.content
    assert "1 assigned policy:" in result.content
    assert "Collapsed 2 archived / superseded version rows" in result.content


def test_dedupe_assigned_policies_notes_prior_version_acceptance():
    """When the current version is unaccepted but an older version was accepted,
    the kept row carries the prior acceptance date so the formatter can flag a
    pending re-acknowledgment."""
    deduped, dropped = drata_common._dedupe_assigned_policies(
        [
            {
                "policyId": 9,
                "acceptedAt": "2025-01-01T00:00:00Z",
                "policyVersion": {"version": 1, "current": False, "id": 100},
            },
            {
                "policyId": 9,
                "acceptedAt": None,
                "policyVersion": {"version": 2, "current": True, "id": 200},
            },
        ]
    )

    assert dropped == 1
    assert len(deduped) == 1
    kept = deduped[0]
    # The current (v2) row is kept, not the accepted-but-superseded v1 row.
    assert kept["policyVersion"]["version"] == 2
    assert kept.get("_priorVersionAcceptedAt") == "2025-01-01T00:00:00Z"
    line = drata_common._fmt_assigned_policy(kept)
    assert "NOT ACCEPTED" in line
    assert "prior version was accepted on 2025-01-01" in line


# ---------------------------------------------------------------------------
# Monitoring tests / failures / exclusions field mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_monitoring_tests_read_check_fields_and_map_filter(monkeypatch):
    """The list must read checkResultStatus / checkStatus / lastPassedAt /
    failedSince (not result / status / lastCheckedAt, which rendered as N/A),
    surface the usable testId, and map the result filter to checkResultStatus."""
    calls = _patch(
        monkeypatch,
        {
            "data": [
                {
                    "id": 9999,
                    "testId": 191,
                    "name": "Contractors Acknowledge the Data Protection Policy",
                    "checkResultStatus": "FAILED",
                    "checkStatus": "ENABLED",
                    "lastPassedAt": None,
                    "failedSince": "2026-06-09T10:25:00Z",
                }
            ],
            "pagination": {},
        },
    )
    executor = _build_executor()
    result = await _call(
        executor, "list_drata_monitoring_tests", {"result": "failed"}
    )

    assert result.is_error is False
    assert "[test 191]" in result.content  # usable testId, not the account-wide id
    assert "Result: FAILED" in result.content
    assert "Status: ENABLED" in result.content
    assert "Failing since: 2026-06-09T10:25:00Z" in result.content
    # The result filter maps to the documented checkResultStatus query param
    # (uppercased); the ignored `status` param is never sent.
    params = calls[0]["kwargs"]["params"]
    assert params.get("checkResultStatus") == "FAILED"
    assert "status" not in params


@pytest.mark.asyncio
async def test_monitoring_test_failures_use_documented_fields(monkeypatch):
    """Failures must render resourceName / providerName / accountName / region /
    cause - not the non-existent reason / detectedAt."""
    _patch(
        monkeypatch,
        {
            "data": [
                {
                    "id": "sg-1",
                    "resourceName": "web-server-sg",
                    "providerName": "Amazon Web Services",
                    "accountName": "Production",
                    "region": "us-east-1",
                    "cause": [{"detail": "open to 0.0.0.0/0"}],
                }
            ],
            "pagination": {},
        },
    )
    executor = _build_executor()
    result = await _call(
        executor,
        "list_drata_monitoring_test_failures",
        {"workspace_id": 1, "test_id": 191},
    )

    assert result.is_error is False
    assert "web-server-sg" in result.content
    assert "Amazon Web Services" in result.content
    assert "Production" in result.content
    assert "us-east-1" in result.content
    # The cause detail text is surfaced cleanly - not the raw dict literal.
    assert "Cause: open to 0.0.0.0/0" in result.content
    assert "{'detail'" not in result.content


@pytest.mark.asyncio
async def test_monitoring_test_exclusions_use_documented_fields(monkeypatch):
    """Exclusions must render targetName / exclusionReason / createdAt - not the
    non-existent resourceName / reason / excludedAt."""
    _patch(
        monkeypatch,
        {
            "data": [
                {
                    "id": 1,
                    "targetId": "sg-1",
                    "targetName": "Production Security Group",
                    "exclusionReason": "Legacy system decommission Q2",
                    "createdAt": "2026-01-01T00:00:00Z",
                }
            ],
            "pagination": {},
        },
    )
    executor = _build_executor()
    result = await _call(
        executor,
        "list_drata_monitoring_test_exclusions",
        {"workspace_id": 1, "test_id": 191},
    )

    assert result.is_error is False
    assert "Production Security Group" in result.content
    assert "Legacy system decommission Q2" in result.content
    # ``createdAt`` is labelled "Created" (matching the field), not "Excluded At".
    assert "Created: 2026-01-01T00:00:00Z" in result.content
    assert "Excluded At" not in result.content


# ---------------------------------------------------------------------------
# Control owner write paths - v2 field names
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_control_owner_posts_owner_id_not_user_id(monkeypatch):
    """The v2 ``Add Control Owner`` endpoint requires ``ownerId`` and rejects
    ``userId`` with a strict whitelist (the live HTTP 400 that blocked the
    Compliance Orchestrator). The body must carry ``ownerId`` only."""
    calls = _patch(monkeypatch, {})
    executor = _build_executor()
    result = await _call(
        executor,
        "add_drata_control_owner",
        {"workspace_id": 1, "control_id": 511, "user_id": 42},
    )

    assert result.is_error is False
    call = calls[0]
    assert call["method"] == "post"
    assert call["url"].endswith("/workspaces/1/controls/511/owners")
    body = call["kwargs"]["json"]
    # The wire field is ``ownerId`` (the user's Drata ID); ``userId`` is rejected.
    assert body == {"ownerId": 42}
    assert "userId" not in body


@pytest.mark.asyncio
async def test_create_control_sends_owners_ids_as_flat_numbers(monkeypatch):
    """The v2 ``Create Control`` body takes ``ownersIds`` as a flat array of
    numeric user IDs - not the ``owners`` key nor the ``[{"id": n}]`` object
    shape other Drata write endpoints use."""
    calls = _patch(monkeypatch, {"id": 99, "name": "Custom Control", "code": "DCF-1"})
    executor = _build_executor()
    result = await _call(
        executor,
        "create_drata_control",
        {
            "workspace_id": 1,
            "name": "Custom Control",
            "description": "A control",
            "owner_ids": "1, 2, 3",
        },
    )

    assert result.is_error is False
    call = calls[0]
    assert call["method"] == "post"
    assert call["url"].endswith("/workspaces/1/controls")
    body = call["kwargs"]["json"]
    assert body["ownersIds"] == [1, 2, 3]
    # The buggy field name / shape must not be sent.
    assert "owners" not in body
    assert {"id": 1} not in body["ownersIds"]


# ---------------------------------------------------------------------------
# Control listing: context-saving filters + inline owner IDs
# ---------------------------------------------------------------------------

_CONTROLS_PAYLOAD = {
    "data": [
        {
            "id": 100,
            "code": "DCF-1",
            "name": "Ctrl One",
            "flags": {"isReady": False, "hasEvidence": False},
            "frameworkTags": ["SOC2"],
            "owners": {
                "data": [
                    {"id": 42, "firstName": "Sam", "lastName": "H", "email": "s@x.io"}
                ]
            },
        },
        {
            "id": 200,
            "code": "DCF-2",
            "name": "Ctrl Two",
            "flags": {"isReady": True, "hasEvidence": True},
            "frameworkTags": [],
            "owners": {"data": []},
        },
    ],
    "pagination": {},
}


@pytest.mark.asyncio
async def test_list_controls_compact_includes_owner_ids(monkeypatch):
    """Compact mode emits inline owner Drata user IDs (so re-assignment needs no
    extra per-control lookup) and marks ownerless controls as ``owners=[none]``."""
    _patch(monkeypatch, _CONTROLS_PAYLOAD)
    executor = _build_executor()
    result = await _call(executor, "list_drata_controls", {"compact": "true"})

    assert result.is_error is False
    assert "(id=100)" in result.content
    assert "Sam H(id=42)" in result.content  # owner ID inline
    assert "owners=[none]" in result.content  # the ownerless control


@pytest.mark.asyncio
async def test_list_controls_without_owner_filter(monkeypatch):
    """``without_owner=true`` returns only the ownerless work queue."""
    _patch(monkeypatch, _CONTROLS_PAYLOAD)
    executor = _build_executor()
    result = await _call(
        executor, "list_drata_controls", {"without_owner": "true"}
    )

    assert result.is_error is False
    assert "DCF-2" in result.content
    assert "DCF-1" not in result.content


@pytest.mark.asyncio
async def test_list_controls_owner_id_filter_and_limit(monkeypatch):
    """``owner_id`` keeps only controls owned by that user; ``limit`` caps the set."""
    _patch(monkeypatch, _CONTROLS_PAYLOAD)
    executor = _build_executor()

    owned = await _call(executor, "list_drata_controls", {"owner_id": 42})
    assert "DCF-1" in owned.content
    assert "DCF-2" not in owned.content

    capped = await _call(executor, "list_drata_controls", {"limit": 1})
    assert "DCF-1" in capped.content
    assert "DCF-2" not in capped.content


# ---------------------------------------------------------------------------
# Bulk control owner assignment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_control_owners_bulk_assign_and_remove(monkeypatch):
    """One call assigns owners across many controls and removes stale ones,
    issuing the right DELETE/POST requests and a compact summary."""
    calls = _patch(monkeypatch, {})
    executor = _build_executor()
    result = await _call(
        executor,
        "set_drata_control_owners",
        {
            "workspace_id": 1,
            "assignments_json": json.dumps(
                [
                    {"control_id": 100, "owner_id": 42},
                    {"control_id": 200, "owner_id": 42, "remove_owner_ids": [7, 8]},
                ]
            ),
        },
    )

    assert result.is_error is False
    assert "2 owner(s) assigned" in result.content
    assert "2 owner(s) removed" in result.content
    assert "0 error(s)" in result.content

    deletes = [c for c in calls if c["method"] == "delete"]
    posts = [c for c in calls if c["method"] == "post"]
    assert {c["url"].rsplit("/", 1)[-1] for c in deletes} == {"7", "8"}
    assert all(c["kwargs"]["json"] == {"ownerId": 42} for c in posts)
    assert len(posts) == 2


@pytest.mark.asyncio
async def test_set_control_owners_does_not_remove_the_owner_being_set(monkeypatch):
    """An owner listed in remove_owner_ids that equals the owner being set is
    NOT removed (so a re-assignment to the same owner is a no-op, not a churn)."""
    calls = _patch(monkeypatch, {})
    executor = _build_executor()
    result = await _call(
        executor,
        "set_drata_control_owners",
        {
            "assignments_json": json.dumps(
                [{"control_id": 100, "owner_id": 42, "remove_owner_ids": [42, 7]}]
            )
        },
    )

    assert result.is_error is False
    deletes = [c for c in calls if c["method"] == "delete"]
    # Only owner 7 is removed; owner 42 (the one being set) is preserved.
    assert {c["url"].rsplit("/", 1)[-1] for c in deletes} == {"7"}
    assert "1 owner(s) assigned" in result.content
    assert "1 owner(s) removed" in result.content


@pytest.mark.asyncio
async def test_set_control_owners_rejects_invalid_json(monkeypatch):
    _patch(monkeypatch, {})
    executor = _build_executor()
    result = await _call(
        executor, "set_drata_control_owners", {"assignments_json": "not json"}
    )
    assert "not valid JSON" in result.content


@pytest.mark.asyncio
async def test_set_control_owners_handles_non_list_remove_owner_ids(monkeypatch):
    """A non-list remove_owner_ids (e.g. a bare int) is reported as an error for
    that item and the batch continues, instead of raising TypeError and aborting
    the whole call (or silently iterating a string character-by-character)."""
    calls = _patch(monkeypatch, {})
    executor = _build_executor()
    result = await _call(
        executor,
        "set_drata_control_owners",
        {
            "assignments_json": json.dumps(
                [
                    {"control_id": 100, "owner_id": 42, "remove_owner_ids": 7},
                    {"control_id": 200, "owner_id": 42},
                ]
            )
        },
    )

    assert result.is_error is False
    # The malformed item is skipped with an error; the valid item still runs.
    assert "1 owner(s) assigned" in result.content
    assert "1 error(s)" in result.content
    assert "remove_owner_ids" in result.content
    # The bad item triggers no DELETE (no char-by-char / crashing iteration) and
    # exactly one POST is issued for the valid assignment.
    assert not [c for c in calls if c["method"] == "delete"]
    assert len([c for c in calls if c["method"] == "post"]) == 1


@pytest.mark.asyncio
async def test_set_control_owners_rejects_falsey_non_list_remove_owner_ids(monkeypatch):
    """A falsey non-list remove_owner_ids (e.g. ``0`` or ``""``) is treated as
    invalid input - NOT silently coerced to ``[]`` and accepted. Regression for
    the ``item.get(...) or []`` shortcut, which would have swallowed ``0``/``""``
    and skipped the type validation, so the malformed item was accepted as if no
    removals were requested. Now it is reported as an error and the item is
    skipped, while a sibling valid item still processes."""
    calls = _patch(monkeypatch, {})
    executor = _build_executor()
    result = await _call(
        executor,
        "set_drata_control_owners",
        {
            "assignments_json": json.dumps(
                [
                    {"control_id": 100, "owner_id": 42, "remove_owner_ids": 0},
                    {"control_id": 200, "owner_id": 42},
                ]
            )
        },
    )

    assert result.is_error is False
    # The falsey-but-wrong-typed item is rejected with an error (NOT accepted as
    # "no removals"); only the genuinely valid sibling item is assigned.
    assert "1 owner(s) assigned" in result.content
    assert "1 error(s)" in result.content
    assert "remove_owner_ids" in result.content
    assert not [c for c in calls if c["method"] == "delete"]
    assert len([c for c in calls if c["method"] == "post"]) == 1


# ---------------------------------------------------------------------------
# Tool args are not type-coerced on every execution path
#
# The integration tool-test endpoint dispatches straight to the executor
# (``executor.execute_tool(call)``), bypassing the agent loop's jsonschema
# pre-validation - exactly what ``_call`` does here. A JSON boolean / array
# therefore reaches the handler as a Python ``bool`` / ``list``, so the
# handlers must coerce/guard instead of raising AttributeError / TypeError.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_controls_tolerates_boolean_flags(monkeypatch):
    """A JSON boolean reaching a string flag as a Python ``bool`` must be
    coerced - not crash the tool with ``AttributeError`` on ``bool.lower()``."""
    _patch(monkeypatch, _CONTROLS_PAYLOAD)
    executor = _build_executor()

    # compact / without_owner passed as real booleans (not the "true"/"false"
    # strings the schema documents) must still work, not raise.
    compacted = await _call(
        executor,
        "list_drata_controls",
        {"compact": True, "without_owner": True},
    )
    assert compacted.is_error is False
    # without_owner=True still filters to the ownerless control queue...
    assert "DCF-2" in compacted.content
    assert "DCF-1" not in compacted.content
    # ...and compact=True still produces the terse, inline-owner format.
    assert "owners=[none]" in compacted.content

    # is_ready / has_evidence booleans are equally tolerated (no crash).
    ready = await _call(
        executor,
        "list_drata_controls",
        {"is_ready": True, "has_evidence": False},
    )
    assert ready.is_error is False


@pytest.mark.asyncio
async def test_set_control_owners_rejects_non_string_assignments_json(monkeypatch):
    """A non-string assignments_json (the model passed the array directly
    instead of a JSON string) makes json.loads raise TypeError, not
    JSONDecodeError. The handler must return a controlled error - not leak the
    raw exception - and issue no requests."""
    calls = _patch(monkeypatch, {})
    executor = _build_executor()
    result = await _call(
        executor,
        "set_drata_control_owners",
        {"assignments_json": [{"control_id": 100, "owner_id": 42}]},
    )

    assert "must be a JSON string" in result.content
    # No DELETE/POST is issued for the malformed call.
    assert not calls


@pytest.mark.asyncio
async def test_set_control_owners_rejects_boolean_control_id(monkeypatch):
    """A JSON boolean control_id (e.g. {"control_id": true}) must NOT be read as
    int 1 - bool subclasses int, so int(True) == 1 would issue writes against the
    WRONG control. The item is rejected as invalid and skipped (no request on
    control 1), while a sibling valid item still processes."""
    calls = _patch(monkeypatch, {})
    executor = _build_executor()
    result = await _call(
        executor,
        "set_drata_control_owners",
        {
            "assignments_json": json.dumps(
                [
                    {"control_id": True, "owner_id": 42},
                    {"control_id": 200, "owner_id": 42},
                ]
            )
        },
    )

    assert result.is_error is False
    # The boolean control_id is rejected (not coerced to control 1); only the
    # genuinely valid sibling (control 200) is assigned.
    assert "1 owner(s) assigned" in result.content
    assert "1 error(s)" in result.content
    # Crucially: no request is ever issued against control 1 (the int(True) misread).
    assert not [c for c in calls if "/controls/1/" in c["url"]]
    # Exactly one POST, and it targets control 200.
    posts = [c for c in calls if c["method"] == "post"]
    assert len(posts) == 1
    assert "/controls/200/owners" in posts[0]["url"]


@pytest.mark.asyncio
async def test_list_controls_false_filter_is_sent_not_dropped(monkeypatch):
    """An explicit ``is_ready`` / ``has_evidence`` false is a real filter value,
    not "absent": it must be forwarded to Drata as ``isReady=False`` /
    ``hasEvidence=False`` rather than treated as "not provided". An unset flag
    (the "" default) still adds no query param at all.

    The old ``if is_ready:`` gate dropped a JSON boolean ``False`` (reachable on
    the tool-test path) as if the flag were absent, so callers could not filter
    for the not-ready / no-evidence queues."""
    calls = _patch(monkeypatch, _CONTROLS_PAYLOAD)
    executor = _build_executor()

    # String "false" (the schema-documented form) -> param sent, value False.
    res = await _call(
        executor,
        "list_drata_controls",
        {"is_ready": "false", "has_evidence": "false"},
    )
    assert res.is_error is False
    params = calls[-1]["kwargs"]["params"]
    assert params.get("isReady") is False
    assert params.get("hasEvidence") is False

    # A JSON boolean False (tool-test path) is honored the same way, not dropped.
    calls.clear()
    res2 = await _call(executor, "list_drata_controls", {"is_ready": False})
    assert res2.is_error is False
    assert calls[-1]["kwargs"]["params"].get("isReady") is False

    # Unset (default "") -> no isReady / hasEvidence key emitted.
    calls.clear()
    res3 = await _call(executor, "list_drata_controls", {})
    assert res3.is_error is False
    unset_params = calls[-1]["kwargs"]["params"]
    assert "isReady" not in unset_params
    assert "hasEvidence" not in unset_params


@pytest.mark.asyncio
async def test_list_controls_coerces_string_owner_id_and_limit(monkeypatch):
    """``owner_id`` / ``limit`` are integers in the schema but can arrive as JSON
    strings on the tool-test path. A string ``owner_id`` must still filter (an
    int control-owner id never equals a str) and a string ``limit`` must not
    raise ``TypeError`` on ``limit > 0`` / the slice."""
    _patch(monkeypatch, _CONTROLS_PAYLOAD)
    executor = _build_executor()

    owned = await _call(executor, "list_drata_controls", {"owner_id": "42"})
    assert owned.is_error is False
    assert "DCF-1" in owned.content  # owned by user 42
    assert "DCF-2" not in owned.content

    capped = await _call(executor, "list_drata_controls", {"limit": "1"})
    assert capped.is_error is False
    assert "DCF-1" in capped.content
    assert "DCF-2" not in capped.content
