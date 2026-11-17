"""Drata risks tools - Risk management: registers, risks, library, categories, notes, documents."""

from __future__ import annotations

from typing import Any

from app.core.egress import egress_client
from app.integrations.file_delivery import uploaded_file_answer
from app.integrations.http import format_entity_list
from app.tools.base import CallableToolExecutor, FileContext, ToolDefinition

from . import _common
from ._common import (
    _PROVIDER,
    _RISK_LIBRARY_GROUPS,
    _coerce_int,
    _empty,
    _full_name,
    _ids_from_csv,
    _int_csv,
    _render_evidence_file,
    _resolve_uploaded_evidence_file,
)


def register(
    base_url: str,
    headers: dict[str, str],
    upload_headers: dict[str, str],
    executor: CallableToolExecutor,
) -> list[ToolDefinition]:
    """Build the Drata risks tools and register their callables."""

    async def list_drata_risk_registers() -> str:
        """List Drata risk registers."""
        async with egress_client(timeout=30) as client:
            items, err = await _common._paginated_fetch(client, f"{base_url}/risk-registers", headers, {})
            if err:
                return err
            return format_entity_list(
                items,
                lambda r: f"[{r.get('id')}] {r.get('name', 'Unnamed')} — {r.get('description', '')[:200]}",
                noun="risk register",
            )

    async def list_drata_risk_categories(risk_register_id: int = 1) -> str:
        """List risk categories in a risk register."""
        async with egress_client(timeout=30) as client:
            items, err = await _common._paginated_fetch(
                client,
                f"{base_url}/risk-registers/{risk_register_id}/risk-categories",
                headers,
                {},
            )
            if err:
                return err
            return format_entity_list(
                items,
                lambda c: f"[{c.get('id')}] {c.get('name', 'Unnamed')}",
                noun="risk category",
            )

    async def create_drata_risk_category(risk_register_id: int, name: str) -> str:
        """Create a new risk category."""
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "post",
                f"{base_url}/risk-registers/{risk_register_id}/risk-categories",
                _PROVIDER,
                "create risk category",
                headers=headers,
                json={"name": name},
            )
            if err:
                return err
            data = resp.json() if resp.content else {}
            return f"Risk category '{name}' created (ID: {data.get('id', '?')})."

    async def delete_drata_risk_category(category_id: int, risk_register_id: int = 1) -> str:
        """Delete a risk category."""
        async with egress_client(timeout=30) as client:
            _, err = await _common.safe_request(
                client,
                "delete",
                f"{base_url}/risk-registers/{risk_register_id}/risk-categories/{category_id}",
                _PROVIDER,
                "delete risk category",
                headers=headers,
            )
            if err:
                return err
            return f"Risk category {category_id} deleted."

    async def list_drata_risks(
        status: str = "",
        treatment_plan: str = "",
        risk_register_id: int = 1,
        title: str = "",
        min_score: int = 0,
        max_score: int = 0,
        vendor_id: int = 0,
    ) -> str:
        """List risks from a risk register. Filter by status, treatment plan, title, score range, or vendor."""
        async with egress_client(timeout=30) as client:
            params: dict[str, Any] = {
                "expand[]": ["categories", "owners", "reviewers", "controls"],
            }
            if status:
                params["status"] = status
            if treatment_plan:
                params["treatmentPlan"] = treatment_plan
            if title:
                params["title"] = title
            if min_score:
                params["minScore"] = min_score
            if max_score:
                params["maxScore"] = max_score
            if vendor_id:
                params["vendorId"] = vendor_id
            items, err = await _common._paginated_fetch(
                client,
                f"{base_url}/risk-registers/{risk_register_id}/risks",
                headers,
                params,
                page_size=20,
            )
            if err:
                return err

            def _owner_str(o: dict) -> str:
                email = (o.get("email") or "").strip()
                name = _full_name(o)
                # _full_name falls back to the email when no name is set; avoid
                # rendering a duplicated "user@x <user@x>" in that case.
                if email and name != email:
                    return f"{name} <{email}>"
                return name

            def _fmt(r: dict) -> str:
                owners = r.get("owners", [])
                owner_names = ", ".join(_owner_str(o) for o in owners) or "N/A"
                reviewers = r.get("reviewers", [])
                reviewer_names = ", ".join(_owner_str(o) for o in reviewers) or "N/A"
                categories = r.get("categories", [])
                cat_names = ", ".join(c.get("name", "") for c in categories) or "N/A"
                return (
                    f"[{r.get('riskId', '?')}] {r.get('title', 'Untitled')} "
                    f"(ID: {r.get('id')}) — "
                    f"Status: {r.get('status', 'N/A')} | "
                    f"Score: {r.get('score', 'N/A')} | "
                    f"Residual: {r.get('residualScore', 'N/A')} | "
                    f"Treatment: {r.get('treatmentPlan', 'N/A')} | "
                    f"Categories: {cat_names} | "
                    f"Owners: {owner_names} | "
                    f"Reviewers: {reviewer_names}"
                )

            return format_entity_list(items, _fmt, noun="risk")

    async def get_drata_risk(risk_id: int, risk_register_id: int = 1) -> str:
        """Get full details of a specific risk including controls, notes, tasks."""
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{base_url}/risk-registers/{risk_register_id}/risks/{risk_id}",
                _PROVIDER,
                "get risk",
                headers=headers,
                params={
                    "expand[]": [
                        "categories",
                        "controls",
                        "owners",
                        "reviewers",
                        "notes",
                        "tasks",
                        "documents",
                    ]
                },
            )
            if err:
                return err
            r = resp.json()

            lines = [
                f"Risk: [{r.get('riskId', '?')}] {r.get('title', 'Untitled')} (ID: {r.get('id')})",
                f"Description: {r.get('description', 'N/A')}",
                f"Status: {r.get('status', 'N/A')}",
                f"Score: {r.get('score', 'N/A')} (Residual: {r.get('residualScore', 'N/A')})",
                f"Treatment Plan: {r.get('treatmentPlan', 'N/A')}",
                f"Treatment Details: {r.get('treatmentDetails', 'N/A')}",
                f"Anticipated Completion: {r.get('anticipatedCompletionDate', 'N/A')}",
                f"Created: {r.get('createdAt', 'N/A')}",
            ]

            owners = r.get("owners", [])
            if owners:
                lines.append(f"\nOwners ({len(owners)}):")
                for o in owners:
                    lines.append(f"  {_full_name(o)} ({o.get('email', 'N/A')})")

            controls = r.get("controls", [])
            if controls:
                lines.append(f"\nLinked Controls ({len(controls)}):")
                for c in controls:
                    lines.append(f"  [{c.get('code', '?')}] {c.get('name', 'Untitled')}")

            categories = r.get("categories", [])
            if categories:
                lines.append(f"\nCategories: {', '.join(c.get('name', '') for c in categories)}")

            notes = r.get("notes", [])
            if notes:
                lines.append(f"\nNotes ({len(notes)}):")
                for n in notes:
                    lines.append(f"  [{n.get('createdAt', '?')}] {n.get('comment', '')[:200]}")

            tasks = r.get("tasks", [])
            if tasks:
                lines.append(f"\nTasks ({len(tasks)}):")
                for t in tasks:
                    done = "Done" if t.get("completedAt") else "Open"
                    lines.append(f"  [{done}] {t.get('title', 'Untitled')} — Due: {t.get('dueDate', 'N/A')}")

            return "\n".join(lines)

    async def create_drata_risk(
        title: str,
        description: str,
        risk_register_id: int = 1,
        impact: int = 3,
        likelihood: int = 3,
        treatment_plan: str = "UNTREATED",
        treatment_details: str = "",
        category_ids: str = "",
        owner_ids: str = "",
        reviewer_ids: str = "",
        control_ids: str = "",
        status: str = "ACTIVE",
        identified_at: str = "",
        anticipated_completion_date: str = "",
        completion_date: str = "",
        residual_impact: int = 0,
        residual_likelihood: int = 0,
    ) -> str:
        """Create a new risk in the risk register.

        ``category_ids`` / ``owner_ids`` / ``reviewer_ids`` / ``control_ids``
        are CSV lists of numeric Drata IDs (resolve user IDs via
        list_drata_users). ``owners`` are the people responsible for the risk;
        ``reviewers`` approve the treatment plan. Dates accept ``YYYY-MM-DD`` or
        full ISO-8601. ``treatment_details`` / ``reviewer_ids`` / the residual
        scores / ``anticipated_completion_date`` / ``completion_date`` cannot be
        set while the treatment plan is UNTREATED (Drata rejects them).
        """
        # Coerce defensively: the tool-test path bypasses jsonschema validation,
        # so a schema integer can arrive as a string (mirrors update_drata_risk).
        impact = _coerce_int(impact) or 3
        likelihood = _coerce_int(likelihood) or 3
        residual_impact = _coerce_int(residual_impact) or 0
        residual_likelihood = _coerce_int(residual_likelihood) or 0
        treated = treatment_plan != "UNTREATED"
        payload: dict[str, Any] = {
            "title": title,
            "description": description,
            "impact": impact,
            "likelihood": likelihood,
            "treatmentPlan": treatment_plan,
            "status": status,
        }
        if identified_at:
            payload["identifiedAt"] = identified_at
        if treatment_details and treated:
            payload["treatmentDetails"] = treatment_details
        if anticipated_completion_date and treated:
            payload["anticipatedCompletionDate"] = anticipated_completion_date
        if completion_date and treated:
            payload["completionDate"] = completion_date
        if residual_impact and treated:
            payload["residualImpact"] = residual_impact
        if residual_likelihood and treated:
            payload["residualLikelihood"] = residual_likelihood
        cats = _ids_from_csv(category_ids)
        if cats:
            payload["categories"] = cats
        owners = _ids_from_csv(owner_ids)
        if owners:
            payload["owners"] = owners
        reviewers = _ids_from_csv(reviewer_ids)
        if reviewers and treated:
            payload["reviewers"] = reviewers
        controls = _ids_from_csv(control_ids)
        if controls:
            payload["controls"] = controls
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "post",
                f"{base_url}/risk-registers/{risk_register_id}/risks",
                _PROVIDER,
                "create risk",
                headers=headers,
                json=payload,
            )
            if err:
                return err
            r = resp.json()
            return (
                f"Risk created: [{r.get('riskId', '?')}] {r.get('title')} "
                f"(ID: {r.get('id')}) — Score: {r.get('score', 'N/A')}"
            )

    async def update_drata_risk(
        risk_id: int,
        risk_register_id: int = 1,
        title: str = "",
        description: str = "",
        treatment_plan: str = "",
        treatment_details: str = "",
        status: str = "",
        impact: int = 0,
        likelihood: int = 0,
        residual_impact: int = 0,
        residual_likelihood: int = 0,
        owner_ids: str = "",
        reviewer_ids: str = "",
        category_ids: str = "",
        control_ids: str = "",
        identified_at: str = "",
        anticipated_completion_date: str = "",
        completion_date: str = "",
    ) -> str:
        """Update an existing risk. Only provided fields are changed.

        Reassign people/links with the CSV ID fields (resolve user IDs via
        list_drata_users): ``owner_ids`` (people responsible for the risk),
        ``reviewer_ids`` (treatment-plan approvers), ``category_ids``, and
        ``control_ids``. Each of these REPLACES the risk's current set with
        exactly the IDs you pass, so to reassign a stale owner pass the full
        desired owner list. Dates accept ``YYYY-MM-DD`` or full ISO-8601.

        ``treatment_details`` / ``reviewer_ids`` / the residual scores /
        ``anticipated_completion_date`` / ``completion_date`` cannot be set while
        the treatment plan is UNTREATED (Drata rejects them); they are dropped
        if you set ``treatment_plan="UNTREATED"`` in the same call.
        """
        # Coerce defensively: the tool-test path bypasses jsonschema validation,
        # so a schema integer can arrive as a string.
        impact = _coerce_int(impact) or 0
        likelihood = _coerce_int(likelihood) or 0
        residual_impact = _coerce_int(residual_impact) or 0
        residual_likelihood = _coerce_int(residual_likelihood) or 0
        # Treatment-gated fields are rejected by Drata when the plan is (being set
        # to) UNTREATED. Only suppress them when this call explicitly sets
        # UNTREATED; otherwise let Drata validate against the stored plan.
        untreating = treatment_plan == "UNTREATED"
        payload: dict[str, Any] = {}
        if title:
            payload["title"] = title
        if description:
            payload["description"] = description
        if treatment_plan:
            payload["treatmentPlan"] = treatment_plan
        if treatment_details and not untreating:
            payload["treatmentDetails"] = treatment_details
        if status:
            payload["status"] = status
        if impact:
            payload["impact"] = impact
        if likelihood:
            payload["likelihood"] = likelihood
        if residual_impact and not untreating:
            payload["residualImpact"] = residual_impact
        if residual_likelihood and not untreating:
            payload["residualLikelihood"] = residual_likelihood
        if identified_at:
            payload["identifiedAt"] = identified_at
        if anticipated_completion_date and not untreating:
            payload["anticipatedCompletionDate"] = anticipated_completion_date
        if completion_date and not untreating:
            payload["completionDate"] = completion_date
        owners = _ids_from_csv(owner_ids)
        if owners:
            payload["owners"] = owners
        reviewers = _ids_from_csv(reviewer_ids)
        if reviewers and not untreating:
            payload["reviewers"] = reviewers
        cats = _ids_from_csv(category_ids)
        if cats:
            payload["categories"] = cats
        controls = _ids_from_csv(control_ids)
        if controls:
            payload["controls"] = controls
        if not payload:
            return "Error: provide at least one field to update."
        async with egress_client(timeout=30) as client:
            _, err = await _common.safe_request(
                client,
                "put",
                f"{base_url}/risk-registers/{risk_register_id}/risks/{risk_id}",
                _PROVIDER,
                "update risk",
                headers=headers,
                json=payload,
            )
            if err:
                return err
            changed = ", ".join(sorted(payload.keys()))
            return f"Risk {risk_id} updated ({changed})."

    def _fmt_risk_register(rr: dict[str, Any]) -> str:
        owners = rr.get("owners") or []
        owner_names = ", ".join(_full_name(o) for o in owners) if owners else "N/A"
        workspaces = rr.get("workspaces") or []
        ws_names = ", ".join(w.get("name", str(w.get("id", "?"))) for w in workspaces) if workspaces else "N/A"
        return (
            f"[{rr.get('id')}] {rr.get('name', 'Unnamed')} — "
            f"{(rr.get('description') or '')[:200]} | "
            f"Owners: {owner_names} | Workspaces: {ws_names}"
        )

    async def get_drata_risk_register(risk_register_id: int = 1) -> str:
        """Get a single risk register including its owners and workspaces."""
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{base_url}/risk-registers/{risk_register_id}",
                _PROVIDER,
                "get risk register",
                headers=headers,
            )
            if err:
                return err
            return _fmt_risk_register(resp.json())

    async def create_drata_risk_register(
        name: str,
        description: str = "",
        owner_ids: str = "",
        workspace_ids: str = "",
    ) -> str:
        """Create a new risk register.

        ``owner_ids`` / ``workspace_ids`` are CSV lists of numeric Drata IDs
        (resolve user IDs via list_drata_users, workspace IDs via
        list_drata_workspaces).
        """
        name = (name or "").strip()
        if not name:
            return "Error: risk register name is required."
        payload: dict[str, Any] = {"name": name}
        if description:
            payload["description"] = description
        owners = _int_csv(owner_ids)
        if owners:
            payload["ownerIds"] = owners
        workspaces = _int_csv(workspace_ids)
        if workspaces:
            payload["workspaceIds"] = workspaces
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "post",
                f"{base_url}/risk-registers",
                _PROVIDER,
                "create risk register",
                headers=headers,
                json=payload,
            )
            if err:
                return err
            rr = resp.json() if resp.content else {}
            return f"Risk register '{name}' created (ID: {rr.get('id', '?')})."

    async def update_drata_risk_register(
        risk_register_id: int,
        name: str = "",
        description: str = "",
        owner_ids: str = "",
        workspace_ids: str = "",
    ) -> str:
        """Update a risk register. Only provided fields are changed.

        ``owner_ids`` / ``workspace_ids`` REPLACE the register's current owners
        / workspaces with exactly the CSV numeric IDs you pass.
        """
        payload: dict[str, Any] = {}
        # Guard against a null name on the tool-test path (bypasses jsonschema).
        name = (name or "").strip()
        if name:
            payload["name"] = name
        if description:
            payload["description"] = description
        owners = _int_csv(owner_ids)
        if owners:
            payload["ownerIds"] = owners
        workspaces = _int_csv(workspace_ids)
        if workspaces:
            payload["workspaceIds"] = workspaces
        if not payload:
            return "Error: provide at least one field to update."
        async with egress_client(timeout=30) as client:
            _, err = await _common.safe_request(
                client,
                "put",
                f"{base_url}/risk-registers/{risk_register_id}",
                _PROVIDER,
                "update risk register",
                headers=headers,
                json=payload,
            )
            if err:
                return err
            return f"Risk register {risk_register_id} updated."

    async def delete_drata_risk_register(risk_register_id: int) -> str:
        """Delete a risk register."""
        async with egress_client(timeout=30) as client:
            _, err = await _common.safe_request(
                client,
                "delete",
                f"{base_url}/risk-registers/{risk_register_id}",
                _PROVIDER,
                "delete risk register",
                headers=headers,
            )
            if err:
                return err
            return f"Risk register {risk_register_id} deleted."

    async def get_drata_risk_insights(risk_register_id: int = 1, owner_ids: str = "", status: str = "") -> str:
        """Risk register analytics: posture, treatment overview, scoring progress.

        Optionally filter by ``owner_ids`` (CSV of Drata user IDs) and/or
        ``status`` (CSV of ACTIVE/CLOSED/ARCHIVED). Requires the Risk Management
        Pro feature.
        """
        params: dict[str, Any] = {}
        owners = _int_csv(owner_ids)
        if owners:
            params["ownerIds[]"] = owners
        statuses = [s.strip().upper() for s in (status or "").split(",") if s.strip()]
        if statuses:
            params["status[]"] = statuses
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{base_url}/risk-registers/{risk_register_id}/insights",
                _PROVIDER,
                "get risk insights",
                headers=headers,
                params=params,
            )
            if err:
                return err
            d = resp.json()
            lines = [f"Risk Insights (register {risk_register_id}):"]
            scored = d.get("scored")
            remaining = d.get("remaining")
            if scored is not None or remaining is not None:
                lines.append(
                    f"Scoring progress: {scored if scored is not None else '?'} scored, "
                    f"{remaining if remaining is not None else '?'} remaining"
                )
            posture = d.get("riskPosture") or []
            if posture:
                lines.append("Posture: " + ", ".join(f"{p.get('title', '?')}={p.get('count', 0)}" for p in posture))
            treatment = d.get("treatmentOverview") or {}
            if treatment:
                lines.append("Treatment: " + ", ".join(f"{k}={v}" for k, v in treatment.items()))
            breakdown = d.get("categoryBreakdown") or []
            if breakdown:
                lines.append(f"Categories ({len(breakdown)}):")
                for cb in breakdown:
                    cat = (cb.get("category") or {}).get("name", "Uncategorized")
                    sev = cb.get("severity") or []
                    sev_str = ", ".join(f"{s.get('title', '?')}={s.get('count', 0)}" for s in sev)
                    lines.append(f"  {cat}: {sev_str or 'N/A'}")
            return "\n".join(lines)

    async def list_drata_risk_library(
        risk_register_id: int = 1,
        risk_id: str = "",
        title: str = "",
        description: str = "",
    ) -> str:
        """List standard (read-only) library risks available to copy into a register.

        Filter by ``risk_id`` (prefix match, e.g. "AC"), ``title`` (contains),
        or ``description`` (contains).
        """
        params: dict[str, Any] = {"expand[]": ["categories", "controls"]}
        if risk_id:
            params["riskId"] = risk_id
        if title:
            params["title"] = title
        if description:
            params["description"] = description
        async with egress_client(timeout=30) as client:
            items, err = await _common._paginated_fetch(
                client,
                f"{base_url}/risk-registers/{risk_register_id}/risk-library",
                headers,
                params,
                page_size=50,
            )
            if err:
                return err
            return format_entity_list(
                items,
                lambda r: (
                    f"[{r.get('riskId', '?')}] {r.get('title', 'Untitled')} "
                    f"(ID: {r.get('id')}) — {(r.get('description') or '')[:160]}"
                ),
                noun="library risk",
            )

    async def get_drata_risk_library_item(risk_library_id: int, risk_register_id: int = 1) -> str:
        """Get full details of a single Risk Library item."""
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{base_url}/risk-registers/{risk_register_id}/risk-library/{risk_library_id}",
                _PROVIDER,
                "get risk library item",
                headers=headers,
                params={"expand[]": ["categories", "controls"]},
            )
            if err:
                return err
            r = resp.json()
            lines = [
                f"Library Risk: [{r.get('riskId', '?')}] {r.get('title', 'Untitled')} (ID: {r.get('id')})",
                f"Description: {r.get('description', 'N/A')}",
            ]
            cats = r.get("categories") or []
            if cats:
                lines.append(f"Categories: {', '.join(c.get('name', '') for c in cats)}")
            controls = r.get("controls") or []
            if controls:
                lines.append(
                    "Suggested Controls: " + ", ".join(f"[{c.get('code', '?')}] {c.get('name', '')}" for c in controls)
                )
            return "\n".join(lines)

    async def copy_drata_risk_library(register_id: int = 1, risk_ids: str = "", risk_groups: str = "") -> str:
        """Copy standard risks from the Risk Library into a register.

        Provide ``risk_ids`` (CSV of library risk IDs, e.g. "AA-01,AA-02") to
        copy specific risks, OR ``risk_groups`` (CSV of group keys from
        ``_RISK_LIBRARY_GROUPS``) to copy whole groups. Provide one, not both
        (Drata performs a single bulk action type per call).
        """
        ids = [s.strip() for s in (risk_ids or "").split(",") if s.strip()]
        groups = [s.strip().upper() for s in (risk_groups or "").split(",") if s.strip()]
        if not ids and not groups:
            return "Error: provide risk_ids or risk_groups to copy."
        # The endpoint runs a single bulkActionType per call, so risk_ids and
        # risk_groups are mutually exclusive. Reject the ambiguous combination
        # instead of silently dropping risk_groups under COPY_BY_IDS (which the
        # API would ignore or reject with a 400).
        if ids and groups:
            return (
                "Error: provide risk_ids OR risk_groups, not both. The Drata "
                "bulk copy performs a single action type per call "
                "(COPY_BY_IDS or COPY_BY_GROUPS)."
            )
        bad = [g for g in groups if g not in _RISK_LIBRARY_GROUPS]
        if bad:
            return f"Error: invalid risk group(s) {', '.join(bad)}. Valid groups: {', '.join(_RISK_LIBRARY_GROUPS)}."
        payload: dict[str, Any] = {"registerId": register_id}
        if ids:
            payload["bulkActionType"] = "COPY_BY_IDS"
            payload["riskIds"] = ids
        else:
            payload["bulkActionType"] = "COPY_BY_GROUPS"
            payload["riskGroups"] = groups
        async with egress_client(timeout=60) as client:
            resp, err = await _common.safe_request(
                client,
                "post",
                f"{base_url}/risk-library/action-copy",
                _PROVIDER,
                "copy risk library items",
                headers=headers,
                json=payload,
            )
            if err:
                return err
            data = resp.json() if resp.content else {}
            copied = data.get("data") if isinstance(data, dict) else data
            if isinstance(copied, list) and copied:
                summary = "; ".join(
                    f"[{c.get('riskId', '?')}] {c.get('title', 'Untitled')} (ID: {c.get('id')})" for c in copied[:25]
                )
                return f"Copied {len(copied)} risk(s) into register {register_id}: {summary}"
            return f"Risk library copy completed for register {register_id}."

    def _fmt_risk_note(n: dict[str, Any]) -> str:
        owner = n.get("owner")
        owner_str = f" — by {_full_name(owner)}" if owner else ""
        return f"[{n.get('id')}] {n.get('comment', '')[:300]} ({n.get('createdAt', 'N/A')}{owner_str})"

    async def list_drata_risk_notes(risk_id: int, risk_register_id: int = 1) -> str:
        """List notes attached to a risk."""
        async with egress_client(timeout=30) as client:
            items, err = await _common._paginated_fetch(
                client,
                f"{base_url}/risk-registers/{risk_register_id}/risks/{risk_id}/notes",
                headers,
                {"expand[]": ["owner"]},
            )
            if err:
                return err
            return format_entity_list(items, _fmt_risk_note, noun="risk note")

    async def get_drata_risk_note(risk_id: int, note_id: int, risk_register_id: int = 1) -> str:
        """Get a single risk note."""
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{base_url}/risk-registers/{risk_register_id}/risks/{risk_id}/notes/{note_id}",
                _PROVIDER,
                "get risk note",
                headers=headers,
                params={"expand[]": ["owner"]},
            )
            if err:
                return err
            return _fmt_risk_note(resp.json())

    async def create_drata_risk_note(risk_id: int, comment: str, risk_register_id: int = 1) -> str:
        """Add a note to a risk."""
        comment = (comment or "").strip()
        if not comment:
            return "Error: comment is required."
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "post",
                f"{base_url}/risk-registers/{risk_register_id}/risks/{risk_id}/notes",
                _PROVIDER,
                "create risk note",
                headers=headers,
                json={"comment": comment},
            )
            if err:
                return err
            n = resp.json() if resp.content else {}
            return f"Note added to risk {risk_id} (ID: {n.get('id', '?')})."

    async def update_drata_risk_note(risk_id: int, note_id: int, comment: str, risk_register_id: int = 1) -> str:
        """Update a risk note's comment."""
        comment = (comment or "").strip()
        if not comment:
            return "Error: comment is required."
        async with egress_client(timeout=30) as client:
            _, err = await _common.safe_request(
                client,
                "put",
                f"{base_url}/risk-registers/{risk_register_id}/risks/{risk_id}/notes/{note_id}",
                _PROVIDER,
                "update risk note",
                headers=headers,
                json={"comment": comment},
            )
            if err:
                return err
            return f"Risk note {note_id} updated."

    async def delete_drata_risk_note(risk_id: int, note_id: int, risk_register_id: int = 1) -> str:
        """Delete a risk note."""
        async with egress_client(timeout=30) as client:
            _, err = await _common.safe_request(
                client,
                "delete",
                f"{base_url}/risk-registers/{risk_register_id}/risks/{risk_id}/notes/{note_id}",
                _PROVIDER,
                "delete risk note",
                headers=headers,
            )
            if err:
                return err
            return f"Risk note {note_id} deleted."

    async def list_drata_risk_documents(risk_id: int, risk_register_id: int = 1) -> str:
        """List supporting documents attached to a risk."""
        async with egress_client(timeout=30) as client:
            items, err = await _common._paginated_fetch(
                client,
                f"{base_url}/risk-registers/{risk_register_id}/risks/{risk_id}/documents",
                headers,
                {},
            )
            if err:
                return err
            return format_entity_list(
                items,
                lambda d: f"[{d.get('id')}] {d.get('name', 'Unnamed')} — Created: {d.get('createdAt', 'N/A')}",
                noun="risk document",
            )

    async def get_drata_risk_document(risk_id: int, document_id: int, risk_register_id: int = 1) -> str:
        """Get a risk document's metadata including its download URL."""
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{base_url}/risk-registers/{risk_register_id}/risks/{risk_id}/documents/{document_id}",
                _PROVIDER,
                "get risk document",
                headers=headers,
            )
            if err:
                return err
            d = resp.json()
            return (
                f"Document: {d.get('name', 'Unnamed')} (ID: {d.get('id')})\n"
                f"Download URL: {d.get('downloadUrl', 'N/A')}\n"
                f"Created: {d.get('createdAt', 'N/A')}"
            )

    async def upload_drata_risk_document(
        risk_id: int,
        risk_register_id: int = 1,
        file_content: str = "",
        file_name: str = "",
        file_encoding: str = "text",
        source_file_id: str = "",
        file_context: FileContext | None = None,
    ) -> str:
        """Upload a supporting document to a risk (requires Risk Management Pro).

        Provide ONE source:

        * ``source_file_id`` — a file the user already uploaded to this
          conversation, forwarded byte-for-byte.
        * ``file_content`` — Markdown / HTML rendered to a real PDF (``.pdf``,
          the default) or DOCX (``.docx``), uploaded verbatim for text formats
          (``.md`` / ``.txt`` / ``.html`` / ``.csv`` / …), or decoded from
          base64 when ``file_encoding='base64'``.

        Up to 10 documents (25 MB each) can be filed per risk; this uploads one
        per call.
        """
        sources = [bool(file_content), bool(source_file_id.strip())]
        if sum(sources) != 1:
            return "Error: provide exactly one artifact source — file_content or source_file_id."
        if source_file_id.strip():
            resolved = await _resolve_uploaded_evidence_file(file_context, source_file_id.strip(), file_name)
        else:
            resolved = await _render_evidence_file(
                file_content, file_name, file_encoding, fallback_name="risk-document"
            )
        if isinstance(resolved, str):
            return resolved
        file_bytes, resolved_name, mime = resolved
        files = [("documents", (resolved_name, file_bytes, mime))]
        async with egress_client(timeout=120) as client:
            resp, err = await _common.safe_request(
                client,
                "post",
                f"{base_url}/risk-registers/{risk_register_id}/risks/{risk_id}/documents",
                _PROVIDER,
                "upload risk document",
                headers=upload_headers,
                files=files,
            )
            if err:
                return err
            data = resp.json() if resp.content else {}
            docs = data.get("data") if isinstance(data, dict) else data
            text = f"Document uploaded to risk {risk_id}."
            if isinstance(docs, list) and docs:
                ids = ", ".join(str(d.get("id", "?")) for d in docs)
                text = f"Document uploaded to risk {risk_id} (document ID(s): {ids})."
            return uploaded_file_answer(text, target=_PROVIDER, filename=resolved_name)

    async def delete_drata_risk_document(risk_id: int, document_id: int, risk_register_id: int = 1) -> str:
        """Delete a supporting document from a risk."""
        async with egress_client(timeout=30) as client:
            _, err = await _common.safe_request(
                client,
                "delete",
                f"{base_url}/risk-registers/{risk_register_id}/risks/{risk_id}/documents/{document_id}",
                _PROVIDER,
                "delete risk document",
                headers=headers,
            )
            if err:
                return err
            return f"Risk document {document_id} deleted."

    callable_map = {
        "list_drata_risk_registers": list_drata_risk_registers,
        "get_drata_risk_register": get_drata_risk_register,
        "create_drata_risk_register": create_drata_risk_register,
        "update_drata_risk_register": update_drata_risk_register,
        "delete_drata_risk_register": delete_drata_risk_register,
        "list_drata_risk_categories": list_drata_risk_categories,
        "create_drata_risk_category": create_drata_risk_category,
        "delete_drata_risk_category": delete_drata_risk_category,
        "list_drata_risks": list_drata_risks,
        "get_drata_risk": get_drata_risk,
        "create_drata_risk": create_drata_risk,
        "update_drata_risk": update_drata_risk,
        "get_drata_risk_insights": get_drata_risk_insights,
        "list_drata_risk_library": list_drata_risk_library,
        "get_drata_risk_library_item": get_drata_risk_library_item,
        "copy_drata_risk_library": copy_drata_risk_library,
        "list_drata_risk_notes": list_drata_risk_notes,
        "get_drata_risk_note": get_drata_risk_note,
        "create_drata_risk_note": create_drata_risk_note,
        "update_drata_risk_note": update_drata_risk_note,
        "delete_drata_risk_note": delete_drata_risk_note,
        "list_drata_risk_documents": list_drata_risk_documents,
        "get_drata_risk_document": get_drata_risk_document,
        "upload_drata_risk_document": upload_drata_risk_document,
        "delete_drata_risk_document": delete_drata_risk_document,
    }
    for _name, _fn in callable_map.items():
        executor.register(_name, _fn)

    return [
        ToolDefinition(
            name="list_drata_risk_registers",
            description="List Drata risk registers.",
            input_schema=_empty,
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="get_drata_risk_register",
            description="Get a single Drata risk register including its owners and workspaces.",
            input_schema={
                "type": "object",
                "properties": {
                    "risk_register_id": {"type": "integer", "default": 1},
                },
                "required": [],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="create_drata_risk_register",
            description="Create a new Drata risk register. owner_ids / workspace_ids are CSV lists of numeric IDs.",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "owner_ids": {
                        "type": "string",
                        "description": "CSV of Drata user IDs to own the register",
                    },
                    "workspace_ids": {
                        "type": "string",
                        "description": "CSV of workspace IDs to associate",
                    },
                },
                "required": ["name"],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="update_drata_risk_register",
            description="Update a Drata risk register. Only provided fields change; owner_ids / workspace_ids (CSV numeric IDs) REPLACE the current sets.",
            input_schema={
                "type": "object",
                "properties": {
                    "risk_register_id": {"type": "integer"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "owner_ids": {"type": "string"},
                    "workspace_ids": {"type": "string"},
                },
                "required": ["risk_register_id"],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="delete_drata_risk_register",
            description="Delete a Drata risk register.",
            input_schema={
                "type": "object",
                "properties": {"risk_register_id": {"type": "integer"}},
                "required": ["risk_register_id"],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="list_drata_risk_categories",
            description="List risk categories in a Drata risk register.",
            input_schema={
                "type": "object",
                "properties": {
                    "risk_register_id": {"type": "integer", "default": 1},
                },
                "required": [],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="create_drata_risk_category",
            description="Create a new risk category in a Drata risk register.",
            input_schema={
                "type": "object",
                "properties": {
                    "risk_register_id": {"type": "integer", "default": 1},
                    "name": {"type": "string"},
                },
                "required": ["risk_register_id", "name"],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="delete_drata_risk_category",
            description="Delete a risk category.",
            input_schema={
                "type": "object",
                "properties": {
                    "risk_register_id": {"type": "integer", "default": 1},
                    "category_id": {"type": "integer"},
                },
                "required": ["category_id"],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="list_drata_risks",
            description="List risks from the Drata risk register with scores, treatment plans, owners, and reviewers. Filter by status, treatment plan, title, score range, or vendor_id.",
            input_schema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["ACTIVE", "ARCHIVED", "CLOSED"],
                    },
                    "treatment_plan": {
                        "type": "string",
                        "enum": ["UNTREATED", "ACCEPT", "TRANSFER", "AVOID", "MITIGATE"],
                    },
                    "risk_register_id": {"type": "integer", "default": 1},
                    "title": {"type": "string"},
                    "min_score": {"type": "integer"},
                    "max_score": {"type": "integer"},
                    "vendor_id": {
                        "type": "integer",
                        "description": "Only risks linked to this vendor/third-party ID",
                    },
                },
                "required": [],
            },
            source="integration:drata",
            priority=0,
        ),
        ToolDefinition(
            name="get_drata_risk",
            description="Get full details of a specific Drata risk including treatment plan, owners, reviewers, linked controls, notes, and tasks.",
            input_schema={
                "type": "object",
                "properties": {
                    "risk_id": {"type": "integer"},
                    "risk_register_id": {"type": "integer", "default": 1},
                },
                "required": ["risk_id"],
            },
            source="integration:drata",
            priority=0,
        ),
        ToolDefinition(
            name="create_drata_risk",
            description="Create a new risk. category_ids / owner_ids / reviewer_ids / control_ids are CSV lists of numeric Drata IDs. Dates accept YYYY-MM-DD or full ISO-8601. Treatment-only fields (treatment_details, reviewer_ids, residual scores, completion dates) are ignored while treatment_plan is UNTREATED.",
            input_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "risk_register_id": {"type": "integer", "default": 1},
                    "impact": {"type": "integer", "default": 3},
                    "likelihood": {"type": "integer", "default": 3},
                    "treatment_plan": {
                        "type": "string",
                        "enum": ["UNTREATED", "ACCEPT", "TRANSFER", "AVOID", "MITIGATE"],
                        "default": "UNTREATED",
                    },
                    "treatment_details": {"type": "string"},
                    "category_ids": {"type": "string"},
                    "owner_ids": {
                        "type": "string",
                        "description": "CSV of Drata user IDs responsible for the risk",
                    },
                    "reviewer_ids": {
                        "type": "string",
                        "description": "CSV of Drata user IDs who approve the treatment plan",
                    },
                    "control_ids": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["ACTIVE", "ARCHIVED", "CLOSED"],
                        "default": "ACTIVE",
                    },
                    "identified_at": {"type": "string"},
                    "anticipated_completion_date": {"type": "string"},
                    "completion_date": {"type": "string"},
                    "residual_impact": {"type": "integer"},
                    "residual_likelihood": {"type": "integer"},
                },
                "required": ["title", "description"],
            },
            source="integration:drata",
            priority=1,
        ),
        ToolDefinition(
            name="update_drata_risk",
            description=(
                "Update an existing risk. Only provided fields change. Reassign "
                "people/links with owner_ids (people responsible), reviewer_ids "
                "(treatment approvers), category_ids, control_ids — each a CSV of "
                "numeric Drata IDs that REPLACES the risk's current set (so to "
                "reassign a stale/departed owner, pass the full desired owner "
                "list; resolve IDs via list_drata_users). Dates accept "
                "YYYY-MM-DD or full ISO-8601."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "risk_id": {"type": "integer"},
                    "risk_register_id": {"type": "integer", "default": 1},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "treatment_plan": {
                        "type": "string",
                        "enum": ["UNTREATED", "ACCEPT", "TRANSFER", "AVOID", "MITIGATE"],
                    },
                    "treatment_details": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["ACTIVE", "ARCHIVED", "CLOSED"],
                    },
                    "impact": {"type": "integer"},
                    "likelihood": {"type": "integer"},
                    "residual_impact": {"type": "integer"},
                    "residual_likelihood": {"type": "integer"},
                    "owner_ids": {
                        "type": "string",
                        "description": "CSV of Drata user IDs — REPLACES the risk owners",
                    },
                    "reviewer_ids": {
                        "type": "string",
                        "description": "CSV of Drata user IDs — REPLACES the risk reviewers",
                    },
                    "category_ids": {
                        "type": "string",
                        "description": "CSV of category IDs — REPLACES the risk categories",
                    },
                    "control_ids": {
                        "type": "string",
                        "description": "CSV of control IDs — REPLACES the linked controls",
                    },
                    "identified_at": {"type": "string"},
                    "anticipated_completion_date": {"type": "string"},
                    "completion_date": {"type": "string"},
                },
                "required": ["risk_id"],
            },
            source="integration:drata",
            priority=1,
        ),
        ToolDefinition(
            name="get_drata_risk_insights",
            description="Risk register analytics: posture distribution, treatment overview, scoring progress, and category breakdown. Filter by owner_ids / status (CSV). Requires Risk Management Pro.",
            input_schema={
                "type": "object",
                "properties": {
                    "risk_register_id": {"type": "integer", "default": 1},
                    "owner_ids": {
                        "type": "string",
                        "description": "CSV of Drata user IDs to filter insights by owner",
                    },
                    "status": {
                        "type": "string",
                        "description": "CSV of ACTIVE/CLOSED/ARCHIVED",
                    },
                },
                "required": [],
            },
            source="integration:drata",
            priority=1,
        ),
        ToolDefinition(
            name="list_drata_risk_library",
            description="List standard (read-only) library risks available to copy into a register. Filter by risk_id prefix, title, or description.",
            input_schema={
                "type": "object",
                "properties": {
                    "risk_register_id": {"type": "integer", "default": 1},
                    "risk_id": {"type": "string"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": [],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="get_drata_risk_library_item",
            description="Get full details of a single Risk Library item.",
            input_schema={
                "type": "object",
                "properties": {
                    "risk_library_id": {"type": "integer"},
                    "risk_register_id": {"type": "integer", "default": 1},
                },
                "required": ["risk_library_id"],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="copy_drata_risk_library",
            description=(
                "Copy standard risks from the Risk Library into a register. Pass "
                "risk_ids (CSV of library risk IDs like 'AA-01,AA-02') to copy "
                "specific risks, or risk_groups (CSV) to copy whole groups: " + ", ".join(_RISK_LIBRARY_GROUPS) + "."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "register_id": {"type": "integer", "default": 1},
                    "risk_ids": {"type": "string"},
                    "risk_groups": {
                        "type": "string",
                        "description": "CSV of group keys (see description)",
                    },
                },
                "required": [],
            },
            source="integration:drata",
            priority=1,
        ),
        ToolDefinition(
            name="list_drata_risk_notes",
            description="List notes attached to a Drata risk.",
            input_schema={
                "type": "object",
                "properties": {
                    "risk_id": {"type": "integer"},
                    "risk_register_id": {"type": "integer", "default": 1},
                },
                "required": ["risk_id"],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="get_drata_risk_note",
            description="Get a single risk note.",
            input_schema={
                "type": "object",
                "properties": {
                    "risk_id": {"type": "integer"},
                    "note_id": {"type": "integer"},
                    "risk_register_id": {"type": "integer", "default": 1},
                },
                "required": ["risk_id", "note_id"],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="create_drata_risk_note",
            description="Add a note to a Drata risk.",
            input_schema={
                "type": "object",
                "properties": {
                    "risk_id": {"type": "integer"},
                    "comment": {"type": "string"},
                    "risk_register_id": {"type": "integer", "default": 1},
                },
                "required": ["risk_id", "comment"],
            },
            source="integration:drata",
            priority=1,
        ),
        ToolDefinition(
            name="update_drata_risk_note",
            description="Update a risk note's comment.",
            input_schema={
                "type": "object",
                "properties": {
                    "risk_id": {"type": "integer"},
                    "note_id": {"type": "integer"},
                    "comment": {"type": "string"},
                    "risk_register_id": {"type": "integer", "default": 1},
                },
                "required": ["risk_id", "note_id", "comment"],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="delete_drata_risk_note",
            description="Delete a risk note.",
            input_schema={
                "type": "object",
                "properties": {
                    "risk_id": {"type": "integer"},
                    "note_id": {"type": "integer"},
                    "risk_register_id": {"type": "integer", "default": 1},
                },
                "required": ["risk_id", "note_id"],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="list_drata_risk_documents",
            description="List supporting documents attached to a Drata risk.",
            input_schema={
                "type": "object",
                "properties": {
                    "risk_id": {"type": "integer"},
                    "risk_register_id": {"type": "integer", "default": 1},
                },
                "required": ["risk_id"],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="get_drata_risk_document",
            description="Get a risk document's metadata including its download URL.",
            input_schema={
                "type": "object",
                "properties": {
                    "risk_id": {"type": "integer"},
                    "document_id": {"type": "integer"},
                    "risk_register_id": {"type": "integer", "default": 1},
                },
                "required": ["risk_id", "document_id"],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="upload_drata_risk_document",
            description=(
                "Upload a supporting document to a risk (Risk Management Pro). "
                "Provide ONE source: source_file_id (a file the user uploaded to "
                "this conversation, forwarded byte-for-byte) or file_content "
                "(Markdown/HTML rendered to PDF/DOCX, verbatim text, or base64 "
                "with file_encoding='base64')."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "risk_id": {"type": "integer"},
                    "risk_register_id": {"type": "integer", "default": 1},
                    "file_content": {"type": "string"},
                    "file_name": {"type": "string"},
                    "file_encoding": {
                        "type": "string",
                        "enum": ["text", "base64"],
                        "default": "text",
                    },
                    "source_file_id": {"type": "string"},
                },
                "required": ["risk_id"],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="delete_drata_risk_document",
            description="Delete a supporting document from a risk.",
            input_schema={
                "type": "object",
                "properties": {
                    "risk_id": {"type": "integer"},
                    "document_id": {"type": "integer"},
                    "risk_register_id": {"type": "integer", "default": 1},
                },
                "required": ["risk_id", "document_id"],
            },
            source="integration:drata",
            priority=2,
        ),
    ]
