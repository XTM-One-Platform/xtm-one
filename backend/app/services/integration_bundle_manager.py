# SPDX-License-Identifier: Apache-2.0

"""Integration bundle ZIP import/export helpers.

v1 bundles support metadata-driven tool declarations with optional declarative
HTTP execution specs. This module parses and validates archive structure +
metadata and can build export bundles with a checksum file.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from app.core.archive_reading import ArchiveTooLargeError, read_bounded

INTEGRATION_BUNDLE_FORMAT = "filigran-integration-v1"
INTEGRATION_BUNDLE_CHECKSUM_FILE = "checksum.sha256"
INTEGRATION_BUNDLE_METADATA_FILE = "metadata.json"

# Public: API upload endpoints bound their reads to this limit so an
# oversized upload is rejected before it is fully buffered in memory.
INTEGRATION_BUNDLE_MAX_BYTES = 10 * 1024 * 1024

_MAX_PACKAGE_BYTES = INTEGRATION_BUNDLE_MAX_BYTES
_MAX_FILE_BYTES = 5 * 1024 * 1024
_MAX_TOTAL_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
_MAX_FILE_COUNT = 512
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
# Official semver.org grammar: no leading zeros in major/minor/patch or in
# numeric pre-release identifiers, non-empty dot-separated identifiers, and
# pre-release + build metadata as independent segments (so
# "1.0.0-alpha+build1" validates while "01.2.3" and "1.0.0-alpha..1" do not).
_SEMVER_RE = re.compile(
    r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_AUTH_TYPES = {"api_key", "oauth", "bot_token", "none", "platform_managed"}
_SETTING_TYPES = {"string", "secret", "boolean", "select"}
# A bundle's OAuth URL: visible ASCII, no space; its scope list: printable ASCII.
_URL_TEXT_RE = re.compile(r"[\x21-\x7e]+")
_SCOPES_TEXT_RE = re.compile(r"[\x20-\x7e]+")


@dataclass(slots=True)
class IntegrationBundleSetting:
    key: str
    label: str
    type: str
    required: bool
    placeholder: str | None = None
    options: list[dict[str, str]] | None = None


@dataclass(slots=True)
class IntegrationBundleTestConnection:
    mode: str
    path: str | None = None
    method: str | None = None


@dataclass(slots=True)
class IntegrationBundleMetadata:
    format: str
    version: str
    name: str
    slug: str
    description: str
    author: str
    provider: str
    auth_type: str
    icon: str | None
    settings: list[IntegrationBundleSetting]
    exported_at: str
    supports_polling: bool
    supports_webhook: bool
    test_connection: IntegrationBundleTestConnection | None = None


@dataclass(slots=True)
class IntegrationBundleInstanceConfig:
    name: str
    auth_type: str
    description: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    api_key: str | None = None
    extra_config: dict[str, Any] | None = None
    authorize_url: str | None = None
    token_url: str | None = None
    scopes: str | None = None
    tags: list[str] | None = None
    is_company_managed: bool = False


@dataclass(slots=True)
class ParsedIntegrationBundle:
    metadata: IntegrationBundleMetadata
    metadata_raw: dict[str, Any]
    metadata_bytes: bytes
    checksum_sha256: str
    icon_bytes: bytes | None = None
    integration_instance: IntegrationBundleInstanceConfig | None = None


def is_valid_semver(version: str) -> bool:
    """True when ``version`` satisfies the official semver.org grammar."""
    return bool(_SEMVER_RE.match((version or "").strip()))


def normalize_custom_provider_slug(slug: str) -> str:
    """Normalize custom provider slugs with the required ``custom:`` namespace."""
    s = slug.strip().lower()
    if s.startswith("custom:"):
        return s
    return f"custom:{s}"


def _normalize_slug_value(value: str) -> str:
    """Normalize free-form names to a slug-safe value.

    Keeps only lowercase letters/digits separated by single hyphens.
    """
    normalized = re.sub(r"[^a-z0-9]+", "-", value.strip().lower())
    normalized = re.sub(r"-{2,}", "-", normalized)
    return normalized.strip("-")


def _safe_zip_names(raw: bytes) -> list[str]:
    if len(raw) > _MAX_PACKAGE_BYTES:
        raise ValueError(
            f"Bundle exceeds maximum size of {_MAX_PACKAGE_BYTES // (1024 * 1024)} MB."
        )
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            infos = [i for i in zf.infolist() if not i.filename.endswith("/")]
    except zipfile.BadZipFile as err:
        raise ValueError("Invalid ZIP file.") from err

    if not infos:
        raise ValueError("ZIP package is empty.")
    if len(infos) > _MAX_FILE_COUNT:
        raise ValueError(f"Bundle contains too many files (max {_MAX_FILE_COUNT}).")

    total_uncompressed = sum(info.file_size for info in infos)
    if total_uncompressed > _MAX_TOTAL_UNCOMPRESSED_BYTES:
        raise ValueError(
            "Bundle decompressed size exceeds maximum of "
            f"{_MAX_TOTAL_UNCOMPRESSED_BYTES // (1024 * 1024)} MB."
        )

    names = [info.filename for info in infos]
    for name in names:
        if "\\" in name:
            raise ValueError(f"Unsafe ZIP path detected: {name}")
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"Unsafe ZIP path detected: {name}")
    return names


def _read_zip_member(zf: zipfile.ZipFile, name: str, *, error: str) -> bytes:
    """A ZIP member's bytes, ``ArchiveTooLargeError(error)`` past ``_MAX_FILE_BYTES``.

    The declared size is an early refusal only: whoever built the archive sets
    it. ``read_bounded`` decompresses at most one byte past the smaller of the
    cap and the declared size, so an under-declared member is refused as
    unreadable having decompressed no more than it declared.
    """
    if zf.getinfo(name).file_size > _MAX_FILE_BYTES:
        raise ArchiveTooLargeError(error)
    try:
        return read_bounded(zf, name, _MAX_FILE_BYTES)
    except ArchiveTooLargeError:
        raise ArchiveTooLargeError(error) from None
    except zipfile.BadZipFile as err:
        raise ValueError("Invalid ZIP file.") from err


def _require_non_empty_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"metadata.json is missing required non-empty field: {key}")
    return value.strip()


def _parse_settings(payload: dict[str, Any]) -> list[IntegrationBundleSetting]:
    # Credential-less providers (e.g. pure OAuth or public APIs) legitimately
    # have zero settings, so an absent/empty list is valid.
    raw = payload.get("settings") or []
    if not isinstance(raw, list):
        raise ValueError("metadata.json field 'settings' must be an array.")

    out: list[IntegrationBundleSetting] = []
    keys_seen: set[str] = set()
    for idx, row in enumerate(raw):
        if not isinstance(row, dict):
            raise ValueError(f"settings[{idx}] must be an object.")

        key = _require_non_empty_str(row, "key")
        if key in keys_seen:
            raise ValueError(f"Duplicate settings key: {key}")
        keys_seen.add(key)

        label = _require_non_empty_str(row, "label")
        setting_type = _require_non_empty_str(row, "type").lower()
        if setting_type not in _SETTING_TYPES:
            raise ValueError(
                f"settings[{idx}].type must be one of {sorted(_SETTING_TYPES)}"
            )

        required = row.get("required")
        if not isinstance(required, bool):
            raise ValueError(f"settings[{idx}].required must be a boolean.")

        placeholder = row.get("placeholder")
        if placeholder is not None and not isinstance(placeholder, str):
            raise ValueError(f"settings[{idx}].placeholder must be a string.")

        options = row.get("options")
        parsed_options: list[dict[str, str]] | None = None
        if setting_type == "select":
            if not isinstance(options, list) or not options:
                raise ValueError(
                    f"settings[{idx}].options must be a non-empty array for select fields."
                )
            parsed_options = []
            for opt_idx, option in enumerate(options):
                if not isinstance(option, dict):
                    raise ValueError(
                        f"settings[{idx}].options[{opt_idx}] must be an object."
                    )
                opt_value = _require_non_empty_str(option, "value")
                opt_label = _require_non_empty_str(option, "label")
                parsed_options.append({"value": opt_value, "label": opt_label})

        out.append(
            IntegrationBundleSetting(
                key=key,
                label=label,
                type=setting_type,
                required=required,
                placeholder=placeholder,
                options=parsed_options,
            )
        )

    return out


def _parse_test_connection(payload: dict[str, Any]) -> IntegrationBundleTestConnection | None:
    raw = payload.get("test_connection")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("metadata.json field 'test_connection' must be an object.")

    mode = _require_non_empty_str(raw, "mode").lower()
    if mode not in {"http", "none"}:
        raise ValueError("test_connection.mode must be either 'http' or 'none'.")

    # The connectivity probe sends the credential the way this block says
    # (``api/v1/integrations.py``): the same rules as a tool's execution.auth.
    if raw.get("auth") is not None:
        from app.services.declarative_http_executor import validate_auth_spec

        auth_errors = validate_auth_spec(raw["auth"], prefix="test_connection.auth")
        if auth_errors:
            raise ValueError("; ".join(auth_errors))

    path = raw.get("path")
    method = raw.get("method")
    if mode == "http":
        if not isinstance(path, str) or not path.strip().startswith("/"):
            raise ValueError("test_connection.path must be an absolute path (starts with '/').")
        if not isinstance(method, str) or method.upper() not in {"GET", "POST", "HEAD"}:
            raise ValueError("test_connection.method must be one of GET, POST, HEAD.")
        return IntegrationBundleTestConnection(mode=mode, path=path.strip(), method=method.upper())

    return IntegrationBundleTestConnection(mode=mode)


def _optional_str_value(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"integration_instance.{key} must be a string.")
    stripped = value.strip()
    return stripped or None


def _parse_integration_instance(
    payload: dict[str, Any],
    *,
    default_auth_type: str,
) -> IntegrationBundleInstanceConfig | None:
    raw = payload.get("integration_instance")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("metadata.json field 'integration_instance' must be an object.")

    name = _require_non_empty_str(raw, "name")

    auth_type_value = raw.get("auth_type")
    if auth_type_value is None:
        auth_type = default_auth_type
    elif not isinstance(auth_type_value, str) or not auth_type_value.strip():
        raise ValueError("integration_instance.auth_type must be a non-empty string.")
    else:
        auth_type = auth_type_value.strip().lower()
    if auth_type not in _AUTH_TYPES:
        raise ValueError(
            f"integration_instance.auth_type must be one of {sorted(_AUTH_TYPES)}"
        )

    extra_config_value = raw.get("extra_config")
    if extra_config_value is not None and not isinstance(extra_config_value, dict):
        raise ValueError("integration_instance.extra_config must be an object.")

    tags_value = raw.get("tags")
    tags: list[str] | None = None
    if tags_value is not None:
        if not isinstance(tags_value, list):
            raise ValueError("integration_instance.tags must be an array of strings.")
        tags = []
        for idx, tag in enumerate(tags_value):
            if not isinstance(tag, str) or not tag.strip():
                raise ValueError(
                    f"integration_instance.tags[{idx}] must be a non-empty string."
                )
            tags.append(tag.strip())

    is_company_managed = raw.get("is_company_managed", False)
    if not isinstance(is_company_managed, bool):
        raise ValueError("integration_instance.is_company_managed must be a boolean.")

    return IntegrationBundleInstanceConfig(
        name=name,
        auth_type=auth_type,
        description=_optional_str_value(raw, "description"),
        client_id=_optional_str_value(raw, "client_id"),
        client_secret=_optional_str_value(raw, "client_secret"),
        api_key=_optional_str_value(raw, "api_key"),
        extra_config=extra_config_value,
        authorize_url=_optional_str_value(raw, "authorize_url"),
        token_url=_optional_str_value(raw, "token_url"),
        scopes=_optional_str_value(raw, "scopes"),
        tags=tags,
        is_company_managed=is_company_managed,
    )


def parse_metadata(metadata_raw: dict[str, Any]) -> IntegrationBundleMetadata:
    if not isinstance(metadata_raw, dict):
        raise ValueError("metadata.json must be a JSON object.")

    fmt = _require_non_empty_str(metadata_raw, "format")
    if fmt != INTEGRATION_BUNDLE_FORMAT:
        raise ValueError(
            f"Unsupported metadata format '{fmt}'. Expected '{INTEGRATION_BUNDLE_FORMAT}'."
        )

    version = _require_non_empty_str(metadata_raw, "version")
    if not _SEMVER_RE.match(version):
        raise ValueError("metadata.json field 'version' must be a semantic version.")

    name = _require_non_empty_str(metadata_raw, "name")
    slug_raw = _require_non_empty_str(metadata_raw, "slug")
    slug = _normalize_slug_value(slug_raw) or _normalize_slug_value(name)
    if not slug or not _SLUG_RE.match(slug):
        raise ValueError(
            "metadata.json field 'slug' could not be normalized to a valid slug."
        )

    description = _require_non_empty_str(metadata_raw, "description")
    author = _require_non_empty_str(metadata_raw, "author")
    provider_raw = _require_non_empty_str(metadata_raw, "provider")
    provider = _normalize_slug_value(provider_raw) or slug
    if not provider or not _SLUG_RE.match(provider):
        raise ValueError(
            "metadata.json field 'provider' could not be normalized to a valid slug."
        )
    auth_type = _require_non_empty_str(metadata_raw, "auth_type").lower()
    if auth_type not in _AUTH_TYPES:
        raise ValueError(
            f"metadata.json field 'auth_type' must be one of {sorted(_AUTH_TYPES)}"
        )

    icon = metadata_raw.get("icon")
    if icon is not None and (not isinstance(icon, str) or not icon.strip()):
        raise ValueError("metadata.json field 'icon' must be a non-empty string when provided.")

    exported_at = _require_non_empty_str(metadata_raw, "exported_at")
    settings = _parse_settings(metadata_raw)
    test_connection = _parse_test_connection(metadata_raw)

    supports_polling = metadata_raw.get("supports_polling", True)
    supports_webhook = metadata_raw.get("supports_webhook", False)
    if not isinstance(supports_polling, bool) or not isinstance(supports_webhook, bool):
        raise ValueError(
            "metadata.json fields 'supports_polling' and 'supports_webhook' must be booleans."
        )

    return IntegrationBundleMetadata(
        format=fmt,
        version=version,
        name=name,
        slug=slug,
        description=description,
        author=author,
        provider=provider,
        auth_type=auth_type,
        icon=icon.strip() if isinstance(icon, str) else None,
        settings=settings,
        exported_at=exported_at,
        supports_polling=supports_polling,
        supports_webhook=supports_webhook,
        test_connection=test_connection,
    )


def parse_integration_bundle_zip(raw: bytes) -> ParsedIntegrationBundle:
    names = _safe_zip_names(raw)
    if INTEGRATION_BUNDLE_METADATA_FILE not in names:
        raise ValueError("Bundle must include metadata.json at archive root.")
    if INTEGRATION_BUNDLE_CHECKSUM_FILE not in names:
        raise ValueError("Bundle must include checksum.sha256 at archive root.")

    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        metadata_bytes = _read_zip_member(
            zf,
            INTEGRATION_BUNDLE_METADATA_FILE,
            error="metadata.json exceeds maximum file size.",
        )
        checksum_bytes = _read_zip_member(
            zf,
            INTEGRATION_BUNDLE_CHECKSUM_FILE,
            error="checksum.sha256 exceeds maximum file size.",
        )

        try:
            metadata_raw = json.loads(metadata_bytes.decode("utf-8"))
        except Exception as err:
            raise ValueError("metadata.json must be valid UTF-8 JSON.") from err

        declared_checksum = checksum_bytes.decode("utf-8").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", declared_checksum):
            raise ValueError("checksum.sha256 must contain a 64-char lowercase SHA-256 hex.")
        actual_checksum = sha256(metadata_bytes).hexdigest()
        if declared_checksum != actual_checksum:
            raise ValueError("Checksum mismatch for metadata.json.")

        metadata = parse_metadata(metadata_raw)
        # Reject malformed tools[].execution blocks at import/validation time
        # so admins get an immediate 400 instead of a runtime tool failure.
        _validate_tools_execution_specs(metadata_raw)
        _validate_oauth_exchange(metadata_raw, auth_type=metadata.auth_type)
        _validate_oauth_endpoints(metadata_raw, auth_type=metadata.auth_type)
        integration_instance = _parse_integration_instance(
            metadata_raw,
            default_auth_type=metadata.auth_type,
        )
        icon_bytes = None
        if metadata.icon:
            icon_path = metadata.icon.strip()
            if icon_path not in names:
                raise ValueError(f"Icon file declared in metadata not found: {icon_path}")
            icon_bytes = _read_zip_member(
                zf,
                icon_path,
                error=f"Icon file exceeds maximum file size: {icon_path}",
            )

        return ParsedIntegrationBundle(
            metadata=metadata,
            metadata_raw=metadata_raw,
            metadata_bytes=metadata_bytes,
            checksum_sha256=declared_checksum,
            icon_bytes=icon_bytes,
            integration_instance=integration_instance,
        )


def _validate_tools_execution_specs(metadata: dict[str, Any]) -> None:
    """Validate optional ``execution`` specs on tool declarations."""
    from app.services.declarative_http_executor import validate_execution_spec

    raw_tools = metadata.get("tools")
    if not isinstance(raw_tools, list):
        raw_tools = metadata.get("available_tools")
    if not isinstance(raw_tools, list):
        return

    for idx, tool in enumerate(raw_tools):
        if not isinstance(tool, dict):
            continue
        execution = tool.get("execution")
        if execution is None:
            continue
        errors = validate_execution_spec(execution, tool_index=idx)
        if errors:
            raise ValueError("; ".join(errors))


def _validate_oauth_exchange(metadata: dict[str, Any], *, auth_type: str) -> None:
    """Validate the optional ``oauth_exchange`` block (service-redeemed OAuth codes)."""
    raw = metadata.get("oauth_exchange")
    if raw is None:
        return
    if auth_type != "oauth":
        raise ValueError("metadata.json field 'oauth_exchange' requires auth_type 'oauth'.")
    from app.integrations.service_token_exchange import validate_oauth_exchange

    errors = validate_oauth_exchange(raw)
    if errors:
        raise ValueError("; ".join(errors))


_OAUTH_KEYS = ("authorize_url", "token_url", "scopes")


def oauth_endpoint_errors(raw: Any) -> list[str]:
    """Errors of a bundle's ``oauth`` block (empty list = valid).

    ``authorize_url`` / ``token_url`` are where an integration created from the
    catalog sends its users and its code, ``scopes`` what it asks for. A URL
    spelling ``/common/`` takes the integration's ``tenant_id`` setting there
    (``integrations/oauth.py`` ``_resolve_tenant_urls``), as Entra ID does.
    """
    if not isinstance(raw, dict):
        return ["oauth must be an object."]
    errors: list[str] = []
    unknown = sorted(str(k) for k in raw if k not in _OAUTH_KEYS)
    if unknown:
        errors.append(f"oauth has unknown keys: {unknown}.")
    for key in ("authorize_url", "token_url"):
        value = raw.get(key)
        try:
            parts = urlsplit(value) if isinstance(value, str) and _URL_TEXT_RE.fullmatch(value) else None
            host = parts.hostname if parts is not None else None
        except ValueError:  # e.g. an unbalanced IPv6 bracket
            parts, host = None, None
        if parts is None or parts.scheme not in ("http", "https") or not host:
            errors.append(f"oauth.{key} must be an absolute http(s) URL.")
    scopes = raw.get("scopes")
    if scopes is not None and (not isinstance(scopes, str) or not _SCOPES_TEXT_RE.fullmatch(scopes)):
        errors.append("oauth.scopes must be a printable string.")
    return errors


def bundle_oauth_endpoints(metadata: Any) -> dict[str, Any] | None:
    """The valid ``oauth`` block of a stored bundle's metadata, else ``None``."""
    raw = metadata.get("oauth") if isinstance(metadata, dict) else None
    if raw is None or oauth_endpoint_errors(raw):
        return None
    return raw


def _validate_oauth_endpoints(metadata: dict[str, Any], *, auth_type: str) -> None:
    """Validate the optional ``oauth`` block (the OAuth endpoints of the catalog entry)."""
    raw = metadata.get("oauth")
    if raw is None:
        return
    if auth_type != "oauth":
        raise ValueError("metadata.json field 'oauth' requires auth_type 'oauth'.")
    errors = oauth_endpoint_errors(raw)
    if errors:
        raise ValueError("; ".join(errors))


def build_integration_bundle_zip(
    metadata: dict[str, Any],
    assets: dict[str, bytes] | None = None,
) -> bytes:
    """Build a v1 integration bundle ZIP.

    Checksum is computed over raw ``metadata.json`` bytes and stored in
    ``checksum.sha256``.
    """
    parsed = parse_metadata(metadata)
    _validate_tools_execution_specs(metadata)
    _validate_oauth_exchange(metadata, auth_type=parsed.auth_type)
    _validate_oauth_endpoints(metadata, auth_type=parsed.auth_type)
    metadata_json = json.dumps(
        {
            **metadata,
            "slug": parsed.slug,
            "provider": parsed.provider,
            "auth_type": parsed.auth_type,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    checksum = sha256(metadata_json).hexdigest()
    bundle = io.BytesIO()
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(INTEGRATION_BUNDLE_METADATA_FILE, metadata_json)
        zf.writestr(INTEGRATION_BUNDLE_CHECKSUM_FILE, checksum)
        for path, payload in sorted((assets or {}).items()):
            if path in {
                INTEGRATION_BUNDLE_METADATA_FILE,
                INTEGRATION_BUNDLE_CHECKSUM_FILE,
            }:
                raise ValueError(f"Asset path conflicts with reserved file: {path}")
            if not isinstance(payload, (bytes, bytearray)):
                raise ValueError(f"Asset payload must be bytes: {path}")
            if "\\" in path:
                raise ValueError(f"Unsafe asset path detected: {path}")
            p = PurePosixPath(path)
            if p.is_absolute() or ".." in p.parts:
                raise ValueError(f"Unsafe asset path detected: {path}")
            zf.writestr(path, bytes(payload))

    raw = bundle.getvalue()
    # Re-parse once to guarantee export artifacts always satisfy import rules.
    parse_integration_bundle_zip(raw)
    return raw
