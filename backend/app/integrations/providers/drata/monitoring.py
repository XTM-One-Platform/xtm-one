"""Drata monitoring tools - Monitoring tests, failures, exclusions, and retest."""

from __future__ import annotations

from typing import Any

from app.core.egress import egress_client
from app.integrations.http import format_entity_list
from app.tools.base import CallableToolExecutor, ToolDefinition

from . import _common
from ._common import (
    _PROVIDER,
    _coerce_int,
)

#: The API key permission the v1 retest endpoint requires.
_RETEST_PERMISSION = "Monitoring: Trigger Monitor Test"


def register(
    base_url: str,
    headers: dict[str, str],
    upload_headers: dict[str, str],
    executor: CallableToolExecutor,
) -> list[ToolDefinition]:
    """Build the Drata monitoring tools and register their callables."""

    def _fmt_monitoring_test(t: dict[str, Any]) -> str:
        # The usable identifier downstream (get / failures / exclusions) is the
        # workspace-scoped ``testId`` - the v2 API rejects the account-wide
        # ``id``. Result/status/timing live under checkResultStatus /
        # checkStatus / lastPassedAt / failedSince (NOT result / status /
        # lastCheckedAt, which do not exist and rendered as N/A).
        line = (
            f"[test {t.get('testId', t.get('id', '?'))}] {t.get('name', 'Unnamed')} — "
            f"Result: {t.get('checkResultStatus', 'N/A')} | "
            f"Status: {t.get('checkStatus', 'N/A')} | "
            f"Last passed: {t.get('lastPassedAt') or 'N/A'}"
        )
        if t.get("failedSince"):
            line += f" | Failing since: {t['failedSince']}"
        return line

    async def list_drata_monitoring_tests(workspace_id: int = 1, result: str = "", status: str = "") -> str:
        """List monitoring tests for a workspace.

        Optionally filter by ``result`` (READY / PASSED / FAILED / ERROR /
        PREAUDIT) and/or ``status`` (UNUSED / NEW / ENABLED / DISABLED /
        TESTING).
        """
        async with egress_client(timeout=30) as client:
            params: dict[str, Any] = {}
            if result:
                params["checkResultStatus"] = result.strip().upper()
            if status:
                params["checkStatus"] = status.strip().upper()
            items, err = await _common._paginated_fetch(
                client,
                f"{base_url}/workspaces/{workspace_id}/monitoring-tests",
                headers,
                params,
            )
            if err:
                return err
            return format_entity_list(items, _fmt_monitoring_test, noun="monitoring test")

    async def get_drata_monitoring_test(test_id: int, workspace_id: int = 1) -> str:
        """Get details of a specific monitoring test.

        ``test_id`` is the workspace-scoped ``testId`` shown by
        list_drata_monitoring_tests (the account-wide ``id`` is rejected).
        """
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{base_url}/workspaces/{workspace_id}/monitoring-tests/{test_id}",
                _PROVIDER,
                "get monitoring test",
                headers=headers,
            )
            if err:
                return err
            t = resp.json()
            lines = [
                f"Monitoring Test: {t.get('name', 'Unnamed')} (testId: {t.get('testId', t.get('id', '?'))})",
                f"Description: {t.get('description', 'N/A')}",
                f"Result: {t.get('checkResultStatus', 'N/A')}",
                f"Status: {t.get('checkStatus', 'N/A')}",
                f"Last passed: {t.get('lastPassedAt') or 'N/A'}",
                f"Failing since: {t.get('failedSince') or 'N/A'}",
                f"Created: {t.get('createdAt', 'N/A')}",
            ]
            return "\n".join(lines)

    def _fmt_monitoring_failure(f: dict[str, Any]) -> str:
        # Documented v2 failure fields are resourceName / providerName /
        # accountName / region / cause (an array of detail objects) - NOT the
        # old reason / detectedAt, which did not exist.
        parts = [
            f"[{f.get('id', '?')}] {f.get('resourceName') or 'N/A'}",
            f"Provider: {f.get('providerName', 'N/A')}",
            f"Account: {f.get('accountName', 'N/A')}",
        ]
        if f.get("region"):
            parts.append(f"Region: {f['region']}")
        cause = f.get("cause")
        if isinstance(cause, list) and cause:
            # ``cause`` is an array of detail objects; surface the ``detail``
            # text rather than the raw dict literal (braces/quotes) so the
            # readout stays human-readable, falling back to ``str()`` otherwise.
            first = cause[0]
            detail = first.get("detail") if isinstance(first, dict) else None
            parts.append(f"Cause: {str(detail or first)[:200]}")
        return " | ".join(parts)

    async def list_drata_monitoring_test_failures(test_id: int, workspace_id: int = 1) -> str:
        """List failing resources for a monitoring test (why it failed)."""
        async with egress_client(timeout=30) as client:
            items, err = await _common._paginated_fetch(
                client,
                f"{base_url}/workspaces/{workspace_id}/monitoring-tests/{test_id}/failures",
                headers,
                {},
            )
            if err:
                return err
            return format_entity_list(items, _fmt_monitoring_failure, noun="monitoring failure")

    async def list_drata_monitoring_test_exclusions(test_id: int, workspace_id: int = 1) -> str:
        """List exclusions for a monitoring test."""
        async with egress_client(timeout=30) as client:
            items, err = await _common._paginated_fetch(
                client,
                f"{base_url}/workspaces/{workspace_id}/monitoring-tests/{test_id}/exclusions",
                headers,
                {},
            )
            if err:
                return err
            return format_entity_list(
                items,
                # Documented v2 exclusion fields are targetId / targetName /
                # exclusionReason / createdAt - NOT resourceName / reason /
                # excludedAt, which did not exist.
                lambda x: (
                    f"[{x.get('id', '?')}] "
                    f"{x.get('targetName') or x.get('targetId') or 'N/A'} — "
                    f"Reason: {(x.get('exclusionReason') or 'N/A')[:200]} | "
                    f"Created: {x.get('createdAt', 'N/A')}"
                ),
                noun="monitoring exclusion",
            )

    # Re-running a test exists only on the v1 API (the "autopilot" retest), so
    # derive the v1 host from the configured v2 base URL, keeping the region.
    base_url_v1 = _common.to_v1_base_url(base_url)

    async def retest_drata_monitoring_test(test_id: int, workspace_id: int = 1) -> str:
        """Re-run a monitoring test now, like "Test now" in the Drata UI (v1 API).

        ``test_id`` is the workspace-scoped ``testId`` shown by
        list_drata_monitoring_tests. The run is asynchronous: the new result
        is read later with get_drata_monitoring_test.
        """
        tid = _coerce_int(test_id)
        if tid is None or tid <= 0:
            return "Error: test_id (the workspace-scoped testId from list_drata_monitoring_tests) is required."
        ws = _coerce_int(workspace_id)
        if ws is None or ws <= 0:
            return "Error: workspace_id must be a positive Drata workspace ID."
        async with egress_client(timeout=30) as client:
            _resp, err = await _common._request_verbatim(
                client,
                "post",
                f"{base_url_v1}/workspaces/{ws}/autopilot/{tid}/retest",
                "retest monitoring test",
                required_permission=_RETEST_PERMISSION,
                headers=headers,
            )
        if err:
            return err
        return (
            f"Retest triggered for monitoring test {tid} in workspace {ws}. The run is asynchronous: "
            "call get_drata_monitoring_test later to read the new result."
        )

    callable_map = {
        "list_drata_monitoring_tests": list_drata_monitoring_tests,
        "get_drata_monitoring_test": get_drata_monitoring_test,
        "list_drata_monitoring_test_failures": list_drata_monitoring_test_failures,
        "list_drata_monitoring_test_exclusions": list_drata_monitoring_test_exclusions,
        "retest_drata_monitoring_test": retest_drata_monitoring_test,
    }
    for _name, _fn in callable_map.items():
        executor.register(_name, _fn)

    return [
        ToolDefinition(
            name="list_drata_monitoring_tests",
            description=(
                "List monitoring tests in a Drata workspace with each test's "
                "result (PASSED/FAILED/ERROR/...), system status, and last-passed "
                "time. Use the shown 'testId' for get/failures/exclusions."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "integer", "default": 1},
                    "result": {
                        "type": "string",
                        "description": ("Filter by result status: READY, PASSED, FAILED, ERROR, or PREAUDIT."),
                    },
                    "status": {
                        "type": "string",
                        "description": ("Filter by system status: UNUSED, NEW, ENABLED, DISABLED, or TESTING."),
                    },
                },
                "required": [],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="get_drata_monitoring_test",
            description=("Get details of a specific monitoring test (result, status, last-passed / failing-since)."),
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "integer", "default": 1},
                    "test_id": {
                        "type": "integer",
                        "description": (
                            "The workspace-scoped 'testId' from list_drata_monitoring_tests (not the account-wide id)."
                        ),
                    },
                },
                "required": ["test_id"],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="list_drata_monitoring_test_failures",
            description="List recent failures for a monitoring test.",
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "integer", "default": 1},
                    "test_id": {"type": "integer"},
                },
                "required": ["test_id"],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="list_drata_monitoring_test_exclusions",
            description="List exclusions configured for a monitoring test.",
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "integer", "default": 1},
                    "test_id": {"type": "integer"},
                },
                "required": ["test_id"],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="retest_drata_monitoring_test",
            description=(
                "Re-run a Drata monitoring test now (the 'Test now' action), e.g. after fixing what "
                "made it fail. The run is asynchronous: read the new result later with "
                "get_drata_monitoring_test. Requires the 'Monitoring: Trigger Monitor Test' API key "
                "permission."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "integer", "default": 1},
                    "test_id": {
                        "type": "integer",
                        "description": (
                            "The workspace-scoped 'testId' from list_drata_monitoring_tests (not the account-wide id)."
                        ),
                    },
                },
                "required": ["test_id"],
            },
            source="integration:drata",
            priority=2,
        ),
    ]
