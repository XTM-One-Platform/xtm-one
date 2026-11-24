# SPDX-License-Identifier: Apache-2.0

"""HarfangLab EDR (Hurukai) integration – Agentic SOC for endpoint security.

Full-fledged HarfangLab endpoint-security integration covering the
detection → investigation → response → intelligence loop, designed as a
best-in-class building block for an agentic security team. HarfangLab is
the leading French/EU sovereign EDR (first EDR certified by ANSSI).

Tool categories (21 tools):
  - Security events (alerts): list with severity/status/type filters, get
    full details, update status (new / investigating / false_positive /
    closed)
  - Threats: list, get details (top agents / rules), update status, add
    an analyst note
  - Endpoints / Agents: list/search, get details (incl. isolation state),
    network isolate / deisolate, policies inventory
  - Hunting / telemetry: search telemetry (processes, binary, network,
    dns, eventlog, authentications), hunt a process hash across running
    and executed processes
  - Threat Intelligence (IOCs): sources, search rules, add, delete
  - Investigation jobs: run artifact-collection / hunt jobs on an
    endpoint (process list, persistence, scheduled tasks, …), poll job
    status, fetch job results

Authentication is an **API token** (``Authorization: Token <token>``)
against the (self-hosted or cloud) HarfangLab manager URL. The integration
stores the manager base URL + API token; the compound ``base_url||api_token``
token is assembled by ``integration_tools._get_token`` and every call
targets ``{base_url}/api/...``.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from app.core.egress import egress_client
from app.integrations.http import (
    format_entity_list,
    parse_compound_token,
    safe_request,
)
from app.tools.base import CallableToolExecutor, ToolDefinition

_PROVIDER = "HarfangLab"
_DEFAULT_TIMEOUT = 30

# Alert / threat severity levels (``level`` filter, comma-separated).
_LEVELS: frozenset[str] = frozenset({"low", "medium", "high", "critical"})

# Security-event statuses (``status`` filter). Updates go through
# POST /api/data/alert/alert/Alert/tag/ with ``new_status``.
_EVENT_STATUSES: frozenset[str] = frozenset(
    {"new", "investigating", "probable_false_positive", "false_positive", "closed"}
)
_UPDATE_STATUSES: frozenset[str] = frozenset(
    {"new", "investigating", "false_positive", "closed"}
)

# IOC rule statuses accepted by POST /api/data/threat_intelligence/IOCRule/.
_IOC_STATUSES: frozenset[str] = frozenset({"enabled", "disabled", "testing"})
_IOC_TYPES: frozenset[str] = frozenset(
    {"hash", "filename", "filepath", "ip_src", "ip_dst", "ip_both", "domain_name", "url"}
)

_LEVEL_ICONS: dict[str, str] = {
    "critical": "🔴",
    "high": "🟠",
    "medium": "🟡",
    "low": "⚪",
}

# Investigation job catalog: friendly job type → (agent action, params,
# hunting result path). Results are fetched from the hunting endpoint
# filtered by ``job_id`` once the job is finished.
_HIVES_PARAMS = {"bSystemHives": True, "bUsersHives": True, "bWantSlowPlugins": False}
_HUNT_JOBS: dict[str, tuple[str, dict[str, Any], str]] = {
    "processes": ("getProcessList", {}, "/api/data/investigation/hunting/Process/"),
    "pipes": ("getPipeList", {}, "/api/data/investigation/hunting/Pipe/"),
    "drivers": ("getLoadedDriverList", {}, "/api/data/investigation/hunting/Driver/"),
    "prefetches": ("getPrefetch", {}, "/api/data/investigation/hunting/Prefetch/"),
    "scheduled_tasks": (
        "getScheduledTasks",
        {},
        "/api/data/investigation/hunting/ScheduledTaskXML/",
    ),
    "run_keys": (
        "getHives",
        _HIVES_PARAMS,
        "/api/data/investigation/hunting/RunKey/",
    ),
    "services": (
        "getHives",
        _HIVES_PARAMS,
        "/api/data/investigation/hunting/Service/",
    ),
    "startup_files": (
        "getStartupFileList",
        {},
        "/api/data/investigation/hunting/Startup/",
    ),
    "wmi": ("getWMI", {}, "/api/data/investigation/hunting/Wmi/"),
    "network_shares": (
        "getNetworkShare",
        {},
        "/api/data/investigation/hunting/NetworkShare/",
    ),
    "sessions": ("getSessions", {}, "/api/data/investigation/hunting/Session/"),
    "persistence": (
        "persistanceScanner",
        {},
        "/api/data/investigation/hunting/PersistanceFile/",
    ),
}

# Telemetry catalog: event type → (endpoint, extra filter param mapping).
_TELEMETRY_TYPES: dict[str, str] = {
    "processes": "/api/data/telemetry/Processes/",
    "binary": "/api/data/telemetry/Binary/",
    "network": "/api/data/telemetry/Network/",
    "dns": "/api/data/telemetry/DNSResolution/",
    "eventlog": "/api/data/telemetry/FullEventLog/",
    "windows_authentications": "/api/data/telemetry/authentication/AuthenticationWindows/",
    "linux_authentications": "/api/data/telemetry/authentication/AuthenticationLinux/",
    "macos_authentications": "/api/data/telemetry/authentication/AuthenticationMacos/",
}


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _trunc(text: Any, max_len: int = 300) -> str:
    if not text:
        return ""
    text = str(text)
    return text[:max_len] + ("…" if len(text) > max_len else "")


def _dt(value: Any) -> str:
    """Trim an ISO timestamp to second precision for display."""
    return str(value or "")[:19]


def _level_icon(level: str | None) -> str:
    return _LEVEL_ICONS.get((level or "").lower(), "⚪")


def _results(resp: dict | None) -> list:
    """Return the ``results`` list from a DRF-style response envelope."""
    if not isinstance(resp, dict):
        return []
    results = resp.get("results")
    return results if isinstance(results, list) else []


def _count(resp: dict | None) -> int | None:
    if not isinstance(resp, dict):
        return None
    count = resp.get("count")
    return count if isinstance(count, int) else None


def _offset_hint(resp: dict | None, offset: int, fetched: int) -> str:
    """Return an offset-pagination hint when more results exist."""
    total = _count(resp)
    if total is not None and offset + fetched < total:
        return (
            f"\n[More results available — {total} total, pass "
            f'offset={offset + fetched}]'
        )
    return ""


def _seg(value: Any) -> str:
    """Percent-encode a value used as a URL path segment.

    IDs come from LLM tool arguments — encoding ``/``, ``?``, ``#``, … keeps
    a crafted ID from escaping its path segment and hitting an unintended
    endpoint on the manager host (mirrors misp.py / virustotal `quote`).
    Accidental surrounding whitespace (copy/paste) is stripped first so a
    valid ID is not turned into a ``%20``-mangled 404.
    """
    return quote(str(value or "").strip(), safe="")


# Telemetry hash filter keys by hex-digest length (MD5 / SHA1 / SHA256).
_HASH_KEYS: dict[int, str] = {32: "hashes.md5", 40: "hashes.sha1", 64: "hashes.sha256"}


def _hash_param(value: str) -> str | None:
    """Map a hash to the HarfangLab telemetry filter key, or ``None`` when
    the value is not a valid MD5/SHA1/SHA256 hex digest."""
    value = (value or "").strip()
    if not value or any(c not in "0123456789abcdefABCDEF" for c in value):
        return None
    return _HASH_KEYS.get(len(value))


_INVALID_HASH_MSG = (
    "Error: invalid file_hash '{value}' — expected an MD5 (32), SHA1 (40), "
    "or SHA256 (64) hex digest."
)


def _fmt_generic(item: dict) -> str:
    """Compact one-line summary of an arbitrary hunting/telemetry record."""
    agent = item.get("agent") or {}
    parts: list[str] = []
    for key in (
        "process_name",
        "image_name",
        "service_name",
        "short_name",
        "requested_name",
        "filename",
        "name",
        "fullpath",
        "path",
        "log_name",
    ):
        if item.get(key):
            parts.append(str(item[key]))
            break
    for key in ("commandline", "arguments"):
        if item.get(key):
            parts.append(_trunc(item[key], 120))
            break
    if item.get("username"):
        parts.append(f"user: {item['username']}")
    if item.get("saddr") or item.get("daddr"):
        parts.append(
            f"{item.get('saddr', '?')}:{item.get('sport', '?')} → "
            f"{item.get('daddr', '?')}:{item.get('dport', '?')}"
        )
    if item.get("ip_addresses"):
        parts.append(f"resolved: {item['ip_addresses']}")
    hashes = item.get("hashes") or {}
    if isinstance(hashes, dict) and hashes.get("sha256"):
        parts.append(f"sha256: {hashes['sha256']}")
    if not parts:
        scalars = [
            f"{k}={_trunc(v, 60)}"
            for k, v in item.items()
            if isinstance(v, (str, int, float, bool)) and v not in ("", None)
        ]
        parts.append(", ".join(scalars[:5]) or "(empty record)")
    host = agent.get("hostname")
    prefix = f"[{host}] " if host else ""
    when = _dt(item.get("@event_create_date") or item.get("last_seen"))
    suffix = f" ({when})" if when else ""
    return f"  - {prefix}{' | '.join(parts)}{suffix}"


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build HarfangLab tools.

    ``token`` is the compound ``base_url||api_token`` assembled by
    ``integration_tools._get_token`` from the integration's stored manager
    URL and API token.
    """
    base_url, api_token = parse_compound_token(token, 2)
    # Strip pasted whitespace so the URL and the Authorization header stay
    # valid (mirrors the connectivity test's strip().rstrip("/")).
    base_url = (base_url or "").strip().rstrip("/")
    api_token = (api_token or "").strip()
    headers = {
        "Authorization": f"Token {api_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    async def _api(
        method: str,
        path: str,
        action: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        expected_statuses: set[int] | None = None,
        timeout: int = _DEFAULT_TIMEOUT,
    ) -> tuple[Any | None, str | None]:
        """Authenticated request helper returning ``(json, error)``.

        A non-2xx status listed in ``expected_statuses`` (e.g. a benign 404
        probe) is treated as a *miss*, not a failure: the helper returns
        ``(None, None)`` so callers never mistake it for a real error. Any
        other non-2xx (or a network error) returns ``(None, error_string)``.
        """
        if not base_url:
            return None, (
                "Error: HarfangLab manager URL is not configured. "
                "Edit the integration and set the manager base URL."
            )
        if not api_token:
            return None, (
                "Error: HarfangLab API token is not configured. Edit the "
                "integration and set the API token."
            )
        async with egress_client(timeout=timeout) as client:
            resp, err = await safe_request(
                client,
                method,
                f"{base_url}{path}",
                _PROVIDER,
                action,
                headers=headers,
                params=params,
                json=json,
                expected_statuses=expected_statuses,
            )
            if err:
                if (
                    expected_statuses
                    and resp is not None
                    and resp.status_code in expected_statuses
                ):
                    return None, None
                return None, err
            # An empty 2xx body is legitimate (204 deletes, the threat-note
            # probe) — treat it as {}. A NON-empty body that fails to parse
            # is a real failure (proxy/WAF/HTML error page) and must be
            # surfaced, not misreported as an empty result set.
            if not (resp.text or "").strip():
                return {}, None
            try:
                return resp.json(), None
            except Exception:
                return None, (
                    f"Error: HarfangLab returned a non-JSON response while "
                    f"trying to {action} — the manager may be unreachable, "
                    "behind a proxy, or degraded."
                )

    # ══════════════════════════════════════════════════════════════════════
    # 0. SECURITY EVENTS (ALERTS)
    # ══════════════════════════════════════════════════════════════════════

    async def list_harfanglab_security_events(
        status: str = "",
        alert_type: str = "",
        level: str = "",
        threat_id: str = "",
        count: int = 25,
        offset: int = 0,
    ) -> str:
        """List HarfangLab security events (alerts), most recent first.

        ``status``: new, investigating, probable_false_positive,
        false_positive, closed (comma-separated).
        ``level``: low, medium, high, critical (comma-separated).
        ``alert_type``: sigma, yara, hlai, vt, ransom, ioc, glimps, orion
        (comma-separated).
        """
        params: dict[str, Any] = {
            "limit": min(count, 100),
            "offset": max(offset, 0),
            "ordering": "-alert_time",
        }
        if status:
            statuses = {s.strip().lower() for s in status.split(",") if s.strip()}
            invalid = statuses - _EVENT_STATUSES
            if invalid:
                return (
                    f"Error: invalid status value(s) {', '.join(sorted(invalid))}. "
                    f"Use: {', '.join(sorted(_EVENT_STATUSES))}."
                )
            params["status"] = ",".join(sorted(statuses))
        if level:
            levels = {v.strip().lower() for v in level.split(",") if v.strip()}
            invalid = levels - _LEVELS
            if invalid:
                return (
                    f"Error: invalid level value(s) {', '.join(sorted(invalid))}. "
                    f"Use: {', '.join(sorted(_LEVELS))}."
                )
            params["level"] = ",".join(sorted(levels))
        if alert_type:
            params["alert_type"] = alert_type
        if threat_id:
            params["threat_key"] = threat_id

        resp, err = await _api(
            "get", "/api/data/alert/alert/Alert/", "list security events", params=params
        )
        if err:
            return err
        events = _results(resp)

        def _fmt(a: dict) -> str:
            agent = a.get("agent") or {}
            return (
                f"  {_level_icon(a.get('level'))} {a.get('rule_name') or 'Unknown rule'} "
                f"[{a.get('alert_type', 'N/A')}]\n"
                f"    id: {a.get('id', '?')} | Level: {a.get('level', 'N/A')} | "
                f"Status: {a.get('status', 'N/A')} | "
                f"Endpoint: {agent.get('hostname', 'N/A')}\n"
                f"    Time: {_dt(a.get('alert_time'))}"
            )

        return (
            format_entity_list(
                events, _fmt, noun="security event", total=_count(resp)
            )
            + _offset_hint(resp, params["offset"], len(events))
        )

    async def get_harfanglab_security_event(event_id: str) -> str:
        """Get full details of a HarfangLab security event (alert) by ID."""
        resp, err = await _api(
            "get",
            f"/api/data/alert/alert/Alert/{_seg(event_id)}/details/",
            f"get security event {event_id}",
        )
        if err:
            return err
        alert = resp.get("alert") if isinstance(resp, dict) else None
        if not isinstance(alert, dict):
            return f"Security event {event_id} not found."
        agent = alert.get("agent") or {}
        process = alert.get("process") or {}
        hashes = process.get("hashes") or {}
        lines = [
            f"Security event: {alert.get('rule_name') or 'Unknown rule'}",
            f"ID: {alert.get('id', event_id)}",
            f"Level: {alert.get('level', 'N/A')} | Status: {alert.get('status', 'N/A')} | "
            f"Type: {alert.get('alert_type', 'N/A')} | Maturity: {alert.get('maturity', 'N/A')}",
            f"Time: {_dt(alert.get('alert_time'))} | "
            f"Last update: {_dt(alert.get('last_update'))}",
            f"Endpoint: {agent.get('hostname', 'N/A')} "
            f"({agent.get('osproducttype') or agent.get('ostype', 'N/A')}) | "
            f"Agent ID: {agent.get('agentid', 'N/A')} | "
            f"Domain: {agent.get('domainname', 'N/A')}",
            f"Threat: {alert.get('threat_key') or 'N/A'}",
        ]
        if alert.get("msg"):
            lines.append(f"Message: {_trunc(alert.get('msg'), 400)}")
        if process:
            lines.append(
                f"Process: {process.get('image_name', 'N/A')} | "
                f"User: {process.get('username', 'N/A')}"
            )
            if process.get("commandline"):
                lines.append(f"Command line: {_trunc(process.get('commandline'), 300)}")
            if hashes.get("sha256"):
                lines.append(f"SHA256: {hashes['sha256']}")
        return "\n".join(lines)

    async def update_harfanglab_security_event_status(
        event_ids: str, status: str
    ) -> str:
        """Set the status of one or more HarfangLab security events.

        ``status`` is one of: new, investigating, false_positive, closed.
        """
        status = (status or "").strip().lower()
        if status not in _UPDATE_STATUSES:
            return (
                f"Error: invalid status '{status}'. Use one of: "
                f"{', '.join(sorted(_UPDATE_STATUSES))}."
            )
        ids = [i.strip() for i in (event_ids or "").split(",") if i.strip()]
        if not ids:
            return "Error: event_ids is required (comma-separated security event IDs)."
        _, err = await _api(
            "post",
            "/api/data/alert/alert/Alert/tag/",
            f"update status of {len(ids)} security event(s)",
            json={"ids": ids, "new_status": status},
        )
        if err:
            return err
        return (
            f"Status of {len(ids)} security event(s) updated to '{status}' "
            f"({', '.join(ids[:10])}{'…' if len(ids) > 10 else ''})."
        )

    # ══════════════════════════════════════════════════════════════════════
    # 1. THREATS
    # ══════════════════════════════════════════════════════════════════════

    async def list_harfanglab_threats(
        level: str = "",
        status: str = "",
        count: int = 25,
        offset: int = 0,
    ) -> str:
        """List HarfangLab threats (grouped security events), newest first.

        ``level``: low, medium, high, critical (comma-separated).
        ``status``: new, investigating, false_positive, closed
        (comma-separated).
        """
        params: dict[str, Any] = {
            "limit": min(count, 100),
            "offset": max(offset, 0),
            "ordering": "-creation_date",
        }
        if level:
            levels = {v.strip().lower() for v in level.split(",") if v.strip()}
            invalid = levels - _LEVELS
            if invalid:
                return (
                    f"Error: invalid level value(s) {', '.join(sorted(invalid))}. "
                    f"Use: {', '.join(sorted(_LEVELS))}."
                )
            params["level"] = ",".join(sorted(levels))
        if status:
            statuses = {s.strip().lower() for s in status.split(",") if s.strip()}
            invalid = statuses - _UPDATE_STATUSES
            if invalid:
                return (
                    f"Error: invalid status value(s) {', '.join(sorted(invalid))}. "
                    f"Use: {', '.join(sorted(_UPDATE_STATUSES))}."
                )
            params["status"] = ",".join(sorted(statuses))

        resp, err = await _api(
            "get", "/api/data/alert/alert/Threat/", "list threats", params=params
        )
        if err:
            return err
        threats = _results(resp)

        def _fmt(t: dict) -> str:
            return (
                f"  {_level_icon(t.get('level'))} {t.get('slug') or 'Unknown threat'}\n"
                f"    id: {t.get('id', '?')} | Level: {t.get('level', 'N/A')} | "
                f"Status: {t.get('status', 'N/A')} | "
                f"Events: {t.get('security_event_count', 'N/A')} | "
                f"Endpoints: {t.get('agent_count', 'N/A')}\n"
                f"    First seen: {_dt(t.get('first_seen'))} | "
                f"Last seen: {_dt(t.get('last_seen'))}"
            )

        return (
            format_entity_list(threats, _fmt, noun="threat", total=_count(resp))
            + _offset_hint(resp, params["offset"], len(threats))
        )

    async def get_harfanglab_threat(threat_id: str) -> str:
        """Get full details of a HarfangLab threat by ID, including the top
        impacted endpoints and matching rules."""
        resp, err = await _api(
            "get",
            f"/api/data/alert/alert/Threat/{_seg(threat_id)}/",
            f"get threat {threat_id}",
        )
        if err:
            return err
        if not isinstance(resp, dict) or not resp.get("id"):
            return f"Threat {threat_id} not found."
        lines = [
            f"Threat: {resp.get('slug') or 'Unknown threat'}",
            f"ID: {resp.get('id', threat_id)}",
            f"Level: {resp.get('level', 'N/A')} | Status: {resp.get('status', 'N/A')}",
            f"Security events: {resp.get('security_event_count', 'N/A')} | "
            f"Endpoints: {resp.get('agent_count', 'N/A')} | "
            f"Rules: {resp.get('rule_count', 'N/A')} | "
            f"Impacted users: {resp.get('impacted_user_count', 'N/A')}",
            f"Created: {_dt(resp.get('creation_date'))} | "
            f"First seen: {_dt(resp.get('first_seen'))} | "
            f"Last seen: {_dt(resp.get('last_seen'))}",
        ]
        top_agents = resp.get("top_agents") or []
        if top_agents:
            names = [str(a.get("agent_hostname", "?")) for a in top_agents[:10]]
            lines.append(f"Top endpoints: {', '.join(names)}")
        top_rules = resp.get("top_rules") or []
        if top_rules:
            names = [str(r.get("rule_name", "?")) for r in top_rules[:10]]
            lines.append(f"Top rules: {', '.join(names)}")
        if resp.get("note"):
            lines.append(f"Note: {_trunc(resp.get('note'), 300)}")
        return "\n".join(lines)

    async def update_harfanglab_threat_status(
        threat_id: str, status: str, update_security_events: bool = True
    ) -> str:
        """Set the status of a HarfangLab threat.

        ``status`` is one of: new, investigating, false_positive, closed.
        When ``update_security_events`` is true (default) the threat's
        security events are tagged with the same status.
        """
        threat_id = (threat_id or "").strip()
        if not threat_id:
            return "Error: threat_id is required (see list_harfanglab_threats)."
        status = (status or "").strip().lower()
        if status not in _UPDATE_STATUSES:
            return (
                f"Error: invalid status '{status}'. Use one of: "
                f"{', '.join(sorted(_UPDATE_STATUSES))}."
            )
        _, err = await _api(
            "patch",
            "/api/data/alert/alert/Threat/status/",
            f"update status of threat {threat_id}",
            json={
                "threat_ids": [threat_id],
                "new_status": status,
                "tag_security_events": bool(update_security_events),
                "update_by_query": False,
            },
        )
        if err:
            return err
        return f"Threat {threat_id} status updated to '{status}'."

    async def add_harfanglab_threat_note(threat_id: str, content: str) -> str:
        """Add or update the analyst note attached to a HarfangLab threat."""
        threat_id = (threat_id or "").strip()
        if not threat_id:
            return "Error: threat_id is required (see list_harfanglab_threats)."
        if not (content or "").strip():
            return "Error: note content is required."
        threat, err = await _api(
            "get",
            f"/api/data/alert/alert/Threat/{_seg(threat_id)}/",
            f"get threat {threat_id}",
        )
        if err:
            return err
        title = (threat or {}).get("slug") or f"Threat {threat_id}"
        # A 404 is the expected "no note yet" miss (returned as note=None,
        # note_err=None); any other failure is a real error and is surfaced.
        note, note_err = await _api(
            "get",
            f"/api/data/alert/alert/Threat/{_seg(threat_id)}/note/",
            f"get note of threat {threat_id}",
            expected_statuses={404},
        )
        if note_err:
            return note_err
        has_note = isinstance(note, dict) and bool(
            note.get("content") or note.get("title")
        )
        method = "patch" if has_note else "post"
        _, err = await _api(
            method,
            f"/api/data/alert/alert/Threat/{_seg(threat_id)}/note/",
            f"add note to threat {threat_id}",
            json={"title": title, "content": content},
        )
        if err:
            return err
        verb = "updated" if has_note else "added"
        return f"Note {verb} on threat {threat_id}."

    # ══════════════════════════════════════════════════════════════════════
    # 2. ENDPOINTS / AGENTS
    # ══════════════════════════════════════════════════════════════════════

    async def list_harfanglab_endpoints(
        hostname: str = "",
        search: str = "",
        count: int = 25,
        offset: int = 0,
    ) -> str:
        """List HarfangLab endpoints (EDR agents).

        Filter by exact ``hostname`` or a free-text ``search`` (hostname,
        domain, IP). Returns hostname, OS, agent version, status, and the
        agent ID needed for isolation and job tools.
        """
        params: dict[str, Any] = {
            "limit": min(count, 100),
            "offset": max(offset, 0),
            "ordering": "-lastseen",
        }
        if hostname:
            params["hostname"] = hostname
        if search:
            params["search"] = search

        resp, err = await _api(
            "get", "/api/data/endpoint/Agent/", "list endpoints", params=params
        )
        if err:
            return err
        agents = _results(resp)

        def _fmt(a: dict) -> str:
            return (
                f"  - {a.get('hostname', 'Unknown')} "
                f"({a.get('osproducttype') or a.get('ostype', 'N/A')})\n"
                f"    agent_id: {a.get('agentid') or a.get('id', '?')} | "
                f"Domain: {a.get('domainname', 'N/A')} | "
                f"Version: {a.get('version', 'N/A')} | "
                f"Status: {a.get('status', 'N/A')} | "
                f"Last seen: {_dt(a.get('lastseen'))}"
            )

        return (
            format_entity_list(agents, _fmt, noun="endpoint", total=_count(resp))
            + _offset_hint(resp, params["offset"], len(agents))
        )

    async def get_harfanglab_endpoint(agent_id: str) -> str:
        """Get full details of a HarfangLab endpoint (agent) by agent ID:
        OS, agent version, policy, groups, IP, isolation state, and
        timestamps."""
        resp, err = await _api(
            "get",
            f"/api/data/endpoint/Agent/{_seg(agent_id)}/",
            f"get endpoint {agent_id}",
        )
        if err:
            return err
        if not isinstance(resp, dict) or not (resp.get("agentid") or resp.get("id")):
            return f"Endpoint {agent_id} not found."
        policy = resp.get("policy") or {}
        groups = resp.get("groups") or []
        group_names = ", ".join(str(g.get("name", "?")) for g in groups) or "N/A"
        isolation = resp.get("isolation_state")
        if isolation is None:
            isolation = resp.get("isolated", "N/A")
        lines = [
            f"Endpoint: {resp.get('hostname', 'Unknown')}",
            f"Agent ID: {resp.get('agentid') or resp.get('id', agent_id)}",
            f"OS: {resp.get('osproducttype') or resp.get('ostype', 'N/A')} "
            f"{resp.get('osversion', '')}".rstrip(),
            f"Domain: {resp.get('domainname', 'N/A')} | "
            f"IP: {resp.get('ipaddress') or resp.get('ip', 'N/A')}",
            f"Agent version: {resp.get('version', 'N/A')} | "
            f"Status: {resp.get('status', 'N/A')} | Isolation: {isolation}",
            f"Policy: {policy.get('name', 'N/A')} | Groups: {group_names}",
            f"First seen: {_dt(resp.get('firstseen'))} | "
            f"Last seen: {_dt(resp.get('lastseen'))}",
        ]
        return "\n".join(lines)

    async def isolate_harfanglab_endpoint(agent_id: str) -> str:
        """Network-isolate a HarfangLab endpoint to contain an active threat.

        DESTRUCTIVE: cuts the machine off the network (it stays connected
        to the EDR manager only). Use deisolate_harfanglab_endpoint to
        restore connectivity.
        """
        # Normalized so the membership checks against the API's
        # requested / policy_not_allowed ID lists match.
        agent_id = (agent_id or "").strip()
        if not agent_id:
            return "Error: agent_id is required (see list_harfanglab_endpoints)."
        resp, err = await _api(
            "post",
            f"/api/data/endpoint/Agent/{_seg(agent_id)}/isolate/",
            f"isolate endpoint {agent_id}",
        )
        if err:
            return err
        requested = (resp or {}).get("requested") or []
        not_allowed = (resp or {}).get("policy_not_allowed") or []
        if agent_id in not_allowed:
            return (
                f"Isolation of endpoint {agent_id} was refused: the agent's "
                "policy does not allow isolation."
            )
        if agent_id in requested:
            return f"Isolation of endpoint {agent_id} successfully requested."
        return (
            f"Isolation request for endpoint {agent_id} returned no "
            f"confirmation (requested: {requested or 'none'})."
        )

    async def deisolate_harfanglab_endpoint(agent_id: str) -> str:
        """Remove network isolation from a HarfangLab endpoint and reconnect
        it to the network."""
        agent_id = (agent_id or "").strip()
        if not agent_id:
            return "Error: agent_id is required (see list_harfanglab_endpoints)."
        resp, err = await _api(
            "post",
            f"/api/data/endpoint/Agent/{_seg(agent_id)}/deisolate/",
            f"deisolate endpoint {agent_id}",
        )
        if err:
            return err
        requested = (resp or {}).get("requested") or []
        if agent_id in requested:
            return f"Deisolation of endpoint {agent_id} successfully requested."
        return (
            f"Deisolation request for endpoint {agent_id} returned no "
            f"confirmation (requested: {requested or 'none'})."
        )

    async def list_harfanglab_policies(search: str = "", count: int = 25) -> str:
        """List HarfangLab agent policies (protection configurations)."""
        params: dict[str, Any] = {"limit": min(count, 100)}
        if search:
            params["search"] = search
        resp, err = await _api(
            "get", "/api/data/endpoint/Policy/", "list policies", params=params
        )
        if err:
            return err
        policies = _results(resp)
        if not policies:
            return "No policies found."

        def _fmt(p: dict) -> str:
            return (
                f"  - {p.get('name', 'Unknown')} (id: {p.get('id', '?')}) | "
                f"Agents: {p.get('agent_count', 'N/A')}"
            )

        # format_entity_list only appends "s" — "policy" needs "policies".
        total = _count(resp)
        total = total if total is not None else len(policies)
        header = f"Found {total} {'policy' if total == 1 else 'policies'}:"
        return "\n".join([header, *(_fmt(p) for p in policies)])

    # ══════════════════════════════════════════════════════════════════════
    # 3. HUNTING / TELEMETRY
    # ══════════════════════════════════════════════════════════════════════

    async def search_harfanglab_telemetry(
        event_type: str,
        hostname: str = "",
        process_name: str = "",
        file_hash: str = "",
        requested_name: str = "",
        destination_address: str = "",
        from_date: str = "",
        to_date: str = "",
        count: int = 25,
    ) -> str:
        """Search HarfangLab telemetry (historical endpoint activity).

        ``event_type``: processes, binary, network, dns, eventlog,
        windows_authentications, linux_authentications,
        macos_authentications.
        Optional filters: ``hostname``, ``process_name`` (processes),
        ``file_hash`` (MD5/SHA1/SHA256, processes/binary), ``requested_name``
        (dns), ``destination_address`` (network), and ISO-8601 ``from_date``
        / ``to_date`` bounds.
        """
        event_type = (event_type or "").strip().lower()
        path = _TELEMETRY_TYPES.get(event_type)
        if not path:
            return (
                f"Error: invalid event_type '{event_type}'. Use one of: "
                f"{', '.join(sorted(_TELEMETRY_TYPES))}."
            )
        params: dict[str, Any] = {"limit": min(count, 100)}
        if hostname:
            params["agent.hostname"] = hostname
        if process_name:
            params["process_name"] = process_name
        if file_hash:
            file_hash = file_hash.strip()
            hash_key = _hash_param(file_hash)
            if not hash_key:
                return _INVALID_HASH_MSG.format(value=file_hash)
            params[hash_key] = file_hash
        if requested_name:
            params["requested_name"] = requested_name
        if destination_address:
            params["daddr"] = destination_address
        if from_date:
            params["@event_create_date__gte"] = from_date
        if to_date:
            params["@event_create_date__lte"] = to_date

        resp, err = await _api(
            "get", path, f"search {event_type} telemetry", params=params
        )
        if err:
            return err
        events = _results(resp)
        return format_entity_list(
            events,
            _fmt_generic,
            noun=f"{event_type} event",
            total=_count(resp),
            empty_msg=f"No {event_type} telemetry events matched the filters.",
        )

    async def hunt_harfanglab_process_hash(file_hash: str, count: int = 25) -> str:
        """Hunt a binary hash (MD5/SHA1/SHA256) across the fleet: finds
        currently RUNNING processes (hunting data) and previously EXECUTED
        processes (telemetry). Use to scope how widespread a malicious
        binary is before responding."""
        file_hash = (file_hash or "").strip()
        if not file_hash:
            return "Error: file_hash is required (MD5, SHA1, or SHA256)."
        hash_key = _hash_param(file_hash)
        if not hash_key:
            return _INVALID_HASH_MSG.format(value=file_hash)
        limit = min(count, 100)
        lines: list[str] = []

        running, err = await _api(
            "get",
            "/api/data/investigation/hunting/Process/",
            "hunt running processes",
            params={"binaryinfo.binaryinfo.sha256": file_hash, "limit": limit}
            if len(file_hash) == 64
            else {"search": file_hash, "limit": limit},
        )
        if err:
            lines.append(f"Running processes: {err}")
        else:
            items = _results(running)
            total = _count(running)
            lines.append(
                f"Running processes matching the hash: "
                f"{total if total is not None else len(items)}"
            )
            lines.extend(_fmt_generic(i) for i in items[:limit])

        executed, err = await _api(
            "get",
            "/api/data/telemetry/Processes/",
            "hunt executed processes",
            params={hash_key: file_hash, "limit": limit},
        )
        if err:
            lines.append(f"Executed processes: {err}")
        else:
            items = _results(executed)
            total = _count(executed)
            lines.append(
                f"\nExecuted processes matching the hash: "
                f"{total if total is not None else len(items)}"
            )
            lines.extend(_fmt_generic(i) for i in items[:limit])

        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 4. THREAT INTELLIGENCE — IOCs
    # ══════════════════════════════════════════════════════════════════════

    async def list_harfanglab_ioc_sources(search: str = "", count: int = 25) -> str:
        """List HarfangLab IOC sources (containers for IOC rules). Use the
        source id when adding IOCs with add_harfanglab_ioc."""
        params: dict[str, Any] = {"limit": min(count, 100)}
        if search:
            params["search"] = search
        resp, err = await _api(
            "get",
            "/api/data/threat_intelligence/IOCSource/",
            "list IOC sources",
            params=params,
        )
        if err:
            return err
        sources = _results(resp)

        def _fmt(s: dict) -> str:
            return (
                f"  - {s.get('name', 'Unknown')} (id: {s.get('id', '?')}) | "
                f"IOCs: {s.get('ioc_count', 'N/A')} | "
                f"Description: {_trunc(s.get('description'), 100) or 'N/A'}"
            )

        return format_entity_list(sources, _fmt, noun="IOC source", total=_count(resp))

    async def search_harfanglab_iocs(
        search: str = "", source_id: str = "", count: int = 25
    ) -> str:
        """Search IOC rules in the HarfangLab threat intelligence database
        by value substring, optionally scoped to a source."""
        params: dict[str, Any] = {"limit": min(count, 100)}
        if search:
            params["search"] = search
        if source_id:
            params["source_id"] = source_id
        resp, err = await _api(
            "get",
            "/api/data/threat_intelligence/IOCRule/",
            "search IOC rules",
            params=params,
        )
        if err:
            return err
        iocs = _results(resp)

        def _fmt(i: dict) -> str:
            return (
                f"  - {_trunc(i.get('value'), 120)} [{i.get('type', 'N/A')}]\n"
                f"    id: {i.get('id', '?')} | Status: {i.get('hl_status', 'N/A')} | "
                f"Source: {i.get('source_id', 'N/A')} | "
                f"Comment: {_trunc(i.get('comment'), 100) or 'N/A'}"
            )

        return format_entity_list(iocs, _fmt, noun="IOC rule", total=_count(resp))

    async def add_harfanglab_ioc(
        value: str,
        ioc_type: str,
        source_id: str,
        status: str = "enabled",
        comment: str = "",
    ) -> str:
        """Add an IOC rule to a HarfangLab IOC source so the EDR detects
        (or blocks, depending on the source configuration) the indicator.

        ``ioc_type``: hash, filename, filepath, ip_src, ip_dst, ip_both,
        domain_name, url. ``status``: enabled, disabled, testing. Find the
        ``source_id`` with list_harfanglab_ioc_sources.
        """
        ioc_type = (ioc_type or "").strip().lower()
        if ioc_type not in _IOC_TYPES:
            return (
                f"Error: invalid ioc_type '{ioc_type}'. Use one of: "
                f"{', '.join(sorted(_IOC_TYPES))}."
            )
        status = (status or "enabled").strip().lower()
        if status not in _IOC_STATUSES:
            return (
                f"Error: invalid status '{status}'. Use one of: "
                f"{', '.join(sorted(_IOC_STATUSES))}."
            )
        # Normalize once and use the stripped values everywhere below —
        # otherwise stray whitespace bypasses the duplicate guard and sends
        # a different IOC value than intended.
        value = (value or "").strip()
        source_id = (source_id or "").strip()
        if not value:
            return "Error: IOC value is required."
        if not source_id:
            return (
                "Error: source_id is required — list sources with "
                "list_harfanglab_ioc_sources first."
            )
        # Duplicate guard: adding the same value twice fails server-side with
        # an opaque error, so surface a friendly message instead.
        existing, err = await _api(
            "get",
            "/api/data/threat_intelligence/IOCRule/",
            "check existing IOC",
            params={"source_id": source_id, "search": value},
        )
        if not err and any(i.get("value") == value for i in _results(existing)):
            return f"IOC '{value}' already exists in source {source_id}."
        body = {
            "type": ioc_type,
            "value": value,
            "comment": comment,
            "source_id": source_id,
            "hl_status": status,
            "hl_local_testing_status": "in_progress" if status == "testing" else None,
        }
        _, err = await _api(
            "post",
            "/api/data/threat_intelligence/IOCRule/",
            f"add IOC {value}",
            json=body,
        )
        if err:
            return err
        return (
            f"IOC '{value}' ({ioc_type}) added to source {source_id} "
            f"with status '{status}'."
        )

    async def delete_harfanglab_ioc(ioc_id: str) -> str:
        """Delete an IOC rule from the HarfangLab threat intelligence
        database by its rule ID. Permanent — cannot be undone."""
        ioc_id = (ioc_id or "").strip()
        if not ioc_id:
            return "Error: ioc_id is required (see search_harfanglab_iocs)."
        _, err = await _api(
            "delete",
            f"/api/data/threat_intelligence/IOCRule/{_seg(ioc_id)}/",
            f"delete IOC rule {ioc_id}",
        )
        if err:
            return err
        return f"IOC rule {ioc_id} deleted."

    # ══════════════════════════════════════════════════════════════════════
    # 5. INVESTIGATION JOBS
    # ══════════════════════════════════════════════════════════════════════

    async def run_harfanglab_hunt_job(agent_id: str, job_type: str) -> str:
        """Trigger an investigation/artifact-collection job on a HarfangLab
        endpoint. The job runs asynchronously on the agent — poll with
        get_harfanglab_job_status, then fetch results with
        get_harfanglab_job_results.

        ``job_type``: processes, pipes, drivers, prefetches, scheduled_tasks,
        run_keys, services, startup_files, wmi, network_shares, sessions,
        persistence.
        """
        job_type = (job_type or "").strip().lower()
        entry = _HUNT_JOBS.get(job_type)
        if not entry:
            return (
                f"Error: invalid job_type '{job_type}'. Use one of: "
                f"{', '.join(sorted(_HUNT_JOBS))}."
            )
        agent_id = (agent_id or "").strip()
        if not agent_id:
            return "Error: agent_id is required (see list_harfanglab_endpoints)."
        action, job_params, _result_path = entry
        resp, err = await _api(
            "post",
            "/api/data/Job/",
            f"run {job_type} job on endpoint {agent_id}",
            json={
                "targets": {"agents": [agent_id]},
                "actions": [{"value": action, "params": job_params}],
            },
        )
        if err:
            return err
        job_id = None
        if isinstance(resp, list) and resp and isinstance(resp[0], dict):
            job_id = resp[0].get("id")
        elif isinstance(resp, dict):
            job_id = resp.get("id")
        if not job_id:
            return (
                f"Job '{job_type}' submitted to endpoint {agent_id}, but no job "
                "ID was returned — check the manager console."
            )
        return (
            f"Job '{job_type}' (action {action}) started on endpoint {agent_id}.\n"
            f"job_id: {job_id}\n"
            f"Poll with get_harfanglab_job_status, then fetch results with "
            f"get_harfanglab_job_results(job_id, job_type='{job_type}')."
        )

    async def get_harfanglab_job_status(job_id: str) -> str:
        """Get the status of a HarfangLab investigation job (running,
        finished, error, canceled, waiting)."""
        job_id = (job_id or "").strip()
        if not job_id:
            return "Error: job_id is required (see run_harfanglab_hunt_job)."
        resp, err = await _api(
            "get", f"/api/data/Job/{_seg(job_id)}", f"get job {job_id} status"
        )
        if err:
            return err
        if not isinstance(resp, dict) or "instance" not in resp:
            # A DRF not-found body ({"detail": "Not found."}) or an empty
            # response has no counters — never report it as finished.
            return f"Job {job_id} not found."
        instances = resp.get("instance") or 0
        done = resp.get("done") or 0
        # Errors / cancellations take precedence: an errored instance also
        # counts as processed, so done == instance alone must not win.
        if (resp.get("error") or 0) > 0:
            status = "error"
        elif (resp.get("canceled") or 0) > 0:
            status = "canceled"
        elif instances > 0 and done >= instances:
            status = "finished"
        elif (resp.get("waiting") or 0) > 0:
            status = "waiting"
        else:
            status = "running"
        return (
            f"Job {job_id}: {status}\n"
            f"Instances: {instances} | Done: {done} | "
            f"Running: {resp.get('running', 0)} | Waiting: {resp.get('waiting', 0)} | "
            f"Errors: {resp.get('error', 0)} | Canceled: {resp.get('canceled', 0)}\n"
            f"Created: {_dt(resp.get('creationtime'))}"
        )

    async def get_harfanglab_job_results(
        job_id: str, job_type: str, count: int = 50
    ) -> str:
        """Fetch the results of a finished HarfangLab investigation job.

        ``job_type`` must match the type passed to run_harfanglab_hunt_job
        (processes, pipes, drivers, prefetches, scheduled_tasks, run_keys,
        services, startup_files, wmi, network_shares, sessions, persistence).
        """
        job_id = (job_id or "").strip()
        if not job_id:
            return "Error: job_id is required (see run_harfanglab_hunt_job)."
        job_type = (job_type or "").strip().lower()
        entry = _HUNT_JOBS.get(job_type)
        if not entry:
            return (
                f"Error: invalid job_type '{job_type}'. Use one of: "
                f"{', '.join(sorted(_HUNT_JOBS))}."
            )
        _action, _params, result_path = entry
        resp, err = await _api(
            "get",
            result_path,
            f"get {job_type} results of job {job_id}",
            params={"job_id": job_id, "limit": min(count, 200)},
        )
        if err:
            return err
        items = _results(resp)
        return format_entity_list(
            items,
            _fmt_generic,
            noun=f"{job_type} result",
            total=_count(resp),
            empty_msg=(
                f"No {job_type} results for job {job_id} yet — the job may "
                "still be running (check get_harfanglab_job_status)."
            ),
        )

    # ══════════════════════════════════════════════════════════════════════
    # Registration
    # ══════════════════════════════════════════════════════════════════════

    executor.register(
        "list_harfanglab_security_events", list_harfanglab_security_events
    )
    executor.register("get_harfanglab_security_event", get_harfanglab_security_event)
    executor.register(
        "update_harfanglab_security_event_status",
        update_harfanglab_security_event_status,
    )
    executor.register("list_harfanglab_threats", list_harfanglab_threats)
    executor.register("get_harfanglab_threat", get_harfanglab_threat)
    executor.register("update_harfanglab_threat_status", update_harfanglab_threat_status)
    executor.register("add_harfanglab_threat_note", add_harfanglab_threat_note)
    executor.register("list_harfanglab_endpoints", list_harfanglab_endpoints)
    executor.register("get_harfanglab_endpoint", get_harfanglab_endpoint)
    executor.register("isolate_harfanglab_endpoint", isolate_harfanglab_endpoint)
    executor.register("deisolate_harfanglab_endpoint", deisolate_harfanglab_endpoint)
    executor.register("list_harfanglab_policies", list_harfanglab_policies)
    executor.register("search_harfanglab_telemetry", search_harfanglab_telemetry)
    executor.register("hunt_harfanglab_process_hash", hunt_harfanglab_process_hash)
    executor.register("list_harfanglab_ioc_sources", list_harfanglab_ioc_sources)
    executor.register("search_harfanglab_iocs", search_harfanglab_iocs)
    executor.register("add_harfanglab_ioc", add_harfanglab_ioc)
    executor.register("delete_harfanglab_ioc", delete_harfanglab_ioc)
    executor.register("run_harfanglab_hunt_job", run_harfanglab_hunt_job)
    executor.register("get_harfanglab_job_status", get_harfanglab_job_status)
    executor.register("get_harfanglab_job_results", get_harfanglab_job_results)

    src = "integration:harfanglab"
    return [
        # ── 0. Security events (alerts) ───────────────────────────────────
        ToolDefinition(
            name="list_harfanglab_security_events",
            description=(
                "List HarfangLab EDR security events (alerts), most recent "
                "first. Filter by status (new, investigating, "
                "probable_false_positive, false_positive, closed), level "
                "(low, medium, high, critical), alert_type (sigma, yara, "
                "hlai, vt, ransom, ioc, glimps, orion), or threat_id. All "
                "filters accept comma-separated values."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                    "alert_type": {"type": "string"},
                    "level": {"type": "string"},
                    "threat_id": {"type": "string"},
                    "count": {"type": "integer", "default": 25},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": [],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="get_harfanglab_security_event",
            description=(
                "Get full details of a HarfangLab security event (alert) by "
                "ID: rule, level, status, endpoint, process (command line, "
                "user, SHA256), and timestamps."
            ),
            input_schema={
                "type": "object",
                "properties": {"event_id": {"type": "string"}},
                "required": ["event_id"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="update_harfanglab_security_event_status",
            description=(
                "Set the status of one or more HarfangLab security events. "
                "event_ids is comma-separated. status: new, investigating, "
                "false_positive, closed."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "event_ids": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["new", "investigating", "false_positive", "closed"],
                    },
                },
                "required": ["event_ids", "status"],
            },
            source=src,
            priority=1,
        ),
        # ── 1. Threats ────────────────────────────────────────────────────
        ToolDefinition(
            name="list_harfanglab_threats",
            description=(
                "List HarfangLab threats (correlated groups of security "
                "events), newest first. Filter by level (low, medium, high, "
                "critical) or status (new, investigating, false_positive, "
                "closed). Both filters accept comma-separated values."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "level": {"type": "string"},
                    "status": {"type": "string"},
                    "count": {"type": "integer", "default": 25},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": [],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="get_harfanglab_threat",
            description=(
                "Get full details of a HarfangLab threat by ID: level, "
                "status, event/endpoint/rule counts, top impacted endpoints, "
                "top matching rules, and the analyst note."
            ),
            input_schema={
                "type": "object",
                "properties": {"threat_id": {"type": "string"}},
                "required": ["threat_id"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="update_harfanglab_threat_status",
            description=(
                "Set the status of a HarfangLab threat (new, investigating, "
                "false_positive, closed). By default the threat's security "
                "events are tagged with the same status."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "threat_id": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["new", "investigating", "false_positive", "closed"],
                    },
                    "update_security_events": {"type": "boolean", "default": True},
                },
                "required": ["threat_id", "status"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="add_harfanglab_threat_note",
            description=(
                "Add or update the analyst note attached to a HarfangLab "
                "threat (investigation summary, findings, decisions)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "threat_id": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["threat_id", "content"],
            },
            source=src,
            priority=2,
        ),
        # ── 2. Endpoints / Agents ─────────────────────────────────────────
        ToolDefinition(
            name="list_harfanglab_endpoints",
            description=(
                "List HarfangLab endpoints (EDR agents). Filter by exact "
                "hostname or free-text search. Returns hostname, OS, agent "
                "version, status, and the agent_id needed for isolation and "
                "investigation jobs."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "hostname": {"type": "string"},
                    "search": {"type": "string"},
                    "count": {"type": "integer", "default": 25},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": [],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="get_harfanglab_endpoint",
            description=(
                "Get full details of a HarfangLab endpoint (agent) by agent "
                "ID: OS, agent version, policy, groups, IP, isolation state, "
                "and first/last seen timestamps."
            ),
            input_schema={
                "type": "object",
                "properties": {"agent_id": {"type": "string"}},
                "required": ["agent_id"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="isolate_harfanglab_endpoint",
            description=(
                "Network-isolate a HarfangLab endpoint to contain an active "
                "threat. DESTRUCTIVE: cuts the machine off the network (it "
                "remains connected to the EDR manager only). The agent's "
                "policy must allow isolation. Reverse with "
                "deisolate_harfanglab_endpoint."
            ),
            input_schema={
                "type": "object",
                "properties": {"agent_id": {"type": "string"}},
                "required": ["agent_id"],
            },
            source=src,
            # Same tier as deisolate so truncation can never keep the
            # destructive isolate while dropping its undo.
            priority=2,
        ),
        ToolDefinition(
            name="deisolate_harfanglab_endpoint",
            description=(
                "Remove network isolation from a HarfangLab endpoint and "
                "reconnect it to the network."
            ),
            input_schema={
                "type": "object",
                "properties": {"agent_id": {"type": "string"}},
                "required": ["agent_id"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="list_harfanglab_policies",
            description=(
                "List HarfangLab agent policies (protection configurations) "
                "with their IDs and agent counts."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search": {"type": "string"},
                    "count": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=src,
            priority=3,
        ),
        # ── 3. Hunting / telemetry ────────────────────────────────────────
        ToolDefinition(
            name="search_harfanglab_telemetry",
            description=(
                "Search HarfangLab telemetry (historical endpoint activity). "
                "event_type: processes, binary, network, dns, eventlog, "
                "windows_authentications, linux_authentications, "
                "macos_authentications. Optional filters: hostname, "
                "process_name (processes), file_hash (MD5/SHA1/SHA256), "
                "requested_name (dns), destination_address (network), and "
                "ISO-8601 from_date/to_date."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "event_type": {
                        "type": "string",
                        "enum": sorted(_TELEMETRY_TYPES),
                    },
                    "hostname": {"type": "string"},
                    "process_name": {"type": "string"},
                    "file_hash": {"type": "string"},
                    "requested_name": {"type": "string"},
                    "destination_address": {"type": "string"},
                    "from_date": {"type": "string"},
                    "to_date": {"type": "string"},
                    "count": {"type": "integer", "default": 25},
                },
                "required": ["event_type"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="hunt_harfanglab_process_hash",
            description=(
                "Hunt a binary hash (MD5/SHA1/SHA256) across the fleet: "
                "returns currently RUNNING processes and previously EXECUTED "
                "processes matching the hash. Use to scope how widespread a "
                "malicious binary is before responding."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "file_hash": {"type": "string"},
                    "count": {"type": "integer", "default": 25},
                },
                "required": ["file_hash"],
            },
            source=src,
            priority=2,
            # "hunt" is no verb the name classifier reads; the hunt is two GETs on hunting and telemetry data.
            read_only=True,
        ),
        # ── 4. Threat Intelligence IOCs ───────────────────────────────────
        ToolDefinition(
            name="list_harfanglab_ioc_sources",
            description=(
                "List HarfangLab IOC sources (containers for IOC rules). "
                "Use the returned source id when adding IOCs with "
                "add_harfanglab_ioc."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search": {"type": "string"},
                    "count": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="search_harfanglab_iocs",
            description=(
                "Search IOC rules in the HarfangLab threat intelligence "
                "database by value substring, optionally scoped to a source."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search": {"type": "string"},
                    "source_id": {"type": "string"},
                    "count": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="add_harfanglab_ioc",
            description=(
                "Add an IOC rule to a HarfangLab IOC source so the EDR "
                "detects or blocks the indicator. ioc_type: hash, filename, "
                "filepath, ip_src, ip_dst, ip_both, domain_name, url. "
                "status: enabled, disabled, testing. Find source_id with "
                "list_harfanglab_ioc_sources."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "value": {"type": "string"},
                    "ioc_type": {"type": "string", "enum": sorted(_IOC_TYPES)},
                    "source_id": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": sorted(_IOC_STATUSES),
                        "default": "enabled",
                    },
                    "comment": {"type": "string"},
                },
                "required": ["value", "ioc_type", "source_id"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="delete_harfanglab_ioc",
            description=(
                "Delete an IOC rule from the HarfangLab threat intelligence "
                "database by rule ID. Permanent — cannot be undone."
            ),
            input_schema={
                "type": "object",
                "properties": {"ioc_id": {"type": "string"}},
                "required": ["ioc_id"],
            },
            source=src,
            priority=3,
        ),
        # ── 5. Investigation jobs ─────────────────────────────────────────
        ToolDefinition(
            name="run_harfanglab_hunt_job",
            description=(
                "Trigger an investigation/artifact-collection job on a "
                "HarfangLab endpoint (runs asynchronously on the agent). "
                "job_type: processes, pipes, drivers, prefetches, "
                "scheduled_tasks, run_keys, services, startup_files, wmi, "
                "network_shares, sessions, persistence. Poll with "
                "get_harfanglab_job_status, then fetch results with "
                "get_harfanglab_job_results."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string"},
                    "job_type": {"type": "string", "enum": sorted(_HUNT_JOBS)},
                },
                "required": ["agent_id", "job_type"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="get_harfanglab_job_status",
            # Polled: the same call later answers a state that moved on its own.
            live_state=True,
            description=(
                "Get the status of a HarfangLab investigation job (running, "
                "finished, error, canceled, waiting) with instance counters."
            ),
            input_schema={
                "type": "object",
                "properties": {"job_id": {"type": "string"}},
                "required": ["job_id"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="get_harfanglab_job_results",
            description=(
                "Fetch the results of a finished HarfangLab investigation "
                "job. job_type must match the type passed to "
                "run_harfanglab_hunt_job."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "job_type": {"type": "string", "enum": sorted(_HUNT_JOBS)},
                    "count": {"type": "integer", "default": 50},
                },
                "required": ["job_id", "job_type"],
            },
            source=src,
            priority=2,
        ),
    ]
