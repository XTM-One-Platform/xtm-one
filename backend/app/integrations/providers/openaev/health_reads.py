# SPDX-License-Identifier: Apache-2.0

"""Read-only OpenAEV module health reads for the Platform Health module checker.

#101 S15b-2c (``dev-docs/platform-health.md`` 13.5): every count is platform-wide
(the integration's tenant), none takes or returns a collector, injector or
executor id (13.4 scope). No argument selects a write; every request is a GET
or a ``POST .../search`` (a read under SEARCH access control).

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

S15b-2c2 adds two reads. Both send ``POST .../search`` with ``size`` 1 and read
``totalElements`` only: a search is a read (SEARCH access control), no row
leaves the module.

* ``get_openaev_data_health``: ``POST /api/scenarios/search``,
  ``/api/exercises/search`` and ``/api/atomic-testings/search`` counted in total
  and by ``*_updated_at`` over the last 7 days vs the 3 weeks before (the
  ``*_created_at`` fields are not ``@Queryable`` in OpenAEV, so growth is read
  on updates and says so), simulations ``RUNNING`` with an ``exercise_end_date``
  over 24 h past, and simulations ``CANCELED`` updated in 24 h (OpenAEV has no
  failed simulation status). Completeness (scenarios with no inject, simulations
  with no target, assets in no group, injects with no expectation) and duplicate
  assets are not filterable through the search API: answered ``not exposed``.
* ``get_openaev_error_rate``: atomic testings whose ``inject_status`` is in
  ``ExecutionStatus.ERROR_STATUSES`` (ERROR, PARTIAL) per day over 7 days, by
  ``inject_updated_at``, with the 7-day daily mean. OpenAEV exposes no platform
  error log through its API, so that part is ``not exposed``.

A request the role cannot make answers its own line and the other still
answers; the checker reports the check that needs it as ``not_run``.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import _api, _search_body
from .read_scope import area_scope, count_text, with_scope

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


#: ``ExecutionStatus.ERROR_STATUSES`` that the filter pickers expose (the two
#: deprecated ``MAYBE_*`` values are ``@Hidden`` and never written any more).
_ERROR_STATUSES: Final = ("ERROR", "PARTIAL")

#: (search path, updated-at field, label) of the objects counted by the data read.
_VOLUME: Final = (
    ("/api/scenarios/search", "scenario_updated_at", "Scenarios"),
    ("/api/exercises/search", "exercise_updated_at", "Simulations"),
    ("/api/atomic-testings/search", "inject_updated_at", "Atomic testings"),
)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _window(field: str, start: datetime | None, end: datetime | None) -> list[dict[str, Any]]:
    clauses: list[dict[str, Any]] = []
    if start is not None:
        clauses.append({"key": field, "operator": "gte", "values": [_iso(start)]})
    if end is not None:
        clauses.append({"key": field, "operator": "lt", "values": [_iso(end)]})
    return clauses


def register(base_url: str, headers: dict[str, str], executor: CallableToolExecutor) -> list[ToolDefinition]:
    async def _count(path: str, filters: list[dict[str, Any]], action: str) -> tuple[int | None, str | None]:
        group = {"mode": "and", "filters": filters} if filters else None
        resp, err = await _api(base_url, headers, "post", path, action, json=_search_body(size=1, filter_group=group))
        if err:
            return None, err
        body = resp.json() if resp is not None else {}
        total = body.get("totalElements") if isinstance(body, dict) else None
        return (int(total), None) if isinstance(total, int) else (None, "no totalElements in the answer")

    async def get_openaev_data_health() -> str:
        # A grant-limited identity is answered an empty page, never a 403 (#1444).
        scope, cause = await area_scope(headers, "assessment")
        now = datetime.now(UTC)
        week, prior = now - timedelta(days=7), now - timedelta(days=28)
        lines: list[str] = []
        for path, field, label in _VOLUME:
            total, err = await _count(path, [], f"count {label.lower()}")
            if err:
                lines.append(f"{label}: {err}")
                continue
            recent, err_recent = await _count(path, _window(field, week, None), f"count {label.lower()} updated")
            before, err_before = await _count(path, _window(field, prior, week), f"count {label.lower()} updated")
            if err_recent or err_before or recent is None or before is None:
                lines.append(
                    f"{label}: total {count_text(total or 0, scope)}; updates per week: {err_recent or err_before}"
                )
                continue
            lines.append(
                f"{label}: total {count_text(total or 0, scope)}; updated last 7 days {count_text(recent, scope)}, "
                f"weekly mean of the 3 weeks before {before / 3:.1f}"
            )
        running, err = await _count(
            "/api/exercises/search",
            [
                {"key": "exercise_status", "operator": "eq", "values": ["RUNNING"]},
                *_window("exercise_end_date", None, now - timedelta(hours=24)),
            ],
            "count simulations running past their end",
        )
        lines.append(f"Simulations running over 24 h past their end: {err or count_text(running or 0, scope)}")
        canceled, err = await _count(
            "/api/exercises/search",
            [
                {"key": "exercise_status", "operator": "eq", "values": ["CANCELED"]},
                *_window("exercise_updated_at", now - timedelta(hours=24), None),
            ],
            "count simulations canceled",
        )
        lines.append(f"Simulations canceled in the last 24 h: {err or count_text(canceled or 0, scope)}")
        lines.append(
            "Completeness (scenarios with no inject, simulations with no target, assets in no group, "
            "injects with no expectation) and duplicate assets: not exposed by the OpenAEV search API"
        )
        lines.append("Creation times are not filterable in OpenAEV: growth is read on updates")
        return with_scope("OpenAEV data health:\n" + "\n".join(lines), scope, cause)

    async def get_openaev_error_rate() -> str:
        scope, cause = await area_scope(headers, "assessment")
        today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        status = {"key": "inject_status", "operator": "eq", "values": list(_ERROR_STATUSES)}
        days: list[str] = []
        counts: list[int] = []
        for back in range(6, -1, -1):
            start = today - timedelta(days=back)
            end = start + timedelta(days=1)
            count, err = await _count(
                "/api/atomic-testings/search",
                [status, *_window("inject_updated_at", start, end)],
                "count atomic testings in error",
            )
            if err or count is None:
                return f"OpenAEV error rate: {err}"
            counts.append(count)
            days.append(f"{start.date().isoformat()} {count_text(count, scope)}")
        mean = sum(counts) / len(counts)
        return with_scope(
            f"OpenAEV error rate (atomic testings in {'/'.join(_ERROR_STATUSES)}, by update day, UTC):\n"
            + "\n".join(days)
            + f"\n7-day daily mean {mean:.1f}; today {counts[-1]}"
            + "\nPlatform error log and failed internal jobs: not exposed by the OpenAEV API",
            scope,
            cause,
        )

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
    executor.register("get_openaev_data_health", get_openaev_data_health)
    executor.register("get_openaev_error_rate", get_openaev_error_rate)

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
        ToolDefinition(
            name="get_openaev_data_health",
            description="Read-only: OpenAEV scenario, simulation and atomic test counts, weekly activity, simulations stuck or canceled.",
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_openaev_error_rate",
            description="Read-only: OpenAEV atomic tests in error per day over 7 days, with the daily mean.",
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
        ),
    ]
