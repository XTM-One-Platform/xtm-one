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
  completes its readable part.

At the end of the run the engine writes :meth:`IntegrationAuthGuard.failures`
on ``AssignmentRun.integration_auth_failures`` and appends
:meth:`IntegrationAuthGuard.coverage_notice` to the result, so a run without
the data it needed is never passed off as a plain success. A run whose
integrations all answered leaves both empty: nothing changes for it.
"""

from __future__ import annotations

import re
from typing import Any

from app.tools.base import ToolCall, ToolDefinition, ToolResult, is_auth_failure_result

#: Prefix of ``ToolDefinition.source`` for an integration tool.
INTEGRATION_SOURCE_PREFIX = "integration:"

_FORBIDDEN_RE = re.compile(r"\b403\b|\bforbidden\b|\bpermission denied\b|\binsufficient (?:scope|permission)", re.I)

STATUS_REJECTED = "401"
STATUS_FORBIDDEN = "403"


def auth_failure_status(content: str) -> str:
    """``"403"`` for a call the credential may not make, ``"401"`` for a credential rejected."""
    return STATUS_FORBIDDEN if _FORBIDDEN_RE.search(content or "") else STATUS_REJECTED


class IntegrationAuthGuard:
    """The auth failures of one run, per integration, in the order they came."""

    def __init__(self) -> None:
        self._failures: dict[str, dict[str, Any]] = {}
        self._called: set[str] = set()
        self._refused_calls = 0

    def note_call(self, integration: str) -> None:
        self._called.add(integration)

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

    def rejected(self, integration: str) -> bool:
        entry = self._failures.get(integration)
        return entry is not None and entry["status"] == STATUS_REJECTED

    def refuse(self) -> None:
        self._refused_calls += 1

    @property
    def refused_calls(self) -> int:
        """Calls refused without being sent because their credential was rejected."""
        return self._refused_calls

    def failures(self) -> list[dict[str, Any]]:
        """``[{integration, status, first_tool, tools}]``; empty when none failed."""
        return [dict(entry, tools=list(entry["tools"])) for entry in self._failures.values()]

    def coverage(self) -> dict[str, list[str]]:
        """Which integrations the run could read: ``readable``, ``partial`` (403 on some tools), ``rejected``."""
        rejected = sorted(k for k, v in self._failures.items() if v["status"] == STATUS_REJECTED)
        partial = sorted(k for k, v in self._failures.items() if v["status"] == STATUS_FORBIDDEN)
        readable = sorted(self._called - set(self._failures))
        return {"readable": readable, "partial": partial, "rejected": rejected}

    def coverage_notice(self) -> str:
        """What the result must say about the data it had; empty when nothing failed."""
        if not self._failures:
            return ""
        cov = self.coverage()
        lines = ["**Integration coverage: degraded.** This result was produced without some of its data."]
        if cov["rejected"]:
            lines.append("- Credential rejected (401), no data read: " + ", ".join(cov["rejected"]) + ".")
        for name in cov["partial"]:
            tools = ", ".join(self._failures[name]["tools"])
            lines.append(f"- {name}: forbidden (403) on {tools}; its other reads succeeded.")
        lines.append("- Readable: " + (", ".join(cov["readable"]) if cov["readable"] else "none") + ".")
        lines.append("Re-authorize the integration(s) above, then run it again before relying on it.")
        return "\n".join(lines)


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
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"Error: not called. The {integration} integration rejected this run's credential "
                    "(authentication failed) earlier in this run, so none of its tools can read "
                    "anything until it is re-authorized. Do not retry its tools: say in your answer "
                    f"that {integration} data was unavailable, and do not infer it."
                ),
                is_error=True,
                refused=True,
            )
        guard.note_call(integration)
        result: ToolResult = await inner.execute_tool(call)
        content = result.content if isinstance(result.content, str) else ""
        if content and is_auth_failure_result(content):
            guard.record(integration, call.name, content)
        return result
