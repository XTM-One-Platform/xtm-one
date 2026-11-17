# SPDX-License-Identifier: Apache-2.0

"""Filigran AI integration — fine-tuned AI models for cyber threat intelligence.

Provides three tools:

* **STIX** section (extraction + entity/relationship extraction)
  – ``extract_stix`` → ``/stix``
  – ``extract_entities_relations`` → ``/extract_entities_relations``
* **TTP** section (TTP extraction)
  – ``extract_ttp`` → ``/extract_ttp``

Each section has its own base URL:

* STIX endpoints use ``base_url_stix``.
* TTP endpoint uses ``base_url_ttp``.

Auth: licence PEM resolved at runtime from the platform (env var or DB).
Extra config:
  ``base_url_stix``     — STIX extraction API base URL
  ``base_url_ttp``      — TTP extraction API base URL
"""

from __future__ import annotations

import base64
import json
import logging
import uuid
from typing import Any, NamedTuple

import httpx

from app.core.egress import egress_client
from app.core.utils import XTM_ONE_STIX_EXTENSION_ID, format_exception_message
from app.integrations.http import format_http_error, format_network_error
from app.tools.base import CallableToolExecutor, FileContext, ToolDefinition, ToolResult

_PROVIDER = "Filigran AI"

logger = logging.getLogger(__name__)


# Maximum number of characters accepted for the ``text`` argument of
# ``extract_ttp`` before truncation kicks in.  Picked at 200 000 chars
# (≈ 200 KiB UTF-8, ≈ 50 K LLM tokens) — well above the size of a
# typical analyst-written threat report (which is what this argument is
# designed to carry) but small enough that a hallucinated megabyte-scale
# argument can't hold the worker's 120s HTTP timeout window or saturate
# outbound bandwidth.  The same cap is announced in the tool's
# ``input_schema`` so a well-behaved LLM reading the spec stays under it.
_TEXT_INPUT_MAX_CHARS = 200_000


# ---------------------------------------------------------------------------
# Licence PEM resolution
# ---------------------------------------------------------------------------

_PLATFORM_HEADER = "X-XTMOne-Certificate"

# Product → certificate header for connected-platform PEM forwarding.
#
# The Filigran import-doc / TTP AI webservices authenticate each request with a
# product-specific certificate header, and validate that header against the
# matching product's signing key (auth-kit ``MultiCertificateAuthMiddleware``).
# A connected platform's licence is issued for its OWN product (OpenCTI /
# OpenAEV), so forwarding it under ``X-XTMOne-Certificate`` is verified against
# the XTM One key and fails with ``401 Invalid certificate``. It must instead
# travel under the header matching the product that issued it.
_OPENCTI_HEADER = "X-OpenCTI-Certificate"
_OPENAEV_HEADER = "X-OpenAEV-Certificate"
_PRODUCT_CERT_HEADERS = {
    "opencti": _OPENCTI_HEADER,
    "openaev": _OPENAEV_HEADER,
    "obas": _OPENAEV_HEADER,
    "openbas": _OPENAEV_HEADER,
}


def _header_for_platform_pem(pem: str) -> str:
    """Return the certificate header matching a connected platform's licence.

    Reads the cert's product OID and maps it to the product-specific header
    (``X-OpenCTI-Certificate`` / ``X-OpenAEV-Certificate``). Falls back to the
    XTM One header when the product is unknown or unparseable — the safe
    default for an XTM-One-issued cert.
    """
    try:
        from app.enterprise.licensing import extract_license_product

        product = (extract_license_product(pem) or "").strip().lower()
    except Exception:
        logger.warning(
            "Filigran AI: could not read product from connected platform PEM; "
            "defaulting to XTM One header",
            exc_info=True,
        )
        return _PLATFORM_HEADER
    return _PRODUCT_CERT_HEADERS.get(product, _PLATFORM_HEADER)



class _PemResolution(NamedTuple):
    """Result of licence PEM resolution — carries the encoded PEM, the HTTP
    header, and a short source label used only for structured logging."""

    pem: str
    header: str
    source: str  # "env" | "db"


def _encode_pem(pem: str) -> str:
    """Return *pem* base64-encoded for use as an HTTP header value.

    Accepts both raw PEM (``-----BEGIN …``) and already-encoded strings.
    """
    if pem.startswith("LS0t") or not pem.startswith("-----"):
        return pem
    return base64.b64encode(pem.encode()).decode()


async def _resolve_pem() -> _PemResolution | None:
    """Resolve the licence PEM from the platform configuration.

    Resolution priority (first non-empty value wins):

    1. ``settings.enterprise_license`` — env-level platform key.
    2. ``platform_settings.enterprise_license`` (DB) — XTM One platform licence.
    3. A connected, active, EE-validated platform's stored licence PEM
       (``platform_registrations.enterprise_license_pem_enc``) — Enterprise
       Edition inherited from a connected EE OpenCTI/OpenAEV, XTM One holding
       no licence of its own. This mirrors how chat is edition-aware, so
       "import doc AI" is entitled to the same included allowance.

    Returns ``None`` when no licence is available from any source.
    """
    from app.core.config import settings

    env_pem = (settings.enterprise_license or "").strip()
    if env_pem:
        logger.debug("Filigran AI: using env-var PEM")
        return _PemResolution(_encode_pem(env_pem), _PLATFORM_HEADER, "env")

    try:
        from app.api.v1.platform_settings_helpers import get_platform_settings
        from app.core.database import async_session

        async with async_session() as db:
            ps = await get_platform_settings(db)
            if ps.enterprise_license and str(ps.enterprise_license).strip():
                db_pem = str(ps.enterprise_license).strip()
                logger.debug("Filigran AI: using DB platform PEM")
                return _PemResolution(_encode_pem(db_pem), _PLATFORM_HEADER, "db")

            platform_pem = await _resolve_connected_platform_pem(db)
            if platform_pem:
                header = _header_for_platform_pem(platform_pem)
                logger.debug(
                    "Filigran AI: using connected EE platform PEM (header=%s)",
                    header,
                )
                return _PemResolution(
                    _encode_pem(platform_pem), header, "platform"
                )
    except Exception:
        logger.warning(
            "Filigran AI: failed to load licence PEM from DB",
            exc_info=True,
        )

    return None


async def _resolve_connected_platform_pem(db: Any) -> str | None:
    """Return a decrypted EE licence PEM from a connected, healthy platform.

    Picks the most recently seen active + ``license_validated`` registration
    that carries a stored PEM. Corrupt/undecryptable entries are skipped so a
    single bad row never masks a valid one.
    """
    from sqlalchemy import select

    from app.core.security import decrypt_value
    from app.models.platform_registration import PlatformRegistration

    result = await db.execute(
        select(PlatformRegistration)
        .where(
            PlatformRegistration.status == "active",
            PlatformRegistration.license_validated.is_(True),
            PlatformRegistration.enterprise_license_pem_enc.isnot(None),
        )
        .order_by(PlatformRegistration.last_heartbeat_at.desc())
    )
    for reg in result.scalars().all():
        if not reg.is_healthy():
            continue
        try:
            pem = decrypt_value(reg.enterprise_license_pem_enc).strip()
        except Exception:
            logger.warning(
                "Filigran AI: could not decrypt stored PEM for platform %s",
                reg.platform_identifier,
                exc_info=True,
            )
            continue
        if pem:
            return pem
    return None


# ---------------------------------------------------------------------------
# Shared API caller
# ---------------------------------------------------------------------------


def _refusal(tool_name: str, log_reason: str, message: str) -> ToolResult:
    """Build the refuse-before-any-work result shared by the three no-op exits
    of ``_call_api`` (no input, no licence, no base URL).

    All three mean the same thing — nothing was sent upstream — so they must
    stay identical in shape: a logged warning plus a ``ToolResult`` flagged
    ``is_error=True`` so the pipeline can tell a refusal apart from a silent
    200 and skip charging the agentic quota for a call that never ran. Kept in
    one place so they cannot drift apart.
    """
    logger.warning("Filigran AI: %s refused — %s", tool_name, log_reason)
    return ToolResult(
        tool_call_id="",
        content=json.dumps({"error": message}),
        is_error=True,
    )


async def _call_api(
    base_url: str,
    endpoint: str,
    file_context: FileContext | None,
    tool_name: str,
    *,
    file_field: str = "file",
    text: str | None = None,
    allow_text_only: bool = False,
) -> str | ToolResult:
    """Send all files in *file_context* to a Filigran AI endpoint and return JSON results.

    Returns a JSON string on the normal path. Returns a ``ToolResult`` with
    ``is_error=True`` when the request is *refused before any work is done* —
    no input (no files and no usable text), no resolvable enterprise licence,
    or no configured base URL — so the refusal is surfaced as a real tool error
    (logged, and distinguishable by the pipeline) and the agentic quota is not
    charged for a call that never ran.
    """

    normalized_text: str | None = None
    if isinstance(text, str):
        stripped = text.strip()
        if stripped:
            normalized_text = stripped
            if len(normalized_text) > _TEXT_INPUT_MAX_CHARS:
                truncated = normalized_text[:_TEXT_INPUT_MAX_CHARS]
                last_space = truncated.rfind(" ")
                if last_space > 0:
                    truncated = truncated[:last_space]
                logger.warning(
                    "%s input text truncated from %d to <= %d characters",
                    tool_name,
                    len(normalized_text),
                    _TEXT_INPUT_MAX_CHARS,
                )
                normalized_text = truncated.rstrip()

    has_files = bool(file_context and not file_context.is_empty)
    if not has_files and not (allow_text_only and normalized_text):
        return _refusal(
            tool_name,
            "no files attached and no usable text input",
            "No files attached to the conversation. "
            "Please attach one or more documents.",
        )

    resolution = await _resolve_pem()
    if not resolution:
        # A genuine entitlement refusal — no PEM from env, the XTM One platform
        # settings, or any connected EE platform.
        return _refusal(
            tool_name,
            "no enterprise licence available "
            "(env / platform settings / connected EE platform)",
            "No enterprise licence configured. "
            "The Filigran AI tools require a valid Filigran licence PEM, "
            "or a connected Enterprise Edition OpenCTI/OpenAEV platform. "
            "Set it in the platform settings.",
        )

    if not base_url:
        # Misconfiguration, but still a refuse-before-work no-op.
        return _refusal(
            tool_name,
            "no API base URL configured",
            f"No API base URL configured for {tool_name}. "
            "Edit the integration and set the appropriate base URL.",
        )

    url = f"{base_url}{endpoint}"
    headers = {resolution.header: resolution.pem}

    async def _post_multipart(parts: list[tuple[str, tuple[Any, ...]]]) -> httpx.Response:
        async with egress_client(timeout=120) as client:
            return await client.post(url, files=parts, headers=headers)

    results: list[dict] = []
    if has_files:
        for ref in file_context.list_files():
            try:
                data = await file_context.read_bytes(ref.file_id)
                if data is None:
                    results.append(
                        {
                            "file_id": ref.file_id,
                            "filename": ref.filename,
                            "error": "Could not read file bytes",
                        }
                    )
                    continue

                files_payload: list[tuple[str, tuple[Any, ...]]] = [
                    (file_field, (ref.filename, data, ref.content_type))
                ]
                if normalized_text:
                    files_payload.append(("text", (None, normalized_text, "text/plain")))

                resp = await _post_multipart(files_payload)

                if not resp.is_success:
                    err = format_http_error(resp, _PROVIDER, tool_name)
                    logger.warning(
                        "%s upstream error for %s: status=%s detail=%s",
                        tool_name,
                        ref.filename,
                        resp.status_code,
                        err,
                    )
                    results.append(
                        {
                            "file_id": ref.file_id,
                            "filename": ref.filename,
                            "error": err,
                        }
                    )
                    continue

                try:
                    extraction = resp.json()
                except Exception:
                    extraction = resp.text

                results.append(
                    {
                        "file_id": ref.file_id,
                        "filename": ref.filename,
                        "content_type": ref.content_type,
                        "size": len(data),
                        "extraction": extraction,
                    }
                )
                logger.info(
                    "%s succeeded for %s (%d bytes)",
                    tool_name,
                    ref.filename,
                    len(data),
                )

            except httpx.HTTPError as exc:
                err = format_network_error(exc, _PROVIDER, tool_name)
                logger.warning(
                    "%s network error for %s: %s",
                    tool_name,
                    ref.filename,
                    err,
                )
                results.append(
                    {
                        "file_id": ref.file_id,
                        "filename": ref.filename,
                        "error": err,
                    }
                )
            except Exception as exc:
                logger.warning(
                    "%s failed for %s: %s",
                    tool_name,
                    ref.filename,
                    format_exception_message(exc),
                    exc_info=True,
                )
                results.append(
                    {
                        "file_id": ref.file_id,
                        "filename": ref.filename,
                        "error": str(exc),
                    }
                )
    else:
        try:
            resp = await _post_multipart([("text", (None, normalized_text, "text/plain"))])

            if not resp.is_success:
                err = format_http_error(resp, _PROVIDER, tool_name)
                logger.warning(
                    "%s upstream error for text-only payload: status=%s detail=%s",
                    tool_name,
                    resp.status_code,
                    err,
                )
                results.append(
                    {
                        "file_id": None,
                        "filename": None,
                        "error": err,
                    }
                )
            else:
                try:
                    extraction = resp.json()
                except Exception:
                    extraction = resp.text
                results.append(
                    {
                        "file_id": None,
                        "filename": None,
                        "content_type": "text/plain",
                        "size": len(normalized_text),
                        "extraction": extraction,
                    }
                )
                logger.info("%s succeeded for text-only payload", tool_name)

        except httpx.HTTPError as exc:
            err = format_network_error(exc, _PROVIDER, tool_name)
            logger.warning(
                "%s network error for text-only payload: %s",
                tool_name,
                err,
            )
            results.append(
                {
                    "file_id": None,
                    "filename": None,
                    "error": err,
                }
            )
        except Exception as exc:
            logger.warning(
                "%s failed for text-only payload: %s",
                tool_name,
                format_exception_message(exc),
                exc_info=True,
            )
            results.append(
                {
                    "file_id": None,
                    "filename": None,
                    "error": str(exc),
                }
            )

    return json.dumps({"files": results}, indent=2)


# ---------------------------------------------------------------------------
# STIX bundle merger
# ---------------------------------------------------------------------------


def _merge_stix_bundles(raw_results: str) -> str:
    """Merge per-file extraction results into a single STIX 2.1 bundle.

    Accepts the JSON string produced by ``_call_api`` (``{"files": [...]}``)
    and returns a single STIX bundle with all objects deduplicated by ``id``.

    Files that errored are reported in a top-level ``_errors`` array so the
    caller sees both the merged bundle **and** any per-file failures.
    """
    files, passthrough = _extract_files_or_passthrough(raw_results)
    if passthrough is not None:
        return passthrough

    ok_entries, errors = _collect_errors(files)

    seen_ids: set[str] = set()
    merged_objects: list[dict] = []

    for entry in ok_entries:
        extraction = entry.get("extraction")
        if not isinstance(extraction, dict):
            continue

        # The API may return the bundle directly or wrap it
        objects = extraction.get("objects", [])

        for obj in objects:
            obj_id = obj.get("id", "")
            if not obj_id:
                logger.warning(
                    "extract_stix dropped object without id from %s",
                    entry.get("filename") or "unknown source",
                )
                continue
            if obj_id not in seen_ids:
                seen_ids.add(obj_id)
                merged_objects.append(obj)

    bundle: dict[str, Any] = {
        "type": "bundle",
        "id": f"bundle--{uuid.uuid4()}",
        "objects": merged_objects,
    }

    if errors:
        bundle["extensions"] = {
            XTM_ONE_STIX_EXTENSION_ID: {
                "extension_type": "property-extension",
                "extraction_errors": errors,
            },
        }

    return json.dumps(bundle, indent=2)


def _extract_files_or_passthrough(raw_results: str) -> tuple[list[dict], str | None]:
    """Parse ``_call_api`` output; return (files, None) or ([], raw passthrough)."""
    try:
        data = json.loads(raw_results)
    except (json.JSONDecodeError, TypeError):
        return [], raw_results
    if "error" in data:
        return [], raw_results
    files = data.get("files", [])
    if not files:
        return [], raw_results
    return files, None


def _collect_errors(files: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split file entries into (successes, errors)."""
    ok, errors = [], []
    for entry in files:
        if "error" in entry:
            errors.append(
                {
                    "file_id": entry.get("file_id"),
                    "filename": entry.get("filename"),
                    "error": entry["error"],
                }
            )
        else:
            ok.append(entry)
    return ok, errors


def _merge_entity_results(raw_results: str) -> str:
    """Merge per-file entity/relationship extraction results.

    Combines ``relations`` arrays and ``metadata.span_based_entities`` arrays
    across files, deduplicating entities by ``id``.
    """
    files, passthrough = _extract_files_or_passthrough(raw_results)
    if passthrough is not None:
        return passthrough

    ok_entries, errors = _collect_errors(files)

    seen_entity_ids: set[str] = set()
    merged_entities: list[dict] = []
    seen_relation_ids: set[str] = set()
    merged_relations: list[dict] = []

    for entry in ok_entries:
        extraction = entry.get("extraction")
        if not isinstance(extraction, dict):
            continue

        for rel in extraction.get("relations", []):
            rid = rel.get("id", "")
            if rid and rid not in seen_relation_ids:
                seen_relation_ids.add(rid)
                merged_relations.append(rel)

        metadata = extraction.get("metadata", {})
        for ent in metadata.get("span_based_entities", []):
            eid = ent.get("id", "")
            if eid and eid not in seen_entity_ids:
                seen_entity_ids.add(eid)
                merged_entities.append(ent)

    result: dict[str, Any] = {
        "relations": merged_relations,
        "metadata": {"span_based_entities": merged_entities},
    }
    if errors:
        result["extensions"] = {
            XTM_ONE_STIX_EXTENSION_ID: {
                "extension_type": "property-extension",
                "extraction_errors": errors,
            },
        }

    return json.dumps(result, indent=2)


def _merge_ttp_results(raw_results: str) -> str:
    """Merge per-file TTP extraction results into a single response.

    The Filigran AI TTP API returns a dict where:
    - Keys are source identifiers (filenames or ``"form_text_input"`` for text)
    - Values are arrays of ``{"text": str, "predictions": {technique_id: score}}``

    This merger combines all sources across multiple files into a single dict,
    prefixing duplicate keys with the original filename to avoid collisions.
    """
    files, passthrough = _extract_files_or_passthrough(raw_results)
    if passthrough is not None:
        return passthrough

    ok_entries, errors = _collect_errors(files)

    merged: dict[str, list[dict]] = {}

    for entry in ok_entries:
        extraction = entry.get("extraction")
        if not isinstance(extraction, dict):
            continue

        source_filename = entry.get("filename") or "unknown"

        for key, predictions_list in extraction.items():
            if not isinstance(predictions_list, list):
                continue

            # Deduplicate key names across multiple file results
            target_key = key
            if target_key in merged and key != "form_text_input":
                target_key = f"{source_filename}::{key}"

            if target_key not in merged:
                merged[target_key] = []
            merged[target_key].extend(predictions_list)

    result: dict[str, Any] = merged if merged else {"ttps": []}
    if errors:
        result["extensions"] = {
            XTM_ONE_STIX_EXTENSION_ID: {
                "extension_type": "property-extension",
                "extraction_errors": errors,
            },
        }

    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Token parsing helper
# ---------------------------------------------------------------------------


def _parse_token(token: str) -> dict[str, str]:
    """Parse compound token into its components.

    Format: ``base_url_stix||base_url_ttp``
    """
    parts = token.split("||")
    return {
        "base_url_stix": (parts[0] if len(parts) > 0 else "").rstrip("/"),
        "base_url_ttp": (parts[1] if len(parts) > 1 else "").rstrip("/"),
    }


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str, executor: CallableToolExecutor, **_kwargs: Any
) -> list[ToolDefinition]:
    """Build Filigran AI tools.

    ``token`` compound format:
    ``base_url_stix||base_url_ttp``
    (PEM parts may be empty → falls back to platform licence).
    """

    cfg = _parse_token(token)

    # -- STIX extraction -------------------------------------------------------

    async def extract_stix(
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        """Extract full STIX bundles from attached files and merge into one bundle."""
        raw = await _call_api(
            cfg["base_url_stix"],
            "/stix",
            file_context,
            "extract_stix",
            file_field="file",
        )
        if isinstance(raw, ToolResult):
            return raw
        return _merge_stix_bundles(raw)

    # -- Entity/relationship extraction ----------------------------------------

    async def extract_entities_relations(
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        """Extract entities and relationships from attached files and merge."""
        raw = await _call_api(
            cfg["base_url_stix"],
            "/extract_entities_relations",
            file_context,
            "extract_entities_relations",
            file_field="file",
        )
        if isinstance(raw, ToolResult):
            return raw
        return _merge_entity_results(raw)

    # -- TTP extraction --------------------------------------------------------

    async def extract_ttp(
        text: str | None = None,
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        """Extract TTPs from provided text and/or attached files and merge."""
        raw = await _call_api(
            cfg["base_url_ttp"],
            "/extract_ttp",
            file_context,
            "extract_ttp",
            file_field="files",
            text=text,
            allow_text_only=True,
        )
        if isinstance(raw, ToolResult):
            return raw
        return _merge_ttp_results(raw)

    executor.register("extract_stix", extract_stix)
    executor.register("extract_entities_relations", extract_entities_relations)
    executor.register("extract_ttp", extract_ttp)

    return [
        ToolDefinition(
            name="extract_stix",
            description=(
                "Extract STIX 2.1 cyber threat intelligence from files attached "
                "to the conversation. Each file is sent to the Filigran AI STIX "
                "extraction API; all results are merged into a single deduplicated "
                "STIX bundle with combined entities and relationships."
            ),
            input_schema={"type": "object", "properties": {}},
            source="integration:filigran_ai",
            priority=1,
        ),
        ToolDefinition(
            name="extract_entities_relations",
            description=(
                "Extract entities and relationships from files attached to the "
                "conversation (non-STIX format). Results from all files are merged "
                "and deduplicated. Returns: "
                "'relations' — array of {id, type, from_id, to_id}; "
                "'metadata.span_based_entities' — array of {id, label, "
                "type (\"entity\" or \"observable\"), text}. "
                "Entities include threat actors, malware, indicators, "
                "observables, etc. Relationships link entities by ID."
            ),
            input_schema={"type": "object", "properties": {}},
            source="integration:filigran_ai",
            priority=1,
        ),
        ToolDefinition(
            name="extract_ttp",
            description=(
                "Extract TTPs (Tactics, Techniques and Procedures) from files "
                "attached to the conversation using Filigran AI. Results from all "
                "files are merged into a single response. Returns structured TTP "
                "data identifying attack patterns, techniques (MITRE ATT&CK), and "
                "procedures found in the documents."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": (
                            f"Text content to analyse for TTPs. Hard-capped "
                            f"at {_TEXT_INPUT_MAX_CHARS} characters — longer "
                            f"input is truncated server-side and a WARNING "
                            f"is logged.  For larger documents, attach them "
                            f"as files instead."
                        ),
                        "maxLength": _TEXT_INPUT_MAX_CHARS,
                    }
                },
            },
            source="integration:filigran_ai",
            priority=1,
        ),
    ]
