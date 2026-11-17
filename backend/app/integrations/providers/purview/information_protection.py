"""Microsoft Purview Information Protection - sensitivity labels.

Sensitivity labels (``/security/dataSecurityAndGovernance/sensitivityLabels``)
classify and protect content across Microsoft 365 (email, files, Teams, and
containers). This surface lists the labels available to the tenant and reads a
single label's configuration (priority, protection, sublabels). Requires
``SensitivityLabels.Read.All`` (supports app-only and delegated).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import PurviewClient, fmt_list, trunc

SRC = "integration:purview"
_LABELS = "/security/dataSecurityAndGovernance/sensitivityLabels"


def _fmt_label(label: dict[str, Any]) -> str:
    enabled = "enabled" if label.get("isEnabled") else "disabled"
    sublabels = label.get("sublabels") or []
    sub = f" | {len(sublabels)} sublabel(s)" if sublabels else ""
    applies = label.get("applicableTo")
    applies_str = f" | applies to: {applies}" if applies else ""
    return (
        f"- {label.get('name', 'Unnamed')} ({enabled}, priority "
        f"{label.get('priority', '?')}){sub}{applies_str}\n"
        f"  id: {label.get('id', '?')} | {trunc(label.get('description'), 140)}"
    )


def register(
    client: PurviewClient, executor: CallableToolExecutor
) -> list[ToolDefinition]:
    async def list_purview_sensitivity_labels(top: int = 50, skiptoken: str = "") -> str:
        params: dict[str, Any] = {}
        if skiptoken:
            params["$skiptoken"] = skiptoken
        items, err, next_tok = await client.graph_paged(
            _LABELS, "list sensitivity labels", top=min(top, 100), params=params
        )
        if err:
            return err
        return fmt_list(
            items,
            _fmt_label,
            noun="sensitivity label",
            skiptoken=next_tok,
            empty_msg="No sensitivity labels are published to this tenant.",
        )

    async def get_purview_sensitivity_label(label_id: str) -> str:
        data, err = await client.graph_get_json(
            f"{_LABELS}/{quote(label_id.strip(), safe='')}",
            "get sensitivity label",
        )
        if err:
            return err
        return _fmt_label(data or {})

    executor.register(
        "list_purview_sensitivity_labels", list_purview_sensitivity_labels
    )
    executor.register("get_purview_sensitivity_label", get_purview_sensitivity_label)

    return [
        ToolDefinition(
            name="list_purview_sensitivity_labels",
            description=(
                "List the Microsoft Purview Information Protection sensitivity "
                "labels available to the tenant (e.g. Public, General, "
                "Confidential, Highly Confidential). Returns each label's name, "
                "priority, enabled state, what it applies to (email/file/teamwork), "
                "and id. Use to understand the organization's data classification "
                "taxonomy."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "top": {"type": "integer", "default": 50},
                    "skiptoken": {"type": "string"},
                },
            },
            source=SRC,
            priority=0,
        ),
        ToolDefinition(
            name="get_purview_sensitivity_label",
            description=(
                "Get one sensitivity label by id, including its description, "
                "priority, protection settings, and sublabels. Use "
                "list_purview_sensitivity_labels first to find the id."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "label_id": {"type": "string", "description": "The sensitivity label id."}
                },
                "required": ["label_id"],
            },
            source=SRC,
            priority=0,
        ),
    ]
