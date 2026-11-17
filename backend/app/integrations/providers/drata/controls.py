"""Drata controls tools - Controls: management, scope, requirements, notes, owners, and external evidence."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from app.core.egress import egress_client
from app.integrations.file_delivery import uploaded_file_answer
from app.integrations.http import format_entity_list
from app.tools.base import CallableToolExecutor, FileContext, ToolDefinition

from . import _common
from ._common import (
    _PROVIDER,
    DRATA_RENEWAL_SCHEDULE_TYPES,
    _coerce_int,
    _full_name,
    _int_csv,
    _note_ref,
    _render_evidence_file,
    _resolve_uploaded_evidence_file,
)

# --- Control notes (Drata v2 API) -----------------------------------------
# Routes verified against the published v2 OpenAPI reference (tag "Control
# Notes"). They are RELATIVE TO THE CONFIGURED v2 BASE URL - the integration's
# region host + ``/public/v2`` (``https://public-api.drata.com/public/v2``,
# ``https://public-api.eu.drata.com/public/v2``, ...); the v1 API (unversioned
# ``/public``) has no control-notes endpoints at all:
#   GET    /workspaces/{workspaceId}/controls/{controlId}/notes
#   POST   /workspaces/{workspaceId}/controls/{controlId}/notes
#   GET    /workspaces/{workspaceId}/controls/{controlId}/notes/{noteId}
#   PUT    /workspaces/{workspaceId}/controls/{controlId}/notes/{noteId}
#   DELETE /workspaces/{workspaceId}/controls/{controlId}/notes/{noteId}
# ``noteId`` is a UUID string, not an integer. The list endpoint paginates with
# the v2 ``cursor`` / ``size`` scheme and the only value ``ControlNotesExpandEnum``
# allows for ``expand[]`` is ``owner`` (``author`` is rejected with HTTP 400).
# Only the note's owner - the Drata user behind the API key that created it -
# may update or delete it; Drata answers 403 for any other identity.

#: Returned before any HTTP call when ``note_id`` is neither a UUID nor a
#: purely numeric legacy id (see ``_common._note_ref``).
_NOTE_ID_ERROR = (
    "Error: note_id must be the note's UUID as returned by list_drata_control_notes "
    "(e.g. 81584418-17a7-44b1-b0f9-47d42b903812). No request was sent to Drata."
)


def _fmt_control_note(n: dict[str, Any]) -> str:
    """One line per note: UUID id, comment, owner (id + email), timestamps.

    The comment is rendered whole (Drata caps it at 191 characters); line
    breaks inside it are flattened so the list stays one note per line.
    """
    owner = n.get("owner") if isinstance(n.get("owner"), dict) else None
    if owner:
        owner_str = f"{_full_name(owner)} (id={owner.get('id')}, {owner.get('email') or 'N/A'})"
    elif n.get("ownerId") is not None:
        owner_str = f"id={n.get('ownerId')}"
    else:
        owner_str = "N/A"
    comment = " ".join(str(n.get("comment") or "").splitlines())
    return (
        f"[{n.get('id')}] {comment} | Owner: {owner_str} | "
        f"Created: {n.get('createdAt', 'N/A')} | Updated: {n.get('updatedAt', 'N/A')}"
    )


def _note_owner_mismatch_error(resp: httpx.Response | None, verb: str, note_id: str, control_id: int) -> str | None:
    """Explain a 403 on a note write as the owner mismatch it almost always is.

    Drata lets only the owner of a note update or delete it, so a 403 here
    means the identity behind this integration's API key did not create the
    note (or, less often, lacks the ``Controls: Update/Delete Control Note``
    permission). Returns ``None`` for any other outcome so the caller falls
    back to the verbatim error.
    """
    if resp is None or resp.status_code != 403:
        return None
    return (
        f"Error: Drata refused to {verb} note {note_id} on control {control_id} (HTTP 403): "
        "only the note's owner can update or delete it, and the Drata user behind this "
        "integration's API key is not the owner of this note. Run the operation with the "
        "API key of the user who created the note (or check that the key has the "
        f"'Controls: {verb.capitalize()} Control Note' permission). "
        f"Response body: {_common._verbatim_body(resp)}"
    )


#: File extensions Drata accepts for a control **external-evidence** artifact
#: (v1 Upload External Evidence). A superset of the User Document set that adds
#: text/markdown/json/csv and zip, but - unlike the Evidence Library - excludes
#: ``msg`` / ``mp4``.
_EXTERNAL_EVIDENCE_FILE_EXTENSIONS: frozenset[str] = frozenset(
    {
        "pdf",
        "docx",
        "odt",
        "doc",
        "xlsx",
        "ods",
        "pptx",
        "odp",
        "gif",
        "jpg",
        "jpeg",
        "png",
        "json",
        "csv",
        "md",
        "markdown",
        "txt",
        "zip",
    }
)

#: Days added to the creation date to derive a renewal date per schedule type
#: when the caller omits ``renewal_date`` (the v1 endpoint requires one).
_RENEWAL_SCHEDULE_DAYS: dict[str, int] = {
    "ONE_MONTH": 30,
    "TWO_MONTHS": 60,
    "THREE_MONTHS": 90,
    "SIX_MONTHS": 180,
    "ONE_YEAR": 365,
    "NONE": 365,
}


def _derive_renewal_date(creation_date: str, schedule: str) -> str:
    """Derive a renewal date from a creation date + schedule type.

    Falls back to a one-year window for unknown/NONE schedules. Returns the
    creation date unchanged if it cannot be parsed (Drata validates the value).
    """
    try:
        base = date.fromisoformat(creation_date[:10])
    except ValueError:
        return creation_date
    return (base + timedelta(days=_RENEWAL_SCHEDULE_DAYS.get(schedule, 365))).isoformat()


def register(
    base_url: str,
    headers: dict[str, str],
    upload_headers: dict[str, str],
    executor: CallableToolExecutor,
) -> list[ToolDefinition]:
    """Build the Drata controls tools and register their callables."""

    async def list_drata_controls(
        workspace_id: int = 1,
        is_ready: str = "",
        framework_tag: str = "",
        has_evidence: str = "",
        has_policy: str = "",
        has_passing_test: str = "",
        is_monitored: str = "",
        is_enabled: str = "",
        is_archived: str = "",
        policy_id: int = 0,
        ticket_status: str = "",
        without_owner: str = "",
        owner_id: int = 0,
        compact: str = "",
        limit: int = 0,
    ) -> str:
        """List compliance controls in a workspace.

        Scope (the most important filter): ``is_archived`` — "false" returns only
        IN-SCOPE controls, "true" only OUT-OF-SCOPE (archived) controls; omit for
        both. Drata models out-of-scope as an archived control, so scope is shown
        on every row ("in scope" / "out of scope (since …)").

        Server-side boolean filters ("true"/"false"): ``is_ready``,
        ``has_evidence``, ``has_policy``, ``has_passing_test``, ``is_monitored``,
        ``is_enabled``, ``is_archived``. Also ``policy_id`` (controls mapped to a
        policy) and ``ticket_status`` (IN_PROGRESS / ARCHIVED).

        Client-side context-saving filters — use these for bulk work so you fetch
        a SMALL, focused slice instead of the whole control set:

          * ``framework_tag`` — only controls tagged with that framework (e.g.
            SOC_2, ISO27001, GDPR).
          * ``without_owner="true"`` — only controls with NO owner (the
            ownerless work queue).
          * ``owner_id`` — only controls owned by that Drata user ID.
          * ``compact="true"`` — terse one line per control.
          * ``limit`` — cap the number of controls returned (0 = no cap).

        Owner Drata user IDs are included inline (``owners=[Name(id=N)]``) so you
        can re-assign / remove owners (e.g. via ``set_drata_control_owners``)
        without an extra per-control lookup.
        """

        def _flag(value: Any) -> bool:
            # Tool args are not type-coerced on every execution path: the
            # integration tool-test endpoint dispatches straight to the handler
            # and bypasses the agent loop's jsonschema pre-validation, so a JSON
            # boolean can arrive here as a Python bool. Coerce defensively so
            # ".lower()" never raises AttributeError on a non-string flag.
            return str(value).strip().lower() == "true"

        def _provided(value: Any) -> bool:
            # The boolean filters are tri-state: unset ("" / None), "true", or
            # "false". Only "" / None means "not provided" - a JSON boolean
            # ``False`` (reachable on the same bypassed-validation path) is an
            # explicit value, so e.g. ``is_archived=false`` must filter for
            # in-scope instead of being silently dropped as if absent.
            return value not in ("", None)

        # ``owner_id`` / ``limit`` / ``policy_id`` are integers in the schema, but
        # the same bypassed-validation path can deliver them as JSON strings;
        # coerce so comparisons / ``limit > 0`` can't raise TypeError and a
        # string ``owner_id`` can't silently match nothing.
        owner_id = _coerce_int(owner_id) or 0
        limit = _coerce_int(limit) or 0
        policy_id = _coerce_int(policy_id) or 0

        want_compact = _flag(compact)
        only_unowned = _flag(without_owner)
        async with egress_client(timeout=30) as client:
            params: dict[str, Any] = {
                "expand[]": ["flags", "owners", "frameworkTags"],
            }
            _bool_filters = {
                "isReady": is_ready,
                "hasEvidence": has_evidence,
                "hasPolicy": has_policy,
                "hasPassingTest": has_passing_test,
                "isMonitored": is_monitored,
                "isEnabled": is_enabled,
                "isArchived": is_archived,
            }
            for api_key, raw in _bool_filters.items():
                if _provided(raw):
                    params[api_key] = _flag(raw)
            if policy_id:
                params["policyId"] = policy_id
            if ticket_status.strip():
                params["ticketStatus"] = ticket_status.strip().upper()
            items, err = await _common._paginated_fetch(
                client,
                f"{base_url}/workspaces/{workspace_id}/controls",
                headers,
                params,
            )
            if err:
                return err

            def _owners_of(c: dict) -> list[dict]:
                owners_data = c.get("owners", {})
                return owners_data.get("data", []) if isinstance(owners_data, dict) else []

            # Client-side filters: Drata's List Controls endpoint has no
            # server-side framework or owner filter, so trim here. This also
            # keeps the RETURNED payload small — the actual driver of context
            # bloat.
            if framework_tag:
                ft = framework_tag.strip().upper()
                items = [c for c in items if ft in [t.upper() for t in (c.get("frameworkTags") or [])]]
            if only_unowned:
                items = [c for c in items if not _owners_of(c)]
            if owner_id:
                items = [c for c in items if any(o.get("id") == owner_id for o in _owners_of(c))]
            if limit and limit > 0:
                items = items[:limit]

            def _owner_label(o: dict) -> str:
                return f"{_full_name(o)}(id={o.get('id')})"

            def _scope_of(c: dict) -> str:
                return "out of scope" if c.get("archivedAt") else "in scope"

            def _fmt_compact(c: dict) -> str:
                flags = c.get("flags", {})
                owners_list = _owners_of(c)
                owners = ", ".join(_owner_label(o) for o in owners_list) if owners_list else "none"
                fw_tags = c.get("frameworkTags", [])
                frameworks = ", ".join(fw_tags) if fw_tags else "N/A"
                return (
                    f"[{c.get('code', '?')}] {c.get('name', 'Untitled')} "
                    f"(id={c.get('id')}) scope={_scope_of(c)} "
                    f"ready={flags.get('isReady', 'N/A')} "
                    f"fw={frameworks} owners=[{owners}]"
                )

            def _fmt(c: dict) -> str:
                flags = c.get("flags", {})
                fw_tags = c.get("frameworkTags", [])
                frameworks = ", ".join(fw_tags) if fw_tags else "N/A"
                owners_list = _owners_of(c)
                owner_names = ", ".join(_owner_label(o) for o in owners_list) or "N/A"
                archived = c.get("archivedAt")
                scope = f"out of scope (since {archived})" if archived else "in scope"
                return (
                    f"[{c.get('code', '?')}] {c.get('name', 'Untitled')} "
                    f"(ID: {c.get('id')}) — "
                    f"Scope: {scope} | "
                    f"Ready: {flags.get('isReady', 'N/A')} | "
                    f"Evidence: {flags.get('hasEvidence', 'N/A')} | "
                    f"Policy: {flags.get('hasPolicy', 'N/A')} | "
                    f"Monitored: {flags.get('isMonitored', 'N/A')} | "
                    f"Frameworks: {frameworks} | "
                    f"Owners: {owner_names}"
                )

            return format_entity_list(items, _fmt_compact if want_compact else _fmt, noun="control")

    async def get_drata_control(control_id: int | str, workspace_id: int = 1) -> str:
        """Get full details of a specific control: scope, readiness flags, owners,
        requirements, test/evidence counts, topics, and custom fields.

        ``control_id`` may be a numeric ID or a control code prefixed with
        ``code:`` (e.g. ``code:DCF-542``); it is interpolated verbatim into the
        request path, so both an integer ID and a string code are accepted.
        """
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{base_url}/workspaces/{workspace_id}/controls/{control_id}",
                _PROVIDER,
                "get control",
                headers=headers,
                params={
                    "expand[]": [
                        "flags",
                        "owners",
                        "requirements",
                        "frameworkTags",
                        "customFields",
                        "testIds",
                        "evidenceIds",
                        "topics",
                    ]
                },
            )
            if err:
                return err
            c = resp.json()

            flags = c.get("flags", {})
            archived = c.get("archivedAt")
            scope = f"OUT OF SCOPE (since {archived})" if archived else "IN SCOPE"
            lines = [
                f"Control: [{c.get('code', '?')}] {c.get('name', 'Untitled')} (ID: {c.get('id')})",
                f"Scope: {scope}",
            ]
            if archived and c.get("rationale"):
                lines.append(f"Out-of-scope rationale: {c.get('rationale')}")
            lines += [
                f"Description: {c.get('description', 'N/A')}",
                f"Question: {c.get('question', 'N/A')}",
                f"Activity: {c.get('activity', 'N/A')}",
                f"Ready: {flags.get('isReady', 'N/A')} | "
                f"Has Evidence: {flags.get('hasEvidence', 'N/A')} | "
                f"Has Policy: {flags.get('hasPolicy', 'N/A')} | "
                f"Monitored: {flags.get('isMonitored', 'N/A')} | "
                f"Has Ticket: {flags.get('hasTicket', 'N/A')} | "
                f"Has Owner: {flags.get('hasOwner', 'N/A')}",
                f"Frameworks: {', '.join(c.get('frameworkTags', [])) or 'N/A'}",
                f"Control template ID: {c.get('controlTemplateId', 'N/A')} | Slug: {c.get('slug', 'N/A')}",
                f"Created: {c.get('createdAt', 'N/A')} | Updated: {c.get('updatedAt', 'N/A')}",
            ]

            test_ids = c.get("testIds")
            if isinstance(test_ids, dict):
                lines.append(
                    "Monitoring tests: "
                    f"{len(test_ids.get('passing') or [])} passing, "
                    f"{len(test_ids.get('failing') or [])} failing, "
                    f"{len(test_ids.get('monitored') or [])} monitored"
                )
            ev_ids = c.get("evidenceIds")
            if isinstance(ev_ids, dict):
                valid_ext = ev_ids.get("validExternalEvidenceIds") or []
                invalid_ext = ev_ids.get("invalidExternalEvidenceIds") or []
                lines.append(
                    "External evidence: "
                    f"{len(valid_ext)} valid, "
                    f"{len(invalid_ext)} invalid; "
                    f"reports: {len(ev_ids.get('validReportIds') or [])} valid, "
                    f"{len(ev_ids.get('invalidReportIds') or [])} invalid"
                )
                # Surface the external-evidence IDs (not just counts) so an agent
                # can act on them directly - e.g. delete an expired/orphaned
                # record via delete_drata_control_external_evidence, or inspect
                # the full set with list_drata_control_external_evidence.
                if valid_ext:
                    lines.append(f"  Valid external evidence IDs: {', '.join(str(i) for i in valid_ext)}")
                if invalid_ext:
                    lines.append(
                        "  Invalid external evidence IDs (expired/orphaned - "
                        f"deletable): {', '.join(str(i) for i in invalid_ext)}"
                    )
            topics = c.get("topics")
            if topics:
                lines.append(f"Topics: {', '.join(str(t) for t in topics)}")

            owners_data = c.get("owners", {})
            owners_list = owners_data.get("data", []) if isinstance(owners_data, dict) else []
            if owners_list:
                lines.append(f"\nOwners ({len(owners_list)}):")
                for o in owners_list:
                    lines.append(f"  {_full_name(o)} (id={o.get('id')}, {o.get('email', 'N/A')})")

            reqs = c.get("requirements", [])
            if reqs:
                lines.append(f"\nRequirements ({len(reqs)}):")
                for r in reqs:
                    oos = " [OUT OF SCOPE]" if r.get("archivedAt") else ""
                    lines.append(
                        f"  [{r.get('name', '?')}] "
                        f"{(r.get('description') or 'N/A')[:150]} "
                        f"(Framework: {r.get('frameworkName', 'N/A')}){oos}"
                    )

            custom_fields = c.get("customFields", [])
            if custom_fields:
                lines.append("\nCustom Fields:")
                for cf in custom_fields:
                    lines.append(f"  {cf.get('name', '?')}: {cf.get('value', 'N/A')}")

            return "\n".join(lines)

    async def create_drata_control(
        workspace_id: int,
        name: str,
        description: str,
        code: str = "",
        question: str = "",
        activity: str = "",
        owner_ids: str = "",
        requirement_ids: str = "",
        policy_ids: str = "",
        report_ids: str = "",
        test_ids: str = "",
    ) -> str:
        """Create a new custom compliance control.

        ``owner_ids`` / ``requirement_ids`` / ``policy_ids`` / ``report_ids`` /
        ``test_ids`` are CSV lists of numeric Drata IDs. A control's frameworks
        are derived from the requirements it is mapped to (there is no framework
        field on a control), so map ``requirement_ids`` to attach frameworks.
        """
        payload: dict[str, Any] = {"name": name, "description": description}
        if code:
            payload["code"] = code
        if question:
            payload["question"] = question
        if activity:
            payload["activity"] = activity
        # The v2 Create Control body takes ID arrays as flat numeric arrays (not
        # the ``[{"id": n}]`` object shape other Drata write endpoints use);
        # ``ownersIds`` specifically is rejected under the ``owners`` key.
        owners = _int_csv(owner_ids)
        if owners:
            payload["ownersIds"] = owners
        reqs = _int_csv(requirement_ids)
        if reqs:
            payload["requirementIds"] = reqs
        policies = _int_csv(policy_ids)
        if policies:
            payload["policyIds"] = policies
        reports = _int_csv(report_ids)
        if reports:
            payload["reportIds"] = reports
        tests = _int_csv(test_ids)
        if tests:
            payload["testIds"] = tests
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "post",
                f"{base_url}/workspaces/{workspace_id}/controls",
                _PROVIDER,
                "create control",
                headers=headers,
                json=payload,
            )
            if err:
                return err
            c = resp.json()
            return f"Control created: [{c.get('code', '?')}] {c.get('name')} (ID: {c.get('id')})"

    async def update_drata_control(
        workspace_id: int,
        control_id: int,
        name: str = "",
        description: str = "",
        question: str = "",
        activity: str = "",
        code: str = "",
        requirement_ids: str = "",
        policy_ids: str = "",
        report_ids: str = "",
        test_ids: str = "",
    ) -> str:
        """Update an existing control. Only provided fields are changed.

        ``requirement_ids`` / ``policy_ids`` / ``report_ids`` / ``test_ids`` are
        CSV lists of numeric Drata IDs and REPLACE the control's current mappings
        for that relation. To change scope (in / out of scope) use
        set_drata_control_scope; to change owners use set_drata_control_owners.
        """
        payload: dict[str, Any] = {}
        if name:
            payload["name"] = name
        if description:
            payload["description"] = description
        if question:
            payload["question"] = question
        if activity:
            payload["activity"] = activity
        if code:
            payload["code"] = code
        reqs = _int_csv(requirement_ids)
        if reqs:
            payload["requirementIds"] = reqs
        policies = _int_csv(policy_ids)
        if policies:
            payload["policyIds"] = policies
        reports = _int_csv(report_ids)
        if reports:
            payload["reportIds"] = reports
        tests = _int_csv(test_ids)
        if tests:
            payload["testIds"] = tests
        if not payload:
            return "Error: provide at least one field to update."
        async with egress_client(timeout=30) as client:
            _, err = await _common.safe_request(
                client,
                "put",
                f"{base_url}/workspaces/{workspace_id}/controls/{control_id}",
                _PROVIDER,
                "update control",
                headers=headers,
                json=payload,
            )
            if err:
                return err
            changed = ", ".join(sorted(payload.keys()))
            return f"Control {control_id} updated ({changed})."

    async def set_drata_control_scope(workspace_id: int, control_id: int, in_scope: str, rationale: str = "") -> str:
        """Mark a control in scope or out of scope (the Drata scope status).

        ``in_scope="true"`` restores the control to IN SCOPE (markInScope);
        ``in_scope="false"`` archives it as OUT OF SCOPE (markOutOfScope), which
        requires a ``rationale`` (business justification). This is the API behind
        the Controls "Status: In scope / Out of scope" filter — out-of-scope is an
        archived control.
        """
        want_in_scope = str(in_scope).strip().lower() == "true"
        if want_in_scope:
            payload: dict[str, Any] = {"action": "markInScope"}
        else:
            if not rationale.strip():
                return "Error: a rationale is required when marking a control out of scope (markOutOfScope)."
            payload = {"action": "markOutOfScope", "rationale": rationale.strip()}
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "post",
                f"{base_url}/workspaces/{workspace_id}/controls/{control_id}/actions",
                _PROVIDER,
                "set control scope",
                headers=headers,
                json=payload,
            )
            if err:
                return err
            # 200 = changed; 204 = already in the requested state (no-op).
            target = "in scope" if want_in_scope else "out of scope"
            if resp is not None and resp.status_code == 204:
                return f"Control {control_id} is already {target}; no change made."
            return f"Control {control_id} marked {target}."

    async def list_drata_control_requirements(
        workspace_id: int,
        control_id: int,
        framework_tag: str = "",
        framework_slug: str = "",
    ) -> str:
        """List the framework requirements mapped to a control.

        Filter by ``framework_tag`` (e.g. SOC_2, ISO27001 — CSV for several) or
        ``framework_slug`` (for custom frameworks).
        """
        params: dict[str, Any] = {}
        tags = [t.strip().upper() for t in (framework_tag or "").split(",") if t.strip()]
        if tags:
            params["frameworkTag[]"] = tags
        if framework_slug.strip():
            params["frameworkSlug"] = framework_slug.strip()
        async with egress_client(timeout=30) as client:
            items, err = await _common._paginated_fetch(
                client,
                f"{base_url}/workspaces/{workspace_id}/controls/{control_id}/requirements",
                headers,
                params,
            )
            if err:
                return err
            return format_entity_list(
                items,
                lambda r: (
                    f"[{r.get('name', '?')}] {(r.get('description') or 'N/A')[:160]} "
                    f"(Framework: {r.get('frameworkName', r.get('frameworkTag', 'N/A'))})"
                    + (" [OUT OF SCOPE]" if r.get("archivedAt") else "")
                ),
                noun="control requirement",
            )

    async def reset_drata_control_requirement_mappings(workspace_id: int, control_ids: str) -> str:
        """Reset controls to their template requirement mappings (max 100).

        ``control_ids`` is a CSV of numeric control IDs. Custom controls cannot be
        reset and are reported as failures.
        """
        ids = _int_csv(control_ids)
        if not ids:
            return "Error: provide at least one control ID."
        if len(ids) > 100:
            return "Error: at most 100 control IDs can be reset at once."
        async with egress_client(timeout=60) as client:
            resp, err = await _common.safe_request(
                client,
                "post",
                f"{base_url}/workspaces/{workspace_id}/controls/action-reset-mappings",
                _PROVIDER,
                "reset control requirement mappings",
                headers=headers,
                json={"controlIds": ids},
            )
            if err:
                return err
            data = resp.json() if resp.content else {}
            results = data.get("results") or []
            ok = [r for r in results if r.get("success")]
            failed = [r for r in results if not r.get("success")]
            lines = [f"Reset complete: {len(ok)} succeeded, {len(failed)} failed."]
            for r in failed:
                lines.append(f"  Control {r.get('controlId', '?')}: {r.get('error', 'failed')}")
            return "\n".join(lines)

    async def compare_drata_control_requirements(workspace_id: int, control_ids: str) -> str:
        """Compare controls' current requirement mappings vs. their template.

        ``control_ids`` is a CSV of numeric control IDs (max 300). Highlights
        where tenant mappings drift from the global template mappings.
        """
        ids = _int_csv(control_ids)
        if not ids:
            return "Error: provide at least one control ID."
        if len(ids) > 300:
            return "Error: at most 300 control IDs can be compared at once."
        async with egress_client(timeout=60) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{base_url}/workspaces/{workspace_id}/controls-requirement-comparison",
                _PROVIDER,
                "compare control requirements",
                headers=headers,
                params={"controlIds[]": ids},
            )
            if err:
                return err
            data = resp.json() if resp.content else {}
            rows = data.get("data") or []
            if not rows:
                return "No comparison data returned."

            def _names(reqs: list[dict] | None) -> str:
                return ", ".join(r.get("name", "?") for r in (reqs or [])) or "none"

            lines = [f"Requirement comparison for {len(rows)} control(s):"]
            for row in rows:
                lines.append(
                    f"  Control {row.get('controlId', '?')}: "
                    f"aligned=[{_names(row.get('alignedRequirements'))}] | "
                    f"user-mapped=[{_names(row.get('userMappedRequirements'))}] | "
                    f"template=[{_names(row.get('templateMappedRequirements'))}]"
                )
            return "\n".join(lines)

    def _note_path_ids(workspace_id: Any, control_id: Any) -> tuple[int, int] | str:
        """Coerce the two numeric path segments of a notes URL, or return an error.

        Both are interpolated raw into the request path. The schema types them
        as integers, but the tool-test endpoint bypasses jsonschema, so coerce
        (and refuse anything non-numeric) before building the URL.
        """
        ws = _coerce_int(workspace_id)
        cid = _coerce_int(control_id)
        if ws is None or ws < 0:
            return "Error: workspace_id must be a numeric Drata workspace ID. No request was sent to Drata."
        if cid is None or cid < 0:
            return "Error: control_id must be a numeric Drata control ID. No request was sent to Drata."
        return ws, cid

    async def list_drata_control_notes(workspace_id: int, control_id: int) -> str:
        """List the notes attached to a control.

        Returns, per note: the UUID ``id`` (the identifier
        ``update_drata_control_note`` / ``delete_drata_control_note`` take),
        the comment, the owner (Drata user id + email, via ``expand[]=owner``,
        the only expansion the endpoint accepts) and the created / updated
        timestamps. Follows Drata's cursor pagination until the last page, so
        the whole set is returned, not just the first page.
        """
        ids = _note_path_ids(workspace_id, control_id)
        if isinstance(ids, str):
            return ids
        ws, cid = ids
        async with egress_client(timeout=30) as client:
            items, err, truncated = await _common._paginated_fetch_all(
                client,
                f"{base_url}/workspaces/{ws}/controls/{cid}/notes",
                headers,
                {"expand[]": ["owner"]},
                action="list control notes",
                verbatim_errors=True,
            )
            if err:
                return err
            result = format_entity_list(items, _fmt_control_note, noun="control note")
            if truncated:
                result += f"\n\n(Showing the first {len(items)} notes on control {cid}; more exist.)"
            return result

    async def create_drata_control_note(workspace_id: int, control_id: int, comment: str) -> str:
        """Add a note to a control (the note is owned by this API key's Drata user)."""
        ids = _note_path_ids(workspace_id, control_id)
        if isinstance(ids, str):
            return ids
        ws, cid = ids
        async with egress_client(timeout=30) as client:
            resp, err = await _common._request_verbatim(
                client,
                "post",
                f"{base_url}/workspaces/{ws}/controls/{cid}/notes",
                "create control note",
                headers=headers,
                json={"comment": comment},
            )
            if err:
                return err
            data = resp.json() if resp is not None and resp.content else {}
            data = data if isinstance(data, dict) else {}
            owner = f" (owner id={data['ownerId']})" if data.get("ownerId") is not None else ""
            return f"Note created (ID: {data.get('id', '?')}) on control {cid}{owner}."

    async def update_drata_control_note(workspace_id: int, control_id: int, note_id: str, comment: str) -> str:
        """Update an existing control note; ``note_id`` is the note's UUID.

        Only the note's owner (the Drata user behind this integration's API
        key) may update it; Drata answers 403 for any other identity, which is
        reported as an owner mismatch.
        """
        ids = _note_path_ids(workspace_id, control_id)
        if isinstance(ids, str):
            return ids
        ws, cid = ids
        ref = _note_ref(note_id)
        if not ref:
            return _NOTE_ID_ERROR
        async with egress_client(timeout=30) as client:
            resp, err = await _common._request_verbatim(
                client,
                "put",
                f"{base_url}/workspaces/{ws}/controls/{cid}/notes/{ref}",
                "update control note",
                headers=headers,
                json={"comment": comment},
            )
            if err:
                return _note_owner_mismatch_error(resp, "update", ref, cid) or err
            return f"Note {ref} updated on control {cid}."

    async def delete_drata_control_note(workspace_id: int, control_id: int, note_id: str) -> str:
        """Delete a control note; ``note_id`` is the note's UUID. PERMANENT.

        Only the note's owner (the Drata user behind this integration's API
        key) may delete it; Drata answers 403 for any other identity, which is
        reported as an owner mismatch.
        """
        ids = _note_path_ids(workspace_id, control_id)
        if isinstance(ids, str):
            return ids
        ws, cid = ids
        ref = _note_ref(note_id)
        if not ref:
            return _NOTE_ID_ERROR
        async with egress_client(timeout=30) as client:
            resp, err = await _common._request_verbatim(
                client,
                "delete",
                f"{base_url}/workspaces/{ws}/controls/{cid}/notes/{ref}",
                "delete control note",
                headers=headers,
            )
            if err:
                return _note_owner_mismatch_error(resp, "delete", ref, cid) or err
            return f"Note {ref} deleted from control {cid}."

    async def list_drata_control_owners(workspace_id: int, control_id: int) -> str:
        """List owners assigned to a control."""
        async with egress_client(timeout=30) as client:
            items, err = await _common._paginated_fetch(
                client,
                f"{base_url}/workspaces/{workspace_id}/controls/{control_id}/owners",
                headers,
                {},
            )
            if err:
                return err
            return format_entity_list(
                items,
                lambda o: f"[{o.get('id')}] {_full_name(o)} <{o.get('email', 'N/A')}>",
                noun="owner",
            )

    async def add_drata_control_owner(workspace_id: int, control_id: int, user_id: int) -> str:
        """Add an owner to a control.

        The v2 ``Add Control Owner`` endpoint requires the owner's Drata user ID
        in an ``ownerId`` field and rejects anything else with a strict
        whitelist (``property userId should not exist``). ``user_id`` stays the
        flat, LLM-facing argument; it is mapped to ``ownerId`` on the wire.
        """
        async with egress_client(timeout=30) as client:
            _, err = await _common.safe_request(
                client,
                "post",
                f"{base_url}/workspaces/{workspace_id}/controls/{control_id}/owners",
                _PROVIDER,
                "add control owner",
                headers=headers,
                json={"ownerId": user_id},
            )
            if err:
                return err
            return f"User {user_id} added as owner of control {control_id}."

    async def remove_drata_control_owner(workspace_id: int, control_id: int, user_id: int) -> str:
        """Remove an owner from a control."""
        async with egress_client(timeout=30) as client:
            _, err = await _common.safe_request(
                client,
                "delete",
                f"{base_url}/workspaces/{workspace_id}/controls/{control_id}/owners/{user_id}",
                _PROVIDER,
                "remove control owner",
                headers=headers,
            )
            if err:
                return err
            return f"User {user_id} removed from owners of control {control_id}."

    async def set_drata_control_owners(
        workspace_id: int = 1,
        assignments_json: str = "",
    ) -> str:
        """Assign (and optionally remove) owners across MANY controls in ONE call.

        Use this instead of calling add/remove_drata_control_owner once per
        control: it processes a whole batch server-side and returns a compact
        summary, so re-owning hundreds of controls does not explode the agent
        loop into hundreds of iterations or balloon the conversation context.

        ``assignments_json`` is a JSON array of objects, each:

            {"control_id": int,
             "owner_id": int,                 # optional — owner to add
             "remove_owner_ids": [int, ...]}  # optional — owners to remove first

        For each item the IDs in ``remove_owner_ids`` are removed first, then
        ``owner_id`` (when given) is added. To enforce a SINGLE owner per
        control, pass the desired ``owner_id`` and list every OTHER current
        owner in ``remove_owner_ids`` (owner IDs are returned inline by
        ``list_drata_controls``). Cap: 100 items per call — re-call with the
        remainder if you have more.
        """
        try:
            raw = json.loads(assignments_json or "[]")
        except json.JSONDecodeError as exc:
            return f"Error: assignments_json is not valid JSON ({exc})."
        except TypeError:
            # A non-string assignments_json (e.g. the model passed the array or
            # object directly instead of a JSON string) makes json.loads raise
            # TypeError, not JSONDecodeError. Not every execution path coerces
            # tool args before the handler - the integration tool-test endpoint
            # dispatches straight to the executor, bypassing the agent loop's
            # jsonschema pre-validation - so return a controlled error here
            # instead of leaking a raw exception as the tool result.
            return (
                "Error: assignments_json must be a JSON string containing an "
                f"array of objects (got {type(assignments_json).__name__})."
            )
        if not isinstance(raw, list):
            return "Error: assignments_json must be a JSON array of objects."
        if not raw:
            return "Error: assignments_json is empty — nothing to do."

        max_batch = 100
        batch = raw[:max_batch]
        remainder = max(0, len(raw) - max_batch)

        assigned = 0
        removed = 0
        skipped = 0
        errors: list[str] = []

        async with egress_client(timeout=60) as client:
            for item in batch:
                if not isinstance(item, dict):
                    if len(errors) < 12:
                        errors.append(f"skipped non-object item: {str(item)[:60]}")
                    continue
                cid = _coerce_int(item.get("control_id"))
                if cid is None:
                    if len(errors) < 12:
                        errors.append(f"missing/invalid control_id: {str(item)[:60]}")
                    continue
                owner = _coerce_int(item.get("owner_id"))
                # Only a missing/null field means "no removals". A present but
                # wrong-typed value (a bare int, a string, or even a falsey one
                # like 0 or "") must NOT be coerced to [] by ``or`` - it has to
                # reach the list validation below and be recorded as an error
                # rather than silently skipping validation.
                remove_raw = item.get("remove_owner_ids")
                if remove_raw is None:
                    remove_raw = []
                # A non-list remove_owner_ids (a bare int, a string, ...) must not
                # abort the whole batch or be iterated character-by-character;
                # record an error for this item and continue with the rest.
                if not isinstance(remove_raw, list):
                    if len(errors) < 12:
                        errors.append(
                            f"control {cid}: remove_owner_ids must be a JSON array "
                            f"of user IDs, got {type(remove_raw).__name__}"
                        )
                    continue
                remove_ids = [rid for rid in (_coerce_int(x) for x in remove_raw) if rid is not None]

                for rid in remove_ids:
                    # Don't remove the owner we're about to keep/set.
                    if owner is not None and rid == owner:
                        continue
                    _, err = await _common.safe_request(
                        client,
                        "delete",
                        f"{base_url}/workspaces/{workspace_id}/controls/{cid}/owners/{rid}",
                        _PROVIDER,
                        "remove control owner",
                        headers=headers,
                    )
                    if err:
                        if len(errors) < 12:
                            errors.append(f"control {cid} remove owner {rid}: {err[:120]}")
                    else:
                        removed += 1

                if owner is not None:
                    _, err = await _common.safe_request(
                        client,
                        "post",
                        f"{base_url}/workspaces/{workspace_id}/controls/{cid}/owners",
                        _PROVIDER,
                        "add control owner",
                        headers=headers,
                        json={"ownerId": owner},
                    )
                    if err:
                        # Treat "already an owner" as idempotent success.
                        if any(s in err.lower() for s in ("already", "exist", "duplicate")):
                            skipped += 1
                        elif len(errors) < 12:
                            errors.append(f"control {cid} add owner {owner}: {err[:120]}")
                    else:
                        assigned += 1

        parts = [
            f"Bulk owner update on {len(batch)} control(s): "
            f"{assigned} owner(s) assigned, {removed} owner(s) removed, "
            f"{skipped} already-owned (skipped), {len(errors)} error(s)."
        ]
        if errors:
            parts.append("Errors: " + " | ".join(errors[:12]))
        if remainder:
            parts.append(
                f"[Processed {len(batch)} of {len(raw)} items — re-call with the remaining {remainder} item(s).]"
            )
        return "\n".join(parts)

    # ── External evidence (Drata v1 API) ─────────────────────────────────
    # External evidence is a SEPARATE object family from the v2 Evidence
    # Library: records mapped directly to a control (what get_drata_control
    # counts under "external evidence"). The management endpoints only exist on
    # the v1 API host, so derive it from the configured v2 base URL. This is the
    # surface for cleaning up evidence left dangling on controls after the
    # Evidence Library was purged - the two are different object types, so
    # deleting library items never removes these.
    base_url_v1 = _common.to_v1_base_url(base_url)

    async def list_drata_control_external_evidence(control_id: int, limit: int = 0) -> str:
        """List external evidence mapped to a control (v1 API).

        External evidence is DISTINCT from the v2 Evidence Library - these
        records are attached directly to the control and are what
        ``get_drata_control`` reports under "external evidence". Use this to get
        each record's ID (needed to remove an expired/orphaned one via
        ``delete_drata_control_external_evidence``), its name, file/url, renewal
        date, and whether it is expired. ``control_id`` is the numeric control ID.
        """
        cid = _coerce_int(control_id)
        if not cid:
            return "Error: control_id (numeric Drata control ID) is required."
        max_items = limit if limit and limit > 0 else 500
        async with egress_client(timeout=30) as client:
            items, err = await _common._paginated_fetch_pages(
                client,
                f"{base_url_v1}/controls/{cid}/external-evidence",
                headers,
                {},
                max_items=max_items,
            )
            if err:
                return err
            return format_entity_list(
                items,
                lambda e: (
                    f"[{e.get('id')}] {e.get('name', 'Unnamed')} - "
                    f"{'EXPIRED' if e.get('isExpired') else 'valid'} | "
                    f"Renewal: {e.get('renewalDate', 'N/A')} "
                    f"({e.get('renewalScheduleType', 'N/A')}) | "
                    + (f"File: {e['file']}" if e.get("file") else f"URL: {e.get('url') or 'N/A'}")
                    + f" | Created: {e.get('createdAt', 'N/A')}"
                ),
                noun="external evidence record",
            )

    async def get_drata_external_evidence_download(external_evidence_id: int) -> str:
        """Get a signed download URL for one external-evidence record (v1 API).

        ``external_evidence_id`` is the record ID from
        ``list_drata_control_external_evidence`` (or shown by ``get_drata_control``).
        """
        eid = _coerce_int(external_evidence_id)
        if not eid:
            return "Error: external_evidence_id is required."
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{base_url_v1}/external-evidence/{eid}/download",
                _PROVIDER,
                "get external evidence download URL",
                headers=headers,
            )
            if err:
                return err
            data = resp.json() if resp.content else {}
            signed = data.get("signedUrl") if isinstance(data, dict) else ""
            if signed:
                return f"Signed download URL for external evidence {eid}:\n{signed}"
            return f"External evidence {eid} returned no signed download URL."

    async def delete_drata_control_external_evidence(external_evidence_id: int) -> str:
        """Remove (detach) one external-evidence record by ID (v1 API). PERMANENT.

        This is the cleanup path for external evidence left mapped to a control -
        e.g. expired/orphaned records that linger after the Evidence Library was
        purged (a different object family, so deleting library items does NOT
        remove them). Find the IDs first with ``list_drata_control_external_evidence``
        or ``get_drata_control`` (it lists the invalid/expired IDs). This cannot
        be undone.
        """
        eid = _coerce_int(external_evidence_id)
        if not eid:
            return "Error: external_evidence_id is required."
        async with egress_client(timeout=30) as client:
            _, err = await _common.safe_request(
                client,
                "delete",
                f"{base_url_v1}/external-evidence/{eid}",
                _PROVIDER,
                "delete external evidence",
                headers=headers,
            )
            if err:
                return err
            return f"External evidence {eid} deleted (detached from its control)."

    async def upload_drata_control_external_evidence(
        workspace_id: int,
        control_id: int,
        name: str = "",
        description: str = "",
        file_content: str = "",
        file_name: str = "",
        file_encoding: str = "text",
        source_url: str = "",
        source_file_id: str = "",
        creation_date: str = "",
        renewal_date: str = "",
        renewal_schedule_type: str = "ONE_YEAR",
        file_context: FileContext | None = None,
    ) -> str:
        """Map a new external-evidence artifact to a control (v1 API).

        External evidence is mapped directly to a control, distinct from the v2
        Evidence Library. Provide EXACTLY ONE artifact source:

        * ``source_file_id`` - a file the user uploaded to the conversation,
          forwarded byte-for-byte.
        * ``file_content`` - Markdown/HTML rendered to a PDF (``.pdf`` default) or
          DOCX, uploaded verbatim for text types, or base64 when
          ``file_encoding='base64'``.
        * ``source_url`` - link an external artifact by URL instead.

        ``renewal_schedule_type`` defaults to ONE_YEAR; ``creation_date`` defaults
        to today; ``renewal_date`` is derived from the schedule when omitted
        (required for CUSTOM).
        """
        cid = _coerce_int(control_id)
        if not cid:
            return "Error: control_id (numeric Drata control ID) is required."
        name = (name or "").strip()
        sources = [bool(file_content), bool(source_url.strip()), bool(source_file_id.strip())]
        if sum(sources) != 1:
            return "Error: provide exactly one artifact source - file_content, source_url, or source_file_id."
        schedule = (renewal_schedule_type or "ONE_YEAR").strip().upper()
        if schedule not in DRATA_RENEWAL_SCHEDULE_TYPES:
            return "Error: renewal_schedule_type must be one of " + ", ".join(sorted(DRATA_RENEWAL_SCHEDULE_TYPES))
        created = (creation_date or "").strip() or datetime.now(UTC).date().isoformat()
        renewal = (renewal_date or "").strip()
        if not renewal:
            if schedule == "CUSTOM":
                return "Error: renewal_date is required when renewal_schedule_type is CUSTOM."
            renewal = _derive_renewal_date(created, schedule)
        url = f"{base_url_v1}/workspaces/{workspace_id}/controls/{cid}/external-evidence"
        data: dict[str, Any] = {
            "creationDate": created,
            "renewalDate": renewal,
            "renewalScheduleType": schedule,
        }
        if description:
            data["description"] = description

        # URL artifact: no binary upload. Post the metadata as JSON - the v1
        # endpoint accepts application/json (the Content-Type set by _headers),
        # so send it with json= rather than data=. A form-encoded body under a
        # JSON Content-Type would be mis-parsed by Drata. Mirrors the v2
        # Evidence Library URL-artifact path.
        if source_url.strip():
            data["url"] = source_url.strip()
            if name:
                data["filename"] = name
            async with egress_client(timeout=30) as client:
                _, err = await _common.safe_request(
                    client,
                    "post",
                    url,
                    _PROVIDER,
                    "upload external evidence",
                    headers=headers,
                    json=data,
                )
                if err:
                    return err
            return f"External evidence linked to control {cid} (URL artifact)."

        # File artifact: a user-uploaded file forwarded as-is, or agent-authored
        # content rendered/encoded to bytes - same pipeline as the Evidence
        # Library, but with the narrower external-evidence file-type set.
        if source_file_id.strip():
            resolved = await _resolve_uploaded_evidence_file(
                file_context,
                source_file_id.strip(),
                file_name,
                accepted_extensions=_EXTERNAL_EVIDENCE_FILE_EXTENSIONS,
            )
        else:
            resolved = await _render_evidence_file(
                file_content,
                file_name,
                file_encoding,
                fallback_name=name or f"control-{cid}-external-evidence",
                accepted_extensions=_EXTERNAL_EVIDENCE_FILE_EXTENSIONS,
            )
        if isinstance(resolved, str):
            return resolved
        file_bytes, resolved_name, mime = resolved
        data["filename"] = name or resolved_name
        files = {"file": (resolved_name, file_bytes, mime)}
        async with egress_client(timeout=120) as client:
            _, err = await _common.safe_request(
                client,
                "post",
                url,
                _PROVIDER,
                "upload external evidence",
                headers=upload_headers,
                data=data,
                files=files,
            )
            if err:
                return err
        return uploaded_file_answer(
            f"External evidence uploaded to control {cid}.", target=_PROVIDER, filename=resolved_name
        )

    callable_map = {
        "list_drata_controls": list_drata_controls,
        "get_drata_control": get_drata_control,
        "create_drata_control": create_drata_control,
        "update_drata_control": update_drata_control,
        "set_drata_control_scope": set_drata_control_scope,
        "list_drata_control_requirements": list_drata_control_requirements,
        "reset_drata_control_requirement_mappings": reset_drata_control_requirement_mappings,
        "compare_drata_control_requirements": compare_drata_control_requirements,
        "list_drata_control_notes": list_drata_control_notes,
        "create_drata_control_note": create_drata_control_note,
        "update_drata_control_note": update_drata_control_note,
        "delete_drata_control_note": delete_drata_control_note,
        "list_drata_control_owners": list_drata_control_owners,
        "add_drata_control_owner": add_drata_control_owner,
        "remove_drata_control_owner": remove_drata_control_owner,
        "set_drata_control_owners": set_drata_control_owners,
        "list_drata_control_external_evidence": list_drata_control_external_evidence,
        "get_drata_external_evidence_download": get_drata_external_evidence_download,
        "delete_drata_control_external_evidence": delete_drata_control_external_evidence,
        "upload_drata_control_external_evidence": upload_drata_control_external_evidence,
    }
    for _name, _fn in callable_map.items():
        executor.register(_name, _fn)

    return [
        ToolDefinition(
            name="list_drata_controls",
            description=(
                "List compliance controls in a Drata workspace. Filter by scope "
                "with is_archived (false = in scope, true = out of scope); scope "
                "is shown on every row. Other server filters: is_ready, "
                "has_evidence, has_policy, has_passing_test, is_monitored, "
                "is_enabled, policy_id, ticket_status. Client filters for focused "
                "bulk work: framework_tag / without_owner / owner_id / compact / "
                "limit. Owner Drata user IDs are returned inline."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "integer", "default": 1},
                    "is_archived": {
                        "type": "string",
                        "enum": ["true", "false"],
                        "description": "Scope: false = in-scope only, true = out-of-scope (archived) only.",
                    },
                    "is_ready": {"type": "string", "enum": ["true", "false"]},
                    "has_evidence": {"type": "string", "enum": ["true", "false"]},
                    "has_policy": {"type": "string", "enum": ["true", "false"]},
                    "has_passing_test": {"type": "string", "enum": ["true", "false"]},
                    "is_monitored": {"type": "string", "enum": ["true", "false"]},
                    "is_enabled": {"type": "string", "enum": ["true", "false"]},
                    "policy_id": {
                        "type": "integer",
                        "description": "Only controls mapped to this policy ID.",
                    },
                    "ticket_status": {
                        "type": "string",
                        "enum": ["IN_PROGRESS", "ARCHIVED"],
                        "description": "Only controls with a ticket in this status.",
                    },
                    "framework_tag": {
                        "type": "string",
                        "description": "Framework tag (e.g. SOC_2, ISO27001, HIPAA, PCI, GDPR, NIST80053, CMMC)",
                    },
                    "without_owner": {
                        "type": "string",
                        "enum": ["true", "false"],
                        "description": "Only controls with no owner (the ownerless work queue).",
                    },
                    "owner_id": {
                        "type": "integer",
                        "description": "Only controls owned by this Drata user ID.",
                    },
                    "compact": {
                        "type": "string",
                        "enum": ["true", "false"],
                        "description": "Terse one-line-per-control output (smaller payload).",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Cap the number of controls returned (0 = no cap).",
                    },
                },
                "required": [],
            },
            source="integration:drata",
            priority=0,
        ),
        ToolDefinition(
            name="get_drata_control",
            description="Get full details of a specific Drata control: scope (in/out of scope), readiness flags, owners, requirements, monitoring-test and evidence counts, topics, and custom fields. control_id may be a numeric ID or a code prefixed with 'code:' (e.g. code:DCF-542).",
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "integer", "default": 1},
                    "control_id": {
                        "type": "string",
                        "description": "Numeric control ID, or a code like 'code:DCF-542'",
                    },
                },
                "required": ["control_id"],
            },
            source="integration:drata",
            priority=0,
        ),
        ToolDefinition(
            name="create_drata_control",
            description="Create a new custom compliance control. owner_ids / requirement_ids / policy_ids / report_ids / test_ids are CSV lists of numeric IDs. Map requirement_ids to attach frameworks (controls have no framework field).",
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "integer", "default": 1},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "code": {"type": "string", "description": "Optional code like 'AC-1'"},
                    "question": {"type": "string"},
                    "activity": {"type": "string"},
                    "owner_ids": {
                        "type": "string",
                        "description": "CSV of numeric Drata user IDs",
                    },
                    "requirement_ids": {"type": "string"},
                    "policy_ids": {"type": "string"},
                    "report_ids": {"type": "string"},
                    "test_ids": {"type": "string"},
                },
                "required": ["workspace_id", "name", "description"],
            },
            source="integration:drata",
            priority=1,
        ),
        ToolDefinition(
            name="update_drata_control",
            description="Update an existing control. Only provided fields change; requirement_ids / policy_ids / report_ids / test_ids (CSV numeric IDs) REPLACE that mapping. Use set_drata_control_scope for scope and set_drata_control_owners for owners.",
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "integer"},
                    "control_id": {"type": "integer"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "question": {"type": "string"},
                    "activity": {"type": "string"},
                    "code": {"type": "string"},
                    "requirement_ids": {"type": "string"},
                    "policy_ids": {"type": "string"},
                    "report_ids": {"type": "string"},
                    "test_ids": {"type": "string"},
                },
                "required": ["workspace_id", "control_id"],
            },
            source="integration:drata",
            priority=1,
        ),
        ToolDefinition(
            name="set_drata_control_scope",
            description=(
                "Mark a control in scope or out of scope (the Controls 'Status: "
                "In scope / Out of scope' status). in_scope='true' restores it "
                "(markInScope); in_scope='false' archives it as out of scope "
                "(markOutOfScope) and requires a rationale."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "integer"},
                    "control_id": {"type": "integer"},
                    "in_scope": {"type": "string", "enum": ["true", "false"]},
                    "rationale": {
                        "type": "string",
                        "description": "Required when marking out of scope.",
                    },
                },
                "required": ["workspace_id", "control_id", "in_scope"],
            },
            source="integration:drata",
            priority=1,
        ),
        ToolDefinition(
            name="list_drata_control_requirements",
            description="List the framework requirements mapped to a control. Filter by framework_tag (CSV) or framework_slug (custom frameworks).",
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "integer"},
                    "control_id": {"type": "integer"},
                    "framework_tag": {"type": "string"},
                    "framework_slug": {"type": "string"},
                },
                "required": ["workspace_id", "control_id"],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="reset_drata_control_requirement_mappings",
            description="Reset controls to their template requirement mappings (control_ids = CSV of numeric IDs, max 100). Custom controls cannot be reset.",
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "integer"},
                    "control_ids": {"type": "string"},
                },
                "required": ["workspace_id", "control_ids"],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="compare_drata_control_requirements",
            description="Compare controls' current requirement mappings against their template mappings (control_ids = CSV of numeric IDs, max 300).",
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "integer"},
                    "control_ids": {"type": "string"},
                },
                "required": ["workspace_id", "control_ids"],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="list_drata_control_notes",
            description=(
                "List the notes attached to a Drata control. Returns each note's "
                "UUID id (the note_id that update/delete_drata_control_note take), "
                "comment, owner (Drata user id + email) and created/updated "
                "timestamps, across every page."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "integer"},
                    "control_id": {"type": "integer"},
                },
                "required": ["workspace_id", "control_id"],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="create_drata_control_note",
            description=(
                "Add a note to a Drata control (comment up to 191 characters). Returns the new note's UUID id."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "integer"},
                    "control_id": {"type": "integer"},
                    "comment": {"type": "string"},
                },
                "required": ["workspace_id", "control_id", "comment"],
            },
            source="integration:drata",
            priority=1,
        ),
        ToolDefinition(
            name="update_drata_control_note",
            description=(
                "Update an existing control note by its UUID note_id (from "
                "list_drata_control_notes). Only the note's owner - the Drata "
                "user behind this integration's API key - can update it; Drata "
                "returns 403 otherwise."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "integer"},
                    "control_id": {"type": "integer"},
                    "note_id": {
                        "type": "string",
                        "description": (
                            "The note's UUID, e.g. 81584418-17a7-44b1-b0f9-47d42b903812 "
                            "(as returned by list_drata_control_notes)."
                        ),
                    },
                    "comment": {"type": "string"},
                },
                "required": ["workspace_id", "control_id", "note_id", "comment"],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="delete_drata_control_note",
            description=(
                "Delete a control note by its UUID note_id (from "
                "list_drata_control_notes). Only the note's owner - the Drata "
                "user behind this integration's API key - can delete it; Drata "
                "returns 403 otherwise. This is unrecoverable."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "integer"},
                    "control_id": {"type": "integer"},
                    "note_id": {
                        "type": "string",
                        "description": (
                            "The note's UUID, e.g. 81584418-17a7-44b1-b0f9-47d42b903812 "
                            "(as returned by list_drata_control_notes)."
                        ),
                    },
                },
                "required": ["workspace_id", "control_id", "note_id"],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="list_drata_control_owners",
            description="List owners assigned to a Drata control.",
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "integer"},
                    "control_id": {"type": "integer"},
                },
                "required": ["workspace_id", "control_id"],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="add_drata_control_owner",
            description="Add a user as owner of a Drata control.",
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "integer"},
                    "control_id": {"type": "integer"},
                    "user_id": {"type": "integer"},
                },
                "required": ["workspace_id", "control_id", "user_id"],
            },
            source="integration:drata",
            priority=1,
        ),
        ToolDefinition(
            name="remove_drata_control_owner",
            description="Remove a user from the owners of a Drata control.",
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "integer"},
                    "control_id": {"type": "integer"},
                    "user_id": {"type": "integer"},
                },
                "required": ["workspace_id", "control_id", "user_id"],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="set_drata_control_owners",
            description=(
                "Bulk-assign (and optionally remove) owners across MANY Drata "
                "controls in one call. Use this instead of add/remove_drata_"
                "control_owner per control when re-owning many controls — it "
                "returns a compact summary and avoids hundreds of loop "
                "iterations. To enforce a single owner, pass the desired "
                "owner_id and list the other current owners in remove_owner_ids."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "integer", "default": 1},
                    "assignments_json": {
                        "type": "string",
                        "description": (
                            "JSON array of objects: "
                            '[{"control_id": int, "owner_id": int, '
                            '"remove_owner_ids": [int, ...]}]. owner_id and '
                            "remove_owner_ids are optional. Max 100 items per call."
                        ),
                    },
                },
                "required": ["assignments_json"],
            },
            source="integration:drata",
            priority=1,
        ),
        # ── External evidence (v1 API) ──
        ToolDefinition(
            name="list_drata_control_external_evidence",
            description=(
                "List external evidence mapped to a Drata control. External "
                "evidence is a DISTINCT object family from the Evidence Library - "
                "these records attach directly to the control and are what "
                "get_drata_control counts as 'external evidence'. Returns each "
                "record's ID, name, file/url, renewal date and expiry status. Use "
                "the IDs to remove expired/orphaned records via "
                "delete_drata_control_external_evidence."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "control_id": {"type": "integer", "description": "Numeric Drata control ID"},
                    "limit": {
                        "type": "integer",
                        "description": "Max records to return (default 500).",
                    },
                },
                "required": ["control_id"],
            },
            source="integration:drata",
            priority=0,
        ),
        ToolDefinition(
            name="get_drata_external_evidence_download",
            description=(
                "Get a signed download URL for a single external-evidence record "
                "(by its ID from list_drata_control_external_evidence or "
                "get_drata_control)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "external_evidence_id": {"type": "integer"},
                },
                "required": ["external_evidence_id"],
            },
            source="integration:drata",
            priority=1,
        ),
        ToolDefinition(
            name="delete_drata_control_external_evidence",
            description=(
                "Permanently remove (detach) ONE external-evidence record from its "
                "control by ID. This is the cleanup path for evidence left mapped "
                "to controls after the Evidence Library was purged - they are a "
                "different object family, so deleting Evidence Library items does "
                "NOT remove them. Find IDs with list_drata_control_external_evidence "
                "or get_drata_control (it lists invalid/expired IDs). Cannot be undone."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "external_evidence_id": {"type": "integer"},
                },
                "required": ["external_evidence_id"],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="upload_drata_control_external_evidence",
            description=(
                "Map a new external-evidence artifact to a control (distinct from "
                "the Evidence Library). Provide exactly ONE source: source_file_id "
                "(a file the user uploaded, forwarded byte-for-byte), file_content "
                "(Markdown/HTML rendered to PDF/DOCX, or base64 with "
                "file_encoding='base64'), or source_url. renewal_schedule_type "
                "defaults to ONE_YEAR; creation_date defaults to today; "
                "renewal_date is derived from the schedule when omitted."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "integer", "default": 1},
                    "control_id": {"type": "integer", "description": "Numeric Drata control ID"},
                    "name": {"type": "string", "description": "Artifact display name / filename"},
                    "description": {"type": "string"},
                    "file_content": {
                        "type": "string",
                        "description": "Artifact content (Markdown/HTML rendered to PDF/DOCX, text, or base64).",
                    },
                    "file_name": {
                        "type": "string",
                        "description": "Artifact file name incl. extension (e.g. 'evidence.pdf').",
                    },
                    "file_encoding": {
                        "type": "string",
                        "enum": ["text", "base64"],
                        "default": "text",
                    },
                    "source_url": {"type": "string", "description": "Link an external artifact by URL."},
                    "source_file_id": {
                        "type": "string",
                        "description": "file_id of a file the user uploaded to this conversation.",
                    },
                    "creation_date": {
                        "type": "string",
                        "description": "ISO date the evidence was created (default today).",
                    },
                    "renewal_date": {
                        "type": "string",
                        "description": "ISO renewal date (required for CUSTOM schedule).",
                    },
                    "renewal_schedule_type": {
                        "type": "string",
                        "enum": sorted(DRATA_RENEWAL_SCHEDULE_TYPES),
                        "default": "ONE_YEAR",
                    },
                },
                "required": ["workspace_id", "control_id"],
            },
            source="integration:drata",
            priority=1,
        ),
    ]
