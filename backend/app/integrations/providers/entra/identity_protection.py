"""Microsoft Entra ID - Identity Protection (risky users, detections, remediation).

The Identity Protection APIs require an Entra ID P2 license. They expose the
risk Entra ID assigns to identities (offline ML + threat intelligence) and the
manual remediation actions an analyst takes: confirm a user compromised (raise
to high risk) or dismiss the risk (clear it).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import EntraClient, dt, fmt_list, odata_quote, trunc

_RISK_ICONS = {"high": "[HIGH]", "medium": "[MED]", "low": "[LOW]", "none": "[none]"}


def _risk(level: str | None) -> str:
    return _RISK_ICONS.get((level or "").lower(), "[?]")


def _fmt_risky_user(u: dict[str, Any]) -> str:
    return (
        f"- {_risk(u.get('riskLevel'))} {u.get('userDisplayName', 'Unknown')} "
        f"<{u.get('userPrincipalName', '?')}>\n"
        f"  userId: {u.get('id', '?')} | state: {u.get('riskState', '?')} | "
        f"detail: {u.get('riskDetail', '?')} | updated: "
        f"{dt(u.get('riskLastUpdatedDateTime'))}"
    )


def _fmt_detection(d: dict[str, Any]) -> str:
    return (
        f"- {_risk(d.get('riskLevel'))} {d.get('riskEventType', '?')} "
        f"({d.get('riskState', '?')})\n"
        f"  user: {d.get('userPrincipalName', '?')} | activity: "
        f"{dt(d.get('activityDateTime'))} | IP: {d.get('ipAddress', '?')} | "
        f"detection: {trunc(d.get('detectionTimingType'), 40)}"
    )


def register(
    client: EntraClient, executor: CallableToolExecutor
) -> list[ToolDefinition]:
    src = "integration:entra"

    async def list_entra_risky_users(
        risk_level: str = "", risk_state: str = "", top: int = 25, skiptoken: str = ""
    ) -> str:
        filters: list[str] = []
        if risk_level:
            filters.append(f"riskLevel eq '{odata_quote(risk_level)}'")
        if risk_state:
            filters.append(f"riskState eq '{odata_quote(risk_state)}'")
        params: dict[str, Any] = {}
        if filters:
            params["$filter"] = " and ".join(filters)
        if skiptoken:
            params["$skiptoken"] = skiptoken
        items, err, next_tok = await client.paged(
            "/identityProtection/riskyUsers",
            "list risky users",
            top=min(top, 100),
            params=params,
        )
        if err:
            return err
        return fmt_list(
            items,
            _fmt_risky_user,
            noun="risky user",
            skiptoken=next_tok,
            empty_msg="No risky users (requires Entra ID P2).",
        )

    async def get_entra_risky_user_history(
        user_id: str, top: int = 25, skiptoken: str = ""
    ) -> str:
        items, err, next_tok = await client.paged(
            f"/identityProtection/riskyUsers/{quote(user_id, safe='')}/history",
            "get risky user history",
            top=min(top, 100),
            params={"$skiptoken": skiptoken} if skiptoken else None,
        )
        if err:
            return err

        def _fmt_hist(h: dict[str, Any]) -> str:
            return (
                f"- {dt(h.get('riskLastUpdatedDateTime'))} | "
                f"{_risk(h.get('riskLevel'))} {h.get('riskState', '?')} | "
                f"{h.get('riskDetail', '?')} | activity: "
                f"{h.get('activity', {}).get('riskEventTypes', '-')}"
            )

        return fmt_list(
            items, _fmt_hist, noun="history entry", skiptoken=next_tok
        )

    async def list_entra_risk_detections(
        risk_level: str = "", top: int = 25, skiptoken: str = ""
    ) -> str:
        params: dict[str, Any] = {}
        if risk_level:
            params["$filter"] = f"riskLevel eq '{odata_quote(risk_level)}'"
        if skiptoken:
            params["$skiptoken"] = skiptoken
        items, err, next_tok = await client.paged(
            "/identityProtection/riskDetections",
            "list risk detections",
            top=min(top, 100),
            params=params,
        )
        if err:
            return err
        return fmt_list(
            items,
            _fmt_detection,
            noun="risk detection",
            skiptoken=next_tok,
            empty_msg="No risk detections (requires Entra ID P2).",
        )

    async def confirm_entra_users_compromised(user_ids: list[str]) -> str:
        if not user_ids:
            return "Error: user_ids is required (list of risky user object ids)."
        _, err = await client.request(
            "post",
            "/identityProtection/riskyUsers/confirmCompromised",
            "confirm users compromised",
            json={"userIds": user_ids},
        )
        if err:
            return err
        return (
            f"Confirmed {len(user_ids)} user(s) as compromised (risk set to high). "
            f"Conditional Access risk policies will now treat them accordingly."
        )

    async def dismiss_entra_risky_users(user_ids: list[str]) -> str:
        if not user_ids:
            return "Error: user_ids is required (list of risky user object ids)."
        _, err = await client.request(
            "post",
            "/identityProtection/riskyUsers/dismiss",
            "dismiss risky users",
            json={"userIds": user_ids},
        )
        if err:
            return err
        return f"Dismissed risk for {len(user_ids)} user(s) (risk set to none)."

    handlers = {
        "list_entra_risky_users": list_entra_risky_users,
        "get_entra_risky_user_history": get_entra_risky_user_history,
        "list_entra_risk_detections": list_entra_risk_detections,
        "confirm_entra_users_compromised": confirm_entra_users_compromised,
        "dismiss_entra_risky_users": dismiss_entra_risky_users,
    }
    for name, fn in handlers.items():
        executor.register(name, fn)

    return [
        ToolDefinition(
            name="list_entra_risky_users",
            description=(
                "List users Entra ID Identity Protection flags as risky. Filter by "
                "risk_level (low/medium/high) and risk_state (atRisk, "
                "confirmedCompromised, dismissed, remediated, ...). Returns the "
                "userId, risk level/state/detail - the starting point for an account-"
                "compromise investigation. Requires Entra ID P2."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "risk_level": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                    },
                    "risk_state": {
                        "type": "string",
                        "description": "e.g. atRisk, confirmedCompromised, dismissed, remediated.",
                    },
                    "top": {"type": "integer", "default": 25},
                    "skiptoken": {"type": "string"},
                },
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="get_entra_risky_user_history",
            description="Get the risk history (state transitions over time) for one risky user by object id.",
            input_schema={
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "Risky user object id."},
                    "top": {"type": "integer", "default": 25},
                    "skiptoken": {
                        "type": "string",
                        "description": "Pagination token from a previous call's 'More results' hint.",
                    },
                },
                "required": ["user_id"],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="list_entra_risk_detections",
            description=(
                "List individual risk detections (anonymous IP, impossible travel, "
                "leaked credentials, malware-linked IP, etc.) raised by Identity "
                "Protection. Filter by risk_level. Requires Entra ID P2."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "risk_level": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                    },
                    "top": {"type": "integer", "default": 25},
                    "skiptoken": {"type": "string"},
                },
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="confirm_entra_users_compromised",
            description=(
                "Confirm one or more users as compromised, raising their risk to "
                "high so risk-based Conditional Access policies act on them. Use after "
                "validating an account takeover. Provide the risky user object ids. "
                "Requires IdentityRiskyUser.ReadWrite.All (Entra ID P2)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "user_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Risky user object ids to confirm as compromised.",
                    },
                },
                "required": ["user_ids"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="dismiss_entra_risky_users",
            description=(
                "Dismiss the risk on one or more users (sets risk to none) - the "
                "false-positive / remediated outcome. Provide the risky user object "
                "ids. Requires IdentityRiskyUser.ReadWrite.All (Entra ID P2)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "user_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Risky user object ids to dismiss.",
                    },
                },
                "required": ["user_ids"],
            },
            source=src,
            priority=1,
        ),
    ]
