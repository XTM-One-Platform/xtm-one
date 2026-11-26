# SPDX-License-Identifier: Apache-2.0

"""CTEM signal listeners (#826): integration events mapped to a trigger path by a table.

:data:`LISTENERS` is the declarative table: one row per (provider, event key,
object type) that is a change in exposure, naming its trigger path, lens and
subject kind. The webhook routes hand every delivery to :func:`note_event`
after its signature check, saying whether it was verified against a
configured webhook secret; an unverified delivery records nothing (a forged
payload never plants a signal, and an unsigned storm writes no row). A
delivery no row matches is ignored, one that
matches records a signal through ``services/ctem_signals.record_signal``
(recorded by ``system``, idempotent on the source object). No model call, no
run, no quota: a signal is data the reconcile and scope gate read next.

Paths with a listener today: 1 (new vulnerability: an OpenCTI Vulnerability,
or a ``has`` relationship to one), 2 (relevant threat: an OpenCTI Intrusion
Set, Threat Actor, Campaign or Malware), 3 (code flaw: a new open Aikido
issue, an SLA breach on one, a failed CI gate, a container image scan with
issues; CTEM-25; a GitHub Dependabot alert on a repository, #1331; a GitHub
repository security advisory the organization published, path 1, #1410), 8
(mitigation regression: an OpenAEV scenario whose score degraded, CTEM-15),
10 (credential leak: an Aikido ``leaked_secret`` issue, CTEM-25; the secret
itself is never stored). The other paths enter through the agent tool and
the API until their products emit an event (design ``dev-docs/ctem-signals.md`` section 4).

An Aikido ``issue.closed`` records no signal: it closes the member of the
signals that issue opened on their case, and the last member closed resolves
the case on a provider re-scan (``ctem_case_loop.close_signal_members``). A
Dependabot alert fixed or dismissed closes its members the same way.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Listener:
    """One event that is a change in exposure."""

    provider: str
    #: The delivery's event key (``create``, ``issue_open_created``...); ``*`` matches any.
    event_keys: tuple[str, ...]
    #: The object type the event is about (OpenCTI STIX type); empty matches any.
    object_types: tuple[str, ...]
    trigger_path: str
    lens: str
    subject_kind: str
    #: The ``name`` of the source this row feeds when its provider has several on the path (#2436).
    source: str = ""


#: The Aikido deliveries about one issue that record (or re-sight) its signal.
#: An SLA breach re-sights the signal of the same issue: same dedupe key, ``seen_count`` + 1.
AIKIDO_ISSUE_EVENTS: tuple[str, ...] = (
    "issue_open_created",
    "issue_open_created_open_source",
    "issue_open_created_sast",
    "issue_sla_breached",
)
#: The Aikido deliveries that close the members an issue's signals hold on their case.
AIKIDO_CLOSING_EVENTS: tuple[str, ...] = ("issue_closed",)

#: The GitHub Dependabot alert deliveries that record (or re-sight) the alert's signal (#1331, slice 2).
DEPENDABOT_OPEN_EVENTS: tuple[str, ...] = (
    "dependabot_alert.created",
    "dependabot_alert.reopened",
    "dependabot_alert.auto_reopened",
    "dependabot_alert.reintroduced",
)
#: The Dependabot alert deliveries that close the members the alert's signal holds on its case.
DEPENDABOT_CLOSING_EVENTS: tuple[str, ...] = (
    "dependabot_alert.fixed",
    "dependabot_alert.dismissed",
    "dependabot_alert.auto_dismissed",
)

#: The GitHub repository advisory deliveries that record a published advisory's signal (#1410).
#: A ``reported`` advisory (a private report still in triage) is never one: the extractor
#: also reads the advisory's own state and keeps ``published`` only.
REPOSITORY_ADVISORY_EVENTS: tuple[str, ...] = ("repository_advisory.published",)

#: The GitHub secret scanning alert deliveries that record a leaked secret's signal (#2436, path 10).
SECRET_SCANNING_OPEN_EVENTS: tuple[str, ...] = ("secret_scanning_alert.created", "secret_scanning_alert.reopened")
SECRET_SCANNING_CLOSING_EVENTS: tuple[str, ...] = ("secret_scanning_alert.resolved",)
#: The GitHub code scanning alert deliveries that record a code flaw's signal (#2436, path 3).
CODE_SCANNING_OPEN_EVENTS: tuple[str, ...] = (
    "code_scanning_alert.created",
    "code_scanning_alert.reopened",
    "code_scanning_alert.reopened_by_user",
)
CODE_SCANNING_CLOSING_EVENTS: tuple[str, ...] = ("code_scanning_alert.fixed", "code_scanning_alert.closed_by_user")

#: The declarative table. Event keys are the ones the webhook routes compute.
LISTENERS: tuple[Listener, ...] = (
    Listener("opencti", ("create", "update"), ("vulnerability",), "new_vulnerability", "world", "vulnerability"),
    Listener("opencti", ("create",), ("relationship:has",), "new_vulnerability", "inside", "vulnerability"),
    # #2434: a sighting of a vulnerability in the organization's data (the former "Vulnerability
    # sighted" trigger); the evidence says ``stix_type: sighting``, which the fleet routes on.
    Listener("opencti", ("create",), ("sighting",), "new_vulnerability", "inside", "vulnerability"),
    Listener(
        "opencti",
        ("create", "update"),
        ("intrusion-set", "threat-actor", "threat-actor-group", "campaign", "malware"),
        "relevant_threat",
        "world",
        "threat",
    ),
    # CTEM-25: a leaked secret is a way in with no vulnerability (path 10); the row
    # comes first so an Aikido ``leaked_secret`` issue never lands on path 3.
    Listener("aikido", AIKIDO_ISSUE_EVENTS, ("leaked_secret",), "credential_leak", "world", "code"),
    Listener("aikido", AIKIDO_ISSUE_EVENTS, (), "code_flaw", "inside", "code"),
    Listener("aikido", ("ci_gate_failed", "scan_image_finished"), ("scan",), "code_flaw", "inside", "code"),
    # #1331 slice 2: a Dependabot alert on a repository synced as an asset (path 3, a
    # dependency flaw before production); the repository is the asset the case opens on.
    Listener("github", DEPENDABOT_OPEN_EVENTS, (), "code_flaw", "inside", "code"),
    # #1410: an advisory the organization published on one of its repositories is a new
    # vulnerability in a product it ships (path 1); the repository is the asset it names.
    Listener("github", REPOSITORY_ADVISORY_EVENTS, (), "new_vulnerability", "inside", "vulnerability"),
    # #2436: a secret GitHub found in a repository (path 10; the secret itself is never read) and
    # a code scanning alert (path 3), each off until an administrator switches its source on.
    Listener(
        "github",
        SECRET_SCANNING_OPEN_EVENTS,
        ("secret_scanning",),
        "credential_leak",
        "world",
        "code",
        "secret_scanning",
    ),
    Listener("github", CODE_SCANNING_OPEN_EVENTS, ("code_scanning",), "code_flaw", "inside", "code", "code_scanning"),
    # #2436: a sighting of an indicator in the organization's data is a threat seen inside.
    Listener(
        "opencti", ("create",), ("sighting:indicator",), "relevant_threat", "inside", "threat", "indicator_sighting"
    ),
    # CTEM-26: OpenAEV reports a failed reachability assertion (a target that should not answer
    # answers) as the scenario's SCORE_DEGRADATION; the receiver stamps the assertion flag
    # (ctem_openaev_events.stamp_reachability_assertion), so it is an outside-lens exposure (path 5).
    Listener("openaev", ("scenario_score_degraded",), ("scenario:assertion",), "new_exposure", "outside", "asset"),
    Listener("openaev", ("scenario_score_degraded",), ("scenario",), "mitigation_regression", "inside", "asset"),
    # CTEM-13: a finished simulation whose attack path reached a high or very high criticality
    # asset (path 4). OpenAEV names the simulation only, never the asset: the subject is the run.
    Listener("openaev", ("critical_asset_reached",), ("simulation",), "proven_exploitable", "inside", "asset"),
    # CTEM-27: OpenAEV's freshness job turned an asset STALE (unseen past the tenant's
    # threshold); the provisioned trigger filters on asset_status, so only that transition arrives.
    Listener("openaev", ("asset_stale",), ("asset",), "stale_asset", "outside", "asset"),
    # #2436: a finding OpenAEV created on an asset (path 5) and an asset an OpenAEV scan found
    # (path 7), each off until an administrator switches its source on.
    Listener("openaev", ("finding_created",), ("finding",), "new_exposure", "outside", "asset", "finding_created"),
    Listener("openaev", ("asset_created",), ("asset",), "surface_drift", "outside", "asset", "asset_created"),
    # CTEM-29: an account or an address a leak source wrote into OpenCTI (path 10, the identity
    # as subject). Only the login or address and the disabled flag are kept, never a secret.
    Listener("opencti", ("create", "update"), ("user-account", "email-addr"), "credential_leak", "world", "identity"),
)


def paths_with_listener() -> frozenset[str]:
    """The trigger paths at least one listener records."""
    return frozenset(row.trigger_path for row in LISTENERS)


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _opencti_object(payload: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    data = payload.get("data")
    if not isinstance(data, dict):
        return "", {}
    kind = _text(data.get("type")).lower()
    if kind == "relationship":
        kind = f"relationship:{_text(data.get('relationship_type')).lower()}"
    return kind, data


#: Where an OpenCTI account or address observable carries the identity, first found wins.
_IDENTITY_KEYS = ("value", "account_login", "user_id", "display_name")


def _identity_signal(kind: str, data: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """CTEM-29: a leaked identity (an email address or a user account) as a path 10 signal.

    Kept only when the identity carries a domain (``name@domain``): the scope gate
    reads it against the profile's owned domains. The account's disabled flag is
    carried so a disabled account is closed "not exposed"; a credential value is
    never read from the delivery.
    """
    ref = next((_text(data.get(k)).lower() for k in _IDENTITY_KEYS if "@" in _text(data.get(k))), "")
    if not ref:
        return None
    evidence: dict[str, Any] = {"event": _text(payload.get("type")), "stix_type": kind}
    if isinstance(data.get("is_disabled"), bool):
        evidence["account_disabled"] = data["is_disabled"]
    meta = payload.get("_opencti_event")
    if isinstance(meta, dict) and _text(meta.get("message")):
        evidence["message"] = _text(meta.get("message"))[:500]
    return {"external_id": _text(data.get("id")) or None, "ref": ref, "label": ref, "evidence": evidence}


def _opencti_signal(row: Listener, payload: Mapping[str, Any]) -> dict[str, Any] | None:
    kind, data = _opencti_object(payload)
    if kind == "sighting" and _text(data.get("sighting_of_ref")).startswith("indicator--"):
        # #2436: a sighting of an indicator is a threat seen in your data, not a vulnerability's.
        kind = "sighting:indicator"
    if row.object_types and kind not in row.object_types:
        return None
    if row.subject_kind == "identity":
        return _identity_signal(kind, data, payload)
    external_id = _text(data.get("id"))
    if kind == "sighting:indicator":
        return _indicator_sighting_signal(external_id, data, payload)
    if kind.startswith("relationship:") or kind == "sighting":
        target = _text(data.get("sighting_of_ref" if kind == "sighting" else "target_ref"))
        if not target.startswith("vulnerability--"):
            return None
        ref, label = target, None
    else:
        ref = _text(data.get("name")) or external_id
        label = _text(data.get("name")) or None
    if not ref:
        return None
    evidence: dict[str, Any] = {"event": _text(payload.get("type")), "stix_type": kind}
    for key in ("x_opencti_cvss_base_score", "x_opencti_epss_score", "x_opencti_cisa_kev", "source_ref"):
        if data.get(key) not in (None, ""):
            evidence[key.removeprefix("x_opencti_")] = data[key]
    cpes = _cpes_of(data)
    if cpes:
        # #1432: the CPEs a vulnerability names, so the scope gate resolves it to a shipped product.
        evidence["cpe"] = cpes
    if kind.startswith("relationship:"):
        # CTEM-08: the software or asset the relationship starts from, so the scope
        # gate resolves the signal to the software product it names.
        from app.services.ctem_trigger_rules import source_name

        source = source_name(payload)
        if source:
            evidence["software"] = [source]
    pirs = _pir_matches(data, payload)
    if pirs:
        evidence["pir"] = pirs
    meta = payload.get("_opencti_event")
    if isinstance(meta, dict) and _text(meta.get("message")):
        evidence["message"] = _text(meta.get("message"))[:500]
    return {"external_id": external_id or None, "ref": ref, "label": label, "evidence": evidence}


def _indicator_sighting_signal(
    external_id: str, data: Mapping[str, Any], payload: Mapping[str, Any]
) -> dict[str, Any] | None:
    """#2436: an indicator sighted in the organization's data as a path 2 signal (inside lens).

    The subject is the sighted indicator (its STIX id); the sighting's own id is the dedupe
    key, so each sighting records once. Where it was seen (``where_sighted_refs``) and how often
    go to the evidence; the scope gate resolves the places to the organization's assets.
    """
    target = _text(data.get("sighting_of_ref"))
    if not target or not external_id:
        return None
    evidence: dict[str, Any] = {"event": _text(payload.get("type")), "stix_type": "sighting", "indicator": target}
    where = [w.strip()[:200] for w in data.get("where_sighted_refs") or [] if isinstance(w, str) and w.strip()]
    if where:
        evidence["where_sighted"] = where[:20]
    count = data.get("count")
    if isinstance(count, int) and not isinstance(count, bool):
        evidence["count"] = count
    for key in ("first_seen", "last_seen"):
        if _text(data.get(key)):
            evidence[key] = _text(data.get(key))
    meta = payload.get("_opencti_event")
    label = None
    if isinstance(meta, dict) and _text(meta.get("message")):
        evidence["message"] = _text(meta.get("message"))[:500]
        label = evidence["message"][:300]
    return {"external_id": external_id, "ref": target, "label": label, "evidence": evidence}


#: The fields of an OpenCTI object that may carry CPE strings (#1432), read when present.
_CPE_FIELDS: tuple[str, ...] = ("x_opencti_cpe", "x_opencti_cpes", "cpe", "cpes")


def _cpes_of(data: Mapping[str, Any]) -> list[str]:
    """The CPE strings an object carries in :data:`_CPE_FIELDS`, each once (at most 20)."""
    found: list[str] = []
    for key in _CPE_FIELDS:
        value = data.get(key)
        values = [value] if isinstance(value, str) else value if isinstance(value, list) else []
        found += [v.strip()[:300] for v in values if isinstance(v, str) and v.strip().lower().startswith("cpe:")]
    return list(dict.fromkeys(found))[:20]


def _pir_matches(data: Mapping[str, Any], payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """CTEM-05: the PIRs an OpenCTI entity is flagged in, as ``[{pir_id, score_at_delivery}]``.

    Read from ``pir_information`` in the entity's OpenCTI extension, which a
    delivery of the provisioned PIR playbook carries (``xtm_source``
    ``pir_playbook``). Ids and score only: the matched criteria and the causing
    relationship stay in OpenCTI, under its own rights model. On a score change
    OpenCTI delivers the entity before its PIR information is rewritten, so the
    score is the one at delivery, possibly the previous one: the fleet reads the
    live score from OpenCTI before acting on it.
    """
    if payload.get("xtm_source") != "pir_playbook":
        return []
    extensions = data.get("extensions")
    found: list[dict[str, Any]] = []
    for ext in extensions.values() if isinstance(extensions, dict) else []:
        info = ext.get("pir_information") if isinstance(ext, dict) else None
        for item in info if isinstance(info, list) else []:
            pir_id = _text(item.get("pir_id")) if isinstance(item, dict) else ""
            score = item.get("pir_score") if isinstance(item, dict) else None
            if pir_id and isinstance(score, int | float) and not isinstance(score, bool):
                found.append({"pir_id": pir_id, "score_at_delivery": score})
    return sorted(found, key=lambda p: p["pir_id"])


#: Where an Aikido delivery may carry the issue (the body nests it, or not).
_AIKIDO_ID_KEYS = ("issue_id", "id", "issue_group_id")
#: Where a CI gate or an image scan carries its own id (the dedupe key of its signal).
_AIKIDO_SCAN_ID_KEYS = ("scan_id", "ci_scan_id", "id")
_AIKIDO_SUBJECT_KEYS = ("affected_package", "affected_file", "code_repo_name", "container_repo_name", "domain", "title")
#: The repository and image an issue sits on: written to ``evidence.assets``, so the scope
#: gate resolves them to a profile asset row (source ``aikido``, its name or external id)
#: and, through the primary asset's asset names, to the primary asset (CTEM-25).
_AIKIDO_ASSET_KEYS = ("code_repo_name", "code_repo_id", "container_repo_name", "container_repo_id")
#: Facts copied to the evidence as they are. Never the secret: a ``leaked_secret`` issue
#: keeps its type, file and repository, the source link stays in Aikido.
_AIKIDO_FACT_KEYS = (
    "severity",
    "severity_score",
    "type",
    "cve_id",
    "code_repo_name",
    "container_repo_name",
    "affected_package",
    "affected_file",
    "installed_version",
    "status",
)
#: Where a delivery may name the version that fixes the issue (an upgrade case keys on it).
_AIKIDO_FIXED_KEYS = ("patched_version", "fixed_version", "fix_version")
#: Counts a scan delivery may carry; when one is present and every one is zero, the scan found nothing.
_AIKIDO_ISSUE_COUNT_KEYS = ("issue_count", "issues_count", "new_issues", "open_issues", "open_issue_count")


def _aikido_bodies(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload.get("data")
    return [b for b in (inner, payload) if isinstance(b, dict)]


def aikido_issue_id(payload: Mapping[str, Any]) -> str:
    """The Aikido issue id a delivery is about, ``""`` when it names none."""
    bodies = _aikido_bodies(payload)
    return next((str(b[k]) for b in bodies for k in _AIKIDO_ID_KEYS if b.get(k) not in (None, "")), "")


def _found_nothing(bodies: list[dict[str, Any]]) -> bool:
    counts = [b[k] for b in bodies for k in _AIKIDO_ISSUE_COUNT_KEYS if k in b]
    numbers = [c for c in counts if isinstance(c, int) and not isinstance(c, bool)]
    return bool(numbers) and len(numbers) == len(counts) and not any(numbers)


def _aikido_signal(row: Listener, payload: Mapping[str, Any]) -> dict[str, Any] | None:
    bodies = _aikido_bodies(payload)
    issue_type = next((_text(b.get("type")).lower() for b in bodies if _text(b.get("type"))), "")
    scan = "scan" in row.object_types
    if scan:
        if _found_nothing(bodies):
            return None
        ids = _AIKIDO_SCAN_ID_KEYS
    else:
        if row.object_types and issue_type not in row.object_types:
            return None
        ids = _AIKIDO_ID_KEYS
    external_id = next((str(b[k]) for b in bodies for k in ids if b.get(k) not in (None, "")), "")
    ref = next((_text(b.get(k)) for b in bodies for k in _AIKIDO_SUBJECT_KEYS if _text(b.get(k))), "")
    if not ref and not external_id:
        return None
    event = _text(payload.get("event_type"))
    evidence: dict[str, Any] = {"event": event}
    for body in bodies:
        for key in _AIKIDO_FACT_KEYS:
            if body.get(key) not in (None, "") and key not in evidence:
                evidence[key] = body[key]
    assets = [str(b[k]).strip() for b in bodies for k in _AIKIDO_ASSET_KEYS if str(b.get(k) or "").strip()]
    if assets:
        evidence["assets"] = list(dict.fromkeys(assets))
    fixed = next((_text(b.get(k)) for b in bodies for k in _AIKIDO_FIXED_KEYS if _text(b.get(k))), "")
    if fixed and not scan:
        evidence["fixed_version"] = fixed
    if event == "issue.sla.breached":
        evidence["sla_breached"] = True
    if scan:
        # A gate or a scan is about its repository or image, not one issue: one signal per scan.
        ref = next((_text(b.get(k)) for b in bodies for k in _AIKIDO_ASSET_KEYS if _text(b.get(k))), ref)
    return {
        "external_id": external_id or None,
        "ref": ref or f"aikido issue {external_id}",
        "label": ref or None,
        "evidence": evidence,
    }


def _openaev_signal(row: Listener, payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """A provisioned OpenAEV delivery, normalized by the route into ``_openaev_event``."""
    meta = payload.get("_openaev_event")
    if not isinstance(meta, dict):
        return None
    kind = _text(meta.get("resource_type")).lower()
    assertion = kind == "scenario" and meta.get("reachability_assertion") is True
    if row.object_types and (f"{kind}:assertion" if assertion else kind) not in row.object_types:
        return None
    external_id = _text(meta.get("resource_id"))
    if not external_id:
        return None
    message = _text(meta.get("message"))[:300]
    evidence: dict[str, Any] = {"event": _text(meta.get("event_type")), "resource_type": kind}
    if assertion:
        evidence["reachability_assertion"] = True
    if message:
        evidence["message"] = message
    url = _text(meta.get("platform_url"))
    if url:
        evidence["platform_url"] = url
    return {
        "external_id": external_id,
        "ref": f"openaev {kind or 'object'} {external_id}",
        "label": message or None,
        "evidence": evidence,
    }


def _dependabot_parts(payload: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    alert = payload.get("alert")
    repository = payload.get("repository")
    return (alert if isinstance(alert, dict) else {}), (repository if isinstance(repository, dict) else {})


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def dependabot_alert_id(payload: Mapping[str, Any]) -> str:
    """The dedupe id of a Dependabot alert: ``<repository id>:<alert number>``, ``""`` when either is missing.

    Alert numbers restart in every repository, so the repository id is part of the key.
    """
    alert, repository = _dependabot_parts(payload)
    number, repo_id = alert.get("number"), repository.get("id")
    if number in (None, "") or repo_id in (None, ""):
        return ""
    return f"{repo_id}:{number}"


def _dependabot_signal(row: Listener, payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """A GitHub Dependabot alert as a path 3 signal on its repository (#1331, slice 2).

    The repository's full name, its id and its identity key (``github.com:repository:<id>``,
    the key the GitHub asset source writes) go to ``evidence.assets``, so the scope gate
    resolves the signal to the repository asset. The package, the vulnerable range and the
    first patched version come from the alert; nothing is written back to GitHub.
    """
    from app.services.organization_context_code import GITHUB_HOST, repository_key

    alert, repository = _dependabot_parts(payload)
    external_id = dependabot_alert_id(payload)
    if not external_id:
        return None
    dependency = _dict(alert.get("dependency"))
    package = _dict(dependency.get("package"))
    advisory = _dict(alert.get("security_advisory"))
    vulnerability = _dict(alert.get("security_vulnerability"))
    patched = _dict(vulnerability.get("first_patched_version"))
    name = _text(package.get("name"))
    full_name = _text(repository.get("full_name"))
    evidence: dict[str, Any] = {"event": f"dependabot_alert.{_text(payload.get('action'))}"}
    facts = {
        "affected_package": name,
        "ecosystem": _text(package.get("ecosystem")),
        "manifest_path": _text(dependency.get("manifest_path")),
        "scope": _text(dependency.get("scope")),
        "vulnerable_range": _text(vulnerability.get("vulnerable_version_range")),
        "severity": _text(vulnerability.get("severity") or advisory.get("severity")).lower(),
        "ghsa_id": _text(advisory.get("ghsa_id")),
        "cve_id": _text(advisory.get("cve_id")),
        "summary": _text(advisory.get("summary"))[:300],
        "repository": full_name,
        "alert_url": _text(alert.get("html_url")),
    }
    evidence.update({k: v for k, v in facts.items() if v})
    fixed = _text(patched.get("identifier"))[:100]
    if fixed:
        evidence["fixed_version"] = fixed
    repo_id = repository.get("id")
    assets = [full_name]
    if repo_id not in (None, ""):
        assets += [str(repo_id).strip(), repository_key(GITHUB_HOST, repo_id)]
    evidence["assets"] = list(dict.fromkeys(a for a in assets if a))
    ref = name or full_name or f"dependabot alert {external_id}"
    return {"external_id": external_id, "ref": ref, "label": ref, "evidence": evidence}


def repository_advisory_id(payload: Mapping[str, Any]) -> str:
    """The dedupe id of a repository advisory: ``<repository id>:<GHSA id>``, ``""`` when either is missing."""
    advisory, repository = _dict(payload.get("repository_advisory")), _dict(payload.get("repository"))
    ghsa, repo_id = _text(advisory.get("ghsa_id")), repository.get("id")
    if not ghsa or repo_id in (None, ""):
        return ""
    return f"{repo_id}:{ghsa}"


def _advisory_packages(advisory: Mapping[str, Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for entry in advisory.get("vulnerabilities") or []:
        if not isinstance(entry, dict):
            continue
        package = _dict(entry.get("package"))
        row = {
            "name": _text(package.get("name"))[:200],
            "ecosystem": _text(package.get("ecosystem"))[:50],
            "vulnerable_range": _text(entry.get("vulnerable_version_range"))[:200],
            "patched_versions": _text(entry.get("patched_versions"))[:200],
        }
        row = {k: v for k, v in row.items() if v}
        if row:
            out.append(row)
    return out[:20]


def _repository_advisory_signal(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """A published repository security advisory as a path 1 signal (#1410).

    The organization published a vulnerability in its own repository, a product it
    ships. The subject is the CVE id when there is one, else the GHSA id, so the same
    CVE from OpenCTI lands on the same subject; the repository goes to ``evidence.assets``
    (full name, id, identity key) as for a Dependabot alert. Only an advisory in state
    ``published`` counts: a private report or a draft records nothing.
    """
    from app.services.organization_context_code import GITHUB_HOST, repository_key

    advisory, repository = _dict(payload.get("repository_advisory")), _dict(payload.get("repository"))
    if _text(advisory.get("state")).lower() != "published":
        return None
    external_id = repository_advisory_id(payload)
    if not external_id:
        return None
    ghsa, cve = _text(advisory.get("ghsa_id")), _text(advisory.get("cve_id")).upper()
    full_name = _text(repository.get("full_name"))
    packages = _advisory_packages(advisory)
    evidence: dict[str, Any] = {"event": f"repository_advisory.{_text(payload.get('action'))}"}
    facts = {
        "ghsa_id": ghsa,
        "cve": cve,
        "severity": _text(advisory.get("severity")).lower(),
        "summary": _text(advisory.get("summary"))[:300],
        "advisory_url": _text(advisory.get("html_url")),
        "published_at": _text(advisory.get("published_at")),
        "repository": full_name,
    }
    evidence.update({k: v for k, v in facts.items() if v})
    cvss = _dict(advisory.get("cvss")).get("score")
    if isinstance(cvss, int | float) and not isinstance(cvss, bool):
        evidence["cvss_base_score"] = cvss
    if packages:
        evidence["affected_packages"] = packages
    repo_id = repository.get("id")
    assets = [full_name]
    if repo_id not in (None, ""):
        assets += [str(repo_id).strip(), repository_key(GITHUB_HOST, repo_id)]
    evidence["assets"] = list(dict.fromkeys(a for a in assets if a))
    ref = cve or ghsa
    label = _text(advisory.get("summary"))[:300] or ref
    return {"external_id": external_id, "ref": ref, "label": label, "evidence": evidence}


def scanning_alert_id(payload: Mapping[str, Any]) -> str:
    """The dedupe id of a secret or code scanning alert: ``<repository id>:<kind>:<alert number>``, ``""`` when
    either is missing (numbers restart per repository and per alert kind)."""
    alert, repository = _dict(payload.get("alert")), _dict(payload.get("repository"))
    number, repo_id = alert.get("number"), repository.get("id")
    kind = "secret" if "secret_type" in alert or "secret_type_display_name" in alert else "code"
    if number in (None, "") or repo_id in (None, ""):
        return ""
    return f"{repo_id}:{kind}:{number}"


def _repository_assets(repository: Mapping[str, Any]) -> list[str]:
    from app.services.organization_context_code import GITHUB_HOST, repository_key

    full_name, repo_id = _text(repository.get("full_name")), repository.get("id")
    assets = [full_name]
    if repo_id not in (None, ""):
        assets += [str(repo_id).strip(), repository_key(GITHUB_HOST, repo_id)]
    return list(dict.fromkeys(a for a in assets if a))


def _secret_scanning_signal(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """#2436: a GitHub secret scanning alert as a path 10 signal on its repository.

    The secret type, its validity and the alert link are kept; the secret itself (``secret``) is never read.
    """
    alert, repository = _dict(payload.get("alert")), _dict(payload.get("repository"))
    if "secret_type" not in alert and "secret_type_display_name" not in alert:
        return None
    external_id = scanning_alert_id(payload)
    if not external_id:
        return None
    secret_type = _text(alert.get("secret_type_display_name")) or _text(alert.get("secret_type"))
    full_name = _text(repository.get("full_name"))
    evidence: dict[str, Any] = {"event": f"secret_scanning_alert.{_text(payload.get('action'))}"}
    facts = {
        "secret_type": secret_type[:200],
        "validity": _text(alert.get("validity")).lower(),
        "repository": full_name,
        "alert_url": _text(alert.get("html_url")),
    }
    evidence.update({k: v for k, v in facts.items() if v})
    if isinstance(alert.get("publicly_leaked"), bool):
        evidence["publicly_leaked"] = alert["publicly_leaked"]
    evidence["assets"] = _repository_assets(repository)
    ref = f"{full_name} {secret_type}".strip() or f"secret scanning alert {external_id}"
    return {"external_id": external_id, "ref": ref, "label": ref, "evidence": evidence}


def _code_scanning_signal(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """#2436: a GitHub code scanning alert as a path 3 signal on its repository: rule, severity, tool, file."""
    alert, repository = _dict(payload.get("alert")), _dict(payload.get("repository"))
    rule = _dict(alert.get("rule"))
    if not rule:
        return None
    external_id = scanning_alert_id(payload)
    if not external_id:
        return None
    instance = _dict(alert.get("most_recent_instance"))
    location = _dict(instance.get("location"))
    full_name = _text(repository.get("full_name"))
    evidence: dict[str, Any] = {"event": f"code_scanning_alert.{_text(payload.get('action'))}"}
    facts = {
        "rule_id": _text(rule.get("id"))[:200],
        "rule": _text(rule.get("description"))[:300],
        "severity": _text(rule.get("security_severity_level") or rule.get("severity")).lower(),
        "tool": _text(_dict(alert.get("tool")).get("name"))[:100],
        "affected_file": _text(location.get("path"))[:300],
        "repository": full_name,
        "alert_url": _text(alert.get("html_url")),
    }
    evidence.update({k: v for k, v in facts.items() if v})
    evidence["assets"] = _repository_assets(repository)
    ref = facts["rule_id"] or full_name or f"code scanning alert {external_id}"
    label = facts["rule"] or ref
    return {"external_id": external_id, "ref": ref, "label": label, "evidence": evidence}


def _github_signal(row: Listener, payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """The GitHub row's signal: a repository advisory (path 1), a Dependabot alert (path 3), a secret
    scanning alert (path 10) or a code scanning alert (path 3, #2436)."""
    if row.source == "secret_scanning":
        return _secret_scanning_signal(payload)
    if row.source == "code_scanning":
        return _code_scanning_signal(payload)
    if row.trigger_path == "new_vulnerability":
        return _repository_advisory_signal(payload)
    return _dependabot_signal(row, payload)


_EXTRACTORS: dict[str, Callable[[Listener, Mapping[str, Any]], dict[str, Any] | None]] = {
    "opencti": _opencti_signal,
    "aikido": _aikido_signal,
    "openaev": _openaev_signal,
    "github": _github_signal,
}


def match(provider: str, event_key: str, payload: Mapping[str, Any]) -> tuple[Listener, dict[str, Any]] | None:
    """The listener row and the extracted signal fields of a delivery, ``None`` when it is not one."""
    extract = _EXTRACTORS.get(provider)
    if extract is None:
        return None
    for row in LISTENERS:
        if row.provider != provider or ("*" not in row.event_keys and event_key not in row.event_keys):
            continue
        found = extract(row, payload)
        if found is not None:
            return row, found
    return None


async def note_event(provider: str, event_key: str, payload: Mapping[str, Any], *, verified: bool) -> bool:
    """Record the signal a webhook delivery carries, if any; never raises. ``True`` when one was recorded.

    XTM One mode only, once an organization is set up, and only for a delivery
    *verified* against a configured webhook secret: the route accepts an
    unsigned delivery when no secret is set, and such a delivery is never
    trusted with a signal.
    """
    from app.core.config import settings

    if not verified or settings.is_copilot_like or not isinstance(payload, Mapping):
        return False
    if provider == "aikido" and event_key in AIKIDO_CLOSING_EVENTS:
        await _close_at_source(provider, aikido_issue_id(payload))
        return False
    if provider == "github" and event_key in DEPENDABOT_CLOSING_EVENTS:
        await _close_at_source(provider, dependabot_alert_id(payload))
        return False
    if provider == "github" and event_key in (*SECRET_SCANNING_CLOSING_EVENTS, *CODE_SCANNING_CLOSING_EVENTS):
        # #2436: a resolved secret or a fixed code alert closes the members its signal holds.
        await _close_at_source(provider, scanning_alert_id(payload))
        return False
    found = match(provider, event_key, payload)
    if found is None:
        return False
    row, fields = found
    from app.core.database import async_session
    from app.services import ctem_signals
    from app.services.ctem_loop_stream import publish_signal_update
    from app.services.organizations import get_primary

    try:
        async with async_session() as db:
            primary = await get_primary(db)
            if primary is None:
                return False
            from app.services.ctem_signal_sources import excluded_sources, is_excluded

            # A delivery of the provisioned PIR playbook is the playbook source, any other the listener (#1445).
            kind = "pir_playbook" if payload.get("xtm_source") == "pir_playbook" else "listener"
            if is_excluded(await excluded_sources(db, primary.id), row.trigger_path, provider, kind, row.source):
                return False
            if row.trigger_path in ("relevant_threat", "new_vulnerability") and fields["evidence"].get("pir"):
                from app.services.ctem_trigger_rules import PIR_FLOOR_KEY

                # #2434: a vulnerability's floor too, so its fleet routes on the event, not a trigger.
                floor = "threat" if row.trigger_path == "relevant_threat" else "vulnerability"
                fields["evidence"][PIR_FLOOR_KEY] = await _pir_floor(
                    db, primary.id, fields["evidence"].get("pir"), kind=floor
                )
            elif row.trigger_path == "relevant_threat":
                from app.services.ctem_trigger_rules import PIR_FLOOR_KEY

                fields["evidence"][PIR_FLOOR_KEY] = False
            signal, created = await ctem_signals.record_signal(
                db,
                organization_id=primary.id,
                signal=ctem_signals.SignalInput(
                    trigger_path=row.trigger_path,
                    lens=row.lens,
                    source_provider=provider,
                    source_external_id=fields["external_id"],
                    subject_kind=row.subject_kind,
                    subject_ref=fields["ref"],
                    subject_label=fields["label"],
                    evidence=fields["evidence"],
                ),
                recorded_via="listener",
                recorded_by=ctem_signals.SYSTEM_ACTOR,
            )
            from app.services import ctem_case_loop, ctem_scope_gate_store

            pending = await ctem_scope_gate_store.evaluate_after_record(db, signal, created=created)
            await db.commit()
            await ctem_case_loop.publish_pending(pending)
            await publish_signal_update(db, signal)
            return True
    except Exception:
        logger.warning("CTEM signal from a %s %s delivery not recorded", provider, event_key, exc_info=True)
        return False


async def _pir_floor(db: Any, organization_id: Any, pirs: object, *, kind: str = "threat") -> bool:
    """#1678: whether a PIR score at delivery reaches the policy's threshold for *kind* (threat, vulnerability).

    The relevance floor of the seeded threat signal run: below it the signal
    is kept for the weekly sweep and starts no run. A profile that cannot be
    read holds the policy defaults.
    """
    from app.services import organization_context as org_context
    from app.services.ctem_trigger_rules import pir_floor_met, threshold_for

    if not pirs:
        return False
    try:
        _row, profile = await org_context.current_profile(db, organization_id)
    except Exception:
        logger.warning("CTEM relevance floor: the organization profile was not read", exc_info=True)
        profile = None
    policy = getattr(profile, "prioritization_policy", None)
    # Each PIR's own minimum from the Intelligence policy (#2435), the policy's minimum for *kind* otherwise.
    return pir_floor_met(pirs, threshold_for(policy, kind), getattr(profile, "pir_min_scores", None) or {})


async def note_tracked_organizations(payload: Mapping[str, Any], organization_ids: list[str], *, verified: bool) -> int:
    """CTEM-29: one path 11 signal per tracked organization an OpenCTI report or incident names.

    *organization_ids* are the XTM One organizations ``organization_events.annotate_payload``
    found in the delivery. The subject is the organization's name, so the scope gate
    resolves it to the asset row that names it and, through a primary asset's asset names,
    to the primary asset its services support; no dependency sends it to the watchlist.
    No model call; never raises. How many signals were recorded.
    """
    from app.core.config import settings

    if not verified or settings.is_copilot_like or not organization_ids or not isinstance(payload, Mapping):
        return 0
    kind, data = _opencti_object(payload)
    if kind not in ("report", "incident"):
        return 0
    from app.core.database import async_session
    from app.services import ctem_case_loop, ctem_scope_gate_store, ctem_signals
    from app.services.ctem_loop_stream import publish_signal_update
    from app.services.organizations import get_organization, get_primary

    recorded = 0
    try:
        async with async_session() as db:
            primary = await get_primary(db)
            if primary is None:
                return 0
            from app.services.ctem_signal_sources import excluded_sources, is_excluded

            # Path 11 excluded for the OpenCTI listener by an administrator (#1445): nothing is recorded.
            if is_excluded(await excluded_sources(db, primary.id), "third_party_event", "opencti", "listener"):
                return 0
            for raw in organization_ids:
                try:
                    tracked = await get_organization(db, uuid.UUID(str(raw)))
                except ValueError:
                    continue
                if tracked is None or tracked.role == "primary" or tracked.archived_at is not None:
                    continue
                evidence: dict[str, Any] = {
                    "event": _text(payload.get("type")),
                    "stix_type": kind,
                    "report": _text(data.get("name"))[:300],
                    "relationship": tracked.relationship or "",
                    "organization_criticality": tracked.criticality,
                }
                signal, created = await ctem_signals.record_signal(
                    db,
                    organization_id=primary.id,
                    signal=ctem_signals.SignalInput(
                        trigger_path="third_party_event",
                        lens="world",
                        source_provider="opencti",
                        source_external_id=f"{_text(data.get('id'))}:{tracked.id}" if data.get("id") else None,
                        subject_kind="third_party",
                        subject_ref=tracked.name,
                        subject_label=tracked.name,
                        evidence=evidence,
                    ),
                    recorded_via="listener",
                    recorded_by=ctem_signals.SYSTEM_ACTOR,
                )
                pending = await ctem_scope_gate_store.evaluate_after_record(db, signal, created=created)
                await db.commit()
                await ctem_case_loop.publish_pending(pending)
                await publish_signal_update(db, signal)
                recorded += 1
    except Exception:
        logger.warning("CTEM third-party signal from an OpenCTI %s not recorded", kind, exc_info=True)
    return recorded


async def _close_at_source(provider: str, external_id: str) -> int:
    """Close the case members of the signals *provider* recorded for *external_id*; never raises.

    How many members were closed. A case whose last open member closes is
    resolved on a provider re-scan validation (CTEM-25).
    """
    if not external_id:
        return 0
    from app.core.database import async_session
    from app.services import ctem_case_loop
    from app.services.organizations import get_primary

    try:
        async with async_session() as db:
            primary = await get_primary(db)
            if primary is None:
                return 0
            pending = ctem_case_loop.PendingEvents()
            closed = await ctem_case_loop.close_signal_members(
                db, organization_id=primary.id, provider=provider, external_id=external_id, events=pending
            )
            await db.commit()
            await ctem_case_loop.publish_pending(pending)
            return closed
    except Exception:
        logger.warning("CTEM case members of %s %s not closed", provider, external_id, exc_info=True)
        return 0
