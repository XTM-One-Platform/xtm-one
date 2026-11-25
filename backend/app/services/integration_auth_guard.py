# SPDX-License-Identifier: Apache-2.0
"""Integration auth failures of one assignment run: recorded, and a rejected
credential is not called again (#1454).

A run whose integration calls fail authentication used to end ``completed``
with nothing saying so, and kept calling the integration that had rejected
its credential from every tool it held: a blind run produced a full answer
from its static context and spent hundreds of thousands of tokens doing it.

:class:`IntegrationAuthGuard` is the run's record. :class:`AuthGuardedExecutor`
wraps the executor of the run's integration tools and, per call:

* an integration whose credential was REJECTED (401 class: the token is
  unknown, expired or revoked) in this run is not called again: the call is
  refused at once with a message that says why, so the model stops retrying
  across the integration's tools and the spend of a blind run is bounded;
* a 403 (the credential works, this endpoint is not allowed) is recorded
  with the tool, and the integration's other tools stay callable: the run
  completes its readable part. An integration that answered 403 on
  :data:`FORBIDDEN_FAIL_FAST` distinct tools without one successful call is
  unreadable too: its further calls are refused like a rejected one (#1589).

A run in which no called integration was readable is
:data:`UNREADABLE_STOP_REASON`: the engine writes it on ``stop_reason`` so the
run list, ``get_run`` and the CTEM cards show it instead of a plain success.

At the end of the run the engine writes :meth:`IntegrationAuthGuard.failures`
on ``AssignmentRun.integration_auth_failures`` and appends
:meth:`IntegrationAuthGuard.coverage_notice` to the result, so a run without
the data it needed is never passed off as a plain success. A run whose
integrations all answered leaves both empty: nothing changes for it.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.tools.base import ToolCall, ToolDefinition, ToolResult, is_auth_failure_result

#: Prefix of ``ToolDefinition.source`` for an integration tool.
INTEGRATION_SOURCE_PREFIX = "integration:"

_FORBIDDEN_RE = re.compile(r"\b403\b|\bforbidden\b|\bpermission denied\b|\binsufficient (?:scope|permission)", re.I)

STATUS_REJECTED = "401"
STATUS_FORBIDDEN = "403"

#: Distinct tools answering 403 with no successful call before the integration
#: counts as unreadable and is no longer called in the run.
FORBIDDEN_FAIL_FAST = 3

#: ``AssignmentRun.stop_reason`` of a run in which no called integration was readable.
UNREADABLE_STOP_REASON = "integrations_unreadable"

#: Ref of the friction a recorded auth failure is filed under, already disposed
#: of: the run reports the failure on ``integration_auth_failures`` (#2204).
AUTH_FAILURE_RECORDED_REF = "integration auth failure recorded on the run"


def auth_failure_status(content: str) -> str:
    """``"403"`` for a call the credential may not make, ``"401"`` for a credential rejected."""
    return STATUS_FORBIDDEN if _FORBIDDEN_RE.search(content or "") else STATUS_REJECTED


class IntegrationAuthGuard:
    """The auth failures of one run, per integration, in the order they came."""

    def __init__(self) -> None:
        self._failures: dict[str, dict[str, Any]] = {}
        self._called: set[str] = set()
        self._answered: set[str] = set()
        self._refused_calls = 0

    def note_call(self, integration: str) -> None:
        self._called.add(integration)

    def note_answer(self, integration: str) -> None:
        """A call of *integration* answered without an auth failure."""
        self._answered.add(integration)

    def record(self, integration: str, tool: str, content: str) -> None:
        status = auth_failure_status(content)
        entry = self._failures.get(integration)
        if entry is None:
            entry = {"integration": integration, "status": status, "first_tool": tool, "tools": []}
            self._failures[integration] = entry
        elif status == STATUS_REJECTED:
            # A rejected credential outranks a forbidden endpoint: the
            # integration as a whole is unreadable from here on.
            entry["status"] = STATUS_REJECTED
        if tool not in entry["tools"]:
            entry["tools"].append(tool)

    def _all_forbidden(self, integration: str) -> bool:
        """403 on every call of *integration* so far: none of its reads succeeded."""
        entry = self._failures.get(integration)
        return entry is not None and entry["status"] == STATUS_FORBIDDEN and integration not in self._answered

    def rejected(self, integration: str) -> bool:
        """Whether *integration* is not called again in this run."""
        entry = self._failures.get(integration)
        if entry is None:
            return False
        if entry["status"] == STATUS_REJECTED:
            return True
        return self._all_forbidden(integration) and len(entry["tools"]) >= FORBIDDEN_FAIL_FAST

    def failures_of(self, integration: str) -> str | None:
        """The recorded status of *integration* (``"401"`` / ``"403"``), ``None`` when it never failed."""
        entry = self._failures.get(integration)
        return entry["status"] if entry else None

    def refuse(self) -> None:
        self._refused_calls += 1

    @property
    def refused_calls(self) -> int:
        """Calls refused without being sent because their credential was rejected."""
        return self._refused_calls

    def failures(self) -> list[dict[str, Any]]:
        """``[{integration, status, first_tool, tools, readable}]``; empty when none failed.

        ``readable`` is false for a rejected credential and for an integration
        whose every call answered 403.
        """
        return [
            dict(
                entry,
                tools=list(entry["tools"]),
                readable=entry["status"] == STATUS_FORBIDDEN and not self._all_forbidden(name),
            )
            for name, entry in self._failures.items()
        ]

    def coverage(self) -> dict[str, list[str]]:
        """Which integrations the run could read.

        ``readable`` (no auth failure), ``partial`` (403 on some tools, others
        answered), ``forbidden`` (403 on every call) and ``rejected`` (401).
        """
        rejected = sorted(k for k, v in self._failures.items() if v["status"] == STATUS_REJECTED)
        forbidden = sorted(k for k in self._failures if self._all_forbidden(k))
        partial = sorted(k for k, v in self._failures.items() if v["status"] == STATUS_FORBIDDEN and k not in forbidden)
        readable = sorted(self._called - set(self._failures))
        return {"readable": readable, "partial": partial, "forbidden": forbidden, "rejected": rejected}

    def unreadable(self) -> bool:
        """An auth failure, and no integration the run called could be read."""
        if not self._failures:
            return False
        cov = self.coverage()
        return not cov["readable"] and not cov["partial"]

    def coverage_notice(self) -> str:
        """What the result must say about the data it had; empty when nothing failed."""
        if not self._failures:
            return ""
        cov = self.coverage()
        lines = ["**Integration coverage: degraded.** This result was produced without some of its data."]
        if cov["rejected"]:
            lines.append("- Credential rejected (401), no data read: " + ", ".join(cov["rejected"]) + ".")
        if cov["forbidden"]:
            lines.append("- Forbidden (403) on every call, no data read: " + ", ".join(cov["forbidden"]) + ".")
        for name in cov["partial"]:
            tools = ", ".join(self._failures[name]["tools"])
            lines.append(f"- {name}: forbidden (403) on {tools}; its other reads succeeded.")
        lines.append("- Readable: " + (", ".join(cov["readable"]) if cov["readable"] else "none") + ".")
        lines.append("Re-authorize the integration(s) above, then run it again before relying on it.")
        return "\n".join(lines)

    def coverage_record(self) -> dict[str, Any]:
        """The machine-readable coverage of the run: ``degraded``, ``unreadable`` and :meth:`coverage`."""
        return {"degraded": bool(self._failures), "unreadable": self.unreadable(), **self.coverage()}


def with_coverage_key(result: str | None, record: dict[str, Any]) -> str | None:
    """*result* (a JSON object) with ``_coverage`` set to *record*.

    A JSON-output agent's result is parsed by its readers, so the coverage goes
    in a key instead of a prose notice. ``None`` when *result* is not a JSON
    object: the caller keeps it as it came (the run record still carries the
    failures and the stop reason).
    """
    if not result:
        return None
    try:
        parsed = json.loads(result)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    parsed["_coverage"] = record
    return json.dumps(parsed, ensure_ascii=False)


def auth_failures_line(failures: list[dict[str, Any]] | None) -> str:
    """One line naming a run's integration auth failures, for a run list or detail."""
    if not failures:
        return ""
    parts = []
    for f in failures:
        if not isinstance(f, dict):
            continue
        tools = ", ".join(str(t) for t in (f.get("tools") or []))
        state = "readable in part" if f.get("readable") else "no data read"
        parts.append(f"{f.get('integration', '?')} {f.get('status', '?')} ({state}; {tools})")
    return "Integration auth failures: " + "; ".join(parts) if parts else ""


class AuthGuardedExecutor:
    """The run's integration executor behind its :class:`IntegrationAuthGuard`.

    Everything but ``execute_tool`` is the inner executor's (``refusals``,
    ``on_composite``, ``file_context``, the delegation bookkeeping), so the
    composite and the run's teardown see the same executor as before.
    """

    def __init__(self, inner: Any, definitions: list[ToolDefinition], guard: IntegrationAuthGuard) -> None:
        self.__dict__["_inner"] = inner
        self.__dict__["_guard"] = guard
        self.__dict__["_integration_of"] = {
            d.name: d.source[len(INTEGRATION_SOURCE_PREFIX) :]
            for d in definitions
            if isinstance(d.source, str) and d.source.startswith(INTEGRATION_SOURCE_PREFIX)
        }

    def __getattr__(self, name: str) -> Any:
        return getattr(self.__dict__["_inner"], name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self.__dict__["_inner"], name, value)

    async def execute_tool(self, call: ToolCall) -> ToolResult:
        integration = self._integration_of.get(call.name)
        guard: IntegrationAuthGuard = self._guard
        inner = self._inner
        if integration is None:
            passed: ToolResult = await inner.execute_tool(call)
            return passed
        if guard.rejected(integration):
            guard.refuse()
            why = (
                "rejected this run's credential (authentication failed)"
                if guard.failures_of(integration) == STATUS_REJECTED
                else f"answered 403 Forbidden on {FORBIDDEN_FAIL_FAST} tools with no successful read"
            )
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"Error: not called. The {integration} integration {why} earlier in this run, "
                    "so none of its tools can read anything until it is re-authorized. Do not retry "
                    f"its tools: say in your answer that {integration} data was unavailable, and do "
                    "not infer it."
                ),
                is_error=True,
                refused=True,
                auth_failure_recorded=f"{integration} {guard.failures_of(integration)}",
            )
        guard.note_call(integration)
        result: ToolResult = await inner.execute_tool(call)
        content = result.content if isinstance(result.content, str) else ""
        if content and is_auth_failure_result(content):
            guard.record(integration, call.name, content)
            # Recorded on the run (integration_auth_failures, coverage notice):
            # the loop files its friction already disposed of (#2204).
            result.auth_failure_recorded = f"{integration} {guard.failures_of(integration)}"
        else:
            guard.note_answer(integration)
        return result
