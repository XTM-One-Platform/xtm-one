"""Drata vendors tools - Vendors, documents, security reviews (internal SOC assessments), and questionnaires."""

from __future__ import annotations

import json
from typing import Any

from app.core.egress import egress_client
from app.integrations.file_delivery import uploaded_file_answer
from app.integrations.http import format_entity_list
from app.tools.base import CallableToolExecutor, FileContext, ToolDefinition

from . import _common
from ._common import (
    _PROVIDER,
    _VENDOR_DOCUMENT_TYPES,
    _VENDOR_SR_ACTIONS,
    _VENDOR_SR_DECISIONS,
    _VENDOR_SR_STATUSES,
    _VENDOR_SR_TYPES,
    _VENDOR_STATS_SCOPES,
    _coerce_int,
    _coerce_str,
    _empty,
    _full_name,
    _render_evidence_file,
    _resolve_uploaded_evidence_file,
)


def register(
    base_url: str,
    headers: dict[str, str],
    upload_headers: dict[str, str],
    executor: CallableToolExecutor,
) -> list[ToolDefinition]:
    """Build the Drata vendors tools and register their callables."""

    async def list_drata_vendors(status: str = "", risk: str = "", category: str = "") -> str:
        """List vendors/third-parties. Optionally filter by status, risk level, or category."""
        async with egress_client(timeout=30) as client:
            params: dict[str, Any] = {"expand[]": ["vendorUser", "latestSecurityReviews"]}
            if status:
                params["status"] = status
            if risk:
                params["risk"] = risk
            if category:
                params["category"] = category
            items, err = await _common._paginated_fetch(client, f"{base_url}/vendors", headers, params)
            if err:
                return err

            def _fmt(v: dict) -> str:
                reviews = v.get("latestSecurityReviews", [])
                review_status = reviews[0].get("decision", "N/A") if reviews else "No review"
                return (
                    f"[{v.get('id')}] {v.get('name', 'Unnamed')} — "
                    f"Status: {v.get('status', 'N/A')} | "
                    f"Risk: {v.get('risk', 'N/A')} | "
                    f"Category: {v.get('category', 'N/A')} | "
                    f"Type: {v.get('type', 'N/A')} | "
                    f"Critical: {v.get('critical', False)} | "
                    f"Impact: {v.get('impactLevel', 'N/A')} | "
                    f"Last Review: {review_status}"
                )

            return format_entity_list(items, _fmt, noun="vendor")

    async def get_drata_vendor(vendor_id: int) -> str:
        """Get full details of a specific vendor including documents and reviews."""
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{base_url}/vendors/{vendor_id}",
                _PROVIDER,
                "get vendor",
                headers=headers,
                params={
                    "expand[]": [
                        "vendorUser",
                        "documents",
                        "latestSecurityReviews",
                        "customFields",
                    ]
                },
            )
            if err:
                return err
            v = resp.json()

            lines = [
                f"Vendor: {v.get('name', 'Unnamed')} (ID: {v.get('id')})",
                f"Status: {v.get('status', 'N/A')}",
                f"Risk: {v.get('risk', 'N/A')}",
                f"Category: {v.get('category', 'N/A')}",
                f"Type: {v.get('type', 'N/A')}",
                f"Critical: {v.get('critical', False)}",
                f"Impact Level: {v.get('impactLevel', 'N/A')}",
                f"Operational Impact: {v.get('operationalImpact', 'N/A')}",
                f"URL: {v.get('url', 'N/A')}",
                f"Location: {v.get('location', 'N/A')}",
                f"Services Provided: {v.get('servicesProvided', 'N/A')}",
                f"Data Stored: {v.get('dataStored', 'N/A')}",
                f"Has PII: {v.get('hasPii', 'N/A')}",
                f"MFA Enabled: {v.get('passwordMfaEnabled', 'N/A')}",
                f"Contact: {v.get('contactAtVendor', 'N/A')} ({v.get('contactsEmail', 'N/A')})",
                f"Renewal: {v.get('renewalDate', 'N/A')} ({v.get('renewalScheduleType', 'N/A')})",
                f"Notes: {v.get('notes', 'N/A')}",
                f"Created: {v.get('createdAt', 'N/A')}",
            ]

            reviews = v.get("latestSecurityReviews", [])
            if reviews:
                lines.append(f"\nSecurity Reviews ({len(reviews)}):")
                for r in reviews:
                    lines.append(
                        f"  [{r.get('type', '?')}] Decision: {r.get('decision', 'N/A')} | "
                        f"Status: {r.get('status', 'N/A')} | "
                        f"Requested: {r.get('requestedAt', 'N/A')}"
                    )

            docs = v.get("documents", [])
            if docs:
                lines.append(f"\nDocuments ({len(docs)}):")
                for d in docs:
                    lines.append(f"  {d.get('name', 'Unnamed')} ({d.get('type', 'N/A')})")

            return "\n".join(lines)

    async def create_drata_vendor(
        name: str,
        category: str = "NONE",
        status: str = "PROSPECTIVE",
        risk: str = "NONE",
        vendor_type: str = "",
        url: str = "",
        notes: str = "",
        critical: bool = False,
        services_provided: str = "",
        has_pii: bool = False,
        user_id: int = 0,
    ) -> str:
        """Create a new vendor.

        ``user_id`` is the Drata user ID of the person responsible for the
        vendor's compliance — the vendor's *security owner* (use list_drata_users
        to resolve a name/email to an ID). The owner also becomes the default
        requester for security reviews started on this vendor.
        """
        payload: dict[str, Any] = {
            "name": name,
            "category": category,
            "status": status,
            "risk": risk,
            "critical": critical,
            "hasPii": has_pii,
        }
        if isinstance(vendor_type, str) and vendor_type.strip():
            payload["type"] = vendor_type.strip().upper()
        if url:
            payload["url"] = url
        if notes:
            payload["notes"] = notes
        if services_provided:
            payload["servicesProvided"] = services_provided
        uid = _coerce_int(user_id) or 0
        if uid:
            payload["userId"] = uid
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "post",
                f"{base_url}/vendors",
                _PROVIDER,
                "create vendor",
                headers=headers,
                json=payload,
            )
            if err:
                return err
            v = resp.json()
            return f"Vendor '{name}' created (ID: {v.get('id', '?')})."

    async def update_drata_vendor(
        vendor_id: int,
        name: str = "",
        status: str = "",
        risk: str = "",
        category: str = "",
        notes: str = "",
        renewal_date: str = "",
        critical: str = "",
        has_pii: str = "",
        user_id: int = 0,
        vendor_type: str = "",
    ) -> str:
        """Update an existing vendor. Only provided fields are changed.

        ``user_id`` reassigns the vendor's *security owner* (the Drata user ID of
        the person responsible for the vendor's compliance) — use this to move a
        vendor off a departed/stale owner. Resolve a name/email to an ID with
        list_drata_users. ``vendor_type`` is the vendor type identifier
        (VENDOR/SUPPLIER/CONTRACTOR/PARTNER/OTHER/NONE).
        """
        payload: dict[str, Any] = {}
        if name:
            payload["name"] = name
        if status:
            payload["status"] = status
        if risk:
            payload["risk"] = risk
        if category:
            payload["category"] = category
        if notes:
            payload["notes"] = notes
        if renewal_date:
            payload["renewalDate"] = renewal_date
        if critical:
            payload["critical"] = critical.lower() == "true"
        if has_pii:
            payload["hasPii"] = has_pii.lower() == "true"
        uid = _coerce_int(user_id) or 0
        if uid:
            payload["userId"] = uid
        if isinstance(vendor_type, str) and vendor_type.strip():
            payload["type"] = vendor_type.strip().upper()
        if not payload:
            return "Error: provide at least one field to update."
        async with egress_client(timeout=30) as client:
            _, err = await _common.safe_request(
                client,
                "put",
                f"{base_url}/vendors/{vendor_id}",
                _PROVIDER,
                "update vendor",
                headers=headers,
                json=payload,
            )
            if err:
                return err
            return f"Vendor {vendor_id} updated."

    async def remove_drata_vendor(vendor_id: int) -> str:
        """Remove (archive) a vendor."""
        async with egress_client(timeout=30) as client:
            _, err = await _common.safe_request(
                client,
                "delete",
                f"{base_url}/vendors/{vendor_id}",
                _PROVIDER,
                "remove vendor",
                headers=headers,
            )
            if err:
                return err
            return f"Vendor {vendor_id} removed."

    async def list_drata_vendor_documents(vendor_id: int, document_type: str = "") -> str:
        """List documents (SOC reports, bridge letters, questionnaires) for a vendor."""
        params: dict[str, Any] = {"expand[]": ["downloadUrl"]}
        dt = _coerce_str(document_type).strip().upper()
        if dt:
            if dt not in _VENDOR_DOCUMENT_TYPES:
                return f"Error: invalid document_type '{dt}'. Valid: {', '.join(_VENDOR_DOCUMENT_TYPES)}."
            params["type"] = dt
        async with egress_client(timeout=30) as client:
            items, err = await _common._paginated_fetch(
                client, f"{base_url}/vendors/{vendor_id}/documents", headers, params
            )
            if err:
                return err

            def _fmt(d: dict) -> str:
                url = (d.get("downloadUrl") or {}).get("signedUrl") if isinstance(d.get("downloadUrl"), dict) else None
                line = (
                    f"[{d.get('id')}] {d.get('name', 'Unnamed')} — "
                    f"Type: {d.get('type', 'N/A')} | "
                    f"Created: {d.get('createdAt', 'N/A')}"
                )
                if url:
                    line += f" | Download: {url}"
                return line

            return format_entity_list(items, _fmt, noun="vendor document")

    async def list_drata_vendor_types() -> str:
        """List available vendor types."""
        async with egress_client(timeout=30) as client:
            items, err = await _common._paginated_fetch(client, f"{base_url}/vendor-types", headers, {})
            if err:
                return err
            return format_entity_list(
                items,
                lambda t: f"[{t.get('id')}] {t.get('name', 'Unnamed')}",
                noun="vendor type",
            )

    async def get_drata_vendor_stats(scopes: str = "", include_archived: bool = False) -> str:
        """Get aggregate vendor portfolio statistics for one or more scopes.

        ``scopes`` is a CSV of stat scopes (see ``_VENDOR_STATS_SCOPES``);
        defaults to all when omitted. Set ``include_archived`` to also count
        archived vendors.
        """
        # Resolve scopes case-insensitively and de-dupe: callers may pass "RISK"
        # or "haspii", so map each back to the canonical camelCase enum Drata
        # expects (other inputs in this file are normalized the same way).
        canonical = {s.lower(): s for s in _VENDOR_STATS_SCOPES}
        raw = [s.strip() for s in (scopes or "").split(",") if s.strip()]
        bad = [s for s in raw if s.lower() not in canonical]
        if bad:
            return f"Error: invalid scope(s) {', '.join(bad)}. Valid scopes: {', '.join(_VENDOR_STATS_SCOPES)}."
        seen: set[str] = set()
        requested: list[str] = []
        for s in raw:
            scope = canonical[s.lower()]
            if scope not in seen:
                seen.add(scope)
                requested.append(scope)
        if not requested:
            requested = list(_VENDOR_STATS_SCOPES)
        # The v2 endpoint is /vendors-stats (not /vendors/stats) and requires at
        # least one expand[] scope — calling it bare returns HTTP 400.
        params: dict[str, Any] = {"expand[]": requested}
        if include_archived:
            params["includeArchived"] = True
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{base_url}/vendors-stats",
                _PROVIDER,
                "get vendor stats",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json()
            lines = ["Vendor Statistics:"]
            for scope, buckets in data.items():
                if isinstance(buckets, list):
                    parts = ", ".join(f"{b.get('key')}={b.get('count', 0)}" for b in buckets)
                    lines.append(f"  {scope}: {parts or 'none'}")
                else:
                    lines.append(f"  {scope}: {buckets}")
            return "\n".join(lines)

    async def _resolve_vendor_file(
        file_content: str,
        file_name: str,
        file_encoding: str,
        source_file_id: str,
        file_context: FileContext | None,
        fallback_name: str,
    ) -> tuple[bytes, str, str] | str:
        """Resolve ONE artifact source into ``(bytes, name, mime)`` or an error.

        Mirrors the Evidence Library upload helpers: ``source_file_id`` forwards a
        user-uploaded file byte-for-byte; ``file_content`` is rendered to a PDF
        (default) / DOCX or uploaded verbatim for text formats / base64.
        """
        # The integration tool-test path dispatches straight to the handler and
        # bypasses the agent loop's jsonschema validation, so these schema-string
        # args can arrive as None or a non-string. Normalize before any .strip()
        # / downstream render so a bad value returns a friendly error instead of
        # raising AttributeError.
        file_content = file_content if isinstance(file_content, str) else ""
        file_name = file_name if isinstance(file_name, str) else ""
        file_encoding = file_encoding if isinstance(file_encoding, str) else "text"
        source_file_id = source_file_id.strip() if isinstance(source_file_id, str) else ""
        sources = [bool(file_content), bool(source_file_id)]
        if sum(sources) != 1:
            return "Error: provide exactly one artifact source — file_content or source_file_id."
        if source_file_id:
            return await _resolve_uploaded_evidence_file(file_context, source_file_id, file_name)
        return await _render_evidence_file(file_content, file_name, file_encoding, fallback_name=fallback_name)

    def _fmt_security_review(r: dict[str, Any]) -> str:
        return (
            f"[{r.get('id')}] {r.get('title') or '(untitled)'} — "
            f"Type: {r.get('type', 'N/A')} | "
            f"Status: {r.get('status', 'N/A')} | "
            f"Decision: {r.get('decision', 'N/A')} | "
            f"Requested: {r.get('requestedAt', 'N/A')} | "
            f"Deadline: {r.get('reviewDeadlineAt', 'N/A')}"
        )

    async def list_drata_vendor_security_reviews(
        vendor_id: int, status: str = "", review_type: str = "", decision: str = ""
    ) -> str:
        """List a vendor's security reviews (the internal assessments).

        Filter by ``status`` (NOT_YET_STARTED/IN_PROGRESS/COMPLETED/NOT_REQUIRED),
        ``review_type`` (SECURITY/SOC_REPORT/UPLOAD_REPORT), or ``decision``
        (PENDING/APPROVED/APPROVED_WITH_CONDITIONS/REJECTED). Each value may be a
        CSV to match several.
        """
        params: dict[str, Any] = {"expand[]": ["user", "requesterUser"]}
        # Validate the filter enums up front (like create_drata_vendor_security_review
        # and the document_type filter) so an invalid value returns a friendly error
        # instead of being forwarded for Drata to reject with an opaque 4xx.
        statuses = [s.strip().upper() for s in _coerce_str(status).split(",") if s.strip()]
        bad = [s for s in statuses if s not in _VENDOR_SR_STATUSES]
        if bad:
            return f"Error: invalid status {', '.join(bad)}. Valid: {', '.join(_VENDOR_SR_STATUSES)}."
        if statuses:
            params["status[]"] = statuses
        types = [t.strip().upper() for t in _coerce_str(review_type).split(",") if t.strip()]
        bad = [t for t in types if t not in _VENDOR_SR_TYPES]
        if bad:
            return f"Error: invalid review_type {', '.join(bad)}. Valid: {', '.join(_VENDOR_SR_TYPES)}."
        if types:
            params["type[]"] = types
        decisions = [d.strip().upper() for d in _coerce_str(decision).split(",") if d.strip()]
        bad = [d for d in decisions if d not in _VENDOR_SR_DECISIONS]
        if bad:
            return f"Error: invalid decision {', '.join(bad)}. Valid: {', '.join(_VENDOR_SR_DECISIONS)}."
        if decisions:
            params["decision[]"] = decisions
        async with egress_client(timeout=30) as client:
            items, err = await _common._paginated_fetch(
                client,
                f"{base_url}/vendors/{vendor_id}/security-reviews",
                headers,
                params,
            )
            if err:
                return err
            return format_entity_list(items, _fmt_security_review, noun="vendor security review")

    def _fmt_soc_section(name: str, section: dict[str, Any]) -> str:
        if not isinstance(section, dict):
            return f"  {name}: (no data)"
        complete = section.get("isComplete")
        mark = "complete" if complete else "INCOMPLETE"
        missing = section.get("missingRequiredFields") or []
        line = f"  {name}: {mark}"
        if missing:
            line += f" (missing: {', '.join(missing)})"
        return line

    async def get_drata_vendor_security_review(vendor_id: int, security_review_id: int) -> str:
        """Get a vendor security review, including the SOC assessment form state.

        For SOC_REPORT reviews the response surfaces each internal-assessment
        section's completion status and the exact missing required fields, so an
        agent knows precisely what is left to fill before finalizing.
        """
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{base_url}/vendors/{vendor_id}/security-reviews/{security_review_id}",
                _PROVIDER,
                "get vendor security review",
                headers=headers,
                params={"expand[]": ["user", "requesterUser"]},
            )
            if err:
                return err
            r = resp.json()
            lines = [
                f"Security Review [{r.get('id')}] {r.get('title') or '(untitled)'}",
                f"Type: {r.get('type', 'N/A')} | Status: {r.get('status', 'N/A')} | "
                f"Decision: {r.get('decision', 'N/A')}",
                f"Requested: {r.get('requestedAt', 'N/A')} | Deadline: {r.get('reviewDeadlineAt', 'N/A')}",
                f"Reviewer: {_full_name(r.get('user'))} | Requester: {_full_name(r.get('requesterUser'))}",
                f"Note: {r.get('note', 'N/A')}",
            ]
            soc = r.get("socReviewForm") or r.get("socReview")
            if isinstance(soc, dict):
                ready = soc.get("readyToFinalize")
                lines.append(f"\nSOC assessment form (ready to finalize: {bool(ready)}):")
                for key in (
                    "reviewerInformation",
                    "complianceScope",
                    "reportOpinion",
                    "findings",
                    "endUserControls",
                    "servicesAndLocations",
                    "cpaFirm",
                    "subserviceOrganizations",
                ):
                    if key in soc:
                        lines.append(_fmt_soc_section(key, soc.get(key)))
            return "\n".join(lines)

    async def create_drata_vendor_security_review(
        vendor_id: int,
        review_deadline_at: str = "",
        security_review_status: str = "NOT_YET_STARTED",
        security_review_type: str = "SECURITY",
        title: str = "",
        note: str = "",
        requested_at: str = "",
        requester_user_id: int = 0,
    ) -> str:
        """Create (start) a security review / internal assessment for a vendor.

        ``review_deadline_at`` is required (YYYY-MM-DD or ISO-8601).
        ``security_review_status`` is one of NOT_YET_STARTED / IN_PROGRESS /
        COMPLETED / NOT_REQUIRED. ``security_review_type`` is SECURITY (internal
        questionnaire-based), SOC_REPORT (review an uploaded SOC report), or
        UPLOAD_REPORT. ``requester_user_id`` defaults to the vendor's security
        owner or the platform admin when omitted.
        """
        review_deadline = _coerce_str(review_deadline_at).strip()
        if not review_deadline:
            return "Error: review_deadline_at is required (YYYY-MM-DD or ISO-8601)."
        srs = _coerce_str(security_review_status).strip().upper() or "NOT_YET_STARTED"
        if srs not in _VENDOR_SR_STATUSES:
            return f"Error: invalid security_review_status '{srs}'. Valid: {', '.join(_VENDOR_SR_STATUSES)}."
        srt = _coerce_str(security_review_type).strip().upper() or "SECURITY"
        if srt not in _VENDOR_SR_TYPES:
            return f"Error: invalid security_review_type '{srt}'. Valid: {', '.join(_VENDOR_SR_TYPES)}."
        payload: dict[str, Any] = {
            "reviewDeadlineAt": review_deadline,
            "securityReviewStatus": srs,
            "securityReviewType": srt,
        }
        if isinstance(title, str) and title.strip():
            payload["title"] = title.strip()
        if note:
            payload["note"] = note
        requested = _coerce_str(requested_at).strip()
        if requested:
            payload["requestedAt"] = requested
        ruid = _coerce_int(requester_user_id) or 0
        if ruid:
            payload["requesterUserId"] = ruid
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "post",
                f"{base_url}/vendors/{vendor_id}/security-reviews",
                _PROVIDER,
                "create vendor security review",
                headers=headers,
                json=payload,
            )
            if err:
                return err
            data = resp.json() if resp.content else {}
            return (
                f"Security review created for vendor {vendor_id} "
                f"(ID: {data.get('id', '?')}, type: {srt}, status: {srs})."
            )

    async def create_drata_vendor_security_review_with_file(
        vendor_id: int,
        title: str = "",
        review_deadline_at: str = "",
        security_review_status: str = "NOT_YET_STARTED",
        security_review_type: str = "SOC_REPORT",
        note: str = "",
        requested_at: str = "",
        requester_user_id: int = 0,
        document_type: str = "",
        file_content: str = "",
        file_name: str = "",
        file_encoding: str = "text",
        source_file_id: str = "",
        file_context: FileContext | None = None,
    ) -> str:
        """Create a security review AND upload its report file in one atomic call.

        This is the fastest path for an autonomous SOC 2 (Type 1 / Type 2) report
        review: it starts the review and files the vendor's report together via
        ``POST /vendors/{id}/security-reviews/with-file``. Then fill the assessment
        with update_drata_vendor_security_review (soc_form_json) and finalize with
        perform_drata_vendor_security_review_action.

        ``title`` and ``review_deadline_at`` (YYYY-MM-DD or ISO-8601) are required.
        ``security_review_type`` defaults to SOC_REPORT (use SECURITY or
        UPLOAD_REPORT otherwise). ``document_type`` is the uploaded report's type
        (e.g. COMPLIANCE_REPORT for a SOC report); omit to let Drata default.
        ``requester_user_id`` defaults to the vendor's security owner or the
        platform admin. Provide ONE artifact source: ``source_file_id`` (a file the
        user uploaded, forwarded byte-for-byte) or ``file_content`` (Markdown/HTML
        rendered to PDF/DOCX, or base64 via file_encoding='base64'). Accepted:
        .pdf, .docx, .odt, .doc, .xlsx, .ods, .pptx, .odp, .gif, .jpg, .jpeg, .png,
        .csv.
        """
        review_title = _coerce_str(title).strip()
        if not review_title:
            return "Error: title is required for a security review created with a file."
        review_deadline = _coerce_str(review_deadline_at).strip()
        if not review_deadline:
            return "Error: review_deadline_at is required (YYYY-MM-DD or ISO-8601)."
        srs = _coerce_str(security_review_status).strip().upper() or "NOT_YET_STARTED"
        if srs not in _VENDOR_SR_STATUSES:
            return f"Error: invalid security_review_status '{srs}'. Valid: {', '.join(_VENDOR_SR_STATUSES)}."
        srt = _coerce_str(security_review_type).strip().upper() or "SOC_REPORT"
        if srt not in _VENDOR_SR_TYPES:
            return f"Error: invalid security_review_type '{srt}'. Valid: {', '.join(_VENDOR_SR_TYPES)}."
        dt = _coerce_str(document_type).strip().upper()
        if dt and dt not in _VENDOR_DOCUMENT_TYPES:
            return f"Error: invalid document_type '{dt}'. Valid: {', '.join(_VENDOR_DOCUMENT_TYPES)}."
        resolved = await _resolve_vendor_file(
            file_content,
            file_name,
            file_encoding,
            source_file_id,
            file_context,
            fallback_name="security-review-report",
        )
        if isinstance(resolved, str):
            return resolved
        file_bytes, resolved_name, mime = resolved
        data: dict[str, Any] = {
            "title": review_title,
            "reviewDeadlineAt": review_deadline,
            "securityReviewStatus": srs,
            "securityReviewType": srt,
        }
        requested = _coerce_str(requested_at).strip()
        if requested:
            data["requestedAt"] = requested
        if note:
            data["note"] = note
        ruid = _coerce_int(requester_user_id) or 0
        if ruid:
            data["requesterUserId"] = str(ruid)
        if dt:
            data["documentType"] = dt
        files = {"file": (resolved_name, file_bytes, mime)}
        async with egress_client(timeout=120) as client:
            resp, err = await _common.safe_request(
                client,
                "post",
                f"{base_url}/vendors/{vendor_id}/security-reviews/with-file",
                _PROVIDER,
                "create vendor security review with file",
                headers=upload_headers,
                data=data,
                files=files,
            )
            if err:
                return err
            created = resp.json() if resp.content else {}
            return uploaded_file_answer(
                f"Security review '{review_title}' created for vendor {vendor_id} "
                f"with its report file (ID: {created.get('id', '?')}, "
                f"type: {srt}, status: {srs}). Fill it with "
                f"update_drata_vendor_security_review, then finalize with "
                f"perform_drata_vendor_security_review_action.",
                target=_PROVIDER,
                filename=resolved_name,
            )

    async def update_drata_vendor_security_review(
        vendor_id: int,
        security_review_id: int,
        title: str = "",
        soc_form_json: str = "",
    ) -> str:
        """Fill / update a vendor security review's internal assessment.

        ``title`` applies to any review type. ``soc_form_json`` is a JSON object
        (string) for the SOC report assessment — only processed when the review's
        type is SOC_REPORT. Provide only the sections you want to set; shape:

        {
          "reviewerInformation": {"reviewerUserId": 42, "reportIssueDate": "2026-01-15"},
          "complianceScope": {"certification": "SOC_2", "scopeType": "TYPE_2",
            "auditPeriodRange": {"start": "2025-01-01", "end": "2025-12-31"},
            "trustServiceCriteria": ["SECURITY", "AVAILABILITY"]},
          "reportOpinion": {"reportOpinion": "UNQUALIFIED",
            "encompassBusinessNeeds": true, "followUpActivity": "..."},
          "findings": {"findings": [{"description": "..."}], "hasMaterialImpact": false},
          "endUserControls": {"userControls": [{"description": "...", "inPlace": true}]},
          "servicesAndLocations": {"services": [{"name": "..."}],
            "locations": [{"city": "Austin", "stateOrCountry": "TX"}]},
          "cpaFirm": {"cpaFirm": "...", "cpaProcedurePerformed": "..."},
          "subserviceOrganizations": {"subserviceOrganization": "...",
            "subserviceOrganizationUsingInclusiveMethod": true,
            "subserviceOrganizationProcedurePerformed": "..."}
        }

        Use get_drata_vendor_security_review first to see which sections/fields are
        still missing, then finalize via perform_drata_vendor_security_review_action.
        """
        payload: dict[str, Any] = {}
        if isinstance(title, str) and title.strip():
            payload["title"] = title.strip()
        if soc_form_json:
            # Check the type before calling .strip(): the tool-test path bypasses
            # jsonschema, so a non-string (e.g. a dict) can arrive here and would
            # otherwise raise AttributeError instead of this friendly error.
            if not isinstance(soc_form_json, str):
                return "Error: soc_form_json must be a JSON string."
            if soc_form_json.strip():
                try:
                    soc_form = json.loads(soc_form_json)
                except (json.JSONDecodeError, TypeError):
                    return "Error: soc_form_json is not valid JSON."
                if not isinstance(soc_form, dict):
                    return "Error: soc_form_json must be a JSON object."
                payload["socForm"] = soc_form
        if not payload:
            return "Error: provide title and/or soc_form_json to update the review."
        async with egress_client(timeout=30) as client:
            _, err = await _common.safe_request(
                client,
                "put",
                f"{base_url}/vendors/{vendor_id}/security-reviews/{security_review_id}",
                _PROVIDER,
                "update vendor security review",
                headers=headers,
                json=payload,
            )
            if err:
                return err
            changed = ", ".join(sorted(payload.keys()))
            return (
                f"Security review {security_review_id} updated ({changed}). "
                f"Use get_drata_vendor_security_review to verify remaining fields."
            )

    async def list_drata_vendor_security_review_actions(vendor_id: int, security_review_id: int) -> str:
        """List the actions available on a security review (e.g. finalize, reopen)."""
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{base_url}/vendors/{vendor_id}/security-reviews/{security_review_id}/actions",
                _PROVIDER,
                "list vendor security review actions",
                headers=headers,
            )
            if err:
                return err
            data = resp.json() if resp.content else {}
            actions = data.get("data") if isinstance(data, dict) else data
            names = [a.get("action") if isinstance(a, dict) else a for a in (actions or [])]
            if not names:
                return (
                    f"No actions available for security review {security_review_id} "
                    "(actions are currently only supported for SOC_REPORT reviews)."
                )
            return f"Available actions for review {security_review_id}: {', '.join(names)}"

    async def perform_drata_vendor_security_review_action(vendor_id: int, security_review_id: int, action: str) -> str:
        """Finalize (complete) or reopen a vendor security review.

        ``action`` is 'finalize' (mark complete) or 'reopen' (return a completed
        review to in-progress). Currently only SOC_REPORT reviews support actions.
        """
        act = _coerce_str(action).strip().lower()
        if act not in _VENDOR_SR_ACTIONS:
            return f"Error: invalid action '{action}'. Valid: {', '.join(_VENDOR_SR_ACTIONS)}."
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "post",
                f"{base_url}/vendors/{vendor_id}/security-reviews/{security_review_id}/actions",
                _PROVIDER,
                "perform vendor security review action",
                headers=headers,
                json={"action": act},
            )
            if err:
                return err
            data = resp.json() if resp.content else {}
            new_status = data.get("newStatus")
            msg = data.get("message") or f"Action '{act}' performed."
            if new_status:
                return f"{msg} (new status: {new_status})."
            return msg

    async def delete_drata_vendor_security_review(vendor_id: int, security_review_id: int) -> str:
        """Delete a vendor security review.

        NOTE: Drata's Public API v2 does not currently expose a delete route for
        vendor security reviews (only finalize/reopen via
        perform_drata_vendor_security_review_action). This tool attempts the
        delete anyway (so it works if your account has a newer API that supports
        it) and, when the route is unavailable, returns a clear explanation
        instead of failing silently or fabricating a workaround — do NOT create
        spurious COMPLETED/SECURITY reviews to mask an unwanted one. A 405/501
        means the route is unsupported; a 404 is ambiguous (the review/vendor id
        may simply not exist), so verify with get_drata_vendor_security_review
        first. To remove a review today, delete it in the Drata UI (Vendors ->
        the vendor -> Security reviews); to retract a finalized review, 'reopen'
        it.
        """
        vid = _coerce_int(vendor_id) or 0
        srid = _coerce_int(security_review_id) or 0
        if vid <= 0 or srid <= 0:
            return "Error: vendor_id and security_review_id are required."
        # Pass expected_statuses so safe_request hands back the failed response
        # (rather than None) and we can branch on the code. 405/501 mean the
        # DELETE route itself is unsupported; 404 is AMBIGUOUS (the route may be
        # missing, OR the vendor/review id may simply not exist), so it gets a
        # distinct message pointing at a way to verify. Anything else falls
        # through to the standard error instead of an opaque HTTP failure.
        route_unsupported = {405, 501}
        not_found = {404}
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "delete",
                f"{base_url}/vendors/{vid}/security-reviews/{srid}",
                _PROVIDER,
                "delete vendor security review",
                headers=headers,
                expected_statuses=route_unsupported | not_found,
            )
            if resp is not None and resp.is_success:
                return f"Security review {srid} deleted from vendor {vid}."
            if resp is not None and resp.status_code in route_unsupported:
                return (
                    f"Drata's Public API does not currently support deleting "
                    f"vendor security reviews (HTTP {resp.status_code} for "
                    f"DELETE /vendors/{vid}/security-reviews/{srid}). The review "
                    "was NOT deleted. Options: (1) if it is finalized and you "
                    "want it editable again, 'reopen' it with "
                    "perform_drata_vendor_security_review_action; (2) delete it "
                    "manually in the Drata UI (Vendors -> vendor -> Security "
                    "reviews). Do not create extra reviews to mask this one."
                )
            if resp is not None and resp.status_code == 404:
                return (
                    f"DELETE /vendors/{vid}/security-reviews/{srid} returned "
                    "HTTP 404, which is ambiguous: either security review "
                    f"{srid} (or vendor {vid}) does not exist, or Drata's "
                    "Public API does not expose a delete route for reviews. The "
                    "review was NOT deleted. First confirm it exists with "
                    "get_drata_vendor_security_review (or "
                    "list_drata_vendor_security_reviews); if it does, deletion "
                    "is unsupported — 'reopen' it with "
                    "perform_drata_vendor_security_review_action or remove it in "
                    "the Drata UI. Do not create extra reviews to mask this one."
                )
            return err or "Error: failed to delete the vendor security review."

    async def list_drata_vendor_security_review_questionnaires(vendor_id: int, security_review_id: int) -> str:
        """List the security questionnaires attached to a specific security review."""
        async with egress_client(timeout=30) as client:
            items, err = await _common._paginated_fetch(
                client,
                f"{base_url}/vendors/{vendor_id}/security-reviews/{security_review_id}/security-questionnaires",
                headers,
                {},
            )
            if err:
                return err
            return format_entity_list(
                items,
                lambda q: (
                    f"[{q.get('id')}] {q.get('title', 'Questionnaire')} — "
                    f"Completed: {q.get('isCompleted', 'N/A')} | "
                    f"By: {q.get('completedBy', 'N/A')} | "
                    f"Manual upload: {q.get('isManualUpload', 'N/A')}"
                ),
                noun="security review questionnaire",
            )

    async def upload_drata_vendor_security_questionnaire(
        vendor_id: int,
        security_review_id: int = 0,
        file_content: str = "",
        file_name: str = "",
        file_encoding: str = "text",
        source_file_id: str = "",
        file_context: FileContext | None = None,
    ) -> str:
        """Upload a completed security questionnaire for a vendor.

        Pass ``security_review_id`` to attach it to a specific review; omit it to
        file it at the vendor level. Provide ONE artifact source: ``source_file_id``
        (a file the user uploaded, forwarded byte-for-byte) or ``file_content``
        (Markdown/HTML rendered to PDF/DOCX, or base64 via file_encoding='base64').
        Accepted upload types: .pdf, .docx, .odt, .doc, .xlsx, .ods, .pptx, .odp,
        .csv.
        """
        resolved = await _resolve_vendor_file(
            file_content,
            file_name,
            file_encoding,
            source_file_id,
            file_context,
            fallback_name="security-questionnaire",
        )
        if isinstance(resolved, str):
            return resolved
        file_bytes, resolved_name, mime = resolved
        srid = _coerce_int(security_review_id) or 0
        if srid:
            url = f"{base_url}/vendors/{vendor_id}/security-reviews/{srid}/security-questionnaires"
            target = f"security review {srid}"
        else:
            url = f"{base_url}/vendors/{vendor_id}/security-questionnaires"
            target = f"vendor {vendor_id}"
        files = [("files", (resolved_name, file_bytes, mime))]
        async with egress_client(timeout=120) as client:
            resp, err = await _common.safe_request(
                client,
                "post",
                url,
                _PROVIDER,
                "upload vendor security questionnaire",
                headers=upload_headers,
                files=files,
            )
            if err:
                return err
            data = resp.json() if resp.content else {}
            qid = data.get("id") if isinstance(data, dict) else None
            return uploaded_file_answer(
                f"Security questionnaire uploaded to {target}" + (f" (ID: {qid})." if qid else "."),
                target=_PROVIDER,
                filename=resolved_name,
            )

    async def list_drata_vendor_questionnaires(vendor_id: int) -> str:
        """List questionnaires sent to a vendor (and their completion status)."""
        async with egress_client(timeout=30) as client:
            items, err = await _common._paginated_fetch(
                client,
                f"{base_url}/vendors/{vendor_id}/questionnaires",
                headers,
                {},
            )
            if err:
                return err
            return format_entity_list(
                items,
                lambda q: (
                    f"[{q.get('id')}] {q.get('title', 'Questionnaire')} — "
                    f"Recipient: {q.get('recipientEmail', 'N/A')} | "
                    f"Completed: {q.get('isCompleted', 'N/A')} | "
                    f"Sent: {q.get('dateSent', 'N/A')}"
                ),
                noun="vendor questionnaire",
            )

    async def get_drata_vendor_questionnaire(vendor_id: int, questionnaire_id: int) -> str:
        """Get a specific questionnaire sent to a vendor."""
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{base_url}/vendors/{vendor_id}/questionnaires/{questionnaire_id}",
                _PROVIDER,
                "get vendor questionnaire",
                headers=headers,
            )
            if err:
                return err
            q = resp.json()
            return (
                f"Questionnaire [{q.get('id')}] {q.get('title', 'Questionnaire')}\n"
                f"Recipient: {q.get('recipientEmail', 'N/A')}\n"
                f"Completed: {q.get('isCompleted', 'N/A')} "
                f"(by {q.get('completedBy', 'N/A')})\n"
                f"Sent: {q.get('dateSent', 'N/A')} | "
                f"Manual upload: {q.get('isManualUpload', 'N/A')}"
            )

    async def send_drata_vendor_questionnaire(
        vendor_id: int,
        email: str = "",
        questionnaire_id: int = 0,
        email_content: str = "",
        security_review_id: int = 0,
        email_subject: str = "",
    ) -> str:
        """Send a questionnaire to a vendor contact by email.

        ``questionnaire_id`` is the Drata questionnaire template ID to send.
        ``security_review_id`` links the sent questionnaire to a security review
        (required by the API) — create one with
        create_drata_vendor_security_review if needed. ``email_content`` is the
        email body sent to the vendor contact.
        """
        email = _coerce_str(email).strip()
        if not email:
            return "Error: a recipient email is required."
        email_content = _coerce_str(email_content)
        if not email_content.strip():
            return "Error: email_content is required."
        # questionnaireId and securityReviewId are required by the API; bail out
        # with a clear message instead of POSTing a 0 that Drata would reject.
        qid = _coerce_int(questionnaire_id) or 0
        if qid <= 0:
            return "Error: questionnaire_id is required (the Drata questionnaire template ID to send)."
        srid = _coerce_int(security_review_id) or 0
        if srid <= 0:
            return (
                "Error: security_review_id is required (links the questionnaire "
                "to a security review; create one with "
                "create_drata_vendor_security_review)."
            )
        payload: dict[str, Any] = {
            "email": email,
            "questionnaireId": qid,
            "emailContent": email_content,
            "securityReviewId": srid,
        }
        if isinstance(email_subject, str) and email_subject.strip():
            payload["emailSubject"] = email_subject.strip()
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "post",
                f"{base_url}/vendors/{vendor_id}/questionnaires",
                _PROVIDER,
                "send vendor questionnaire",
                headers=headers,
                json=payload,
            )
            if err:
                return err
            data = resp.json() if resp.content else {}
            return f"Questionnaire sent to {email} for vendor {vendor_id} (questionnaire ID: {data.get('id', '?')})."

    async def get_drata_vendor_document(vendor_id: int, document_id: int) -> str:
        """Get a vendor document's metadata including its download URL."""
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{base_url}/vendors/{vendor_id}/documents/{document_id}",
                _PROVIDER,
                "get vendor document",
                headers=headers,
                params={"expand[]": ["downloadUrl"]},
            )
            if err:
                return err
            d = resp.json()
            url = (d.get("downloadUrl") or {}).get("signedUrl") if isinstance(d.get("downloadUrl"), dict) else None
            return (
                f"Document [{d.get('id')}] {d.get('name', 'Unnamed')}\n"
                f"Type: {d.get('type', 'N/A')} | Created: {d.get('createdAt', 'N/A')}\n"
                f"Download URL: {url or 'N/A'}"
            )

    async def upload_drata_vendor_document(
        vendor_id: int,
        document_type: str = "",
        security_review_id: int = 0,
        file_content: str = "",
        file_name: str = "",
        file_encoding: str = "text",
        source_file_id: str = "",
        file_context: FileContext | None = None,
    ) -> str:
        """Upload a document (e.g. SOC report, bridge letter) for a vendor.

        Pass ``security_review_id`` to attach the document to a specific security
        review. ``document_type`` is one of COMPLIANCE_REPORT,
        COMPLIANCE_REPORT_REVIEW, BRIDGE_LETTER, UPLOADED_COMPLIANCE_REPORT_REVIEW,
        QUESTIONNAIRE_ATTACHMENT, SOC_DOCUMENT, QUESTIONNAIRE_REPORT. Provide ONE
        artifact source: ``source_file_id`` (a user-uploaded file forwarded
        byte-for-byte) or ``file_content`` (rendered to PDF/DOCX, or base64).
        """
        dt = _coerce_str(document_type).strip().upper()
        if not dt:
            return f"Error: document_type is required. Valid: {', '.join(_VENDOR_DOCUMENT_TYPES)}."
        if dt not in _VENDOR_DOCUMENT_TYPES:
            return f"Error: invalid document_type '{dt}'. Valid: {', '.join(_VENDOR_DOCUMENT_TYPES)}."
        resolved = await _resolve_vendor_file(
            file_content,
            file_name,
            file_encoding,
            source_file_id,
            file_context,
            fallback_name="vendor-document",
        )
        if isinstance(resolved, str):
            return resolved
        file_bytes, resolved_name, mime = resolved
        data: dict[str, Any] = {"type": dt}
        srid = _coerce_int(security_review_id) or 0
        if srid:
            data["securityReviewId"] = str(srid)
        files = {"file": (resolved_name, file_bytes, mime)}
        async with egress_client(timeout=120) as client:
            resp, err = await _common.safe_request(
                client,
                "post",
                f"{base_url}/vendors/{vendor_id}/documents",
                _PROVIDER,
                "upload vendor document",
                headers=upload_headers,
                data=data,
                files=files,
            )
            if err:
                return err
            d = resp.json() if resp.content else {}
            suffix = f" (attached to review {srid})" if srid else ""
            return uploaded_file_answer(
                f"Document uploaded to vendor {vendor_id}{suffix} (ID: {d.get('id', '?')}).",
                target=_PROVIDER,
                filename=resolved_name,
            )

    async def delete_drata_vendor_document(vendor_id: int, document_id: int) -> str:
        """Delete a vendor document (SOC report, bridge letter, report review, etc.).

        NOTE: Drata's Public API v2 does not currently expose a delete route for
        vendor documents — the Vendor Documents API is list / upload / get only
        (https://developers.drata.com/openapi/reference/v2/tag/Vendor-Documents/).
        This tool attempts the delete anyway (so it works automatically if your
        account has a newer API that adds the route) and, when the route is
        unavailable, returns a clear explanation instead of failing silently or
        fabricating a workaround. Do NOT remove the whole vendor
        (remove_drata_vendor) or upload masking documents to hide an unwanted one.
        A 405/501 means the route is unsupported; a 404 is AMBIGUOUS (the
        document/vendor id may simply not exist), so verify with
        get_drata_vendor_document / list_drata_vendor_documents first. To remove a
        document today, delete it in the Drata UI (Vendors -> the vendor ->
        Reports & documents).
        """
        vid = _coerce_int(vendor_id) or 0
        did = _coerce_int(document_id) or 0
        if vid <= 0 or did <= 0:
            return "Error: vendor_id and document_id are required."
        # Pass expected_statuses so safe_request hands back the failed response
        # (rather than None) and we can branch on the code. 405/501 mean the
        # DELETE route itself is unsupported; 404 is ambiguous (route missing OR
        # the vendor/document id does not exist). Anything else falls through to
        # the standard error instead of an opaque HTTP failure.
        route_unsupported = {405, 501}
        not_found = {404}
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "delete",
                f"{base_url}/vendors/{vid}/documents/{did}",
                _PROVIDER,
                "delete vendor document",
                headers=headers,
                expected_statuses=route_unsupported | not_found,
            )
            if resp is not None and resp.is_success:
                return f"Document {did} deleted from vendor {vid}."
            if resp is not None and resp.status_code in route_unsupported:
                return (
                    f"Drata's Public API does not currently support deleting vendor "
                    f"documents (HTTP {resp.status_code} for DELETE "
                    f"/vendors/{vid}/documents/{did}). The document was NOT deleted — "
                    "the Vendor Documents API is list/upload/get only. To remove it, "
                    "delete it manually in the Drata UI (Vendors -> the vendor -> "
                    "Reports & documents). Do NOT remove the whole vendor or upload "
                    "masking documents to work around this."
                )
            if resp is not None and resp.status_code == 404:
                return (
                    f"DELETE /vendors/{vid}/documents/{did} returned HTTP 404, which "
                    f"is ambiguous: either document {did} (or vendor {vid}) does not "
                    "exist, or Drata's Public API does not expose a delete route for "
                    "vendor documents. The document was NOT deleted. First confirm it "
                    "exists with get_drata_vendor_document (or "
                    "list_drata_vendor_documents); if it does, deletion is unsupported "
                    "via the API — remove it in the Drata UI (Vendors -> the vendor -> "
                    "Reports & documents)."
                )
            return err or "Error: failed to delete the vendor document."

    callable_map = {
        "list_drata_vendors": list_drata_vendors,
        "get_drata_vendor": get_drata_vendor,
        "create_drata_vendor": create_drata_vendor,
        "update_drata_vendor": update_drata_vendor,
        "remove_drata_vendor": remove_drata_vendor,
        "list_drata_vendor_documents": list_drata_vendor_documents,
        "get_drata_vendor_document": get_drata_vendor_document,
        "upload_drata_vendor_document": upload_drata_vendor_document,
        "delete_drata_vendor_document": delete_drata_vendor_document,
        "list_drata_vendor_types": list_drata_vendor_types,
        "get_drata_vendor_stats": get_drata_vendor_stats,
        "list_drata_vendor_security_reviews": list_drata_vendor_security_reviews,
        "get_drata_vendor_security_review": get_drata_vendor_security_review,
        "create_drata_vendor_security_review": create_drata_vendor_security_review,
        "create_drata_vendor_security_review_with_file": create_drata_vendor_security_review_with_file,
        "update_drata_vendor_security_review": update_drata_vendor_security_review,
        "list_drata_vendor_security_review_actions": list_drata_vendor_security_review_actions,
        "perform_drata_vendor_security_review_action": perform_drata_vendor_security_review_action,
        "delete_drata_vendor_security_review": delete_drata_vendor_security_review,
        "list_drata_vendor_security_review_questionnaires": list_drata_vendor_security_review_questionnaires,
        "upload_drata_vendor_security_questionnaire": upload_drata_vendor_security_questionnaire,
        "list_drata_vendor_questionnaires": list_drata_vendor_questionnaires,
        "get_drata_vendor_questionnaire": get_drata_vendor_questionnaire,
        "send_drata_vendor_questionnaire": send_drata_vendor_questionnaire,
    }
    for _name, _fn in callable_map.items():
        executor.register(_name, _fn)

    return [
        ToolDefinition(
            name="list_drata_vendors",
            description="List vendors/third-parties. Optionally filter by status, risk, or category.",
            input_schema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": [
                            "PROSPECTIVE",
                            "ACTIVE",
                            "ARCHIVED",
                            "APPROVED",
                            "REJECTED",
                            "FLAGGED",
                            "ON_HOLD",
                            "OFFBOARDED",
                            "UNDER_REVIEW",
                            "NONE",
                        ],
                    },
                    "risk": {
                        "type": "string",
                        "enum": ["NONE", "LOW", "MODERATE", "HIGH"],
                    },
                    "category": {
                        "type": "string",
                        "enum": [
                            "ENGINEERING",
                            "PRODUCT",
                            "MARKETING",
                            "CS",
                            "SALES",
                            "FINANCE",
                            "HR",
                            "ADMINISTRATIVE",
                            "SECURITY",
                            "LEGAL",
                            "INFORMATION_TECHNOLOGY",
                            "NONE",
                        ],
                    },
                },
                "required": [],
            },
            source="integration:drata",
            priority=1,
        ),
        ToolDefinition(
            name="get_drata_vendor",
            description="Get full details of a specific Drata vendor including services, data handling, security reviews, and documents.",
            input_schema={
                "type": "object",
                "properties": {
                    "vendor_id": {"type": "integer"},
                },
                "required": ["vendor_id"],
            },
            source="integration:drata",
            priority=1,
        ),
        ToolDefinition(
            name="create_drata_vendor",
            description="Create a new vendor in Drata.",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "category": {"type": "string", "default": "NONE"},
                    "status": {"type": "string", "default": "PROSPECTIVE"},
                    "risk": {"type": "string", "default": "NONE"},
                    "vendor_type": {
                        "type": "string",
                        "description": ("Vendor type identifier: VENDOR/SUPPLIER/CONTRACTOR/PARTNER/OTHER/NONE."),
                    },
                    "url": {"type": "string"},
                    "notes": {"type": "string"},
                    "critical": {"type": "boolean", "default": False},
                    "services_provided": {"type": "string"},
                    "has_pii": {"type": "boolean", "default": False},
                    "user_id": {
                        "type": "integer",
                        "description": (
                            "Drata user ID of the vendor's security owner (person "
                            "responsible for the vendor's compliance). Resolve "
                            "with list_drata_users."
                        ),
                    },
                },
                "required": ["name"],
            },
            source="integration:drata",
            priority=1,
        ),
        ToolDefinition(
            name="update_drata_vendor",
            description=(
                "Update an existing vendor. Only provided fields are changed. "
                "Pass user_id to (re)assign the vendor's security owner."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "vendor_id": {"type": "integer"},
                    "name": {"type": "string"},
                    "status": {"type": "string"},
                    "risk": {"type": "string"},
                    "category": {"type": "string"},
                    "notes": {"type": "string"},
                    "renewal_date": {"type": "string"},
                    "critical": {"type": "string", "enum": ["true", "false"]},
                    "has_pii": {"type": "string", "enum": ["true", "false"]},
                    "user_id": {
                        "type": "integer",
                        "description": (
                            "Drata user ID of the vendor's security owner (person "
                            "responsible for the vendor's compliance). Resolve "
                            "with list_drata_users."
                        ),
                    },
                    "vendor_type": {
                        "type": "string",
                        "description": ("Vendor type identifier: VENDOR/SUPPLIER/CONTRACTOR/PARTNER/OTHER/NONE."),
                    },
                },
                "required": ["vendor_id"],
            },
            source="integration:drata",
            priority=1,
        ),
        ToolDefinition(
            name="remove_drata_vendor",
            description="Remove (archive) a vendor from Drata.",
            input_schema={
                "type": "object",
                "properties": {
                    "vendor_id": {"type": "integer"},
                },
                "required": ["vendor_id"],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="list_drata_vendor_documents",
            description="List documents (SOC reports, bridge letters, questionnaires) for a vendor. Optionally filter by document_type.",
            input_schema={
                "type": "object",
                "properties": {
                    "vendor_id": {"type": "integer"},
                    "document_type": {
                        "type": "string",
                        "enum": list(_VENDOR_DOCUMENT_TYPES),
                    },
                },
                "required": ["vendor_id"],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="get_drata_vendor_document",
            description="Get a vendor document's metadata including its download URL.",
            input_schema={
                "type": "object",
                "properties": {
                    "vendor_id": {"type": "integer"},
                    "document_id": {"type": "integer"},
                },
                "required": ["vendor_id", "document_id"],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="upload_drata_vendor_document",
            description=(
                "Upload a document (SOC report, bridge letter, etc.) for a vendor, "
                "optionally attaching it to a security review via security_review_id. "
                "Provide ONE source: source_file_id (a user-uploaded file forwarded "
                "byte-for-byte) or file_content (rendered to PDF/DOCX, or base64 "
                "with file_encoding='base64')."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "vendor_id": {"type": "integer"},
                    "document_type": {
                        "type": "string",
                        "enum": list(_VENDOR_DOCUMENT_TYPES),
                    },
                    "security_review_id": {"type": "integer"},
                    "file_content": {"type": "string"},
                    "file_name": {"type": "string"},
                    "file_encoding": {
                        "type": "string",
                        "enum": ["text", "base64"],
                        "default": "text",
                    },
                    "source_file_id": {"type": "string"},
                },
                "required": ["vendor_id", "document_type"],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="delete_drata_vendor_document",
            description=(
                "Delete a vendor document (SOC report, bridge letter, report review, etc.). "
                "NOTE: Drata's Public API does not currently expose a delete route for vendor "
                "documents (list/upload/get only), so this attempts the delete and, when "
                "unsupported, returns clear guidance (remove it in the Drata UI: Vendors -> the "
                "vendor -> Reports & documents) rather than failing silently or fabricating a "
                "workaround. Verify the id with get_drata_vendor_document first; a 404 is "
                "ambiguous. Never remove the whole vendor or upload masking documents to work "
                "around the missing delete."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "vendor_id": {"type": "integer"},
                    "document_id": {"type": "integer"},
                },
                "required": ["vendor_id", "document_id"],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="list_drata_vendor_types",
            description="List available vendor types.",
            input_schema=_empty,
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="get_drata_vendor_stats",
            description=(
                "Get aggregate vendor portfolio statistics for one or more "
                "scopes. scopes is a CSV (defaults to all): "
                + ", ".join(_VENDOR_STATS_SCOPES)
                + ". Set include_archived to also count archived vendors."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "scopes": {
                        "type": "string",
                        "description": "CSV of stat scopes; empty = all scopes",
                    },
                    "include_archived": {"type": "boolean", "default": False},
                },
                "required": [],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="list_drata_vendor_security_reviews",
            description="List a vendor's security reviews (internal assessments). Filter by status, review_type, or decision (each a CSV).",
            input_schema={
                "type": "object",
                "properties": {
                    "vendor_id": {"type": "integer"},
                    "status": {
                        "type": "string",
                        "description": "CSV of " + "/".join(_VENDOR_SR_STATUSES),
                    },
                    "review_type": {
                        "type": "string",
                        "description": "CSV of " + "/".join(_VENDOR_SR_TYPES),
                    },
                    "decision": {
                        "type": "string",
                        "description": "CSV of " + "/".join(_VENDOR_SR_DECISIONS),
                    },
                },
                "required": ["vendor_id"],
            },
            source="integration:drata",
            priority=1,
        ),
        ToolDefinition(
            name="get_drata_vendor_security_review",
            description="Get a vendor security review, including the SOC assessment form's per-section completion status and the exact missing required fields.",
            input_schema={
                "type": "object",
                "properties": {
                    "vendor_id": {"type": "integer"},
                    "security_review_id": {"type": "integer"},
                },
                "required": ["vendor_id", "security_review_id"],
            },
            source="integration:drata",
            priority=1,
        ),
        ToolDefinition(
            name="create_drata_vendor_security_review",
            description="Create (start) a security review / internal assessment for a vendor. review_deadline_at is required (YYYY-MM-DD or ISO-8601).",
            input_schema={
                "type": "object",
                "properties": {
                    "vendor_id": {"type": "integer"},
                    "review_deadline_at": {"type": "string"},
                    "security_review_status": {
                        "type": "string",
                        "enum": list(_VENDOR_SR_STATUSES),
                        "default": "NOT_YET_STARTED",
                    },
                    "security_review_type": {
                        "type": "string",
                        "enum": list(_VENDOR_SR_TYPES),
                        "default": "SECURITY",
                    },
                    "title": {"type": "string"},
                    "note": {"type": "string"},
                    "requested_at": {"type": "string"},
                    "requester_user_id": {
                        "type": "integer",
                        "description": "Drata user ID requesting the review",
                    },
                },
                "required": ["vendor_id", "review_deadline_at"],
            },
            source="integration:drata",
            priority=1,
        ),
        ToolDefinition(
            name="create_drata_vendor_security_review_with_file",
            description=(
                "Create a security review AND upload its report file in ONE atomic "
                "call (the fastest path for an autonomous SOC 2 Type 1/Type 2 "
                "report review). title and review_deadline_at are required; "
                "security_review_type defaults to SOC_REPORT. Provide ONE source: "
                "source_file_id (a user-uploaded file forwarded byte-for-byte) or "
                "file_content (rendered to PDF/DOCX, or base64 with "
                "file_encoding='base64'). Then fill it with "
                "update_drata_vendor_security_review and finalize with "
                "perform_drata_vendor_security_review_action."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "vendor_id": {"type": "integer"},
                    "title": {"type": "string"},
                    "review_deadline_at": {"type": "string"},
                    "security_review_status": {
                        "type": "string",
                        "enum": list(_VENDOR_SR_STATUSES),
                        "default": "NOT_YET_STARTED",
                    },
                    "security_review_type": {
                        "type": "string",
                        "enum": list(_VENDOR_SR_TYPES),
                        "default": "SOC_REPORT",
                    },
                    "note": {"type": "string"},
                    "requested_at": {"type": "string"},
                    "requester_user_id": {
                        "type": "integer",
                        "description": "Drata user ID requesting the review",
                    },
                    "document_type": {
                        "type": "string",
                        "enum": list(_VENDOR_DOCUMENT_TYPES),
                        "description": "Type of the uploaded report (omit to default)",
                    },
                    "file_content": {"type": "string"},
                    "file_name": {"type": "string"},
                    "file_encoding": {
                        "type": "string",
                        "enum": ["text", "base64"],
                        "default": "text",
                    },
                    "source_file_id": {"type": "string"},
                },
                "required": ["vendor_id", "title", "review_deadline_at"],
            },
            source="integration:drata",
            priority=1,
        ),
        ToolDefinition(
            name="update_drata_vendor_security_review",
            description=(
                "Fill / update a vendor security review's internal assessment. "
                "title applies to any type; soc_form_json is a JSON object (string) "
                "for the SOC report assessment (only used for SOC_REPORT reviews) "
                "with sections reviewerInformation, complianceScope, reportOpinion, "
                "findings, endUserControls, servicesAndLocations, cpaFirm, "
                "subserviceOrganizations. Call get_drata_vendor_security_review "
                "first to see missing fields, then finalize via "
                "perform_drata_vendor_security_review_action."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "vendor_id": {"type": "integer"},
                    "security_review_id": {"type": "integer"},
                    "title": {"type": "string"},
                    "soc_form_json": {
                        "type": "string",
                        "description": "JSON object string of the SOC assessment form",
                    },
                },
                "required": ["vendor_id", "security_review_id"],
            },
            source="integration:drata",
            priority=1,
        ),
        ToolDefinition(
            name="list_drata_vendor_security_review_actions",
            description="List available actions on a security review (e.g. finalize, reopen). SOC_REPORT reviews only.",
            input_schema={
                "type": "object",
                "properties": {
                    "vendor_id": {"type": "integer"},
                    "security_review_id": {"type": "integer"},
                },
                "required": ["vendor_id", "security_review_id"],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="perform_drata_vendor_security_review_action",
            description="Finalize (complete) or reopen a vendor security review. SOC_REPORT reviews only.",
            input_schema={
                "type": "object",
                "properties": {
                    "vendor_id": {"type": "integer"},
                    "security_review_id": {"type": "integer"},
                    "action": {"type": "string", "enum": list(_VENDOR_SR_ACTIONS)},
                },
                "required": ["vendor_id", "security_review_id", "action"],
            },
            source="integration:drata",
            priority=1,
        ),
        ToolDefinition(
            name="delete_drata_vendor_security_review",
            description=(
                "Delete a vendor security review. NOTE: Drata's Public API v2 has "
                "no delete route for reviews, so this attempts the delete and "
                "returns clear guidance when unsupported (reopen via "
                "perform_drata_vendor_security_review_action, or delete in the "
                "Drata UI) rather than failing silently. Never create extra "
                "reviews to mask an unwanted one."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "vendor_id": {"type": "integer"},
                    "security_review_id": {"type": "integer"},
                },
                "required": ["vendor_id", "security_review_id"],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="list_drata_vendor_security_review_questionnaires",
            description="List the security questionnaires attached to a specific security review.",
            input_schema={
                "type": "object",
                "properties": {
                    "vendor_id": {"type": "integer"},
                    "security_review_id": {"type": "integer"},
                },
                "required": ["vendor_id", "security_review_id"],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="upload_drata_vendor_security_questionnaire",
            description=(
                "Upload a completed security questionnaire for a vendor; pass "
                "security_review_id to attach it to a specific review (omit for "
                "vendor-level). Provide ONE source: source_file_id (a user-uploaded "
                "file forwarded byte-for-byte) or file_content (rendered to "
                "PDF/DOCX, or base64). Accepted: .pdf/.docx/.odt/.doc/.xlsx/.ods/"
                ".pptx/.odp/.csv."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "vendor_id": {"type": "integer"},
                    "security_review_id": {"type": "integer"},
                    "file_content": {"type": "string"},
                    "file_name": {"type": "string"},
                    "file_encoding": {
                        "type": "string",
                        "enum": ["text", "base64"],
                        "default": "text",
                    },
                    "source_file_id": {"type": "string"},
                },
                "required": ["vendor_id"],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="list_drata_vendor_questionnaires",
            description="List questionnaires sent to a vendor and their completion status.",
            input_schema={
                "type": "object",
                "properties": {
                    "vendor_id": {"type": "integer"},
                },
                "required": ["vendor_id"],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="get_drata_vendor_questionnaire",
            description="Get a specific questionnaire sent to a vendor.",
            input_schema={
                "type": "object",
                "properties": {
                    "vendor_id": {"type": "integer"},
                    "questionnaire_id": {"type": "integer"},
                },
                "required": ["vendor_id", "questionnaire_id"],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="send_drata_vendor_questionnaire",
            description=(
                "Send a questionnaire to a vendor contact by email. questionnaire_id "
                "is the Drata questionnaire template ID; security_review_id links it "
                "to a security review (required by the API)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "vendor_id": {"type": "integer"},
                    "email": {"type": "string"},
                    "questionnaire_id": {"type": "integer"},
                    "email_content": {"type": "string"},
                    "security_review_id": {"type": "integer"},
                    "email_subject": {"type": "string"},
                },
                "required": [
                    "vendor_id",
                    "email",
                    "questionnaire_id",
                    "email_content",
                    "security_review_id",
                ],
            },
            source="integration:drata",
            priority=1,
        ),
    ]
