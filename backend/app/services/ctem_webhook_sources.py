# SPDX-License-Identifier: Apache-2.0

"""Generic signed inbound webhook sources of the CTEM trigger paths (#2506).

A tool XTM One has no integration for records signals on one trigger path by
posting JSON to the source's own receiver (``/api/hooks/ctem-webhook-sources/{id}``):

- **Off by default.** A source records nothing until an administrator switches it on.
- **Signed.** Each source holds its own secret (encrypted at rest, shown once at
  creation and at each rotation). A delivery carries ``X-XTM-Signature:
  sha256=<hex HMAC-SHA256 of the raw body with the secret>``; a wrong or missing
  signature is refused (401) and records nothing, the source on or off.
- **Mapped.** ``mapping`` names the dotted path of the posted JSON each signal
  field is read from: ``id`` (required: the event's id, its dedupe key),
  ``label`` and ``severity``.
- **Listed.** Each source is a source of its path in the signal sources view
  (kind ``webhook``, key ``webhook:webhook:<id>``), with its 30-day count.

No model call. The design: ``dev-docs/ctem-signals.md`` section 4ter.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decrypt_value, encrypt_value, timing_safe_str_eq
from app.models.ctem_signal import SUBJECT_KINDS, TRIGGER_PATHS
from app.models.ctem_webhook_source import CtemWebhookSource

#: The source provider and kind a webhook source records under.
PROVIDER = "webhook"
KIND = "webhook"
#: The header a delivery is signed in.
SIGNATURE_HEADER = "X-XTM-Signature"
SIGNATURE_PREFIX = "sha256="
#: The signal fields a mapping names; ``id`` is required.
MAPPING_FIELDS: tuple[str, ...] = ("id", "label", "severity")
MAX_BODY_BYTES = 256 * 1024


class WebhookSourceError(ValueError):
    """A source or a delivery that cannot be accepted, with the reason in words."""


class SignatureError(WebhookSourceError):
    """A delivery whose signature is missing or wrong: refused with 401, nothing recorded."""


def new_secret() -> str:
    """A fresh per-source secret (256 bits, URL safe)."""
    return secrets.token_urlsafe(32)


def sign(secret: str, body: bytes) -> str:
    """The ``X-XTM-Signature`` value of *body* signed with *secret*."""
    return SIGNATURE_PREFIX + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify(secret: str, body: bytes, header: str | None) -> bool:
    """Whether *header* is the signature of *body* with *secret*; constant time, never raises."""
    if not header or not secret:
        return False
    return timing_safe_str_eq(header.strip(), sign(secret, body))


def source_key(source_id: Any) -> str:
    """The key a webhook source is counted and named by (``ctem_signal_sources.source_key``)."""
    return f"{PROVIDER}:{KIND}:{source_id}"


def clean_mapping(mapping: dict[str, Any] | None) -> dict[str, str]:
    """The mapping kept to its known fields, each a dotted path; :class:`WebhookSourceError` without ``id``."""
    out: dict[str, str] = {}
    for field in MAPPING_FIELDS:
        value = str((mapping or {}).get(field) or "").strip()
        if value:
            if len(value) > 200:
                raise WebhookSourceError(f"the {field} path is longer than 200 characters")
            out[field] = value
    if "id" not in out:
        raise WebhookSourceError("the mapping needs the path of the event id in the posted JSON")
    return out


def read_path(payload: Any, dotted: str) -> Any:
    """The value at *dotted* (``a.b.0.c``) in *payload*, ``None`` when any step is missing."""
    node = payload
    for step in dotted.split("."):
        if isinstance(node, dict):
            node = node.get(step)
        elif isinstance(node, list) and step.isdigit() and int(step) < len(node):
            node = node[int(step)]
        else:
            return None
        if node is None:
            return None
    return node


def _scalar(value: Any, limit: int) -> str:
    if value is None or isinstance(value, dict | list):
        return ""
    return str(value).strip()[:limit]


@dataclass(frozen=True)
class Mapped:
    """What a delivery maps to: the event id, its label and its severity."""

    external_id: str
    label: str | None
    severity: str | None


def map_payload(mapping: dict[str, Any], payload: Any) -> Mapped:
    """The signal fields read from *payload*; :class:`WebhookSourceError` when the id is missing."""
    paths = clean_mapping(mapping)
    external_id = _scalar(read_path(payload, paths["id"]), 300)
    if not external_id:
        raise WebhookSourceError(f"no event id at '{paths['id']}' in the posted JSON")
    label = _scalar(read_path(payload, paths["label"]), 300) if "label" in paths else ""
    severity = _scalar(read_path(payload, paths["severity"]), 40).lower() if "severity" in paths else ""
    return Mapped(external_id=external_id, label=label or None, severity=severity or None)


def check_new(trigger_path: str, name: str, subject_kind: str) -> str:
    """The cleaned name of a source to create; :class:`WebhookSourceError` on an unknown path or kind."""
    if trigger_path not in TRIGGER_PATHS:
        raise WebhookSourceError(f"trigger_path must be one of: {', '.join(TRIGGER_PATHS)}")
    if subject_kind not in SUBJECT_KINDS:
        raise WebhookSourceError(f"subject_kind must be one of: {', '.join(SUBJECT_KINDS)}")
    clean = (name or "").strip()[:120]
    if not clean:
        raise WebhookSourceError("a webhook source needs a name")
    return clean


async def create(
    db: AsyncSession,
    *,
    trigger_path: str,
    name: str,
    subject_kind: str,
    mapping: dict[str, Any],
    created_by: uuid.UUID | None,
) -> tuple[CtemWebhookSource, str]:
    """A new source, off, and its secret in clear (shown once). The caller commits."""
    clean = check_new(trigger_path, name, subject_kind)
    secret = new_secret()
    row = CtemWebhookSource(
        id=uuid.uuid4(),
        trigger_path=trigger_path,
        name=clean,
        enabled=False,
        subject_kind=subject_kind,
        secret_enc=encrypt_value(secret),
        mapping=clean_mapping(mapping),
        created_by=created_by,
    )
    db.add(row)
    await db.flush()
    return row, secret


def rotate(row: CtemWebhookSource, *, now: datetime | None = None) -> str:
    """A new secret for *row*, the old one refused from now on; the new secret in clear (shown once)."""
    secret = new_secret()
    row.secret_enc = encrypt_value(secret)
    row.secret_rotated_at = now or datetime.now(UTC)
    return secret


async def list_sources(db: AsyncSession) -> list[CtemWebhookSource]:
    rows = await db.execute(select(CtemWebhookSource).order_by(CtemWebhookSource.created_at))
    return list(rows.scalars().all())


def receiver_path(source_id: Any) -> str:
    return f"/api/hooks/ctem-webhook-sources/{source_id}"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def to_dict(row: CtemWebhookSource) -> dict[str, Any]:
    """The source as the API answers it: never its secret."""
    return {
        "id": str(row.id),
        "trigger_path": row.trigger_path,
        "name": row.name,
        "enabled": bool(row.enabled),
        "subject_kind": row.subject_kind,
        "mapping": dict(row.mapping or {}),
        "receiver_path": receiver_path(row.id),
        "signature_header": SIGNATURE_HEADER,
        "secret_rotated_at": _iso(row.secret_rotated_at),
        "last_delivery_at": _iso(row.last_delivery_at),
        "last_error": row.last_error,
        "created_at": _iso(row.created_at),
    }


def path_sources_of(rows: list[CtemWebhookSource]) -> dict[str, list[dict[str, Any]]]:
    """Per trigger path, its webhook sources in the shape of ``ctem_signal_sources.path_sources``."""
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        out.setdefault(row.trigger_path, []).append(
            {
                "provider": PROVIDER,
                "kind": KIND,
                "key": source_key(row.id),
                "name": row.name,
                "default_on": False,
                "event": "A tool posts a signed JSON event to this source's receiver",
                "bound": bool(row.enabled),
                "off": not row.enabled,
                "not_set_up": False,
                "error_count": 0,
                "excluded": False,
                # Never "Use on this path": a webhook source belongs to one path, its own switch turns it off.
                "available": False,
                "also_feeds": [],
                "webhook_id": str(row.id),
                "last_error": row.last_error,
            }
        )
    return out


async def receive(db: AsyncSession, row: CtemWebhookSource, body: bytes, signature: str | None) -> dict[str, Any]:
    """Verify and record one delivery to *row*. :class:`SignatureError` (401) on a bad or missing signature.

    The signature is checked before anything else, the source on or off, and a
    refused delivery writes nothing. A source that is off answers ``ignored``; a
    delivery whose id is not where the mapping says answers ``unmapped`` and
    keeps the reason on the source. The caller commits.
    """
    import json

    from app.services import ctem_signals
    from app.services.organizations import get_primary

    if len(body) > MAX_BODY_BYTES:
        raise WebhookSourceError(f"the body is larger than {MAX_BODY_BYTES} bytes")
    try:
        secret = decrypt_value(row.secret_enc)
    except Exception:  # an unreadable secret never lets a delivery through
        raise SignatureError("the source's secret cannot be read: rotate it") from None
    if not verify(secret, body, signature):
        raise SignatureError("missing or wrong signature")
    if not row.enabled:
        return {"status": "ignored", "reason": "webhook source is off", "recorded": False}
    try:
        payload = json.loads(body)
    except ValueError:
        raise WebhookSourceError("the body is not JSON") from None
    now = datetime.now(UTC)
    row.last_delivery_at = now
    try:
        mapped = map_payload(row.mapping, payload)
    except WebhookSourceError as exc:
        row.last_error = str(exc)[:300]
        return {"status": "unmapped", "reason": str(exc), "recorded": False}
    primary = await get_primary(db)
    if primary is None:
        return {"status": "ignored", "reason": "no organization set up yet", "recorded": False}
    evidence: dict[str, Any] = {ctem_signals.SOURCE_KEY_EVIDENCE: source_key(row.id), "webhook_source": row.name}
    if mapped.severity:
        evidence["severity"] = mapped.severity
    signal, created = await ctem_signals.record_signal(
        db,
        organization_id=primary.id,
        signal=ctem_signals.SignalInput(
            trigger_path=row.trigger_path,
            source_provider=PROVIDER,
            source_external_id=mapped.external_id,
            subject_kind=row.subject_kind,
            subject_ref=mapped.external_id,
            subject_label=mapped.label,
            observed_at=now,
            evidence=evidence,
            dedupe_key=f"webhook:{row.id}:{mapped.external_id}"[:200],
        ),
        recorded_via="listener",
        recorded_by=ctem_signals.SYSTEM_ACTOR,
        now=now,
    )
    row.last_error = None
    return {"status": "ok", "recorded": True, "created": created, "signal": signal}
