# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""The enriched CTEM case read of the ``ctem`` group (#1337, #2443).

``get_ctem_case`` of the ``ctem`` group answers, on top of the case tools'
read, who opened the case (``opened_by``, the actor of its ``created`` event)
and why each empty section is empty (``empty_sections``), so an agent reports
a blank as a fact, not a guess. It acts for the person the turn or run acts
for: a case renders through that person's own source read
(``ctem_cases_view``), so a case they see none of does not exist.

The former ``ctem_read`` group is a [DEPRECATED] id of ``ctem``
(``RENAMED_BUILTIN_GROUPS``); its inventory read ``list_ctem_assets`` is
replaced by the organization inventory functions (``search_assets``).
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime
from typing import Any

from app.tools.base import ToolDefinition

#: History events one ``get_ctem_case`` answer carries (as the case tools).
_HISTORY_PAGE = 20


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def opened_by(events: list[Any]) -> dict[str, Any] | None:
    """Who opened the case: the actor and time of its oldest ``created`` event, ``None`` without one."""
    created = [e for e in events if getattr(e, "kind", None) == "created" and getattr(e, "at", None) is not None]
    if not created:
        return None
    first = min(created, key=lambda e: e.at)
    return {"actor": first.actor, "at": first.at.isoformat(), "how": first.to_value}


def empty_sections(
    rendered: dict[str, Any],
    running_versions: Any,
    inventory_sources: list[str] | None = None,
    software_assets: int = 0,
) -> dict[str, str]:
    """Why each empty section of a rendered case is empty, keyed by the section.

    *inventory_sources*, the imports the asset software came from, are named when an
    upgrade's product runs on no asset: the loop then reads the deployment or asks the
    asset owner with those sources listed, never asks what they already answered (#1736).
    *software_assets*, the assets the organization software row of the case's product
    counts: when it is not zero the read failed to match them, which is said (#2450).
    """
    reasons: dict[str, str] = {}
    if not rendered.get("exploitation"):
        reasons["exploitation"] = "no exploitation reading recorded on this case yet"
    if rendered.get("attack_path") is None:
        reasons["attack_path"] = "the attack-path reach of this case has not been read yet"
    if rendered.get("treatment_plan") is None:
        reasons["treatment_plan"] = "no treatment plan attached to this case"
    if rendered.get("threat_package") is None:
        reasons["threat_package"] = "no threat package drafted for this case"
    if not rendered.get("tickets"):
        reasons["tickets"] = "no ticket recorded on this case"
    if not rendered.get("validations"):
        reasons["validations"] = "no validation recorded on this case"
    if running_versions is None:
        if rendered.get("remediation_kind") != "upgrade":
            reasons["running_versions"] = "only an upgrade case has running versions"
        elif software_assets > 0:
            reasons["running_versions"] = (
                f"the running versions could not be matched: the organization software of this product counts "
                f"{software_assets} asset(s), but none of its inventory rows matched the case's product. "
                "The versions are unread, not absent: never conclude that no asset runs it"
            )
        elif inventory_sources:
            reasons["running_versions"] = (
                "no asset of the inventory runs this product; inventory sources checked: "
                + ", ".join(inventory_sources)
                + ". Read the deployment (image tags, Helm values) before asking the asset owner,"
                " and list these sources in the ask"
            )
        else:
            reasons["running_versions"] = "no running version read for this upgrade"
    if not rendered.get("history"):
        reasons["history"] = "no event recorded on this case"
    if rendered.get("details_unavailable"):
        reasons["issues"] = "these source products did not answer for you: " + ", ".join(
            rendered["details_unavailable"]
        )
    return reasons


def build_case_read(*, user_id: str, agent_id: str) -> tuple[ToolDefinition, Any]:
    """The enriched ``get_ctem_case`` of the ``ctem`` group, acting for *user_id*: its definition and handler."""
    from .ctem_cases import _bounded_issues, _uuid, build_ctem_case_tools, missing_case_answer, resolve_case_id

    case_defs, _case_exec = build_ctem_case_tools(user_id=user_id, agent_id=agent_id)
    scope = _uuid(user_id)

    async def _get(case_id: str = "", issues_offset: int = 0) -> str:
        from app.core.database import async_session
        from app.models.user import User
        from app.services import ctem_case_versions as case_versions
        from app.services import ctem_cases_store as store
        from app.services import ctem_cases_view as view_of
        from app.services.ctem_verticals import normalize_vertical
        from app.services.organizations import get_primary

        if not str(case_id or "").strip():
            return "case_id must be the id list_ctem_cases answers."
        if scope is None:
            return f"No case {case_id}."
        try:
            async with async_session() as db:
                primary = await get_primary(db)
                if primary is None:
                    return f"No case {case_id}."
                # A full id or a unique 8+ character prefix (#2103), as the case tools.
                wanted, why = await resolve_case_id(db, case_id, primary.id)
                if wanted is None:
                    return why
                user = await db.get(User, scope)
                vertical = normalize_vertical(getattr(user, "ctem_vertical", None))
                found = await store.get_case(db, wanted, organization_id=primary.id)
                if found is None:
                    from app.services.ctem_case_delete import deleted_case_answer

                    # A deleted case says so, from its audit row (#2328); a merged-away case
                    # answers its survivor (#2289): never a bare miss.
                    return await deleted_case_answer(db, wanted) or await missing_case_answer(
                        db, case_id, wanted, primary.id
                    )
                case, rows, events = found
                # This case's members only (#2092): never a whole view build on the call during a drain.
                members = [f"{r.source}:{r.external_id}" for r in rows]
                view = await view_of.viewer_view(db, str(scope), vertical, members=members)
                # Every section from the whole history, only the served page cut (#2429).
                rendered = view_of.hydrate(case, rows, events, view, history_page=_HISTORY_PAGE)
                if rendered is None:
                    return f"No case {case_id}."
                from app.services import ctem_case_version_history as version_history

                # The list's asset-count rule on the single read (#1704): list and case read agree.
                version_history.with_asset_count(rendered, events)
                # The detection desk's gaps from the whole history (#2393), as the case tools answer them.
                rendered["detection_gaps"] = view_of.detection_gaps_of(events)
                running = await case_versions.for_case(db, case)
                if running is not None:
                    # Why the versions moved, per device (#1694), as the case tools' read: the drift is explained.
                    running = {**running, "history": version_history.history_of(events)}
                # The asset count agrees with the running versions this read serves (#2225).
                version_history.with_live_asset_count(rendered, running, at=datetime.now(UTC))
                unread = running is None and rendered.get("remediation_kind") == "upgrade"
                sources = await case_versions.inventory_sources(db, primary.id) if unread else None
                # A software row that counts assets the case matched none of: the match failed (#2450).
                software_assets = (
                    await case_versions.software_asset_count(db, primary.id, case.product_label) if unread else 0
                )
                from .ctem_signals import case_signal_scope

                # A bounded page of issues, as the case tools (#2092, #2103): a 700-issue case never answers 150k characters.
                return _dump(
                    {
                        **_bounded_issues(rendered, issues_offset),
                        "running_versions": running,
                        "opened_by": opened_by(events),
                        "history_total": len(events),
                        "empty_sections": empty_sections(rendered, running, sources, software_assets),
                        "scope": await case_signal_scope(db, scope, view.signals, case_id=case.id),
                    }
                )
        except Exception as exc:
            from .ctem_cases import CASE_READ_FAILED, case_read_failed

            # A pool timeout answers busy, retry (#2561); any other failure the generic answer.
            return case_read_failed(exc, "CTEM case read failed", CASE_READ_FAILED)

    by_name = {d.name: d for d in case_defs}
    definition = dataclasses.replace(
        by_name["get_ctem_case"],
        description=(
            "Read one CTEM case: its fix, member issues (source product and id), assets, primary assets, "
            "latest history, who opened it, and why each empty section is empty."
        ),
        input_schema=by_name["get_ctem_case"].input_schema,
    )
    return definition, _get


__all__ = [
    "build_case_read",
    "empty_sections",
    "opened_by",
]
