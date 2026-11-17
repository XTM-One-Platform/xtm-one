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

"""Platform AI settings an administrator's agent reads and changes.

Two tools served with the ``platform_insights`` suite:
``get_platform_ai_settings`` and ``update_platform_ai_settings``. They cover
the agent-run settings of ``PlatformSettings``: the ``wait`` policy (per-call
cap, total per run, unbounded while ``until`` names a condition), the
default reasoning effort and the two tool approval switches of Settings >
Security > Tool approvals (``tool_approvals_enabled``, the platform kill
switch, and ``approve_tool_calls_default``), and the platform default chat
model and its provider (``agent_default_provider`` / ``agent_default_model``),
written through the handler of Settings > AI and models > Models
(``update_general_assistant_config``: its validation, its audit entry with
the values before and after, its announcement), so a monitoring agent can
move the default to another provider serving the model. Every switch an
administrator changes there in the UI is readable and writable here, with the
same admin check and the same validation and audit entry
(``app.services.tool_approval_settings``). The
acting user must be an administrator, checked at every call (the tool may be
bound to an agent a non-admin also talks to); a refusal is the 403 sentence,
and every change is audited like the Settings page.

And ``purge_platform_storage``: the retention sweeps of check files,
unused Video Studio media and figure stills applied now, with an age
threshold, ``dry_run`` by default (``app.services.storage_purge``).

And the OAuth defaults of Settings > Integrations > OAuth Defaults: ``list_oauth_defaults``,
``upsert_oauth_default`` and ``delete_oauth_default`` call the REST handlers of
``/api/v1/settings/oauth-defaults`` themselves (the vault-reference writer
check, the masked secret extra config kept, the audit row), so an agent and
the page store a default the same way. Answers carry the route's
``has_client_id`` / ``has_client_secret`` booleans, never a value.
"""

from __future__ import annotations

import json
from typing import Any

from app.core import database
from app.core.utils import format_exception_message, safe_uuid

from ._shared import logger

GET_PLATFORM_AI_SETTINGS = "get_platform_ai_settings"
UPDATE_PLATFORM_AI_SETTINGS = "update_platform_ai_settings"
PURGE_PLATFORM_STORAGE = "purge_platform_storage"
LIST_OAUTH_DEFAULTS = "list_oauth_defaults"
UPSERT_OAUTH_DEFAULT = "upsert_oauth_default"
DELETE_OAUTH_DEFAULT = "delete_oauth_default"

_REASONING_EFFORTS = ("low", "medium", "high", "max")

_FORBIDDEN = (
    "Error 403: only an administrator can read or change the platform AI settings; "
    "the person this agent acts for is not one."
)

_FORBIDDEN_OAUTH_DEFAULTS = (
    "Error 403: only an administrator can read or change the OAuth defaults (Settings > "
    "Integrations > OAuth Defaults); the person this agent acts for is not one."
)


def _http_error(exc: Any) -> str:
    """The sentence of an HTTPException a REST handler raised, as a tool error."""
    detail = getattr(exc, "detail", None) or str(exc)
    return f"Error {getattr(exc, 'status_code', 400)}: {detail}"


async def _admin(db: Any, user_id: str) -> Any | None:
    from sqlalchemy import select

    from app.models.user import User

    parsed = safe_uuid(user_id) if user_id else None
    if parsed is None:
        return None
    user = (await db.execute(select(User).where(User.id == parsed))).scalar_one_or_none()
    if user is None or not user.is_active or not user.is_admin:
        return None
    return user


_DEFAULT_MODEL_FIELDS = ("agent_default_provider", "agent_default_model")


def _default_model_env_locked() -> bool:
    """Whether DEFAULT_MODEL pins the platform default (re-seeded at every boot).

    The rule of ``_env_managed_ga_fields`` in ``app.api.v1.settings``: nothing
    is env-managed under BYOLLM_ACTIVATED.
    """
    from app.core.config import settings

    return bool(settings.default_model.strip()) and not settings.byollm_activated


async def _provider_configs(db: Any) -> list[Any]:
    from sqlalchemy import select

    from app.models.llm_config import LLMProviderConfig

    return list((await db.execute(select(LLMProviderConfig))).scalars().all())


def _serves(config: Any, model: str) -> bool:
    return model in (getattr(config, "available_models", None) or [])


def _serving_providers(configs: list[Any], model: str | None, current_id: Any) -> list[dict[str, Any]]:
    """The configured providers listing *model*: the valid targets of a switch."""
    if not model:
        return []
    return [
        {
            "id": str(cfg.id),
            "provider": cfg.provider,
            "display_name": getattr(cfg, "display_name", None),
            "current": current_id is not None and str(cfg.id) == str(current_id),
        }
        for cfg in configs
        if _serves(cfg, model)
    ]


def _resolve_default_provider(configs: list[Any], wanted: str, model: str) -> tuple[Any | None, str | None]:
    """(the config *wanted* names for *model*, None) or (None, the refusal).

    *wanted* is a config id, or a provider key or display name naming exactly
    one config that lists *model*.
    """
    by_id = safe_uuid(wanted)
    if by_id is not None:
        named = [cfg for cfg in configs if cfg.id == by_id]
    else:
        key = wanted.strip().lower()
        named = [
            cfg
            for cfg in configs
            if (cfg.provider or "").lower() == key or (getattr(cfg, "display_name", None) or "").lower() == key
        ]
    if not named:
        return None, (
            f"Error: no configured LLM provider is named '{wanted}'. Pick one of default_model.serving_providers "
            "of get_platform_ai_settings (Settings > AI and models > Providers lists every one)."
        )
    serving = [cfg for cfg in named if _serves(cfg, model)]
    if not serving:
        return None, (
            f"Error: the provider '{wanted}' does not serve the model '{model}' (not in its available models). "
            "Pick one of default_model.serving_providers, or name a model it serves."
        )
    if len(serving) > 1:
        ids = ", ".join(str(cfg.id) for cfg in serving)
        return None, f"Error: '{wanted}' names several configured providers serving '{model}'; name one by id: {ids}."
    return serving[0], None


#: The routes ``default_routes`` takes, each to its request fields: (provider key field or None,
#: provider id field, model field).
_ROUTE_REQUEST_FIELDS: dict[str, tuple[str | None, str, str]] = {
    "agent_default": ("agent_default_provider", "agent_default_provider_id", "agent_default_model"),
    "routing": (None, "routing_provider_id", "routing_model"),
    "fallback": (None, "fallback_provider_id", "fallback_model"),
    "decision": (None, "decision_provider_id", "decision_model"),
    "image": ("image_provider", "image_provider_id", "image_model"),
    "embedding": ("embedding_provider", "embedding_provider_id", "embedding_model"),
}


def _route_env_lock(route: str, locked: set[str]) -> str | None:
    """The refusal when an environment variable pins *route*, else None."""
    key_field, id_field, model_field = _ROUTE_REQUEST_FIELDS[route]
    pinned = sorted(f for f in (key_field, id_field, model_field) if f and f in locked)
    if route == "embedding" and "embedding_dimensions" in locked:
        pinned.append("embedding_dimensions")
    if not pinned:
        return None
    return (
        f"Error 403: the {route} route is set by its DEFAULT_* environment variable ({', '.join(pinned)} "
        "locked) and is read-only; change the variable and restart the backend."
    )


def _resolve_route_provider(configs: list[Any], wanted: str, model: str) -> tuple[Any | None, str | None]:
    """(the config *wanted* names listing *model* in its available models, None) or (None, the refusal)."""
    by_id = safe_uuid(wanted)
    if by_id is not None:
        named = [cfg for cfg in configs if cfg.id == by_id]
    else:
        key = wanted.strip().lower()
        named = [
            cfg
            for cfg in configs
            if (cfg.provider or "").lower() == key or (getattr(cfg, "display_name", None) or "").lower() == key
        ]
    if not named:
        return None, (
            f"Error: no configured LLM provider is named '{wanted}' (Settings > AI and models > Providers "
            "lists every one; get_platform_ai_settings default_routes names the current ones)."
        )
    serving = [cfg for cfg in named if _serves(cfg, model)]
    if not serving:
        return None, f"Error: the provider '{wanted}' does not serve the model '{model}' (not in its available models)."
    if len(serving) > 1:
        ids = ", ".join(str(cfg.id) for cfg in serving)
        return None, f"Error: '{wanted}' names several configured providers serving '{model}'; name one by id: {ids}."
    return serving[0], None


def _default_routes(ps: Any, configs: list[Any], locked: set[str]) -> dict[str, Any]:
    """Every default route: provider id, provider key, model, env_managed. No credential."""
    from app.services.default_model_routes import ROUTES

    keys = {str(cfg.id): cfg.provider for cfg in configs}
    routes: dict[str, Any] = {}
    for route, (id_field, key_field, model_field) in ROUTES.items():
        provider_id = getattr(ps, id_field, None)
        provider_id = str(provider_id) if provider_id else None
        routes[route] = {
            "provider_id": provider_id,
            "provider": (getattr(ps, key_field, None) if key_field else None) or keys.get(provider_id or ""),
            "model": getattr(ps, model_field, None),
            "env_managed": _route_env_lock(route, locked) is not None,
        }
    routes["embedding"]["dimensions"] = getattr(ps, "embedding_dimensions", None)
    return routes


def _locked_fields() -> set[str]:
    from app.api.v1.settings import env_managed_default_fields

    return env_managed_default_fields()


def _snapshot(
    ps: Any,
    serving_providers: list[dict[str, Any]] | None = None,
    default_routes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from app.core.config import settings
    from app.services.tool_approval_settings import serialize_tool_approval_settings
    from app.services.wait_settings import serialize_wait_settings

    return {
        "agent_runs": serialize_wait_settings(ps),
        "tool_approvals": {
            **serialize_tool_approval_settings(ps),
            # TOOL_APPROVALS_ENABLED as deployed: a seed read once when the
            # settings row is created, never a rule consulted later.
            "env_seed": getattr(settings, "tool_approvals_enabled", None) or None,
        },
        "default_reasoning_effort": getattr(ps, "default_reasoning_effort", None),
        "default_reasoning_effort_env_locked": bool(settings.default_reasoning_effort.strip()),
        "default_model": {
            "agent_default_model": getattr(ps, "agent_default_model", None),
            "agent_default_provider": getattr(ps, "agent_default_provider", None),
            "agent_default_provider_id": (
                str(getattr(ps, "agent_default_provider_id", None))
                if getattr(ps, "agent_default_provider_id", None)
                else None
            ),
            "serving_providers": serving_providers or [],
            "env_locked": _default_model_env_locked(),
            "note": (
                "update_platform_ai_settings agent_default_provider (an id of serving_providers) moves the "
                "default to another provider serving the model, audited like Settings > AI and models > Models; "
                "read-only while DEFAULT_MODEL pins it (env_locked)."
            ),
        },
        "default_routes": default_routes or {},
        "default_routes_note": (
            "Every default of Settings > AI and models > Models. update_platform_ai_settings default_routes "
            "{route: {provider, model?}} moves one (embedding: provider only, same model and dimensions); "
            "an env_managed route is read-only. Each change raises platform.default_model_route.changed."
        ),
        "notes": [
            "Waits: maximum_wait_time caps one wait, maximum_total_wait_time the total one run may wait; "
            "agents and assignments may narrow them with their own wait_policy (assignment > agent > platform).",
            "Tool approvals: tool_approvals_enabled false suspends every approval for everyone (Suspend all "
            "approvals); approve_tool_calls_default is what an agent following the platform default does.",
        ],
    }


def build_platform_admin_tools(
    user_id: str, *, assignment_run_id: str | None = None, conversation_id: str | None = None
) -> tuple[list[Any], dict[str, Any]]:
    """The admin tools acting for *user_id*: (definitions, {name: handler}).

    *assignment_run_id* / *conversation_id* name the run calling them, recorded
    on the audit entry of a default route change.
    """

    def _run_context() -> dict[str, Any]:
        return {"assignment_run_id": assignment_run_id, "conversation_id": conversation_id}

    from app.tools.base import ToolDefinition
    from app.tools.wait_policy import MAXIMUM_TOTAL_WAIT_TIME_CEILING, MAXIMUM_WAIT_TIME_CEILING

    async def _get(**_args: Any) -> str:
        from app.api.v1.platform_settings_helpers import get_platform_settings

        try:
            async with database.async_session() as db:
                if await _admin(db, user_id) is None:
                    return _FORBIDDEN
                ps = await get_platform_settings(db)
                from app.services.default_model_routes import ROUTES

                # The configs name the providers of the routes; none to read when no route is set.
                configs = (
                    await _provider_configs(db)
                    if any(getattr(ps, fields[0], None) for fields in ROUTES.values())
                    else []
                )
                model = getattr(ps, "agent_default_model", None)
                serving = (
                    _serving_providers(configs, model, getattr(ps, "agent_default_provider_id", None))
                    if model
                    else []
                )
                routes = _default_routes(ps, configs, _locked_fields())
                return json.dumps(_snapshot(ps, serving, routes), ensure_ascii=False, default=str)
        except Exception as exc:
            logger.error("get_platform_ai_settings failed: %s", format_exception_message(exc), exc_info=True)
            return "Error: the platform AI settings could not be read right now."

    async def _update(**args: Any) -> str:
        from app.api.v1.platform_settings_helpers import get_platform_settings
        from app.core.config import settings
        from app.services.audit import log_audit
        from app.services.tool_approval_settings import (
            TOOL_APPROVAL_DEFAULTS,
            TOOL_APPROVALS_AUDIT_ENTITY,
            apply_tool_approval_settings,
            validate_tool_approval_settings,
        )
        from app.services.wait_settings import WaitSettingsLockedError, apply_wait_settings

        body = {key: value for key, value in args.items() if key != "file_context"}
        routes = body.pop("default_routes", None)
        if routes is not None:
            if any(v is not None for v in body.values()):
                return "Error: change the default routes in a call of their own, without the other settings."
            return await _set_default_routes(routes)
        # null on these leaves the default as it is, like a switch.
        default_model_fields = {
            f: v for f in _DEFAULT_MODEL_FIELDS if (v := body.pop(f, None)) is not None
        }
        if default_model_fields:
            if body:
                return (
                    "Error: change the default model and provider in a call of their own, without the other "
                    "settings."
                )
            return await _set_default_model(default_model_fields)
        # null on a switch leaves it as it is: a switch has no "reset".
        for field in TOOL_APPROVAL_DEFAULTS:
            if field in body and body[field] is None:
                del body[field]
        if not body:
            return "Error: name at least one setting to change."
        try:
            # Validated before the session opens: a malformed switch writes nothing.
            validate_tool_approval_settings(body)
        except ValueError as exc:
            return f"Error: {exc}"
        try:
            async with database.async_session() as db:
                user = await _admin(db, user_id)
                if user is None:
                    return _FORBIDDEN
                ps = await get_platform_settings(db)
                try:
                    changes = apply_wait_settings(ps, body)
                except WaitSettingsLockedError as exc:
                    return f"Error 403: {exc}"
                except ValueError as exc:
                    return f"Error: {exc}"
                if "default_reasoning_effort" in body:
                    effort = body["default_reasoning_effort"] or None
                    if effort is not None and effort not in _REASONING_EFFORTS:
                        return f"Error: default_reasoning_effort must be one of {', '.join(_REASONING_EFFORTS)} or none."
                    if settings.default_reasoning_effort.strip():
                        return "Error 403: default_reasoning_effort is set by DEFAULT_REASONING_EFFORT and is read-only."
                    if ps.default_reasoning_effort != effort:
                        ps.default_reasoning_effort = effort
                        changes["default_reasoning_effort"] = effort
                approval_changes = apply_tool_approval_settings(ps, body)
                for change in approval_changes:
                    # The entry PUT /settings/platform writes, plus where it came from.
                    await log_audit(
                        db,
                        user=user,
                        action="toggle",
                        entity_type="platform_settings",
                        entity_name=TOOL_APPROVALS_AUDIT_ENTITY,
                        summary=change.summary,
                        details={change.field: change.value, "via": UPDATE_PLATFORM_AI_SETTINGS},
                    )
                    changes[change.field] = change.value
                wait_or_effort = {k: v for k, v in changes.items() if k not in TOOL_APPROVAL_DEFAULTS}
                if wait_or_effort:
                    await log_audit(
                        db,
                        user=user,
                        action="update",
                        entity_type="platform_settings",
                        entity_name="Platform AI settings",
                        summary="Changed the platform AI settings from an agent",
                        details={**wait_or_effort, "via": UPDATE_PLATFORM_AI_SETTINGS},
                    )
                if changes:
                    await db.commit()
                return json.dumps(
                    {"changed": changes, "settings": _snapshot(ps)}, ensure_ascii=False, default=str
                )
        except Exception as exc:
            logger.error("update_platform_ai_settings failed: %s", format_exception_message(exc), exc_info=True)
            return "Error: the platform AI settings could not be changed right now."

    async def _set_default_model(fields: dict[str, Any]) -> str:
        """Move the platform default chat model / provider through the Settings page handler."""
        from fastapi import BackgroundTasks, HTTPException

        from app.api.v1.platform_settings_helpers import get_platform_settings
        from app.api.v1.schemas.settings import GeneralAssistantConfigRequest
        from app.api.v1.settings import update_general_assistant_config

        wanted_provider = str(fields.get("agent_default_provider") or "").strip()
        wanted_model = str(fields.get("agent_default_model") or "").strip()
        if not wanted_provider:
            return "Error: agent_default_provider is required to change the default model (an id of serving_providers)."
        try:
            async with database.async_session() as db:
                user = await _admin(db, user_id)
                if user is None:
                    return _FORBIDDEN
                if _default_model_env_locked():
                    return (
                        "Error 403: the platform default model is set by the DEFAULT_MODEL environment variable "
                        "and is read-only; change DEFAULT_MODEL and restart the backend."
                    )
                ps = await get_platform_settings(db)
                before = {
                    "agent_default_provider": getattr(ps, "agent_default_provider", None),
                    "agent_default_provider_id": getattr(ps, "agent_default_provider_id", None),
                    "agent_default_model": getattr(ps, "agent_default_model", None),
                }
                model = wanted_model or before["agent_default_model"]
                if not model:
                    return "Error: no default model is set yet; name agent_default_model with the provider."
                config, refusal = _resolve_default_provider(await _provider_configs(db), wanted_provider, model)
                if refusal:
                    return refusal
                request = GeneralAssistantConfigRequest(
                    agent_default_provider=config.provider,
                    agent_default_provider_id=str(config.id),
                    agent_default_model=model,
                )
                from app.services.default_model_routes import mark_cause

                mark_cause(db, "agent_tool", user.id)
                try:
                    # The Settings page handler: validation, audit before / after, announcement, commit.
                    await update_general_assistant_config(
                        body=request, request=None, background_tasks=BackgroundTasks(), user=user, db=db
                    )
                except HTTPException as exc:
                    return _http_error(exc)
            async with database.async_session() as db:
                ps = await get_platform_settings(db)
                after = _snapshot(
                    ps,
                    _serving_providers(
                        await _provider_configs(db),
                        getattr(ps, "agent_default_model", None),
                        getattr(ps, "agent_default_provider_id", None),
                    ),
                )
            changed = {
                field: {"before": str(value) if value is not None else None, "after": after["default_model"].get(field)}
                for field, value in before.items()
                if (str(value) if value is not None else None) != after["default_model"].get(field)
            }
            return json.dumps({"changed": changed, "settings": after}, ensure_ascii=False, default=str)
        except Exception as exc:
            logger.error("update_platform_ai_settings failed: %s", format_exception_message(exc), exc_info=True)
            return "Error: the platform default model could not be changed right now."

    async def _set_default_routes(routes: Any) -> str:
        """Move default routes through the Settings page handler, all or nothing."""
        from fastapi import BackgroundTasks, HTTPException

        from app.api.v1.platform_settings_helpers import get_platform_settings
        from app.api.v1.schemas.settings import GeneralAssistantConfigRequest
        from app.api.v1.settings import update_general_assistant_config
        from app.services.audit import log_audit
        from app.services.default_model_routes import mark_cause, route_state

        if not isinstance(routes, dict) or not routes:
            return "Error: default_routes is an object {route: {provider, model?}} naming at least one route."
        unknown = sorted(r for r in routes if r not in _ROUTE_REQUEST_FIELDS)
        if unknown:
            return f"Error: unknown route(s) {', '.join(unknown)}; routes: {', '.join(_ROUTE_REQUEST_FIELDS)}."
        try:
            async with database.async_session() as db:
                user = await _admin(db, user_id)
                if user is None:
                    return _FORBIDDEN
                locked = _locked_fields()
                ps = await get_platform_settings(db)
                configs = await _provider_configs(db)
                fields: dict[str, Any] = {}
                for route, wanted in routes.items():
                    if not isinstance(wanted, dict) or not str(wanted.get("provider") or "").strip():
                        return f"Error: default_routes.{route} needs a provider (a provider id or key)."
                    refusal = _route_env_lock(route, locked)
                    if refusal:
                        return refusal
                    key_field, id_field, model_field = _ROUTE_REQUEST_FIELDS[route]
                    current_model = getattr(ps, model_field, None)
                    model = str(wanted.get("model") or "").strip() or current_model
                    if route == "embedding" and (
                        model != current_model
                        or (
                            wanted.get("dimensions") is not None
                            and wanted.get("dimensions") != getattr(ps, "embedding_dimensions", None)
                        )
                    ):
                        return (
                            "Error: the embedding route changes provider only: the same model and dimensions "
                            f"({current_model}, {getattr(ps, 'embedding_dimensions', None)}), so nothing is "
                            "re-embedded. A new embedding model is chosen in Settings > AI and models > Models."
                        )
                    if not model:
                        return f"Error: the {route} route has no model yet; name default_routes.{route}.model."
                    config, refusal = _resolve_route_provider(configs, str(wanted["provider"]), model)
                    if config is None:
                        return refusal or f"Error: no provider resolved for the {route} route."
                    if key_field:
                        fields[key_field] = config.provider
                    fields[id_field] = str(config.id)
                    fields[model_field] = model
                    if route == "embedding":
                        fields["embedding_dimensions"] = getattr(ps, "embedding_dimensions", None)
                before = {route: route_state(ps, route) for route in routes}
                mark_cause(db, "agent_tool", user.id)
                try:
                    # The Settings page handler: its validation, its audit entry before / after, its commit.
                    await update_general_assistant_config(
                        body=GeneralAssistantConfigRequest(**fields),
                        request=None,  # type: ignore[arg-type]  # the page handler reads no request field here
                        background_tasks=BackgroundTasks(),
                        user=user,
                        db=db,
                    )
                except HTTPException as exc:
                    # The session closes uncommitted: nothing the handler set is written.
                    return _http_error(exc)
            async with database.async_session() as db:
                ps = await get_platform_settings(db)
                configs = await _provider_configs(db)
                after_routes = _default_routes(ps, configs, _locked_fields())
                changed = {
                    route: {"before": before[route], "after": route_state(ps, route, after_routes[route]["provider"])}
                    for route in routes
                    if (before[route]["provider_id"], before[route]["model"])
                    != (after_routes[route]["provider_id"], after_routes[route]["model"])
                }
                if changed:
                    # Which agent run moved them: the page handler's entry carries the values.
                    await log_audit(
                        db,
                        user=user,
                        action="update",
                        entity_type="platform_settings",
                        entity_name="Default model routes",
                        summary="Moved default model routes from an agent: " + ", ".join(sorted(changed)),
                        details={"changed": changed, "via": UPDATE_PLATFORM_AI_SETTINGS, **_run_context()},
                    )
                    await db.commit()
            return json.dumps({"changed": changed, "default_routes": after_routes}, ensure_ascii=False, default=str)
        except Exception as exc:
            logger.error("update_platform_ai_settings failed: %s", format_exception_message(exc), exc_info=True)
            return "Error: the default model routes could not be changed right now."

    async def _purge(**args: Any) -> str:
        from app.services import storage_purge
        from app.services.audit import log_audit

        try:
            older_than_days = float(args["older_than_days"])
        except (KeyError, TypeError, ValueError):
            return "Error: older_than_days is required, a number of days (0: everything past the one-hour floor)."
        if older_than_days < 0:
            return "Error: older_than_days cannot be negative."
        dry_run = args.get("dry_run", True) is not False
        categories = args.get("categories") or None
        unknown = sorted(set(categories or []) - set(storage_purge.CATEGORIES))
        if unknown:
            return f"Error: unknown categories {unknown}; one of {', '.join(storage_purge.CATEGORIES)}."
        limit = args.get("limit") or storage_purge.DEFAULT_LIMIT
        try:
            async with database.async_session() as db:
                user = await _admin(db, user_id)
                if user is None:
                    return _FORBIDDEN
                report = await storage_purge.purge(
                    db, older_than_days=older_than_days, dry_run=dry_run, categories=categories, limit=int(limit)
                )
                if not dry_run:
                    await log_audit(
                        db,
                        user=user,
                        action="delete",
                        entity_type="platform_storage",
                        entity_name="Platform storage",
                        summary=f"Purged working storage older than {older_than_days:g} days from an agent",
                        details={"via": PURGE_PLATFORM_STORAGE, **report},
                    )
                    await db.commit()
                return json.dumps(report, ensure_ascii=False, default=str)
        except Exception as exc:
            logger.error("purge_platform_storage failed: %s", format_exception_message(exc), exc_info=True)
            return "Error: the platform storage could not be purged right now; what was deleted before the error stays deleted."

    async def _list_oauth_defaults(**_args: Any) -> str:
        from app.api.v1.settings import list_oauth_defaults

        try:
            async with database.async_session() as db:
                user = await _admin(db, user_id)
                if user is None:
                    return _FORBIDDEN_OAUTH_DEFAULTS
                rows = await list_oauth_defaults(user=user, db=db)
                return json.dumps(
                    {"oauth_defaults": [row.model_dump() for row in rows]}, ensure_ascii=False, default=str
                )
        except Exception as exc:
            logger.error("list_oauth_defaults failed: %s", format_exception_message(exc), exc_info=True)
            return "Error: the OAuth defaults could not be read right now."

    async def _upsert_oauth_default(**args: Any) -> str:
        from fastapi import HTTPException
        from pydantic import ValidationError

        from app.api.v1.schemas.settings import OAuthDefaultRequest
        from app.api.v1.settings import upsert_oauth_default

        try:
            body = OAuthDefaultRequest(
                provider=str(args.get("provider") or "").strip(),
                client_id=args.get("client_id") or "",
                client_secret=args.get("client_secret") or "",
                extra_config=args.get("extra_config") or None,
            )
        except ValidationError as exc:
            # The validation message never quotes the submitted secret.
            problems = "; ".join(str(error.get("msg")) for error in exc.errors(include_input=False))
            return f"Error: {problems}"
        if not body.provider or not body.client_id or not body.client_secret:
            return "Error: provider, client_id and client_secret are required."
        try:
            async with database.async_session() as db:
                user = await _admin(db, user_id)
                if user is None:
                    return _FORBIDDEN_OAUTH_DEFAULTS
                try:
                    saved = await upsert_oauth_default(body=body, request=None, user=user, db=db)
                except HTTPException as exc:
                    return _http_error(exc)
                return json.dumps({"saved": saved.model_dump()}, ensure_ascii=False, default=str)
        except Exception as exc:
            logger.error("upsert_oauth_default failed: %s", format_exception_message(exc), exc_info=True)
            return "Error: the OAuth default could not be saved right now."

    async def _delete_oauth_default(**args: Any) -> str:
        from fastapi import HTTPException

        from app.api.v1.settings import delete_oauth_default

        provider = str(args.get("provider") or "").strip()
        if not provider:
            return "Error: provider is required."
        try:
            async with database.async_session() as db:
                user = await _admin(db, user_id)
                if user is None:
                    return _FORBIDDEN_OAUTH_DEFAULTS
                try:
                    await delete_oauth_default(provider=provider, request=None, user=user, db=db)
                except HTTPException as exc:
                    return _http_error(exc)
                return json.dumps({"deleted": provider}, ensure_ascii=False)
        except Exception as exc:
            logger.error("delete_oauth_default failed: %s", format_exception_message(exc), exc_info=True)
            return "Error: the OAuth default could not be deleted right now."

    definitions = [
        ToolDefinition(
            source="builtin:platform_insights",
            name=GET_PLATFORM_AI_SETTINGS,
            description=(
                "Read the platform AI settings bounding every agent run: the wait policy (ceilings, env locks), "
                "the default reasoning effort, the default model (serving_providers), default_routes and "
                "tool_approvals (tool_approvals_enabled false = every approval suspended; "
                "approve_tool_calls_default; env_seed = TOOL_APPROVALS_ENABLED as deployed). "
                "Administrators only (403 otherwise)."
            ),
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        ),
        ToolDefinition(
            source="builtin:platform_insights",
            name=UPDATE_PLATFORM_AI_SETTINGS,
            description=(
                "Change the platform AI settings of every agent run: maximum_wait_time (seconds, one wait), "
                "maximum_total_wait_time (seconds per run), wait_unbounded_with_until, default_reasoning_effort "
                "(null resets these); tool_approvals_enabled (false suspends every approval) and "
                "approve_tool_calls_default (null leaves them unchanged). agent_default_provider (a "
                "serving_providers id, optional agent_default_model; alone) moves the default chat model. "
                "default_routes {route: {provider (id or key), model?}} (alone; agent_default, routing, fallback, "
                "decision, image, embedding: provider only) moves a default route. Administrators only (403 "
                "otherwise), audited like the Settings page; env-pinned values are read-only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "maximum_wait_time": {
                        "type": ["integer", "null"],
                        "minimum": 1,
                        "maximum": MAXIMUM_WAIT_TIME_CEILING,
                    },
                    "maximum_total_wait_time": {
                        "type": ["integer", "null"],
                        "minimum": 1,
                        "maximum": MAXIMUM_TOTAL_WAIT_TIME_CEILING,
                    },
                    "wait_unbounded_with_until": {"type": ["boolean", "null"]},
                    "default_reasoning_effort": {"type": ["string", "null"], "enum": [*_REASONING_EFFORTS, None]},
                    "tool_approvals_enabled": {"type": ["boolean", "null"]},
                    "approve_tool_calls_default": {"type": ["boolean", "null"]},
                    "agent_default_provider": {"type": ["string", "null"]},
                    "agent_default_model": {"type": ["string", "null"]},
                    "default_routes": {
                        "type": ["object", "null"],
                        "additionalProperties": {
                            "type": "object",
                            "properties": {"provider": {"type": "string"}, "model": {"type": ["string", "null"]}},
                            "required": ["provider"],
                        },
                    },
                },
                "additionalProperties": False,
            },
        ),
        ToolDefinition(
            source="builtin:platform_insights",
            name=PURGE_PLATFORM_STORAGE,
            description=(
                "Apply the storage retention now, with an age threshold: deletes the check files (frames, contact "
                "sheets, insert crops, screenshots an agent looked at), the media of ready recordings and AI shots "
                "no video uses (video, keyframes, poster, contact sheet, frames; the library row stays, expired) "
                "and the editor's figure stills, made more than older_than_days ago (never the last hour). Never "
                "a file a video needs, a medium a video's timeline names or marked 'Keep its media', nor a "
                "video's renders. dry_run (default true) only counts files and bytes per category: run it first. "
                "At most limit items per category a call; 'more' says call again. Administrators only (403 "
                "otherwise), audited."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "older_than_days": {"type": "number", "minimum": 0},
                    "dry_run": {"type": "boolean", "default": True},
                    "categories": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["agent_check", "unused_recording", "unused_shot", "figure_stills"]},
                        "description": "Only these (default: all).",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50000, "default": 5000},
                },
                "required": ["older_than_days"],
                "additionalProperties": False,
            },
        ),
        ToolDefinition(
            source="builtin:platform_insights",
            name=LIST_OAUTH_DEFAULTS,
            description=(
                "List the admin OAuth defaults (Settings > Integrations > OAuth Defaults): per provider, whether a client id, a "
                "client secret and extra config are stored (has_client_id, has_client_secret, has_extra_config) "
                "and the names of the secret extra-config keys holding a value. Never a value. An OAuth "
                "integration created without its own client id takes its provider's default. Administrators "
                "only (403 otherwise)."
            ),
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        ),
        ToolDefinition(
            source="builtin:platform_insights",
            name=UPSERT_OAUTH_DEFAULT,
            description=(
                "Create or replace a provider's admin OAuth default (Settings > Integrations > OAuth Defaults), the app every "
                "OAuth integration of that provider created without its own client id is put on. client_secret "
                "may be a vault reference the caller may store. extra_config holds provider settings (e.g. "
                "tenant_id); a secret key sent as the mask keeps its stored value. Answers the saved row's "
                "booleans, never a value. Administrators only (403 otherwise), audited."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "provider": {
                        "type": "string",
                        "description": "The integration provider key, as the integration catalog names it.",
                    },
                    "client_id": {"type": "string"},
                    "client_secret": {"type": "string"},
                    "extra_config": {"type": "object", "additionalProperties": {"type": "string"}},
                },
                "required": ["provider", "client_id", "client_secret"],
                "additionalProperties": False,
            },
        ),
        ToolDefinition(
            source="builtin:platform_insights",
            name=DELETE_OAUTH_DEFAULT,
            description=(
                "Delete a provider's admin OAuth default (Settings > Integrations > OAuth Defaults). Integrations already on the "
                "admin app keep their stored credentials. Administrators only (403 otherwise), audited."
            ),
            input_schema={
                "type": "object",
                "properties": {"provider": {"type": "string"}},
                "required": ["provider"],
                "additionalProperties": False,
            },
        ),
    ]
    return definitions, {
        GET_PLATFORM_AI_SETTINGS: _get,
        UPDATE_PLATFORM_AI_SETTINGS: _update,
        PURGE_PLATFORM_STORAGE: _purge,
        LIST_OAUTH_DEFAULTS: _list_oauth_defaults,
        UPSERT_OAUTH_DEFAULT: _upsert_oauth_default,
        DELETE_OAUTH_DEFAULT: _delete_oauth_default,
    }
