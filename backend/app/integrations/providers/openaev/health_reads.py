# SPDX-License-Identifier: Apache-2.0

"""Read-only OpenAEV module health reads for the Platform Health module checker.

#101 S15b-2c (``dev-docs/platform-health.md`` 13.5): every count is platform-wide
(the integration's tenant), none takes or returns a collector, injector or
executor id (13.4 scope). No argument selects a write; every request is a GET.

* ``get_openaev_platform_health``: ``GET /api/settings`` (platform version,
  licence state and expiry, XTM Composer, XTM Hub, SMTP and IMAP availability)
  and ``GET /api/tenants/{tenant}/tenant-settings`` (the OpenCTI and OpenCRQ
  links: enabled and URL set). OpenAEV exposes no last call result of the
  OpenCTI link, so the answer says so and the checker reports that part
  ``not_run``.
* ``list_openaev_expectations_due``: ``GET /api/injects/expectations`` (the
  manual, prevention and detection expectations with no result yet, READ on
  SIMULATION), counted by type, with the overdue ones (created more than their
  expiration time ago) and the oldest creation time. The route is read only:
  this tool never posts a verdict (``expectations.py`` keeps the write path).

A request the role cannot make answers its own line and the other still
answers; the checker reports the check that needs it as ``not_run``.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import _api

logger = logging.getLogger(__name__)

_SOURCE = "integration:openaev"

#: The licence flags of ``License`` (openaev-api ``io.openaev.ee.License``) read here.
_LICENCE_FLAGS: Final = (
    ("license_is_enterprise", "enterprise"),
    ("license_is_validated", "validated"),
    ("license_is_expired", "expired"),
)

#: Service fields of ``PlatformSettings`` (openaev-api ``rest.settings.response``).
_SERVICE_FIELDS: Final = (
    ("xtm_composer_version", "XTM Composer version"),
    ("xtm_composer_last_connectivity_check", "XTM Composer last connectivity check"),
    ("xtm_hub_enable", "XTM Hub enabled"),
    ("xtm_hub_reachable", "XTM Hub reachable"),
    ("smtp_service_available", "SMTP available"),
    ("imap_service_available", "IMAP available"),
)

#: The links of ``TenantSettingsOutput`` read here: (enabled flag, URL, label).
_LINKS: Final = (
    ("xtm_opencti_enable", "xtm_opencti_url", "OpenCTI link"),
    ("xtm_opencrq_enable", "xtm_opencrq_url", "OpenCRQ link"),
)


def _flag(value: Any) -> str:
    return "unknown" if value is None else str(value).lower()


def _licence_line(licence: Any) -> str:
    if not isinstance(licence, dict) or not licence:
        return "Licence: not reported by this OpenAEV."
    flags = ", ".join(f"{label} {_flag(licence.get(key))}" for key, label in _LICENCE_FLAGS)
    return f"Licence: type {licence.get('license_type') or '?'}, {flags}, expires {licence.get('license_expiration_date') or 'never'}"


def _link_line(settings: dict[str, Any], enabled_key: str, url_key: str, label: str) -> str:
    enabled = settings.get(enabled_key)
    url_set = bool(str(settings.get(url_key) or "").strip())
    return f"{label}: enabled {_flag(enabled)}, URL set {str(url_set).lower()}, last call result not exposed by OpenAEV"


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _is_overdue(row: dict[str, Any], now: datetime) -> bool:
    created = _parse_time(row.get("inject_expectation_created_at"))
    seconds = row.get("inject_expiration_time")
    if created is None or not isinstance(seconds, int | float) or seconds <= 0:
        return False
    return created + timedelta(seconds=seconds) < now


def _expectations_answer(rows: list[dict[str, Any]], now: datetime) -> str:
    if not rows:
        return "OpenAEV expectations due: none waiting for a result."
    by_type = Counter(str(r.get("inject_expectation_type") or "?").upper() for r in rows)
    types = ", ".join(f"{name} {count}" for name, count in sorted(by_type.items()))
    overdue = sum(1 for r in rows if _is_overdue(r, now))
    times = [t for t in (_parse_time(r.get("inject_expectation_created_at")) for r in rows) if t]
    oldest = min(times).isoformat() if times else "unknown"
    return (
        f"OpenAEV expectations due (no result yet): {len(rows)} ({types}); "
        f"{overdue} past their expiration time; oldest created {oldest}"
    )


def register(base_url: str, headers: dict[str, str], executor: CallableToolExecutor) -> list[ToolDefinition]:
    async def get_openaev_platform_health() -> str:
        lines: list[str] = []
        resp, err = await _api(base_url, headers, "get", "/api/settings", "read platform settings")
        if err:
            lines.append(f"Platform settings: {err}")
        else:
            settings = resp.json() if resp is not None else {}
            settings = settings if isinstance(settings, dict) else {}
            lines.append(f"Version: {settings.get('platform_version') or 'unknown'}")
            lines.append(_licence_line(settings.get("platform_license")))
            lines.extend(f"{label}: {_flag(settings.get(key))}" for key, label in _SERVICE_FIELDS)
        resp, err = await _api(base_url, headers, "get", "/api/tenant-settings", "read tenant settings")
        if err:
            lines.append(f"Links: {err}")
        else:
            tenant = resp.json() if resp is not None else {}
            tenant = tenant if isinstance(tenant, dict) else {}
            lines.extend(_link_line(tenant, enabled, url, label) for enabled, url, label in _LINKS)
        return "OpenAEV platform health:\n" + "\n".join(lines)

    async def list_openaev_expectations_due() -> str:
        resp, err = await _api(base_url, headers, "get", "/api/injects/expectations", "read expectations due")
        if err:
            return f"OpenAEV expectations due: {err}"
        body = resp.json() if resp is not None else []
        rows = [r for r in (body if isinstance(body, list) else []) if isinstance(r, dict)]
        return _expectations_answer(rows, datetime.now(UTC))

    executor.register("get_openaev_platform_health", get_openaev_platform_health)
    executor.register("list_openaev_expectations_due", list_openaev_expectations_due)

    return [
        ToolDefinition(
            name="get_openaev_platform_health",
            description="Read-only: OpenAEV version, licence, XTM Composer, XTM Hub, mail services and OpenCTI/OpenCRQ links.",
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
        ),
        ToolDefinition(
            name="list_openaev_expectations_due",
            description="Read-only: OpenAEV expectations with no result yet, counted by type, overdue and oldest.",
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
        ),
    ]
