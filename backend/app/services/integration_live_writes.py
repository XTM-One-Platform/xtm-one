# SPDX-License-Identifier: Apache-2.0

"""OpenCTI Live writes per agent binding: the one module for the rule.

An OpenCTI integration has an opt-in Live writes setting (``extra_config``
``live_writes``, off by default): on, knowledge changes skip the draft gate and
``apply_opencti_draft`` / ``create_opencti_label`` are offered. An agent may
override it per binding, ``Agent.integration_live_writes`` =
``{key: "on" | "off"}``, where *key* is the integration id of an instance
binding, or the provider key of a template binding (``integration_template_
bindings``, resolved per user at run time, so no id exists to name ahead). An
absent key inherits the integration's setting.

:func:`effective_live_writes` resolves it when the tools are built, per binding:

- a read-only binding never writes live (its write tools are withheld anyway);
- else the binding's override (``on`` = Live, ``off`` = Draft only);
- else the integration's setting; else off.

Everything that writes the setting (the agents REST API, the agent-builder
``create_agent`` / ``update_agent`` tools) parses and checks it here. Turning a
binding to Live needs the authority to edit the integration itself (the rule of
the integration-level field); Draft only and Inherit only need the agent.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from app.core.utils import safe_uuid

#: The providers whose bindings carry a Live writes override.
LIVE_WRITES_PROVIDERS: frozenset[str] = frozenset({"opencti"})
#: What a binding says about the integration's own setting.
LIVE_WRITES_MODES = ("inherit", "on", "off")
#: Context key ``build_integration_tools`` reads an agent's overrides from.
LIVE_WRITES_CONTEXT_KEY = "integration_live_writes"

_FORMAT_ERROR = (
    "integration_live_writes must be an object keyed by the integration id of an "
    "OpenCTI binding (or 'opencti' for its template binding), each value "
    "'inherit', 'on' (Live) or 'off' (Draft only); or 'none'/'clear' to clear "
    "every override."
)


class LiveWritesSettingError(ValueError):
    """An override a write may not store; ``status_code`` is the API's answer."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _key(raw: object) -> str | None:
    """A canonical override key: an integration id, or a provider key."""
    text = str(raw or "").strip()
    if not text:
        return None
    parsed = safe_uuid(text)
    if parsed is not None:
        return str(parsed)
    lowered = text.lower()
    return lowered if lowered in LIVE_WRITES_PROVIDERS else None


def _mode(raw: object) -> str | None:
    if isinstance(raw, bool):
        return "on" if raw else "off"
    text = str(raw or "").strip().lower()
    aliases = {"live": "on", "draft": "off", "draft_only": "off", "draft only": "off"}
    text = aliases.get(text, text)
    return text if text in LIVE_WRITES_MODES else None


def stored_live_writes(raw: object) -> dict[str, str]:
    """The overrides a stored ``Agent.integration_live_writes`` holds (inherit and malformed dropped)."""
    if not isinstance(raw, Mapping):
        return {}
    kept: dict[str, str] = {}
    for key, value in raw.items():
        k, m = _key(key), _mode(value)
        if k is not None and m in ("on", "off"):
            kept[k] = m
    return kept


def merge_live_writes(current: object, update: Mapping[str, str]) -> dict[str, str] | None:
    """*current* with each entry of *update* replacing its key's (``inherit`` removes it)."""
    merged = stored_live_writes(current)
    for key, mode in update.items():
        if mode == "inherit":
            merged.pop(key, None)
        else:
            merged[key] = mode
    return merged or None


def parse_integration_live_writes(raw: object) -> tuple[dict[str, str] | None, str | None, bool]:
    """``(value, error, provided)`` for an ``integration_live_writes`` argument.

    Accepts an object (or its JSON) keyed by integration id or ``opencti``,
    each value ``inherit`` / ``on`` / ``off`` (``live`` and ``draft`` read as
    ``on`` and ``off``), or ``"key:mode"`` pairs separated by commas;
    ``none`` / ``clear`` (or an empty object) clears every override.
    ``provided`` is ``False`` for a blank value, which leaves the setting
    unchanged. ``inherit`` entries are kept so a merge removes them.
    """
    if raw is None:
        return None, None, False
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None, None, False
        if text.lower() in ("none", "clear"):
            return None, None, True
        if text[:1] == "{":
            try:
                raw = json.loads(text)
            except ValueError:
                return None, _FORMAT_ERROR, True
        else:
            pairs: dict[str, str] = {}
            for part in text.split(","):
                if not part.strip():
                    continue
                key, sep, mode = part.strip().rpartition(":")
                if not sep:
                    return None, _FORMAT_ERROR, True
                # 'opencti:<id>:on' (the binding pair form) names the id.
                key = key.partition(":")[2] if key.count(":") == 1 and safe_uuid(key.partition(":")[2]) else key
                pairs[key] = mode
            raw = pairs
    if not isinstance(raw, Mapping):
        return None, _FORMAT_ERROR, True
    value: dict[str, str] = {}
    malformed: list[str] = []
    for key, mode in raw.items():
        k, m = _key(key), _mode(mode)
        if k is None or m is None:
            malformed.append(str(key))
            continue
        value[k] = m
    if malformed:
        return None, f"Invalid integration_live_writes entry(ies): {', '.join(malformed)}. {_FORMAT_ERROR}", True
    return value or None, None, True


def _bound_ids(bindings: Mapping[str, Any] | None, providers: Iterable[str]) -> set[str]:
    wanted = set(providers)
    return {
        str(parsed)
        for key, ids in (bindings or {}).items()
        if str(key).split("#", 1)[0] in wanted
        for iid in (ids if isinstance(ids, list) else [ids])
        if (parsed := safe_uuid(str(iid))) is not None
    }


def pruned_live_writes(
    overrides: object, bindings: Mapping[str, Any] | None, template_bindings: Iterable[str] | None
) -> dict[str, str] | None:
    """The overrides of the OpenCTI bindings still bound (by id or by template)."""
    bound = _bound_ids(bindings, LIVE_WRITES_PROVIDERS)
    templated = {str(p) for p in (template_bindings or []) if p}
    kept = {k: m for k, m in stored_live_writes(overrides).items() if k in bound or k in templated}
    return kept or None


def unbound_live_writes_keys(
    value: Mapping[str, str] | None, bindings: Mapping[str, Any] | None, template_bindings: Iterable[str] | None
) -> list[str]:
    """The keys of *value* that name no OpenCTI binding of the agent."""
    bound = _bound_ids(bindings, LIVE_WRITES_PROVIDERS)
    templated = {str(p) for p in (template_bindings or []) if p}
    return sorted(k for k in (value or {}) if k not in bound and k not in templated)


def newly_live_keys(value: Mapping[str, str] | None, baseline: object) -> list[str]:
    """The keys *value* turns to Live that *baseline* did not already hold Live."""
    before = stored_live_writes(baseline)
    return sorted(k for k, m in (value or {}).items() if m == "on" and before.get(k) != "on")


def live_writes_overrides(agent: Any, resolved_bindings: Mapping[str, Any] | None = None) -> dict[str, str]:
    """The Live writes override of each binding of *agent* that has one, by integration id.

    An instance binding takes its id's entry. *resolved_bindings* are the
    bindings the run builds its tools from (``{provider or provider#N: id or
    [ids]}``): each of them that is not one of the agent's own id bindings (a
    template binding resolved for the run) takes its provider key's entry.
    Only bound keys count, so a stale entry never reaches a run.
    """
    stored = pruned_live_writes(
        getattr(agent, "integration_live_writes", None),
        getattr(agent, "integration_bindings", None),
        getattr(agent, "integration_template_bindings", None),
    )
    if not stored:
        return {}
    direct = _bound_ids(getattr(agent, "integration_bindings", None), LIVE_WRITES_PROVIDERS)
    overrides = {iid: mode for iid, mode in stored.items() if iid in direct}
    for key, ids in (resolved_bindings or {}).items():
        provider = str(key).split("#", 1)[0]
        mode = stored.get(provider)
        if mode is None:
            continue
        for iid in _bound_ids({key: ids}, (provider,)) - direct:
            overrides[iid] = mode
    return overrides


def binding_override(overrides: Mapping[str, str] | None, integration_id: object) -> str | None:
    """The override (``on`` / ``off``) of one binding in a run's *overrides*, or ``None``."""
    if not overrides:
        return None
    parsed = safe_uuid(str(integration_id))
    return overrides.get(str(parsed) if parsed is not None else str(integration_id))


def effective_live_writes(
    integration_value: object,
    override: str | None,
    *,
    read_only: bool = False,
) -> bool:
    """Whether one binding writes live: read-only never; else the override; else the integration; else off."""
    if read_only:
        return False
    if override in ("on", "off"):
        return override == "on"
    return str(integration_value or "").strip().lower() == "on"


def override_label(mode: str | None) -> str:
    """A binding's override as ``get_agent`` shows it; empty when it inherits."""
    if mode == "on":
        return "live writes: live"
    if mode == "off":
        return "live writes: draft only"
    return ""


def live_override_refusal(keys: Iterable[str]) -> str:
    """The refusal of a Live override by someone who may not edit the integration."""
    return (
        "integration_live_writes turns Live writes on for integrations you may not edit: "
        f"{', '.join(keys)}. Only an administrator or the integration's manager may set an "
        "agent binding to Live; Draft only ('off') and Inherit need only the agent."
    )


async def checked_integration_live_writes(
    db: Any,
    user_id: Any,
    is_admin: bool,
    value: Mapping[str, str] | None,
    *,
    bindings: Mapping[str, Any] | None,
    template_bindings: Iterable[str] | None,
    baseline: object,
) -> dict[str, str] | None:
    """*value* (already merged) as an agent write stores it.

    422 for a key that names no OpenCTI binding of the agent; 403 when a key
    newly turned to Live names an integration the caller may not edit (a template
    key needs an administrator, since it reaches whichever integration the run
    resolves). A value already stored is never re-judged.
    """
    stored = stored_live_writes(value)
    if not stored:
        return None
    unbound = unbound_live_writes_keys(stored, bindings, template_bindings)
    if unbound:
        raise LiveWritesSettingError(
            422,
            "integration_live_writes names no OpenCTI binding of this agent: "
            f"{', '.join(unbound)}. Use the integration id of a bound OpenCTI integration, "
            "or 'opencti' for its template binding.",
        )
    fresh = newly_live_keys(stored, baseline)
    if fresh and not is_admin:
        refused = await _keys_user_may_not_edit(db, user_id, fresh)
        if refused:
            raise LiveWritesSettingError(403, live_override_refusal(refused))
    return stored


async def _keys_user_may_not_edit(db: Any, user_id: Any, keys: Iterable[str]) -> list[str]:
    """The keys among *keys* naming an integration a non-admin may not edit (a provider key always)."""
    import uuid

    from sqlalchemy import select

    from app.api.v1.helpers import can_manage_as, get_user_group_ids
    from app.models.integration import Integration

    keys = list(keys)
    ids = [k for k in keys if safe_uuid(k) is not None]
    refused = sorted(k for k in keys if safe_uuid(k) is None)
    if not ids:
        return refused
    rows = (
        (await db.execute(select(Integration).where(Integration.id.in_([uuid.UUID(i) for i in ids])))).scalars().all()
    )
    by_id = {str(row.id): row for row in rows}
    uid = safe_uuid(str(user_id))
    group_ids = await get_user_group_ids(db, uid) if uid is not None else []
    for iid in ids:
        row = by_id.get(iid)
        if row is None or not can_manage_as(uid, False, row, group_ids):
            refused.append(iid)
    return sorted(refused)
