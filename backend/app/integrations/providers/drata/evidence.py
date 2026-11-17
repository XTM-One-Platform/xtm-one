"""Drata evidence tools - Evidence Library items + artifact versions."""

from __future__ import annotations

import logging
import mimetypes
from datetime import date
from typing import Any
from urllib.parse import unquote, urlparse

from app.core.egress import egress_client
from app.core.utils import format_exception_message
from app.integrations.file_delivery import untrusted_file_facts
from app.tools.base import (
    CallableToolExecutor,
    FileContext,
    ToolDefinition,
    ToolResult,
)

from . import _common
from ._common import (
    _PROVIDER,
    _coerce_int,
    _evidence_artifact_meta,
    _evidence_write_summary,
    _full_name,
    _int_csv,
    _render_evidence_file,
    _resolve_uploaded_evidence_file,
)

logger = logging.getLogger(__name__)

# Overall guard so a single download can't read an unbounded blob into memory.
_MAX_DRATA_DOWNLOAD_BYTES = 100 * 1024 * 1024  # 100 MB
# Drata serves document/evidence downloads from its own S3 buckets (signed
# URLs) / CDN. ``download_drata_file`` restricts its server-side fetch to these
# so a crafted url can't turn the tool into an SSRF vector.
_DRATA_FILE_HOST_SUFFIXES = ("amazonaws.com", "drata.com")


def _is_drata_file_host(url: str) -> bool:
    """True if *url* points at a Drata-hosted/AWS file host (download allow-list)."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return bool(host) and any(host == suffix or host.endswith("." + suffix) for suffix in _DRATA_FILE_HOST_SUFFIXES)


def _drata_filename_from_url(url: str) -> str:
    """Best-effort filename from a signed download URL path."""
    try:
        path = urlparse(url).path
    except ValueError:
        return ""
    return unquote(path.rsplit("/", 1)[-1]).strip()


#: Drata v2 ``ComputedEvidenceStatusEnum``: the values ``evidenceStatuses[]``
#: of List Evidence Items accepts (an unknown value is a 400 from Drata).
_EVIDENCE_STATUSES: tuple[str, ...] = (
    "NEEDS_ARTIFACT",
    "EXPIRED",
    "EXPIRING_SOON",
    "TEST_ERROR",
    "TEST_FAILED",
    "READY",
    "TEST_PASSED",
    "TEST_UNUSED",
    "TEST_DISABLED",
    "TEST_DELETED",
)
#: Words an agent uses that are not Drata values, mapped to the statuses they
#: mean. "Needs renewal" is the Drata UI's wording for an item whose renewal
#: date is near or past.
_EVIDENCE_STATUS_ALIASES: dict[str, tuple[str, ...]] = {
    "NEEDS_RENEWAL": ("EXPIRING_SOON", "EXPIRED"),
}
#: The list reads every page up to this many items (Drata caps one page at
#: 500): past it the header says the count is incomplete.
_EVIDENCE_LIST_MAX_ITEMS = 5000


def _evidence_status_filter(status: str) -> tuple[list[str], str | None]:
    """``status`` (comma-separated, case-insensitive) as Drata statuses, or an error."""
    statuses: list[str] = []
    for raw in (status or "").split(","):
        value = raw.strip().upper().replace("-", "_").replace(" ", "_")
        if not value:
            continue
        expanded = _EVIDENCE_STATUS_ALIASES.get(value, (value,))
        for one in expanded:
            if one not in _EVIDENCE_STATUSES:
                accepted = ", ".join((*_EVIDENCE_STATUSES, *_EVIDENCE_STATUS_ALIASES))
                return [], f"Error: unknown evidence status '{raw.strip()}'. Accepted: {accepted}."
            if one not in statuses:
                statuses.append(one)
    return statuses, None


def _parse_iso_date(value: str, field: str) -> date | None:
    """``YYYY-MM-DD`` (a datetime is cut to its date) as a date; empty is None."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        raise ValueError(f"{field} must be a date YYYY-MM-DD, got '{value}'.") from None


def _renewal_in_window(item: dict[str, Any], before: date | None, after: date | None) -> bool:
    """True when the item's renewal date is in [after, before]; no date never matches."""
    raw = item.get("renewalDate")
    if not raw:
        return False
    try:
        renewal = date.fromisoformat(str(raw)[:10])
    except ValueError:
        return False
    if before and renewal > before:
        return False
    return not (after and renewal < after)


def _evidence_owners(item: dict[str, Any]) -> str:
    """Owner names from the expanded ``owners`` (``{data: [...]}`` or a list)."""
    owners = item.get("owners")
    if isinstance(owners, dict):
        owners = owners.get("data")
    if not owners:
        return "none"
    return ", ".join(_full_name(o) for o in owners if isinstance(o, dict)) or "none"


def _evidence_artifact_types(item: dict[str, Any]) -> str:
    """Distinct artifact types of the expanded ``artifacts``, else N/A."""
    artifacts = item.get("artifacts")
    if isinstance(artifacts, dict):
        artifacts = artifacts.get("data")
    types: list[str] = []
    for art in artifacts or []:
        kind = art.get("artifactType") if isinstance(art, dict) else None
        if kind and kind not in types:
            types.append(kind)
    return ", ".join(types) or "N/A"


def _format_evidence_row(e: dict[str, Any]) -> str:
    schedule = e.get("renewalScheduleType")
    renewal = e.get("renewalDate") or "N/A"
    if schedule:
        renewal = f"{renewal} ({schedule})"
    return (
        f"[{e.get('id')}] {e.get('name') or 'Unnamed'} | "
        f"Status: {e.get('status') or 'N/A'} | "
        f"Renewal: {renewal} | "
        f"Owner: {_evidence_owners(e)} | "
        f"Artifacts: {_evidence_artifact_types(e)}"
    )


def _format_evidence_list(items: list[dict[str, Any]], *, truncated: bool, statuses: list[str]) -> str:
    """The rows under a header stating the count and whether it is complete."""
    scope = f" with status {' or '.join(statuses)}" if statuses else ""
    if not items and not truncated:
        return f"No evidence items found{scope} (complete)."
    count = len(items)
    noun = "evidence item" if count == 1 else "evidence items"
    if truncated:
        header = (
            f"Found {count} {noun}{scope} (INCOMPLETE: the read stopped at "
            f"{_EVIDENCE_LIST_MAX_ITEMS} items and Drata has more; narrow with name or status)."
        )
    else:
        header = f"Found {count} {noun}{scope} (complete: every page read)."
    return header + "\n" + "\n".join(_format_evidence_row(e) for e in items)


def register(
    base_url: str,
    headers: dict[str, str],
    upload_headers: dict[str, str],
    executor: CallableToolExecutor,
    *,
    persist_file_fn=None,
) -> list[ToolDefinition]:
    """Build the Drata evidence tools and register their callables."""

    async def list_drata_evidence(
        workspace_id: int = 1,
        name: str = "",
        status: str = "",
        renewal_before: str = "",
        renewal_after: str = "",
    ) -> str:
        """List the evidence items of a workspace with status, renewal and owner.

        Reads Drata v2 ``GET /workspaces/{id}/evidence`` (List Evidence Items),
        which carries ``status`` / ``renewalDate`` / ``renewalScheduleType`` and
        expands ``owners`` and ``artifacts``; the legacy ``/evidence-library``
        list has no status field and ignored the old ``status=`` parameter.
        The status filter goes to Drata as ``evidenceStatuses[]``; every page
        is read so the count is exact, and the header says when it is not.
        """
        statuses, err = _evidence_status_filter(status)
        if err:
            return err
        try:
            before = _parse_iso_date(renewal_before, "renewal_before")
            after = _parse_iso_date(renewal_after, "renewal_after")
        except ValueError as exc:
            return f"Error: {exc}"
        params: dict[str, Any] = {"expand[]": ["owners", "artifacts"]}
        if name:
            params["name"] = name
        if statuses:
            params["evidenceStatuses[]"] = statuses
        async with egress_client(timeout=30) as client:
            items, err, truncated = await _common._paginated_fetch_all(
                client,
                f"{base_url}/workspaces/{workspace_id}/evidence",
                headers,
                params,
                max_items=_EVIDENCE_LIST_MAX_ITEMS,
                page_size=_common._DRATA_MAX_PAGE_SIZE,
                action="list evidence",
            )
        if err:
            return err
        if before or after:
            items = [e for e in items if _renewal_in_window(e, before, after)]
        return _format_evidence_list(items, truncated=truncated, statuses=statuses)

    async def get_drata_evidence(workspace_id: int, evidence_id: int) -> str:
        """Get details of a specific evidence library item."""
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{base_url}/workspaces/{workspace_id}/evidence-library/{evidence_id}",
                _PROVIDER,
                "get evidence",
                headers=headers,
                # Drata v2 ``EvidenceLibraryExpandEnum`` only accepts these three
                # values — the old ``owner`` / ``customFields`` values are not in
                # the enum and Drata rejects the whole request with HTTP 400.
                params={"expand[]": ["user", "controls", "renewalSchemaAndVersions"]},
            )
            if err:
                return err
            e = resp.json()
            # The owner is returned under ``user`` (not ``owner``) and the
            # renewal date is nested under ``renewalSchema`` (not a top-level
            # ``renewalDate``) — see the v2 Get Evidence Library Item schema.
            renewal = e.get("renewalSchema") or {}
            lines = [
                f"Evidence: {e.get('name', 'Unnamed')} (ID: {e.get('id')})",
                f"Description: {e.get('description', 'N/A')}",
                f"Status: {e.get('status', 'N/A')}",
                f"Renewal: {renewal.get('renewalDate', 'N/A')} ({renewal.get('renewalScheduleType', 'N/A')})",
                f"Created: {e.get('createdAt', 'N/A')}",
                # ``updatedAt`` is the item's last-modified timestamp — a
                # freshness signal the agent can use to confirm a just-filed
                # change landed (read-your-write) without guessing.
                f"Updated: {e.get('updatedAt', 'N/A')}",
                f"Owner: {_full_name(e.get('user'))}",
            ]
            versions = e.get("versions") or []
            if versions:
                lines.append(f"\nArtifacts ({len(versions)}):")
                for v in versions:
                    marker = " (current)" if v.get("current") else ""
                    label = v.get("name") or v.get("source") or v.get("type") or "artifact"
                    # Surface the version's own ``id`` — that is what
                    # get_drata_evidence_version needs to fetch the file name +
                    # download URL. The human ``version`` number is only present
                    # on some payloads, so show it alongside when available.
                    vid = v.get("id", "?")
                    human = v.get("version")
                    tag = f"version_id={vid}"
                    if human is not None:
                        tag += f", v{human}"
                    created = v.get("createdAt") or v.get("filedAt")
                    created_str = f" | Filed/Created: {created}" if created else ""
                    lines.append(f"  [{tag}] {label} — Type: {v.get('type', 'N/A')}{created_str}{marker}")
                lines.append(
                    "Tip: call get_drata_evidence_version with a version_id "
                    "above to get the artifact's file name and download URL."
                )
            controls = e.get("controls") or []
            if controls:
                lines.append(f"\nLinked Controls ({len(controls)}):")
                for c in controls:
                    lines.append(f"  [{c.get('code', '?')}] {c.get('name', 'Untitled')}")
            return "\n".join(lines)

    async def get_drata_evidence_version(workspace_id: int, evidence_id: int, version_id: int) -> str:
        """Get one evidence artifact version, with its file name and a download URL."""
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{base_url}/workspaces/{workspace_id}/evidence-library/{evidence_id}/versions/{version_id}",
                _PROVIDER,
                "get evidence version",
                headers=headers,
                # ``downloadUrl`` is the only value the v2
                # ``EvidenceLibraryVersionExpandEnum`` accepts — it returns a
                # link the agent can use to verify the stored artifact
                # byte-for-byte instead of inferring it.
                params={"expand[]": ["downloadUrl"]},
            )
            if err:
                return err
            v = resp.json()
            marker = " (current)" if v.get("current") else ""
            lines = [
                f"Evidence artifact version (version_id={v.get('id', version_id)}){marker}",
                f"File: {v.get('name', 'N/A')}",
                f"Version: {v.get('version', 'N/A')}",
                f"Type: {v.get('type', 'N/A')}",
                f"Source: {v.get('source') or 'N/A'}",
                f"Filed: {v.get('filedAt', 'N/A')}",
                f"Created: {v.get('createdAt', 'N/A')}",
                f"Updated: {v.get('updatedAt', 'N/A')}",
            ]
            download = v.get("downloadUrl")
            if download:
                lines.append(f"Download URL: {download}")
            else:
                lines.append(
                    "Download URL: not returned (the artifact is likely a "
                    "URL/ticket link rather than an uploaded file)."
                )
            return "\n".join(lines)

    async def _send_evidence_artifact(
        *,
        method: str,
        url: str,
        action: str,
        base_fields: dict[str, str],
        control_id_list: list[int],
        meta: dict[str, Any],
        file_content: str,
        file_name: str,
        file_encoding: str,
        source_url: str,
        fallback_name: str,
        prebuilt: tuple[bytes, str, str] | None = None,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Send an Evidence Library artifact create/update request.

        A binary ``file`` goes out as ``multipart/form-data`` (the only way to
        upload bytes); a ``url`` artifact uses the JSON variant so ``controlIds``
        stays a clean numeric array. Returns ``(response_json, None)`` or
        ``(None, error_string)``.

        ``prebuilt`` carries already-resolved ``(bytes, file_name, mime_type)``
        for the user-uploaded-file path (``source_file_id``): the original bytes
        are uploaded verbatim, skipping the Markdown/HTML render + base64 decode
        that agent-authored ``file_content`` goes through.
        """
        if prebuilt is not None or file_content:
            if prebuilt is not None:
                file_bytes, resolved_name, mime = prebuilt
            else:
                resolved = await _render_evidence_file(
                    file_content, file_name, file_encoding, fallback_name=fallback_name
                )
                if isinstance(resolved, str):
                    return None, resolved
                file_bytes, resolved_name, mime = resolved
            data: dict[str, Any] = {k: str(v) for k, v in meta.items()}
            data.update(base_fields)
            if control_id_list:
                # Repeated form fields — httpx emits one ``controlIds`` part per
                # value, matching the v2 multipart array convention.
                data["controlIds"] = [str(cid) for cid in control_id_list]
            files = {"file": (resolved_name, file_bytes, mime)}
            async with egress_client(timeout=120) as client:
                resp, err = await _common.safe_request(
                    client,
                    method,
                    url,
                    _PROVIDER,
                    action,
                    headers=upload_headers,
                    data=data,
                    files=files,
                )
        else:
            clean_url = (source_url or "").strip()
            if not clean_url:
                return None, (
                    "Error: source_url is empty. Provide a non-blank URL to link "
                    "an external artifact, or pass file_content to upload a file."
                )
            payload: dict[str, Any] = {"url": clean_url, **meta}
            payload.update(base_fields)
            if control_id_list:
                payload["controlIds"] = control_id_list
            async with egress_client(timeout=60) as client:
                resp, err = await _common.safe_request(
                    client,
                    method,
                    url,
                    _PROVIDER,
                    action,
                    headers=headers,
                    json=payload,
                )
        if err:
            return None, err
        return (resp.json() if resp.content else {}), None

    async def create_drata_evidence(
        workspace_id: int,
        name: str,
        description: str = "",
        evidence_type: str = "",
        owner_id: int = 0,
        control_ids: str = "",
        renewal_date: str = "",
        file_content: str = "",
        file_name: str = "",
        file_encoding: str = "text",
        source_url: str = "",
        source_file_id: str = "",
        filed_at: str = "",
        renewal_schedule_type: str = "",
        file_context: FileContext | None = None,
    ) -> str:
        """Create a new evidence library item, optionally with a real artifact.

        ``control_ids`` is a CSV list of control IDs to link the evidence to.

        To attach an artifact, supply ONE source:

        * ``source_file_id`` — file a file the user already uploaded to this
          conversation (or attached to the assignment trigger), uploaded
          **byte-for-byte** by its file_id. Use this to attach an existing
          PDF/document/image as-is instead of re-authoring it.
        * ``file_content`` — Markdown / HTML rendered to a real PDF (``.pdf``
          file_name, the default) or DOCX (``.docx``), uploaded verbatim as
          text for ``.md`` / ``.txt`` / ``.html`` / ``.csv`` / …, or decoded
          from base64 when ``file_encoding='base64'``.
        * ``source_url`` — link an external artifact by URL instead.

        When an artifact is attached Drata requires an ``owner_id`` (a Drata
        user ID); ``filed_at`` defaults to today and ``renewal_schedule_type``
        defaults to ``ONE_YEAR``.
        """
        name = (name or "").strip()
        if not name:
            return "Error: evidence name is required."
        # A whitespace-only source_url is effectively empty (``_send_evidence_artifact``
        # strips it), so don't let it trip the "one source" guard and block a
        # valid file_content / source_file_id upload.
        sources = [
            bool(file_content),
            bool(source_url.strip()),
            bool(source_file_id.strip()),
        ]
        if sum(sources) > 1:
            return "Error: provide only one artifact source — file_content, source_url, or source_file_id."
        control_id_list = _int_csv(control_ids)
        url = f"{base_url}/workspaces/{workspace_id}/evidence-library"

        if file_content or source_url or source_file_id.strip():
            # Forward a user-uploaded file as-is when referenced by id (resolve
            # before hitting Drata so a missing/expired file fails fast).
            prebuilt: tuple[bytes, str, str] | None = None
            if source_file_id.strip():
                resolved_upload = await _resolve_uploaded_evidence_file(file_context, source_file_id.strip(), file_name)
                if isinstance(resolved_upload, str):
                    return resolved_upload
                prebuilt = resolved_upload
            meta, meta_err = _evidence_artifact_meta(
                owner_id,
                filed_at,
                renewal_schedule_type,
                renewal_date,
                owner_required=True,
            )
            if meta_err:
                return meta_err
            base_fields = {"name": name}
            if description:
                base_fields["description"] = description
            # The evidence ``type`` applies on the artifact path too: base_fields
            # flows into both the multipart form data and the URL-artifact JSON,
            # exactly like ``name`` / ``description`` (and the no-artifact path
            # sends ``type`` as well), so evidence_type is honoured here.
            if evidence_type:
                base_fields["type"] = evidence_type
            data, err = await _send_evidence_artifact(
                method="post",
                url=url,
                action="upload evidence file",
                base_fields=base_fields,
                control_id_list=control_id_list,
                meta=meta,
                file_content=file_content,
                file_name=file_name,
                file_encoding=file_encoding,
                source_url=source_url,
                fallback_name=name,
                prebuilt=prebuilt,
            )
            if err:
                return err
            artifact = "file" if (file_content or prebuilt) else "URL artifact"
            return _evidence_write_summary(
                f"Evidence '{name}' created with {artifact}",
                data,
                workspace_id=workspace_id,
                fallback_id=None,
            )

        # No artifact — text-only evidence library item (JSON).
        payload: dict[str, Any] = {"name": name}
        if description:
            payload["description"] = description
        if evidence_type:
            payload["type"] = evidence_type
        if owner_id:
            payload["ownerId"] = int(owner_id)
        if renewal_date:
            payload["renewalDate"] = renewal_date
        if control_id_list:
            payload["controlIds"] = control_id_list
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "post",
                url,
                _PROVIDER,
                "create evidence",
                headers=headers,
                json=payload,
            )
            if err:
                return err
            e = resp.json() if resp.content else {}
            return f"Evidence '{name}' created (ID: {e.get('id', '?')})."

    async def update_drata_evidence(
        workspace_id: int,
        evidence_id: int,
        name: str = "",
        description: str = "",
        status: str = "",
        renewal_date: str = "",
        control_ids: str = "",
        file_content: str = "",
        file_name: str = "",
        file_encoding: str = "text",
        source_url: str = "",
        source_file_id: str = "",
        owner_id: int = 0,
        filed_at: str = "",
        renewal_schedule_type: str = "",
        file_context: FileContext | None = None,
    ) -> str:
        """Update an evidence library item. Only provided fields are changed.

        Attaching ``source_file_id`` (a file the user uploaded — forwarded
        byte-for-byte), ``file_content`` (rendered to PDF/DOCX or uploaded
        verbatim), or ``source_url`` files a NEW artifact version against the
        item — the standard evidence-renewal workflow. ``filed_at`` defaults to
        today and ``renewal_schedule_type`` defaults to ``ONE_YEAR``.

        ``owner_id`` reassigns the item owner and can be changed on its own as a
        metadata-only update (no artifact required) — the Drata v2 evidence
        library PUT accepts ``ownerId`` independently at any time.
        """
        # Normalise name once: a whitespace-only value is "not provided" (so it
        # neither updates the name nor seeds a bogus artifact base filename).
        name = (name or "").strip()
        # Coerce defensively: the tool-test path bypasses jsonschema validation,
        # so a schema integer can arrive as a string (mirrors list/manage tools).
        owner_id = _coerce_int(owner_id) or 0
        # A whitespace-only source_url is effectively empty (``_send_evidence_artifact``
        # strips it), so don't let it trip the "one source" guard and block a
        # valid file_content / source_file_id upload.
        sources = [
            bool(file_content),
            bool(source_url.strip()),
            bool(source_file_id.strip()),
        ]
        if sum(sources) > 1:
            return "Error: provide only one artifact source — file_content, source_url, or source_file_id."
        control_id_list = _int_csv(control_ids)
        url = f"{base_url}/workspaces/{workspace_id}/evidence-library/{evidence_id}"

        if file_content or source_url or source_file_id.strip():
            # Forward a user-uploaded file as-is when referenced by id (resolve
            # before hitting Drata so a missing/expired file fails fast).
            prebuilt: tuple[bytes, str, str] | None = None
            if source_file_id.strip():
                resolved_upload = await _resolve_uploaded_evidence_file(file_context, source_file_id.strip(), file_name)
                if isinstance(resolved_upload, str):
                    return resolved_upload
                prebuilt = resolved_upload
            meta, meta_err = _evidence_artifact_meta(
                owner_id,
                filed_at,
                renewal_schedule_type,
                renewal_date,
                owner_required=False,
            )
            if meta_err:
                return meta_err
            base_fields = {}
            if name:
                base_fields["name"] = name
            if description:
                base_fields["description"] = description
            # Forward ``status`` on the artifact path too (same channel as the
            # no-artifact JSON path) so "only provided fields are changed" holds
            # when filing a new artifact version with a status change in one call.
            if status:
                base_fields["status"] = status
            data, err = await _send_evidence_artifact(
                method="put",
                url=url,
                action="upload evidence file",
                base_fields=base_fields,
                control_id_list=control_id_list,
                meta=meta,
                file_content=file_content,
                file_name=file_name,
                file_encoding=file_encoding,
                source_url=source_url,
                fallback_name=name or f"evidence-{evidence_id}",
                prebuilt=prebuilt,
            )
            if err:
                return err
            artifact = "file" if (file_content or prebuilt) else "URL artifact"
            return _evidence_write_summary(
                f"Evidence {evidence_id} updated with new {artifact}",
                data,
                workspace_id=workspace_id,
                fallback_id=evidence_id,
            )

        # No artifact — metadata-only update (JSON).
        payload: dict[str, Any] = {}
        if name:
            payload["name"] = name
        if description:
            payload["description"] = description
        if status:
            payload["status"] = status
        # The v2 evidence-library PUT accepts ``ownerId`` on its own ("can be
        # updated independently at any time"), so honour an owner reassignment
        # without forcing a new artifact version.
        if owner_id:
            payload["ownerId"] = owner_id
        if renewal_date:
            payload["renewalDate"] = renewal_date
        if control_id_list:
            payload["controlIds"] = control_id_list
        if not payload:
            return "Error: provide at least one field to update."
        async with egress_client(timeout=30) as client:
            _, err = await _common.safe_request(
                client,
                "put",
                url,
                _PROVIDER,
                "update evidence",
                headers=headers,
                json=payload,
            )
            if err:
                return err
            return f"Evidence {evidence_id} updated."

    async def delete_drata_evidence(workspace_id: int, evidence_id: int) -> str:
        """Delete an evidence library item. This is unrecoverable."""
        async with egress_client(timeout=30) as client:
            _, err = await _common.safe_request(
                client,
                "delete",
                f"{base_url}/workspaces/{workspace_id}/evidence-library/{evidence_id}",
                _PROVIDER,
                "delete evidence",
                headers=headers,
            )
            if err:
                return err
            return f"Evidence {evidence_id} deleted."

    async def download_drata_file(url: str = "", filename: str = "") -> str | ToolResult:
        """Download a Drata document's RAW BYTES into a reusable file_id.

        The Drata get_* document tools (get_drata_evidence_version,
        get_drata_vendor_document, get_drata_user_document, get_drata_risk_document,
        get_drata_external_evidence_download) only return a short-lived SIGNED
        download URL - not the bytes. Pass that URL here to fetch the bytes
        **server-side** (byte-for-byte, no URL-length cap, no truncation) and
        store them as a downloadable file. Returns a ``[[FILE:id]]`` card + a
        ``file_id`` you can forward to any tool that accepts a file - e.g.
        re-file a policy/evidence PDF as evidence elsewhere, embed it in a
        recap, or attach it to another system.

        Only Drata / AWS file hosts are allowed (SSRF guard). ``filename``
        overrides the saved name.
        """
        from app.llm.base import FileRef

        url = (url or "").strip()
        filename = (filename or "").strip()

        def _err(msg: str) -> ToolResult:
            return ToolResult(tool_call_id="", content=msg, is_error=True)

        if not url:
            return _err(
                "Error: url is required - the signed download URL from a Drata "
                "get_* document tool (e.g. get_drata_evidence_version, "
                "get_drata_vendor_document, get_drata_external_evidence_download)."
            )
        if not url.lower().startswith("https://"):
            return _err("Error: the download URL must be an HTTPS Drata signed URL.")
        if not _is_drata_file_host(url):
            host = urlparse(url).hostname or "?"
            return _err(
                f"Error: refusing to download from '{host}' - only Drata / AWS file hosts can be fetched byte-for-byte."
            )

        # Fetch with a plain client: the signed URL self-authenticates via its
        # query string, so the Drata bearer token must NOT be sent to S3.
        async with egress_client(follow_redirects=True, timeout=120.0) as client:
            resp, err = await _common.safe_request(client, "get", url, _PROVIDER, "download file bytes")
        if err:
            return _err(err)
        content = resp.content
        if not content:
            return _err("Error: the downloaded Drata file is empty.")
        if len(content) > _MAX_DRATA_DOWNLOAD_BYTES:
            return _err(
                f"Error: file is {round(len(content) / 1024 / 1024, 1)} MB, over "
                f"the {_MAX_DRATA_DOWNLOAD_BYTES // 1024 // 1024} MB download limit."
            )

        derived_name = (filename or _drata_filename_from_url(url) or "drata-download").strip()
        mime = (
            resp.headers.get("content-type", "").split(";", 1)[0].strip()
            or mimetypes.guess_type(derived_name)[0]
            or "application/octet-stream"
        )

        file_ref = None
        if persist_file_fn is not None:
            try:
                file_ref = await persist_file_fn(derived_name, content, mime)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Drata file persistence failed: %s", format_exception_message(exc))
                file_ref = None
        if file_ref is None:
            file_ref = FileRef.from_bytes(data=content, filename=derived_name, content_type=mime)
        facts = untrusted_file_facts(
            source="Drata",
            filename=derived_name,
            content_type=mime,
            links=[("Download URL (signed by Drata, short-lived)", url)],
        )
        return ToolResult(
            tool_call_id="",
            content=(
                f"Downloaded a {round(len(content) / 1024, 1)} KB file from Drata "
                f"as a reusable file. [[FILE:{file_ref.file_id}]]\n"
                "Forward it to another tool "
                f"with file_id={file_ref.file_id} (e.g. re-file as evidence via "
                "source_file_id), and include the [[FILE:...]] marker verbatim so "
                f"the user gets a download card.\n{facts}"
            ),
            files=[file_ref],
        )

    callable_map = {
        "list_drata_evidence": list_drata_evidence,
        "get_drata_evidence": get_drata_evidence,
        "get_drata_evidence_version": get_drata_evidence_version,
        "create_drata_evidence": create_drata_evidence,
        "update_drata_evidence": update_drata_evidence,
        "delete_drata_evidence": delete_drata_evidence,
        "download_drata_file": download_drata_file,
    }
    for _name, _fn in callable_map.items():
        executor.register(_name, _fn)

    return [
        ToolDefinition(
            name="download_drata_file",
            # ``filename`` only names the saved copy: a rename is the same download.
            cosmetic_arguments=("filename",),
            workspace_ingest=True,
            description=(
                "Download a Drata document's RAW BYTES into a reusable file_id "
                "(with a download card). Use this when you need to FORWARD a "
                "Drata-stored file (policy file, evidence artifact, vendor / user "
                "/ risk document, external evidence) to another tool - the Drata "
                "get_* document tools only return a short-lived signed URL, not "
                "bytes, and that URL is too long / expiry-bound for other tools "
                "to fetch. Pass the signed URL here (from get_drata_evidence_version, "
                "get_drata_vendor_document, get_drata_user_document, "
                "get_drata_risk_document, or get_drata_external_evidence_download); "
                "the bytes are fetched server-side and returned as a file_id you "
                "can hand to any tool that accepts a file (e.g. re-file as "
                "evidence via source_file_id)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": (
                            "The signed download URL returned by a Drata get_* document tool (Drata / AWS host)."
                        ),
                    },
                    "filename": {
                        "type": "string",
                        "description": "Optional override for the saved file name.",
                    },
                },
                "required": ["url"],
            },
            source="integration:drata",
            priority=1,
        ),
        ToolDefinition(
            name="list_drata_evidence",
            description=(
                "List the evidence items of a Drata workspace, one row each with status, "
                "renewal date and schedule, owners and artifact types. Every page is read: "
                "the header gives the exact count and says whether it is complete. "
                "Filter by name prefix, status (server-side) or renewal date window."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "integer", "default": 1},
                    "name": {"type": "string", "description": "Name prefix filter."},
                    "status": {
                        "type": "string",
                        "description": (
                            "Comma-separated evidence statuses: NEEDS_ARTIFACT, EXPIRED, "
                            "EXPIRING_SOON, READY, TEST_ERROR, TEST_FAILED, TEST_PASSED, "
                            "TEST_UNUSED, TEST_DISABLED, TEST_DELETED. NEEDS_RENEWAL means "
                            "EXPIRING_SOON and EXPIRED."
                        ),
                    },
                    "renewal_before": {
                        "type": "string",
                        "description": "Keep items whose renewal date is on or before this date (YYYY-MM-DD).",
                    },
                    "renewal_after": {
                        "type": "string",
                        "description": "Keep items whose renewal date is on or after this date (YYYY-MM-DD).",
                    },
                },
                "required": [],
            },
            source="integration:drata",
            priority=1,
        ),
        ToolDefinition(
            name="get_drata_evidence",
            description=(
                "Get details of a specific evidence library item, including its "
                "artifact versions (each with a version_id) and last-updated "
                "time. Use get_drata_evidence_version on a version_id to verify "
                "the stored file (name + download URL)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "integer"},
                    "evidence_id": {"type": "integer"},
                },
                "required": ["workspace_id", "evidence_id"],
            },
            source="integration:drata",
            priority=1,
        ),
        ToolDefinition(
            name="get_drata_evidence_version",
            description=(
                "Get one evidence artifact version by its version_id (from the "
                "Artifacts list in get_drata_evidence), returning the stored "
                "file name, version number, filed/created/updated timestamps, "
                "and a download URL. Use this to verify the exact artifact "
                "currently filed against an evidence item — e.g. confirm an "
                "evidence renewal uploaded the correct original file."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "integer"},
                    "evidence_id": {"type": "integer"},
                    "version_id": {
                        "type": "integer",
                        "description": (
                            "Artifact version ID (the version_id shown in get_drata_evidence's Artifacts list)."
                        ),
                    },
                },
                "required": ["workspace_id", "evidence_id", "version_id"],
            },
            source="integration:drata",
            priority=1,
        ),
        ToolDefinition(
            name="create_drata_evidence",
            description=(
                "Create an evidence library item and optionally attach a REAL "
                "artifact. To file a file the user uploaded to this conversation "
                "AS-IS (byte-for-byte), pass source_file_id with its file_id "
                "from the Available Files list — preferred when the user attached "
                "a document/PDF/image. To author an artifact instead, pass "
                "file_content (Markdown/HTML) with file_name ending in .pdf — it "
                "is rendered server-side to a real PDF (use .docx for Word, or "
                ".md/.txt/.html/.csv for verbatim text); set "
                "file_encoding='base64' to upload pre-built binary, or source_url "
                "to link an external artifact. Provide only ONE artifact source. "
                "When an artifact is attached, owner_id is required (filed_at "
                "defaults to today, renewal_schedule_type to ONE_YEAR). "
                "control_ids is a CSV of control IDs to link."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "integer"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "evidence_type": {"type": "string"},
                    "owner_id": {
                        "type": "integer",
                        "description": (
                            "Drata user ID of the evidence owner. Required when "
                            "attaching an artifact (find IDs via list_drata_users)."
                        ),
                    },
                    "control_ids": {
                        "type": "string",
                        "description": "CSV of control IDs to link.",
                    },
                    "renewal_date": {
                        "type": "string",
                        "description": ("YYYY-MM-DD. Required only when renewal_schedule_type is CUSTOM."),
                    },
                    "file_content": {
                        "type": "string",
                        "description": (
                            "Artifact content: Markdown/HTML (rendered to "
                            "PDF/DOCX) or base64 bytes (with file_encoding='base64')."
                        ),
                    },
                    "file_name": {
                        "type": "string",
                        "description": (
                            "Artifact file name incl. extension, e.g. soc2-recap.pdf. Defaults to NAME.pdf."
                        ),
                    },
                    "file_encoding": {
                        "type": "string",
                        "enum": ["text", "base64"],
                        "default": "text",
                    },
                    "source_url": {
                        "type": "string",
                        "description": ("Link an external artifact by URL instead of uploading a file."),
                    },
                    "source_file_id": {
                        "type": "string",
                        "description": (
                            "Attach a file the user already uploaded to this "
                            "conversation BYTE-FOR-BYTE, by its file_id (from the "
                            "Available Files list). Use this to file an existing "
                            "PDF/document/image as-is instead of re-authoring it. "
                            "Mutually exclusive with file_content / source_url."
                        ),
                    },
                    "filed_at": {
                        "type": "string",
                        "description": ("YYYY-MM-DD date the artifact was filed (defaults to today)."),
                    },
                    "renewal_schedule_type": {
                        "type": "string",
                        "enum": [
                            "ONE_MONTH",
                            "TWO_MONTHS",
                            "THREE_MONTHS",
                            "SIX_MONTHS",
                            "ONE_YEAR",
                            "CUSTOM",
                            "NONE",
                        ],
                    },
                },
                "required": ["workspace_id", "name"],
            },
            source="integration:drata",
            priority=1,
        ),
        ToolDefinition(
            name="update_drata_evidence",
            description=(
                "Update an evidence library item. Only provided fields change. "
                "Attach source_file_id (a file the user uploaded — forwarded "
                "byte-for-byte), file_content (rendered to PDF/DOCX or uploaded "
                "verbatim), or source_url to file a NEW artifact version "
                "(evidence renewal); provide only ONE source. Pass owner_id on "
                "its own to reassign the evidence owner (no artifact needed)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "integer"},
                    "evidence_id": {"type": "integer"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "status": {"type": "string"},
                    "renewal_date": {
                        "type": "string",
                        "description": ("YYYY-MM-DD. Required only when renewal_schedule_type is CUSTOM."),
                    },
                    "control_ids": {
                        "type": "string",
                        "description": "CSV of control IDs to link.",
                    },
                    "file_content": {
                        "type": "string",
                        "description": (
                            "New artifact content: Markdown/HTML (rendered to "
                            "PDF/DOCX) or base64 bytes (with file_encoding='base64')."
                        ),
                    },
                    "file_name": {
                        "type": "string",
                        "description": ("Artifact file name incl. extension, e.g. soc2-recap.pdf. Defaults to a .pdf."),
                    },
                    "file_encoding": {
                        "type": "string",
                        "enum": ["text", "base64"],
                        "default": "text",
                    },
                    "source_url": {
                        "type": "string",
                        "description": ("Link an external artifact by URL instead of uploading a file."),
                    },
                    "source_file_id": {
                        "type": "string",
                        "description": (
                            "File a NEW artifact version from a file the user "
                            "uploaded to this conversation BYTE-FOR-BYTE, by its "
                            "file_id (from the Available Files list). Use this to "
                            "renew evidence with an existing PDF/document/image "
                            "as-is. Mutually exclusive with file_content / "
                            "source_url."
                        ),
                    },
                    "owner_id": {
                        "type": "integer",
                        "description": (
                            "Drata user ID of the evidence owner. Can be set or "
                            "changed independently at any time (metadata-only "
                            "update, no artifact required). Also required when "
                            "attaching an artifact if the item has no owner yet."
                        ),
                    },
                    "filed_at": {
                        "type": "string",
                        "description": ("YYYY-MM-DD date the artifact was filed (defaults to today)."),
                    },
                    "renewal_schedule_type": {
                        "type": "string",
                        "enum": [
                            "ONE_MONTH",
                            "TWO_MONTHS",
                            "THREE_MONTHS",
                            "SIX_MONTHS",
                            "ONE_YEAR",
                            "CUSTOM",
                            "NONE",
                        ],
                    },
                },
                "required": ["workspace_id", "evidence_id"],
            },
            source="integration:drata",
            priority=1,
        ),
        ToolDefinition(
            name="delete_drata_evidence",
            description="Delete an evidence library item. This is unrecoverable.",
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "integer"},
                    "evidence_id": {"type": "integer"},
                },
                "required": ["workspace_id", "evidence_id"],
            },
            source="integration:drata",
            priority=1,
        ),
    ]
