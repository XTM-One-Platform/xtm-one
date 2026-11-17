# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""OpenAEV widgets: the ad hoc dashboard query OpenAEV's own widgets run, and its answer.

``POST /api/dashboards/adhoc/{count|series|entities}`` with
``{"widget_config": ...}``, the endpoints OpenAEV's reporting pages use
(``useReportingRenderData.ts``). The integration's Tenant field, when set,
sends the call to that tenant's twin (``/api/tenants/{id}/...``); otherwise
OpenAEV answers for the caller's default tenant.

==========================  ============  =============================================
configuration               endpoint      answer
==========================  ============  =============================================
``flat``                    ``count``     ``{interval_count, difference_count}``
``temporal-histogram``      ``series``    ``[{label, data: [{key, label, value}]}]``
``structural-histogram``    ``series``    the first series, one item per bucket
``list``                    ``entities``  ``{es_datas: [{base_representative, ...}], total}``
``average``                 ``average``   ``{security_domain_average: [{label, data: [series]}]}``
==========================  ============  =============================================

Its security posture widgets read the same answers and what OpenAEV's own
widgets read besides, and become validation outcomes (``outcomes``) drawn by
the engine's posture views (:func:`presentation`); the SUCCESS, FAILED and
PENDING series are found by their label, else by their place:

* the command center: an outcome per expectation type, in OpenAEV's gate order,
  with the security platforms it orbits (``/security_platforms``, a sample set
  when none is connected) and its calls to action;
* the exposure score: an outcome per bucket; a resilience gauge: the status
  buckets of one expectation type, with its glyph;
* the security domains (the average answer): an outcome per domain, its
  expectation types as parts with their status rows, and every domain OpenAEV
  knows (``/domains``) in its order, the unclassified one left out;
* the security coverage: every kill chain and its phases, every top-level
  technique (``/kill_chain_phases``, ``/attack_patterns/search``, read once an
  hour per credential) and the tested ones' outcomes, a sub-technique counted
  in its top-most parent's; when the reference cannot be read, the techniques
  tested are listed alone;
* an attack path: the techniques its simulation's injects carry (read with
  ``/adhoc/entities``, all time), each under every one of its phases, linked to
  the techniques of the injects that follow it
  (``base_attack_patterns_children_side``) and opening the simulation's
  injects on it, with the simulation's dates (``/exercises/{id}``); without a
  simulation, it asks for one;
* a posture radar's series stay series.

Every link is built here from the integration's base URL and ids.

A viewer's range replaces the widget's time range with OpenAEV's nearest
named one (``LAST_WEEK`` ... ``LAST_YEAR``).
"""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import uuid
from datetime import UTC, datetime
from urllib.parse import quote

from app.services.dashboard_platform.openaev import TYPE_SHAPES

from . import (
    LiveError,
    LiveRequest,
    distribution_data,
    link,
    number,
    number_data,
    outcomes_data,
    rows_data,
    series_data,
    text,
)
from ._http import request_json

_ENDPOINTS = {"flat": "count", "temporal-histogram": "series", "structural-histogram": "series", "list": "entities", "average": "average"}
#: The technique widgets read every technique, as OpenAEV raises their limit for them.
_TECHNIQUE_TYPES = ("security-coverage", "attack-path")
_COVERAGE_LIMIT = 10_000
#: The security domain OpenAEV has not classified, left out of its cards.
_UNCLASSIFIED = "To classify"
#: The kill chains and techniques a technique grid is laid out with: read again after an hour.
REFERENCE_SECONDS = 3600
_PATTERN_PAGE = 250
_MAX_PATTERN_PAGES = 8
_MAX_PHASES = 200
#: How far a sub-technique's parents are followed.
_MAX_PARENT_DEPTH = 5
#: An attack path reads at most this many of its simulation's injects.
_MAX_INJECTS = 1000
#: OpenAEV's time range of each period key (``TimeRangeUtils``), then of a length in days.
_RANGE_KEYS = {
    "1d": "LAST_DAY", "7d": "LAST_WEEK", "30d": "LAST_MONTH", "1m": "LAST_MONTH", "90d": "LAST_QUARTER",
    "3m": "LAST_QUARTER", "6m": "LAST_SEMESTER", "1y": "LAST_YEAR", "all": "ALL_TIME",
}  # fmt: skip
_RANGES = {1: "LAST_DAY", 7: "LAST_WEEK", 30: "LAST_MONTH", 90: "LAST_QUARTER", 182: "LAST_SEMESTER", 365: "LAST_YEAR"}
#: The span a number's change is said over, by OpenAEV's time range (``TimeRangeFilters`` ``_progression``).
_SPANS = {
    "LAST_DAY": "1d", "LAST_WEEK": "7d", "LAST_MONTH": "1m", "LAST_QUARTER": "3m", "LAST_SEMESTER": "6m",
    "LAST_YEAR": "1y", "ALL_TIME": "all", "CUSTOM": "custom",
}  # fmt: skip


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def build_request(
    native: dict,
    *,
    range_days: int | None,
    tenant_id: str = "",
    range_key: str | None = None,
    range_start: datetime | None = None,
    range_end: datetime | None = None,
) -> tuple[str, dict]:
    """The path and body drawing *native*, over the dashboard's period when it has one (a fixed one as ``CUSTOM``)."""
    config = copy.deepcopy(native["widget_config"])
    endpoint = _ENDPOINTS.get(config.get("widget_configuration_type"))
    if endpoint is None:
        raise LiveError("unsupported")
    if native.get("widget_type") in _TECHNIQUE_TYPES:
        config["limit"] = _COVERAGE_LIMIT
    named = _RANGE_KEYS.get(range_key or "") or _RANGES.get(range_days or 0)
    if range_start or range_end:
        # OpenAEV's custom range has both ends: an open one runs from the epoch, or up to now.
        start, end = range_start or datetime(1970, 1, 1, tzinfo=UTC), range_end or datetime.now(UTC)
        config.update(time_range="CUSTOM", start=_iso(start), end=_iso(end))
    elif named:
        config["time_range"] = named
        config.pop("start", None)
        config.pop("end", None)
    prefix = f"/api/tenants/{quote(tenant_id, safe='')}" if tenant_id else "/api"
    return f"{prefix}/dashboards/adhoc/{endpoint}", {"widget_config": config}


def _bucket_label(bucket: dict) -> object:
    return bucket.get("label") or bucket.get("key")


def _roles(series: list[dict]) -> dict[str, dict]:
    """The SUCCESS, FAILED and PENDING series: by their label, else by their place (0, 1, 2)."""
    roles: dict[str, dict] = {}
    for role, word in (("success", "SUCCESS"), ("failed", "FAIL"), ("pending", "PENDING")):
        found = next((s for s in series if word in str(s.get("label") or "").upper()), None)
        if found is not None:
            roles[role] = found
    taken = [id(s) for s in roles.values()]
    for index, role in enumerate(("success", "failed", "pending")):
        if role not in roles and index < len(series) and id(series[index]) not in taken:
            roles[role] = series[index]
    return roles


def _buckets(series: dict | None) -> list[dict]:
    return [b for b in (series or {}).get("data") or [] if isinstance(b, dict)]


def _status_counts(buckets: list[dict]) -> tuple[float, float, float]:
    """A resilience gauge's buckets (expectation statuses) as success, failed and pending counts."""
    counts = {"success": 0.0, "failed": 0.0, "pending": 0.0}
    for bucket in buckets:
        name = str(_bucket_label(bucket) or "").upper()
        role = "success" if "SUCCESS" in name else "failed" if "FAIL" in name else "pending" if "PENDING" in name else None
        value = number(bucket.get("value"))
        if role and value is not None:
            counts[role] += value
    return counts["success"], counts["failed"], counts["pending"]


def _filter_value(config: dict, key: str) -> str | None:
    """The first value of the first series' filter on *key* (a resilience gauge's expectation type)."""
    series = config.get("series") or []
    group = series[0].get("filter") if series and isinstance(series[0], dict) else None
    for item in (group or {}).get("filters") or []:
        if isinstance(item, dict) and item.get("key") == key and item.get("values"):
            return str(item["values"][0])
    return None


# ── Security posture (``outcomes``) ─────────────────────────────────────────

#: An expectation type as OpenAEV's posture widgets write it, what a success is
#: for it, and its icon (``AttackFlow.tsx`` ``KNOWN_LABELS`` and ``OUTCOME_LABELS``,
#: ``ExpectationIconByType.tsx``, as Lucide names); another type is humanised,
#: its success "stopped".
_TYPES: dict[str, tuple[str, str, str]] = {
    "PREVENTION": ("Prevention", "prevented", "shield-check"),
    "DETECTION": ("Detection", "detected", "radio"),
    "VULNERABILITY": ("Vulnerability", "not vulnerable", "bug"),
    "MANUAL": ("Manual", "validated", "circle-check-big"),
    "CHALLENGE": ("Challenge", "validated", "trophy"),
    "ARTICLE": ("Article", "validated", "newspaper"),
    "HUMAN_RESPONSE": ("", "stopped", "headset"),
}
#: Every known expectation type is drawn in one identity colour (``EXPECTATION_TYPE_IDENTITY_COLOR``).
_TYPE_COLOR = "#0fbcff"
#: The command center's gates, in attack order; another type comes after, by name (``CommandCenterWidget.tsx``).
_GATE_ORDER = ("VULNERABILITY", "PREVENTION", "DETECTION", "MANUAL", "CHALLENGE", "ARTICLE")
#: A resilience gauge's glyph, by the expectation type its first series filters on (``ResilienceGaugeWidget.tsx``).
_GLYPHS = (("PREVENTION", "shield-check"), ("DETECTION", "eye"), ("VULNERABILITY", "bug"), ("MANUAL", "headset"),
           ("ARTICLE", "headset"), ("CHALLENGE", "headset"))  # fmt: skip
_GLYPH = "crosshair"
#: How a status reads for an expectation type (``statusUtils.computeInjectExpectationLabel``).
_STATUS_LABELS: dict[str, dict[str, str]] = {
    "SUCCESS": {"PREVENTION": "Prevented", "DETECTION": "Detected", "VULNERABILITY": "Not vulnerable"},
    "FAILED": {"PREVENTION": "Not Prevented", "DETECTION": "Not Detected", "VULNERABILITY": "Vulnerable"},
    "PARTIAL": {"PREVENTION": "Partially Prevented", "DETECTION": "Partially Detected", "VULNERABILITY": "Partially Vulnerable"},
    "PENDING": {"PREVENTION": "Pending", "DETECTION": "Pending", "VULNERABILITY": "Pending"},
}
_HUMAN_TYPES = ("MANUAL", "CHALLENGE", "ARTICLE")
#: The security domains in OpenAEV's order and their icons (``utils/domains/domainIcons.tsx``); another comes after.
_DOMAINS: dict[str, str] = {
    "Endpoint": "monitor-smartphone", "Network": "earth", "Web App": "monitor", "E-mail Infiltration": "mail",
    "Data Exfiltration": "database", "URL Filtering": "lock", "Cloud": "cloud", "Artificial Intelligence": "bot", "Tabletop": "users",
}  # fmt: skip
_DOMAIN_ICON = "circle-question-mark"
#: What a domain card shows while it has no data (``DEFAULT_EMPTY_EXPECTATIONS``).
_PLACEHOLDER_TYPES = ("PREVENTION", "DETECTION", "VULNERABILITY")
#: A security platform type's colour (``components/securityPlatformType.ts``), and the platforms
#: the command center orbits when none is connected.
_PLATFORM_COLORS: dict[str, str] = {
    "EDR": "#0fbcff", "XDR": "#00bcd4", "SIEM": "#ffb300", "SOAR": "#9575cd", "NDR": "#26a96c", "ISPM": "#ff7043",
    "EMAIL_SECURITY": "#4dd0e1", "LLM_FIREWALL": "#00f1bd", "AI_GATEWAY": "#7e57c2", "VULNERABILITY_SCANNER": "#ef5350",
}  # fmt: skip
_SAMPLE_PLATFORMS = ("EDR", "SIEM", "NDR", "SOAR", "XDR", "ISPM")
_MAX_PLATFORMS = 8
#: The command center's calls to action (``CommandCenterWidget.tsx``): label, caption, icon, colour, page.
_ACTIONS = (
    ("Design a scenario", "Emulate real-world adversaries", "route", "accent", "/admin/scenarios"),
    ("Launch a simulation", "Validate your defenses now", "circle-play", "info", "/admin/simulations"),
    ("Run an atomic test", "Fire a single technique", "target", "warning", "/admin/atomic_testings"),
    ("Review findings", "Hunt down validated gaps", "binoculars", "danger", "/admin/findings"),
)
#: The kill chains OpenAEV gives their product name (``killChainLabel.ts``).
_CHAIN_LABELS = {"mitre-attack": "MITRE ATT&CK", "mitre-atlas": "MITRE ATLAS"}
#: An attack path's technique opens its simulation's injects filtered on it, 20 a page (``AttackPath.tsx``).
_INJECTS_PAGE = 20


def _type_label(key: str) -> str:
    known = _TYPES.get(key.upper())
    if known and known[0]:
        return known[0]
    clean = key.replace("_", " ").replace("-", " ").lower()
    return clean[:1].upper() + clean[1:]


def _type_extras(key: str, *, colored: bool = True) -> dict:
    upper = key.upper()
    known = _TYPES.get(upper)
    extras = {"key": upper, "successLabel": known[1] if known else "stopped", "icon": known[2] if known else "circle-question-mark"}
    if colored and known:
        extras["color"] = _TYPE_COLOR
    return extras


def _by_label(series: list[dict]) -> dict[str, dict[str, float]]:
    """The SUCCESS, FAILED and PENDING counts of each bucket label, in the order labels come."""
    merged: dict[str, dict[str, float]] = {}
    for role, line in _roles(series).items():
        for bucket in _buckets(line):
            label = bucket.get("label")
            if not label:
                continue
            entry = merged.setdefault(str(label), {"success": 0.0, "failed": 0.0, "pending": 0.0})
            entry[role] += number(bucket.get("value")) or 0.0
    return merged


def _gate_rank(key: str) -> tuple[int, str]:
    upper = key.upper()
    return (_GATE_ORDER.index(upper) if upper in _GATE_ORDER else len(_GATE_ORDER), key)


def _status_role(status: str) -> str:
    upper = status.upper()
    return "success" if upper == "SUCCESS" else "failed" if upper == "FAILED" else "pending"


def _status_label(status: str, expectation: str) -> str:
    upper, kind = status.upper(), expectation.upper()
    label = _STATUS_LABELS.get(upper, {}).get(kind)
    if label:
        return label
    return status[:1].upper() + status[1:].lower() if kind in _HUMAN_TYPES else ""


def _domain_parts(series: list[dict]) -> list[tuple]:
    """A domain's expectation types: counts by status, and the status rows its open card lists."""
    parts = []
    for line in series:
        kind = str(line.get("label") or "")
        rows = []
        for bucket in _buckets(line):
            status = str(bucket.get("key") or bucket.get("label") or "")
            rows.append({"label": _status_label(status, kind), "value": bucket.get("value"), "status": _status_role(status), "key": status})
        counts = {"success": 0.0, "failed": 0.0, "pending": 0.0}
        for bucket in _buckets(line):
            status = str(bucket.get("key") or "").upper()
            role = "success" if status == "SUCCESS" else "failed" if status == "FAILED" else "pending"
            counts[role] += number(bucket.get("value")) or 0.0
        name = kind[:1].upper() + kind[1:].lower()
        extras = {"icon": _type_extras(kind)["icon"], "key": kind.upper(), "total": line.get("value"), "rows": rows}
        parts.append((name, counts["success"], counts["failed"], counts["pending"], extras))
    return parts


def domain_reference(domains: object) -> list[dict]:
    """Every security domain OpenAEV knows but the unclassified one, in its order, each with its icon."""
    rows = [d for d in domains if isinstance(d, dict) and d.get("domain_name")] if isinstance(domains, list) else []
    rows = [d for d in rows if d["domain_name"] != _UNCLASSIFIED]
    order = list(_DOMAINS)
    rows.sort(key=lambda d: order.index(d["domain_name"]) if d["domain_name"] in order else len(order))
    placeholders = [{"label": k[:1] + k[1:].lower(), "icon": _type_extras(k)["icon"]} for k in _PLACEHOLDER_TYPES]
    return [
        {"id": str(d.get("domain_id") or d["domain_name"]), "label": d["domain_name"], "icon": _DOMAINS.get(d["domain_name"], _DOMAIN_ICON), "parts": placeholders}
        for d in rows
    ]


def orbit_of(platforms: object) -> list[dict]:
    """The security platforms the command center orbits, the first eight; a sample set when none is connected."""
    rows = [p for p in platforms if isinstance(p, dict) and p.get("asset_name")] if isinstance(platforms, list) else []
    if not rows:
        return [{"label": kind, "type": kind, "color": _PLATFORM_COLORS[kind]} for kind in _SAMPLE_PLATFORMS]
    orbit = []
    for platform in rows[:_MAX_PLATFORMS]:
        kind = str(platform.get("security_platform_type") or "EDR")
        orbit.append({"label": platform["asset_name"], "type": kind, "color": _PLATFORM_COLORS.get(kind)})
    return orbit


def _posture(native: dict, answer: object, *, base_url: str = "", context: dict | None = None) -> dict:
    """A security posture widget's answer: outcomes by label, or a posture radar's series."""
    widget_type = native.get("widget_type")
    config = native["widget_config"]
    context = context or {}
    if widget_type == "average":
        if not isinstance(answer, dict):
            raise LiveError("upstream_error")
        items = []
        for domain in (d for d in answer.get("security_domain_average") or [] if isinstance(d, dict)):
            label = domain.get("label")
            if not label or label == _UNCLASSIFIED:
                continue
            parts = _domain_parts([s for s in domain.get("data") or [] if isinstance(s, dict)])
            totals = [sum(p[i] for p in parts) for i in (1, 2, 3)]
            items.append((label, *totals, parts, "", "", [], {"icon": _DOMAINS.get(str(label), _DOMAIN_ICON)}))
        return outcomes_data(items, reference=domain_reference(context["domains"]) if "domains" in context else None)
    series = [s for s in answer if isinstance(s, dict)] if isinstance(answer, list) else []
    if widget_type == "posture-radar":
        return series_data(
            [
                (_status_name(s.get("label")), [(_bucket_label(b), b.get("value"), b.get("key")) for b in _buckets(s)], {"color": _radar_color(s.get("label"), index)})
                for index, s in enumerate(series)
            ]
        )
    if widget_type == "resilience-gauge":
        kind = _filter_value(config, "inject_expectation_type") or ""
        label = _type_label(kind) if kind else config.get("title") or (series[0].get("label") if series else "")
        glyph = next((icon for word, icon in _GLYPHS if word in kind.upper()), _GLYPH)
        return outcomes_data([(label, *_status_counts(_buckets(series[0] if series else None)), [], "", "", [], {"icon": glyph})])
    counts = _by_label(series)
    if widget_type == "command-center":
        keys = sorted(counts, key=_gate_rank)
        actions = [
            {"label": label, "caption": caption, "icon": icon, "color": tone, "link": link(base_url, page)}
            for label, caption, icon, tone, page in _ACTIONS
        ]
        return outcomes_data(
            [(_type_label(k), counts[k]["success"], counts[k]["failed"], counts[k]["pending"], [], "", "", [], _type_extras(k)) for k in keys],
            actions=actions, orbit=orbit_of(context.get("platforms")),
        )  # fmt: skip
    if widget_type == "exposure-score":
        # One outcome per bucket, as it comes.
        return outcomes_data(
            [(_type_label(k), c["success"], c["failed"], c["pending"], [], "", "", [], _type_extras(k, colored=False)) for k, c in counts.items()]
        )
    # A technique grid without its reference: the techniques tested, as OpenAEV labels them.
    return outcomes_data([(k, c["success"], c["failed"], c["pending"], []) for k, c in counts.items()])


def _prefix(client) -> str:
    return f"/api/tenants/{quote(client.tenant_id, safe='')}" if client.tenant_id else "/api"


def _credential_key(client, kind: str) -> str:
    """A cache key derived from the credential itself, so that no one reads another's: OpenAEV filters by the caller's grants."""
    secret = "|".join((client.base_url, client.tenant_id, client.headers.get("Authorization", ""), client.credential))
    return f"dashboard-aev-{kind}:" + hashlib.sha256(secret.encode()).hexdigest()[:40]


async def read_reference(client) -> dict:
    """The kill chain phases and the techniques (id, external id, name, parent, phases), as this credential reads them.

    Kept an hour under a key derived from the credential itself, so that no
    one reads another's: OpenAEV filters both lists by the caller's grants.
    """
    from . import _cache_get, _cache_set

    key = _credential_key(client, "reference")
    cached = await _cache_get(key)
    if cached is not None:
        return cached
    base = f"{client.base_url}{_prefix(client)}"
    answer = await request_json("GET", f"{base}/kill_chain_phases", client.headers, what="OpenAEV")
    phases = [
        {
            "id": text(p.get("phase_id"), 100),
            "name": text(p.get("phase_name"), 120),
            "order": number(p.get("phase_order")) or 0.0,
            "chain": text(p.get("phase_kill_chain_name"), 120),
        }
        for p in (answer if isinstance(answer, list) else [])[:_MAX_PHASES]
        if isinstance(p, dict) and p.get("phase_id")
    ]
    patterns: list[dict] = []
    for page in range(_MAX_PATTERN_PAGES):
        answer = await request_json(
            "POST", f"{base}/attack_patterns/search", client.headers, json_body={"page": page, "size": _PATTERN_PAGE}, what="OpenAEV"
        )
        content = answer.get("content") if isinstance(answer, dict) else None
        rows = [p for p in content if isinstance(p, dict)] if isinstance(content, list) else []
        patterns += [
            {
                "id": text(p.get("attack_pattern_id"), 100),
                "external_id": text(p.get("attack_pattern_external_id"), 40),
                "name": text(p.get("attack_pattern_name"), 160),
                "parent": text(p.get("attack_pattern_parent"), 100) or None,
                "phases": [text(x, 100) for x in (p.get("attack_pattern_kill_chain_phases") or [])[:20] if x],
            }
            for p in rows
            if p.get("attack_pattern_id")
        ]
        if len(rows) < _PATTERN_PAGE or (isinstance(answer, dict) and answer.get("last") is True):
            break
    reference = {"phases": phases, "patterns": patterns}
    await _cache_set(key, reference, ttl=REFERENCE_SECONDS)
    return reference


def _pattern_counts(answer: object, patterns: dict[str, dict], *, roll_up: bool) -> dict[str, dict[str, float]]:
    """The SUCCESS and FAILED counts of each technique OpenAEV still knows, a sub-technique's in its top-most parent's."""

    def top(pattern_id: str) -> str:
        for _ in range(_MAX_PARENT_DEPTH):
            parent = patterns[pattern_id].get("parent")
            if not parent or parent not in patterns:
                break
            pattern_id = parent
        return pattern_id

    series = [s for s in answer if isinstance(s, dict)] if isinstance(answer, list) else []
    counts: dict[str, dict[str, float]] = {}
    for role, line in _roles(series).items():
        if role == "pending":
            continue
        for bucket in _buckets(line):
            key = str(bucket.get("key") or "")
            # A technique OpenAEV no longer knows is left out, as it does.
            if key in patterns:
                entry = counts.setdefault(top(key) if roll_up else key, {"success": 0.0, "failed": 0.0})
                entry[role] += number(bucket.get("value")) or 0.0
    return counts


def _kill_chains(phases: list[dict]) -> list[dict]:
    """The kill chains by name, each with its phases in order; the first holding "attack" opens (``SecurityCoverageContent.tsx``)."""
    by_chain: dict[str, list[dict]] = {}
    for phase in phases:
        by_chain.setdefault(phase.get("chain") or "", []).append(phase)
    names = sorted(by_chain, key=str.casefold)
    opening = next((name for name in names if "attack" in name.lower()), names[0] if names else None)
    return [
        {
            "id": name,
            "label": _CHAIN_LABELS.get(name.lower(), name),
            "default": name == opening,
            "groups": [{"id": p["id"], "label": p.get("name") or "-"} for p in sorted(by_chain[name], key=lambda p: p.get("order") or 0)],
        }
        for name in names
    ]


def technique_matrix(native: dict, answer: object, reference: dict) -> dict:
    """A security coverage: every kill chain and its phases, every top-level technique, the tested ones' outcomes.

    A sub-technique's outcomes count in its top-most parent's, and each top-level
    technique lists its sub-techniques (``subIds``), which a click on it covers too.
    """
    patterns = {p["id"]: p for p in reference.get("patterns") or [] if isinstance(p, dict) and p.get("id")}
    phases = [p for p in reference.get("phases") or [] if isinstance(p, dict) and p.get("id")]
    if not phases:
        return _posture(native, answer)
    counts = _pattern_counts(answer, patterns, roll_up=True)
    children: dict[str, list[str]] = {}
    for pattern in patterns.values():
        if pattern.get("parent"):
            children.setdefault(pattern["parent"], []).append(pattern["id"])
    tops = [p for p in patterns.values() if not p.get("parent")]
    entries = [
        {"id": p["id"], "label": p.get("name") or p["id"], "code": p.get("external_id"), "phases": p.get("phases") or [], "subIds": children.get(p["id"], [])}
        for p in tops
    ]
    items = [
        (patterns[pid].get("name") or pid, got["success"], got["failed"], 0.0, [], "", pid, [], {"code": patterns[pid].get("external_id")})
        for pid, got in counts.items()
        if not patterns[pid].get("parent")
    ]
    return outcomes_data(items, chains=_kill_chains(phases), reference=entries)


def injects_request(native: dict, tenant_id: str = "") -> tuple[str, dict] | None:
    """The ad hoc list of an attack path's simulation's injects, or ``None`` when it names no simulation."""
    simulation = _filter_value(native["widget_config"], "base_simulation_side")
    if not simulation:
        return None
    prefix = f"/api/tenants/{quote(tenant_id, safe='')}" if tenant_id else "/api"
    filters = [
        {"key": "base_entity", "operator": "eq", "mode": "or", "values": ["inject"]},
        {"key": "base_simulation_side", "operator": "eq", "mode": "or", "values": [simulation]},
    ]
    config = {
        "widget_configuration_type": "list", "time_range": "ALL_TIME", "date_attribute": "base_created_at",
        "perspective": {"name": "inject", "filter": {"mode": "and", "filters": filters}}, "columns": [], "sorts": [], "limit": _MAX_INJECTS,
    }  # fmt: skip
    return f"{prefix}/dashboards/adhoc/entities", {"widget_config": config}


def _qs(value: object, prefix: str = "") -> list[str]:
    """*value* as ``qs.stringify`` writes it (brackets for nesting, indices for lists, RFC 3986 encoding)."""
    if isinstance(value, dict):
        return [pair for key, inner in value.items() for pair in _qs(inner, f"{prefix}[{key}]" if prefix else str(key))]
    if isinstance(value, list):
        return [pair for index, inner in enumerate(value) for pair in _qs(inner, f"{prefix}[{index}]")]
    return [f"{quote(prefix, safe='')}={quote(str(value), safe='')}"]


def injects_link(base_url: str, simulation: str, pattern_id: str) -> str | None:
    """The simulation's injects on one technique, as an attack path's node opens them (``AttackPath.tsx``)."""
    query = {
        "page": 0, "size": _INJECTS_PAGE, "sorts": [{"property": "inject_updated_at", "direction": "DESC"}],
        "filterGroup": {"mode": "or", "filters": [{
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"inject_attack_patterns:{pattern_id}")), "key": "inject_attack_patterns",
            "mode": "and", "values": [pattern_id], "operator": "contains",
        }]},
        "key": "simulation-injects-results",
    }  # fmt: skip
    encoded = base64.b64encode("&".join(_qs(query)).encode()).decode()
    return link(base_url, f"/admin/simulations/{quote(simulation, safe='')}?query={encoded}")


def attack_path(native: dict, answer: object, reference: dict, injects: object, *, base_url: str = "", exercise: object = None) -> dict:
    """An attack path as OpenAEV draws it: the techniques its simulation's injects carry, each
    placed under every one of its kill chain phases (in their order), linked to the techniques of
    the injects that follow, and opening the simulation's injects on it; the simulation's dates."""
    patterns = {p["id"]: p for p in reference.get("patterns") or [] if isinstance(p, dict) and p.get("id")}
    phases = {p["id"]: p for p in reference.get("phases") or [] if isinstance(p, dict) and p.get("id")}
    counts = _pattern_counts(answer, patterns, roll_up=False)
    simulation = _filter_value(native["widget_config"], "base_simulation_side") or ""
    rows = injects.get("es_datas") if isinstance(injects, dict) else None
    nodes: dict[str, set[str]] = {}
    for inject in rows if isinstance(rows, list) else []:
        if not isinstance(inject, dict):
            continue
        children = {str(c) for c in inject.get("base_attack_patterns_children_side") or [] if c}
        for pattern_id in inject.get("base_attack_patterns_side") or []:
            nodes.setdefault(str(pattern_id), set()).update(children)
    items = []
    used: dict[str, dict] = {}
    for pattern_id, children in nodes.items():
        pattern = patterns.get(pattern_id)
        if pattern is None:
            continue
        placed = [phases[p] for p in pattern.get("phases") or [] if p in phases]
        if not placed:
            continue
        used.update({p["id"]: p for p in placed})
        got = counts.get(pattern_id, {})
        links = sorted(c for c in children if c in nodes and c != pattern_id and c in patterns)
        extras = {"code": pattern.get("external_id"), "phases": [p["id"] for p in placed], "link": injects_link(base_url, simulation, pattern_id)}
        items.append((pattern.get("name") or pattern_id, got.get("success", 0.0), got.get("failed", 0.0), 0.0, [], "", pattern_id, links, extras))
    # The phases its techniques sit under, in their order (``sortKillChainPhase``: by order alone).
    groups = [{"id": p["id"], "label": p.get("name") or "-"} for p in sorted(used.values(), key=lambda p: p.get("order") or 0)]
    timeline = None
    if isinstance(exercise, dict):
        timeline = {"start": exercise.get("exercise_start_date"), "end": exercise.get("exercise_end_date")}
    return outcomes_data(items, chains=[{"id": "path", "label": "", "groups": groups}], timeline=timeline)


# ── Colours and looks ──────────────────────────────────────────────────────
#
# OpenAEV colours its charts by what their series and buckets mean
# (``chartColorUtils.ts``, ``statusUtils.ts``, ``PostureRadarWidget.tsx``;
# ``dev-docs/dashboard-rendering-reference.md``): computed here and sent as the
# series' and items' ``color``, so the engine never reads a label. A theme
# colour is the design system's named colour (``accent``, ``success``...) or,
# where OpenAEV's is the same in both themes, its hex; the engine draws the
# light twin of a dark one (``themedColor``).

_ERROR = "#f14337"
_AMBER = "#ff9800"
_ORANGE = "#ff7043"
_SECONDARY = "#00f0bc"
_DISABLED = "#75829a"
_GREY = "#9e9e9e"
_WARNING = "#ffa726"
#: Material green and red 400: true and false in a donut.
_GREEN, _RED = "#66bb6a", "#ef5350"


def _semantic_colors(names: list) -> list[str] | None:
    """Each series' colour by its name's meaning, or ``None`` when no name means anything (the palette then)."""
    matched = False
    colors = []
    for index, name in enumerate(names):
        upper = str(name or "").upper()
        color = None
        if "UNDETECTED" in upper or "NOT DETECTED" in upper:
            color = _ERROR
        elif "UNPREVENTED" in upper or "NOT PREVENTED" in upper:
            color = _AMBER
        elif "NOT " in upper or "FAILED" in upper or "MISSED" in upper:
            color = _ERROR
        elif "DETECTED" in upper:
            color = _SECONDARY
        elif "PREVENTED" in upper or "SUCCESS" in upper:
            color = "success"
        elif "PENDING" in upper:
            color = _DISABLED
        matched = matched or color is not None
        colors.append(color or ("accent", _SECONDARY, _AMBER, _ORANGE)[index % 4])
    return colors if matched else None


_STATUS_COLORS: dict[str, str] = {
    **dict.fromkeys(
        ("success", "executed", "warning", "access_denied", "prevented", "detected", "not vulnerable", "successful",
         "100", "ok", "running", "on-going"), "success"),
    **dict.fromkeys(
        ("error", "command_not_found", "command_cannot_be_executed", "invalid_usage", "timeout", "interrupted", "failed",
         "undetected", "unprevented", "not prevented", "not detected", "vulnerable", "0", "replace"), _ERROR),
    **dict.fromkeys(("asset_agentless", "agent_inactive", "pending", "draft", "finished", "not_planned"), _GREY),
    **dict.fromkeys(("partially prevented", "partially detected", "executed_with_cleanup_failure", "paused"), _WARNING),
    **dict.fromkeys(("info", "executing", "scheduled"), "info"),
    **dict.fromkeys(("partial", "update"), "warning"),
    "queuing": "alert",
    "canceled": "neutral",
    "maybe_prevented": "#ba88ff",
    "maybe_partial_prevented": "#ea80fc",
}  # fmt: skip


def _status_color(label: object) -> str:
    """A status' colour (``getStatusColor``): an unknown one is an error's."""
    return _STATUS_COLORS.get(str(label or "").lower(), _ERROR)


def _breakdown_colors(native: dict, labels: list, series_color: str | None) -> list[str | None]:
    """A breakdown's bucket colours: a donut's by status (or true and false), a bar's its series' meaning."""
    field = str(native["widget_config"].get("field") or "").lower()
    widget_type = native.get("widget_type")
    if widget_type == "donut":
        if "status" in field or "vulnerable_endpoint_action" in field:
            return [_status_color(label) for label in labels]
        if len(labels) == 2 and labels[0] in ("true", "false"):
            return [_GREEN, _RED] if labels[0] == "true" else [_RED, _GREEN]
    if widget_type in ("vertical-barchart", "horizontal-barchart") and series_color:
        return [series_color] * len(labels)
    return [None] * len(labels)


#: A status series as OpenAEV names it in a legend: its translation of the status (``t(s.label)``).
_STATUS_NAMES = {"SUCCESS": "Success", "FAILED": "Failed", "PENDING": "Pending"}


def _status_name(label: object) -> str:
    text = str(label or "")
    return _STATUS_NAMES.get(text.upper(), text)


def _radar_color(label: object, index: int) -> str:
    """A posture radar series' colour: a miss red, a success green, else the accent, the secondary, the warning colour.

    OpenAEV tests the successes first, so "NOT DETECTED" (it contains DETECTED) is drawn green; the misses are
    tested first here, as the widget means (listed in the reference's defects).
    """
    upper = str(label or "").upper()
    if "FAIL" in upper or "NOT " in upper or "UNDETECTED" in upper or "UNPREVENTED" in upper:
        return _ERROR
    if "SUCCESS" in upper or "DETECTED" in upper or "PREVENTED" in upper:
        return "success"
    return ("accent", _SECONDARY, _WARNING)[index % 3]


#: What every OpenAEV chart shares: axis labels in the secondary text colour, no axis line, the legend below.
_CHART = {"axisLabelColor": "muted", "hideAxisLines": True, "legendDisplay": "bottom", "niceTicks": True, "legendMarker": "square"}


def _chart_preset(native: dict) -> dict | None:
    """How OpenAEV draws a chart widget, as a visualization type and its generic options."""
    widget_type = native.get("widget_type")
    config = native.get("widget_config") or {}
    series = config.get("series") or []
    temporal = config.get("widget_configuration_type") == "temporal-histogram"
    if widget_type == "line":
        # An area chart: one series in the accent, several in the Material colours; a gradient under each.
        colors = {"palette": "material"} if len(series) >= 2 else {"paletteLead": "accent"}
        return {
            "view": "xtm.line",
            "options": {
                **_CHART, **colors, "lineWidth": 2, "fill": "gradient", "fillOpacityFrom": 0.35, "fillOpacityTo": 0.02,
                "markerSize": 4, "xAxisType": "time", "dateFormat": "longDate", "xAxisFontSize": 12, "yAxisFontSize": 14,
                "gridLines": "horizontal", "emptySample": "temporal",
            },
        }  # fmt: skip
    if widget_type == "vertical-barchart":
        return {
            "view": "xtm.column",
            "options": {
                **_CHART, "palette": "results", "fill": "gradient", "fillOpacityFrom": 0.95, "fillOpacityTo": 0.65,
                "barRadius": 4, "barSize": 55, "barGap": 4, "numberFormat": "locale", "xAxisFontSize": 12, "gridLines": "horizontal",
                **({"dateFormat": "longDate", "emptySample": "temporal"} if temporal else {"emptySample": "platforms"}),
            },
        }  # fmt: skip
    if widget_type == "horizontal-barchart":
        # One series: a colour per bar (the accent first) and its value at its end; two: by meaning, with a legend.
        distributed = len(series) <= 1
        return {
            "view": "xtm.bar",
            "options": {
                **_CHART, "paletteLead": "accent", "palette": "material", "fill": "gradient", "fillOpacityFrom": 1,
                "fillOpacityTo": 0.75, "barRadius": 5, "barSize": 38, "barGap": 3, "gridLines": "vertical", "gridOpacity": 0.08,
                "xAxisFontSize": 12, "yAxisFontSize": 11, "labelMaxLength": 28, "labelCut": "chars", "emptySample": "ttp",
                **({"colorByPoint": True, "dataValuesDisplay": "all", "dataValuesStrong": True, "numberFormat": "locale",
                    "legendDisplay": "off"} if distributed else {}),
            },
        }  # fmt: skip
    if widget_type == "donut":
        return {
            "view": "xtm.pie",
            "options": {
                "palette": "material", "separatorWidth": 3, "tooltipContent": "label", "labelDisplay": "percentage",
                "showDonutHole": True, "donutHoleSize": 70, "legendDisplay": "bottom", "collapseThreshold": 0, "emptySample": "status",
                "legendMarker": "circle",
            },
        }  # fmt: skip
    if widget_type == "posture-radar":
        return {
            "view": "xtm.radar",
            "options": {
                "lineWidth": 2, "areaOpacity": 0.18, "markerSize": 3, "radius": 108, "xAxisFontSize": 10, "axisLabelColor": "muted",
                "labelMaxLength": 100, "gridOpacity": 0.08, "legendDisplay": "bottom", "emptySample": "radar", "legendMarker": "circle",
            },
        }  # fmt: skip
    return None


# ── Numbers and lists, as OpenAEV draws them ─────────────────────────────────

#: ``NumberWidget``'s icon tile per counted entity (the left navigation's icons): a Lucide name and its colour.
_NUMBER_ICONS: dict[str, tuple[str, str]] = {
    "scenario": ("clapperboard", "#00f0bc"),
    "simulation": ("network", "accent"),
    "inject": ("zap", "warning"),
    "asset": ("server", "accent"),
    "finding": ("binoculars", "danger"),
    "vulnerable-endpoint": ("bug", "danger"),
    "asset-group": ("group", "accent"),
    "expectation-inject": ("siren", "warning"),
}
_NUMBER_ICON = ("hash", "accent")

#: A list row's leading icon per entity (``EntityLeadingIcon``) and per expectation type (``ExpectationIconByType``).
_ROW_ICONS: dict[str, str] = {
    "asset": "monitor-smartphone", "vulnerable-endpoint": "monitor-smartphone", "asset-group": "group", "team": "users",
    "finding": "binoculars", "scenario": "route", "inject": "crosshair", "attack-pattern": "lock-keyhole",
}  # fmt: skip
_EXPECTATION_ICONS: dict[str, str] = {
    "PREVENTION": "shield-check", "DETECTION": "radio", "VULNERABILITY": "bug", "HUMAN_RESPONSE": "headset",
    "MANUAL": "circle-check-big", "ARTICLE": "newspaper", "CHALLENGE": "trophy",
}  # fmt: skip
_EXPECTATION_LABELS = {
    "PREVENTION": "Prevention", "DETECTION": "Detection", "VULNERABILITY": "Vulnerability", "HUMAN_RESPONSE": "Human Response",
    "MANUAL": "Manual", "ARTICLE": "Article", "CHALLENGE": "Challenge",
}  # fmt: skip
#: The expectation type chip's colour (``EXPECTATION_TYPE_IDENTITY_COLOR``).
_EXPECTATION_COLOR = "#0fbcff"
#: What an expectation's status reads as, by its type (``computeInjectExpectationLabel``).
_EXPECTATION_RESULTS: dict[str, dict[str, str]] = {
    "SUCCESS": {"PREVENTION": "Prevented", "DETECTION": "Detected", "VULNERABILITY": "Not vulnerable"},
    "FAILED": {"PREVENTION": "Not Prevented", "DETECTION": "Not Detected", "VULNERABILITY": "Vulnerable"},
    "PARTIAL": {"PREVENTION": "Partially Prevented", "DETECTION": "Partially Detected", "VULNERABILITY": "Partially Vulnerable"},
    "PENDING": {"PREVENTION": "Pending", "DETECTION": "Pending", "VULNERABILITY": "Pending"},
}
_HUMAN_EXPECTATIONS = ("HUMAN_RESPONSE", "MANUAL", "ARTICLE", "CHALLENGE")
#: A status chip's colour (``statusSeverity``: green, blue, red, orange and grey as the engine's named colours).
_STATUS_SEVERITY: dict[str, str] = {
    **dict.fromkeys(("SUCCESS", "EXECUTED", "WARNING", "ACCESS_DENIED", "PREVENTED", "DETECTED", "NOT VULNERABLE", "RUNNING"), "success"),
    **dict.fromkeys(("ERROR", "COMMAND_NOT_FOUND", "COMMAND_CANNOT_BE_EXECUTED", "PREREQUISITE_FAILED", "INVALID_USAGE",
                     "TIMEOUT", "INTERRUPTED", "MAYBE_PREVENTED", "MAYBE_PARTIAL_PREVENTED", "FAILED", "ASSET_INACTIVE",
                     "NOT PREVENTED", "NOT DETECTED", "VULNERABLE"), "danger"),
    **dict.fromkeys(("EXECUTED_WITH_CLEANUP_FAILURE", "PARTIAL", "QUEUING", "PARTIALLY PREVENTED", "PARTIALLY DETECTED", "PAUSED"), "warning"),
    **dict.fromkeys(("INFO", "EXECUTING", "PENDING", "SCHEDULED"), "info"),
}  # fmt: skip
#: ``FINDING_TYPE_COLORS``: a finding type's chip.
_FINDING_COLORS = {"CVE": "#f44336", "PortsScan": "#0fbcff", "IPAddress": "#00bcd4", "Hostname": "#9575cd", "Text": "#78909c"}
_DATE_COLUMNS = frozenset({"base_created_at", "base_updated_at", "execution_date"})
#: Where a row opens in OpenAEV (``ListNavigationHandler``); other entities do not open.
_ROW_PATHS = {
    "asset": "/admin/assets", "asset-group": "/admin/asset_groups", "team": "/admin/teams", "scenario": "/admin/scenarios",
    "simulation": "/admin/simulations", "finding": "/admin/findings",
}  # fmt: skip


def _number_entity(config: dict) -> str:
    """What a number widget counts: its first series' ``base_entity``."""
    series = config.get("series") or []
    group = series[0].get("filter") if series and isinstance(series[0], dict) else None
    for item in (group or {}).get("filters") or []:
        if isinstance(item, dict) and item.get("key") == "base_entity" and item.get("values"):
            return str(item["values"][0])
    return ""


def _expectation_status(row: dict) -> dict | None:
    status = str(row.get("inject_expectation_status") or "").upper()
    kind = str(row.get("inject_expectation_type") or "").upper()
    label = _EXPECTATION_RESULTS.get(status, {}).get(kind) or (status.capitalize() if kind in _HUMAN_EXPECTATIONS and status else None)
    if not label:
        return None
    return {"label": label, "color": _STATUS_SEVERITY.get(label.upper(), "neutral"), "icon": _EXPECTATION_ICONS.get(kind, "circle-question-mark")}


def _list_cell(column: str, row: dict) -> object:
    """A list cell as ``ListColumnConfig`` renders it: a chip, a date or text."""
    value = row.get(column)
    if column == "finding_type" and value:
        return {"label": str(value), "color": _FINDING_COLORS.get(str(value), "#607d8b")}
    if column == "inject_expectation_type" and value:
        key = str(value).upper()
        return {"label": _EXPECTATION_LABELS.get(key, str(value)), "color": _EXPECTATION_COLOR, "icon": _EXPECTATION_ICONS.get(key, "circle-question-mark")}
    if column == "inject_expectation_status":
        return _expectation_status(row)
    if column in ("status", "inject_status") and value:
        return {"label": str(value).replace("_", " ").capitalize(), "color": _STATUS_SEVERITY.get(str(value).upper(), "neutral")}
    if isinstance(value, list):
        return ", ".join(str(v) for v in value if isinstance(v, str | int | float | bool))
    return value if isinstance(value, str | int | float | bool) else None


def _column_kind(column: str) -> str:
    if column in ("finding_type", "inject_expectation_type", "inject_expectation_status", "status", "inject_status"):
        return "chip"
    return "datetime" if column in _DATE_COLUMNS else "text"


def _row_link(base_url: str, row: dict) -> str | None:
    entity = str(row.get("base_entity") or "")
    row_id = row.get("base_id")
    if entity == "expectation-inject" and row.get("base_inject_side"):
        inject = quote(str(row["base_inject_side"]), safe="")
        simulation = row.get("base_simulation_side")
        page = f"/admin/simulations/{quote(str(simulation), safe='')}/injects/{inject}" if simulation else f"/admin/atomic_testings/{inject}"
        return link(base_url, f"{page}?expectation_id={quote(str(row_id or ''), safe='')}")
    if entity == "inject" and row_id:
        simulation = row.get("base_simulation_side")
        page = f"/admin/simulations/{quote(str(simulation), safe='')}/injects/{quote(str(row_id), safe='')}" if simulation else f"/admin/atomic_testings/{quote(str(row_id), safe='')}"
        return link(base_url, page)
    if entity == "vulnerable-endpoint" and row.get("vulnerable_endpoint_id"):
        return link(base_url, f"/admin/assets/{quote(str(row['vulnerable_endpoint_id']), safe='')}")
    if entity in _ROW_PATHS and row_id:
        return link(base_url, f"{_ROW_PATHS[entity]}/{quote(str(row_id), safe='')}")
    return None


def _row_icon(row: dict) -> str | None:
    expectation = str(row.get("inject_expectation_type") or "").upper()
    if expectation:
        return _EXPECTATION_ICONS.get(expectation, "circle-question-mark")
    entity = str(row.get("base_entity") or "")
    # A simulation row has no leading icon (``ListWidget``).
    return None if entity == "simulation" else _ROW_ICONS.get(entity, "monitor-smartphone")


def list_rows(native: dict, answer: dict, base_url: str = "") -> dict:
    """OpenAEV's list widget: its configured columns, typed as it renders them, a leading icon and a link per row."""
    entities = [e for e in answer.get("es_datas") or [] if isinstance(e, dict)] if isinstance(answer.get("es_datas"), list) else []
    configured = [c for c in native["widget_config"].get("columns") or [] if isinstance(c, str)]
    names = configured or ["base_representative"]
    columns = [(c, c.removeprefix("base_").replace("_", " ").capitalize(), _column_kind(c)) for c in names]
    rows = []
    for entity in entities:
        row: dict = {c: _list_cell(c, entity) for c in names}
        row.update(_url=_row_link(base_url, entity), _icon=_row_icon(entity), _entityType=entity.get("base_entity"))
        rows.append(row)
    return rows_data(columns, rows, answer.get("total"))


def _preset(native: dict) -> dict | None:
    """OpenAEV's look of a widget type as a visualization's options (``presentation``)."""
    widget_type = native.get("widget_type")
    if widget_type == "number":
        # An icon tile, a figure that counts up, a trend chip (``NumberWidget``, ``TrendChip``); the comparison
        # sentence behind an info sign in the title row (``WidgetTitle``: ``numberTooltipContent``).
        return {"view": "xtm.singlevalue", "options": {"valueStyle": "tile", "valueInfo": "header"}}
    if widget_type == "list":
        # The virtualised list: 50 px rows, a leading icon, typed cells, a trailing arrow, no header.
        return {"view": "xtm.list", "options": {"listStyle": "rows", "rowHeight": 50}}
    return outcome_presentation(native) or _chart_preset(native)



def presentation(native: dict) -> dict | None:
    """OpenAEV's look of a widget type as a visualization's options (``dashboard_live.presentation_of``), its
    title written as OpenAEV writes it: in letter-spaced capitals (``WidgetTitle``), and nothing to show said
    as it says it ("No data to display", ``Empty``)."""
    from app.services.dashboard_definition import VIEW_TO_TYPE
    from app.services.dashboard_platform import NativeError, check_native

    preset = _preset(native)
    if preset is None:
        try:
            view = VIEW_TO_TYPE.get(check_native("openaev", native).preferred_view or "")
        except NativeError:
            return None
        if view is None:
            return None
        preset = {"view": view, "options": {}}
    return {**preset, "options": {**preset["options"], "titleStyle": "caps", "emptyMessage": "display", "accentColor": "brand"}}

def normalize(native: dict, answer: object, base_url: str = "", *, time_range: str | None = None) -> dict:
    """The closed shape of an OpenAEV ad hoc answer (row links from *base_url*); a number says what it counts
    and over which span its change is measured (the *time_range* asked, else the widget's own), as OpenAEV's
    title tooltip does."""
    if native.get("widget_type") in TYPE_SHAPES:
        return _posture(native, answer, base_url=base_url)
    config_type = native["widget_config"]["widget_configuration_type"]
    if config_type == "flat":
        if not isinstance(answer, dict):
            raise LiveError("upstream_error")
        entity = _number_entity(native["widget_config"])
        name, color = _NUMBER_ICONS.get(entity, _NUMBER_ICON)
        span = _SPANS.get(str(time_range or native["widget_config"].get("time_range") or ""))
        return number_data(
            answer.get("interval_count"), answer.get("difference_count"),
            previous=answer.get("previous_interval_count"), period="interval", icon_name=name, icon_color=color,
            subject=entity, span=span,
        )  # fmt: skip
    if config_type in ("temporal-histogram", "structural-histogram"):
        series = answer if isinstance(answer, list) else []
        series = [s for s in series if isinstance(s, dict)]
        widget_type = native.get("widget_type")
        # The bars colour their series by meaning (a miss red, a detection teal...), as OpenAEV does - but horizontal
        # bars of one series, a colour per bar.
        by_meaning = widget_type == "vertical-barchart" or (
            widget_type == "horizontal-barchart" and len(native["widget_config"].get("series") or []) > 1
        )
        colors = _semantic_colors([s.get("label") for s in series]) if by_meaning else None
        if config_type == "structural-histogram":
            buckets = [b for b in (series[0].get("data") if series else None) or [] if isinstance(b, dict)]
            labels = [_bucket_label(b) for b in buckets]
            item_colors = _breakdown_colors(native, labels, colors[0] if colors else None)
            return distribution_data(
                [(label, b.get("value"), {"key": b.get("key"), "color": color}) for b, label, color in zip(buckets, labels, item_colors, strict=True)]
            )
        return series_data(
            [
                (
                    s.get("label") or "",
                    [(str(b.get("key") or b.get("label") or "")[:10], b.get("value"), b.get("key")) for b in s.get("data") or []
                     if isinstance(b, dict)],
                    {"color": colors[index] if colors else None},
                )
                for index, s in enumerate(series)
            ]
        )  # fmt: skip
    if not isinstance(answer, dict):
        raise LiveError("upstream_error")
    return list_rows(native, answer, base_url)


async def _read_list(client, kind: str, path: str, fields: tuple[str, ...], limit: int) -> list[dict]:
    """A list OpenAEV's posture widgets read from its store (``/domains``, ``/security_platforms``), kept
    an hour per credential, each row cut to *fields*."""
    from . import _cache_get, _cache_set

    key = _credential_key(client, kind)
    cached = await _cache_get(key)
    if cached is not None:
        return cached.get("rows") or []
    answer = await request_json("GET", f"{client.base_url}{_prefix(client)}{path}", client.headers, what="OpenAEV")
    rows = [{f: text(row.get(f), 160) for f in fields} for row in (answer if isinstance(answer, list) else [])[:limit] if isinstance(row, dict)]
    await _cache_set(key, {"rows": rows}, ttl=REFERENCE_SECONDS)
    return rows


async def read_exercise(client, simulation: str) -> dict:
    """A simulation's start and end, as an attack path draws them (``GET /exercises/{id}``)."""
    answer = await request_json("GET", f"{client.base_url}{_prefix(client)}/exercises/{quote(simulation, safe='')}", client.headers, what="OpenAEV")
    if not isinstance(answer, dict):
        raise LiveError("upstream_error")
    return {"exercise_start_date": answer.get("exercise_start_date"), "exercise_end_date": answer.get("exercise_end_date")}


async def fetch(client, request: LiveRequest) -> dict:
    native = request.native
    widget_type = native.get("widget_type")
    path_request = injects_request(native, client.tenant_id) if widget_type == "attack-path" else None
    if widget_type == "attack-path" and path_request is None:
        # As OpenAEV: an attack path is a simulation's, and asks for one.
        return outcomes_data([], needs_scope=True)
    path, body = build_request(
        native,
        range_days=request.range_days,
        tenant_id=client.tenant_id,
        range_key=request.range_key,
        range_start=request.range_start,
        range_end=request.range_end,
    )
    data = request_json("POST", f"{client.base_url}{path}", client.headers, json_body=body, what="OpenAEV")
    if widget_type in ("command-center", "average"):
        name, read = (
            ("platforms", _read_list(client, "platforms", "/security_platforms", ("asset_id", "asset_name", "security_platform_type"), 500))
            if widget_type == "command-center"
            else ("domains", _read_list(client, "domains", "/domains", ("domain_id", "domain_name"), 200))
        )
        answer, extra = await asyncio.gather(data, read, return_exceptions=True)
        if isinstance(answer, BaseException):
            raise answer
        # Without its platforms, the command center orbits its sample set; without the domains, the cards are those with data.
        return _posture(native, answer, base_url=client.base_url, context={} if isinstance(extra, BaseException) else {name: extra})
    if widget_type not in _TECHNIQUE_TYPES:
        return normalize(native, await data, client.base_url, time_range=body["widget_config"].get("time_range"))
    reads = [data, read_reference(client)]
    if path_request is not None:
        simulation = _filter_value(native["widget_config"], "base_simulation_side") or ""
        reads += [
            request_json("POST", f"{client.base_url}{path_request[0]}", client.headers, json_body=path_request[1], what="OpenAEV"),
            read_exercise(client, simulation),
        ]
    answer, reference, *rest = await asyncio.gather(*reads, return_exceptions=True)
    if isinstance(answer, BaseException):
        raise answer
    if path_request is not None:
        injects, exercise = rest
        for failed in (reference, injects):
            if isinstance(failed, BaseException):
                # Its techniques cannot be placed or linked: a failure, never a coverage passed off as the path.
                raise failed if isinstance(failed, LiveError) else LiveError("upstream_error")
        # Without the simulation's dates, the path is drawn without them.
        return attack_path(native, answer, reference, injects, base_url=client.base_url, exercise=None if isinstance(exercise, BaseException) else exercise)
    if isinstance(reference, BaseException):
        # Without the kill chains, the techniques tested are listed alone.
        return normalize(native, answer)
    return technique_matrix(native, answer, reference)


# ── Presentation (the security posture widgets) ──────────────────────────────

#: How OpenAEV draws each security posture widget: the engine's view, and the generic options
#: of its look (a widget with no data yet shows a greyed sample, desaturated on the command center).
OUTCOME_PRESETS: dict[str, tuple[str, dict]] = {
    # Its header always offers the fullscreen (``WidgetTitle``: ``isSecurityCoverage``).
    "security-coverage": ("xtm.outcomematrix", {"expandShown": True}),
    "attack-path": ("xtm.pathgraph", {}),
    "command-center": ("xtm.exposureconsole", {"sampleDisplay": "subtle"}),
    "exposure-score": ("xtm.scoregauge", {"sampleDisplay": "full"}),
    "resilience-gauge": ("xtm.ringgauge", {"sampleDisplay": "full"}),
    "average": ("xtm.domaincards", {}),
}


def outcome_presentation(native: dict) -> dict | None:
    """A security posture widget's preset, or ``None`` for another type."""
    preset = OUTCOME_PRESETS.get(native.get("widget_type"))
    return {"view": preset[0], "options": dict(preset[1])} if preset else None


def open_url(client, native: dict) -> str | None:
    return link(client.base_url, "/admin/workspaces/custom_dashboards")


__all__ = ["build_request", "fetch", "list_rows", "normalize", "open_url", "presentation"]
