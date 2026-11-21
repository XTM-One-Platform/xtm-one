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
issue), 8 (mitigation regression: an OpenAEV scenario whose score degraded,
CTEM-15). The other paths enter through the agent tool and the API until their
products emit an event (design ``dev-docs/ctem-signals.md`` section 4).
"""

from __future__ import annotations

import logging
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
    Listener(
        "aikido",
        ("issue_open_created", "issue_open_created_open_source", "issue_open_created_sast"),
        (),
        "code_flaw",
        "inside",
        "code",
    ),
    Listener("openaev", ("scenario_score_degraded",), ("scenario",), "mitigation_regression", "inside", "asset"),
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


def _opencti_signal(row: Listener, payload: Mapping[str, Any]) -> dict[str, Any] | None:
    kind, data = _opencti_object(payload)
    if row.object_types and kind not in row.object_types:
        return None
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
    meta = payload.get("_opencti_event")
    if isinstance(meta, dict) and _text(meta.get("message")):
        evidence["message"] = _text(meta.get("message"))[:500]
    return {"external_id": external_id or None, "ref": ref, "label": label, "evidence": evidence}


#: Where an Aikido delivery may carry the issue (the body nests it, or not).
_AIKIDO_ID_KEYS = ("issue_id", "id", "issue_group_id")
_AIKIDO_SUBJECT_KEYS = ("affected_package", "affected_file", "code_repo_name", "container_repo_name", "domain", "title")


def _aikido_signal(row: Listener, payload: Mapping[str, Any]) -> dict[str, Any] | None:
    inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload.get("data")
    bodies = [b for b in (inner, payload) if isinstance(b, dict)]
    external_id = next((str(b[k]) for b in bodies for k in _AIKIDO_ID_KEYS if b.get(k) not in (None, "")), "")
    ref = next((_text(b.get(k)) for b in bodies for k in _AIKIDO_SUBJECT_KEYS if _text(b.get(k))), "")
    if not ref and not external_id:
        return None
    evidence: dict[str, Any] = {"event": _text(payload.get("event_type"))}
    for body in bodies:
        for key in ("severity", "type", "cve_id", "code_repo_name", "affected_package"):
            if body.get(key) not in (None, "") and key not in evidence:
                evidence[key] = body[key]
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
