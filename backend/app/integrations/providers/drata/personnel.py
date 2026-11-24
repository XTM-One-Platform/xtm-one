"""Drata personnel tools - Personnel, user documents (compliance evidence), and background checks."""

from __future__ import annotations

from typing import Any

from app.core.egress import egress_client
from app.integrations.file_delivery import uploaded_file_answer
from app.integrations.http import format_entity_list
from app.tools.base import CallableToolExecutor, FileContext, ToolDefinition

from . import _common
from ._common import (
    _DRATA_NONCOMPLIANT_CHECK_STATUSES,
    _PROVIDER,
    DRATA_USER_DOCUMENT_FILE_EXTENSIONS,
    DRATA_USER_DOCUMENT_TYPES,
    DRATA_USER_DOCUMENT_UPLOAD_TYPES,
    _coerce_int,
    _full_name,
    _render_evidence_file,
    _resolve_uploaded_evidence_file,
    _user_ref,
)

#: Drata ComplianceCheckTypeEnum value of the background-check requirement.
_BG_CHECK_TYPE = "BG_CHECK"


def register(
    base_url: str,
    headers: dict[str, str],
    upload_headers: dict[str, str],
    executor: CallableToolExecutor,
) -> list[ToolDefinition]:
    """Build the Drata personnel tools and register their callables."""

    def _fmt_personnel(p: dict[str, Any]) -> str:
        user = p.get("user") or {}
        name = _full_name(user)
        email = user.get("email") or "N/A"
        checks = p.get("complianceChecks") or []
        issues = sum(1 for c in checks if (c.get("status") or "").upper() in _DRATA_NONCOMPLIANT_CHECK_STATUSES)
        if not checks:
            compliance = "no checks reported"
        elif issues:
            compliance = f"{issues} issue(s)"
        else:
            compliance = "compliant"
        return (
            f"[{p.get('id')}] {name} ({email}) — "
            f"Status: {p.get('employmentStatus', 'N/A')} | "
            f"Started: {p.get('startedAt', 'N/A')} | "
            f"Compliance: {compliance}"
        )

    async def list_drata_personnel(employment_status: str = "", compliance_status: str = "") -> str:
        """List personnel. Optionally filter by employment and/or compliance status."""
        async with egress_client(timeout=30) as client:
            params: dict[str, Any] = {"expand[]": ["user", "complianceChecks"]}
            if employment_status:
                params["employmentStatus[]"] = [employment_status]
            if compliance_status:
                params["complianceStatus[]"] = [compliance_status]
            items, err, truncated = await _common._paginated_fetch_all(client, f"{base_url}/personnel", headers, params)
            if err:
                return err
            if not items:
                return "No personnel found."
            body = "\n".join(_fmt_personnel(p) for p in items)
            result = f"Found {len(items)} personnel:\n{body}"
            if truncated:
                result += (
                    f"\n\n(Showing the first {len(items)} personnel; more exist. "
                    "Filter by employment_status or compliance_status to narrow "
                    "the result.)"
                )
            return result

    async def get_drata_personnel(personnel_id: str) -> str:
        """Get details for a specific person. Accepts numeric ID or 'email:user@host'."""
        # ``personnel_id`` (numeric ID or ``email:<address>``) is interpolated
        # into the request path, so normalize and validate it through the shared
        # guard (rejects anything that could inject ``/`` / ``?`` / ``#`` /
        # reserved chars into the Drata API URL).
        ref = _user_ref(personnel_id)
        if not ref:
            return "Error: personnel_id is required (a numeric Drata ID or an email)."
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{base_url}/personnel/{ref}",
                _PROVIDER,
                "get personnel",
                headers=headers,
                params={"expand[]": ["user", "complianceChecks", "customFields"]},
            )
            if err:
                return err
            p = resp.json()

            user = p.get("user", {})
            name = _full_name(user)

            lines = [
                f"Personnel: {name} (ID: {p.get('id')})",
                f"Email: {user.get('email', 'N/A') if user else 'N/A'}",
                # Surface the underlying user ID: personnel compliance evidence
                # (e.g. offboarding) is filed against the USER, not the personnel
                # record — upload_drata_user_document takes this user_id (or the
                # email above).
                f"User ID: {user.get('id', 'N/A') if user else 'N/A'}",
                f"Employment Status: {p.get('employmentStatus', 'N/A')}",
                f"Started: {p.get('startedAt', 'N/A')}",
                f"Separated: {p.get('separatedAt', 'N/A')}",
                f"Created: {p.get('createdAt', 'N/A')}",
            ]

            checks = p.get("complianceChecks", [])
            if checks:
                lines.append(f"\nCompliance Checks ({len(checks)}):")
                for c in checks:
                    exclusion = c.get("exclusion")
                    exc_info = f" (Excluded: {exclusion.get('reason', 'N/A')})" if exclusion else ""
                    lines.append(
                        f"  {c.get('type', '?')} — "
                        f"Status: {c.get('status', 'N/A')} | "
                        f"Frequency: {c.get('checkFrequency', 'N/A')} | "
                        f"Expires: {c.get('expiresAt', 'N/A')}{exc_info}"
                    )

            custom_fields = p.get("customFields", [])
            if custom_fields:
                lines.append("\nCustom Fields:")
                for cf in custom_fields:
                    lines.append(f"  {cf.get('name', '?')}: {cf.get('value', 'N/A')}")

            return "\n".join(lines)

    async def create_drata_personnel(
        first_name: str,
        last_name: str,
        email: str,
        employment_status: str = "CURRENT_EMPLOYEE",
        job_title: str = "",
        started_at: str = "",
        is_contractor: bool = False,
    ) -> str:
        """Create (manually add) a personnel record."""
        payload: dict[str, Any] = {
            "firstName": first_name,
            "lastName": last_name,
            "email": email,
            "employmentStatus": employment_status,
            "isContractor": is_contractor,
        }
        if job_title:
            payload["jobTitle"] = job_title
        if started_at:
            payload["startedAt"] = started_at
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "post",
                f"{base_url}/personnel",
                _PROVIDER,
                "create personnel",
                headers=headers,
                json=payload,
            )
            if err:
                return err
            p = resp.json()
            return f"Personnel created: {first_name} {last_name} <{email}> (ID: {p.get('id', '?')})"

    async def update_drata_personnel(
        personnel_id: int,
        employment_status: str = "",
        job_title: str = "",
        started_at: str = "",
        separated_at: str = "",
    ) -> str:
        """Update an existing personnel record. Only provided fields are changed."""
        payload: dict[str, Any] = {}
        if employment_status:
            payload["employmentStatus"] = employment_status
        if job_title:
            payload["jobTitle"] = job_title
        if started_at:
            payload["startedAt"] = started_at
        if separated_at:
            payload["separatedAt"] = separated_at
        if not payload:
            return "Error: provide at least one field to update."
        async with egress_client(timeout=30) as client:
            _, err = await _common.safe_request(
                client,
                "put",
                f"{base_url}/personnel/{personnel_id}",
                _PROVIDER,
                "update personnel",
                headers=headers,
                json=payload,
            )
            if err:
                return err
            return f"Personnel {personnel_id} updated."

    def _fmt_user_document(d: dict[str, Any]) -> str:
        return (
            f"[{d.get('id')}] {d.get('name', 'Unnamed')} — "
            f"Type: {d.get('type', 'N/A')} | "
            f"Renewal: {d.get('renewalDate', 'N/A')} | "
            f"Created: {d.get('createdAt', 'N/A')}"
        )

    async def list_drata_user_documents(user_id: str, doc_type: str = "", name: str = "") -> str:
        """List a user's manual compliance documents (incl. offboarding evidence).

        ``user_id`` accepts a numeric Drata user ID or an email (``leaver@acme.com``
        or ``email:leaver@acme.com``). Optionally filter by ``doc_type`` (e.g.
        ``OFFBOARDING_EVIDENCE``) or ``name``.
        """
        ref = _user_ref(user_id)
        if not ref:
            return "Error: user_id is required (a Drata user ID or an email)."
        doc_type = (doc_type or "").strip().upper()
        if doc_type and doc_type not in DRATA_USER_DOCUMENT_TYPES:
            return f"Error: invalid doc_type '{doc_type}'. Use one of: {', '.join(sorted(DRATA_USER_DOCUMENT_TYPES))}."
        async with egress_client(timeout=30) as client:
            params: dict[str, Any] = {}
            if doc_type:
                params["type"] = doc_type
            if name:
                params["name"] = name
            items, err, truncated = await _common._paginated_fetch_all(
                client, f"{base_url}/users/{ref}/documents", headers, params
            )
            if err:
                return err
            result = format_entity_list(items, _fmt_user_document, noun="user document")
            if truncated:
                result += (
                    f"\n\n(Showing the first {len(items)} documents; more exist. "
                    "Filter by doc_type or name to narrow the result.)"
                )
            return result

    async def get_drata_user_document(user_id: str, document_id: int) -> str:
        """Get one user compliance document, with its name, type and download URL.

        ``user_id`` accepts a numeric Drata user ID or an email.
        """
        ref = _user_ref(user_id)
        if not ref:
            return "Error: user_id is required (a Drata user ID or an email)."
        did = _coerce_int(document_id)
        if not did:
            return "Error: document_id must be a numeric user-document ID."
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{base_url}/users/{ref}/documents/{did}",
                _PROVIDER,
                "get user document",
                headers=headers,
                params={"expand[]": ["downloadUrl"]},
            )
            if err:
                return err
            d = resp.json()
            download = d.get("downloadUrl")
            signed = ""
            if isinstance(download, dict):
                signed = download.get("signedUrl") or ""
            elif isinstance(download, str):
                signed = download
            lines = [
                f"User Document: {d.get('name', 'Unnamed')} (ID: {d.get('id')})",
                f"Type: {d.get('type', 'N/A')}",
                f"User ID: {d.get('userId', 'N/A')}",
                f"Renewal Date: {d.get('renewalDate', 'N/A')}",
                f"Created: {d.get('createdAt', 'N/A')}",
                f"Updated: {d.get('updatedAt', 'N/A')}",
            ]
            if signed:
                lines.append(f"Download URL: {signed}")
            return "\n".join(lines)

    async def upload_drata_user_document(
        user_id: str,
        doc_type: str = "OFFBOARDING_EVIDENCE",
        file_content: str = "",
        file_name: str = "",
        file_encoding: str = "text",
        source_file_id: str = "",
        completion_date: str = "",
        file_context: FileContext | None = None,
    ) -> str:
        """Upload manual compliance evidence for a user (incl. OFFBOARDING).

        This is the write path for **personnel offboarding evidence**: file an
        artifact with ``doc_type='OFFBOARDING_EVIDENCE'`` (the default) to satisfy
        a leaver's OFFBOARDING personnel compliance check. It is a DISTINCT
        surface from the workspace Evidence Library — offboarding proof filed
        there will NOT clear the personnel check, so use this tool instead.

        ``user_id`` accepts a numeric Drata user ID or an email (e.g.
        ``leaver@acme.com``) — convenient when reconciling leavers from an HRIS by
        email rather than numeric ID.

        Provide ONE artifact source:

        * ``source_file_id`` — a file the user already uploaded to this
          conversation, forwarded byte-for-byte (e.g. a signed termination
          letter / offboarding checklist filed as-is).
        * ``file_content`` — Markdown / HTML rendered to a real PDF (``.pdf``, the
          default) or DOCX (``.docx``), or decoded from base64 when
          ``file_encoding='base64'`` (for a pre-built image / Office file). Plain
          text / markdown files are NOT accepted by this endpoint, so author the
          recap and let it render to PDF.

        ``doc_type`` must be one of MFA_EVIDENCE, SEC_TRAINING,
        HIPAA_TRAINING_EVIDENCE, OFFBOARDING_EVIDENCE. ``completion_date``
        (YYYY-MM-DD) records when the compliance task was completed.
        """
        ref = _user_ref(user_id)
        if not ref:
            return "Error: user_id is required (a Drata user ID or an email)."
        doc_type = (doc_type or "").strip().upper()
        if doc_type not in DRATA_USER_DOCUMENT_UPLOAD_TYPES:
            return (
                f"Error: invalid doc_type '{doc_type or '(empty)'}'. Drata only "
                "accepts these manual user-document types via the API: "
                f"{', '.join(sorted(DRATA_USER_DOCUMENT_UPLOAD_TYPES))}."
            )
        sources = [bool(file_content), bool(source_file_id.strip())]
        if sum(sources) != 1:
            return "Error: provide exactly one artifact source — file_content or source_file_id."
        if source_file_id.strip():
            resolved = await _resolve_uploaded_evidence_file(
                file_context,
                source_file_id.strip(),
                file_name,
                accepted_extensions=DRATA_USER_DOCUMENT_FILE_EXTENSIONS,
            )
        else:
            resolved = await _render_evidence_file(
                file_content,
                file_name,
                file_encoding,
                fallback_name="offboarding-evidence",
                accepted_extensions=DRATA_USER_DOCUMENT_FILE_EXTENSIONS,
            )
        if isinstance(resolved, str):
            return resolved
        file_bytes, resolved_name, mime = resolved
        data: dict[str, Any] = {"type": doc_type}
        completion = (completion_date or "").strip()
        if completion:
            data["completionDate"] = completion
        files = {"file": (resolved_name, file_bytes, mime)}
        async with egress_client(timeout=120) as client:
            resp, err = await _common.safe_request(
                client,
                "post",
                f"{base_url}/users/{ref}/documents",
                _PROVIDER,
                "upload user document",
                headers=upload_headers,
                data=data,
                files=files,
            )
            if err:
                return err
            d = resp.json() if resp.content else {}
            doc_id = d.get("id", "?")
            return uploaded_file_answer(
                f"Filed the {doc_type} document for user {ref} "
                f"(document ID: {doc_id}). Verify with get_drata_user_document("
                f"user_id='{ref}', document_id={doc_id}).",
                target=_PROVIDER,
                filename=resolved_name,
            )

    async def delete_drata_user_document(user_id: str, document_id: int) -> str:
        """Delete a user compliance document.

        ``user_id`` accepts a numeric Drata user ID or an email.
        """
        ref = _user_ref(user_id)
        if not ref:
            return "Error: user_id is required (a Drata user ID or an email)."
        did = _coerce_int(document_id)
        if not did:
            return "Error: document_id must be a numeric user-document ID."
        async with egress_client(timeout=30) as client:
            _, err = await _common.safe_request(
                client,
                "delete",
                f"{base_url}/users/{ref}/documents/{did}",
                _PROVIDER,
                "delete user document",
                headers=headers,
            )
            if err:
                return err
            return f"User document {did} deleted for user {ref}."

    def _fmt_background_check(p: dict[str, Any], check: dict[str, Any]) -> str:
        user = p.get("user") or {}
        exclusion = check.get("exclusion") or {}
        excluded = f" | Excluded: {exclusion.get('reason') or 'yes'}" if exclusion else ""
        return (
            f"[{p.get('id')}] {_full_name(user)} ({user.get('email') or 'N/A'}, user {user.get('id', '?')}) — "
            f"Employment: {p.get('employmentStatus', 'N/A')} | "
            f"Status: {check.get('status', 'N/A')} | "
            f"Completed: {check.get('completionDate') or 'N/A'} | "
            f"Last checked: {check.get('lastCheckedAt') or 'N/A'} | "
            f"Expires: {check.get('expiresAt') or 'N/A'}{excluded}"
        )

    async def list_drata_background_checks(user_id: int = 0) -> str:
        """List each person's background-check compliance (the BG_CHECK check).

        Drata's public API v2 has no route that lists background checks
        (``/background-checks`` serves POST only): the state lives on each
        Personnel record as its ``BG_CHECK`` compliance check, read through
        ``GET /personnel`` with ``expand[]=complianceChecks``.
        """
        async with egress_client(timeout=30) as client:
            params: dict[str, Any] = {"expand[]": ["user", "complianceChecks"]}
            items, err, truncated = await _common._paginated_fetch_all(client, f"{base_url}/personnel", headers, params)
            if err:
                return err
        uid = _coerce_int(user_id)
        if uid:
            items = [p for p in items if _coerce_int((p.get("user") or {}).get("id")) == uid]
        rows = [
            (p, c)
            for p in items
            for c in (p.get("complianceChecks") or [])
            if (c.get("type") or "").upper() == _BG_CHECK_TYPE
        ]
        if not rows:
            scope = f" for user {uid}" if uid else ""
            return (
                f"No background-check compliance found{scope}: {len(items)} personnel read, "
                "none carries a BG_CHECK compliance check."
            )
        result = f"Found {len(rows)} background check(s):\n" + "\n".join(_fmt_background_check(p, c) for p, c in rows)
        if truncated:
            result += f"\n\n(Read the first {len(items)} personnel; more exist.)"
        return result

    async def create_drata_background_check(
        user_id: int,
        status: str = "OK",
        manual_check_date: str = "",
        manually_check_url: str = "",
        check_type: str = "MANUAL",
    ) -> str:
        """Create a manual background check for a user."""
        payload: dict[str, Any] = {
            "userId": user_id,
            "status": status,
            "type": check_type,
        }
        if manual_check_date:
            payload["manualCheckDate"] = manual_check_date
        if manually_check_url:
            payload["manuallyCheckUrl"] = manually_check_url
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "post",
                f"{base_url}/background-checks",
                _PROVIDER,
                "create background check",
                headers=headers,
                json=payload,
            )
            if err:
                return err
            b = resp.json() if resp.content else {}
            return f"Background check created for user {user_id} (ID: {b.get('id', '?')}, status: {status})."

    callable_map = {
        "list_drata_personnel": list_drata_personnel,
        "get_drata_personnel": get_drata_personnel,
        "create_drata_personnel": create_drata_personnel,
        "update_drata_personnel": update_drata_personnel,
        "upload_drata_user_document": upload_drata_user_document,
        "list_drata_user_documents": list_drata_user_documents,
        "get_drata_user_document": get_drata_user_document,
        "delete_drata_user_document": delete_drata_user_document,
        "list_drata_background_checks": list_drata_background_checks,
        "create_drata_background_check": create_drata_background_check,
    }
    for _name, _fn in callable_map.items():
        executor.register(_name, _fn)

    return [
        ToolDefinition(
            name="list_drata_personnel",
            personal_records=True,
            description="List personnel with compliance status. Optionally filter by employment and/or compliance status.",
            input_schema={
                "type": "object",
                "properties": {
                    "employment_status": {
                        "type": "string",
                        "enum": [
                            "CURRENT_EMPLOYEE",
                            "FORMER_EMPLOYEE",
                            "CURRENT_CONTRACTOR",
                            "FORMER_CONTRACTOR",
                            "FUTURE_HIRE",
                            "UNKNOWN",
                            "OUT_OF_SCOPE",
                            "SERVICE_ACCOUNT",
                        ],
                    },
                    "compliance_status": {
                        "type": "string",
                        "enum": ["MISCONFIGURED", "PASS", "FAIL", "EXCLUDED"],
                        "description": "Filter by overall compliance check status",
                    },
                },
                "required": [],
            },
            source="integration:drata",
            priority=1,
        ),
        ToolDefinition(
            name="get_drata_personnel",
            personal_records=True,
            description="Get details for a specific person. Accepts numeric ID or 'email:user@host'.",
            input_schema={
                "type": "object",
                "properties": {
                    "personnel_id": {"type": "string"},
                },
                "required": ["personnel_id"],
            },
            source="integration:drata",
            priority=1,
        ),
        ToolDefinition(
            name="create_drata_personnel",
            description="Manually create a personnel record in Drata.",
            input_schema={
                "type": "object",
                "properties": {
                    "first_name": {"type": "string"},
                    "last_name": {"type": "string"},
                    "email": {"type": "string"},
                    "employment_status": {"type": "string", "default": "CURRENT_EMPLOYEE"},
                    "job_title": {"type": "string"},
                    "started_at": {"type": "string", "description": "YYYY-MM-DD"},
                    "is_contractor": {"type": "boolean", "default": False},
                },
                "required": ["first_name", "last_name", "email"],
            },
            source="integration:drata",
            priority=1,
        ),
        ToolDefinition(
            name="update_drata_personnel",
            description="Update an existing personnel record. Only provided fields are changed.",
            input_schema={
                "type": "object",
                "properties": {
                    "personnel_id": {"type": "integer"},
                    "employment_status": {"type": "string"},
                    "job_title": {"type": "string"},
                    "started_at": {"type": "string"},
                    "separated_at": {"type": "string"},
                },
                "required": ["personnel_id"],
            },
            source="integration:drata",
            priority=1,
        ),
        ToolDefinition(
            name="upload_drata_user_document",
            description=(
                "File manual compliance evidence for a Drata user, including "
                "PERSONNEL OFFBOARDING EVIDENCE (doc_type=OFFBOARDING_EVIDENCE, "
                "the default) to satisfy a leaver's OFFBOARDING compliance check. "
                "This is a distinct surface from the Evidence Library — offboarding "
                "proof must be filed here, not as an evidence-library item. "
                "user_id accepts a numeric Drata user ID or an email (e.g. "
                "leaver@acme.com). Provide exactly one source: source_file_id "
                "(forward a user-uploaded file byte-for-byte) or file_content "
                "(Markdown/HTML rendered to PDF/DOCX, or base64 with "
                "file_encoding='base64')."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "user_id": {
                        "type": "string",
                        "description": ("Drata user ID or email (e.g. '123' or 'leaver@acme.com')"),
                    },
                    "doc_type": {
                        "type": "string",
                        "enum": sorted(DRATA_USER_DOCUMENT_UPLOAD_TYPES),
                        "default": "OFFBOARDING_EVIDENCE",
                        "description": "User document type (write enum)",
                    },
                    "file_content": {
                        "type": "string",
                        "description": (
                            "Markdown/HTML rendered to PDF (default) or DOCX, or "
                            "base64 bytes when file_encoding='base64'"
                        ),
                    },
                    "file_name": {
                        "type": "string",
                        "description": (
                            "Artifact file name; defaults to a .pdf. Accepted: "
                            ".pdf, .docx, .odt, .doc, .xlsx, .ods, .pptx, .odp, "
                            ".gif, .jpg, .jpeg, .png"
                        ),
                    },
                    "file_encoding": {
                        "type": "string",
                        "enum": ["text", "base64"],
                        "default": "text",
                    },
                    "source_file_id": {
                        "type": "string",
                        "description": (
                            "file_id of a file the user uploaded to this conversation, forwarded byte-for-byte"
                        ),
                    },
                    "completion_date": {
                        "type": "string",
                        "description": ("YYYY-MM-DD date the compliance task was completed"),
                    },
                },
                "required": ["user_id"],
            },
            source="integration:drata",
            priority=1,
        ),
        ToolDefinition(
            name="list_drata_user_documents",
            description=(
                "List a user's manual compliance documents (e.g. offboarding "
                "evidence). user_id accepts a numeric Drata user ID or an email. "
                "Optionally filter by doc_type or name."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "user_id": {
                        "type": "string",
                        "description": "Drata user ID or email",
                    },
                    "doc_type": {
                        "type": "string",
                        "enum": sorted(DRATA_USER_DOCUMENT_TYPES),
                        "description": "Filter by user document type",
                    },
                    "name": {
                        "type": "string",
                        "description": "Filter by document name",
                    },
                },
                "required": ["user_id"],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="get_drata_user_document",
            description=(
                "Get one user compliance document with its name, type and a "
                "download URL. user_id accepts a numeric Drata user ID or an email."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "user_id": {
                        "type": "string",
                        "description": "Drata user ID or email",
                    },
                    "document_id": {"type": "integer"},
                },
                "required": ["user_id", "document_id"],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="delete_drata_user_document",
            description=("Delete a user compliance document. user_id accepts a numeric Drata user ID or an email."),
            input_schema={
                "type": "object",
                "properties": {
                    "user_id": {
                        "type": "string",
                        "description": "Drata user ID or email",
                    },
                    "document_id": {"type": "integer"},
                },
                "required": ["user_id", "document_id"],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="list_drata_background_checks",
            description=(
                "List each person's background-check compliance (Drata BG_CHECK check): personnel id, name, "
                "email, user id, employment, status, completion, last checked, expiry, exclusion."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "user_id": {"type": "integer", "description": "Drata user ID to scope to one person"},
                },
                "required": [],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="create_drata_background_check",
            description="Create (manually log) a background check for a Drata user.",
            input_schema={
                "type": "object",
                "properties": {
                    "user_id": {"type": "integer"},
                    "status": {
                        "type": "string",
                        "enum": ["OK", "FLAGGED", "PENDING", "EXEMPT"],
                        "default": "OK",
                    },
                    "manual_check_date": {"type": "string"},
                    "manually_check_url": {"type": "string"},
                    "check_type": {"type": "string", "default": "MANUAL"},
                },
                "required": ["user_id"],
            },
            source="integration:drata",
            priority=2,
        ),
    ]
