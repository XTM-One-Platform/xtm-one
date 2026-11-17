# SPDX-License-Identifier: Apache-2.0

"""Which signature an email-sending integration's tools add: the one module for the rule.

Every integration of a provider that sends email (``email_signatures.
email_sender_providers()``) has an Email signature setting:
``Integration.signature_enabled`` (on by default) and
``Integration.signature_integration_id`` (empty: picked automatically). An agent
may override it per binding, ``Agent.integration_signatures`` =
``{integration_id: {"signature_mode": "inherit" | "on" | "off",
"signature_integration_id": "..."}}`` (an entry only where something differs
from inherit).

:func:`plan_signatures` resolves it when the tools are built, per sending
integration - two bound mailboxes may resolve differently:

- on / off: the binding's mode, else the integration's setting, else on;
- the signature integration: the binding's, else the integration's, else the
  single signature integration bound to the agent, else the single one the
  acting person may use; else none, with a logged hint that says how to set one.

Everything that writes the settings (the integrations and agents REST API, the
agent-builder tools) checks them here: a signature integration someone sets is
one they can see, of a provider that holds signatures; an override names a
bound integration that sends email. A value already stored is never re-judged.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import safe_uuid
from app.services.email_signatures import (
    OFF_BY_BINDING,
    OFF_BY_INTEGRATION,
    SignaturePlan,
    email_sender_providers,
    signature_providers,
)

logger = logging.getLogger(__name__)

#: What a binding says about the integration's own setting.
SIGNATURE_MODES = ("inherit", "on", "off")
#: Context key ``build_integration_tools`` reads an agent's overrides from.
SIGNATURE_CONTEXT_KEY = "integration_signatures"

_FORMAT_ERROR = (
    "integration_signatures must be an object keyed by integration id, each value "
    '{"signature_mode": "inherit" | "on" | "off"} with an optional "signature_integration_id" '
    "(or just the mode), or 'none'/'clear' to clear every override."
)


class SignatureSettingError(ValueError):
    """A signature setting a write may not store; ``status_code`` is the API's answer."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


# -- Overrides ----------------------------------------------------------------


def _entry(raw: object) -> dict[str, str] | None:
    """One binding's override, canonical; ``None`` when it is not one."""
    if isinstance(raw, str):
        raw = {"signature_mode": raw}
    if not isinstance(raw, Mapping):
        return None
    mode = str(raw.get("signature_mode") or "inherit").strip().lower()
    if mode not in SIGNATURE_MODES:
        return None
    entry = {"signature_mode": mode}
    chosen = raw.get("signature_integration_id")
    if chosen:
        parsed = safe_uuid(str(chosen).strip())
        if parsed is None:
            return None
        entry["signature_integration_id"] = str(parsed)
    return entry


def _is_inherit(entry: Mapping[str, str]) -> bool:
    return entry.get("signature_mode") == "inherit" and not entry.get("signature_integration_id")


def compact_signatures(value: Mapping[str, Mapping[str, str]] | None) -> dict[str, dict[str, str]] | None:
    """*value* without the entries that only inherit, or ``None`` when nothing is left."""
    kept = {iid: dict(entry) for iid, entry in (value or {}).items() if not _is_inherit(entry)}
    return kept or None


def merge_signatures(
    current: Mapping[str, Mapping[str, str]] | None, update: Mapping[str, Mapping[str, str]]
) -> dict[str, dict[str, str]] | None:
    """*current* with each entry of *update* replacing its binding's (a pure inherit removes it)."""
    return compact_signatures({**{k: dict(v) for k, v in (current or {}).items()}, **update})


def parse_integration_signatures(raw: object) -> tuple[dict[str, dict[str, str]] | None, str | None, bool]:
    """``(value, error, provided)`` for an ``integration_signatures`` argument.

    Accepts an object (or its JSON) keyed by integration id, each value a mode
    or ``{"signature_mode", "signature_integration_id"}``; ``none`` / ``clear``
    (or an empty object) clears every override. ``provided`` is ``False`` for a
    blank value, which leaves the setting unchanged. Entries that only inherit
    are kept, so a merge can remove the override they name.
    """
    if raw is None:
        return None, None, False
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None, None, False
        if text.lower() in ("none", "clear"):
            return None, None, True
        try:
            raw = json.loads(text)
        except ValueError:
            return None, _FORMAT_ERROR, True
    if not isinstance(raw, Mapping):
        return None, _FORMAT_ERROR, True
    value: dict[str, dict[str, str]] = {}
    malformed: list[str] = []
    for key, entry in raw.items():
        parsed, normalized = safe_uuid(str(key).strip()), _entry(entry)
        if parsed is None or normalized is None:
            malformed.append(str(key))
            continue
        value[str(parsed)] = normalized
    if malformed:
        return None, f"Invalid integration_signatures entry(ies): {', '.join(malformed)}. {_FORMAT_ERROR}", True
    return value or None, None, True


def stored_overrides(raw: object) -> dict[str, dict[str, str]]:
    """The overrides a stored ``Agent.integration_signatures`` holds, malformed entries dropped."""
    if not isinstance(raw, Mapping):
        return {}
    kept: dict[str, dict[str, str]] = {}
    for key, entry in raw.items():
        parsed, normalized = safe_uuid(str(key)), _entry(entry)
        if parsed is not None and normalized is not None and not _is_inherit(normalized):
            kept[str(parsed)] = normalized
    return kept


def bound_integration_ids(bindings: Mapping[str, Any] | None) -> set[str]:
    """The canonical ids of an agent's instance bindings."""
    return {
        str(parsed)
        for ids in (bindings or {}).values()
        for iid in (ids if isinstance(ids, list) else [ids])
        if (parsed := safe_uuid(str(iid))) is not None
    }


def pruned_signatures(
    signatures: Mapping[str, Mapping[str, str]] | None, bindings: Mapping[str, Any] | None
) -> dict[str, dict[str, str]] | None:
    """The overrides of the integrations still bound."""
    bound = bound_integration_ids(bindings)
    return compact_signatures({iid: entry for iid, entry in stored_overrides(signatures).items() if iid in bound})


def override_label(entry: Mapping[str, str] | None) -> str:
    """A binding's override as ``get_agent`` shows it; empty when it inherits everything."""
    if not entry or _is_inherit(entry):
        return ""
    mode = entry.get("signature_mode", "inherit")
    chosen = entry.get("signature_integration_id")
    return f"signature: {mode}" + (f" via {chosen}" if chosen else "")


# -- Resolution ---------------------------------------------------------------


def _provider_names(providers: Iterable[str]) -> str:
    from app.api.v1.integration_catalog import PROVIDER_CATALOG

    return ", ".join(sorted((PROVIDER_CATALOG.get(p) or {}).get("name") or p for p in providers))


def _none_available() -> str:
    names = _provider_names(signature_providers())
    connect = f"Connect one ({names})" if names else "Connect one"
    return (
        "no signature integration is available to this integration's user. "
        f"{connect}, then choose it in the integration's Email signature setting."
    )


def _several(names: list[str]) -> str:
    return (
        f"several signature integrations are available ({', '.join(names)}) and none is chosen. "
        "Choose one in the integration's Email signature setting, or on the agent's binding."
    )


_GONE = (
    "the signature integration chosen for it no longer holds email signatures or is gone. "
    "Choose another in the integration's Email signature setting, or on the agent's binding."
)


async def _visible_signature_integrations(
    db: AsyncSession, user_id: uuid.UUID | str | None
) -> list[tuple[str, str, str]]:
    """``(id, name, provider)`` of the signature integrations *user_id* may use, by name."""
    from app.api.v1.helpers import caller_visible_filter, get_user_group_ids
    from app.models.integration import Integration

    sources = signature_providers()
    if not sources:
        return []
    uid = safe_uuid(str(user_id)) if user_id else None
    groups = await get_user_group_ids(db, uid) if uid is not None else []
    rows = await db.execute(
        select(Integration.id, Integration.name, Integration.provider)
        .where(Integration.provider.in_(sorted(sources)), caller_visible_filter(Integration, uid, groups))
        .order_by(Integration.name, Integration.id)
    )
    return [(str(row[0]), row[1] or "", row[2]) for row in rows.all()]


async def signature_integration_options(db: AsyncSession, user_id: uuid.UUID) -> list[tuple[str, str, str]]:
    """The signature integrations *user_id* may choose, ``(id, name, provider)`` by name."""
    return await _visible_signature_integrations(db, user_id)


async def plan_signatures(
    db: AsyncSession,
    rows: Mapping[str, Any],
    *,
    overrides: Mapping[str, Any] | None = None,
    acting_user_id: uuid.UUID | str | None = None,
) -> dict[str, SignaturePlan]:
    """The plan of each email-sending integration of *rows* (``{integration_id: Integration}``).

    *rows* are the integrations the tools are built for (the signature
    integrations bound alongside included), *overrides* the agent's
    ``integration_signatures`` and *acting_user_id* the person the run acts
    for (an integration's own owner when it is not known).
    """
    from app.models.integration import Integration

    senders = email_sender_providers()
    sending = {str(iid): row for iid, row in rows.items() if getattr(row, "provider", None) in senders}
    if not sending:
        return {}
    sources = signature_providers()
    entries = stored_overrides(overrides)
    bound = sorted(
        ((str(row.id), row.name or "") for row in rows.values() if getattr(row, "provider", None) in sources),
        key=lambda pair: (pair[1], pair[0]),
    )

    chosen_by: dict[str, str] = {}
    for iid, row in sending.items():
        stored = getattr(row, "signature_integration_id", None)
        chosen = entries.get(iid, {}).get("signature_integration_id") or (str(stored) if stored else "")
        if chosen and safe_uuid(chosen) is not None:
            chosen_by[iid] = str(safe_uuid(chosen))
    names: dict[str, str] = {}
    wanted = {uuid.UUID(chosen) for chosen in chosen_by.values()}
    if wanted:
        found = await db.execute(
            select(Integration.id, Integration.name, Integration.provider).where(Integration.id.in_(wanted))
        )
        names = {str(row[0]): row[1] or "" for row in found.all() if row[2] in sources}

    visible_by_user: dict[str, list[tuple[str, str, str]]] = {}
    plans: dict[str, SignaturePlan] = {}
    for iid, row in sending.items():
        mode = entries.get(iid, {}).get("signature_mode", "inherit")
        if mode == "off" or (mode == "inherit" and getattr(row, "signature_enabled", True) is False):
            plans[iid] = SignaturePlan(
                enabled=False, off_by=OFF_BY_BINDING if mode == "off" else OFF_BY_INTEGRATION
            )
            continue
        if iid in chosen_by:
            chosen = chosen_by[iid]
            if chosen in names:
                plans[iid] = SignaturePlan(integration_id=chosen, integration_name=names[chosen])
            else:
                plans[iid] = SignaturePlan(missing=_GONE)
                logger.info("Email signature of integration %s: %s", iid, _GONE)
            continue
        if len(bound) == 1:
            plans[iid] = SignaturePlan(integration_id=bound[0][0], integration_name=bound[0][1])
            continue
        if bound:
            missing = _several([name for _id, name in bound])
        else:
            user = acting_user_id or getattr(row, "user_id", None)
            key = str(user or "")
            if key not in visible_by_user:
                visible_by_user[key] = await _visible_signature_integrations(db, user)
            visible = visible_by_user[key]
            if len(visible) == 1:
                plans[iid] = SignaturePlan(integration_id=visible[0][0], integration_name=visible[0][1])
                continue
            missing = _several([name for _id, name, _p in visible]) if visible else _none_available()
        plans[iid] = SignaturePlan(missing=missing)
        logger.info("Email signature of integration %s: %s", iid, missing)
    return plans


# -- Writes -------------------------------------------------------------------


async def checked_signature_integration(
    db: AsyncSession, user_id: uuid.UUID, value: object, *, baseline: object = None
) -> uuid.UUID | None:
    """The signature integration a write sets: one *user_id* can see, holding email signatures.

    Empty clears it (picked automatically). The value *baseline* already holds
    is kept without a check.
    """
    from app.api.v1.helpers import get_user_group_ids, user_visible_filter
    from app.models.integration import Integration

    if not value:
        return None
    parsed = safe_uuid(str(value).strip())
    if parsed is None:
        raise SignatureSettingError(422, "signature_integration_id must be an integration id.")
    if baseline is not None and parsed == safe_uuid(str(baseline)):
        return parsed
    groups = await get_user_group_ids(db, user_id)
    row = (
        await db.execute(
            select(Integration).where(Integration.id == parsed, user_visible_filter(Integration, user_id, groups))
        )
    ).scalar_one_or_none()
    if row is None:
        raise SignatureSettingError(400, "The signature integration is not one you can use.")
    if row.provider not in signature_providers():
        raise SignatureSettingError(400, f"The integration '{row.name}' does not hold email signatures.")
    return parsed


async def checked_integration_signatures(
    db: AsyncSession,
    user_id: uuid.UUID,
    value: Mapping[str, Mapping[str, str]] | None,
    *,
    bindings: Mapping[str, Any] | None,
    baseline: Mapping[str, Any] | None,
) -> dict[str, dict[str, str]] | None:
    """*value* as an agent write stores it: overrides of bound email-sending integrations.

    422 for an override of an integration the agent does not bind, or that
    sends no email; each signature integration it sets (and the baseline did
    not hold) goes through :func:`checked_signature_integration`.
    """
    from app.models.integration import Integration

    value = compact_signatures(value)
    if not value:
        return None
    bound = bound_integration_ids(bindings)
    unbound = sorted(iid for iid in value if iid not in bound)
    if unbound:
        raise SignatureSettingError(
            422, f"integration_signatures names integrations the agent does not bind: {', '.join(unbound)}."
        )
    providers = {
        str(row[0]): row[1]
        for row in (
            await db.execute(
                select(Integration.id, Integration.provider).where(Integration.id.in_([uuid.UUID(i) for i in value]))
            )
        ).all()
    }
    senders = email_sender_providers()
    silent = sorted(iid for iid in value if providers.get(iid) not in senders)
    if silent:
        raise SignatureSettingError(
            422, f"integration_signatures names integrations that send no email: {', '.join(silent)}."
        )
    before = stored_overrides(baseline)
    for iid, entry in value.items():
        chosen = entry.get("signature_integration_id")
        if chosen:
            checked = await checked_signature_integration(
                db, user_id, chosen, baseline=before.get(iid, {}).get("signature_integration_id")
            )
            entry["signature_integration_id"] = str(checked)
    return value
