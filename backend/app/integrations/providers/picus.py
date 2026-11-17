# SPDX-License-Identifier: Apache-2.0

"""Picus Security integration - Agentic Breach and Attack Simulation.

Full-fledged Picus Security (Breach and Attack Simulation / Security
Validation) integration covering the validate -> assess -> analyze ->
mitigate loop, designed as a best-in-class building block for an agentic
security-validation team.

Tool categories (24 tools):
  - Simulations & results: list, detail (+ run history), run result
    (prevention + detection analytics), threat/objective/action results,
    account-wide posture summary
  - Simulation lifecycle: create + schedule, re-run (simulate now), and a
    consolidated manage tool (update / cancel / delete)
  - Threat library: threats list/detail, attack actions list/detail, threat
    actors
  - Templates: list + detail (the building blocks of a simulation)
  - Agents: list + detail (the endpoints that run simulations)
  - Integrations: detection integrations (SIEM/EDR) + health
  - Mitigations: prevention devices + per-device block stats, vendor
    signatures for actions, not-blocked-action prevention recommendations,
    and SIEM detection-content rules (detection recommendations)
  - Admin (consolidated read): detection sources / log sources, detection
    rule detail, integration agents, activity logs, tags, users, roles

Authentication is a **refresh-token / access-token OAuth2 exchange**. The
integration stores the customer's Picus manager base URL + a long-lived
refresh token; the compound ``base_url||refresh_token`` token is assembled by
``integration_tools._get_token``. The refresh token is exchanged for a
short-lived (~2 h) bearer access token via ``POST /v1/auth/token``, cached
in-process (keyed by a hash of base URL + refresh token) so repeated tool
calls within an agentic loop reuse a single access token (mirrors the
Defender / Sentinel app-only token cache).
"""

from __future__ import annotations

import logging
import re
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from app.core.egress import egress_client
from app.core.utils import format_exception_message
from app.integrations.http import (
    app_token_cache_key,
    bearer_auth_headers,
    format_entity_list,
    get_cached_app_token,
    parse_compound_token,
    safe_request,
    set_cached_app_token,
)
from app.tools.base import CallableToolExecutor, ToolDefinition

logger = logging.getLogger(__name__)

_PROVIDER = "Picus Security"
_DEFAULT_BASE_URL = "https://api.picussecurity.com"
_DEFAULT_TIMEOUT = 30

_AUTH_ERROR = (
    "Error: Failed to authenticate with Picus Security. Check that the manager "
    "base URL is correct and that the refresh token is valid and not expired "
    "(refresh tokens live ~6 months and are generated in the Picus app under "
    "Settings > Rest API Token). Also confirm the token's scope covers this "
    "operation."
)

_SEVERITY_ICONS: dict[str, str] = {
    "critical": "\U0001f534",
    "high": "\U0001f534",
    "medium": "\U0001f7e0",
    "low": "\U0001f7e1",
    "informational": "\U0001f535",
    "info": "\U0001f535",
}

# manage_picus_simulation actions.
_SIMULATION_ACTIONS: frozenset[str] = frozenset({"update", "cancel", "delete"})

# manage_picus_admin read actions.
_ADMIN_ACTIONS: frozenset[str] = frozenset(
    {
        "list_integration_agents",
        "list_detection_sources",
        "list_detection_log_sources",
        "get_detection_rule",
        "list_activity_logs",
        "list_tags",
        "list_users",
        "list_roles",
    }
)


# ---------------------------------------------------------------------------
# Base URL + auth helpers
# ---------------------------------------------------------------------------


def resolve_base_url(base_url: str) -> str:
    """Normalise a Picus manager base URL.

    Accepts the SaaS default host or a self-hosted / regional Picus manager
    URL. An empty value falls back to the SaaS API host. A raw ``http://`` URL
    is upgraded to ``https://`` because the token exchange carries the refresh
    token and must never be sent in cleartext. The trailing slash is stripped.
    """
    value = (base_url or "").strip()
    if not value:
        return _DEFAULT_BASE_URL
    lowered = value.lower()
    if lowered.startswith("http://"):
        logger.warning(
            "Picus base URL was supplied over http://; upgrading to https:// so "
            "the refresh token is not sent in cleartext."
        )
        return ("https://" + value[len("http://") :]).rstrip("/")
    if not lowered.startswith("https://"):
        value = "https://" + value
    return value.rstrip("/")


async def _request_access_token(
    base_url: str, refresh_token: str
) -> tuple[str | None, int]:
    """Exchange a Picus refresh token for a short-lived access token.

    Returns ``(access_token, expires_in_seconds)`` on success, or ``(None, 0)``
    on failure. The access token is short-lived (~2 h); the endpoint takes no
    prior authentication.
    """
    if not refresh_token:
        return None, 0
    url = f"{base_url}/v1/auth/token"
    try:
        async with egress_client(timeout=20) as client:
            resp = await client.post(
                url,
                json={"refresh_token": refresh_token},
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            if resp.is_success:
                # Guard against non-JSON bodies (proxy/WAF HTML, empty body):
                # a JSON decode error must degrade to the documented (None, 0)
                # failure instead of raising out of the connectivity test.
                try:
                    data = resp.json()
                except ValueError:
                    logger.warning(
                        "Picus token response was not valid JSON (status %s)",
                        resp.status_code,
                    )
                    return None, 0
                if not isinstance(data, dict):
                    logger.warning(
                        "Picus token response had unexpected shape: %s",
                        type(data).__name__,
                    )
                    return None, 0
                token = data.get("token")
                if not token or not isinstance(token, str):
                    logger.warning("Picus token response contained no token")
                    return None, 0
                expire_at = data.get("expire_at")
                # ``expire_at`` is a unix timestamp. Picus timestamps appear in
                # both seconds and milliseconds across the API, so normalise
                # (a ms value taken as seconds would cache the ~2 h token for
                # millennia). Fall back to the documented ~2 h lifetime only
                # when the field is absent or non-numeric.
                ttl = 7200
                if isinstance(expire_at, (int, float)):
                    if expire_at > 1e12:  # milliseconds
                        expire_at = expire_at / 1000
                    ttl = int(expire_at - time.time())
                    if ttl <= 0:
                        # The endpoint says the token is already expired
                        # (clock skew or a malformed timestamp). Falling back
                        # to ~2 h would cache a dead token and cause repeated
                        # 401s - return a 1 s lifetime instead: usable for the
                        # immediate call, but below the shared cache's expiry
                        # skew so it is never cached.
                        logger.warning(
                            "Picus token response reported a non-positive "
                            "lifetime (expire_at=%s); not caching the token",
                            data.get("expire_at"),
                        )
                        ttl = 1
                return token, ttl
            logger.warning(
                "Picus token request failed (%s): %s",
                resp.status_code,
                resp.text[:300],
            )
    except httpx.HTTPError as exc:
        logger.warning("Picus token request error: %s", format_exception_message(exc))
    return None, 0


async def validate_connection(
    base_url: str, refresh_token: str
) -> tuple[bool, str]:
    """Verify Picus credentials by performing a token exchange.

    Returns ``(ok, message)``. A successful refresh->access token exchange
    against the manager confirms the base URL and refresh token are valid.
    Used by the integration connectivity test.
    """
    refresh_token = (refresh_token or "").strip()
    if not refresh_token:
        return False, "Missing Picus refresh token."
    resolved = resolve_base_url(base_url)
    token, _ = await _request_access_token(resolved, refresh_token)
    if token:
        return True, "Picus Security is connected and authenticated."
    return False, (
        "Authentication failed - check the manager base URL and confirm the "
        "refresh token is valid and not expired."
    )


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _sev(severity: str | None) -> str:
    return _SEVERITY_ICONS.get((severity or "").lower(), "\u26aa")


def _trunc(text: Any, max_len: int = 300) -> str:
    if not text:
        return ""
    text = str(text)
    return text[:max_len] + ("..." if len(text) > max_len else "")


def _ts(value: Any) -> str:
    """Render a Picus timestamp (unix seconds or milliseconds) for display."""
    if value in (None, "", 0):
        return "N/A"
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 1e12 else value
        try:
            return datetime.fromtimestamp(seconds, tz=UTC).strftime("%Y-%m-%d %H:%M")
        except (OverflowError, OSError, ValueError):
            return str(value)
    return str(value)[:19]


def _list_from(data: Any, *keys: str) -> list:
    """Return the first list payload from a Picus response envelope.

    Picus list endpoints wrap results in a named key (``simulations``,
    ``threats``, ``agents``, ...) alongside a ``pages`` object; a few return a
    bare list. This resolves either shape.
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def _total(data: Any) -> int | None:
    if isinstance(data, dict):
        pages = data.get("pages")
        if isinstance(pages, dict):
            return pages.get("total_count")
    return None


def _next_offset(data: Any, offset: int, count: int) -> int | None:
    """Return the next offset when more results exist, else ``None``."""
    total = _total(data)
    if isinstance(total, int) and offset + count < total:
        return offset + count
    return None


def _csv(value: str) -> str:
    """Normalise a comma-separated value into a tightly comma-joined string.

    Picus multi-value filters take ``key=value1,value2``. Splits on commas
    ONLY - individual values may legitimately contain spaces (MITRE tactics
    like ``Credential Access``, threat actor names, template names).
    """
    parts = [p.strip() for p in (value or "").split(",") if p.strip()]
    return ",".join(parts)


def _int_ids(value: str) -> list[int]:
    """Extract a list of integer IDs from a comma/space separated string."""
    return [int(m) for m in re.findall(r"\d+", value or "")]


def _envelope_error(data: Any) -> str:
    """Return a human-readable string from a Picus error envelope, if any."""
    if not isinstance(data, dict):
        return ""
    msg = data.get("message")
    errors = data.get("errors")
    parts: list[str] = []
    if msg:
        parts.append(str(msg))
    if isinstance(errors, dict) and errors:
        parts.append("; ".join(f"{k}: {v}" for k, v in errors.items()))
    elif isinstance(errors, list) and errors:
        parts.append("; ".join(str(e) for e in errors))
    return " - ".join(parts)[:400]


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build Picus Security tools.

    ``token`` is the compound ``base_url||refresh_token`` assembled by
    ``integration_tools._get_token`` from the integration's stored manager URL
    and refresh token.
    """
    # ``parse_compound_token`` does not strip its segments - a refresh token
    # stored with surrounding whitespace (pasted with a trailing newline)
    # would otherwise be sent verbatim to the token endpoint and fail auth
    # with a misleading "token expired" message (matches infoblox / rapid7).
    raw_base_url, refresh_token = (
        part.strip() for part in parse_compound_token(token, 2)
    )
    base_url = resolve_base_url(raw_base_url)
    # Cache key derived from credentials (never stores the secret) so a rotated
    # refresh token transparently invalidates the cached access token.
    cache_key = app_token_cache_key("picus", base_url, refresh_token)

    async def _token() -> str | None:
        cached = get_cached_app_token(cache_key)
        if cached:
            return cached
        access, ttl = await _request_access_token(base_url, refresh_token)
        if access:
            # The shared cache applies its own expiry skew - pass the raw
            # lifetime (mirrors the Defender / Sentinel pattern).
            set_cached_app_token(cache_key, access, ttl)
        return access

    async def _api(
        method: str,
        path: str,
        action: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        timeout: int = _DEFAULT_TIMEOUT,
    ) -> tuple[Any, str | None]:
        """Authenticated request helper returning ``(json, error)``.

        Acquires a (cached) bearer access token, performs the request via the
        shared rate-limit-aware ``safe_request``, and parses the JSON body.
        """
        access_token = await _token()
        if not access_token:
            return None, _AUTH_ERROR
        headers = bearer_auth_headers(
            access_token,
            extra={"Accept": "application/json", "Content-Type": "application/json"},
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
            )
            if err:
                return None, err
            # Empty body (e.g. 204 on cancel/delete) - success with no payload.
            if resp.status_code == 204 or not resp.content:
                return {}, None
            try:
                return resp.json(), None
            except ValueError:
                # A non-empty 2xx body that is not JSON (proxy/WAF HTML in
                # front of the manager) must surface as an explicit error -
                # swallowing it as an empty payload would produce misleading
                # downstream messages like "not found".
                logger.warning(
                    "Picus returned a non-JSON body for %s (status %s)",
                    action,
                    resp.status_code,
                )
                return None, (
                    f"Error: {_PROVIDER} returned a non-JSON response for "
                    f"'{action}' (status {resp.status_code}). A proxy or WAF "
                    "may be intercepting API traffic - check the manager "
                    "base URL."
                )

    # =====================================================================
    # 0. SIMULATIONS & RESULTS (read)
    # =====================================================================

    async def list_picus_simulations(
        status: str = "",
        simulation_name: str = "",
        agent_name: str = "",
        template_name: str = "",
        prevention_result_lte: int = -1,
        prevention_result_gte: int = -1,
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        """List Picus simulations with their last prevention/detection scores."""
        params: dict[str, Any] = {"limit": min(limit, 50), "offset": max(offset, 0)}
        if status:
            params["status"] = _csv(status)
        if simulation_name:
            params["simulation_name"] = simulation_name
        if agent_name:
            params["agent_name"] = agent_name
        if template_name:
            params["template_name"] = template_name
        if prevention_result_lte >= 0:
            params["prevention_result_lte"] = prevention_result_lte
        if prevention_result_gte >= 0:
            params["prevention_result_gte"] = prevention_result_gte

        data, err = await _api(
            "get", "/v1/simulations", "list simulations", params=params
        )
        if err:
            return err
        sims = _list_from(data, "simulations")

        def _fmt(s: dict) -> str:
            agent = s.get("agent") or {}
            prev = s.get("last_prevention_security_score")
            det = s.get("last_detection_security_score")
            return (
                f"  #{s.get('simulation_id', '?')} {s.get('simulation_name', 'Unnamed')} "
                f"[{s.get('status', '?')}]\n"
                f"    Agent: {agent.get('name', 'N/A')} ({agent.get('status', '?')}) | "
                f"Template: {s.get('template_name', 'N/A')} | "
                f"Prevention: {prev if prev is not None else 'N/A'} | "
                f"Detection: {det if det is not None else 'N/A'}"
            )

        out = format_entity_list(
            sims, _fmt, noun="simulation", total=_total(data) or len(sims)
        )
        nxt = _next_offset(data, params["offset"], len(sims))
        if nxt is not None:
            out += f"\n[More results available - pass offset={nxt}]"
        return out

    async def get_picus_simulation(simulation_id: int) -> str:
        """Get a Picus simulation's detail and run history (run IDs + status)."""
        data, err = await _api(
            "get", f"/v1/simulations/{simulation_id}", f"get simulation {simulation_id}"
        )
        if err:
            return err
        s = data.get("Simulations") if isinstance(data, dict) else None
        if not isinstance(s, dict):
            s = data if isinstance(data, dict) else {}
        if not s.get("simulation_id"):
            return f"Simulation {simulation_id} not found."
        agent = s.get("agent") or {}
        lines = [
            f"Simulation #{s.get('simulation_id')}: {s.get('simulation_name', 'Unnamed')}",
            f"Status: {s.get('status', '?')} | Completion: {s.get('completion_ratio', '?')}%",
            f"Agent: {agent.get('name', 'N/A')} ({agent.get('type', '?')}, "
            f"{agent.get('status', '?')})",
            f"Template: {s.get('template_name', 'N/A')} (id {s.get('template_id', '?')})",
            f"Last prevention score: {s.get('last_prevention_security_score', 'N/A')} | "
            f"Last detection score: {s.get('last_detection_security_score', 'N/A')}",
            f"Created: {_ts(s.get('created_at'))} | Updated: {_ts(s.get('updated_at'))}",
        ]
        runs = s.get("simulation_run") or []
        if runs:
            lines.append(f"\nRuns ({len(runs)}):")
            for r in runs[:20]:
                lines.append(
                    f"  - run {r.get('id', '?')} [{r.get('status', '?')}] "
                    f"started {_ts(r.get('started_at'))}"
                    + (
                        f", completed {_ts(r.get('completed_at'))}"
                        if r.get("completed_at")
                        else ""
                    )
                )
        return "\n".join(lines)

    async def get_picus_simulation_result(
        simulation_id: int, run_id: int = 0
    ) -> str:
        """Get prevention + detection analytics for a simulation run.

        Omit ``run_id`` (or pass 0) for the latest run.
        """
        if run_id:
            path = f"/v1/simulations/{simulation_id}/run/{run_id}"
        else:
            path = f"/v1/simulations/{simulation_id}/run/latest"
        data, err = await _api("get", path, f"get simulation {simulation_id} result")
        if err:
            return err
        if not isinstance(data, dict) or not data.get("simulation_id"):
            return f"No result available for simulation {simulation_id}."
        lines = [
            f"Simulation #{data.get('simulation_id')} run {data.get('simulation_run_id', '?')}",
            f"Status: {data.get('status', '?')} | "
            f"Completion: {data.get('completion_ratio', '?')}%",
            f"Started: {_ts(data.get('started_at'))} | "
            f"Completed: {_ts(data.get('completed_at'))}",
        ]
        results = (data.get("results") or {}) if isinstance(data, dict) else {}
        prevention = results.get("prevention") or {}
        if prevention:
            threat = prevention.get("threat") or {}
            obj = prevention.get("attacker_objectives") or {}
            lines.append(
                f"\nPrevention score: {prevention.get('security_score', 'N/A')}\n"
                f"  Threats: {threat.get('blocked_count', 0)} blocked / "
                f"{threat.get('not_blocked_count', 0)} not blocked "
                f"(total {threat.get('total_count', 0)})\n"
                f"  Attacker objectives: {obj.get('achived_count', 0)} achieved / "
                f"{obj.get('unachived_count', 0)} unachieved (total {obj.get('total_count', 0)})"
            )
        if results.get("has_detection_analysis"):
            det = results.get("detection_analysis") or {}
            log = det.get("log") or {}
            alert = det.get("alert") or {}
            integrations = ", ".join(
                i.get("product_name", "?") for i in det.get("integrations", []) or []
            )
            lines.append(
                f"\nDetection score: {det.get('security_score', 'N/A')}\n"
                f"  Logged: {log.get('logged_threat_count', 0)} / "
                f"not logged: {log.get('not_logged_threat_count', 0)} "
                f"(score {log.get('score', 'N/A')})\n"
                f"  Alerted: {alert.get('alerted_threat_count', 0)} / "
                f"not alerted: {alert.get('not_alerted_threat_count', 0)} "
                f"(score {alert.get('score', 'N/A')})\n"
                f"  Integrations: {integrations or 'N/A'}"
            )
        return "\n".join(lines)

    async def list_picus_simulation_threats(
        simulation_id: int,
        run_id: int = 0,
        limit: int = 10,
        offset: int = 0,
    ) -> str:
        """List threat/objective/action results (blocked, logged, alerted) for a run.

        Omit ``run_id`` (or pass 0) for the latest run.
        """
        params = {"limit": min(limit, 50), "offset": max(offset, 0)}
        if run_id:
            path = f"/v1/simulations/{simulation_id}/run/{run_id}/threats"
        else:
            path = f"/v1/simulations/{simulation_id}/run/latest/threats"
        data, err = await _api(
            "get", path, f"list simulation {simulation_id} threats", params=params
        )
        if err:
            return err
        threats = _list_from(data, "threats")

        def _fmt(t: dict) -> str:
            det = t.get("detection_analysis") or {}
            det_str = ""
            if t.get("has_detection_analysis"):
                det_str = (
                    f" | Logged: {det.get('log_result', 'N/A')} | "
                    f"Alerted: {det.get('alert_result', 'N/A')}"
                )
            return (
                f"  Threat {t.get('threat_id', '?')} {t.get('threat_name', 'Unnamed')} "
                f"- Prevention: {t.get('prevention', 'N/A')}{det_str}\n"
                f"    Objectives: {len(t.get('objectives', []) or [])} | "
                f"Started: {_ts(t.get('started_at'))}"
            )

        out = format_entity_list(
            threats, _fmt, noun="threat result", total=_total(data) or len(threats)
        )
        nxt = _next_offset(data, params["offset"], len(threats))
        if nxt is not None:
            out += f"\n[More results available - pass offset={nxt}]"
        return out

    async def get_picus_summary(
        period_type: str = "MONTH", simulation_ids: str = ""
    ) -> str:
        """Get the account-wide prevention/detection posture summary.

        ``period_type`` is one of WEEK, MONTH, QUARTER. Optionally scope to
        specific simulations with a comma-separated ``simulation_ids``.
        """
        period = (period_type or "MONTH").upper()
        if period not in ("WEEK", "MONTH", "QUARTER"):
            return "Error: period_type must be one of WEEK, MONTH, QUARTER."
        body: dict[str, Any] = {"periodType": period}
        ids = _int_ids(simulation_ids)
        if ids:
            body["simulationIds"] = ids
        data, err = await _api(
            "post", "/v1/summary/overall", "get summary", json=body
        )
        if err:
            return err
        summary = data.get("summary") if isinstance(data, dict) else None
        if not isinstance(summary, dict):
            summary = data if isinstance(data, dict) else {}
        prev = summary.get("prevention") or {}
        det = summary.get("detection") or {}
        threats = prev.get("threats") or {}
        objectives = prev.get("objectives") or {}
        lines = [
            f"Picus posture summary ({period}):",
            f"\nPrevention score: {prev.get('score', 'N/A')}",
            f"  Threats: {(threats.get('blocked') or {}).get('count', 0)} blocked / "
            f"{(threats.get('notBlocked') or {}).get('count', 0)} not blocked "
            f"(total {threats.get('count', 0)})",
            f"  Objectives: {(objectives.get('achieved') or {}).get('count', 0)} achieved / "
            f"{(objectives.get('notAchieved') or {}).get('count', 0)} not achieved "
            f"(total {objectives.get('count', 0)})",
            f"\nDetection score: {det.get('score', 'N/A')} "
            f"(threats {det.get('threatCount', 0)})",
            f"  Logged: {(det.get('loggedThreat') or {}).get('count', 0)} / "
            f"not logged: {(det.get('notLoggedThreat') or {}).get('count', 0)}",
            f"  Alerted: {(det.get('alertedThreat') or {}).get('count', 0)} / "
            f"not alerted: {(det.get('notAlertedThreat') or {}).get('count', 0)}",
        ]
        return "\n".join(lines)

    # =====================================================================
    # 1. SIMULATION LIFECYCLE (write / destructive)
    # =====================================================================

    async def create_picus_simulation(
        name: str,
        template_id: int,
        agent_id: int = 0,
        description: str = "",
        integrations: str = "",
        run_now: bool = True,
        force: bool = False,
    ) -> str:
        """Create a Picus simulation from a template and run it immediately.

        ``template_id`` (required) selects the threat template; ``agent_id`` is
        the simulation agent that runs it. Set ``integrations`` to a
        comma-separated list of detection integration IDs to enrich results
        with detection analytics. ``run_now`` schedules an immediate one-off
        run; ``force`` allows creating an identical simulation.
        """
        if not name or not template_id:
            return "Error: both name and template_id are required."
        body: dict[str, Any] = {
            "name": name,
            "template_id": template_id,
            "protocols": [],
            "obfuscation_methods": [],
        }
        if agent_id:
            body["agent_id"] = agent_id
        if description:
            body["description"] = description
        integ_ids = _int_ids(integrations)
        if integ_ids:
            body["integrations"] = integ_ids
        if force:
            body["force"] = True
        if run_now:
            body["schedule"] = {
                "frequency": "ONCE",
                "start_time": int(time.time()),
                "day_of_week": [],
                "day_of_month": [],
            }
        data, err = await _api(
            "post", "/v1/simulations", "create simulation", json=body
        )
        if err:
            return err
        env_err = _envelope_error(data)
        if env_err and not (isinstance(data, dict) and data.get("simulation")):
            return f"Error creating simulation: {env_err}"
        sim = (data.get("simulation") or {}) if isinstance(data, dict) else {}
        run = (data.get("run_info") or {}) if isinstance(data, dict) else {}
        return (
            f"Simulation created: #{sim.get('id', '?')} '{sim.get('name', name)}' "
            f"(run {run.get('id', '?')}, status {run.get('status', 'N/A')}, "
            f"run_immediately={data.get('run_immediately') if isinstance(data, dict) else '?'})."
        )

    async def run_picus_simulation(simulation_id: int) -> str:
        """Re-run an existing Picus simulation now (simulate-now)."""
        data, err = await _api(
            "post",
            f"/v1/simulations/{simulation_id}/simulate-now",
            f"run simulation {simulation_id}",
        )
        if err:
            return err
        env_err = _envelope_error(data)
        if env_err and not (isinstance(data, dict) and data.get("run_info")):
            return f"Error running simulation {simulation_id}: {env_err}"
        run = (data.get("run_info") or {}) if isinstance(data, dict) else {}
        return (
            f"Simulation {simulation_id} re-run started: run {run.get('id', '?')} "
            f"(status {run.get('status', 'N/A')})."
        )

    async def manage_picus_simulation(
        action: str,
        simulation_id: int,
        name: str = "",
        description: str = "",
        is_active: bool | None = None,
    ) -> str:
        """Manage a Picus simulation's lifecycle: update, cancel, or delete.

        ``action`` is one of:
          - update: rename / re-describe / toggle active (name, description,
            is_active). Only the fields provided are changed - omitting
            ``is_active`` leaves the current active state untouched.
          - cancel: cancel the simulation if it is currently running
          - delete: permanently delete the simulation (cannot be undone)
        """
        act = (action or "").lower().strip()
        if act not in _SIMULATION_ACTIONS:
            return (
                f"Error: invalid action '{action}'. Use one of: "
                f"{', '.join(sorted(_SIMULATION_ACTIONS))}."
            )
        if act == "update":
            body: dict[str, Any] = {}
            if is_active is not None:
                body["is_active"] = is_active
            if name:
                body["name"] = name
            if description:
                body["description"] = description
            if not body:
                return (
                    "Error: provide at least one of name, description, or "
                    "is_active for action='update'."
                )
            data, err = await _api(
                "put",
                f"/v1/simulations/{simulation_id}",
                f"update simulation {simulation_id}",
                json=body,
            )
            if err:
                return err
            env_err = _envelope_error(data)
            if env_err:
                return f"Error updating simulation {simulation_id}: {env_err}"
            return f"Simulation {simulation_id} updated."
        if act == "cancel":
            data, err = await _api(
                "put",
                f"/v1/simulations/{simulation_id}/cancel",
                f"cancel simulation {simulation_id}",
            )
            if err:
                return err
            env_err = _envelope_error(data)
            if env_err:
                return f"Error cancelling simulation {simulation_id}: {env_err}"
            return f"Simulation {simulation_id} cancelled."
        # delete
        data, err = await _api(
            "delete",
            f"/v1/simulations/{simulation_id}",
            f"delete simulation {simulation_id}",
        )
        if err:
            return err
        env_err = _envelope_error(data)
        if env_err:
            return f"Error deleting simulation {simulation_id}: {env_err}"
        return f"Simulation {simulation_id} deleted."

    # =====================================================================
    # 2. THREAT LIBRARY (read)
    # =====================================================================

    async def list_picus_threats(
        severities: str = "",
        mitre_attack: str = "",
        attacker_objectives: str = "",
        threat_actors: str = "",
        affected_os: str = "",
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        """List threats in the Picus Threat Library.

        Filter by ``severities`` (Low, Medium, High), ``mitre_attack`` tactics,
        ``attacker_objectives``, ``threat_actors``, and ``affected_os``
        (Windows, Linux, macOS). Multi-value filters accept comma-separated
        values.
        """
        params: dict[str, Any] = {"limit": min(limit, 50), "offset": max(offset, 0)}
        if severities:
            params["severities"] = _csv(severities)
        if mitre_attack:
            params["mitre_attack"] = _csv(mitre_attack)
        if attacker_objectives:
            params["attacker_objectives"] = _csv(attacker_objectives)
        if threat_actors:
            params["threat_actors"] = _csv(threat_actors)
        if affected_os:
            params["affected_os"] = _csv(affected_os)
        data, err = await _api(
            "get", "/v1/threat-library/threats", "list threats", params=params
        )
        if err:
            return err
        threats = _list_from(data, "threats")

        def _fmt(t: dict) -> str:
            mitres = ", ".join(
                m.get("tactic", "") for m in t.get("mitre_attack", []) or []
            )
            return (
                f"  {_sev(t.get('severity'))} [{t.get('severity', '?')}] "
                f"{t.get('name', 'Unnamed')} (id {t.get('id', '?')}, "
                f"display {t.get('display_id', '?')})\n"
                f"    OS: {', '.join(t.get('affected_os', []) or []) or 'N/A'} | "
                f"MITRE: {mitres or 'N/A'} | Released: {_ts(t.get('release_date'))}"
            )

        out = format_entity_list(
            threats, _fmt, noun="threat", total=_total(data) or len(threats)
        )
        nxt = _next_offset(data, params["offset"], len(threats))
        if nxt is not None:
            out += f"\n[More results available - pass offset={nxt}]"
        return out

    async def get_picus_threat(threat_id: int) -> str:
        """Get full detail of a Picus threat: objectives, actions, MITRE, actors."""
        data, err = await _api(
            "get",
            f"/v1/threat-library/threats/{threat_id}",
            f"get threat {threat_id}",
        )
        if err:
            return err
        t = data if isinstance(data, dict) else {}
        if isinstance(data, dict) and isinstance(data.get("threat"), dict):
            t = data["threat"]
        if not t.get("id"):
            return f"Threat {threat_id} not found."
        mitres = ", ".join(m.get("tactic", "") for m in t.get("mitre_attack", []) or [])
        actors = ", ".join(
            a.get("name", a) if isinstance(a, dict) else str(a)
            for a in t.get("threat_actors", []) or []
        )
        lines = [
            f"Threat {t.get('id')}: {t.get('name', 'Unnamed')} "
            f"(display {t.get('display_id', '?')})",
            f"Severity: {_sev(t.get('severity'))} {t.get('severity', '?')}",
            f"Affected OS: {', '.join(t.get('affected_os', []) or []) or 'N/A'}",
            f"MITRE tactics: {mitres or 'N/A'}",
            f"Threat actors: {actors or 'N/A'}",
            f"Predefined: {t.get('is_predefined', '?')} | "
            f"Released: {_ts(t.get('release_date'))}",
        ]
        if t.get("description"):
            lines.append(f"\nDescription:\n{_trunc(t.get('description'), 600)}")
        flows = t.get("flows") or []
        if flows:
            lines.append(f"\nObjective flows ({len(flows)}):")
            for f in flows[:15]:
                lines.append(
                    f"  - {f.get('name', 'Objective')} "
                    f"({len(f.get('actions', []) or [])} actions)"
                )
        return "\n".join(lines)

    async def list_picus_threat_actions(
        updated_at_gte: int = 0,
        updated_at_lte: int = 0,
        limit: int = 100,
        offset: int = 0,
    ) -> str:
        """List attack actions in the Picus Threat Library (the atomic attacks)."""
        params: dict[str, Any] = {"limit": min(limit, 100), "offset": max(offset, 0)}
        if updated_at_gte:
            params["updated_at_gte"] = updated_at_gte
        if updated_at_lte:
            params["updated_at_lte"] = updated_at_lte
        data, err = await _api(
            "get", "/v2/threat-library/actions", "list actions", params=params
        )
        if err:
            return err
        actions = _list_from(data, "actions")

        def _fmt(a: dict) -> str:
            return (
                f"  {a.get('action_name') or a.get('name', 'Unnamed')} "
                f"(id {a.get('action_id') or a.get('id', '?')}, "
                f"display {a.get('action_display_id') or a.get('display_id', '?')}) | "
                f"Updated: {_ts(a.get('updated_at'))}"
            )

        out = format_entity_list(
            actions, _fmt, noun="action", total=_total(data) or len(actions)
        )
        nxt = _next_offset(data, params["offset"], len(actions))
        if nxt is not None:
            out += f"\n[More results available - pass offset={nxt}]"
        return out

    async def get_picus_threat_action(action_id: int) -> str:
        """Get full detail of a Picus attack action: modules, payloads, MITRE."""
        data, err = await _api(
            "get",
            f"/v1/threat-library/actions/{action_id}",
            f"get action {action_id}",
        )
        if err:
            return err
        a = data if isinstance(data, dict) else {}
        if isinstance(data, dict) and isinstance(data.get("action"), dict):
            a = data["action"]
        if not (a.get("action_id") or a.get("id")):
            return f"Action {action_id} not found."
        mitres = ", ".join(
            m.get("tactic", "") for m in a.get("mitres", []) or []
        )
        lines = [
            f"Action {a.get('action_id') or a.get('id')}: "
            f"{a.get('action_name') or a.get('name', 'Unnamed')}",
            f"Category: {a.get('action_category') or a.get('category', 'N/A')} | "
            f"Attack module: {a.get('attack_module', 'N/A')}",
            f"MITRE: {mitres or 'N/A'}",
            f"Affected OS: {', '.join(a.get('action_affected_os', []) or a.get('affected_os', []) or []) or 'N/A'}",
            f"Released: {_ts(a.get('action_release_date') or a.get('release_date'))}",
        ]
        desc = a.get("action_description") or a.get("description")
        if desc:
            lines.append(f"\nDescription:\n{_trunc(desc, 600)}")
        return "\n".join(lines)

    async def list_picus_threat_actors(limit: int = 100, offset: int = 0) -> str:
        """List threat actors tracked in the Picus Threat Library."""
        params = {"limit": min(limit, 100), "offset": max(offset, 0)}
        data, err = await _api(
            "get", "/v1/threat-library/threat-actors", "list threat actors",
            params=params,
        )
        if err:
            return err
        actors = _list_from(data, "threat_actors", "actors")

        def _fmt(a: Any) -> str:
            if isinstance(a, dict):
                return f"  {a.get('name', a.get('id', '?'))}"
            return f"  {a}"

        out = format_entity_list(
            actors, _fmt, noun="threat actor", total=_total(data) or len(actors)
        )
        nxt = _next_offset(data, params["offset"], len(actors))
        if nxt is not None:
            out += f"\n[More results available - pass offset={nxt}]"
        return out

    # =====================================================================
    # 3. TEMPLATES (read)
    # =====================================================================

    async def list_picus_templates(
        category_name: str = "",
        content_type: str = "",
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        """List Picus threat templates (the building blocks of a simulation)."""
        params: dict[str, Any] = {"limit": min(limit, 100), "offset": max(offset, 0)}
        if category_name:
            params["category_name"] = category_name
        if content_type:
            params["content_type"] = content_type
        data, err = await _api(
            "get", "/v1/templates", "list templates", params=params
        )
        if err:
            return err
        templates = _list_from(data, "templates")

        def _fmt(t: dict) -> str:
            return (
                f"  {t.get('name', 'Unnamed')} (id {t.get('id', '?')}) | "
                f"Threats: {t.get('threat_count', '?')} | "
                f"Type: {t.get('content_type', 'N/A')} | "
                f"Category: {t.get('category_name', 'N/A')}"
            )

        out = format_entity_list(
            templates, _fmt, noun="template", total=_total(data) or len(templates)
        )
        nxt = _next_offset(data, params["offset"], len(templates))
        if nxt is not None:
            out += f"\n[More results available - pass offset={nxt}]"
        return out

    async def get_picus_template(template_id: int) -> str:
        """Get a Picus template's detail: threats, applicable agent types, rules."""
        data, err = await _api(
            "get", f"/v1/templates/{template_id}", f"get template {template_id}"
        )
        if err:
            return err
        t = data if isinstance(data, dict) else {}
        if isinstance(data, dict) and isinstance(data.get("template"), dict):
            t = data["template"]
        if not t.get("id"):
            return f"Template {template_id} not found."
        threats = t.get("threat_list") or t.get("threats") or []
        lines = [
            f"Template {t.get('id')}: {t.get('name', 'Unnamed')}",
            f"Type: {t.get('content_type', 'N/A')} | "
            f"Category: {t.get('category_name', 'N/A')} / "
            f"{t.get('subcategory_name', 'N/A')}",
            f"Applicable agent types: "
            f"{', '.join(t.get('applicable_agent_types', []) or []) or 'N/A'}",
            f"Threats: {len(threats)}",
        ]
        if t.get("description"):
            lines.append(f"\nDescription:\n{_trunc(t.get('description'), 400)}")
        return "\n".join(lines)

    # =====================================================================
    # 4. AGENTS (read)
    # =====================================================================

    async def list_picus_agents(limit: int = 100, offset: int = 0) -> str:
        """List Picus simulation agents (the endpoints that run simulations)."""
        params = {"limit": min(limit, 100), "offset": max(offset, 0)}
        data, err = await _api("get", "/v1/agents", "list agents", params=params)
        if err:
            return err
        agents = _list_from(data, "agents", "result")

        def _fmt(a: dict) -> str:
            return (
                f"  {a.get('name', 'Unnamed')} (id {a.get('id', '?')}) "
                f"[{a.get('type', '?')}] - {a.get('status', '?')} | "
                f"Platform: {a.get('platform', 'N/A')} | "
                f"Created: {_ts(a.get('created_at'))}"
            )

        out = format_entity_list(
            agents, _fmt, noun="agent", total=_total(data) or len(agents)
        )
        nxt = _next_offset(data, params["offset"], len(agents))
        if nxt is not None:
            out += f"\n[More results available - pass offset={nxt}]"
        return out

    async def get_picus_agent(agent_id: int) -> str:
        """Get a Picus agent's detail: status, platform, mitigation devices, modules."""
        data, err = await _api(
            "get", f"/v1/agents/{agent_id}", f"get agent {agent_id}"
        )
        if err:
            return err
        a = data if isinstance(data, dict) else {}
        if isinstance(data, dict) and isinstance(data.get("agent"), dict):
            a = data["agent"]
        if not a.get("id") and not a.get("name"):
            return f"Agent {agent_id} not found."
        devices = a.get("mitigation_devices") or []
        modules = a.get("attack_modules") or []
        lines = [
            f"Agent {a.get('id', agent_id)}: {a.get('name', 'Unnamed')}",
            f"Status: {a.get('status', '?')} | Type: {a.get('type', '?')} | "
            f"Platform: {a.get('platform', 'N/A')}",
            f"Created: {_ts(a.get('created_at'))}",
            f"Mitigation devices: {len(devices)} | Attack modules: {len(modules)}",
        ]
        return "\n".join(lines)

    # =====================================================================
    # 5. INTEGRATIONS (read)
    # =====================================================================

    async def list_picus_integrations() -> str:
        """List Picus detection integrations (SIEM/EDR) with health status."""
        data, err = await _api("get", "/v1/integrations", "list integrations")
        if err:
            return err
        integrations = _list_from(data, "integrations")

        def _fmt(i: dict) -> str:
            return (
                f"  {i.get('name', 'Unnamed')} (id {i.get('id', '?')}) | "
                f"Health: {i.get('health_status', 'N/A')} | "
                f"Agent: {i.get('integration_agent_name', 'N/A')} | "
                f"Updated: {_ts(i.get('updated_at'))}"
            )

        return format_entity_list(integrations, _fmt, noun="integration")

    # =====================================================================
    # 6. MITIGATIONS - PREVENTION & DETECTION (read)
    # =====================================================================

    async def list_picus_mitigation_devices() -> str:
        """List Picus mitigation (prevention) devices with block scores."""
        data, err = await _api(
            "get", "/v2/mitigation/devices", "list mitigation devices"
        )
        if err:
            return err
        devices = _list_from(data, "devices")

        def _fmt(d: dict) -> str:
            return (
                f"  {d.get('name', d.get('device_name', 'Unnamed'))} "
                f"(id {d.get('id', d.get('device_id', '?'))}) | "
                f"Score: {d.get('score', 'N/A')}"
            )

        return format_entity_list(devices, _fmt, noun="mitigation device")

    async def get_picus_mitigation_device(device_id: int) -> str:
        """Get a mitigation device's block/not-blocked stats and score."""
        data, err = await _api(
            "get",
            f"/v2/mitigation/devices/{device_id}",
            f"get mitigation device {device_id}",
        )
        if err:
            return err
        d = data if isinstance(data, dict) else {}
        return (
            f"Mitigation device {device_id}:\n"
            f"  Blocked: {d.get('blocked_count', d.get('BlockedCount', 'N/A'))} | "
            f"Not blocked: {d.get('not_blocked_count', d.get('NotBlockedCount', 'N/A'))} | "
            f"Total: {d.get('total_count', d.get('TotalCount', 'N/A'))} | "
            f"Score: {d.get('score', d.get('Score', 'N/A'))}"
        )

    async def list_picus_device_signatures(device_id: int, action_ids: str) -> str:
        """List vendor signatures for specific actions on a mitigation device.

        ``action_ids`` is a comma-separated list of up to 10 action IDs -
        prevention content (signature IDs, severities, product platform) that
        would block those attacks on the device.
        """
        ids = _int_ids(action_ids)
        if not ids:
            return "Error: action_ids is required (up to 10 comma-separated IDs)."
        if len(ids) > 10:
            return (
                f"Error: action_ids accepts at most 10 IDs ({len(ids)} provided). "
                "Split the request into batches of 10."
            )
        data, err = await _api(
            "get",
            f"/v1/mitigation/devices/{device_id}/signatures",
            f"list device {device_id} signatures",
            params={"action_ids": ",".join(str(i) for i in ids)},
        )
        if err:
            return err
        sigs = _list_from(data, "signatures")

        def _fmt(s: dict) -> str:
            return (
                f"  {_sev(s.get('vendor_severity'))} {s.get('name', 'Unnamed')} "
                f"(id {s.get('id', '?')}) | "
                f"Severity: {s.get('vendor_severity', 'N/A')} | "
                f"Platform: {s.get('product_platform', 'N/A')} "
                f"{s.get('product_version', '')}"
            )

        return format_entity_list(sigs, _fmt, noun="signature")

    async def list_picus_prevention_recommendations(
        threat_ids: str = "",
        threat_severities: str = "",
        attack_modules: str = "",
        run_id: int = 0,
        is_all_assessments: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> str:
        """List not-blocked actions with vendor prevention recommendations.

        Surfaces the actions that were NOT blocked, each with its associated
        vendor mitigation signatures and recommendations - the prevention
        remediation backlog. Filter by ``threat_ids``, ``threat_severities``,
        ``attack_modules``, or a specific ``run_id``.
        """
        params: dict[str, Any] = {"limit": min(limit, 100), "offset": max(offset, 0)}
        if threat_ids:
            params["threat_ids"] = _csv(threat_ids)
        if threat_severities:
            params["threat_severities"] = _csv(threat_severities)
        if attack_modules:
            params["attack_modules"] = _csv(attack_modules)
        if run_id:
            params["run_id"] = run_id
        if is_all_assessments:
            params["is_all_assessments"] = "true"
        data, err = await _api(
            "get",
            "/v1/mitigation/generic/not-blocked-actions",
            "list prevention recommendations",
            params=params,
        )
        if err:
            return err
        actions = _list_from(data, "actions", "not_blocked_actions")

        def _fmt(a: dict) -> str:
            recs = a.get("recommendations") or a.get("signatures") or []
            return (
                f"  {a.get('action_name') or a.get('name', 'Unnamed')} "
                f"(id {a.get('action_id') or a.get('id', '?')}) | "
                f"Module: {a.get('attack_module', 'N/A')} | "
                f"Category: {a.get('category', 'N/A')} | "
                f"Recommendations: {len(recs)}"
            )

        out = format_entity_list(
            actions,
            _fmt,
            noun="not-blocked action",
            total=_total(data) or len(actions),
        )
        nxt = _next_offset(data, params["offset"], len(actions))
        if nxt is not None:
            out += f"\n[More results available - pass offset={nxt}]"
        return out

    async def list_picus_detection_rules(
        source: str,
        rule_severity: str = "",
        alerted: str = "",
        threats: str = "",
        search: str = "",
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        """List detection-content rules for a source (SIEM detection recommendations).

        ``source`` is the detection content source name (use manage_picus_admin
        action=list_detection_sources to discover). Filter by ``rule_severity``,
        ``alerted`` (true/false), ``threats``, or a free-text ``search``.
        """
        if not source:
            return "Error: source is required (see list_detection_sources)."
        params: dict[str, Any] = {"limit": min(limit, 50), "offset": max(offset, 0)}
        if rule_severity:
            params["ruleSeverity"] = _csv(rule_severity)
        if alerted:
            params["alerted"] = alerted
        if threats:
            params["threats"] = _csv(threats)
        if search:
            params["search"] = search
        data, err = await _api(
            "get",
            f"/v1/mitigation/detection-content/{quote(source, safe='')}/rules",
            f"list detection rules for {source}",
            params=params,
        )
        if err:
            return err
        rules = _list_from(data, "rules")

        def _fmt(r: dict) -> str:
            return (
                f"  {_sev(r.get('severity') or r.get('rule_severity'))} "
                f"{r.get('name', r.get('rule_name', 'Unnamed'))} "
                f"(id {r.get('id', r.get('rule_id', '?'))}) | "
                f"Alerted: {r.get('alerted', 'N/A')}"
            )

        out = format_entity_list(
            rules, _fmt, noun="detection rule", total=_total(data) or len(rules)
        )
        nxt = _next_offset(data, params["offset"], len(rules))
        if nxt is not None:
            out += f"\n[More results available - pass offset={nxt}]"
        return out

    # =====================================================================
    # 7. ADMIN / LONG TAIL (read, consolidated)
    # =====================================================================

    async def manage_picus_admin(
        action: str,
        source: str = "",
        rule_id: str = "",
        date_start: int = 0,
        date_end: int = 0,
        role: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> str:
        """Read Picus admin / long-tail resources via a single consolidated tool.

        ``action`` is one of:
          - list_integration_agents: detection integration agents + status
          - list_detection_sources: available detection content sources
          - list_detection_log_sources: log sources for a ``source``
          - get_detection_rule: a single rule (needs ``source`` + ``rule_id``)
          - list_activity_logs: account activity logs (``date_start``/``date_end``)
          - list_tags: threat-library tags
          - list_users: account users (optional ``role`` filter)
          - list_roles: account roles
        """
        act = (action or "").lower().strip()
        if act not in _ADMIN_ACTIONS:
            return (
                f"Error: invalid action '{action}'. Use one of: "
                f"{', '.join(sorted(_ADMIN_ACTIONS))}."
            )
        page = {"limit": min(limit, 100), "offset": max(offset, 0)}

        if act == "list_integration_agents":
            data, err = await _api(
                "get", "/v1/integrations/agents", "list integration agents"
            )
            if err:
                return err
            items = _list_from(data, "integration_agents", "agents")
            return format_entity_list(
                items,
                lambda a: (
                    f"  {a.get('name', a.get('integration_agent_name', 'Unnamed'))} "
                    f"(id {a.get('id', a.get('integration_agent_id', '?'))}) | "
                    f"Status: {a.get('status', 'N/A')} | "
                    f"Installed: {a.get('is_installed', 'N/A')}"
                ),
                noun="integration agent",
            )
        if act == "list_detection_sources":
            data, err = await _api(
                "get",
                "/v1/mitigation/detection-content/sources",
                "list detection sources",
            )
            if err:
                return err
            items = _list_from(data, "sources")
            return format_entity_list(
                items,
                lambda s: f"  {s.get('name', s) if isinstance(s, dict) else s}",
                noun="detection source",
            )
        if act == "list_detection_log_sources":
            if not source:
                return "Error: source is required for list_detection_log_sources."
            data, err = await _api(
                "get",
                f"/v1/mitigation/detection-content/{quote(source, safe='')}/log-sources",
                f"list log sources for {source}",
            )
            if err:
                return err
            items = _list_from(data, "log_sources", "logSources")
            return format_entity_list(
                items,
                lambda s: f"  {s.get('name', s) if isinstance(s, dict) else s}",
                noun="log source",
            )
        if act == "get_detection_rule":
            if not source or not rule_id:
                return "Error: both source and rule_id are required."
            data, err = await _api(
                "get",
                f"/v1/mitigation/detection-content/{quote(source, safe='')}/rules/"
                f"{quote(rule_id, safe='')}",
                f"get detection rule {rule_id}",
            )
            if err:
                return err
            r = data if isinstance(data, dict) else {}
            if isinstance(data, dict) and isinstance(data.get("rule"), dict):
                r = data["rule"]
            lines = [
                f"Detection rule {r.get('id', r.get('rule_id', rule_id))}: "
                f"{r.get('name', r.get('rule_name', 'Unnamed'))}",
                f"Severity: {r.get('severity', r.get('rule_severity', 'N/A'))} | "
                f"Source: {source}",
            ]
            if r.get("description"):
                lines.append(f"\n{_trunc(r.get('description'), 600)}")
            if r.get("content") or r.get("query"):
                lines.append(f"\nContent:\n{_trunc(r.get('content') or r.get('query'), 800)}")
            return "\n".join(lines)
        if act == "list_activity_logs":
            params = dict(page)
            if date_start:
                params["date_start"] = date_start
            if date_end:
                params["date_end"] = date_end
            data, err = await _api(
                "get", "/v1/activity-logs", "list activity logs", params=params
            )
            if err:
                return err
            items = _list_from(data, "activity_logs", "logs")
            out = format_entity_list(
                items,
                lambda log: (
                    f"  {_ts(log.get('date') or log.get('created_at'))} - "
                    f"{log.get('user', log.get('user_email', 'N/A'))}: "
                    f"{_trunc(log.get('message') or log.get('action'), 200)}"
                ),
                noun="activity log",
                total=_total(data) or len(items),
            )
            nxt = _next_offset(data, params["offset"], len(items))
            if nxt is not None:
                out += f"\n[More results available - pass offset={nxt}]"
            return out
        if act == "list_tags":
            data, err = await _api(
                "get", "/v1/threat-library/tags", "list tags"
            )
            if err:
                return err
            items = _list_from(data, "tags")
            return format_entity_list(
                items,
                lambda t: f"  {t.get('name', t) if isinstance(t, dict) else t}",
                noun="tag",
            )
        if act == "list_users":
            params = dict(page)
            if role:
                params["role"] = role
            data, err = await _api(
                "get", "/v1/users", "list users", params=params
            )
            if err:
                return err
            items = _list_from(data, "users")
            out = format_entity_list(
                items,
                lambda u: (
                    f"  {u.get('name', '')} {u.get('surname', '')} "
                    f"<{u.get('email', 'N/A')}> (id {u.get('user_id', u.get('id', '?'))}) | "
                    f"Role: {u.get('assigned_role', u.get('role', 'N/A'))} | "
                    f"Active: {u.get('is_active', u.get('IsActive', 'N/A'))}"
                ),
                noun="user",
                total=_total(data) or len(items),
            )
            nxt = _next_offset(data, params["offset"], len(items))
            if nxt is not None:
                out += f"\n[More results available - pass offset={nxt}]"
            return out
        # list_roles
        data, err = await _api("get", "/v1/users/roles", "list roles")
        if err:
            return err
        items = _list_from(data, "roles")
        return format_entity_list(
            items,
            lambda r: (
                f"  {r.get('name', r.get('role_name', 'Unnamed'))} "
                f"(id {r.get('id', r.get('role_id', '?'))})"
            ),
            noun="role",
        )

    # =====================================================================
    # REGISTER ALL TOOLS
    # =====================================================================

    executor.register("list_picus_simulations", list_picus_simulations)
    executor.register("get_picus_simulation", get_picus_simulation)
    executor.register("get_picus_simulation_result", get_picus_simulation_result)
    executor.register("list_picus_simulation_threats", list_picus_simulation_threats)
    executor.register("get_picus_summary", get_picus_summary)
    executor.register("create_picus_simulation", create_picus_simulation)
    executor.register("run_picus_simulation", run_picus_simulation)
    executor.register("manage_picus_simulation", manage_picus_simulation)
    executor.register("list_picus_threats", list_picus_threats)
    executor.register("get_picus_threat", get_picus_threat)
    executor.register("list_picus_threat_actions", list_picus_threat_actions)
    executor.register("get_picus_threat_action", get_picus_threat_action)
    executor.register("list_picus_threat_actors", list_picus_threat_actors)
    executor.register("list_picus_templates", list_picus_templates)
    executor.register("get_picus_template", get_picus_template)
    executor.register("list_picus_agents", list_picus_agents)
    executor.register("get_picus_agent", get_picus_agent)
    executor.register("list_picus_integrations", list_picus_integrations)
    executor.register("list_picus_mitigation_devices", list_picus_mitigation_devices)
    executor.register("get_picus_mitigation_device", get_picus_mitigation_device)
    executor.register("list_picus_device_signatures", list_picus_device_signatures)
    executor.register(
        "list_picus_prevention_recommendations",
        list_picus_prevention_recommendations,
    )
    executor.register("list_picus_detection_rules", list_picus_detection_rules)
    executor.register("manage_picus_admin", manage_picus_admin)

    src = "integration:picus"
    return [
        # -- 0. Simulations & results -------------------------------------
        ToolDefinition(
            name="list_picus_simulations",
            description=(
                "List Picus Breach and Attack Simulations with their last "
                "prevention and detection security scores. Filter by status "
                "(RUNNING, COMPLETED, STOPPED, NOT STARTED, SCHEDULED), "
                "simulation_name, agent_name, template_name, and prevention "
                "score bounds. Returns simulation IDs for follow-up calls."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "Status filter (comma-separated for multiple)",
                    },
                    "simulation_name": {"type": "string"},
                    "agent_name": {"type": "string"},
                    "template_name": {"type": "string"},
                    "prevention_result_lte": {"type": "integer", "default": -1},
                    "prevention_result_gte": {"type": "integer", "default": -1},
                    "limit": {"type": "integer", "default": 25},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": [],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="get_picus_simulation",
            description=(
                "Get a Picus simulation's full detail and its run history "
                "(each run's ID, status, and start/complete time). Use the run "
                "IDs with get_picus_simulation_result or "
                "list_picus_simulation_threats."
            ),
            input_schema={
                "type": "object",
                "properties": {"simulation_id": {"type": "integer"}},
                "required": ["simulation_id"],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="get_picus_simulation_result",
            description=(
                "Get the prevention and detection analytics for a simulation "
                "run: prevention score with blocked/not-blocked threat counts "
                "and attacker-objective counts, plus detection score with "
                "log/alert analysis per integration. Omit run_id for the "
                "latest run."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "simulation_id": {"type": "integer"},
                    "run_id": {
                        "type": "integer",
                        "default": 0,
                        "description": "Specific run ID; 0 = latest run",
                    },
                },
                "required": ["simulation_id"],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="list_picus_simulation_threats",
            description=(
                "List the threat / objective / action results for a simulation "
                "run - which threats were blocked or unblocked, and (when "
                "detection analysis is enabled) whether each was logged and "
                "alerted per integration. Omit run_id for the latest run."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "simulation_id": {"type": "integer"},
                    "run_id": {"type": "integer", "default": 0},
                    "limit": {"type": "integer", "default": 10},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": ["simulation_id"],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="get_picus_summary",
            description=(
                "Get the account-wide security-validation posture summary over "
                "a period (WEEK, MONTH, QUARTER): overall prevention score with "
                "blocked/not-blocked threats and achieved/unachieved objectives, "
                "plus detection score with logged/alerted threat counts. "
                "Optionally scope to specific simulation IDs."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "period_type": {
                        "type": "string",
                        "enum": ["WEEK", "MONTH", "QUARTER"],
                        "default": "MONTH",
                    },
                    "simulation_ids": {
                        "type": "string",
                        "description": "Comma-separated simulation IDs (optional)",
                    },
                },
                "required": [],
            },
            source=src,
            priority=0,
        ),
        # -- 1. Simulation lifecycle --------------------------------------
        ToolDefinition(
            name="create_picus_simulation",
            description=(
                "Create a Picus simulation from a threat template and run it "
                "immediately (a one-off ONCE schedule) by default. Requires a "
                "name and template_id (see list_picus_templates); set agent_id "
                "to the simulation agent (see list_picus_agents). Add "
                "integrations (comma-separated detection integration IDs) to "
                "enrich results with detection analytics. Set force=true to "
                "allow an identical simulation."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "template_id": {"type": "integer"},
                    "agent_id": {"type": "integer", "default": 0},
                    "description": {"type": "string"},
                    "integrations": {
                        "type": "string",
                        "description": "Comma-separated detection integration IDs",
                    },
                    "run_now": {"type": "boolean", "default": True},
                    "force": {"type": "boolean", "default": False},
                },
                "required": ["name", "template_id"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="run_picus_simulation",
            description=(
                "Re-run an existing Picus simulation now (simulate-now). "
                "Returns the new run ID and status; poll results with "
                "get_picus_simulation_result."
            ),
            input_schema={
                "type": "object",
                "properties": {"simulation_id": {"type": "integer"}},
                "required": ["simulation_id"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="manage_picus_simulation",
            description=(
                "Manage a Picus simulation's lifecycle. action='update' renames "
                "/ re-describes / toggles active (name, description, is_active) "
                "- only the fields provided are changed; action='cancel' "
                "cancels a running simulation; action='delete' permanently "
                "deletes the simulation (cannot be undone)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["update", "cancel", "delete"],
                    },
                    "simulation_id": {"type": "integer"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "is_active": {
                        "type": "boolean",
                        "description": "Omit to leave the active state unchanged",
                    },
                },
                "required": ["action", "simulation_id"],
            },
            source=src,
            priority=2,
        ),
        # -- 2. Threat library --------------------------------------------
        ToolDefinition(
            name="list_picus_threats",
            description=(
                "List threats in the Picus Threat Library with severity, "
                "affected OS, and MITRE ATT&CK tactics. Filter by severities "
                "(Low, Medium, High), mitre_attack tactics, attacker_objectives, "
                "threat_actors, and affected_os (Windows, Linux, macOS). "
                "Multi-value filters accept comma-separated values."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "severities": {"type": "string"},
                    "mitre_attack": {"type": "string"},
                    "attacker_objectives": {"type": "string"},
                    "threat_actors": {"type": "string"},
                    "affected_os": {"type": "string"},
                    "limit": {"type": "integer", "default": 25},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": [],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="get_picus_threat",
            description=(
                "Get full detail of a Picus threat: severity, affected OS, "
                "MITRE tactics, threat actors, objective flows, and the actions "
                "each objective runs."
            ),
            input_schema={
                "type": "object",
                "properties": {"threat_id": {"type": "integer"}},
                "required": ["threat_id"],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="list_picus_threat_actions",
            description=(
                "List attack actions in the Picus Threat Library (the atomic "
                "attacks that make up threats). Optionally filter by "
                "updated_at_gte / updated_at_lte (unix timestamps)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "updated_at_gte": {"type": "integer", "default": 0},
                    "updated_at_lte": {"type": "integer", "default": 0},
                    "limit": {"type": "integer", "default": 100},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": [],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="get_picus_threat_action",
            description=(
                "Get full detail of a Picus attack action: attack module, "
                "category, affected OS/products, MITRE mapping, payloads, and "
                "reference links."
            ),
            input_schema={
                "type": "object",
                "properties": {"action_id": {"type": "integer"}},
                "required": ["action_id"],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="list_picus_threat_actors",
            description="List the threat actors tracked in the Picus Threat Library.",
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 100},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": [],
            },
            source=src,
            priority=0,
        ),
        # -- 3. Templates -------------------------------------------------
        ToolDefinition(
            name="list_picus_templates",
            description=(
                "List Picus threat templates - the reusable threat collections "
                "used to build simulations. Filter by category_name and "
                "content_type. Returns template IDs for create_picus_simulation."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "category_name": {"type": "string"},
                    "content_type": {"type": "string"},
                    "limit": {"type": "integer", "default": 25},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": [],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="get_picus_template",
            description=(
                "Get a Picus template's detail: content type, category, "
                "applicable agent types, and the threats it contains."
            ),
            input_schema={
                "type": "object",
                "properties": {"template_id": {"type": "integer"}},
                "required": ["template_id"],
            },
            source=src,
            priority=0,
        ),
        # -- 4. Agents ----------------------------------------------------
        ToolDefinition(
            name="list_picus_agents",
            description=(
                "List Picus simulation agents (the endpoints that execute "
                "simulations) with status, type (Windows, Linux, macOS, "
                "Browser, Email, ...), and platform. Returns agent IDs for "
                "create_picus_simulation."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 100},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": [],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="get_picus_agent",
            description=(
                "Get a Picus agent's detail: status, type, platform, "
                "created time, mitigation devices, and attack modules."
            ),
            input_schema={
                "type": "object",
                "properties": {"agent_id": {"type": "integer"}},
                "required": ["agent_id"],
            },
            source=src,
            priority=0,
        ),
        # -- 5. Integrations ----------------------------------------------
        ToolDefinition(
            name="list_picus_integrations",
            description=(
                "List Picus detection integrations (SIEM/EDR products wired to "
                "Picus for detection analytics) with their health status and "
                "integration-agent binding."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=src,
            priority=0,
        ),
        # -- 6. Mitigations: prevention & detection -----------------------
        ToolDefinition(
            name="list_picus_mitigation_devices",
            description=(
                "List Picus mitigation (prevention) devices - the security "
                "controls (firewalls, IPS, WAF, ...) Picus evaluates - with "
                "their block scores. Returns device IDs for follow-up calls."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="get_picus_mitigation_device",
            description=(
                "Get a mitigation device's prevention stats: blocked count, "
                "not-blocked count, total, and block score."
            ),
            input_schema={
                "type": "object",
                "properties": {"device_id": {"type": "integer"}},
                "required": ["device_id"],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="list_picus_device_signatures",
            description=(
                "List the vendor signatures for specific attack actions on a "
                "mitigation device - the prevention content (signature IDs, "
                "severities, product platform/version) that would block those "
                "attacks. action_ids is a comma-separated list of up to 10 "
                "action IDs."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "device_id": {"type": "integer"},
                    "action_ids": {
                        "type": "string",
                        "description": "Comma-separated action IDs (max 10)",
                    },
                },
                "required": ["device_id", "action_ids"],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="list_picus_prevention_recommendations",
            description=(
                "List not-blocked actions with their vendor prevention "
                "recommendations and mitigation signatures - the prevention "
                "remediation backlog (what to fix to block more attacks). "
                "Filter by threat_ids, threat_severities, attack_modules, or a "
                "specific run_id."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "threat_ids": {"type": "string"},
                    "threat_severities": {"type": "string"},
                    "attack_modules": {"type": "string"},
                    "run_id": {"type": "integer", "default": 0},
                    "is_all_assessments": {"type": "boolean", "default": False},
                    "limit": {"type": "integer", "default": 100},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": [],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="list_picus_detection_rules",
            description=(
                "List detection-content rules for a source - the SIEM detection "
                "rule recommendations (e.g. Sigma/vendor rules) Picus maps to "
                "threats. 'source' is a detection content source name (discover "
                "with manage_picus_admin action=list_detection_sources). Filter "
                "by rule_severity, alerted (true/false), threats, or search."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "rule_severity": {"type": "string"},
                    "alerted": {"type": "string"},
                    "threats": {"type": "string"},
                    "search": {"type": "string"},
                    "limit": {"type": "integer", "default": 25},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": ["source"],
            },
            source=src,
            priority=0,
        ),
        # -- 7. Admin / long tail (consolidated read) ---------------------
        ToolDefinition(
            name="manage_picus_admin",
            description=(
                "Read Picus admin and long-tail resources via one tool. "
                "action: 'list_integration_agents' (detection integration "
                "agents), 'list_detection_sources' (available detection content "
                "sources), 'list_detection_log_sources' (needs source), "
                "'get_detection_rule' (needs source + rule_id), "
                "'list_activity_logs' (account audit log, optional "
                "date_start/date_end unix), 'list_tags' (threat-library tags), "
                "'list_users' (optional role filter), 'list_roles'."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": sorted(_ADMIN_ACTIONS),
                    },
                    "source": {"type": "string"},
                    "rule_id": {"type": "string"},
                    "date_start": {"type": "integer", "default": 0},
                    "date_end": {"type": "integer", "default": 0},
                    "role": {"type": "string"},
                    "limit": {"type": "integer", "default": 100},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": ["action"],
            },
            source=src,
            priority=0,
        ),
    ]
