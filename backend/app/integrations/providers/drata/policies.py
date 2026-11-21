"""Drata policies tools - Policies, versions, content, and per-user acknowledgements."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from app.core.egress import egress_client
from app.integrations.http import format_entity_list
from app.tools.base import CallableToolExecutor, ToolDefinition

from . import _common
from ._common import (
    _PROVIDER,
    _dedupe_assigned_policies,
    _fmt_assigned_policy,
    _full_name,
)

# Window of policy text one call returns; offset / max_chars page the rest.
_POLICY_TEXT_DEFAULT_CHARS = 10_000
_POLICY_TEXT_MAX_CHARS = 50_000


def _policy_text_window(version_id: int, text: str, offset: int, max_chars: int) -> str:
    """One window of a policy text, with its range, total and the next offset."""
    total = len(text)
    size = max_chars if max_chars > 0 else _POLICY_TEXT_DEFAULT_CHARS
    size = min(size, _POLICY_TEXT_MAX_CHARS)
    start = max(offset, 0)
    if total and start >= total:
        return f"Policy Version {version_id} Content: offset {start} is past the end of the text ({total} characters)."
    end = min(start + size, total)
    header = f"Policy Version {version_id} Content, characters {start} to {end} of {total}"
    if end < total:
        header += f"; {total - end} more: call again with offset={end}"
    return f"{header}:\n\n{text[start:end]}"


def register(
    base_url: str,
    headers: dict[str, str],
    upload_headers: dict[str, str],
    executor: CallableToolExecutor,
) -> list[ToolDefinition]:
    """Build the Drata policies tools and register their callables."""

    async def list_drata_policies(name: str = "", status: str = "") -> str:
        """List published policies from Drata. Optionally filter by name or status."""
        async with egress_client(timeout=30) as client:
            params: dict[str, Any] = {"expand[]": ["owner", "groups"]}
            if name:
                params["name"] = name
            if status:
                params["statuses[]"] = [status]
            items, err = await _common._paginated_fetch(client, f"{base_url}/policies", headers, params)
            if err:
                return err
            if not items:
                return "No policies found."
            lines: list[str] = []
            for p in items:
                owner_name = _full_name(p.get("owner"))
                lines.append(
                    f"[{p.get('id')}] {p.get('name', 'Untitled')} — "
                    f"Status: {p.get('status', 'N/A')} | "
                    f"Version: {p.get('version', '?')}.{p.get('subVersion', '0')} | "
                    f"Owner: {owner_name} | "
                    f"Renewal: {p.get('renewalDate', 'N/A')}"
                )
            return "\n".join(lines)

    async def get_drata_policy(policy_id: int) -> str:
        """Get a specific policy by ID with full details including controls and groups."""
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{base_url}/policies/{policy_id}",
                _PROVIDER,
                "get policy",
                headers=headers,
                params={"expand[]": ["owner", "groups", "controls"]},
            )
            if err:
                return err
            p = resp.json()

            owner_name = _full_name(p.get("owner"))

            lines = [
                f"Policy: {p.get('name', 'Untitled')} (ID: {p.get('id')})",
                f"Status: {p.get('status', 'N/A')}",
                f"Scope: {p.get('scope', 'N/A')}",
                f"Version: {p.get('version', '?')}.{p.get('subVersion', '0')}",
                f"Owner: {owner_name}",
                f"Published: {p.get('publishedAt', 'N/A')}",
                f"Approved: {p.get('approvedAt', 'N/A')}",
                f"Renewal: {p.get('renewalDate', 'N/A')}",
                f"Created: {p.get('createdAt', 'N/A')}",
            ]

            controls = p.get("controls", [])
            if controls:
                lines.append(f"\nLinked Controls ({len(controls)}):")
                for c in controls:
                    lines.append(
                        f"  [{c.get('code', '?')}] {c.get('name', 'Untitled')} — Ready: {c.get('isReady', 'N/A')}"
                    )

            groups = p.get("groups", [])
            if groups:
                lines.append(f"\nAssigned Groups ({len(groups)}):")
                for g in groups:
                    lines.append(f"  {g.get('name', 'Unnamed')} (source: {g.get('source', 'N/A')})")

            lines.extend(await _approval_lines(client, policy_id))
            return "\n".join(lines)

    async def _approval_lines(client: Any, policy_id: int) -> list[str]:
        """The approval workflow of a policy: the latest version's state and the approver tiers.

        The policy object carries only ``approvedAt``; who approves and whether a
        newer version waits for approval live in the policy versions and the
        approval configuration. A read that fails is named, never dropped.
        """
        out = ["\nApproval workflow:"]
        versions, verr = await _common._paginated_fetch(
            client, f"{base_url}/policies/{policy_id}/policy-versions", headers, {}
        )
        if verr:
            out.append(f"  Versions: not readable ({verr[:160]})")
        else:
            waiting = [v for v in versions if v.get("policyVersionStatus") == "NEEDS_APPROVAL"]
            unpublished = [v for v in versions if v.get("policyVersionStatus") == "APPROVED" and not v.get("current")]
            latest = max(versions, key=lambda v: v.get("id") or 0, default=None)
            if latest is not None:
                out.append(
                    f"  Latest version: {latest.get('version', '?')}.{latest.get('subVersion', '0')} "
                    f"(ID: {latest.get('id')}) status {latest.get('policyVersionStatus', 'N/A')}, "
                    f"current: {latest.get('current', False)}, approved: {latest.get('approvedAt') or 'not yet'}, "
                    f"updated: {latest.get('updatedAt', 'N/A')}"
                )
            out.append(f"  Versions awaiting approval: {len(waiting)}; approved but not published: {len(unpublished)}")
        resp, cerr = await _common.safe_request(  # type: ignore[attr-defined]  # patched here by the tests
            client,
            "get",
            f"{base_url}/policies/{policy_id}/approval-configuration",
            _PROVIDER,
            "get policy approval configuration",
            headers=headers,
        )
        if cerr or resp is None:
            out.append(f"  Approvers: not readable ({(cerr or 'no response')[:160]})")
            return out
        tiers = (resp.json() or {}).get("reviewGroups") or []
        if not tiers:
            out.append("  Approvers: no approval tier configured")
        for tier in sorted(tiers, key=lambda t: t.get("tier") or 0):
            approvers = (
                ", ".join(f"{_full_name(a)} <{a.get('email', '?')}>" for a in tier.get("approvers") or []) or "none"
            )
            out.append(
                f"  Tier {tier.get('tier', '?')} {tier.get('name', 'Unnamed')}: "
                f"{tier.get('consensusRule', '?')} of {approvers}, {tier.get('timeline', '?')} day(s)"
            )
        return out

    async def list_drata_policy_versions(policy_id: int) -> str:
        """List all versions of a specific policy."""
        async with egress_client(timeout=30) as client:
            items, err = await _common._paginated_fetch(
                client,
                f"{base_url}/policies/{policy_id}/policy-versions",
                headers,
                {"expand[]": ["owner"]},
            )
            if err:
                return err

            def _fmt(v: dict) -> str:
                owner_name = _full_name(v.get("owner"))
                parts = [
                    f"Version {v.get('version', '?')}.{v.get('subVersion', '0')} "
                    f"(ID: {v.get('id')}) — "
                    f"Status: {v.get('policyVersionStatus', 'N/A')} | "
                    f"Type: {v.get('type', 'N/A')} | "
                    f"Current: {v.get('current', False)} | "
                    f"Owner: {owner_name} | "
                    f"Published: {v.get('publishedAt', 'N/A')} | "
                    f"Approved: {v.get('approvedAt') or 'not yet'} | "
                    f"Updated: {v.get('updatedAt', 'N/A')}"
                ]
                desc = v.get("description", "")
                if desc:
                    parts.append(f"  Description: {desc[:200]}")
                changes = v.get("changesExplanation", "")
                if changes:
                    parts.append(f"  Changes: {changes[:200]}")
                return "\n".join(parts)

            return format_entity_list(
                items,
                _fmt,
                noun="policy version",
                empty_msg=f"No versions found for policy {policy_id}.",
            )

    async def get_drata_policy_version_content(
        policy_id: int, version_id: int, offset: int = 0, max_chars: int = _POLICY_TEXT_DEFAULT_CHARS
    ) -> str:
        """Get the text of a policy version, one window at a time.

        The answer names the range shown, the total length and the offset of
        the next window, so a policy of any length is read whole.
        """
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{base_url}/policies/{policy_id}/policy-versions/{version_id}",
                _PROVIDER,
                "get policy version content",
                headers={**headers, "Accept": "text/html"},
            )
            if err:
                return err

            content_type = resp.headers.get("content-type", "")
            if "text/html" in content_type:
                html = resp.text
                text = re.sub(r"<[^>]+>", "", html)
                text = re.sub(r"\s+", " ", text).strip()
                return _policy_text_window(version_id, text, offset, max_chars)

            data = resp.json()
            lines = [
                f"Policy Version {version_id}:",
                f"Status: {data.get('policyVersionStatus', 'N/A')}",
                f"Version: {data.get('version', '?')}.{data.get('subVersion', '0')}",
            ]
            desc = data.get("description", "")
            if desc:
                lines.append(f"Description: {desc}")
            return "\n".join(lines)

    async def acknowledge_drata_assigned_policy(
        user_id: int, policy_id: int, accepted_at: str = "", details: str = ""
    ) -> str:
        """Acknowledge (accept) a policy assigned to a user.

        ``policy_id`` is the policy ID (shown as ``[policy N]`` by
        list_drata_user_assigned_policies). ``accepted_at`` defaults to now and
        must be within the last year; ``details`` optionally records how/where
        it was accepted.
        """
        payload: dict[str, Any] = {
            "acceptedAt": (accepted_at or "").strip() or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        }
        if details:
            payload["details"] = details
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "post",
                f"{base_url}/users/{user_id}/assigned-policies/{policy_id}/action-acknowledge",
                _PROVIDER,
                "acknowledge assigned policy",
                headers=headers,
                json=payload,
            )
            if err:
                return err
            data = resp.json() if resp.content else {}
            return (
                f"Policy {policy_id} acknowledged for user {user_id}. "
                f"Accepted at: {data.get('acceptedAt', payload['acceptedAt'])}."
            )

    async def list_drata_user_assigned_policies(user_id: int) -> str:
        """List policies assigned to a user with their acceptance status.

        Rows are collapsed to one per policy (the current published version) and
        each shows a clear ACCEPTED / NOT ACCEPTED verdict driven solely by
        ``acceptedAt`` - a missing date is never masked as "no acknowledgment
        required". Use the ``[policy N]`` ID shown here with
        ``acknowledge_drata_assigned_policy`` to accept a policy.
        """
        async with egress_client(timeout=30) as client:
            items, err = await _common._paginated_fetch(
                client,
                f"{base_url}/users/{user_id}/assigned-policies",
                headers,
                {"expand[]": ["policy", "policyVersion"]},
            )
            if err:
                return err
            if not items:
                return f"No assigned policies found for user {user_id}."
            deduped, dropped = _dedupe_assigned_policies(items)
            accepted = sum(1 for ap in deduped if ap.get("acceptedAt"))
            pending = len(deduped) - accepted
            label = "policy" if len(deduped) == 1 else "policies"
            header = f"User {user_id} — {len(deduped)} assigned {label}: {accepted} accepted, {pending} not accepted."
            if dropped:
                row_word = "row" if dropped == 1 else "rows"
                header += (
                    f" (Collapsed {dropped} archived / superseded version "
                    f"{row_word}; showing the current version of each policy.)"
                )
            body = "\n".join(_fmt_assigned_policy(ap) for ap in deduped)
            return f"{header}\n{body}"

    callable_map = {
        "list_drata_policies": list_drata_policies,
        "get_drata_policy": get_drata_policy,
        "list_drata_policy_versions": list_drata_policy_versions,
        "get_drata_policy_version_content": get_drata_policy_version_content,
        "list_drata_user_assigned_policies": list_drata_user_assigned_policies,
        "acknowledge_drata_assigned_policy": acknowledge_drata_assigned_policy,
    }
    for _name, _fn in callable_map.items():
        executor.register(_name, _fn)

    return [
        ToolDefinition(
            name="list_drata_policies",
            description="List published policies from Drata with their status, version, and owner. Optionally filter by name or status (ACTIVE, ARCHIVED, REPLACED, UNACCEPTABLE, OUTDATED).",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Filter by name (partial)"},
                    "status": {
                        "type": "string",
                        "enum": [
                            "ACTIVE",
                            "ARCHIVED",
                            "REPLACED",
                            "UNACCEPTABLE",
                            "OUTDATED",
                        ],
                        "description": "Filter by policy status",
                    },
                },
                "required": [],
            },
            source="integration:drata",
            priority=0,
        ),
        ToolDefinition(
            name="get_drata_policy",
            description=(
                "Get a specific Drata policy by ID with linked controls, assigned groups and its "
                "approval workflow: the latest version's status and the approver tiers."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "policy_id": {"type": "integer", "description": "Drata policy ID"},
                },
                "required": ["policy_id"],
            },
            source="integration:drata",
            priority=1,
        ),
        ToolDefinition(
            name="list_drata_policy_versions",
            description="List all versions of a specific Drata policy with their status and approval date.",
            input_schema={
                "type": "object",
                "properties": {
                    "policy_id": {"type": "integer", "description": "Drata policy ID"},
                },
                "required": ["policy_id"],
            },
            source="integration:drata",
            priority=1,
        ),
        ToolDefinition(
            name="get_drata_policy_version_content",
            description=(
                "Get the text of a Drata policy version by window: the answer names "
                "the characters shown, the total and the offset of the next window."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "policy_id": {"type": "integer"},
                    "version_id": {"type": "integer"},
                    "offset": {
                        "type": "integer",
                        "description": "First character to return (default 0)",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Characters to return (default 10000, at most 50000)",
                    },
                },
                "required": ["policy_id", "version_id"],
            },
            source="integration:drata",
            priority=1,
        ),
        ToolDefinition(
            name="list_drata_user_assigned_policies",
            description=(
                "List policies assigned to a Drata user, one row per policy "
                "(current published version), each with a clear ACCEPTED (with "
                "date) / NOT ACCEPTED verdict. A missing acceptance is always "
                "NOT ACCEPTED - never 'no acknowledgment required'. For "
                "authoritative acknowledgment compliance, cross-check the "
                "policy's monitoring test."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "user_id": {"type": "integer", "description": "Drata user ID"},
                },
                "required": ["user_id"],
            },
            source="integration:drata",
            priority=1,
        ),
        ToolDefinition(
            name="acknowledge_drata_assigned_policy",
            description="Acknowledge a policy assigned to a user (mark it as accepted).",
            input_schema={
                "type": "object",
                "properties": {
                    "user_id": {"type": "integer", "description": "Drata user ID"},
                    "policy_id": {
                        "type": "integer",
                        "description": ("Policy ID (the '[policy N]' value from list_drata_user_assigned_policies)"),
                    },
                    "accepted_at": {
                        "type": "string",
                        "description": ("When the user accepted (ISO 8601, within the last year). Defaults to now."),
                    },
                    "details": {
                        "type": "string",
                        "description": "Optional note on how/where it was accepted",
                    },
                },
                "required": ["user_id", "policy_id"],
            },
            source="integration:drata",
            priority=2,
        ),
    ]
