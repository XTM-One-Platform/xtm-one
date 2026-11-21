# SPDX-License-Identifier: Apache-2.0

"""Grafana integration — Dashboards, Data Sources, Alerts, Logs, and Annotations.

API docs: https://grafana.com/docs/grafana/latest/http_api/
Auth: API key or Service Account token (both use Authorization: Bearer <token>)
Base URL: configured per instance (e.g. https://grafana.company.com)
Token format (assembled by _get_token in integration_tools.py):
  base_url||api_key  (|| = double-pipe separator)

Tool categories:
    1. Organization & User — org info, current user
    2. Dashboards — list, read, search
    3. Folders — list, browse
    4. Data Sources — list, get details
    5. Queries — run queries against data sources
    6. Alerts — list, get rule details, create/update/delete rules, get alertmanager config
    7. Annotations — list annotations (notes, tags, system)
    8. Search — unified search across dashboards, folders, panels
    9. Panel images — render a panel as a PNG (needs the Grafana image renderer)
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from app.core.egress import egress_client
from app.core.redaction import redact_secrets
from app.integrations.file_delivery import deliver_file, fetch_file, link_lines
from app.integrations.http import (
    TRANSIENT_RETRY_STATUSES,
    format_entity_list,
    parse_compound_token,
    safe_request,
)
from app.tools.base import CallableToolExecutor, ToolDefinition, ToolResult

logger = logging.getLogger(__name__)
_PROVIDER = "Grafana"
_TIMEOUT_READ = 30
_TIMEOUT_QUERY = 60
_MAX_ROWS = 100
#: A panel render waits on the image renderer (Grafana's own render timeout
#: is 60 s by default).
_TIMEOUT_RENDER = 90
_MAX_IMAGE_BYTES = 20 * 1024 * 1024

_NO_RENDERER_ERROR = (
    "Error: this Grafana instance has no image renderer, so it cannot render a panel as "
    "an image (its /render endpoint only answers a placeholder picture). A Grafana "
    "administrator must set up the Grafana Image Renderer service ([rendering] server_url "
    "and renderer_token in grafana.ini): "
    "https://grafana.com/docs/grafana/latest/setup-grafana/image-rendering/ - until then, "
    "read the panel's data with list_dashboard_panel_queries and run_grafana_query."
)


def _flatten_panels(panel_list: list[dict]) -> list[dict]:
    """A dashboard's panels, the panels of its rows flattened in."""
    out: list[dict] = []
    for p in panel_list:
        if not isinstance(p, dict):
            continue
        if p.get("type") == "row":
            out.extend(_flatten_panels(p.get("panels", [])))
        else:
            out.append(p)
    return out


def _renderer_missing(settings: httpx.Response) -> bool:
    """``rendererAvailable: false`` in ``/api/frontend/settings``."""
    try:
        data = settings.json()
    except ValueError:
        return False
    return isinstance(data, dict) and data.get("rendererAvailable") is False


def _parse_template_variables(raw: str) -> list[tuple[str, str]]:
    """``(name, value)`` pairs from a JSON object or ``name=value,...`` text."""
    text = (raw or "").strip()
    if not text:
        return []
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise ValueError(f"variables is not valid JSON ({exc}).") from exc
        if not isinstance(data, dict):
            raise ValueError("variables must be a JSON object of name: value.")
        pairs: list[tuple[str, str]] = []
        for name, value in data.items():
            values = value if isinstance(value, list) else [value]
            pairs.extend((str(name), str(v)) for v in values)
        return pairs
    pairs = []
    for item in text.split(","):
        name, sep, value = item.partition("=")
        if not sep or not name.strip():
            raise ValueError(f"variables entry '{item.strip()}' is not name=value.")
        pairs.append((name.strip(), value.strip()))
    return pairs


def _pixels(value: Any, default: int) -> int:
    try:
        return max(100, min(int(value), 4000))
    except (TypeError, ValueError):
        return default


def _file_stem(text: Any) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(text)).strip("-.")[:60] or "grafana"


async def _grafana_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    provider: str,
    action: str,
    **kwargs: Any,
) -> tuple[httpx.Response | None, str | None]:
    """``safe_request`` with retry-with-backoff on transient 502/503/504.

    Grafana deployments commonly sit behind a reverse proxy / ingress whose
    gateway tier intermittently answers 502/503 for a few seconds — without a retry every such blip surfaced as a hard tool
    failure to the calling agent. All Grafana reads and queries route
    through this wrapper; mutations (silence/annotation creation) call
    ``safe_request`` directly because retrying a write could apply it twice
    when the first attempt succeeded behind a failing gateway.
    """
    kwargs.setdefault("retry_statuses", TRANSIENT_RETRY_STATUSES)
    return await safe_request(client, method, url, provider, action, **kwargs)


#: The longest upstream detail a Loki server error quotes.
_LOKI_ERROR_DETAIL_CHARS = 300
_TIMEOUT_WORDS_RE = re.compile(r"time[d ]?out|deadline|context canceled", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _loki_server_error(err: str, resp: httpx.Response | None, elapsed_ms: int) -> str:
    """A Loki query's server error with its upstream cause and elapsed time.

    A 502/503/504 from the datasource proxy (after the transient retries)
    otherwise reads "temporarily unavailable" whatever the cause: a heavy
    unanchored query that hits the gateway timeout needs a narrower query,
    an outage a later retry. Other failures pass through unchanged.
    """
    if resp is None or resp.status_code < 500:
        return err
    status = resp.status_code
    text = _HTML_TAG_RE.sub(" ", resp.text or "")
    detail = redact_secrets(" ".join(text.split()))[:_LOKI_ERROR_DETAIL_CHARS]
    lines = [err, f"HTTP {status} after {elapsed_ms} ms (retries included)."]
    if detail:
        lines.append(f"Upstream said: {detail}")
    if status == 504 or _TIMEOUT_WORDS_RE.search(detail):
        lines.append(
            "Verdict: gateway timeout. The query is too heavy for the proxy: "
            "narrow the time range, add stream labels to the selector, or drop "
            "a regex line filter, then query again."
        )
    else:
        lines.append(
            "Verdict: upstream unavailable (no timeout named). Retry later; "
            "if it persists, check_datasource_health shows whether Grafana "
            "reaches Loki."
        )
    return "\n".join(lines)


# Grafana's Alertmanager-compatible API is namespaced by datasource. The
# "grafana" segment targets the built-in Grafana-managed Alertmanager; the
# legacy path without it (/api/alertmanager/api/v2/...) 404s on modern
# Grafana instances.
_ALERTMANAGER_BASE = "/api/alertmanager/grafana/api/v2"


def _alertmanager_not_found_hint(err: str, resp: httpx.Response | None) -> str:
    """Append actionable guidance when the Grafana-managed Alertmanager 404s.

    A 404 on the Grafana-managed Alertmanager path is permanent for the
    instance (unified alerting disabled or an external Alertmanager is used),
    so agents should stop retrying and be told what to do instead.

    The check branches on the actual HTTP status code (the alertmanager
    callers pass ``expected_statuses=set()`` to ``safe_request`` so the
    failed response is returned alongside the error string) — substring
    matching on the error text would misfire on non-404 errors whose detail
    happens to contain "not found" (e.g. Grafana answers a silence POST
    carrying a stale ``id`` with HTTP 400 "silence not found"). Network
    errors have no response and always pass through unchanged.
    """
    if resp is not None and resp.status_code == 404:
        return (
            f"{err}\n"
            "The Grafana-managed Alertmanager API was not found on this "
            "Grafana instance. This usually means the instance does not use "
            "Grafana-managed alerting (unified alerting may be disabled, or "
            "alerts may be handled by an external Alertmanager datasource). "
            "Retrying this tool will not help. Use list_grafana_alerts to "
            "inspect alert rule definitions instead, or ask the user how "
            "alerting is configured on this Grafana instance."
        )
    return err


# Unified alerting serves the Alertmanager configuration per datasource:
# "grafana" is the built-in Grafana-managed Alertmanager, any other value the
# UID of an external Alertmanager datasource.
_ALERTMANAGER_DATASOURCE_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _alertmanager_config_path(datasource: str) -> str:
    return f"/api/alertmanager/{datasource}/config/api/v1/alerts"


def _alertmanager_config_error(
    err: str, resp: httpx.Response | None, path: str, datasource: str
) -> str:
    """Name the HTTP status and path so a 404 reads apart from a 403."""
    if resp is None:
        return err
    msg = f"{err}\nHTTP {resp.status_code} on GET {path}."
    if resp.status_code == 404:
        if datasource == "grafana":
            return _alertmanager_not_found_hint(msg, resp)
        msg += (
            f" No Alertmanager datasource with UID '{datasource}' was found; "
            "list_grafana_data_sources shows the UIDs, and 'grafana' targets "
            "the built-in Grafana-managed Alertmanager."
        )
    return msg


def _format_alertmanager_config(body: dict[str, Any], datasource: str) -> str:
    wrapped = body.get("alertmanager_config")
    config: dict[str, Any] = wrapped if isinstance(wrapped, dict) else body
    route_raw = config.get("route")
    route: dict[str, Any] = route_raw if isinstance(route_raw, dict) else {}
    routes = route.get("routes") or []
    receivers = [r for r in config.get("receivers") or [] if isinstance(r, dict)]
    lines = [
        f"Alertmanager Config ({datasource}):",
        f"Receivers ({len(receivers)}): {', '.join(str(r.get('name', '?')) for r in receivers)}",
        f"Root route: {route.get('receiver', 'N/A')}",
        f"Group by: {', '.join(str(g) for g in route.get('group_by') or []) or 'N/A'}",
        f"Top-level routes ({len(routes)}):",
    ]
    for sub in routes[:50]:
        if not isinstance(sub, dict):
            continue
        matchers = sub.get("object_matchers") or sub.get("matchers") or []
        rendered = ", ".join(
            " ".join(str(p) for p in m) if isinstance(m, list) else str(m)
            for m in matchers
        )
        lines.append(
            f"  - receiver={sub.get('receiver', '(inherited)')}"
            + (f" matchers=[{rendered}]" if rendered else "")
        )
    if len(routes) > 50:
        lines.append(f"  ... {len(routes) - 50} more")
    return "\n".join(lines)


# Unified alerting (Grafana 9+, the only engine from Grafana 11): the
# Prometheus-compatible rules API carries each rule's evaluation state and its
# alert instances. The ruler API (/api/ruler/grafana/api/v1/rules) answers an
# object keyed by folder with no state, and the legacy /api/alerts endpoint is
# gone once legacy alerting is removed: it is only a fallback on a 404.
_UNIFIED_RULES_PATH = "/api/prometheus/grafana/api/v1/rules"
_LEGACY_ALERTS_PATH = "/api/alerts"
# One Grafana-managed rule by uid (Grafana 9.1+): the uids the unified rules
# API lists. /api/v1/rule/grafana/{uid} is not a Grafana route (it 404s).
_PROVISIONING_RULES_PATH = "/api/v1/provisioning/alert-rules"
# A rule written through the API stays editable in the Grafana UI.
_PROVENANCE_HEADER = {"X-Disable-Provenance": "true"}
_RULE_CREATE_FIELDS = ("title", "folderUID", "ruleGroup", "condition", "data")


def _parse_rule_json(rule_json: str, *, require_create_fields: bool) -> tuple[dict[str, Any], str | None]:
    """The rule object of *rule_json*, or an error naming what is wrong."""
    try:
        rule = json.loads(rule_json or "")
    except ValueError as exc:
        return {}, f"rule_json is not valid JSON: {exc}"
    if not isinstance(rule, dict):
        return {}, "rule_json must be a JSON object."
    missing = [f for f in _RULE_CREATE_FIELDS if not rule.get(f)]
    if require_create_fields and missing:
        return {}, f"rule_json is missing: {', '.join(missing)}."
    return rule, None


# Caller-facing state filter -> (unified rule state, legacy alert state).
_ALERT_STATE_FILTERS: dict[str, tuple[str, str]] = {
    "firing": ("firing", "alerting"),
    "pending": ("pending", "pending"),
    "normal": ("inactive", "ok"),
}
_ALERT_STATE_ALIASES = {"alerting": "firing", "inactive": "normal", "ok": "normal"}


def _normalise_alert_state(state: str) -> str:
    """The canonical state filter (``firing``, ``pending``, ``normal`` or ``""``)."""
    key = (state or "").strip().lower()
    if not key or key == "all":
        return ""
    key = _ALERT_STATE_ALIASES.get(key, key)
    if key not in _ALERT_STATE_FILTERS:
        raise ValueError(
            f"Unknown alert state '{state}'. Use one of: firing, pending, normal "
            "(or leave it empty for every rule)."
        )
    return key


def _firing_instances(rule: dict[str, Any]) -> int:
    """Firing alert instances of a unified rule: ``totals`` when present, else counted."""
    totals = rule.get("totals")
    if isinstance(totals, dict):
        for key in ("alerting", "firing"):
            value = totals.get(key)
            if isinstance(value, int):
                return value
    alerts = rule.get("alerts") or []
    return sum(
        1
        for a in alerts
        if isinstance(a, dict)
        and str(a.get("state", "")).lower().startswith(("alerting", "firing"))
    )


def _parse_unified_rules(payload: Any, wanted: str) -> list[dict[str, Any]]:
    """Alert rules of a ``/api/prometheus/grafana/api/v1/rules`` answer."""
    data = payload.get("data", {}) if isinstance(payload, dict) else {}
    groups = data.get("groups", []) if isinstance(data, dict) else []
    target = _ALERT_STATE_FILTERS[wanted][0] if wanted else ""
    rules_out: list[dict[str, Any]] = []
    for group in groups or []:
        if not isinstance(group, dict):
            continue
        for rule in group.get("rules", []) or []:
            if not isinstance(rule, dict) or rule.get("type", "alerting") != "alerting":
                continue
            rule_state = str(rule.get("state", "")).lower()
            if target and rule_state != target:
                continue
            rules_out.append(
                {
                    "name": rule.get("name", "?"),
                    "uid": rule.get("uid") or "N/A",
                    "state": rule_state or "N/A",
                    "health": rule.get("health", "N/A"),
                    "folder": group.get("file") or "N/A",
                    "group": group.get("name", "N/A"),
                    "firing": _firing_instances(rule),
                }
            )
    return rules_out


def _format_provisioned_rule(rule: Any, uid: str) -> str:
    """A ``/api/v1/provisioning/alert-rules/{uid}`` answer, one field per line."""
    if not isinstance(rule, dict) or not rule:
        return f"Alert rule with UID '{uid}' not found."
    lines = [
        f"Alert: {rule.get('title', '?')} (UID: {rule.get('uid') or uid})",
        f"Folder UID: {rule.get('folderUID') or 'N/A'}",
        f"Rule Group: {rule.get('ruleGroup') or 'N/A'}",
        f"Condition: {rule.get('condition') or 'N/A'}",
        f"Pending period (for): {rule.get('for') or 'N/A'}",
        f"No data state: {rule.get('noDataState') or 'N/A'}",
        f"Error state: {rule.get('execErrState') or 'N/A'}",
    ]
    if rule.get("isPaused"):
        lines.append("Paused: yes")
    queries = [q for q in rule.get("data") or [] if isinstance(q, dict)]
    if queries:
        lines.append(f"Queries ({len(queries)}):")
        for q in queries:
            lines.append(f"  {q.get('refId', '?')}: datasource {q.get('datasourceUid') or 'N/A'}")
    for title, key in (("Labels", "labels"), ("Annotations", "annotations")):
        values = rule.get(key)
        if isinstance(values, dict) and values:
            lines.append(f"{title}:")
            lines.extend(f"  {k}: {v}" for k, v in values.items())
    return "\n".join(lines)


def _parse_legacy_alerts(payload: Any, wanted: str) -> list[dict[str, Any]]:
    """Alert rules of a legacy ``/api/alerts`` answer (pre-unified alerting)."""
    target = _ALERT_STATE_FILTERS[wanted][1] if wanted else ""
    rules_out: list[dict[str, Any]] = []
    for alert in payload if isinstance(payload, list) else []:
        if not isinstance(alert, dict):
            continue
        alert_state = str(alert.get("state", "")).lower()
        if target and alert_state != target:
            continue
        rules_out.append(
            {
                "name": alert.get("name", "?"),
                "uid": str(alert.get("id", "N/A")),
                "state": alert_state or "N/A",
                "health": "N/A",
                "folder": alert.get("dashboardUid") or "N/A",
                "group": "legacy",
                "firing": 1 if alert_state == "alerting" else 0,
            }
        )
    return rules_out


def _format_alert_rules(rules: list[dict[str, Any]], limit: int, wanted: str) -> str:
    """The alert rule listing, with the firing instance total in the header."""
    try:
        cap = max(1, int(limit))
    except (TypeError, ValueError):
        cap = 50
    listing = format_entity_list(
        rules[:cap],
        lambda r: (
            f"{r['name']} | UID: {r['uid']} | State: {r['state']} "
            f"| Health: {r['health']} | Firing instances: {r['firing']} "
            f"| Folder: {r['folder']} | Group: {r['group']}"
        ),
        noun=f"{wanted} alert rule" if wanted else "alert rule",
        total=len(rules),
        empty_msg=(
            f"No {wanted} alert rules found." if wanted else "No alert rules found."
        ),
    )
    if not rules:
        return listing
    firing = sum(r["firing"] for r in rules)
    footer = f"Firing alert instances across these rules: {firing}."
    if len(rules) > cap:
        footer += f" Showing {cap} of {len(rules)} rules; raise limit to see more."
    return f"{listing}\n{footer}"


def _parse_token(token: str) -> tuple[str, str]:
    return parse_compound_token(token, 2)


def _headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _format_table(data: dict, max_rows: int = _MAX_ROWS) -> str:
    rows = data.get("results", {})
    if not rows:
        return "Query returned no results."

    lines: list[str] = []
    for ref_id, result in rows.items():
        frame = result.get("frames", [])
        if not frame:
            lines.append(f"[{ref_id}] No data returned.")
            continue

        # Grafana 10+ frame format
        schema = frame[0].get("schema", {})
        fields = schema.get("fields", [])
        cols = [f.get("name", "?") for f in fields]

        data_rows: list[list[Any]] = []
        for f in frame:
            values = f.get("data", {}).get("values", [])
            if values:
                # Rotate columns → rows
                n_cols = len(cols)
                rotated: list[list[Any]] = [[] for _ in range(n_cols)]
                for col_vals in values:
                    for i, v in enumerate(col_vals):
                        if i < n_cols:
                            rotated[i].append(v)
                data_rows = rotated
                break

        if not data_rows:
            lines.append(f"[{ref_id}] No rows in result.")
            continue

        header = " | ".join(cols)
        sep = "-" * len(header)
        lines.append(f"[{ref_id}] Results ({len(data_rows)} rows):")
        lines.append(header)
        lines.append(sep)
        for row in data_rows[:max_rows]:
            lines.append(" | ".join(str(v) if v is not None else "" for v in row))
        if len(data_rows) > max_rows:
            lines.append(f"... ({len(data_rows) - max_rows} more rows)")
        lines.append("")

    return "\n".join(lines).strip()


def _parse_time_to_ns(time_str: str) -> int:
    """Convert a time string to epoch nanoseconds (Loki's native time format).

    Supports:
    - "now" → current time
    - "now-1h", "now-30m", "now-2d", "now-7d" → relative offsets
    - ISO 8601 strings like "2024-01-15T10:00:00Z"
    """
    now_ns = int(time.time() * 1_000_000_000)

    if time_str == "now":
        return now_ns

    # Relative: now-Xunit
    m = re.match(r"^now-(\d+)([smhd])$", time_str.strip())
    if m:
        amount = int(m.group(1))
        unit = m.group(2)
        seconds = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
        return now_ns - amount * seconds * 1_000_000_000

    # ISO 8601
    try:
        dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
        return int(dt.astimezone(UTC).timestamp() * 1_000_000_000)
    except ValueError:
        pass

    # Fallback: return now
    logger.warning("Could not parse time string %r; defaulting to now", time_str)
    return now_ns


async def _find_datasources_by_type(
    client: httpx.AsyncClient, base_url: str, headers: dict[str, str], ds_type: str
) -> list[dict[str, Any]]:
    """Fetch all Grafana datasources filtered to a specific type (e.g. 'loki', 'prometheus')."""
    resp, err = await _grafana_request(
        client,
        "get",
        f"{base_url}/api/datasources",
        _PROVIDER,
        "list data sources",
        headers=headers,
    )
    if err:
        return []
    return [ds for ds in resp.json() if ds.get("type", "") == ds_type]


# One Loki line can hold a whole multi-KB stack trace: a line is cut at
# _LOKI_LINE_CHARS and the answer at _LOKI_TOTAL_CHARS, the rest counted,
# so a few traceback lines never fill the tool-result cap (gap
# loki-query-result-uncapped: 7 lines answered 198,000 characters).
_LOKI_LINE_CHARS = 2_000
_LOKI_TOTAL_CHARS = 40_000


def _format_loki_logs(
    data: dict,
    max_lines: int = 200,
    line_chars: int = _LOKI_LINE_CHARS,
    total_chars: int = _LOKI_TOTAL_CHARS,
) -> str:
    """Format Loki query_range response into human-readable log output.

    Loki response structure:
      {"data": {"resultType": "streams", "result": [
          {"stream": {"job": "app", ...}, "values": [["<ns>", "<line>"], ...]}
      ]}}

    A metric query (``sum by (x) (...)``) answers ``resultType`` ``matrix``
    (``{"metric": {...}, "values": [...]}``) or ``vector``
    (``{"metric": {...}, "value": [ts, v]}``): each series is headed by its
    label set, so the values of a grouped query are never shown unlabelled.

    Each line is cut at ``line_chars`` and the answer stops at ``total_chars``;
    what is left out is counted, never silently dropped.
    """
    result_data = data.get("data", {})
    results = result_data.get("result", [])
    is_metric = result_data.get("resultType") in ("matrix", "vector")
    if is_metric:
        results = [
            {**s, "values": [s["value"]]} if "values" not in s and isinstance(s.get("value"), list) else s
            for s in results
        ]
    available = sum(len(s.get("values") or []) for s in results)

    if not results:
        status = data.get("status", "unknown")
        return f"No log results found (Loki status: {status})."

    lines: list[str] = []
    total_lines = 0
    size = 0
    cut_lines = 0

    for stream_entry in results:
        stream_labels = stream_entry.get("metric" if is_metric else "stream") or {}
        values = stream_entry.get("values", [])  # [[ns_timestamp, log_line], ...]

        if not values:
            continue

        # Format stream label selector for context
        label_str = ", ".join(f'{k}="{v}"' for k, v in stream_labels.items())
        lines.append(f"{'Series' if is_metric else 'Stream'}: {{{label_str}}}")
        lines.append("-" * min(60, len(label_str) + 10))

        for ts_ns, log_line in values:
            if total_lines >= max_lines:
                lines.append(f"... (output truncated at {max_lines} lines)")
                return "\n".join(lines)
            if size >= total_chars:
                lines.append(
                    f"... ({available - total_lines} more log line(s) left out past "
                    f"{total_chars:,} characters; narrow the LogQL with |= or "
                    "line_format, or lower limit)"
                )
                return "\n".join(lines)
            # Convert nanoseconds (seconds for a metric sample) → UTC timestamp
            try:
                ts_sec = float(ts_ns) if is_metric else int(ts_ns) / 1_000_000_000
                ts_fmt = datetime.fromtimestamp(ts_sec, tz=UTC).strftime(
                    "%Y-%m-%d %H:%M:%S UTC"
                )
            except (ValueError, TypeError):
                ts_fmt = str(ts_ns)
            text = str(log_line)
            if len(text) > line_chars:
                text = f"{text[:line_chars]} [... {len(text) - line_chars:,} chars cut]"
                cut_lines += 1
            entry = f"[{ts_fmt}] {text}"
            lines.append(entry)
            size += len(entry) + 1
            total_lines += 1

        lines.append("")

    total = (
        f"Total: {total_lines} sample(s) across {len(results)} series."
        if is_metric
        else f"Total: {total_lines} log line(s) across {len(results)} stream(s)."
    )
    if cut_lines:
        total += f" {cut_lines} line(s) cut at {line_chars:,} characters."
    lines.append(total)
    return "\n".join(lines).strip()


def _format_bytes(num_bytes: int) -> str:
    """Format a byte count into a human-readable string."""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024.0
    # Fallback return to avoid implicit None; should be unreachable with current logic.
    return f"{float(num_bytes):.1f} B"


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str, executor: CallableToolExecutor, **_kwargs: Any
) -> list[ToolDefinition]:
    base_url, api_key = _parse_token(token)
    if not base_url or not api_key:
        logger.warning(
            "Grafana token missing base URL or API key "
            "(expected 'base_url||api_key'). Cannot build tools."
        )
        return []
    base_url = base_url.rstrip("/")
    headers = _headers(api_key)
    persist_file_fn = _kwargs.get("persist_file_fn")

    # ── 1. Organization & User ───────────────────────────────────────────────

    async def get_grafana_org() -> str:
        """Get the current Grafana organization info."""
        async with egress_client(timeout=_TIMEOUT_READ) as client:
            resp, err = await _grafana_request(
                client,
                "get",
                f"{base_url}/api/org",
                _PROVIDER,
                "get org info",
                headers=headers,
            )
            if err:
                return err
            org = resp.json()
            return f"Organization: {org.get('name', '?')} " f"(ID: {org.get('id', '')})"

    async def get_grafana_user() -> str:
        """Get the current authenticated user info."""
        async with egress_client(timeout=_TIMEOUT_READ) as client:
            resp, err = await _grafana_request(
                client,
                "get",
                f"{base_url}/api/user",
                _PROVIDER,
                "get user info",
                headers=headers,
            )
            if err:
                return err
            user = resp.json()
            lines = [
                f"User: {user.get('login', '?')} | Email: {user.get('email', '?')}",
                f"Name: {user.get('name', 'N/A')}",
                f"ID: {user.get('id', '?')} | Is Admin: {user.get('isGrafanaAdmin', '?')}",
            ]
            return "\n".join(lines)

    # ── 2. Dashboards ────────────────────────────────────────────────────────

    async def list_grafana_dashboards(
        limit: int = 30,
        folder_ids: str = "",
    ) -> str:
        """List dashboards. Optionally filter by folder IDs (comma-separated)."""
        params: dict[str, Any] = {"limit": limit}
        if folder_ids:
            ids = ",".join(
                str(int(x.strip()))
                for x in folder_ids.split(",")
                if x.strip().isdigit()
            )
            if ids:
                params["folderIds"] = ids

        async with egress_client(timeout=_TIMEOUT_READ) as client:
            resp, err = await _grafana_request(
                client,
                "get",
                f"{base_url}/api/search",
                _PROVIDER,
                "list dashboards",
                headers=headers,
                params=params,
            )
            if err:
                return err
            items = resp.json()
            dashboards = [d for d in items if d.get("type") == "dash-db"]
            return format_entity_list(
                dashboards[:limit],
                lambda d: (
                    f"{d.get('title', 'Untitled')} | UID: {d.get('uid', 'N/A')} "
                    f"| ID: {d.get('id', 'N/A')} "
                    f"| Folder: {d.get('folderTitle', 'root')}"
                ),
                noun="dashboard",
                total=len(dashboards),
                empty_msg="No dashboards found.",
            )

    async def read_grafana_dashboard(uid: str) -> str:
        """Get a dashboard by its UID (unique identifier)."""
        async with egress_client(timeout=_TIMEOUT_READ) as client:
            resp, err = await _grafana_request(
                client,
                "get",
                f"{base_url}/api/dashboards/uid/{uid}",
                _PROVIDER,
                "get dashboard",
                headers=headers,
            )
            if err:
                return err
            dash = resp.json()
            meta = dash.get("dashboard", {})
            lines = [
                f"Dashboard: {meta.get('title', '?')} (UID: {uid})",
                f"ID: {meta.get('id', '?')} | Schema Version: {meta.get('schemaVersion', '?')}",
                f"Description: {meta.get('description') or 'N/A'}",
                f"Folder ID: {dash.get('meta', {}).get('folderId', 'N/A')}",
                f"Folder: {dash.get('meta', {}).get('folderTitle', 'N/A')}",
                f"Created: {meta.get('created', 'N/A')} | Updated: {meta.get('updated', 'N/A')}",
            ]
            panels = meta.get("panels", [])
            if panels:
                lines.append(f"Panels ({len(panels)}):")
                for p in panels[:20]:
                    ptype = p.get("type", "?")
                    title = p.get("title") or ptype
                    pid = p.get("id", "?")
                    lines.append(f"  • [{pid}] {title} (type: {ptype})")
                if len(panels) > 20:
                    lines.append(f"  ... ({len(panels) - 20} more panels)")
            return "\n".join(lines)

    async def list_dashboard_panel_queries(uid: str) -> str:
        """Extract all datasource queries from every panel in a dashboard.

        Fetches the dashboard by UID and returns each panel's name, type, and
        the query expressions targeting each datasource — without the noise of
        layout, overrides, or templating data.

        Use this to understand what a dashboard monitors and to reuse those
        queries with run_grafana_query or query_loki_logs.
        uid: dashboard UID (from list_grafana_dashboards or search_grafana).
        """
        async with egress_client(timeout=_TIMEOUT_READ) as client:
            resp, err = await _grafana_request(
                client,
                "get",
                f"{base_url}/api/dashboards/uid/{uid}",
                _PROVIDER,
                "get dashboard panels",
                headers=headers,
            )
            if err:
                return err
        dash = resp.json()
        meta = dash.get("dashboard", {})
        title = meta.get("title", uid)

        all_panels = _flatten_panels(meta.get("panels", []))
        if not all_panels:
            return f"Dashboard '{title}' has no panels."

        total_queries = sum(len(p.get("targets", [])) for p in all_panels)
        lines = [
            f"Dashboard: {title} ({len(all_panels)} panels, {total_queries} queries)"
        ]

        for panel in all_panels:
            ptype = panel.get("type", "?")
            ptitle = panel.get("title") or f"[untitled {ptype}]"
            targets = panel.get("targets", [])
            if not targets:
                continue
            lines.append(f'\nPanel "{ptitle}" (type: {ptype}):')
            for t in targets:
                ds = t.get("datasource", {})
                ds_type = ds.get("type", "?") if isinstance(ds, dict) else str(ds)
                expr = (
                    t.get("expr")
                    or t.get("query")
                    or t.get("rawSql")
                    or t.get("target")
                    or t.get("body")
                    or "[no expression found]"
                )
                lines.append(f"  • [{ds_type}] {expr}")

        return "\n".join(lines)

    async def search_grafana(
        query: str = "",
        type_filter: str = "",
        limit: int = 30,
    ) -> str:
        """Search across dashboards, folders, and panels.

        type_filter: 'dash-db' (dashboard), 'folder', 'dash-folder', 'panel'.
        Leave empty to search all types.
        """
        params: dict[str, Any] = {"limit": limit}
        if query:
            params["query"] = query
        if type_filter:
            params["type"] = type_filter

        async with egress_client(timeout=_TIMEOUT_READ) as client:
            resp, err = await _grafana_request(
                client,
                "get",
                f"{base_url}/api/search",
                _PROVIDER,
                "search",
                headers=headers,
                params=params,
            )
            if err:
                return err
            items = resp.json()
            if not items:
                return "No results found."
            return format_entity_list(
                items[:limit],
                lambda i: (
                    f"[{i.get('type', '?'):12}] {i.get('title', '?')} "
                    f"| UID: {i.get('uid', 'N/A')} "
                    f"| Folder: {i.get('folderTitle', 'N/A')}"
                ),
                noun="result",
                total=len(items),
            )

    # ── 3. Folders ───────────────────────────────────────────────────────────

    async def list_grafana_folders(limit: int = 50) -> str:
        """List all dashboard folders."""
        async with egress_client(timeout=_TIMEOUT_READ) as client:
            resp, err = await _grafana_request(
                client,
                "get",
                f"{base_url}/api/folders",
                _PROVIDER,
                "list folders",
                headers=headers,
                params={"limit": limit},
            )
            if err:
                return err
            folders = resp.json()
            return format_entity_list(
                folders[:limit],
                lambda f: (
                    f"{f.get('title', '?')} | UID: {f.get('uid', 'N/A')} "
                    f"| ID: {f.get('id', 'N/A')}"
                ),
                noun="folder",
                total=len(folders),
                empty_msg="No folders found.",
            )

    # ── 4. Data Sources ──────────────────────────────────────────────────────

    async def list_grafana_data_sources() -> str:
        """List all configured Grafana data sources."""
        async with egress_client(timeout=_TIMEOUT_READ) as client:
            resp, err = await _grafana_request(
                client,
                "get",
                f"{base_url}/api/datasources",
                _PROVIDER,
                "list data sources",
                headers=headers,
            )
            if err:
                return err
            ds_list = resp.json()
            return format_entity_list(
                ds_list,
                lambda ds: (
                    f"{ds.get('name', '?')} | ID: {ds.get('id', 'N/A')} "
                    f"| Type: {ds.get('type', 'N/A')} "
                    f"| URL: {ds.get('url', 'N/A')}"
                ),
                noun="data source",
                total=len(ds_list),
                empty_msg="No data sources configured.",
            )

    async def read_grafana_data_source(datasource_id: int) -> str:
        """Get details for a specific data source by its numeric ID."""
        async with egress_client(timeout=_TIMEOUT_READ) as client:
            resp, err = await _grafana_request(
                client,
                "get",
                f"{base_url}/api/datasources/{datasource_id}",
                _PROVIDER,
                "get data source",
                headers=headers,
            )
            if err:
                return err
            ds = resp.json()
            lines = [
                f"Data Source: {ds.get('name', '?')} (ID: {datasource_id})",
                f"Type: {ds.get('type', '?')} | Access: {ds.get('access', 'N/A')}",
                f"URL: {ds.get('url', 'N/A')}",
                f"Database: {ds.get('database', 'N/A')}",
                f"User: {ds.get('user', 'N/A')}",
                f"Default: {ds.get('isDefault', 'N/A')} | Basic Auth: {ds.get('basicAuth', 'N/A')}",
            ]
            json_data = ds.get("jsonData", {})
            if json_data:
                lines.append(f"JSON Data: {json_data}")
            return "\n".join(lines)

    async def check_datasource_health(datasource_uid: str) -> str:
        """Check whether a Grafana datasource is reachable and healthy.

        Uses Grafana's datasource health check API, which verifies the backend
        connection (e.g. can Grafana reach Loki, Prometheus, etc.).
        datasource_uid: string UID of the datasource (from list_grafana_data_sources
        or discover_loki_datasource / discover_prometheus_datasource).
        """
        async with egress_client(timeout=_TIMEOUT_READ) as client:
            resp, err = await _grafana_request(
                client,
                "get",
                f"{base_url}/api/datasources/uid/{datasource_uid}/health",
                _PROVIDER,
                f"health check datasource '{datasource_uid}'",
                headers=headers,
            )
            if err:
                return err
        data = resp.json()
        status = data.get("status", "UNKNOWN")
        message = data.get("message", "")
        ds_name = data.get("details", {}).get("name", datasource_uid)
        result = f"Datasource '{ds_name}' (uid: {datasource_uid}): {status}"
        if message:
            result += f"\nMessage: {message}"
        details = data.get("details", {})
        if details:
            extra = {k: v for k, v in details.items() if k != "name"}
            if extra:
                result += f"\nDetails: {extra}"
        return result

    # ── 5. Queries ───────────────────────────────────────────────────────────

    async def run_grafana_query(
        expr: str,
        datasource_id: int = 0,
        datasource_uid: str = "",
        ref_id: str = "A",
        from_time: str = "now-1h",
        to_time: str = "now",
    ) -> str:
        """Run a Prometheus/Flux/etc. metrics query against a Grafana data source.

        For Prometheus data sources, use PromQL (e.g. 'up{job="prometheus"}').
        For InfluxDB, use InfluxQL.

        For Loki log queries, use the dedicated Loki tools instead:
        discover_loki_datasource → get_loki_labels → query_loki_logs.

        You MUST provide either datasource_uid (preferred, string UID) or
        datasource_id (numeric ID > 0). Call list_grafana_data_sources first
        if you do not know the identifier.
        from_time/to_time: Grafana relative strings (e.g. 'now-1h', 'now-30m')
        or ISO 8601 timestamps.
        """
        if not datasource_uid and datasource_id <= 0:
            return (
                "Error: a datasource identifier is required to run a query. "
                "Call list_grafana_data_sources first to discover available data sources "
                "and their UIDs, then retry with datasource_uid set to the UID string "
                "(e.g. 'P8E80F9AEF21F6940') or datasource_id set to the numeric ID (> 0)."
            )

        query: dict[str, Any] = {"refId": ref_id, "expr": expr}
        if datasource_uid:
            query["datasource"] = {"uid": datasource_uid}
        else:
            # Grafana's query parser resolves the datasource from
            # query.datasource.uid or the legacy TOP-LEVEL integer
            # "datasourceId" field — it never reads datasource.id, so
            # {"datasource": {"id": N}} would still be rejected with the
            # same HTTP 400 "Query does not contain a valid data source
            # identifier" this guard exists to prevent.
            query["datasourceId"] = datasource_id

        body: dict[str, Any] = {
            "queries": [query],
            "from": from_time,
            "to": to_time,
        }

        async with egress_client(timeout=_TIMEOUT_QUERY) as client:
            resp, err = await _grafana_request(
                client,
                "post",
                f"{base_url}/api/ds/query",
                _PROVIDER,
                "run query",
                headers=headers,
                json=body,
            )
            if err:
                return err
            data = resp.json()
            return _format_table(data)

    # ── 5b. Loki (via datasource proxy) ─────────────────────────────────────
    # The Grafana datasource proxy forwards requests through Grafana's auth
    # layer directly to the underlying Loki instance, using its numeric ID.
    # Workflow: discover_loki_datasource → (optionally) get_loki_labels /
    #           get_loki_label_values / get_loki_series → query_loki_logs

    async def discover_loki_datasource() -> str:
        """Find all Loki datasources configured in Grafana.

        Always call this first before using any other Loki tool — it returns
        the numeric datasource ID required by query_loki_logs, get_loki_labels,
        get_loki_label_values, and get_loki_series.
        """
        async with egress_client(timeout=_TIMEOUT_READ) as client:
            loki_sources = await _find_datasources_by_type(
                client, base_url, headers, "loki"
            )
        if not loki_sources:
            return (
                "No Loki datasources found in Grafana. "
                "Verify that a Loki datasource is configured in Grafana → Data sources."
            )
        lines = [f"Found {len(loki_sources)} Loki datasource(s):"]
        for ds in loki_sources:
            lines.append(
                f"  • {ds.get('name', '?')} | ID: {ds.get('id', 'N/A')} "
                f"| UID: {ds.get('uid', 'N/A')} "
                f"| URL: {ds.get('url', 'N/A')} "
                f"| Default: {ds.get('isDefault', False)}"
            )
        lines.append(
            "\nUse the numeric ID with: query_loki_logs, get_loki_labels, "
            "get_loki_label_values, get_loki_series."
        )
        return "\n".join(lines)

    async def query_loki_logs(
        datasource_id: int,
        expr: str,
        start: str = "now-1h",
        end: str = "now",
        limit: int = 100,
        direction: str = "backward",
    ) -> str:
        """Query Loki logs via the Grafana datasource proxy using LogQL.

        Workflow:
          1. Call discover_loki_datasource to get the numeric datasource ID.
          2. (Optional) Call get_loki_labels / get_loki_label_values to explore
             available label selectors.
          3. Call this tool with a LogQL expression.

        datasource_id: numeric ID from discover_loki_datasource (required).
        expr: LogQL query, e.g. '{job="backend"} |= "error" | json'.
              Must include a stream selector in braces: {label="value"}.
        start/end: time range — relative ('now-1h', 'now-30m', 'now-2d')
                   or ISO 8601 ('2024-01-15T10:00:00Z'). Default: last 1 hour.
        limit: max number of log lines to return (default 100, max 1000).
        direction: 'backward' (newest first, default) or 'forward' (oldest first).

        The answer is bounded: a line past 2,000 characters is cut and the whole
        output stops at 40,000, the rest counted (_format_loki_logs).
        """
        start_ns = _parse_time_to_ns(start)
        end_ns = _parse_time_to_ns(end)
        params: dict[str, Any] = {
            "query": expr,
            "start": str(start_ns),
            "end": str(end_ns),
            "limit": min(limit, 1000),
            "direction": direction,
        }
        proxy_url = (
            f"{base_url}/api/datasources/proxy/{datasource_id}/loki/api/v1/query_range"
        )

        started = time.monotonic()
        async with egress_client(timeout=_TIMEOUT_QUERY) as client:
            # expected_statuses=set(): the failed response comes back so a
            # server error names its upstream cause (_loki_server_error).
            resp, err = await _grafana_request(
                client,
                "get",
                proxy_url,
                _PROVIDER,
                "query Loki logs",
                headers=headers,
                params=params,
                expected_statuses=set(),
            )
            if err:
                elapsed_ms = int((time.monotonic() - started) * 1000)
                return _loki_server_error(err, resp, elapsed_ms)
            return _format_loki_logs(resp.json())

    async def get_loki_labels(
        datasource_id: int,
        start: str = "now-6h",
        end: str = "now",
    ) -> str:
        """List all available Loki log stream label names via the datasource proxy.

        Call this after discover_loki_datasource to explore what labels are
        available before writing a LogQL query. Use the returned label names
        with get_loki_label_values to find valid values.

        datasource_id: numeric ID from discover_loki_datasource.
        start/end: time range to restrict label discovery (default last 6 hours).
        """
        start_ns = _parse_time_to_ns(start)
        end_ns = _parse_time_to_ns(end)
        proxy_url = (
            f"{base_url}/api/datasources/proxy/{datasource_id}/loki/api/v1/labels"
        )
        params: dict[str, Any] = {"start": str(start_ns), "end": str(end_ns)}

        async with egress_client(timeout=_TIMEOUT_READ) as client:
            resp, err = await _grafana_request(
                client,
                "get",
                proxy_url,
                _PROVIDER,
                "get Loki labels",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json()
        labels = data.get("data", [])
        if not labels:
            return "No labels found for this Loki datasource in the given time range."
        return (
            f"Available Loki labels ({len(labels)}):\n"
            + "\n".join(f"  • {label}" for label in sorted(labels))
            + "\n\nUse get_loki_label_values(datasource_id, label) to see values for each label."
        )

    async def get_loki_label_values(
        datasource_id: int,
        label: str,
        start: str = "now-6h",
        end: str = "now",
    ) -> str:
        """Get all values for a specific Loki label via the datasource proxy.

        Use this after get_loki_labels to discover what values a label can have,
        so you can build precise LogQL stream selectors like '{job="backend"}'.

        datasource_id: numeric ID from discover_loki_datasource.
        label: the label name to inspect (e.g. 'job', 'namespace', 'container').
        start/end: time range to restrict value discovery (default last 6 hours).
        """
        start_ns = _parse_time_to_ns(start)
        end_ns = _parse_time_to_ns(end)
        proxy_url = (
            f"{base_url}/api/datasources/proxy/{datasource_id}"
            f"/loki/api/v1/label/{label}/values"
        )
        params: dict[str, Any] = {"start": str(start_ns), "end": str(end_ns)}

        async with egress_client(timeout=_TIMEOUT_READ) as client:
            resp, err = await _grafana_request(
                client,
                "get",
                proxy_url,
                _PROVIDER,
                f"get Loki label values for '{label}'",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json()
        values = data.get("data", [])
        if not values:
            return f"No values found for label '{label}' in the given time range."
        return (
            f"Values for label '{label}' ({len(values)}):\n"
            + "\n".join(f"  • {v}" for v in sorted(values))
            + f'\n\nExample LogQL selector: {{{label}="{values[0]}"}}'
        )

    async def get_loki_series(
        datasource_id: int,
        match: str = "{}",
        start: str = "now-6h",
        end: str = "now",
    ) -> str:
        """Find all unique Loki log stream combinations matching a selector.

        Use this to discover which exact label combinations exist. More specific
        than get_loki_labels — returns actual stream fingerprints (all label=value
        pairs together).

        datasource_id: numeric ID from discover_loki_datasource.
        match: LogQL stream selector to filter results, e.g. '{namespace="prod"}'.
               Use '{}' (default) to list all streams.
        start/end: time range (default last 6 hours).
        """
        start_ns = _parse_time_to_ns(start)
        end_ns = _parse_time_to_ns(end)
        proxy_url = (
            f"{base_url}/api/datasources/proxy/{datasource_id}/loki/api/v1/series"
        )
        params: dict[str, Any] = {
            "match[]": match,
            "start": str(start_ns),
            "end": str(end_ns),
        }

        async with egress_client(timeout=_TIMEOUT_READ) as client:
            resp, err = await _grafana_request(
                client,
                "get",
                proxy_url,
                _PROVIDER,
                "get Loki series",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json()
        series = data.get("data", [])
        if not series:
            return f"No log streams found matching '{match}' in the given time range."
        lines = [f"Found {len(series)} log stream(s) matching '{match}':"]
        for i, stream in enumerate(series[:50], 1):
            label_str = ", ".join(f'{k}="{v}"' for k, v in stream.items())
            lines.append(f"  {i}. {{{label_str}}}")
        if len(series) > 50:
            lines.append(f"  ... ({len(series) - 50} more streams)")
        return "\n".join(lines)

    async def get_loki_log_stats(
        datasource_id: int,
        query: str,
        start: str = "now-1h",
        end: str = "now",
    ) -> str:
        """Get volume statistics for a Loki log stream selector (requires Loki 2.7+).

        Returns the number of streams, log entries, chunks, and total bytes that
        match a given stream selector — without fetching the actual log lines.
        Use this before query_loki_logs to gauge how much data exists and whether
        a query will return useful results.

        datasource_id: numeric ID from discover_loki_datasource.
        query: LogQL stream selector, e.g. '{job="backend"}'. Filter expressions
               like '|= "error"' are NOT supported here — use only label selectors.
        start/end: time range (default last 1 hour).
        """
        start_ns = _parse_time_to_ns(start)
        end_ns = _parse_time_to_ns(end)
        proxy_url = (
            f"{base_url}/api/datasources/proxy/{datasource_id}"
            "/loki/api/v1/index/stats"
        )
        params: dict[str, Any] = {
            "query": query,
            "start": str(start_ns),
            "end": str(end_ns),
        }

        async with egress_client(timeout=_TIMEOUT_READ) as client:
            resp, err = await _grafana_request(
                client,
                "get",
                proxy_url,
                _PROVIDER,
                "get Loki log stats",
                headers=headers,
                params=params,
            )
            if err:
                if "404" in err or "not found" in err.lower():
                    return (
                        "Log stats endpoint not available. "
                        "This feature requires Loki 2.7+. "
                        "Use query_loki_logs with a small limit to probe log volume instead."
                    )
                return err
        data = resp.json()
        streams = data.get("streams", 0)
        entries = data.get("entries", 0)
        chunks = data.get("chunks", 0)
        raw_bytes = data.get("bytes", 0)
        return (
            f"Log stats for {query} ({start} → {end}):\n"
            f"  Streams:  {streams:,}\n"
            f"  Entries:  {entries:,}\n"
            f"  Chunks:   {chunks:,}\n"
            f"  Size:     {_format_bytes(raw_bytes)}"
        )

    # ── 5b. Prometheus tools ─────────────────────────────────────────────────

    async def discover_prometheus_datasource() -> str:
        """Find all Prometheus datasources configured in Grafana.

        Returns the numeric ID, UID, name, and URL for each Prometheus
        datasource.  The numeric ID is required by get_prometheus_metric_names
        and get_prometheus_label_values.  Always call this first.
        """
        async with egress_client(timeout=_TIMEOUT_READ) as client:
            datasources = await _find_datasources_by_type(
                client, base_url, headers, "prometheus"
            )
        if not datasources:
            return "No Prometheus datasources found in this Grafana instance."
        lines = [f"Found {len(datasources)} Prometheus datasource(s):"]
        for ds in datasources:
            lines.append(
                f"\n  ID={ds['id']}  UID={ds['uid']}\n"
                f"  Name: {ds['name']}\n"
                f"  URL:  {ds['url']}"
            )
        return "\n".join(lines)

    async def get_prometheus_metric_names(
        datasource_id: int,
        match: str = "",
        start: str = "now-6h",
        end: str = "now",
    ) -> str:
        """List metric names available in a Prometheus datasource.

        datasource_id: numeric Prometheus datasource ID from
                       discover_prometheus_datasource.
        match: optional PromQL series selector to limit results
               (e.g. '{job="backend"}').  Leave empty to list all metrics.
        start/end: time range for the query (default last 6 hours).
        """
        start_ns = _parse_time_to_ns(start)
        end_ns = _parse_time_to_ns(end)
        # Convert ns → seconds for Prometheus HTTP API
        start_s = str(start_ns // 1_000_000_000)
        end_s = str(end_ns // 1_000_000_000)

        params: dict[str, Any] = {"start": start_s, "end": end_s}
        if match:
            params["match[]"] = match

        async with egress_client(timeout=_TIMEOUT_READ) as client:
            resp, err = await _grafana_request(
                client,
                "get",
                f"{base_url}/api/datasources/proxy/{datasource_id}/api/v1/label/__name__/values",
                _PROVIDER,
                "get Prometheus metric names",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json()

        if data.get("status") != "success":
            return f"Prometheus API error: {data.get('error', 'unknown error')}"

        names = data.get("data", [])
        if not names:
            return "No metrics found for the specified selector and time range."
        preview = names[:200]
        summary = f"Found {len(names)} metric name(s)"
        if len(names) > 200:
            summary += f" (showing first 200 of {len(names)})"
        return summary + ":\n" + "\n".join(f"  {n}" for n in preview)

    async def get_prometheus_label_values(
        datasource_id: int,
        label: str,
        match: str = "",
        start: str = "now-6h",
        end: str = "now",
    ) -> str:
        """Get all values for a specific Prometheus label.

        datasource_id: numeric Prometheus datasource ID from
                       discover_prometheus_datasource.
        label: the label name whose values you want
               (e.g. 'job', 'instance', 'namespace').
        match: optional PromQL series selector to narrow the search
               (e.g. '{__name__="up"}').
        start/end: time range for label value discovery (default last 6 hours).
        """
        start_ns = _parse_time_to_ns(start)
        end_ns = _parse_time_to_ns(end)
        start_s = str(start_ns // 1_000_000_000)
        end_s = str(end_ns // 1_000_000_000)

        params: dict[str, Any] = {"start": start_s, "end": end_s}
        if match:
            params["match[]"] = match

        async with egress_client(timeout=_TIMEOUT_READ) as client:
            resp, err = await _grafana_request(
                client,
                "get",
                f"{base_url}/api/datasources/proxy/{datasource_id}/api/v1/label/{label}/values",
                _PROVIDER,
                f"get Prometheus label values for '{label}'",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json()

        if data.get("status") != "success":
            return f"Prometheus API error: {data.get('error', 'unknown error')}"

        values = data.get("data", [])
        if not values:
            return f"No values found for label '{label}' in the specified time range."
        preview = values[:200]
        summary = f"Label '{label}' has {len(values)} value(s)"
        if len(values) > 200:
            summary += f" (showing first 200 of {len(values)})"
        return summary + ":\n" + "\n".join(f"  {v}" for v in preview)

    # ── 6. Alerts ───────────────────────────────────────────────────────────

    async def list_grafana_alerts(limit: int = 50, state: str = "") -> str:
        """List alert rules with their evaluation state.

        Reads the unified-alerting Prometheus-compatible rules API (Grafana
        10+), which carries each rule's state and its alert instances. Falls
        back to the legacy ``/api/alerts`` endpoint only when the unified
        endpoint answers 404 (legacy alerting instances).
        """
        try:
            wanted = _normalise_alert_state(state)
        except ValueError as exc:
            return str(exc)
        async with egress_client(timeout=_TIMEOUT_READ) as client:
            resp, err = await _grafana_request(
                client,
                "get",
                f"{base_url}{_UNIFIED_RULES_PATH}",
                _PROVIDER,
                "list alert rules",
                headers=headers,
                expected_statuses=set(),
            )
            if err and resp is not None and resp.status_code == 404:
                resp, err = await _grafana_request(
                    client,
                    "get",
                    f"{base_url}{_LEGACY_ALERTS_PATH}",
                    _PROVIDER,
                    "list legacy alert rules",
                    headers=headers,
                )
                if err:
                    return err
                rules_out = _parse_legacy_alerts(resp.json(), wanted)
            elif err:
                return err
            else:
                rules_out = _parse_unified_rules(resp.json(), wanted)
        return _format_alert_rules(rules_out, limit, wanted)

    async def read_grafana_alert(alert_uid: str) -> str:
        """Get full alert rule details by UID.

        Reads the provisioning API (Grafana 9.1+), which serves every
        Grafana-managed rule by the uid ``list_grafana_alerts`` lists.
        """
        uid = (alert_uid or "").strip()
        if not uid:
            return "alert_uid is required: pass a UID that list_grafana_alerts returns."
        async with egress_client(timeout=_TIMEOUT_READ) as client:
            resp, err = await _grafana_request(
                client,
                "get",
                f"{base_url}{_PROVISIONING_RULES_PATH}/{quote(uid, safe='')}",
                _PROVIDER,
                "get alert rule",
                headers=headers,
            )
            if err:
                return err
            return _format_provisioned_rule(resp.json(), uid)

    async def _write_alert_rule(method: str, uid: str, rule_json: str) -> str:
        rule, error = _parse_rule_json(rule_json, require_create_fields=method == "post")
        if error:
            return error
        url = f"{base_url}{_PROVISIONING_RULES_PATH}"
        if uid:
            url = f"{url}/{quote(uid, safe='')}"
            rule["uid"] = uid
        action = "create alert rule" if method == "post" else "update alert rule"
        async with egress_client(timeout=_TIMEOUT_READ) as client:
            # Mutation: no transient retry, a write could land twice.
            resp, err = await safe_request(
                client,
                method,
                url,
                _PROVIDER,
                action,
                headers={**headers, **_PROVENANCE_HEADER},
                json=rule,
            )
            if err or resp is None:
                return err or f"Grafana did not answer ({action})."
            written = resp.json() if resp.content else {}
            written_uid = (written.get("uid") if isinstance(written, dict) else None) or uid
            if not written_uid:
                return f"Grafana answered without a rule uid ({action})."
            # Read-back: the rule as Grafana serves it now.
            back, err = await _grafana_request(
                client,
                "get",
                f"{base_url}{_PROVISIONING_RULES_PATH}/{quote(written_uid, safe='')}",
                _PROVIDER,
                "read back alert rule",
                headers=headers,
            )
        verb = "Created" if method == "post" else "Updated"
        if err or back is None:
            return f"{verb} alert rule {written_uid}; the read-back failed: {err or 'no answer'}"
        return f"{verb} alert rule {written_uid}.\n\n{_format_provisioned_rule(back.json(), written_uid)}"

    async def create_grafana_alert_rule(rule_json: str) -> str:
        """Create a Grafana-managed alert rule through the provisioning API, then read it back."""
        return await _write_alert_rule("post", "", rule_json)

    async def update_grafana_alert_rule(alert_uid: str, rule_json: str) -> str:
        """Replace the alert rule *alert_uid* with *rule_json*, then read it back."""
        uid = (alert_uid or "").strip()
        if not uid:
            return "alert_uid is required: pass a UID that list_grafana_alerts returns."
        return await _write_alert_rule("put", uid, rule_json)

    async def delete_grafana_alert_rule(alert_uid: str) -> str:
        """Delete the alert rule *alert_uid*."""
        uid = (alert_uid or "").strip()
        if not uid:
            return "alert_uid is required: pass a UID that list_grafana_alerts returns."
        async with egress_client(timeout=_TIMEOUT_READ) as client:
            _resp, err = await safe_request(
                client,
                "delete",
                f"{base_url}{_PROVISIONING_RULES_PATH}/{quote(uid, safe='')}",
                _PROVIDER,
                "delete alert rule",
                headers={**headers, **_PROVENANCE_HEADER},
            )
        if err:
            return err
        return f"Deleted alert rule {uid}."

    async def get_grafana_alertmanager_config(datasource: str = "grafana") -> str:
        """Get the Alertmanager configuration (receivers, routes).

        ``datasource`` is ``grafana`` for the built-in Grafana-managed
        Alertmanager, or the UID of an external Alertmanager datasource.
        """
        name = (datasource or "").strip() or "grafana"
        if not _ALERTMANAGER_DATASOURCE_RE.fullmatch(name):
            return (
                f"Error: datasource '{name}' is not a valid Alertmanager "
                "datasource UID (letters, digits, '-' and '_' only); use "
                "'grafana' for the built-in Grafana-managed Alertmanager."
            )
        path = _alertmanager_config_path(name)
        async with egress_client(timeout=_TIMEOUT_READ) as client:
            resp, err = await _grafana_request(
                client,
                "get",
                f"{base_url}{path}",
                _PROVIDER,
                "get alertmanager config",
                headers=headers,
                # Receive the failed response so the error names the status.
                expected_statuses=set(),
            )
            if err:
                return _alertmanager_config_error(err, resp, path, name)
            body = resp.json()
        return _format_alertmanager_config(body if isinstance(body, dict) else {}, name)

    async def list_firing_alerts(
        filter_labels: str = "",
        include_silenced: bool = False,
        include_inhibited: bool = False,
        limit: int = 50,
    ) -> str:
        """List currently firing (active) alert instances from Alertmanager.

        This shows which alerts are ON FIRE RIGHT NOW — not the alert rule
        definitions (use list_grafana_alerts for those). An alert instance is
        created when a rule's condition is met.

        filter_labels: optional Alertmanager matcher, e.g. 'severity=critical'
                       or 'namespace=prod,job=api'. Multiple matchers are AND-ed.
        include_silenced: also show silenced alerts (default false).
        include_inhibited: also show inhibited alerts (default false).
        limit: max alert instances to return (default 50).
        """
        params: dict[str, Any] = {
            "silenced": str(include_silenced).lower(),
            "inhibited": str(include_inhibited).lower(),
            "active": "true",
        }
        if filter_labels:
            # Alertmanager expects repeated ?filter= params for each matcher
            params["filter"] = [
                m.strip() for m in filter_labels.split(",") if m.strip()
            ]

        async with egress_client(timeout=_TIMEOUT_READ) as client:
            resp, err = await _grafana_request(
                client,
                "get",
                f"{base_url}{_ALERTMANAGER_BASE}/alerts",
                _PROVIDER,
                "list firing alerts",
                headers=headers,
                params=params,
                # Opt into receiving the failed response (empty set keeps all
                # non-2xx statuses in the warning log) so the 404 hint can
                # branch on the real status code instead of the error text.
                expected_statuses=set(),
            )
            if err:
                return _alertmanager_not_found_hint(err, resp)
        alerts = resp.json()
        if not isinstance(alerts, list):
            alerts = alerts.get("data", [])
        if not alerts:
            return "No active alerts firing right now."

        lines = [f"{len(alerts[:limit])} active alert instance(s):"]
        for i, alert in enumerate(alerts[:limit], 1):
            status = alert.get("status", {})
            state = status.get("state", "unknown").upper()
            labels = alert.get("labels", {})
            annotations = alert.get("annotations", {})
            starts_at = alert.get("startsAt", "N/A")

            # Parse startsAt to readable form
            try:
                dt = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
                starts_at = dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
            except (ValueError, AttributeError) as exc:
                # If the timestamp cannot be parsed, keep the original value.
                logger.debug(
                    "Grafana: unable to parse startsAt value %r: %s",
                    starts_at,
                    exc,
                )

            alert_name = labels.get("alertname", "?")
            label_str = ", ".join(
                f"{k}={v}" for k, v in labels.items() if k != "alertname"
            )
            summary = annotations.get("summary") or annotations.get("message") or ""
            silenced_by = status.get("silencedBy", [])

            line = f"\n  {i}. [{state}] {alert_name}"
            if label_str:
                line += f"\n     Labels:  {label_str}"
            line += f"\n     Started: {starts_at}"
            if summary:
                line += f"\n     Summary: {summary}"
            if silenced_by:
                line += f"\n     Silenced by: {', '.join(silenced_by)}"
            lines.append(line)

        if len(alerts) > limit:
            lines.append(f"\n  ... ({len(alerts) - limit} more alerts not shown)")
        return "\n".join(lines)

    async def list_alert_silences(
        filter_labels: str = "",
        state: str = "active",
    ) -> str:
        """List Alertmanager silences (active, pending, or expired).

        A silence suppresses matching alert instances — they still fire but
        are hidden from notifications. Use this to see what is currently
        being silenced and by whom.

        filter_labels: optional comma-separated label=value matchers to filter
                       (e.g. 'namespace=prod'). Leave empty for all silences.
        state: 'active' (default), 'pending', 'expired', or 'all'.
        """
        async with egress_client(timeout=_TIMEOUT_READ) as client:
            resp, err = await _grafana_request(
                client,
                "get",
                f"{base_url}{_ALERTMANAGER_BASE}/silences",
                _PROVIDER,
                "list alert silences",
                headers=headers,
                expected_statuses=set(),
            )
            if err:
                return _alertmanager_not_found_hint(err, resp)
        silences = resp.json()
        if not isinstance(silences, list):
            silences = silences.get("data", [])

        # Filter by state
        if state != "all":
            silences = [
                s for s in silences if s.get("status", {}).get("state", "") == state
            ]

        # Filter by label matchers
        if filter_labels:
            wanted = dict(m.split("=", 1) for m in filter_labels.split(",") if "=" in m)

            def _matches(silence: dict) -> bool:
                matchers = {m["name"]: m["value"] for m in silence.get("matchers", [])}
                return all(matchers.get(k) == v for k, v in wanted.items())

            silences = [s for s in silences if _matches(s)]

        if not silences:
            return f"No {state} silences found."

        lines = [f"{len(silences)} {state} silence(s):"]
        for i, s in enumerate(silences, 1):
            sid = s.get("id", "?")
            s_state = s.get("status", {}).get("state", "?")
            matchers = s.get("matchers", [])
            matcher_str = ", ".join(
                f'{m["name"]}{"=~" if m.get("isRegex") else "="}{m["value"]}'
                for m in matchers
            )
            ends_at = s.get("endsAt", "N/A")
            created_by = s.get("createdBy", "N/A")
            comment = s.get("comment", "")

            try:
                ends_dt = datetime.fromisoformat(ends_at.replace("Z", "+00:00"))
                ends_at = ends_dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
            except (ValueError, AttributeError):
                logger.debug(
                    "Failed to parse endsAt timestamp %r for silence %r",
                    ends_at,
                    sid,
                    exc_info=True,
                )

            line = (
                f"\n  {i}. [{s_state.upper()}] ID: {sid}\n"
                f"     Matchers: {matcher_str}\n"
                f"     Expires:  {ends_at}\n"
                f"     By:       {created_by}"
            )
            if comment:
                line += f"\n     Comment:  {comment}"
            lines.append(line)
        return "\n".join(lines)

    async def create_alert_silence(
        matchers: str,
        comment: str,
        duration: str = "2h",
        created_by: str = "Copilot Agent",
    ) -> str:
        """Create an Alertmanager silence to suppress matching alert notifications.

        Silenced alerts still fire but are hidden from notification channels.
        Requires Editor role or alerting:silences:write permission.

        matchers: comma-separated label=value pairs that define which alerts to
                  silence, e.g. 'alertname=HighMemory,namespace=prod'.
                  All matchers are AND-ed (the alert must match ALL of them).
        comment: reason for the silence (required — shown in Grafana UI).
        duration: how long to silence — '30m', '2h', '1d', '7d' (default '2h').
        created_by: author label (default 'Copilot Agent').
        """
        # Parse duration into seconds
        dur_match = re.match(r"^(\d+)([smhd])$", duration.strip())
        if not dur_match:
            return f"Invalid duration format '{duration}'. Use e.g. '30m', '2h', '1d'."
        amount = int(dur_match.group(1))
        unit = dur_match.group(2)
        duration_secs = amount * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]

        now_ts = time.time()
        starts_at = datetime.fromtimestamp(now_ts, tz=UTC).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        )
        ends_at = datetime.fromtimestamp(now_ts + duration_secs, tz=UTC).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        )

        # Build matchers list
        matcher_list = []
        for m in matchers.split(","):
            m = m.strip()
            if "=~" in m:
                name, value = m.split("=~", 1)
                matcher_list.append(
                    {
                        "name": name.strip(),
                        "value": value.strip(),
                        "isRegex": True,
                        "isEqual": True,
                    }
                )
            elif "=" in m:
                name, value = m.split("=", 1)
                matcher_list.append(
                    {
                        "name": name.strip(),
                        "value": value.strip(),
                        "isRegex": False,
                        "isEqual": True,
                    }
                )
            else:
                return f"Invalid matcher '{m}'. Expected format: label=value or label=~regex."

        if not matcher_list:
            return "No valid matchers provided. Silence not created."

        body: dict[str, Any] = {
            "matchers": matcher_list,
            "startsAt": starts_at,
            "endsAt": ends_at,
            "createdBy": created_by,
            "comment": comment,
        }

        async with egress_client(timeout=_TIMEOUT_READ) as client:
            # Mutation: no transient retry — a duplicate silence could be
            # created if the first attempt succeeded behind a failing proxy.
            resp, err = await safe_request(
                client,
                "post",
                f"{base_url}{_ALERTMANAGER_BASE}/silences",
                _PROVIDER,
                "create alert silence",
                headers=headers,
                json=body,
                expected_statuses=set(),
            )
            if err:
                return _alertmanager_not_found_hint(err, resp)
        result = resp.json()
        silence_id = result.get("silenceID") or result.get("id") or "?"
        return (
            f"Silence created successfully.\n"
            f"  ID:       {silence_id}\n"
            f"  Matchers: {matchers}\n"
            f"  Duration: {duration} (until {ends_at})\n"
            f"  Comment:  {comment}"
        )

    # ── 7. Annotations ──────────────────────────────────────────────────────

    async def list_grafana_annotations(
        from_time: str = "now-6h",
        to_time: str = "now",
        limit: int = 50,
        alert_id: str = "",
    ) -> str:
        """List annotations (alert events, user notes, API posts).

        from_time/to_time: Grafana relative time strings (e.g. 'now-6h', 'now')
          or absolute ISO 8601 timestamps.
        alert_id: filter to a specific alert rule UID (leave empty for all).
        """
        params: dict[str, Any] = {
            "from": from_time,
            "to": to_time,
            "limit": min(limit, 500),
        }
        if alert_id:
            params["alertId"] = alert_id

        async with egress_client(timeout=_TIMEOUT_READ) as client:
            resp, err = await _grafana_request(
                client,
                "get",
                f"{base_url}/api/annotations",
                _PROVIDER,
                "list annotations",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json()
            if isinstance(data, list):
                annotations = data
            elif isinstance(data, dict):
                # Prefer a "result" field if present; otherwise, treat the dict
                # itself as a single annotation or fall back to an empty list.
                annotations = data.get("result")
                if annotations is None:
                    # Heuristic: a single annotation object vs. unexpected shape
                    annotations = (
                        [data]
                        if any(
                            k in data for k in ("id", "time", "text", "tags", "type")
                        )
                        else []
                    )
            else:
                annotations = []
            return format_entity_list(
                annotations[:limit],
                lambda a: (
                    f"[{a.get('id', '?')}] {a.get('type', 'annotation')} "
                    f"| Time: {a.get('time', '?')} "
                    f"| {a.get('text', 'N/A')[:80]} "
                    f"| Tags: {', '.join(a.get('tags', []) or [])}"
                ),
                noun="annotation",
                total=len(annotations),
                empty_msg="No annotations found in this time range.",
            )

    async def create_grafana_annotation(
        text: str,
        time: str = "",
        tags: str = "",
    ) -> str:
        """Create a user annotation. Requires 'annotations:write' permission.

        text: the annotation text.
        time: optional ISO 8601 or relative time string (default: now).
        tags: optional comma-separated tags.
        """
        body: dict[str, Any] = {"text": text}
        if time:
            body["time"] = time
        if tags:
            body["tags"] = [t.strip() for t in tags.split(",") if t.strip()]

        async with egress_client(timeout=_TIMEOUT_READ) as client:
            # Mutation: no transient retry — a duplicate annotation could be
            # created if the first attempt succeeded behind a failing proxy.
            resp, err = await safe_request(
                client,
                "post",
                f"{base_url}/api/annotations",
                _PROVIDER,
                "create annotation",
                headers=headers,
                json=body,
            )
            if err:
                return err
            result = resp.json()
            return f"Created annotation ID {result.get('id', '?')}."

    # ── 8. Health & Info ────────────────────────────────────────────────────

    async def get_grafana_health() -> str:
        """Check Grafana instance health and version."""
        async with egress_client(timeout=10) as client:
            resp, err = await _grafana_request(
                client,
                "get",
                f"{base_url}/api/health",
                _PROVIDER,
                "health check",
                headers=headers,
            )
            if err:
                return err
            health = resp.json()
            lines = [
                f"Grafana Health: {health.get('commit', 'N/A')}",
                f"Version: {health.get('version', 'N/A')}",
                f"Started: {health.get('created', 'N/A')}",
            ]
            return "\n".join(lines)

    # ── 9. Panel images ──────────────────────────────────────────────

    async def export_grafana_panel_image(
        dashboard_uid: str,
        panel_id: int,
        width: int = 1000,
        height: int = 500,
        from_time: str = "now-6h",
        to_time: str = "now",
        timezone: str = "",
        theme: str = "",
        variables: str = "",
    ) -> str | ToolResult:
        """Render one dashboard panel as a PNG and store it as a file of the run."""
        uid = str(dashboard_uid or "").strip()
        if not uid:
            return "Error: dashboard_uid is required."
        try:
            template_vars = _parse_template_variables(variables)
        except ValueError as exc:
            return f"Error: {exc}"
        async with egress_client(timeout=_TIMEOUT_READ) as client:
            # Without an image renderer Grafana answers /render with HTTP 200
            # and a placeholder picture: its absence is read beforehand. A
            # settings read that fails leaves the answer to the render.
            settings, _ = await _grafana_request(
                client,
                "get",
                f"{base_url}/api/frontend/settings",
                _PROVIDER,
                "read frontend settings",
                headers=headers,
            )
            if settings is not None and _renderer_missing(settings):
                return _NO_RENDERER_ERROR
            resp, err = await _grafana_request(
                client,
                "get",
                f"{base_url}/api/dashboards/uid/{quote(uid, safe='')}",
                _PROVIDER,
                "get dashboard",
                headers=headers,
            )
            if err:
                return err
        dash = resp.json()
        dashboard = dash.get("dashboard") or {}
        panels = _flatten_panels(dashboard.get("panels") or [])
        panel = next((p for p in panels if str(p.get("id")) == str(panel_id).strip()), None)
        if panel is None:
            listed = ", ".join(f"[{p.get('id')}] {p.get('title') or p.get('type')}" for p in panels[:30])
            return f"Error: dashboard {uid} has no panel {panel_id}. Panels: {listed or 'none'}."
        slug = (dash.get("meta") or {}).get("slug") or "dashboard"
        window = [("from", from_time or "now-6h"), ("to", to_time or "now")]
        extra = [(f"var-{name}", value) for name, value in template_vars]
        if timezone:
            extra.append(("tz", timezone))
        render_params = [
            ("panelId", str(panel_id)),
            ("width", str(_pixels(width, 1000))),
            ("height", str(_pixels(height, 500))),
            *window,
            *extra,
        ]
        if theme:
            render_params.append(("theme", theme.strip().lower()))
        dashboard_path = f"{quote(uid, safe='')}/{quote(slug, safe='')}"
        render_url = f"{base_url}/render/d-solo/{dashboard_path}?{urlencode(render_params)}"
        view_url = (
            f"{base_url}/d/{dashboard_path}?"
            f"{urlencode([('viewPanel', str(panel_id)), *window, *extra])}"
        )
        links = [("Web URL", view_url), ("Render URL", render_url)]
        async with egress_client(timeout=_TIMEOUT_RENDER) as client:
            fetched = await fetch_file(
                client,
                "get",
                render_url,
                provider=_PROVIDER,
                action=f"render panel {panel_id} of dashboard {uid}",
                max_bytes=_MAX_IMAGE_BYTES,
                headers={"Authorization": headers["Authorization"]},
            )
        if isinstance(fetched, str):
            return "\n".join([fetched, *link_lines(links)])
        content_type = fetched.content_type.split(";", 1)[0].strip().lower()
        if not content_type.startswith("image/"):
            return "\n".join(
                [
                    f"Error: Grafana answered the render with {content_type or 'no content type'}, "
                    "not an image (a login page or a proxy in front of Grafana?).",
                    *link_lines(links),
                ]
            )
        title = str(panel.get("title") or f"panel {panel_id}")
        return await deliver_file(
            data=fetched.data,
            filename=f"{_file_stem(dashboard.get('title') or uid)}-{_file_stem(title)}.png",
            content_type=content_type,
            source="Grafana",
            persist_file_fn=persist_file_fn,
            links=links,
            details=[
                f"Panel '{title}' of dashboard '{dashboard.get('title') or uid}', "
                f"{window[0][1]} to {window[1][1]}."
            ],
        )

    # ── Register all tools ───────────────────────────────────────────────────

    executor.register("get_grafana_org", get_grafana_org)
    executor.register("get_grafana_user", get_grafana_user)
    executor.register("list_grafana_dashboards", list_grafana_dashboards)
    executor.register("read_grafana_dashboard", read_grafana_dashboard)
    executor.register("list_dashboard_panel_queries", list_dashboard_panel_queries)
    executor.register("search_grafana", search_grafana)
    executor.register("list_grafana_folders", list_grafana_folders)
    executor.register("list_grafana_data_sources", list_grafana_data_sources)
    executor.register("read_grafana_data_source", read_grafana_data_source)
    executor.register("check_datasource_health", check_datasource_health)
    executor.register("run_grafana_query", run_grafana_query)
    executor.register("discover_loki_datasource", discover_loki_datasource)
    executor.register("query_loki_logs", query_loki_logs)
    executor.register("get_loki_labels", get_loki_labels)
    executor.register("get_loki_label_values", get_loki_label_values)
    executor.register("get_loki_series", get_loki_series)
    executor.register("get_loki_log_stats", get_loki_log_stats)
    executor.register("discover_prometheus_datasource", discover_prometheus_datasource)
    executor.register("get_prometheus_metric_names", get_prometheus_metric_names)
    executor.register("get_prometheus_label_values", get_prometheus_label_values)
    executor.register("list_grafana_alerts", list_grafana_alerts)
    executor.register("read_grafana_alert", read_grafana_alert)
    executor.register("create_grafana_alert_rule", create_grafana_alert_rule)
    executor.register("update_grafana_alert_rule", update_grafana_alert_rule)
    executor.register("delete_grafana_alert_rule", delete_grafana_alert_rule)
    executor.register(
        "get_grafana_alertmanager_config", get_grafana_alertmanager_config
    )
    executor.register("list_firing_alerts", list_firing_alerts)
    executor.register("list_alert_silences", list_alert_silences)
    executor.register("create_alert_silence", create_alert_silence)
    executor.register("list_grafana_annotations", list_grafana_annotations)
    executor.register("create_grafana_annotation", create_grafana_annotation)
    executor.register("get_grafana_health", get_grafana_health)
    executor.register("export_grafana_panel_image", export_grafana_panel_image)

    return [
        ToolDefinition(
            name="export_grafana_panel_image",
            description=(
                "Render one dashboard panel as a PNG image into the conversation: a download "
                "card, the panel's links and a file_id to forward to any tool that takes a file "
                "(a post, an email, a report); the image comes with the result. Needs the "
                "Grafana image renderer on the Grafana side. Use read_grafana_dashboard for "
                "panel ids. Include the returned [[FILE:...]] marker verbatim so the user gets "
                "a download card."
            ),
            input_schema={
                "type": "object",
                "required": ["dashboard_uid", "panel_id"],
                "properties": {
                    "dashboard_uid": {"type": "string", "description": "Dashboard UID."},
                    "panel_id": {"type": "integer", "description": "Panel ID (from read_grafana_dashboard)."},
                    "width": {"type": "integer", "default": 1000, "description": "Image width in pixels (100-4000)."},
                    "height": {"type": "integer", "default": 500, "description": "Image height in pixels (100-4000)."},
                    "from_time": {"type": "string", "default": "now-6h", "description": "Start of the time range (e.g. 'now-24h', epoch ms)."},
                    "to_time": {"type": "string", "default": "now", "description": "End of the time range."},
                    "timezone": {"type": "string", "default": "", "description": "IANA timezone of the axes (e.g. 'Europe/Paris'); default the renderer's."},
                    "theme": {"type": "string", "default": "", "description": "light or dark (default: the Grafana default)."},
                    "variables": {
                        "type": "string",
                        "default": "",
                        "description": "Dashboard variables, as a JSON object ('{\"env\": \"prod\"}') or 'name=value' pairs separated by commas.",
                    },
                },
            },
            source="integration:grafana",
        ),
        ToolDefinition(
            name="get_grafana_org",
            description="Get the current Grafana organization info (name, ID).",
            input_schema={"type": "object", "properties": {}},
            source="integration:grafana",
        ),
        ToolDefinition(
            name="get_grafana_user",
            description="Get the currently authenticated Grafana user info (login, email, name, admin status).",
            input_schema={"type": "object", "properties": {}},
            source="integration:grafana",
        ),
        ToolDefinition(
            name="list_grafana_dashboards",
            description="List dashboards. Optionally filter by folder IDs (comma-separated numeric IDs).",
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "default": 30,
                        "description": "Max dashboards to return (default 30).",
                    },
                    "folder_ids": {
                        "type": "string",
                        "description": "Comma-separated folder IDs to filter by (optional).",
                    },
                },
            },
            source="integration:grafana",
        ),
        ToolDefinition(
            name="read_grafana_dashboard",
            description="Get a dashboard's full details (panels, schema, metadata) by its UID. Use list_grafana_dashboards first to find the UID.",
            input_schema={
                "type": "object",
                "required": ["uid"],
                "properties": {
                    "uid": {
                        "type": "string",
                        "description": "Dashboard UID (from list_grafana_dashboards).",
                    }
                },
            },
            source="integration:grafana",
        ),
        ToolDefinition(
            name="list_dashboard_panel_queries",
            description=(
                "Extract and list all query expressions from every panel in a dashboard. "
                "Returns each panel's title, datasource type, and query expressions (PromQL, LogQL, SQL, etc.). "
                "Useful for auditing what data a dashboard queries, or understanding which metrics/logs it uses."
            ),
            input_schema={
                "type": "object",
                "required": ["uid"],
                "properties": {
                    "uid": {
                        "type": "string",
                        "description": "Dashboard UID (from list_grafana_dashboards).",
                    }
                },
            },
            source="integration:grafana",
        ),
        ToolDefinition(
            name="search_grafana",
            description="Search across dashboards, folders, and panels. Use type_filter='dash-db' for dashboards only.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query string (optional).",
                    },
                    "type_filter": {
                        "type": "string",
                        "description": "Filter by type: 'dash-db', 'folder', 'dash-folder', 'panel' (optional).",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 30,
                        "description": "Max results (default 30).",
                    },
                },
            },
            source="integration:grafana",
        ),
        ToolDefinition(
            name="list_grafana_folders",
            description="List all dashboard folders.",
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "default": 50,
                        "description": "Max folders to return (default 50).",
                    },
                },
            },
            source="integration:grafana",
        ),
        ToolDefinition(
            name="list_grafana_data_sources",
            description=(
                "List all configured Grafana data sources (Prometheus, Loki, InfluxDB, Elasticsearch, etc.) "
                "with their name, numeric ID, type, and URL. "
                "For Loki-specific discovery, prefer discover_loki_datasource which returns only "
                "Loki datasources with the ID ready for querying."
            ),
            input_schema={"type": "object", "properties": {}},
            source="integration:grafana",
        ),
        ToolDefinition(
            name="read_grafana_data_source",
            description="Get full details for a specific data source by its numeric ID (type, URL, database, auth settings).",
            input_schema={
                "type": "object",
                "required": ["datasource_id"],
                "properties": {
                    "datasource_id": {
                        "type": "integer",
                        "description": "Data source numeric ID.",
                    }
                },
            },
            source="integration:grafana",
        ),
        ToolDefinition(
            name="check_datasource_health",
            description=(
                "Run the built-in Grafana health check for a specific datasource by its UID (not numeric ID). "
                "Returns whether the datasource is reachable and any error details. "
                "Use list_grafana_data_sources first to find the UID."
            ),
            input_schema={
                "type": "object",
                "required": ["datasource_uid"],
                "properties": {
                    "datasource_uid": {
                        "type": "string",
                        "description": "Datasource UID string (e.g. 'P8E80F9AEF21F6940') from list_grafana_data_sources.",
                    }
                },
            },
            source="integration:grafana",
        ),
        ToolDefinition(
            name="run_grafana_query",
            description=(
                "Run a metrics query against a Grafana data source via the unified query endpoint. "
                "For Prometheus: use PromQL. For InfluxDB: use InfluxQL. "
                "For Loki log queries, use the dedicated Loki tools instead "
                "(discover_loki_datasource → get_loki_labels → query_loki_logs). "
                "You MUST provide datasource_uid (preferred) or datasource_id (> 0). "
                "Call list_grafana_data_sources first if you do not know the datasource identifier."
            ),
            input_schema={
                "type": "object",
                "required": ["expr"],
                "properties": {
                    "expr": {
                        "type": "string",
                        "description": "Query expression (PromQL, InfluxQL, etc.).",
                    },
                    "datasource_id": {
                        "type": "integer",
                        "description": (
                            "Numeric data source ID (alternative to datasource_uid). "
                            "Use list_grafana_data_sources to find valid IDs."
                        ),
                    },
                    "datasource_uid": {
                        "type": "string",
                        "description": (
                            "String UID of the data source — preferred over datasource_id "
                            "(e.g. 'P8E80F9AEF21F6940'). "
                            "Use list_grafana_data_sources to find valid UIDs. "
                            "At least one of datasource_uid or datasource_id must be supplied."
                        ),
                    },
                    "ref_id": {
                        "type": "string",
                        "default": "A",
                        "description": "Reference ID for the query (default 'A').",
                    },
                    "from_time": {
                        "type": "string",
                        "default": "now-1h",
                        "description": "Start time — Grafana relative (now-1h) or ISO 8601 (default now-1h).",
                    },
                    "to_time": {
                        "type": "string",
                        "default": "now",
                        "description": "End time — Grafana relative (now) or ISO 8601 (default now).",
                    },
                },
            },
            source="integration:grafana",
        ),
        ToolDefinition(
            name="discover_loki_datasource",
            description=(
                "Find all Loki datasources configured in Grafana and return their numeric ID, UID, and URL. "
                "Always call this first before using any other Loki tool. "
                "The numeric ID is required by: query_loki_logs, get_loki_labels, "
                "get_loki_label_values, get_loki_series."
            ),
            input_schema={"type": "object", "properties": {}},
            source="integration:grafana",
        ),
        ToolDefinition(
            name="query_loki_logs",
            description=(
                "Query Loki logs via the Grafana datasource proxy using LogQL. "
                "Workflow: 1) discover_loki_datasource to get the datasource ID, "
                "2) (optional) get_loki_labels + get_loki_label_values to explore available selectors, "
                "3) call this tool with a LogQL expression. "
                'Example expressions: \'{job="backend"} |= "error" | json\', '
                '\'{namespace="prod", container="api"} | logfmt | level="error"\'.'
            ),
            input_schema={
                "type": "object",
                "required": ["datasource_id", "expr"],
                "properties": {
                    "datasource_id": {
                        "type": "integer",
                        "description": "Numeric Loki datasource ID from discover_loki_datasource.",
                    },
                    "expr": {
                        "type": "string",
                        "description": 'LogQL query expression. Must include a stream selector: {label="value"}.',
                    },
                    "start": {
                        "type": "string",
                        "default": "now-1h",
                        "description": "Start time — relative (now-1h, now-30m, now-2d) or ISO 8601. Default: now-1h.",
                    },
                    "end": {
                        "type": "string",
                        "default": "now",
                        "description": "End time — relative (now) or ISO 8601. Default: now.",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 100,
                        "description": "Max log lines to return (default 100, max 1000).",
                    },
                    "direction": {
                        "type": "string",
                        "default": "backward",
                        "description": "'backward' (newest first, default) or 'forward' (oldest first).",
                    },
                },
            },
            source="integration:grafana",
        ),
        ToolDefinition(
            name="get_loki_labels",
            description=(
                "List all available Loki log stream label names for a datasource. "
                "Call this after discover_loki_datasource to explore what labels exist "
                "before writing a LogQL query. "
                "Follow up with get_loki_label_values to see what values each label has."
            ),
            input_schema={
                "type": "object",
                "required": ["datasource_id"],
                "properties": {
                    "datasource_id": {
                        "type": "integer",
                        "description": "Numeric Loki datasource ID from discover_loki_datasource.",
                    },
                    "start": {
                        "type": "string",
                        "default": "now-6h",
                        "description": "Start time for label discovery window (default now-6h).",
                    },
                    "end": {
                        "type": "string",
                        "default": "now",
                        "description": "End time for label discovery window (default now).",
                    },
                },
            },
            source="integration:grafana",
        ),
        ToolDefinition(
            name="get_loki_label_values",
            description=(
                "Get all values for a specific Loki label (e.g. all values of 'job', 'namespace', 'container'). "
                "Use this after get_loki_labels to build precise LogQL stream selectors like '{job=\"backend\"}'."
            ),
            input_schema={
                "type": "object",
                "required": ["datasource_id", "label"],
                "properties": {
                    "datasource_id": {
                        "type": "integer",
                        "description": "Numeric Loki datasource ID from discover_loki_datasource.",
                    },
                    "label": {
                        "type": "string",
                        "description": "Label name to inspect (e.g. 'job', 'namespace', 'container', 'pod').",
                    },
                    "start": {
                        "type": "string",
                        "default": "now-6h",
                        "description": "Start time for value discovery window (default now-6h).",
                    },
                    "end": {
                        "type": "string",
                        "default": "now",
                        "description": "End time for value discovery window (default now).",
                    },
                },
            },
            source="integration:grafana",
        ),
        ToolDefinition(
            name="get_loki_series",
            description=(
                "Find all unique Loki log stream combinations (label sets) matching a selector. "
                "More specific than get_loki_labels — returns actual stream fingerprints "
                "(all label=value pairs for each stream). Useful for discovering exactly "
                "which streams exist before querying. Use '{}' to list all streams."
            ),
            input_schema={
                "type": "object",
                "required": ["datasource_id"],
                "properties": {
                    "datasource_id": {
                        "type": "integer",
                        "description": "Numeric Loki datasource ID from discover_loki_datasource.",
                    },
                    "match": {
                        "type": "string",
                        "default": "{}",
                        "description": "LogQL stream selector to filter results (e.g. '{namespace=\"prod\"}'). Default '{}' lists all streams.",
                    },
                    "start": {
                        "type": "string",
                        "default": "now-6h",
                        "description": "Start time (default now-6h).",
                    },
                    "end": {
                        "type": "string",
                        "default": "now",
                        "description": "End time (default now).",
                    },
                },
            },
            source="integration:grafana",
        ),
        ToolDefinition(
            name="get_loki_log_stats",
            description=(
                "Get ingestion statistics for a Loki log stream — number of streams, total entries, "
                "chunks, and compressed byte size — without downloading the actual log lines. "
                "Useful for understanding log volume before running a full query."
            ),
            input_schema={
                "type": "object",
                "required": ["datasource_id", "query"],
                "properties": {
                    "datasource_id": {
                        "type": "integer",
                        "description": "Numeric Loki datasource ID from discover_loki_datasource.",
                    },
                    "query": {
                        "type": "string",
                        "description": "LogQL stream selector (e.g. '{job=\"backend\"}' or '{namespace=\"prod\"}').",
                    },
                    "start": {
                        "type": "string",
                        "default": "now-1h",
                        "description": "Start time — relative (now-1h, now-30m) or ISO 8601. Default: now-1h.",
                    },
                    "end": {
                        "type": "string",
                        "default": "now",
                        "description": "End time — relative (now) or ISO 8601. Default: now.",
                    },
                },
            },
            source="integration:grafana",
        ),
        ToolDefinition(
            name="discover_prometheus_datasource",
            description=(
                "Find all Prometheus datasources configured in Grafana and return their numeric ID, UID, name, and URL. "
                "Always call this first before using get_prometheus_metric_names or get_prometheus_label_values. "
                "The numeric ID is required by those tools."
            ),
            input_schema={"type": "object", "properties": {}},
            source="integration:grafana",
        ),
        ToolDefinition(
            name="get_prometheus_metric_names",
            description=(
                "List all metric names available in a Prometheus datasource. "
                "Optionally filter by a PromQL series selector (e.g. '{job=\"backend\"}'). "
                "Use discover_prometheus_datasource first to get the numeric datasource ID."
            ),
            input_schema={
                "type": "object",
                "required": ["datasource_id"],
                "properties": {
                    "datasource_id": {
                        "type": "integer",
                        "description": "Numeric Prometheus datasource ID from discover_prometheus_datasource.",
                    },
                    "match": {
                        "type": "string",
                        "default": "",
                        "description": "PromQL series selector to limit results (e.g. '{job=\"backend\"}'). Leave empty for all metrics.",
                    },
                    "start": {
                        "type": "string",
                        "default": "now-6h",
                        "description": "Start of the discovery window (default now-6h).",
                    },
                    "end": {
                        "type": "string",
                        "default": "now",
                        "description": "End of the discovery window (default now).",
                    },
                },
            },
            source="integration:grafana",
        ),
        ToolDefinition(
            name="get_prometheus_label_values",
            description=(
                "Get all values for a specific Prometheus label (e.g. all 'job' values, all 'instance' values). "
                "Use discover_prometheus_datasource first to get the numeric datasource ID, "
                "then call this to build precise PromQL selectors."
            ),
            input_schema={
                "type": "object",
                "required": ["datasource_id", "label"],
                "properties": {
                    "datasource_id": {
                        "type": "integer",
                        "description": "Numeric Prometheus datasource ID from discover_prometheus_datasource.",
                    },
                    "label": {
                        "type": "string",
                        "description": "Label name to inspect (e.g. 'job', 'instance', 'namespace', '__name__').",
                    },
                    "match": {
                        "type": "string",
                        "default": "",
                        "description": "PromQL series selector to narrow results (e.g. '{__name__=\"up\"}'). Optional.",
                    },
                    "start": {
                        "type": "string",
                        "default": "now-6h",
                        "description": "Start of the discovery window (default now-6h).",
                    },
                    "end": {
                        "type": "string",
                        "default": "now",
                        "description": "End of the discovery window (default now).",
                    },
                },
            },
            source="integration:grafana",
        ),
        ToolDefinition(
            name="list_grafana_alerts",
            description=(
                "List Grafana-managed alert rules with their state (firing, "
                "pending, normal), health and firing instance counts. For the "
                "alert instances themselves use list_firing_alerts."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "default": 50,
                        "description": "Max alert rules to return (default 50).",
                    },
                    "state": {
                        "type": "string",
                        "enum": ["", "firing", "pending", "normal"],
                        "default": "",
                        "description": "Only rules in this state (empty: every rule).",
                    },
                },
            },
            source="integration:grafana",
        ),
        ToolDefinition(
            name="read_grafana_alert",
            description="Get full alert rule details by UID. Includes condition, datasource, annotations.",
            input_schema={
                "type": "object",
                "required": ["alert_uid"],
                "properties": {
                    "alert_uid": {"type": "string", "description": "Alert rule UID."}
                },
            },
            source="integration:grafana",
        ),
        ToolDefinition(
            name="create_grafana_alert_rule",
            description=(
                "Create a Grafana-managed alert rule (provisioning API) and read it back. "
                "Needs Editor or alerting rule write permission."
            ),
            input_schema={
                "type": "object",
                "required": ["rule_json"],
                "properties": {
                    "rule_json": {
                        "type": "string",
                        "description": "Rule as JSON: title, folderUID, ruleGroup, condition, data (queries); optional for, labels, annotations.",
                    }
                },
            },
            source="integration:grafana",
        ),
        ToolDefinition(
            name="update_grafana_alert_rule",
            description="Replace a Grafana-managed alert rule by UID with the given rule and read it back.",
            input_schema={
                "type": "object",
                "required": ["alert_uid", "rule_json"],
                "properties": {
                    "alert_uid": {"type": "string", "description": "Alert rule UID."},
                    "rule_json": {
                        "type": "string",
                        "description": "The whole rule as JSON (read_grafana_alert fields).",
                    },
                },
            },
            source="integration:grafana",
        ),
        ToolDefinition(
            name="delete_grafana_alert_rule",
            description="Delete a Grafana-managed alert rule by UID.",
            input_schema={
                "type": "object",
                "required": ["alert_uid"],
                "properties": {"alert_uid": {"type": "string", "description": "Alert rule UID."}},
            },
            source="integration:grafana",
        ),
        ToolDefinition(
            name="get_grafana_alertmanager_config",
            description=(
                "Get the current Grafana Alertmanager configuration (receivers, routes) "
                "from the unified-alerting API. Defaults to the built-in Grafana-managed "
                "Alertmanager; pass an external Alertmanager datasource UID to read that one."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "datasource": {
                        "type": "string",
                        "description": (
                            "'grafana' (default) for the built-in Alertmanager, or the "
                            "UID of an external Alertmanager datasource."
                        ),
                    }
                },
            },
            source="integration:grafana",
        ),
        ToolDefinition(
            name="list_firing_alerts",
            description=(
                "List currently active/firing alert instances from Grafana Alertmanager. "
                "Optionally filter by label matchers (e.g. 'severity=critical,namespace=prod'). "
                "Returns alert name, state, labels, annotations, and when it started firing."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "filter_labels": {
                        "type": "string",
                        "default": "",
                        "description": "Comma-separated label=value pairs to filter alerts (e.g. 'severity=critical,namespace=prod'). Leave empty for all firing alerts.",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 50,
                        "description": "Max alerts to return (default 50).",
                    },
                },
            },
            source="integration:grafana",
        ),
        ToolDefinition(
            name="list_alert_silences",
            description=(
                "List Alertmanager silences. Silences suppress alert notifications without stopping the alert from firing. "
                "Filter by state (active/pending/expired/all) and optionally by label matchers."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "state": {
                        "type": "string",
                        "default": "active",
                        "description": "Filter by silence state: 'active' (default), 'pending', 'expired', or 'all'.",
                    },
                    "filter_labels": {
                        "type": "string",
                        "default": "",
                        "description": "Comma-separated label=value pairs that the silence must match (e.g. 'alertname=HighMemory,namespace=prod'). Optional.",
                    },
                },
            },
            source="integration:grafana",
        ),
        ToolDefinition(
            name="create_alert_silence",
            description=(
                "Create an Alertmanager silence to suppress notifications for matching alerts for a given duration. "
                "The alert continues to fire but no notifications are sent. "
                "Requires Editor role or alerting:silences:write permission."
            ),
            input_schema={
                "type": "object",
                "required": ["matchers", "comment"],
                "properties": {
                    "matchers": {
                        "type": "string",
                        "description": "Comma-separated label=value (or label=~regex) pairs defining which alerts to silence (e.g. 'alertname=HighMemory,namespace=prod'). All matchers are AND-ed.",
                    },
                    "comment": {
                        "type": "string",
                        "description": "Reason for the silence — shown in the Grafana UI (required).",
                    },
                    "duration": {
                        "type": "string",
                        "default": "2h",
                        "description": "How long to silence: '30m', '2h', '1d', '7d' (default '2h').",
                    },
                    "created_by": {
                        "type": "string",
                        "default": "Copilot Agent",
                        "description": "Author label for the silence (default 'Copilot Agent').",
                    },
                },
            },
            source="integration:grafana",
        ),
        ToolDefinition(
            name="list_grafana_annotations",
            description="List annotations (alert events, user notes, API annotations) within a time range.",
            input_schema={
                "type": "object",
                "properties": {
                    "from_time": {
                        "type": "string",
                        "default": "now-6h",
                        "description": "Start time — Grafana relative (now-6h) or ISO 8601.",
                    },
                    "to_time": {
                        "type": "string",
                        "default": "now",
                        "description": "End time — Grafana relative (now) or ISO 8601.",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 50,
                        "description": "Max annotations to return (default 50).",
                    },
                    "alert_id": {
                        "type": "string",
                        "default": "",
                        "description": "Filter to a specific alert UID (optional).",
                    },
                },
            },
            source="integration:grafana",
        ),
        ToolDefinition(
            name="create_grafana_annotation",
            description="Create a user annotation in Grafana. Requires 'annotations:write' permission.",
            input_schema={
                "type": "object",
                "required": ["text"],
                "properties": {
                    "text": {"type": "string", "description": "Annotation text."},
                    "time": {
                        "type": "string",
                        "description": "Timestamp (ISO 8601 or Grafana relative, e.g. 'now-1h'). Default: now.",
                    },
                    "tags": {
                        "type": "string",
                        "description": "Comma-separated tags (optional).",
                    },
                },
            },
            source="integration:grafana",
        ),
        ToolDefinition(
            name="get_grafana_health",
            description="Check Grafana instance health, version, and build info.",
            input_schema={"type": "object", "properties": {}},
            source="integration:grafana",
        ),
    ]
