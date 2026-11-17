# SPDX-License-Identifier: Apache-2.0

"""ANY.RUN integration - interactive malware sandbox + threat intelligence.

Full-fledged ANY.RUN (https://any.run) API v1 integration for the agentic
SOC/CTI loop: detonate files and URLs in the interactive cloud sandbox, poll
task status, read the analysis verdict / IOCs / MITRE ATT&CK mapping, browse
the analysis history, look up indicators in ANY.RUN Threat Intelligence Lookup,
and run YARA searches over the public sample database.

This is the FIRST interactive-malware-sandbox integration in XTM One (the
category was previously absent).

Tool categories:
  - Connection: validate the key + report the account's API limits
    (``GET /v1/user``).
  - Sandbox reads: analysis history (``GET /v1/analysis``), a full analysis
    report by task UUID (``GET /v1/analysis/{uuid}``) with the links of its
    artefacts, the report IOCs (``GET /report/{uuid}/ioc/json``), and the
    available sandbox environments (``GET /v1/environment``).
  - Artefact downloads: the HTML / STIX / MISP / IOC reports, PCAP,
    SSL keys, video, screenshots, process dumps and the sample, each stored
    as a file of the run - the sample and dumps only as ANY.RUN's encrypted
    ZIP (``deliver_live_artefact``).
  - Sandbox detonation (write): submit a URL (``POST /v1/analysis`` with
    ``obj_type=url``) or a chat-attached file (``POST /v1/analysis`` multipart
    with ``obj_type=file``) for analysis; add execution time
    (``PATCH /v1/analysis/addtime/{uuid}``); stop a running task
    (``PATCH /v1/analysis/stop/{uuid}``).
  - Sandbox cleanup (destructive): delete a task from history
    (``DELETE /v1/analysis/delete/{uuid}``).
  - Threat Intelligence: TI Lookup search by IOC or structured filters
    (``POST /v1/intelligence/api/search``), start a YARA search
    (``POST /v1/intelligence/yara-lookup/search``), and read its results
    (``GET /v1/intelligence/yara-lookup/search/{uuid}``).

Authentication is a single **API key** sent in the ``Authorization`` header.
ANY.RUN's current scheme expects the key with an ``API-KEY `` prefix; a raw key
(or a legacy ``Basic``-prefixed key) is normalised at the single header
chokepoint. All calls target ``https://api.any.run``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import httpx

from app.core.egress import egress_client
from app.core.utils import is_valid_uuid
from app.integrations.base import BaseIntegration, OAuthDefaults
from app.integrations.file_delivery import (
    SEALED_ARCHIVE_PASSWORD,
    PersistFileFn,
    deliver_file,
    deliver_live_artefact,
    fetch_file,
    uploaded_file_answer,
)
from app.integrations.http import (
    format_entity_list,
    safe_request,
)
from app.tools.base import CallableToolExecutor, FileContext, ToolDefinition, ToolResult

logger = logging.getLogger(__name__)

_PROVIDER = "ANY.RUN"
_SOURCE = "integration:anyrun"

# All ANY.RUN services live under the api.any.run host.
ANYRUN_API = "https://api.any.run/v1"
ANYRUN_REPORT_API = "https://api.any.run/report"
# Fixed public interactive-analysis host used only for building the vendor's
# own task/lookup deep links (never a user-configured base URL).
ANYRUN_APP_URL = "https://app.any.run"
ANYRUN_INTELLIGENCE_URL = "https://intelligence.any.run"

# TI Lookup structured filter -> ANY.RUN query field name.
_TI_FILTER_FIELDS: dict[str, str] = {
    "sha256": "sha256",
    "sha1": "sha1",
    "md5": "md5",
    "domain": "domainName",
    "ip": "destinationIP",
    "url": "url",
    "threat_name": "threatName",
    "mitre": "MITRE",
    "suricata_id": "suricataID",
    "task_type": "taskType",
}

# IOC reputation filter -> the numeric reputations ANY.RUN uses in its IOC
# report (0 = unknown, 1 = suspicious, 2 = malicious).
_IOC_REPUTATIONS: dict[str, set[int]] = {
    "all": {0, 1, 2},
    "suspicious": {1, 2},
    "malicious": {2},
}

# A single detonation can extract hundreds of IOCs - cap the tool output so a
# noisy sample cannot flood the LLM context (the count header stays exact).
_MAX_IOCS_SHOWN = 100


def _trunc(text: Any, max_len: int = 300) -> str:
    if text is None:
        return ""
    text = str(text).replace("\r", " ").replace("\n", " ").strip()
    return text[:max_len] + ("..." if len(text) > max_len else "")


# ---------------------------------------------------------------------------
# Auth + URL helpers (shared with integration_testing.py)
# ---------------------------------------------------------------------------


def _normalize_key(api_key: str) -> str:
    """Return the API key normalised to the canonical ``API-KEY <key>`` scheme.

    ANY.RUN's current auth scheme wants ``Authorization: API-KEY <key>``. A key
    pasted without a prefix gets one, and a pasted ``API-KEY`` prefix in any
    casing is canonicalised to uppercase (so a lowercase paste can never trip a
    case-sensitive scheme check). The legacy ``Basic`` scheme is passed through
    untouched for backward compatibility. A blank input stays blank so a
    missing credential fails auth loudly instead of sending ``API-KEY ``.
    """
    key = (api_key or "").strip()
    if not key:
        return ""
    lowered = key.lower()
    if lowered.startswith("basic "):
        return key
    if lowered == "api-key":
        return ""
    if lowered.startswith("api-key "):
        key = key[len("api-key ") :].strip()
        if not key:
            return ""
    return f"API-KEY {key}"


def build_anyrun_headers(api_key: str) -> dict[str, str]:
    """Build ANY.RUN request headers (single chokepoint for the credential).

    The key is normalised (prefix + whitespace stripped) so a pasted value can
    never produce a malformed ``Authorization`` header. Shared by the tool
    builder and the connectivity test.
    """
    return {
        "Authorization": _normalize_key(api_key),
        "Accept": "application/json",
    }


# ---------------------------------------------------------------------------
# Response formatting helpers
# ---------------------------------------------------------------------------


def _coerce_threat_level(threat_level: Any) -> int | None:
    """Int-coerce an ANY.RUN threat level (the API may return it as a string)."""
    try:
        return int(threat_level)
    except (TypeError, ValueError):
        return None


def _verdict_icon(threat_level: Any) -> str:
    mapping = {0: "[info]", 1: "[suspicious]", 2: "[malicious]"}
    level = _coerce_threat_level(threat_level)
    return mapping.get(level, "[unknown]") if level is not None else "[unknown]"


def _verdict_label(threat_level: Any) -> str:
    """Text verdict for an ANY.RUN threat level (int-coerced like the icon)."""
    mapping = {0: "No threat info", 1: "Suspicious", 2: "Malicious"}
    level = _coerce_threat_level(threat_level)
    return mapping.get(level, "unknown") if level is not None else "unknown"


def _task_link(task_uuid: str) -> str:
    return f"{ANYRUN_APP_URL}/tasks/{task_uuid}"


def _format_history_task(task: dict) -> str:
    """One line for an analysis-history entry (``GET /v1/analysis``)."""
    uuid = task.get("uuid") or task.get("id") or "?"
    verdict = task.get("verdict") or task.get("threatLevelText") or "?"
    name = task.get("name") or task.get("mainObject") or "?"
    date = task.get("date") or task.get("creationText") or ""
    tags = task.get("tags") or []
    tag_str = ""
    if isinstance(tags, list) and tags:
        names = [str(t.get("tag") if isinstance(t, dict) else t) for t in tags[:5]]
        tag_str = f" | tags: {', '.join(n for n in names if n)}"
    return (
        f"  {uuid}: {_trunc(name, 90)} | verdict: {verdict}"
        + (f" | {date}" if date else "")
        + tag_str
        + f"\n    Interactive: {_task_link(uuid)}"
    )


def _as_dict(value: Any) -> dict:
    """Return *value* if it is a dict, else an empty dict (shape guard)."""
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list:
    """Return *value* if it is a list, else an empty list (shape guard)."""
    return value if isinstance(value, list) else []


# Artefacts of an analysis: artifact -> (file name suffix, content type). The
# report formats are served by api.any.run, the recorded content by
# content.any.run (``TaskReportDto`` in https://files.any.run/schema/SandboxV1.json).
_ARTIFACTS: dict[str, tuple[str, str]] = {
    "html_report": ("report.html", "text/html"),
    "stix_report": ("stix.json", "application/json"),
    "misp_report": ("misp.json", "application/json"),
    "ioc_report": ("iocs.json", "application/json"),
    "pcap": ("traffic.pcap", "application/vnd.tcpdump.pcap"),
    "sslkeys": ("sslkeys.log", "text/plain"),
    "video": ("recording.mp4", "video/mp4"),
    "screenshot": ("screenshot.jpeg", "image/jpeg"),
    "process_dump": ("dump.bin", "application/octet-stream"),
    "sample": ("sample.zip", "application/zip"),
}
# The submitted file and the process memory dumps can hold live malware.
_LIVE_ARTIFACTS = frozenset({"sample", "process_dump"})
_REPORT_FORMATS = {
    "html_report": ("HTML", "summary/html"),
    "stix_report": ("STIX", "summary/stix"),
    "misp_report": ("MISP", "summary/misp"),
    "ioc_report": ("IOC", "ioc/json"),
}
_MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024


def _is_anyrun_url(url: str) -> bool:
    """Whether *url* is an https URL on an ANY.RUN host (the API key goes nowhere else)."""
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (host == "any.run" or host.endswith(".any.run"))


def _artifact_url(payload: dict, artifact: str, index: int) -> str:
    """The download URL of *artifact* in a ``GET /v1/analysis/{uuid}`` report."""
    analysis = _as_dict(_as_dict(payload.get("data")).get("analysis"))
    content = _as_dict(analysis.get("content"))
    if artifact in _REPORT_FORMATS:
        key, path = _REPORT_FORMATS[artifact]
        url = _as_dict(analysis.get("reports")).get(key)
        uuid = analysis.get("uuid")
        return str(url or (f"{ANYRUN_REPORT_API}/{uuid}/{path}" if uuid else ""))
    if artifact in ("pcap", "sslkeys", "video"):
        return str(_as_dict(content.get(artifact)).get("permanentUrl") or "")
    if artifact == "sample":
        main = _as_dict(content.get("mainObject"))
        return str(main.get("permanentUrl") or "") if main.get("type") == "file" else ""
    entries = _as_list(content.get("screenshots" if artifact == "screenshot" else "dumps"))
    if 1 <= index <= len(entries):
        return str(_as_dict(entries[index - 1]).get("permanentUrl") or "")
    return ""


def _format_artifacts(analysis: dict) -> list[str]:
    """The downloadable artefacts of a report, one line each, with their URLs."""
    content = _as_dict(analysis.get("content"))
    reports = _as_dict(analysis.get("reports"))
    lines: list[str] = []
    formats = " | ".join(
        f"{key} {reports[key]}" for key in ("HTML", "STIX", "MISP", "IOC") if reports.get(key)
    )
    if formats:
        lines.append(f"Reports: {formats}")
    for label, key in (("PCAP", "pcap"), ("SSL keys", "sslkeys"), ("Video", "video")):
        url = _as_dict(content.get(key)).get("permanentUrl")
        if url:
            lines.append(f"{label}: {url}")
    screenshots = _as_list(content.get("screenshots"))
    if screenshots:
        lines.append(f"Screenshots: {len(screenshots)} (artifact=screenshot, index 1-{len(screenshots)})")
    dumps = _as_list(content.get("dumps"))
    if dumps:
        lines.append(
            f"Process dumps: {len(dumps)} (artifact=process_dump, index 1-{len(dumps)}; "
            "live code, stored only as an encrypted archive)"
        )
    main = _as_dict(content.get("mainObject"))
    if main.get("type") == "file" and main.get("permanentUrl"):
        lines.append(
            f"Sample (ZIP, password '{SEALED_ARCHIVE_PASSWORD}'): {main['permanentUrl']}"
        )
    if lines:
        lines.append(
            "Store any of them as a file with download_anyrun_analysis_file(task_uuid, artifact)."
        )
    return lines


def _format_report(payload: dict) -> str:
    """Compact summary of a full analysis report (``GET /v1/analysis/{uuid}``)."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return "ANY.RUN returned an analysis report with no data."
    analysis = _as_dict(data.get("analysis"))
    scores = _as_dict(analysis.get("scores"))
    verdict = _as_dict(scores.get("verdict"))
    threat_level = verdict.get("threatLevel")
    threat_text = verdict.get("threatLevelText") or "unknown"

    content = _as_dict(analysis.get("content"))
    main = _as_dict(content.get("mainObject"))
    uuid = analysis.get("uuid") or _as_dict(data.get("task")).get("uuid") or "?"

    lines: list[str] = [
        f"ANY.RUN analysis {uuid}",
        f"Verdict: {_verdict_icon(threat_level)} {threat_text}",
    ]

    obj_type = main.get("type")
    obj_name = main.get("name") or main.get("filename")
    if obj_type or obj_name:
        lines.append(f"Object: {obj_type or '?'} {_trunc(obj_name, 120)}".strip())
    hashes = _as_dict(main.get("hashes"))
    if hashes:
        parts = [
            f"{algo}: {hashes[algo]}"
            for algo in ("md5", "sha1", "sha256")
            if hashes.get(algo)
        ]
        if parts:
            lines.append("Hashes: " + " | ".join(parts))

    tags = analysis.get("tags") or []
    if isinstance(tags, list) and tags:
        names = [str(t.get("tag") if isinstance(t, dict) else t) for t in tags]
        lines.append(f"Tags: {', '.join(n for n in names if n)[:400]}")

    # MITRE ATT&CK techniques (shape varies across report versions).
    mitre = data.get("mitre") or analysis.get("mitre") or []
    if isinstance(mitre, list) and mitre:
        techniques = []
        for item in mitre[:15]:
            if isinstance(item, dict):
                tid = item.get("id") or item.get("technique") or ""
                tname = item.get("name") or ""
                techniques.append(f"{tid} {tname}".strip())
        if techniques:
            extra = f" (+{len(mitre) - 15} more)" if len(mitre) > 15 else ""
            lines.append(f"MITRE ATT&CK: {'; '.join(techniques)}{extra}")

    # Process tree summary. A process counts as flagged only when its
    # int-coerced threat level is > 0 (a string "0" must not count, and a
    # non-dict scores/verdict shape must not raise).
    processes = data.get("processes") or analysis.get("processes") or []
    if isinstance(processes, list) and processes:
        flagged = 0
        for p in processes:
            if not isinstance(p, dict):
                continue
            p_verdict = _as_dict(_as_dict(p.get("scores")).get("verdict"))
            level = _coerce_threat_level(p_verdict.get("threatLevel"))
            if level is not None and level > 0:
                flagged += 1
        lines.append(
            f"Processes: {len(processes)} observed"
            + (f" ({flagged} flagged)" if flagged else "")
        )

    # IOC / network counters when present.
    counters = analysis.get("counters") or {}
    if isinstance(counters, dict) and counters:
        network = _as_dict(counters.get("network"))
        parts = []
        for label, value in (
            ("connections", network.get("connections")),
            ("http", network.get("http")),
            ("DNS", network.get("dns")),
            ("threats", network.get("threats")),
        ):
            if value:
                parts.append(f"{label}: {value}")
        if parts:
            lines.append("Network: " + " | ".join(parts))

    lines.append(f"Interactive report: {_task_link(uuid)}")
    lines.extend(_format_artifacts(analysis))
    lines.append(
        "Use get_anyrun_analysis_iocs with this UUID to pull the extracted IOCs."
    )
    return "\n".join(lines)


def _submit_body(
    obj_type: str,
    *,
    obj_url: str | None,
    env_os: str,
    env_version: str,
    env_bitness: int,
    env_type: str,
    opt_network_connect: bool,
    opt_privacy_type: str,
    opt_timeout: int,
) -> dict[str, Any]:
    """Assemble the sandbox submission body shared by URL + file detonation."""
    body: dict[str, Any] = {
        "obj_type": obj_type,
        "env_os": env_os,
        "env_version": env_version,
        "env_bitness": env_bitness,
        "env_type": env_type,
        "opt_network_connect": opt_network_connect,
        "opt_privacy_type": opt_privacy_type,
        "opt_timeout": opt_timeout,
    }
    if obj_url:
        body["obj_url"] = obj_url
    return body


# ---------------------------------------------------------------------------
# Provider class
# ---------------------------------------------------------------------------


class AnyRunIntegration(BaseIntegration):
    """ANY.RUN interactive malware sandbox + threat intelligence integration."""

    provider_name = "anyrun"
    display_name = "ANY.RUN"
    # Must be a key present in the frontend ICON_MAP (constants.ts) or the UI
    # silently falls back to the generic Bot icon.
    icon = "FlaskConical"
    description = (
        "Connect ANY.RUN interactive malware sandbox: detonate files and URLs, "
        "poll analysis status, read the verdict / IOCs / MITRE ATT&CK mapping, "
        "browse analysis history, look up indicators in Threat Intelligence "
        "Lookup, and run YARA searches over the public sample database."
    )
    auth_types = ["api_key"]

    def get_oauth_defaults(self) -> OAuthDefaults | None:
        return None

    async def validate_connection(self, token: str) -> bool:
        headers = build_anyrun_headers(token)
        if not headers["Authorization"]:
            return False
        async with egress_client(timeout=15) as client:
            resp, _err = await safe_request(
                client,
                "get",
                f"{ANYRUN_API}/user",
                _PROVIDER,
                "validate connection",
                headers=headers,
            )
        return resp is not None

    async def get_user_info(self, token: str) -> dict:
        headers = build_anyrun_headers(token)
        if not headers["Authorization"]:
            return {}
        async with egress_client(timeout=15) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{ANYRUN_API}/user",
                _PROVIDER,
                "get user info",
                headers=headers,
            )
            if err or resp is None:
                return {}
            data = resp.json()
        return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    persist_file_fn: PersistFileFn | None = None,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build ANY.RUN tools. ``token`` is the ANY.RUN API key.

    ``persist_file_fn`` (injected by ``integration_tools``) stores a
    downloaded artefact as a file of the run.
    """
    # Gate on the NORMALISED credential, not the raw token: a paste like a
    # bare "api-key" prefix normalises to an empty Authorization value, and
    # registering tools for it would guarantee unauthenticated failures.
    headers = build_anyrun_headers(token or "")
    if not headers["Authorization"]:
        return []

    def _client(timeout: int = 30) -> httpx.AsyncClient:
        return egress_client(timeout=timeout)

    # ══════════════════════════════════════════════════════════════════════
    # 0. CONNECTION
    # ══════════════════════════════════════════════════════════════════════

    async def get_anyrun_status() -> str:
        """Validate the ANY.RUN connection and report the account API limits."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{ANYRUN_API}/user",
                _PROVIDER,
                "get user limits",
                headers=headers,
            )
            if err:
                return err
            data = (resp.json() or {}).get("data") or {}
        limits = data.get("limits") or {}
        lines = ["Connected to ANY.RUN."]
        if limits:
            for scope in ("api", "web"):
                block = limits.get(scope)
                if isinstance(block, dict) and block:
                    parts = [f"{k}: {v}" for k, v in block.items()]
                    lines.append(f"  {scope} limits: {', '.join(parts)}")
            if not any(isinstance(limits.get(s), dict) for s in ("api", "web")):
                lines.append(f"  Limits: {limits}")
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 1. SANDBOX READS
    # ══════════════════════════════════════════════════════════════════════

    async def list_anyrun_analyses(
        team: bool = False, skip: int = 0, limit: int = 25
    ) -> str:
        """List recent submissions from the analysis history (task UUID + verdict)."""
        params = {
            "team": "true" if team else "false",
            "skip": max(0, int(skip or 0)),
            "limit": max(1, min(int(limit or 25), 100)),
        }
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{ANYRUN_API}/analysis",
                _PROVIDER,
                "list analyses",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = (resp.json() or {}).get("data") or {}
        tasks = data.get("tasks") or []
        return format_entity_list(
            tasks,
            _format_history_task,
            noun="analysis task",
            total=len(tasks),
            empty_msg="No analyses found in the ANY.RUN history.",
        )

    async def get_anyrun_analysis_report(task_uuid: str) -> str:
        """Get the full analysis report (verdict, IOCs summary, MITRE, processes)."""
        task_uuid = (task_uuid or "").strip()
        if not is_valid_uuid(task_uuid):
            return "Error: a valid task UUID is required (from a submit or list tool)."
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{ANYRUN_API}/analysis/{task_uuid}",
                _PROVIDER,
                f"get report {task_uuid}",
                headers=headers,
            )
            if err:
                return err
            payload = resp.json() or {}
        return _format_report(payload)

    async def get_anyrun_analysis_iocs(
        task_uuid: str, reputation: str = "suspicious"
    ) -> str:
        """Get the extracted IOCs for a finished analysis (filtered by reputation)."""
        task_uuid = (task_uuid or "").strip()
        if not is_valid_uuid(task_uuid):
            return "Error: a valid task UUID is required (from a submit or list tool)."
        reputation = (reputation or "suspicious").strip().lower()
        keep = _IOC_REPUTATIONS.get(reputation)
        if keep is None:
            return "Error: reputation must be one of all, suspicious, malicious."
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{ANYRUN_REPORT_API}/{task_uuid}/ioc/json",
                _PROVIDER,
                f"get IOCs {task_uuid}",
                headers=headers,
            )
            if err:
                return err
            payload = resp.json() or {}
        iocs = payload if isinstance(payload, list) else payload.get("iocs") or []
        if reputation != "all":
            iocs = [i for i in iocs if isinstance(i, dict) and i.get("reputation") in keep]

        def _fmt(ioc: dict) -> str:
            rep = {0: "unknown", 1: "suspicious", 2: "malicious"}.get(
                ioc.get("reputation"), "?"
            )
            return (
                f"  [{ioc.get('category') or ioc.get('type') or '?'}] "
                f"{_trunc(ioc.get('ioc') or ioc.get('value'), 200)} "
                f"(reputation: {rep})"
            )

        result = format_entity_list(
            iocs[:_MAX_IOCS_SHOWN],
            _fmt,
            noun="IOC",
            total=len(iocs),
            empty_msg=(
                f"No IOCs with reputation '{reputation}' for task {task_uuid}. "
                "The analysis may still be running - poll get_anyrun_analysis_report."
            ),
        )
        if len(iocs) > _MAX_IOCS_SHOWN:
            result += (
                f"\n  ... showing the first {_MAX_IOCS_SHOWN} of {len(iocs)} IOCs "
                "(narrow with reputation='malicious')."
            )
        return result

    async def download_anyrun_analysis_file(
        task_uuid: str, artifact: str, index: int = 1
    ) -> str | ToolResult:
        """Store one artefact of an analysis (report, PCAP, video, screenshot, sample...) as a file."""
        task_uuid = (task_uuid or "").strip()
        if not is_valid_uuid(task_uuid):
            return "Error: a valid task UUID is required (from a submit or list tool)."
        artifact = (artifact or "").strip().lower()
        if artifact not in _ARTIFACTS:
            return f"Error: artifact must be one of {', '.join(_ARTIFACTS)}."
        try:
            index = int(index or 1)
        except (TypeError, ValueError):
            index = 1
        async with _client(timeout=120) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{ANYRUN_API}/analysis/{task_uuid}",
                _PROVIDER,
                f"get report {task_uuid}",
                headers=headers,
            )
            if err:
                return err
            url = _artifact_url(resp.json() or {}, artifact, index)
            if not url:
                return (
                    f"Analysis {task_uuid} has no {artifact.replace('_', ' ')}"
                    + (f" #{index}" if artifact in ("screenshot", "process_dump") else "")
                    + " (see get_anyrun_analysis_report for what it recorded)."
                )
            if not _is_anyrun_url(url):
                return f"Error: ANY.RUN returned a {artifact} URL outside its own hosts: {_trunc(url, 200)}"
            fetched = await fetch_file(
                client,
                "get",
                url,
                provider=_PROVIDER,
                action=f"download {artifact} of {task_uuid}",
                max_bytes=_MAX_DOWNLOAD_BYTES,
                headers=headers,
                follow_redirects=True,
            )
        if isinstance(fetched, str):
            return f"{fetched}\nDownload URL: {url}"
        data = fetched.data
        suffix, content_type = _ARTIFACTS[artifact]
        numbered = artifact in ("screenshot", "process_dump")
        filename = f"anyrun-{task_uuid}-{f'{index}-' if numbered else ''}{suffix}"
        links = [("Web URL", _task_link(task_uuid)), ("Download URL", url)]
        if artifact in _LIVE_ARTIFACTS:
            return await deliver_live_artefact(
                data=data,
                filename=filename,
                source=_PROVIDER,
                persist_file_fn=persist_file_fn,
                links=links,
            )
        return await deliver_file(
            data=data,
            filename=filename,
            content_type=content_type,
            source=_PROVIDER,
            persist_file_fn=persist_file_fn,
            links=links,
        )

    async def get_anyrun_environment() -> str:
        """List the sandbox OS/environment options available to this account."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{ANYRUN_API}/environment",
                _PROVIDER,
                "get environment",
                headers=headers,
            )
            if err:
                return err
            payload = resp.json() or {}
        data = payload.get("data") if isinstance(payload, dict) else payload
        return (
            "ANY.RUN available sandbox environments (OS versions, bitness, "
            "software sets):\n" + _trunc(data, 3000)
        )

    # ══════════════════════════════════════════════════════════════════════
    # 2. SANDBOX DETONATION (WRITE)
    # ══════════════════════════════════════════════════════════════════════

    async def submit_anyrun_url_analysis(
        url: str,
        env_os: str = "windows",
        env_version: str = "10",
        env_bitness: int = 64,
        env_type: str = "complete",
        opt_network_connect: bool = True,
        opt_privacy_type: str = "bylink",
        opt_timeout: int = 120,
    ) -> str:
        """Detonate a URL in the ANY.RUN interactive sandbox (starts a new task)."""
        url = (url or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            return "Error: a valid http(s) URL is required (e.g. https://example.com)."
        body = _submit_body(
            "url",
            obj_url=url,
            env_os=env_os,
            env_version=env_version,
            env_bitness=env_bitness,
            env_type=env_type,
            opt_network_connect=opt_network_connect,
            opt_privacy_type=opt_privacy_type,
            opt_timeout=max(10, min(int(opt_timeout or 120), 660)),
        )
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{ANYRUN_API}/analysis",
                _PROVIDER,
                f"submit URL {_trunc(url, 120)}",
                headers=headers,
                json=body,
            )
            if err:
                return err
            payload = (resp.json() or {}).get("data") or {}
        task_uuid = payload.get("taskid") or payload.get("uuid") or ""
        if not task_uuid:
            return "URL submitted, but ANY.RUN returned no task UUID."
        return (
            f"URL submitted to ANY.RUN for analysis.\nTask UUID: {task_uuid}\n"
            f"Interactive session: {_task_link(task_uuid)}\n"
            "Poll get_anyrun_analysis_report with this UUID until the verdict is "
            "available (detonation takes ~1-2 minutes)."
        )

    async def submit_anyrun_file_analysis(
        file_id: str,
        env_os: str = "windows",
        env_version: str = "10",
        env_bitness: int = 64,
        env_type: str = "complete",
        opt_network_connect: bool = True,
        opt_privacy_type: str = "bylink",
        opt_timeout: int = 120,
        file_context: FileContext | None = None,
    ) -> str:
        """Detonate a file attached in this conversation in the ANY.RUN sandbox."""
        file_id = (file_id or "").strip()
        if not file_id:
            return "Error: file_id (a file attached in this conversation) is required."
        if file_context is None:
            return "Error: no files are available in this conversation."
        try:
            resolved = await file_context.resolve_upload(file_id)
        except Exception as exc:  # pragma: no cover - defensive
            return f"Error: could not read attached file {_trunc(file_id, 120)}: {_trunc(exc, 200)}"
        if not resolved:
            return f"Error: file '{_trunc(file_id, 120)}' is not available in this conversation."
        data, filename, mime = resolved
        # The multipart name is cut; the answers name the file by its id, and
        # its stored name reaches the model only in the closing block.
        display_name = _trunc(filename, 120) or "sample"
        subject = f"file '{_trunc(file_id, 120)}'"

        body = _submit_body(
            "file",
            obj_url=None,
            env_os=env_os,
            env_version=env_version,
            env_bitness=env_bitness,
            env_type=env_type,
            opt_network_connect=opt_network_connect,
            opt_privacy_type=opt_privacy_type,
            opt_timeout=max(10, min(int(opt_timeout or 120), 660)),
        )
        # obj_type=file is sent as a multipart form field alongside the file.
        # Booleans must be the lowercase strings the form API documents
        # ("true"/"false"), not Python's str(True) == "True".
        form = {
            k: ("true" if v is True else "false" if v is False else str(v))
            for k, v in body.items()
        }
        async with _client(timeout=120) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{ANYRUN_API}/analysis",
                _PROVIDER,
                f"submit {subject}",
                headers=headers,
                data=form,
                files={"file": (display_name, data, mime or "application/octet-stream")},
            )
            if err:
                return err
            payload = (resp.json() or {}).get("data") or {}
        task_uuid = payload.get("taskid") or payload.get("uuid") or ""
        size_kb = round(len(data) / 1024, 1)
        if not task_uuid:
            return uploaded_file_answer(
                f"Uploaded {subject} ({size_kb} KB), but ANY.RUN returned no task UUID.",
                target=_PROVIDER,
                filename=filename,
            )
        return uploaded_file_answer(
            f"Submitted {subject} ({size_kb} KB) to ANY.RUN for analysis.\n"
            f"Task UUID: {task_uuid}\nInteractive session: {_task_link(task_uuid)}\n"
            "Poll get_anyrun_analysis_report with this UUID until the verdict is "
            "available (detonation takes ~1-2 minutes).",
            target=_PROVIDER,
            filename=filename,
        )

    async def add_anyrun_analysis_time(task_uuid: str) -> str:
        """Add 60 seconds of execution time to a running analysis you own."""
        task_uuid = (task_uuid or "").strip()
        if not is_valid_uuid(task_uuid):
            return "Error: a valid task UUID is required."
        async with _client() as client:
            _resp, err = await safe_request(
                client,
                "patch",
                f"{ANYRUN_API}/analysis/addtime/{task_uuid}",
                _PROVIDER,
                f"add time {task_uuid}",
                headers=headers,
            )
            if err:
                return err
        return f"Added 60 seconds of execution time to task {task_uuid}."

    async def stop_anyrun_analysis(task_uuid: str) -> str:
        """Stop a running analysis you own (ends detonation early)."""
        task_uuid = (task_uuid or "").strip()
        if not is_valid_uuid(task_uuid):
            return "Error: a valid task UUID is required."
        async with _client() as client:
            _resp, err = await safe_request(
                client,
                "patch",
                f"{ANYRUN_API}/analysis/stop/{task_uuid}",
                _PROVIDER,
                f"stop task {task_uuid}",
                headers=headers,
            )
            if err:
                return err
        return f"Requested stop of running task {task_uuid}."

    # ══════════════════════════════════════════════════════════════════════
    # 3. SANDBOX CLEANUP (DESTRUCTIVE)
    # ══════════════════════════════════════════════════════════════════════

    async def delete_anyrun_analysis(task_uuid: str) -> str:
        """Delete an analysis from your history. Permanent, cannot be undone."""
        task_uuid = (task_uuid or "").strip()
        if not is_valid_uuid(task_uuid):
            return "Error: a valid task UUID is required."
        async with _client() as client:
            _resp, err = await safe_request(
                client,
                "delete",
                f"{ANYRUN_API}/analysis/delete/{task_uuid}",
                _PROVIDER,
                f"delete task {task_uuid}",
                headers=headers,
            )
            if err:
                return err
        return f"Deleted task {task_uuid} from the ANY.RUN history."

    # ══════════════════════════════════════════════════════════════════════
    # 4. THREAT INTELLIGENCE
    # ══════════════════════════════════════════════════════════════════════

    async def lookup_anyrun_ti(
        query: str = "",
        sha256: str = "",
        sha1: str = "",
        md5: str = "",
        domain: str = "",
        ip: str = "",
        url: str = "",
        threat_name: str = "",
        mitre: str = "",
        lookup_depth: int = 30,
    ) -> str:
        """Search ANY.RUN Threat Intelligence Lookup by IOC or structured filters."""
        # A raw query takes priority; otherwise AND-join the structured filters.
        raw_query = (query or "").strip()
        structured = {
            "sha256": sha256,
            "sha1": sha1,
            "md5": md5,
            "domain": domain,
            "ip": ip,
            "url": url,
            "threat_name": threat_name,
            "mitre": mitre,
        }
        if not raw_query:
            clauses = [
                f'{_TI_FILTER_FIELDS[k]}:"{str(v).strip()}"'
                for k, v in structured.items()
                if str(v or "").strip()
            ]
            if not clauses:
                return (
                    "Error: provide a raw 'query' or at least one filter "
                    "(sha256/sha1/md5/domain/ip/url/threat_name/mitre)."
                )
            raw_query = " AND ".join(clauses)

        depth = max(1, min(int(lookup_depth or 30), 180))
        now = datetime.now(UTC).date()
        body = {
            "query": raw_query,
            "startDate": (now - timedelta(days=depth)).strftime("%Y-%m-%d"),
            "endDate": now.strftime("%Y-%m-%d"),
        }
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{ANYRUN_API}/intelligence/api/search",
                _PROVIDER,
                f"TI lookup {_trunc(raw_query, 120)}",
                headers=headers,
                json=body,
            )
            if err:
                return err
            payload = resp.json() or {}
        return _format_ti_lookup(payload, raw_query)

    async def search_anyrun_yara(yara_rule: str) -> str:
        """Start a YARA search over the ANY.RUN public sample database."""
        yara_rule = (yara_rule or "").strip()
        if not yara_rule:
            return "Error: a valid YARA rule is required."
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{ANYRUN_API}/intelligence/yara-lookup/search",
                _PROVIDER,
                "start YARA search",
                headers=headers,
                json={"query": yara_rule},
            )
            if err:
                return err
            payload = resp.json() or {}
        # The YARA lookup API returns the search id as a top-level "queryId"
        # (per the official SDK); older/wrapped shapes are kept as fallbacks.
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            data = {}
        search_uuid = (
            (payload.get("queryId") if isinstance(payload, dict) else None)
            or data.get("queryId")
            or data.get("searchId")
            or data.get("uuid")
            or ""
        )
        if not search_uuid:
            return "YARA search started, but ANY.RUN returned no search id."
        return (
            f"YARA search started.\nSearch id: {search_uuid}\n"
            "Poll get_anyrun_yara_results with this id until the search completes."
        )

    async def get_anyrun_yara_results(search_uuid: str) -> str:
        """Get the status and matching samples of a YARA search by id."""
        search_uuid = (search_uuid or "").strip()
        if not is_valid_uuid(search_uuid):
            return "Error: a valid YARA search id is required (from search_anyrun_yara)."
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{ANYRUN_API}/intelligence/yara-lookup/search/{search_uuid}",
                _PROVIDER,
                f"get YARA results {search_uuid}",
                headers=headers,
            )
            if err:
                return err
            payload = resp.json() or {}
        # Matches live in the top-level "matches" key (per the official SDK);
        # searchInfo.status tells a finished empty search from a running one.
        status = ""
        matches: list = []
        if isinstance(payload, dict):
            info = payload.get("searchInfo")
            if isinstance(info, dict):
                status = str(info.get("status") or "").lower()
            data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
            raw = (
                payload.get("matches")
                or data.get("matches")
                or data.get("results")
                or data.get("tasks")
                or []
            )
            if isinstance(raw, list):
                matches = raw
        elif isinstance(payload, list):
            matches = payload

        def _fmt(match: dict) -> str:
            uuid = match.get("uuid") or match.get("taskId") or match.get("id") or "?"
            name = match.get("name") or match.get("fileName") or "?"
            verdict = match.get("verdict") or match.get("threatLevelText") or "?"
            return f"  {uuid}: {_trunc(name, 90)} | verdict: {verdict}"

        if not matches:
            if status == "done":
                return f"YARA search {search_uuid} completed: no matching samples."
            return (
                f"YARA search {search_uuid}: no matches yet"
                + (f" (status: {status})" if status else "")
                + ". The search may still be running - poll again in a few seconds."
            )
        return format_entity_list(
            matches,
            _fmt,
            noun="YARA match",
            total=len(matches),
            empty_msg="No matching samples.",
        )

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    executor.register("get_anyrun_status", get_anyrun_status)
    executor.register("list_anyrun_analyses", list_anyrun_analyses)
    executor.register("get_anyrun_analysis_report", get_anyrun_analysis_report)
    executor.register("get_anyrun_analysis_iocs", get_anyrun_analysis_iocs)
    executor.register("download_anyrun_analysis_file", download_anyrun_analysis_file)
    executor.register("get_anyrun_environment", get_anyrun_environment)
    executor.register("lookup_anyrun_ti", lookup_anyrun_ti)
    executor.register("get_anyrun_yara_results", get_anyrun_yara_results)
    executor.register("submit_anyrun_url_analysis", submit_anyrun_url_analysis)
    executor.register("submit_anyrun_file_analysis", submit_anyrun_file_analysis)
    executor.register("search_anyrun_yara", search_anyrun_yara)
    executor.register("add_anyrun_analysis_time", add_anyrun_analysis_time)
    executor.register("stop_anyrun_analysis", stop_anyrun_analysis)
    executor.register("delete_anyrun_analysis", delete_anyrun_analysis)

    _env_props = {
        "env_os": {
            "type": "string",
            "enum": ["windows", "linux", "android"],
            "default": "windows",
            "description": "Sandbox operating system",
        },
        "env_version": {
            "type": "string",
            "default": "10",
            "description": "OS version, e.g. Windows '7', '10', '11'",
        },
        "env_bitness": {
            "type": "integer",
            "enum": [32, 64],
            "default": 64,
            "description": "OS bitness",
        },
        "env_type": {
            "type": "string",
            "enum": ["complete", "clean", "office", "development"],
            "default": "complete",
            "description": "Installed software set",
        },
        "opt_network_connect": {
            "type": "boolean",
            "default": True,
            "description": "Enable internet connection in the sandbox",
        },
        "opt_privacy_type": {
            "type": "string",
            "enum": ["public", "bylink", "owner", "byteam"],
            "default": "bylink",
            "description": "Who can see the analysis result",
        },
        "opt_timeout": {
            "type": "integer",
            "default": 120,
            "description": "Detonation time in seconds (10-660)",
        },
    }

    return [
        ToolDefinition(
            name="get_anyrun_status",
            description=(
                "Validate the ANY.RUN connection and report the account's API "
                "request limits. Use first if other ANY.RUN tools fail or return "
                "permission/quota errors."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_anyrun_analyses",
            description=(
                "List recent submissions from the ANY.RUN analysis history with "
                "their task UUID, verdict, and tags. Use to find a prior "
                "detonation to inspect with get_anyrun_analysis_report, or to "
                "review recent activity. Set team=true for the team history."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "team": {
                        "type": "boolean",
                        "default": False,
                        "description": "List the team history instead of your own",
                    },
                    "skip": {
                        "type": "integer",
                        "default": 0,
                        "description": "Number of tasks to skip (pagination offset)",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 25,
                        "description": "Max tasks to return (1-100)",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_anyrun_analysis_report",
            description=(
                "Get the full report for one analysis by task UUID: the threat "
                "verdict (No threats / Suspicious / Malicious), sample hashes, "
                "tags, MITRE ATT&CK techniques, process-tree summary, network "
                "counters, and the download links of its artefacts (HTML / STIX / "
                "MISP / IOC reports, PCAP, SSL keys, video, screenshots, process "
                "dumps, the sample). This is also how you poll a submitted task for "
                "completion (a running task returns a partial report). Follow up "
                "with get_anyrun_analysis_iocs for the extracted indicators and "
                "download_anyrun_analysis_file to store an artefact."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "task_uuid": {
                        "type": "string",
                        "description": "Task UUID from a submit or list tool",
                    },
                },
                "required": ["task_uuid"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_anyrun_analysis_iocs",
            description=(
                "Get the extracted indicators of compromise (IOCs) for a finished "
                "analysis - hashes, domains, IPs, and URLs - filtered by "
                "reputation. Use after get_anyrun_analysis_report shows a "
                "malicious/suspicious verdict to pull the actionable IOCs."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "task_uuid": {
                        "type": "string",
                        "description": "Task UUID of a finished analysis",
                    },
                    "reputation": {
                        "type": "string",
                        "enum": ["all", "suspicious", "malicious"],
                        "default": "suspicious",
                        "description": "Minimum IOC reputation to include",
                    },
                },
                "required": ["task_uuid"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="download_anyrun_analysis_file",
            workspace_ingest=True,
            description=(
                "Store one artefact of an ANY.RUN analysis as a file of the "
                "conversation (download card + file_id to forward): the HTML, "
                "STIX, MISP or IOC JSON report, the PCAP, the SSL key log, the "
                "video, a screenshot, a process dump, or the submitted sample. "
                "Reports, PCAP, keys, video and screenshots are also read inline. "
                "The sample and process dumps can be live malware: they are "
                "stored only as ANY.RUN's ZIP encrypted with the password "
                "'infected', exactly as it came and never read inline; anything "
                "else gets its link and the reason instead."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "task_uuid": {
                        "type": "string",
                        "description": "Task UUID of the analysis",
                    },
                    "artifact": {
                        "type": "string",
                        "enum": list(_ARTIFACTS),
                        "description": "Which artefact to store",
                    },
                    "index": {
                        "type": "integer",
                        "default": 1,
                        "description": "1-based screenshot or process dump number (see the report)",
                    },
                },
                "required": ["task_uuid", "artifact"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_anyrun_environment",
            description=(
                "List the sandbox environments (OS versions, bitness, software "
                "sets) available to this ANY.RUN account. Use to pick valid "
                "env_os / env_version / env_bitness values before submitting a "
                "detonation."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="lookup_anyrun_ti",
            description=(
                "Search ANY.RUN Threat Intelligence Lookup - a database of IOCs, "
                "IOAs, and events from millions of public sandbox tasks. Pass a "
                "single indicator via the structured filters (sha256/sha1/md5/"
                "domain/ip/url/threat_name/mitre - AND-combined) OR a raw 'query' "
                "using ANY.RUN's query language with AND/OR/NOT and parentheses "
                "(e.g. 'domainName:\"evil.top\" AND threatName:\"Phishing\"'). "
                "Returns the threat verdict, tags, related IOCs, and linked "
                "sample tasks. Use to enrich an indicator with real-world "
                "detonation context."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Raw ANY.RUN TI query (overrides the structured filters)",
                    },
                    "sha256": {"type": "string", "description": "File SHA-256 hash"},
                    "sha1": {"type": "string", "description": "File SHA-1 hash"},
                    "md5": {"type": "string", "description": "File MD5 hash"},
                    "domain": {"type": "string", "description": "Domain name"},
                    "ip": {"type": "string", "description": "Destination IP address"},
                    "url": {"type": "string", "description": "URL"},
                    "threat_name": {
                        "type": "string",
                        "description": "Threat/malware family name, e.g. 'Phishing'",
                    },
                    "mitre": {
                        "type": "string",
                        "description": "MITRE ATT&CK technique id, e.g. 'T1071'",
                    },
                    "lookup_depth": {
                        "type": "integer",
                        "default": 30,
                        "description": "Lookback window in days (1-180)",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_anyrun_yara_results",
            description=(
                "Get the status and matching samples of a YARA search started "
                "with search_anyrun_yara. Returns the task UUIDs of samples whose "
                "content matched the rule (poll until matches appear)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search_uuid": {
                        "type": "string",
                        "description": "Search id returned by search_anyrun_yara",
                    },
                },
                "required": ["search_uuid"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="submit_anyrun_url_analysis",
            description=(
                "Detonate a URL in the ANY.RUN interactive sandbox. Starts a new "
                "analysis task and returns its UUID - poll "
                "get_anyrun_analysis_report until the verdict is ready. This "
                "consumes an API quota unit and runs a live detonation; only "
                "submit URLs you intend to analyse."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "http(s) URL to detonate",
                    },
                    **_env_props,
                },
                "required": ["url"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="submit_anyrun_file_analysis",
            description=(
                "Detonate a file the user attached in this conversation in the "
                "ANY.RUN interactive sandbox. Pass the file_id from the "
                "available-files list; the original bytes are uploaded as-is. "
                "Starts a new task and returns its UUID - poll "
                "get_anyrun_analysis_report for the verdict. NOTE: with the "
                "default 'bylink' privacy the sample may be visible to others - "
                "use opt_privacy_type='owner' for confidential samples. Consumes "
                "an API quota unit."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "file_id": {
                        "type": "string",
                        "description": "id of a file attached in this conversation",
                    },
                    **_env_props,
                },
                "required": ["file_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="search_anyrun_yara",
            description=(
                "Start a YARA search over the ANY.RUN public sample database to "
                "find malware samples whose content matches a YARA rule. Returns "
                "a search id - read the matches with get_anyrun_yara_results. Use "
                "for content-based threat hunting across historical detonations."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "yara_rule": {
                        "type": "string",
                        "description": "A valid YARA rule (full rule text)",
                    },
                },
                "required": ["yara_rule"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="add_anyrun_analysis_time",
            description=(
                "Add 60 seconds of execution time to a running analysis you own. "
                "Use when a detonation needs longer to reveal behaviour. The task "
                "must be currently running and belong to your account."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "task_uuid": {
                        "type": "string",
                        "description": "UUID of your running task",
                    },
                },
                "required": ["task_uuid"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="stop_anyrun_analysis",
            description=(
                "Stop a running analysis you own, ending the detonation early. "
                "The task must be currently running and belong to your account."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "task_uuid": {
                        "type": "string",
                        "description": "UUID of your running task",
                    },
                },
                "required": ["task_uuid"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="delete_anyrun_analysis",
            description=(
                "Delete an analysis from your ANY.RUN history. Permanent and "
                "cannot be undone. The task must belong to your account."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "task_uuid": {
                        "type": "string",
                        "description": "UUID of the task to delete",
                    },
                },
                "required": ["task_uuid"],
            },
            source=_SOURCE,
            priority=2,
        ),
    ]


def _format_ti_lookup(payload: dict, query: str) -> str:
    """Format an ANY.RUN TI Lookup response into a compact summary."""
    if not isinstance(payload, dict):
        return f"No TI Lookup results for query: {query}"
    # Shape-guard every nested field so a malformed response never raises.
    summary = _as_dict(payload.get("summary"))
    threat_level = summary.get("threatLevel")

    related_files = _as_list(payload.get("relatedFiles"))
    related_ips = _as_list(payload.get("destinationIP"))
    related_dns = _as_list(payload.get("relatedDNS"))
    related_urls = _as_list(payload.get("relatedURLs"))
    source_tasks = _as_list(payload.get("sourceTasks"))
    industries = _as_list(payload.get("industries"))

    has_data = any(
        [related_files, related_ips, related_dns, related_urls, source_tasks]
    )
    if not has_data and threat_level is None:
        return (
            f"ANY.RUN TI Lookup: no matches for query: {query}\n"
            "The indicator is not present in ANY.RUN's public sample database."
        )

    lines = [f"ANY.RUN TI Lookup for: {query}"]
    if threat_level is not None:
        lines.append(f"Verdict: {_verdict_icon(threat_level)} {_verdict_label(threat_level)}")
    if summary.get("lastSeen"):
        lines.append(f"Last seen: {summary['lastSeen']}")
    tags = _as_list(summary.get("tags"))
    if tags:
        lines.append(f"Tags: {', '.join(str(t) for t in tags)[:400]}")
    if industries:
        names = [
            f"{i.get('industryName')} ({i.get('confidence')}%)"
            for i in industries[:8]
            if isinstance(i, dict)
        ]
        if names:
            lines.append(f"Targeted industries: {', '.join(names)}")

    lines.append(
        "Related indicators: "
        f"{len(related_files)} files, {len(related_ips)} IPs, "
        f"{len(related_dns)} domains, {len(related_urls)} URLs"
    )

    if related_dns:
        shown = [str(d.get("domainName")) for d in related_dns[:5] if isinstance(d, dict)]
        lines.append(f"  Domains: {', '.join(shown)}" + (" ..." if len(related_dns) > 5 else ""))
    if related_ips:
        shown = [str(i.get("destinationIP")) for i in related_ips[:5] if isinstance(i, dict)]
        lines.append(f"  IPs: {', '.join(shown)}" + (" ..." if len(related_ips) > 5 else ""))
    if related_files:
        shown = []
        for f in related_files[:5]:
            if isinstance(f, dict):
                h = _as_dict(f.get("hashes"))
                shown.append(str(h.get("sha256") or f.get("fileName") or "?"))
        lines.append(f"  Files: {', '.join(shown)}" + (" ..." if len(related_files) > 5 else ""))

    if source_tasks:
        shown = [str(t.get("related")) for t in source_tasks[:5] if isinstance(t, dict)]
        lines.append(f"Linked sandbox tasks: {', '.join(shown)}")
        lines.append(
            "  Inspect any of these with get_anyrun_analysis_report (task UUID)."
        )
    return "\n".join(lines)
