# SPDX-License-Identifier: Apache-2.0

"""OpenAEV connector (read) + catalog deployment + platform settings tools.

* ``query_openaev_connectors``          - read injectors / collectors / executors.
* ``query_openaev_catalog``             - browse the connector catalog and a
  connector's configuration parameters (so the agent knows what to ask for).
* ``manage_openaev_connector_instance`` - deploy / start / stop / reconfigure /
  delete a connector instance from the catalog (collectors, injectors,
  executors) via the XTM Composer.
* ``manage_openaev_settings``           - read/update platform settings.

Deploying / reconfiguring / starting / stopping a connector instance requires
OpenAEV **Enterprise Edition** with a running **XTM Composer** (enforced
server-side; surfaced as friendly errors here). Browsing the catalog does not.
Secret configuration values (``PASSWORD`` format) are encrypted server-side, so
this tool only sends plaintext over the (TLS) REST transport.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.core.module_versions import RegisteredVersionFn, version_answer
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import PlatformCapabilities, _api, _search_body, is_not_found_error, json_answer
from .engine_schema import engine_entity_of, engine_filter_keys


async def _engine_keys_lines(base_url: str, headers: dict[str, str], class_name: str) -> list[str]:
    """The engine filter keys of the entity indexing *class_name*, for query_openaev_analytics.

    The analytics engine filters on its own indexed fields, not on the model
    attributes above (asset_tags there is base_tags_side here).
    """
    entity = engine_entity_of(class_name)
    if not entity:
        return []
    keys, err = await engine_filter_keys(_api, base_url, headers, [entity])
    if err or not keys:
        return [f"Engine keys of '{entity}' for query_openaev_analytics: unavailable ({err or 'none reported'})."]
    lines = [
        f"Engine keys of '{entity}' ({len(keys)}) - the ONLY keys query_openaev_analytics "
        "accepts (the model keys above are for search tools):"
    ]
    for name in sorted(keys):
        entry = keys[name]
        line = f"  - {name}"
        if entry["type"]:
            line += f" ({entry['type']})"
        if entry["operators"]:
            line += f" operators: {', '.join(sorted(entry['operators']))}"
        lines.append(line)
    return lines


def _version_from_body(body: str | None) -> str:
    """The platform version out of the body of ``GET /api/settings/version``.

    OpenAEV answers a bare text string (``3.261116.1``, the Java method returns
    ``String``), which ``json.loads`` rejects with "Extra data". A JSON body (a
    quoted string, or an object carrying ``version`` / ``platform_version``) is
    still read, should a later OpenAEV answer JSON. Empty string when the body
    carries no version.
    """
    text = (body or "").strip()
    if not text:
        return ""
    try:
        data = json.loads(text)
    except ValueError:
        return text.strip("\"'").strip()
    if isinstance(data, dict):
        data = data.get("version") or data.get("platform_version") or ""
    if data is None:
        return ""
    return str(data).strip("\"'").strip()


def _runtime_settings_answer(settings: Any) -> str:
    """The ``runtime_settings`` of ``GET /api/settings`` as one line per setting.

    OpenAEV lists the settings an administrator may change at runtime (the
    inject execution threshold among them) with the value in force and its
    source; the full settings object is long enough to be cut by
    ``json_answer``, so this reads the list alone, whole. An OpenAEV without
    the list (before the release carrying OpenAEV #9416) is named as such.
    """
    items = settings.get("runtime_settings") if isinstance(settings, dict) else None
    if not isinstance(items, list):
        return (
            "This OpenAEV does not expose runtime settings (no 'runtime_settings' "
            "in GET /api/settings): it predates the release that makes the inject "
            "execution threshold settable at runtime. Its value is then the "
            "environment variable or the configuration default, changed only by "
            "a redeploy."
        )
    if not items:
        return "OpenAEV lists no runtime setting."
    lines = [f"OpenAEV runtime settings ({len(items)}):"]
    for item in items:
        if not isinstance(item, dict):
            continue
        line = (
            f"  - {item.get('key', '?')} = {item.get('value', '')} "
            f"(source {item.get('source', '?')}, type {item.get('type', '?')}"
        )
        if item.get("min") is not None or item.get("max") is not None:
            line += f", range {item.get('min', '')}..{item.get('max', '')}"
        if item.get("enterprise_only"):
            line += ", Enterprise Edition only"
        line += ")"
        if item.get("locked"):
            line += f" LOCKED by env var {item.get('env_var', '?')}: update_runtime refuses it"
        elif item.get("env_var"):
            line += f" env var {item['env_var']} unset: settable"
        lines.append(line)
    return "\n".join(lines)


logger = logging.getLogger(__name__)

_SOURCE = "integration:openaev"
#: Connector-instance log entries read per call (one page, newest first).
_MAX_LOG_ENTRIES = 50
_CONNECTOR_PATHS = {
    "injectors": "/api/injectors",
    "collectors": "/api/collectors",
    "executors": "/api/executors",
}
#: Actions of manage_openaev_connector_instance that act on an existing instance.
_INSTANCE_ACTIONS = ("get", "update_config", "start", "stop", "logs", "delete")
_CATALOG_ID_IS_NOT_AN_INSTANCE = (
    "A catalog_connector_id names a catalog entry, and several deployments of it "
    "can exist, so it cannot pick one connector instance. Pass connector_id (the "
    "injector, collector or executor id from query_openaev_connectors, which also "
    "shows each connector_instance_id) or connector_instance_id."
)

#: Runtime fields of a connector instance get, in the order they are shown. An
#: OpenAEV that does not send one leaves it out of the answer (older builds send
#: only the id and the two statuses). The keys are the ones OpenAEV already uses
#: for the instance (ConnectorInstancePersisted) and for the composer
#: (XtmComposerInstanceOutput.connector_image).
_INSTANCE_RUNTIME_FIELDS = (
    ("connector_image", "image"),
    ("connector_instance_started_at", "started at"),
    ("connector_instance_restart_count", "restart count"),
    ("connector_instance_is_in_reboot_loop", "in reboot loop"),
)


def _instance_runtime_lines(data: dict[str, object]) -> list[str]:
    """The image and container lines of action='get', each only when OpenAEV sends it.

    When OpenAEV reports no start time, the answer says so, so a reader never
    takes a missing line for a confirmed rolling-image restart.
    """
    lines: list[str] = []
    for key, label in _INSTANCE_RUNTIME_FIELDS:
        value = data.get(key)
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            value = "yes" if value else "no"
        lines.append(f"  {label}: {value}")
    if not data.get("connector_instance_started_at"):
        lines.append(
            "  started at: not reported by this OpenAEV (whether a new image was pulled cannot be read from here)"
        )
    return lines


#: The XTM Composer fields of ``GET /api/settings`` (OpenAEV #9612), in the
#: order they are shown.
_COMPOSER_FIELDS = (
    ("xtm_composer_version", "version"),
    ("xtm_composer_image", "image"),
    ("xtm_composer_last_connectivity_check", "last connectivity check"),
)


def _composer_lines(settings: object) -> list[str]:
    """The XTM Composer the OpenAEV settings report, as answer lines.

    The version and image are the ones XTM Composer itself reports at
    registration and on each connectivity refresh ('unknown' for an XTM
    Composer too old to report one). An OpenAEV that predates these fields, or
    with no XTM Composer registered, is named as such: the platform version is
    never shown in place of the Composer's.
    """
    if not isinstance(settings, dict) or not any(key in settings for key, _ in _COMPOSER_FIELDS):
        return [
            "XTM Composer: not reported by this OpenAEV (no XTM Composer registered, "
            "or an OpenAEV release that does not expose its version and image)."
        ]
    lines = ["XTM Composer:"]
    for key, label in _COMPOSER_FIELDS:
        value = settings.get(key)
        lines.append(f"  {label}: {value if value else 'not reported'}")
    return lines


def register(
    base_url: str,
    headers: dict[str, str],
    executor: CallableToolExecutor,
    *,
    capabilities: PlatformCapabilities,
    registered_version_fn: RegisteredVersionFn | None = None,
) -> list[ToolDefinition]:
    async def query_openaev_connectors(kind: str = "injectors") -> str:
        kind = (kind or "injectors").strip().lower()
        path = _CONNECTOR_PATHS.get(kind)
        if not path:
            return f"kind must be one of: {', '.join(_CONNECTOR_PATHS)}."
        resp, err = await _api(base_url, headers, "get", path, f"list {kind}")
        if err:
            return err
        items = resp.json() or []
        if not items:
            return f"No {kind} found."
        lines = [f"{kind.capitalize()} ({len(items)}):"]
        for c in items:
            name = (
                c.get(f"{kind[:-1]}_name")
                or c.get("name")
                or c.get("injector_name")
                or c.get("collector_name")
                or c.get("executor_name")
                or "?"
            )
            cid = (
                c.get(f"{kind[:-1]}_id")
                or c.get("id")
                or c.get("injector_id")
                or c.get("collector_id")
                or c.get("executor_id")
            )
            active = c.get("injector_state") or c.get("collector_last_execution") or ""
            instance = c.get("connector_instance")
            iid = instance.get("connector_instance_id") if isinstance(instance, dict) else None
            ids = f"id: {cid}, connector_instance_id: {iid}" if iid else f"id: {cid}"
            lines.append(f"- {name} ({ids}){f' [{active}]' if active else ''}")
        lines.append(
            "A connector with a connector_instance_id was deployed from the catalog: "
            "manage_openaev_connector_instance takes that id as connector_instance_id, "
            "or this connector id as connector_id (with connector_kind for a collector "
            "or executor). A connector without one is registered externally."
        )
        # The XTM Composer that deploys the catalog connectors: a failed settings
        # read leaves the connector list whole and says so.
        sresp, serr = await _api(base_url, headers, "get", "/api/settings", "read the XTM Composer")
        if serr:
            lines.append(f"XTM Composer: could not be read ({serr})")
        else:
            lines.extend(_composer_lines(sresp.json() if sresp is not None else None))
        return "\n".join(lines)

    async def _related_ids(connector_id: str, kind: str) -> tuple[dict[str, Any] | None, str]:
        """Resolve a connector id through OpenAEV's ``/{kind}/{id}/related-ids``.

        Returns ``(ids, kind)`` (``ids`` carries connector_instance_id,
        catalog_connector_id, connector_registered) or ``(None, message)``. Without
        *kind* the injectors, collectors and executors are tried in turn, a 404
        moving on to the next one.
        """
        kinds = [kind] if kind else list(_CONNECTOR_PATHS)
        for k in kinds:
            resp, err = await _api(
                base_url,
                headers,
                "get",
                f"{_CONNECTOR_PATHS[k]}/{connector_id}/related-ids",
                f"resolve the connector instance of {k[:-1]} {connector_id}",
            )
            if err:
                if len(kinds) > 1 and is_not_found_error(err):
                    continue
                return None, err
            data = resp.json() if resp is not None else None
            return (data if isinstance(data, dict) else {}), k
        return None, (
            f"No injector, collector or executor with id {connector_id} is visible "
            "on this platform (OpenAEV answers 404 to each; the id may be wrong, or "
            "the API token may lack permission to read it). Take the id from "
            "query_openaev_connectors."
        )

    async def query_openaev_catalog(
        action: str = "list",
        catalog_connector_id: str = "",
        search: str = "",
    ) -> str:
        action = (action or "list").strip().lower()

        if action == "list":
            resp, err = await _api(
                base_url, headers, "get", "/api/catalog-connector", "list catalog"
            )
            if err:
                return err
            items = resp.json() or []
            if not items:
                return (
                    "No catalog connectors available. This can mean none are "
                    "configured on the platform, the API token lacks permission to "
                    "read them, or the platform is unreachable. (Browsing the "
                    "catalog itself does not require Enterprise Edition; deploying a "
                    "connector instance does — EE with a running XTM Composer.)"
                )
            lines = [f"Catalog connectors ({len(items)}):"]
            for c in items:
                title = c.get("catalog_connector_title") or "?"
                cid = c.get("catalog_connector_id")
                ctype = c.get("catalog_connector_type") or "?"
                deployable = c.get("catalog_connector_manager_supported")
                deployed = c.get("instance_deployed_count") or 0
                lines.append(
                    f"- {title} (id: {cid}) type={ctype}"
                    f"{'' if deployable else ' [not deployable]'}"
                    f"{f' deployed={deployed}' if deployed else ''}"
                )
            lines.append(
                "\nUse action='get' with catalog_connector_id to see the parameters "
                "needed before deploying with manage_openaev_connector_instance."
            )
            return "\n".join(lines)

        if action == "get":
            if not catalog_connector_id:
                return "catalog_connector_id is required for action 'get'."
            resp, err = await _api(
                base_url,
                headers,
                "get",
                f"/api/catalog-connector/{catalog_connector_id}",
                "get catalog connector",
            )
            if err:
                return err
            c = resp.json() or {}
            cfg_resp, cfg_err = await _api(
                base_url,
                headers,
                "get",
                f"/api/catalog-connector/{catalog_connector_id}/configurations",
                "get catalog connector configurations",
            )
            if cfg_err:
                return cfg_err
            configs = cfg_resp.json() or []
            lines = [f"Connector: {c.get('catalog_connector_title', '?')}"]
            lines.append(f"  catalog_connector_id: {c.get('catalog_connector_id')}")
            lines.append(f"  type: {c.get('catalog_connector_type')}")
            if c.get("catalog_connector_description"):
                lines.append(
                    f"  description: {c['catalog_connector_description'][:400]}"
                )
            deployable = c.get("catalog_connector_manager_supported")
            lines.append(
                "  deployable: " + ("yes" if deployable else "no (not manager-supported)")
            )
            if not configs:
                lines.append("  (no configurable parameters)")
                return "\n".join(lines)
            lines.append("  Parameters:")
            for cfg in configs:
                key = cfg.get("connector_configuration_key", "?")
                bits = [f"type={cfg.get('connector_configuration_type', 'STRING')}"]
                if cfg.get("connector_configuration_required"):
                    bits.append("REQUIRED")
                fmt = cfg.get("connector_configuration_format")
                if fmt and fmt != "DEFAULT":
                    bits.append(fmt.lower())
                if cfg.get("connector_configuration_format") == "PASSWORD":
                    bits.append("secret")
                default = cfg.get("connector_configuration_default")
                if default not in (None, ""):
                    bits.append(f"default={default}")
                enum = cfg.get("connector_configuration_enum") or []
                if enum:
                    bits.append(f"one of: {', '.join(map(str, enum))}")
                desc = cfg.get("connector_configuration_description") or ""
                suffix = f" — {desc[:160]}" if desc else ""
                lines.append(f"    - {key} ({', '.join(bits)}){suffix}")
            lines.append(
                "\nTo deploy: manage_openaev_connector_instance action='deploy' with "
                "this catalog_connector_id and config (JSON of the parameters above)."
            )
            return "\n".join(lines)

        return (
            f"Unknown action '{action}'. Valid actions: list, get."
        )

    async def manage_openaev_connector_instance(
        action: str = "",
        catalog_connector_id: str = "",
        connector_instance_id: str = "",
        config: str = "",
        connector_id: str = "",
        connector_kind: str = "",
    ) -> str:
        action = (action or "").strip().lower()
        connector_instance_id = (connector_instance_id or "").strip()
        connector_id = (connector_id or "").strip()
        kind = (connector_kind or "").strip().lower()
        if kind and not kind.endswith("s"):
            kind += "s"
        if kind and kind not in _CONNECTOR_PATHS:
            return "connector_kind must be one of: injector, collector, executor."

        resolved_from = ""
        if action in _INSTANCE_ACTIONS and not connector_instance_id:
            if connector_id:
                ids, found = await _related_ids(connector_id, kind)
                if ids is None:
                    # A connector instance id passed as connector_id: OpenAEV
                    # knows no connector by it, but it names an instance.
                    if not is_not_found_error(found) and not found.startswith("No injector"):
                        return found
                    instance_resp, instance_err = await _api(
                        base_url,
                        headers,
                        "get",
                        f"/api/connector-instances/{connector_id}",
                        "get connector instance",
                    )
                    if instance_err:
                        return found
                    instance = instance_resp.json() if instance_resp is not None else None
                    ids = {
                        "connector_instance_id": connector_id,
                        "catalog_connector_id": (instance or {}).get("catalog_connector_id")
                        if isinstance(instance, dict)
                        else None,
                    }
                    found = "connector instances"
                instance_id = ids.get("connector_instance_id")
                if not instance_id:
                    return (
                        f"The {found[:-1]} {connector_id} has no connector instance "
                        "managed by OpenAEV: it was not deployed from the catalog "
                        "(it is registered externally), so it cannot be read, "
                        "started, stopped, reconfigured or deleted here and OpenAEV "
                        "holds no logs for it."
                    )
                connector_instance_id = str(instance_id)
                if not catalog_connector_id:
                    catalog_connector_id = ids.get("catalog_connector_id") or ""
                resolved_from = (
                    " (connector_id was a connector instance id)"
                    if found == "connector instances"
                    else f" (resolved from {found[:-1]} {connector_id})"
                )
            elif catalog_connector_id and action != "update_config":
                return _CATALOG_ID_IS_NOT_AN_INSTANCE
            elif catalog_connector_id:
                return (
                    "connector_instance_id or connector_id is required for action "
                    "'update_config'. " + _CATALOG_ID_IS_NOT_AN_INSTANCE
                )

        def _instance_err(err: str) -> str:
            # The usual mistake: a connector or catalog id passed as the instance id.
            if resolved_from or not is_not_found_error(err):
                return err
            return (
                f"{err} If {connector_instance_id} is an injector, collector or "
                "executor id, pass it as connector_id instead: the tool resolves "
                "its connector instance."
            )

        parsed_config: dict = {}
        if config:
            try:
                parsed_config = json.loads(config)
            except ValueError as exc:
                return f"config must be a valid JSON object: {exc}"
            if not isinstance(parsed_config, dict):
                return "config must be a JSON object of parameter->value pairs."

        def _config_payload() -> list[dict]:
            # Stringify values (None → "") so a JSON null from the LLM
            # (e.g. {"KEY": null}) doesn't send configuration_value: null, which
            # the API rejects. Mirrors the OpenCTI manager_contract_configuration
            # path for a stable, predictable payload shape.
            return [
                {
                    "configuration_key": str(k),
                    "configuration_value": "" if v is None else str(v),
                }
                for k, v in parsed_config.items()
            ]

        if action == "deploy":
            if not catalog_connector_id:
                return (
                    "catalog_connector_id is required for action 'deploy'. Get it "
                    "from query_openaev_catalog."
                )
            body = {
                "catalog_connector_id": catalog_connector_id,
                "connector_instance_configurations": _config_payload(),
            }
            resp, err = await _api(
                base_url,
                headers,
                "post",
                "/api/connector-instances",
                "deploy connector instance",
                json=body,
            )
            if err:
                return err
            data = resp.json() or {}
            new_id = data.get("connector_instance_id") or data.get("id")
            return (
                f"Deployed connector instance (id: {new_id}). Use action='start' to "
                "enable it, or action='get' to check its status."
            )

        if action == "get":
            if not connector_instance_id:
                return "connector_instance_id or connector_id is required for action 'get'."
            resp, err = await _api(
                base_url,
                headers,
                "get",
                f"/api/connector-instances/{connector_instance_id}",
                "get connector instance",
            )
            if err:
                return _instance_err(err)
            data = resp.json() or {}
            lines = [
                f"Connector instance {data.get('connector_instance_id')}{resolved_from}"
            ]
            lines.append(
                f"  current status: {data.get('connector_instance_current_status')}"
            )
            lines.append(
                f"  requested status: {data.get('connector_instance_requested_status')}"
            )
            lines.extend(_instance_runtime_lines(data))
            return "\n".join(lines)

        if action == "update_config":
            if not connector_instance_id:
                return "connector_instance_id or connector_id is required for action 'update_config'."
            if not catalog_connector_id:
                return (
                    "catalog_connector_id is required for action 'update_config' "
                    "(the connector this instance was deployed from)."
                )
            if not parsed_config:
                return "config (JSON of parameter->value pairs) is required for 'update_config'."
            body = {
                "catalog_connector_id": catalog_connector_id,
                "connector_instance_configurations": _config_payload(),
            }
            _, err = await _api(
                base_url,
                headers,
                "put",
                f"/api/connector-instances/{connector_instance_id}/configurations",
                "update connector instance configuration",
                json=body,
            )
            if err:
                return _instance_err(err)
            return f"Updated configuration for connector instance {connector_instance_id}."

        if action in ("start", "stop"):
            if not connector_instance_id:
                return f"connector_instance_id or connector_id is required for action '{action}'."
            status = "starting" if action == "start" else "stopping"
            _, err = await _api(
                base_url,
                headers,
                "put",
                f"/api/connector-instances/{connector_instance_id}/requested-status",
                f"{action} connector instance",
                json={"connector_instance_requested_status": status},
            )
            if err:
                return _instance_err(err)
            return f"Connector instance {connector_instance_id} is now {status}."

        if action == "logs":
            if not connector_instance_id:
                return "connector_instance_id or connector_id is required for action 'logs'."
            # OpenAEV only exposes the paginated search (ConnectorInstanceApi:
            # POST .../logs/search, a SearchPaginationInput body), newest first.
            resp, err = await _api(
                base_url,
                headers,
                "post",
                f"/api/connector-instances/{connector_instance_id}/logs/search",
                "search connector instance logs",
                json=_search_body(size=_MAX_LOG_ENTRIES),
            )
            if err:
                return _instance_err(err)
            data = resp.json()
            page = data if isinstance(data, dict) else {}
            content = page.get("content")
            items = content[:_MAX_LOG_ENTRIES] if isinstance(content, list) else []
            if not items:
                return f"No logs for connector instance {connector_instance_id}{resolved_from}."
            # Oldest first, so the most recent entry reads last. Capped by entry
            # count, never by slicing the serialized string (which could cut a
            # token mid-string and return invalid JSON); a marker says the page
            # holds only the most recent entries.
            items.reverse()
            total = page.get("totalElements")
            payload: dict[str, Any] = {}
            if resolved_from:
                payload["connector_instance_id"] = connector_instance_id
            if isinstance(total, int) and total > len(items):
                payload["truncated"] = f"showing the most recent {len(items)} of {total} log entries"
            payload["logs"] = items
            return json.dumps(payload, default=str)

        if action == "delete":
            if not connector_instance_id:
                return "connector_instance_id or connector_id is required for action 'delete'."
            _, err = await _api(
                base_url,
                headers,
                "delete",
                f"/api/connector-instances/{connector_instance_id}",
                "delete connector instance",
            )
            if err:
                return _instance_err(err)
            return f"Deleted connector instance {connector_instance_id}."

        return (
            f"Unknown action '{action}'. Valid actions: deploy, get, update_config, "
            "start, stop, logs, delete."
        )

    async def manage_openaev_settings(
        action: str = "",
        config: str = "",
        class_name: str = "",
    ) -> str:
        action = (action or "").strip().lower()

        extra: dict = {}
        if config:
            try:
                extra = json.loads(config)
            except ValueError as exc:
                return f"config must be a valid JSON object: {exc}"
            if not isinstance(extra, dict):
                return "config must be a JSON object of field->value pairs."

        if action == "capabilities":
            version = await capabilities.version()
            tenant_id = await capabilities.tenant_id()
            features = await capabilities.enabled_features()
            lines = ["OpenAEV platform capabilities:"]
            lines.append(f"  Version: {version or 'unknown'}")
            lines.append(f"  Default tenant id: {tenant_id or 'unavailable'}")
            if capabilities.has_all_features_wildcard(features):
                lines.append(
                    "  Preview features: ALL enabled (FEATURE_FLAG_ALL '*' is set)"
                )
            elif features:
                lines.append(f"  Preview features enabled: {', '.join(features)}")
            else:
                lines.append("  Preview features enabled: none")
            lines.append(
                "  Note: endpoints belonging to a DISABLED preview feature answer "
                "404, not 403. ATTACK_PATH gates the attack-path tools and "
                "INJECT_CHAINING gates the workflow/chaining tools."
            )
            return "\n".join(lines)

        if action == "filter_keys":
            if not class_name:
                return (
                    "class_name is required for 'filter_keys' - the OpenAEV model "
                    "class to introspect, e.g. Asset, Payload, Exercise, "
                    "Scenario, Finding, Vulnerability, InjectExpectation."
                )
            resp, err = await _api(
                base_url, headers, "post", f"/api/schemas/{class_name}",
                "list filterable attributes",
                params={"filterableOnly": "true"}, json=[],
            )
            schemas = (resp.json() or []) if resp is not None and not err else []
            if not schemas:
                engine_lines = await _engine_keys_lines(base_url, headers, class_name)
                if engine_lines:
                    return "\n".join(engine_lines)
                if err:
                    return err
                return (
                    f"No filterable attributes reported for '{class_name}'. Check "
                    "the class name (it is the OpenAEV model class, e.g. Asset)."
                )
            lines = [
                f"Filterable attributes of {class_name} ({len(schemas)}) - use these "
                "verbatim as filter 'key' values:"
            ]
            for s in schemas:
                if not isinstance(s, dict):
                    continue
                name = s.get("schema_property_name") or s.get("name") or "?"
                ptype = s.get("schema_property_type") or s.get("type") or ""
                values = s.get("schema_property_values") or []
                line = f"  - {name}"
                if ptype:
                    line += f" ({ptype})"
                if values:
                    line += f" values: {', '.join(str(v) for v in values[:12])}"
                lines.append(line)
            lines.append(
                "OpenAEV silently coerces an unknown filter key's operator to 'eq' "
                "instead of returning an error, so always take keys from this list "
                "rather than guessing."
            )
            lines.extend(await _engine_keys_lines(base_url, headers, class_name))
            return "\n".join(lines)

        if action == "get":
            resp, err = await _api(base_url, headers, "get", "/api/settings", "get settings")
            if err:
                return err
            return json_answer(resp.json())

        if action == "get_runtime":
            resp, err = await _api(base_url, headers, "get", "/api/settings", "get settings")
            if err:
                return err
            return _runtime_settings_answer(resp.json() if resp is not None else None)

        if action == "update_runtime":
            values = extra.get("settings", extra) if extra else {}
            if not isinstance(values, dict) or not values:
                return (
                    "config is required for 'update_runtime': a JSON object of "
                    "runtime setting key -> value, e.g. "
                    '{"inject_execution_threshold_minutes": 60}. Read the keys, '
                    "bounds and locks with action 'get_runtime' first."
                )
            resp, err = await _api(
                base_url,
                headers,
                "put",
                "/api/settings/runtime",
                "update runtime settings",
                json={"settings": values},
            )
            if err:
                return err
            return "Updated runtime settings.\n" + _runtime_settings_answer(resp.json() if resp is not None else None)

        if action == "version":
            resp, err = await _api(
                base_url, headers, "get", "/api/settings/version", "get version"
            )
            version = "" if err else _version_from_body(resp.text if resp is not None else "")[:200]
            if err:
                answer = err
            elif not version:
                answer = (
                    "OpenAEV answered GET /api/settings/version without a version "
                    "(empty body): the platform version could not be read."
                )
            else:
                answer = f"OpenAEV version: {json.dumps({'version': version})}"
            if registered_version_fn is None:
                return answer
            return version_answer("OpenAEV", answer, version, await registered_version_fn())

        if action == "update_policies":
            if not extra:
                return "config (JSON of policy fields) is required for 'update_policies'."
            _, err = await _api(
                base_url, headers, "put", "/api/settings/policies", "update policies",
                json=extra,
            )
            if err:
                return err
            return "Updated platform policies."

        if action == "update_enterprise":
            if not extra:
                return "config (JSON, e.g. {\"platform_enterprise_license\": \"...\"}) is required."
            _, err = await _api(
                base_url, headers, "put", "/api/settings/enterprise-edition",
                "update enterprise edition", json=extra,
            )
            if err:
                return err
            return "Updated enterprise edition settings."

        return (
            f"Unknown action '{action}'. Valid actions: capabilities, filter_keys, "
            "get, get_runtime, update_runtime, version, update_policies, "
            "update_enterprise."
        )

    executor.register("query_openaev_connectors", query_openaev_connectors)
    executor.register("query_openaev_catalog", query_openaev_catalog)
    executor.register(
        "manage_openaev_connector_instance", manage_openaev_connector_instance
    )
    executor.register("manage_openaev_settings", manage_openaev_settings)

    return [
        ToolDefinition(
            name="query_openaev_connectors",
            description=(
                "List OpenAEV connectors (read-only). kind: 'injectors' (inject "
                "delivery types), 'collectors' (security platform data collectors), or "
                "'executors' (agent execution backends). Also shows the XTM Composer "
                "version, image and last connectivity check."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["injectors", "collectors", "executors"],
                        "default": "injectors",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="query_openaev_catalog",
            description=(
                "Browse the OpenAEV connector catalog (collectors / injectors / "
                "executors that can be deployed via the XTM Composer). "
                "action='list' lists available catalog connectors; action='get' "
                "(with catalog_connector_id) shows a connector's configuration "
                "parameters — including which are required and which are secrets — "
                "so you know what to ask the user for before deploying."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "get"],
                        "default": "list",
                    },
                    "catalog_connector_id": {
                        "type": "string",
                        "description": "Catalog connector id for action='get'.",
                    },
                    "search": {
                        "type": "string",
                        "description": "Reserved for future filtering.",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="manage_openaev_connector_instance",
            description=(
                "Deploy and manage OpenAEV connector instances from the catalog "
                "(collectors, injectors, executors). Actions: 'deploy' "
                "(catalog_connector_id + config JSON of the connector's "
                "parameters), 'get' (connector_instance_id), 'update_config' "
                "(connector_instance_id + catalog_connector_id + config), "
                "'start'/'stop' (connector_instance_id), 'logs' "
                "(connector_instance_id), 'delete' (connector_instance_id). For a "
                "connector deployed from the UI, pass its injector, collector or "
                "executor id as connector_id instead of connector_instance_id. "
                "Requires Enterprise Edition with a running XTM Composer; secret "
                "values are encrypted server-side. Always confirm required "
                "parameters with query_openaev_catalog get before deploying, and "
                "ask the user for any secret values (e.g. API keys)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "deploy",
                            "get",
                            "update_config",
                            "start",
                            "stop",
                            "logs",
                            "delete",
                        ],
                    },
                    "catalog_connector_id": {
                        "type": "string",
                        "description": "Catalog connector to deploy from (deploy/update_config).",
                    },
                    "connector_instance_id": {
                        "type": "string",
                        "description": "Existing connector instance id.",
                    },
                    "connector_id": {
                        "type": "string",
                        "description": (
                            "Injector, collector or executor id (query_openaev_"
                            "connectors); resolved to its connector instance."
                        ),
                    },
                    "connector_kind": {
                        "type": "string",
                        "enum": ["injector", "collector", "executor"],
                        "description": "Kind of connector_id; tried in turn when omitted.",
                    },
                    "config": {
                        "type": "string",
                        "description": (
                            "JSON object of the connector's configuration "
                            "parameters, e.g. {\"ALIENVAULT_API_KEY\": \"...\"}. "
                            "Keys come from query_openaev_catalog get."
                        ),
                    },
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="manage_openaev_settings",
            description=(
                "Read or update OpenAEV platform settings, and discover what this "
                "platform supports. Actions: 'capabilities' (version, default "
                "tenant id and which PREVIEW FEATURES are enabled - check this "
                "first when an endpoint unexpectedly 404s, because disabled "
                "preview features answer 404 rather than 403), 'filter_keys' "
                "(class_name - the valid filter 'key' names for an entity, and "
                "the engine keys query_openaev_analytics accepts; "
                "OpenAEV silently coerces an unknown filter operator to 'eq' "
                "instead of erroring, so never guess keys), 'get' (full "
                "settings), 'get_runtime' (the runtime settings in full: inject "
                "execution threshold, agent jobs and queue, agent auto-update, each "
                "with its value, source env/ui/default, bounds and lock), "
                "'update_runtime' (config = JSON of runtime key -> value, e.g. "
                '{"inject_execution_threshold_minutes": 60}; a setting pinned by '
                "its environment variable is refused, admin), 'version', "
                "'update_policies' (config = JSON of policy "
                "fields, admin), 'update_enterprise' (config = JSON "
                "enterprise-edition settings, admin)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "capabilities", "filter_keys", "get", "get_runtime",
                            "update_runtime", "version",
                            "update_policies", "update_enterprise",
                        ],
                    },
                    "class_name": {
                        "type": "string",
                        "description": (
                            "OpenAEV model class to introspect for 'filter_keys', "
                            "e.g. Asset, Payload, Exercise, Scenario, Finding, "
                            "Vulnerability, InjectExpectation."
                        ),
                    },
                    "config": {"type": "string", "description": "JSON object body for update_* actions."},
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
    ]
