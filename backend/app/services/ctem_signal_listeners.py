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
issues; CTEM-25), 8 (mitigation regression: an OpenAEV scenario whose score
degraded, CTEM-15), 10 (credential leak: an Aikido ``leaked_secret`` issue,
CTEM-25; the secret itself is never stored). The other paths enter through
the agent tool and the API until their products emit an event (design
``dev-docs/ctem-signals.md`` section 4).

An Aikido ``issue.closed`` records no signal: it closes the member of the
signals that issue opened on their case, and the last member closed resolves
the case on a provider re-scan (``ctem_case_loop.close_signal_members``).
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

#: The declarative table. Event keys are the ones the webhook routes compute.
LISTENERS: tuple[Listener, ...] = (
    Listener("opencti", ("create", "update"), ("vulnerability",), "new_vulnerability", "world", "vulnerability"),
    Listener("opencti", ("create",), ("relationship:has",), "new_vulnerability", "inside", "vulnerability"),
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
    Listener("openaev", ("scenario_score_degraded",), ("scenario",), "mitigation_regression", "inside", "asset"),
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
    if row.object_types and kind not in row.object_types:
        return None
    if row.subject_kind == "identity":
        return _identity_signal(kind, data, payload)
    external_id = _text(data.get("id"))
    if kind.startswith("relationship:"):
        target = _text(data.get("target_ref"))
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
    if kind.startswith("relationship:"):
        # CTEM-08: the software or asset the relationship starts from, so the scope
        # gate resolves the signal to the tech stack product it names.
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
#: and, through the crown jewel's asset names, to the crown jewel (CTEM-25).
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
    if row.object_types and kind not in row.object_types:
        return None
    external_id = _text(meta.get("resource_id"))
    if not external_id:
        return None
    message = _text(meta.get("message"))[:300]
    evidence: dict[str, Any] = {"event": _text(meta.get("event_type")), "resource_type": kind}
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


_EXTRACTORS: dict[str, Callable[[Listener, Mapping[str, Any]], dict[str, Any] | None]] = {
    "opencti": _opencti_signal,
    "aikido": _aikido_signal,
    "openaev": _openaev_signal,
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


async def note_tracked_organizations(payload: Mapping[str, Any], organization_ids: list[str], *, verified: bool) -> int:
    """CTEM-29: one path 11 signal per tracked organization an OpenCTI report or incident names.

    *organization_ids* are the XTM One organizations ``organization_events.annotate_payload``
    found in the delivery. The subject is the organization's name, so the scope gate
    resolves it to the asset row that names it and, through a crown jewel's asset names,
    to the crown jewel its services support; no dependency sends it to the watchlist.
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
