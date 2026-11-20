"""OpenCTI connector catalog + managed-connector deployment tools.

These tools let an agent browse the OpenCTI **Enterprise Edition** connector
catalog and deploy / enable connectors from it (the "Composer" managed
connectors), supplying the per-connector configuration (e.g. an AlienVault OTX
API key) interactively.

* ``query_opencti_catalog``    - browse the catalog: list catalogs, list the
  available connector contracts, and inspect a single contract's configuration
  schema (so the agent knows which parameters — and which are required /
  secret — to ask the user for before deploying).
* ``manage_opencti_connector`` - deploy a connector from the catalog
  (``managedConnectorAdd``), list deployed connectors, start/stop them
  (``updateConnectorRequestedStatus``), update configuration
  (``managedConnectorEdit``), and delete them (``deleteConnector``).

Prerequisites (enforced server-side, surfaced as friendly errors here):
  * OpenCTI Enterprise Edition with a registered **connector manager**
    (XTM Composer). Without it, ``managedConnectorAdd`` fails with
    "There is no connector manager configured".
  * The API token's user needs ``CONNECTORAPI`` (deploy/edit) and
    ``MODULES_MODMANAGE`` (start/stop/delete) capabilities.

Secret configuration values (``format: "password"``) are RSA-encrypted
**server-side** by OpenCTI using the connector manager's public key, so this
tool only ever sends plaintext values over the (TLS) GraphQL transport — it
never performs client-side crypto. Not draft-gated (platform configuration,
not STIX knowledge).
"""

from __future__ import annotations

import json
import logging
import re

from app.core.redaction import redact_secrets
from app.core.utils import format_exception_message
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import _client, _graphql
from .access import _refuse_unapplied

logger = logging.getLogger(__name__)

_SOURCE = "integration:opencti"
# The fields each action of manage_opencti_connector reads; any other field is refused.
_CONNECTOR_APPLIES: dict[str, frozenset[str]] = {
    "list": frozenset(),
    "get": frozenset({"connector_id"}),
    "logs": frozenset({"connector_id", "tail"}),
    "deploy": frozenset(
        {"name", "manager_contract_image", "catalog_id", "config", "user_id", "automatic_user", "confidence_level"}
    ),
    "update_config": frozenset({"connector_id", "name", "config", "user_id", "replace"}),
    "start": frozenset({"connector_id"}),
    "stop": frozenset({"connector_id"}),
    "delete": frozenset({"connector_id"}),
}

# Substrings that mark a configuration key as sensitive. OpenCTI encrypts
# password-format values server-side, but its ``ManagerContractConfiguration``
# type exposes only ``key`` and ``value`` (no flag saying which value is a
# secret), so every value is treated as possibly secret: a value is redacted
# when its key name looks sensitive or when the value itself looks like the
# ciphertext OpenCTI stores. Over-redaction here is harmless — the agent
# supplied these values and never needs them read back.
_SECRET_KEY_MARKERS = (
    "key",
    "token",
    "secret",
    "password",
    "passwd",
    "passphrase",
    "credential",
    "private",
)


def _looks_secret(key: str) -> bool:
    lowered = (key or "").lower()
    return any(marker in lowered for marker in _SECRET_KEY_MARKERS)


# A base64 run this long with no space is an encrypted blob, never a setting.
_CIPHERTEXT_RE = re.compile(r"^[A-Za-z0-9+/=_-]{64,}$")


def _looks_encrypted(value: object) -> bool:
    return isinstance(value, str) and bool(_CIPHERTEXT_RE.match(value.strip()))


# Bounds of action='logs': XTM Composer keeps a rolling window of a managed
# connector's output in Connector.manager_connector_logs; a stack trace line
# can be long, so each line and the whole answer are capped.
_LOG_TAIL_DEFAULT = 50
_LOG_TAIL_MAX = 500
_LOG_GET_TAIL = 5
_LOG_LINE_CAP = 1000
_LOG_TOTAL_CAP = 30000

_LOGS_QUERY = (
    "query ConnectorLogs($id: String!) { connector(id: $id) {"
    " id name is_managed manager_current_status manager_requested_status"
    " manager_connector_uptime manager_connector_logs } }"
)


def _coerce_tail(raw: object) -> int | None:
    """*raw* as a line count in ``[1, _LOG_TAIL_MAX]``, the default when unset."""
    if raw is None or raw == "":
        return _LOG_TAIL_DEFAULT
    if isinstance(raw, bool):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    if value < 1:
        return None
    return min(value, _LOG_TAIL_MAX)


def _format_uptime(seconds: object) -> str | None:
    if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 0:
        return None
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _tail_log_lines(raw_lines: object, tail: int) -> tuple[list[str], int]:
    """The last *tail* lines, oldest first, redacted and bounded.

    Returns the lines kept and how many of the requested ones the total cap
    dropped (the oldest go first, so the newest error always survives).
    """
    lines = [str(line) for line in (raw_lines or []) if line is not None]
    picked: list[str] = []
    for line in lines[-tail:]:
        clean = redact_secrets(line.rstrip("\n"))
        if len(clean) > _LOG_LINE_CAP:
            clean = clean[:_LOG_LINE_CAP] + " [line cut]"
        picked.append(clean)
    dropped = 0
    total = sum(len(line) + 1 for line in picked)
    while picked and total > _LOG_TOTAL_CAP:
        total -= len(picked.pop(0)) + 1
        dropped += 1
    return picked, dropped


def _logs_field_missing(err: str) -> bool:
    """True when OpenCTI refused the query because it predates the log fields."""
    lowered = (err or "").lower()
    return "cannot query field" in lowered and (
        "manager_connector_logs" in lowered or "manager_connector_uptime" in lowered
    )


def _coerce_confidence_level(raw: object) -> int | None:
    """Coerce *raw* to a bounded confidence ``int`` in ``[0, 100]`` or ``None``.

    OpenCTI types the connector confidence as a String and ``parseInt()``s it,
    so a malformed or out-of-range value silently becomes an unintended (or
    effectively absent) confidence on the auto-created service-account user —
    the exact data-quality risk these tools guard against. The tool executor
    forwards the LLM's raw JSON argument without coercion, so this is the single
    chokepoint that validates *every* confidence value (the caller-supplied
    ``confidence_level`` and the contract default alike) before it is sent.

    Accepts ``int``, integral ``float``, and numeric ``str``; rejects ``bool``
    (an ``int`` subclass — ``True``/``False`` must not masquerade as a
    confidence of 1/0), non-integral numbers, unparseable strings, and anything
    outside ``[0, 100]``. Returns ``None`` for any invalid value so the caller
    can fail safe instead of forwarding garbage.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, float) and raw.is_integer():
        value = int(raw)
    elif isinstance(raw, str):
        try:
            value = int(raw.strip())
        except (ValueError, AttributeError):
            return None
    else:
        return None
    return value if 0 <= value <= 100 else None


def _contract_confidence_default(contract: dict) -> int | None:
    """Return the connector contract's default confidence (``max_confidence_level``).

    OpenCTI catalog contracts carry a ``max_confidence_level`` that the native
    deploy UI uses as the **default** confidence for the connector's
    (auto-created) service-account user. We mirror that default so an autonomous
    deploy doesn't create a service account with *no* confidence level — which
    would let the connector overwrite higher-confidence platform data.

    Returns a bounded ``int`` in ``[0, 100]`` or ``None`` when the value is
    absent or malformed (so the caller can decide how to proceed).
    """
    return _coerce_confidence_level(contract.get("max_confidence_level"))


def _contract_config_value(value: object) -> str:
    """Serialise one ``manager_contract_configuration`` value for OpenCTI.

    The managed connector input carries every value as a string, and OpenCTI
    validates it against the connector contract's type: a boolean field takes
    ``"true"`` or ``"false"`` only, so Python's ``str(True)`` (``"True"``) is
    refused ("must be a boolean value (true or false)"). Booleans are written
    in their JSON form, lists and objects as JSON (never a Python repr), None
    as an empty string, and anything else with ``str()``.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        return json.dumps(value, separators=(",", ":"))
    return str(value)


def _contract_configuration(config: dict[str, object]) -> list[dict[str, str]]:
    """Build the ``manager_contract_configuration`` list of key/value pairs."""
    return [{"key": str(k), "value": _contract_config_value(v)} for k, v in config.items()]


# Keys OpenCTI adds to the configuration it serves (runtime identity, platform
# access, proxy settings): never part of the connector's own settings, so an
# edit never sends them back.
_RUNTIME_CONFIG_KEYS = frozenset(
    {
        "CONNECTOR_ID",
        "CONNECTOR_NAME",
        "CONNECTOR_TYPE",
        "CONNECTOR_RUN_AND_TERMINATE",
        "OPENCTI_TOKEN",
        "OPENCTI_URL",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "HTTPS_CA_CERTIFICATES",
        "HTTPS_PROXY_REJECT_UNAUTHORIZED",
    }
)
# What action='get' prints in place of a hidden value: never a value to send.
_HIDDEN_VALUE_PLACEHOLDERS = frozenset({"<redacted (secret-like key)>", "<encrypted>"})

_CONNECTOR_CONFIG_QUERY = (
    "query ConnectorConfig($id: String!) { connector(id: $id) {"
    " id name connector_user_id manager_contract_definition"
    " manager_contract_configuration { key value } } }"
)
# An OpenCTI older than Connector.manager_contract_definition: no contract
# schema, secrets are then recognised by their ciphertext shape.
_CONNECTOR_CONFIG_QUERY_NO_CONTRACT = (
    "query ConnectorConfig($id: String!) { connector(id: $id) {"
    " id name connector_user_id manager_contract_configuration { key value } } }"
)


def _contract_schema(definition: object) -> tuple[dict[str, dict[str, object]], frozenset[str]]:
    """The properties and required keys of a stored contract definition (JSON)."""
    if not isinstance(definition, str) or not definition.strip():
        return {}, frozenset()
    try:
        contract = json.loads(definition)
    except (ValueError, TypeError):
        return {}, frozenset()
    schema = contract.get("config_schema") if isinstance(contract, dict) else None
    if not isinstance(schema, dict):
        return {}, frozenset()
    props = schema.get("properties")
    if not isinstance(props, dict):
        return {}, frozenset()
    raw_required = schema.get("required")
    required = raw_required if isinstance(raw_required, list) else []
    return (
        {str(k): v for k, v in props.items() if isinstance(v, dict)},
        frozenset(str(k) for k in required),
    )


def _merge_contract_configuration(
    stored: object,
    properties: dict[str, dict[str, object]],
    provided: dict[str, object],
    *,
    replace: bool,
    required: frozenset[str] = frozenset(),
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """The configuration an edit sends, and what it changes, keeps and preserves.

    OpenCTI rebuilds the whole stored configuration from the contract on every
    edit: a key it does not receive keeps its stored value only when it is a
    secret (``format: password``) and is otherwise reset to its default or
    dropped. So a merge sends every stored non-secret setting with its stored
    value, overlaid with *provided*; a secret the caller did not pass is never
    sent (OpenCTI keeps it), nor is anything that looks encrypted. With
    *replace* only *provided* is sent. The report lists key names, never values:
    a secret is reported preserved when it is stored or required (OpenCTI serves
    no encrypted value, so a required secret is the stored one).
    """
    secret_keys = {k for k, p in properties.items() if p.get("format") == "password"}
    secrets = {k for k in secret_keys if k in required}
    stored_values: dict[str, str] = {}
    for item in stored if isinstance(stored, list) else []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "")
        value = item.get("value")
        if key in secret_keys:
            secrets.add(key)
            continue
        if not key or value is None or key in _RUNTIME_CONFIG_KEYS:
            continue
        if properties:
            if key not in properties:
                continue
        elif _looks_encrypted(value):
            # No contract to read: a ciphertext-shaped value is a secret.
            secrets.add(key)
            continue
        stored_values[key] = str(value)
    sent_provided = {str(k): _contract_config_value(v) for k, v in provided.items()}
    to_send = {} if replace else dict(stored_values)
    to_send.update(sent_provided)
    report = {
        "changed": [k for k, v in sent_provided.items() if stored_values.get(k) != v],
        "unchanged": [k for k, v in sent_provided.items() if stored_values.get(k) == v],
        "kept": [] if replace else [k for k in stored_values if k not in sent_provided],
        "reset": [k for k in stored_values if k not in sent_provided] if replace else [],
        "secrets": sorted(k for k in secrets if k not in sent_provided),
    }
    return to_send, report


def _config_update_report(node: dict[str, object], report: dict[str, list[str]], *, replace: bool) -> str:
    """The answer of update_config: key names per outcome, never a value."""
    head = f"connector '{node.get('name')}' (id: {node.get('id')})"
    if replace:
        lines = [
            f"Replaced the configuration of {head}: only the keys sent are set, "
            "every other setting is back to its contract default (a secret keeps "
            "its stored value)."
        ]
    else:
        lines = [f"Updated the configuration of {head}, merged with the stored configuration."]
    labels = (
        ("changed", "changed"),
        ("unchanged", "sent with its stored value"),
        ("kept", "kept as stored"),
        ("reset", "reset to the contract default"),
        ("secrets", "secret preserved"),
    )
    for field, label in labels:
        if report.get(field):
            lines.append(f"  {label}: {', '.join(report[field])}")
    return "\n".join(lines)


def _summarize_contract(contract: dict) -> list[str]:
    """Render a one-connector summary (no config schema) for catalog listings."""
    title = contract.get("title") or contract.get("slug") or "?"
    lines = [f"- {title}"]
    if contract.get("slug"):
        lines.append(f"    slug: {contract['slug']}")
    if contract.get("container_image"):
        lines.append(f"    image: {contract['container_image']}")
    if contract.get("container_type"):
        lines.append(f"    type: {contract['container_type']}")
    manager_supported = contract.get("manager_supported")
    lines.append(
        "    deployable: "
        + ("yes" if manager_supported else "no (not manager-supported)")
    )
    if contract.get("short_description"):
        lines.append(f"    {contract['short_description'][:160]}")
    return lines


def _describe_contract_config(contract: dict) -> list[str]:
    """Render a contract's full configuration schema for the deploy step."""
    lines: list[str] = []
    title = contract.get("title") or contract.get("slug") or "?"
    lines.append(f"Connector: {title}")
    if contract.get("slug"):
        lines.append(f"  slug: {contract['slug']}")
    if contract.get("container_image"):
        lines.append(f"  manager_contract_image: {contract['container_image']}")
    if contract.get("container_type"):
        lines.append(f"  type: {contract['container_type']}")
    if contract.get("description"):
        lines.append(f"  description: {contract['description'][:400]}")
    manager_supported = contract.get("manager_supported")
    lines.append(
        "  deployable: "
        + ("yes" if manager_supported else "no (not manager-supported)")
    )
    conf_default = _contract_confidence_default(contract)
    if conf_default is not None:
        lines.append(
            f"  default confidence level: {conf_default} "
            "(applied to the auto-created service-account user on deploy "
            "unless you override confidence_level)"
        )

    schema = contract.get("config_schema") or {}
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    if not props:
        lines.append("  (no configurable parameters)")
        return lines

    lines.append("  Parameters:")
    for key, prop in props.items():
        if not isinstance(prop, dict):
            continue
        bits = [f"type={prop.get('type', 'string')}"]
        if key in required:
            bits.append("REQUIRED")
        if prop.get("format") == "password":
            bits.append("secret")
        if prop.get("default") not in (None, ""):
            bits.append(f"default={prop['default']}")
        desc = prop.get("description") or ""
        suffix = f" — {desc[:160]}" if desc else ""
        lines.append(f"    - {key} ({', '.join(bits)}){suffix}")
    return lines


def register(
    base_url: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    async def _gql(query: str, variables: dict | None = None) -> tuple[dict, str | None]:
        async with _client(headers, timeout=30) as client:
            return await _graphql(client, base_url, headers, query, variables)

    async def _load_catalogs() -> tuple[list[dict], str | None]:
        """Fetch all catalogs and parse their JSON-string contracts in place."""
        query = (
            "query Catalogs { catalogs { id name description contracts } }"
        )
        data, err = await _gql(query)
        if err:
            return [], err
        catalogs = data.get("catalogs") or []
        parsed: list[dict] = []
        for cat in catalogs:
            contracts: list[dict] = []
            for raw in cat.get("contracts") or []:
                try:
                    contracts.append(json.loads(raw))
                except (ValueError, TypeError) as exc:
                    # An unparseable contract is exceptional (unexpected payload
                    # or an API change). Skip it so one bad entry doesn't hide
                    # the rest, but log it so the now-missing connector is
                    # diagnosable instead of silently disappearing.
                    logger.warning(
                        "Skipping unparseable connector contract in catalog %s: %s",
                        cat.get("id"),
                        format_exception_message(exc),
                    )
                    continue
            parsed.append(
                {
                    "id": cat.get("id"),
                    "name": cat.get("name"),
                    "description": cat.get("description"),
                    "contracts": contracts,
                }
            )
        return parsed, None

    # ── Catalog browsing ──────────────────────────────────────────────────
    async def query_opencti_catalog(
        action: str = "list",
        catalog_id: str = "",
        slug: str = "",
        search: str = "",
    ) -> str:
        action = (action or "list").strip().lower()

        if action == "list":
            catalogs, err = await _load_catalogs()
            if err:
                return err
            if not catalogs:
                return (
                    "No connector catalogs available. This can mean none are "
                    "configured on the platform, the API token lacks permission to "
                    "read them, or the platform is unreachable. (Browsing the "
                    "catalog itself does not require Enterprise Edition; deploying a "
                    "connector does — EE with a running connector manager.)"
                )
            lines = [f"Connector catalogs ({len(catalogs)}):"]
            for cat in catalogs:
                lines.append(
                    f"- {cat['name']} (id: {cat['id']}) — "
                    f"{len(cat['contracts'])} connectors"
                )
            lines.append(
                "\nUse action='contracts' (optionally catalog_id / search) to list "
                "connectors, then action='get' with a slug to see its parameters."
            )
            return "\n".join(lines)

        if action == "contracts":
            catalogs, err = await _load_catalogs()
            if err:
                return err
            # ``search`` can arrive as ``None`` (the LLM may emit a JSON null
            # even though the schema says string), so guard before ``.strip()``
            # — matching the ``(action or ...)`` pattern used throughout.
            term = (search or "").strip().lower()
            out: list[str] = []
            total = 0
            for cat in catalogs:
                if catalog_id and cat["id"] != catalog_id:
                    continue
                matches = []
                for c in cat["contracts"]:
                    hay = (
                        f"{c.get('title', '')} {c.get('slug', '')} "
                        f"{c.get('short_description', '')} "
                        f"{' '.join(c.get('use_cases') or [])}"
                    ).lower()
                    if term and term not in hay:
                        continue
                    matches.append(c)
                if not matches:
                    continue
                out.append(f"\n# {cat['name']} ({len(matches)})")
                for c in matches:
                    out.extend(_summarize_contract(c))
                    total += 1
            if not total:
                return (
                    "No matching connectors found"
                    + (f" for '{search}'." if search else ".")
                )
            return f"Connectors ({total}):" + "\n".join(out)

        if action == "get":
            if not slug:
                return "slug is required for action 'get'. List options with action='contracts'."
            query = (
                "query Contract($slug: String!) {"
                "  contract(slug: $slug) { catalog_id contract } }"
            )
            data, err = await _gql(query, {"slug": slug})
            if err:
                return err
            node = data.get("contract")
            if not node or not node.get("contract"):
                return f"No connector contract found with slug '{slug}'."
            try:
                contract = json.loads(node["contract"])
            except (ValueError, TypeError):
                return f"Connector contract '{slug}' could not be parsed."
            lines = _describe_contract_config(contract)
            lines.append(f"  catalog_id: {node.get('catalog_id')}")
            lines.append(
                "\nTo deploy: manage_opencti_connector action='deploy' with this "
                "manager_contract_image, the catalog_id above, a name, config (JSON "
                "of the parameters above), and either user_id or automatic_user=true "
                "+ a username. With automatic_user, the new service-account user "
                "inherits this connector's default confidence level (above) unless "
                "you deliberately override confidence_level."
            )
            return "\n".join(lines)

        return (
            f"Unknown action '{action}'. Valid actions: list (catalogs), "
            "contracts (available connectors), get (one connector's parameters)."
        )

    # ── Managed connector lifecycle ───────────────────────────────────────
    async def manage_opencti_connector(
        action: str = "",
        connector_id: str = "",
        name: str = "",
        manager_contract_image: str = "",
        catalog_id: str = "",
        config: str = "",
        user_id: str = "",
        automatic_user: bool = False,
        confidence_level: int | None = None,
        tail: int | None = None,
        replace: bool = False,
        **extra: object,
    ) -> str:
        action = (action or "").strip().lower()
        # A field the action ignores (or an undeclared one) is refused by name:
        # nothing is sent, never a silent "Deleted" or "Started".
        refused = _refuse_unapplied(
            action,
            _CONNECTOR_APPLIES,
            {
                "connector_id": connector_id,
                "name": name,
                "manager_contract_image": manager_contract_image,
                "catalog_id": catalog_id,
                "config": config,
                "user_id": user_id,
                "automatic_user": automatic_user,
                "confidence_level": confidence_level,
                "tail": tail,
                "replace": replace,
            },
            dict(extra),
        )
        if refused:
            return refused

        parsed_config: dict = {}
        if config:
            try:
                parsed_config = json.loads(config)
            except ValueError as exc:
                return f"config must be a valid JSON object: {exc}"
            if not isinstance(parsed_config, dict):
                return "config must be a JSON object of parameter->value pairs."

        if action == "list":
            query = (
                "query Connectors { connectors {"
                " id name active is_managed connector_type"
                " manager_current_status manager_requested_status"
                " manager_contract_image } }"
            )
            data, err = await _gql(query)
            if err:
                return err
            conns = [c for c in (data.get("connectors") or []) if c.get("is_managed")]
            if not conns:
                return (
                    "No managed (catalog-deployed) connectors found. Deploy one with "
                    "action='deploy' after browsing query_opencti_catalog."
                )
            lines = [f"Managed connectors ({len(conns)}):"]
            for c in conns:
                status = c.get("manager_current_status") or "unknown"
                requested = c.get("manager_requested_status")
                state = status + (
                    f" (→{requested})" if requested and requested != status else ""
                )
                lines.append(
                    f"- {c.get('name')} [{state}] (id: {c.get('id')}) "
                    f"image: {c.get('manager_contract_image')}"
                )
            return "\n".join(lines)

        if action == "get":
            if not connector_id:
                return "connector_id is required for action 'get'."
            query = (
                "query Connector($id: String!) { connector(id: $id) {"
                " id name active is_managed connector_type connector_scope"
                " manager_current_status manager_requested_status"
                " manager_contract_image"
                " manager_contract_configuration { key value } } }"
            )
            data, err = await _gql(query, {"id": connector_id})
            if err:
                return err
            c = data.get("connector")
            if not c:
                return f"Connector {connector_id} not found."
            lines = [f"Connector '{c.get('name')}' (id: {c.get('id')})"]
            lines.append(f"  managed: {bool(c.get('is_managed'))}")
            lines.append(f"  type: {c.get('connector_type')}")
            lines.append(
                f"  status: {c.get('manager_current_status')} "
                f"(requested: {c.get('manager_requested_status')})"
            )
            if c.get("manager_contract_image"):
                lines.append(f"  image: {c['manager_contract_image']}")
            cfg = c.get("manager_contract_configuration") or []
            if cfg:
                lines.append("  configuration:")
                for item in cfg:
                    if not isinstance(item, dict):
                        continue
                    key = item.get("key")
                    # OpenCTI says nothing about which value is a secret: redact
                    # on the key name and on a ciphertext-looking value.
                    raw = item.get("value")
                    if _looks_secret(str(key or "")):
                        val = "<redacted (secret-like key)>"
                    elif _looks_encrypted(raw):
                        val = "<encrypted>"
                    else:
                        val = raw
                    lines.append(f"    - {key}: {val}")
            if c.get("is_managed"):
                # A separate read so an OpenCTI without the log fields still
                # answers 'get'; its error only drops the excerpt.
                logs_data, logs_err = await _gql(_LOGS_QUERY, {"id": connector_id})
                logs_node = None if logs_err else logs_data.get("connector")
                if logs_node:
                    recent, _ = _tail_log_lines(
                        logs_node.get("manager_connector_logs"), _LOG_GET_TAIL
                    )
                    if recent:
                        lines.append(
                            f"  last {len(recent)} log line(s) (action='logs' for more):"
                        )
                        lines.extend(f"    {line}" for line in recent)
            return "\n".join(lines)

        if action == "logs":
            if not connector_id:
                return "connector_id is required for action 'logs'."
            wanted = _coerce_tail(tail)
            if wanted is None:
                return f"tail must be an integer between 1 and {_LOG_TAIL_MAX}."
            data, err = await _gql(_LOGS_QUERY, {"id": connector_id})
            if err:
                if _logs_field_missing(err):
                    return (
                        "This OpenCTI version does not expose managed connector "
                        "logs (Connector.manager_connector_logs): read them on "
                        "the connector page or upgrade OpenCTI."
                    )
                return err
            c = data.get("connector")
            if not c:
                return f"Connector {connector_id} not found."
            label = f"Connector '{c.get('name')}' (id: {c.get('id')})"
            if not c.get("is_managed"):
                return (
                    f"{label} is not a managed (XTM Composer) connector: OpenCTI "
                    "keeps no logs for it. Read them where the connector runs."
                )
            status = c.get("manager_current_status") or "unknown"
            requested = c.get("manager_requested_status")
            if requested and requested != status:
                status += f" (requested: {requested})"
            header = f"{label}, status: {status}"
            uptime = _format_uptime(c.get("manager_connector_uptime"))
            if uptime:
                header += f", uptime: {uptime}"
            picked, dropped = _tail_log_lines(c.get("manager_connector_logs"), wanted)
            if not picked:
                return f"{header}\nNo logs reported by the connector manager yet."
            lines = [f"{header}", f"Last {len(picked)} log line(s), oldest first:"]
            lines.extend(picked)
            if dropped:
                lines.append(
                    f"[{dropped} older line(s) left out to keep the answer under "
                    f"{_LOG_TOTAL_CAP} characters]"
                )
            return "\n".join(lines)

        if action == "deploy":
            if not name or len(name.strip()) < 2:
                return "name is required for action 'deploy' (at least 2 characters)."
            if not manager_contract_image:
                return (
                    "manager_contract_image is required for action 'deploy'. Get it "
                    "from query_opencti_catalog action='get' (the connector's "
                    "manager_contract_image)."
                )
            if not user_id:
                return (
                    "user_id is required for action 'deploy' — the OpenCTI user that "
                    "the connector ingests data as. Pass an existing user id, or set "
                    "automatic_user=true and pass a service-account name as user_id "
                    "to have OpenCTI create one on the fly."
                )
            # Validate an explicitly-provided confidence_level up front. The tool
            # executor forwards the LLM's raw JSON argument without coercion, so a
            # malformed (bool, non-numeric string) or out-of-range value would
            # otherwise be stringified and sent to OpenCTI — which parseInt()s it
            # — creating the service account with an unintended or effectively
            # absent confidence (the very risk this guards against). Fail fast
            # instead of forwarding garbage; the caller can omit it to inherit the
            # contract default.
            explicit_confidence: int | None = None
            if confidence_level is not None:
                explicit_confidence = _coerce_confidence_level(confidence_level)
                if explicit_confidence is None:
                    return (
                        "confidence_level must be an integer between 0 and 100. "
                        "Leave it unset to inherit the connector contract's "
                        "default (what OpenCTI's own deploy UI does)."
                    )
            # catalog_id is a REQUIRED top-level field of AddManagedConnectorInput
            # (typed ID!) — the connector row is stored against its source catalog.
            # Prefer the value the caller supplies (from query_opencti_catalog
            # action='get'), but fall back to resolving it from the catalog that
            # owns this manager_contract_image so a deploy still succeeds when the
            # caller forgets to pass it.
            #
            # We also load the catalog to recover the connector *contract* when we
            # need to default the auto-created service account's confidence level
            # (automatic_user with no explicit confidence_level) — OpenCTI's own
            # deploy UI defaults this to the contract's max_confidence_level, and
            # skipping it creates a service account with no confidence at all,
            # which can let the connector overwrite higher-confidence data.
            resolved_catalog_id = (catalog_id or "").strip()
            matched_contract: dict | None = None
            needs_contract = bool(automatic_user) and explicit_confidence is None
            if not resolved_catalog_id or needs_contract:
                catalogs, cat_err = await _load_catalogs()
                if cat_err:
                    return cat_err
                for cat in catalogs:
                    for c in cat["contracts"]:
                        if c.get("container_image") == manager_contract_image:
                            matched_contract = c
                            if not resolved_catalog_id:
                                resolved_catalog_id = cat["id"]
                            break
                    if matched_contract:
                        break
            if not resolved_catalog_id:
                return (
                    "catalog_id is required for action 'deploy' and could not be "
                    "resolved from manager_contract_image. Get it from "
                    "query_opencti_catalog action='get' (the connector's "
                    "catalog_id) and pass it as catalog_id."
                )
            contract_config = _contract_configuration(parsed_config)
            inp: dict = {
                "name": name.strip(),
                "catalog_id": resolved_catalog_id,
                "manager_contract_image": manager_contract_image,
                "manager_contract_configuration": contract_config,
                "user_id": user_id,
                "automatic_user": bool(automatic_user),
            }
            # Resolve the confidence assigned to the auto-created service-account
            # user. Honour an explicit value; otherwise (automatic_user) fall back
            # to the connector contract's default (max_confidence_level), matching
            # OpenCTI's own deploy UI. Leaving it unset makes OpenCTI create the
            # service account with NO confidence level, which can let the connector
            # overwrite higher-confidence platform data.
            effective_confidence = explicit_confidence
            from_contract = False
            if (
                bool(automatic_user)
                and effective_confidence is None
                and matched_contract is not None
            ):
                contract_default = _contract_confidence_default(matched_contract)
                if contract_default is not None:
                    effective_confidence = contract_default
                    from_contract = True
            # ``is not None`` (not a falsy check) so an explicit 0 is still sent.
            # OpenCTI's AddManagedConnectorInput types confidence_level as String
            # (the resolver parseInt()s it), so send a stringified value — a raw
            # int is rejected by GraphQL variable coercion for a String field.
            if effective_confidence is not None:
                inp["confidence_level"] = str(effective_confidence)
            query = (
                "mutation DeployConnector($input: AddManagedConnectorInput!) {"
                "  managedConnectorAdd(input: $input) {"
                "    id name manager_requested_status manager_contract_image } }"
            )
            data, err = await _gql(query, {"input": inp})
            if err:
                return err
            node = data.get("managedConnectorAdd") or {}
            confidence_note = ""
            if bool(automatic_user) and effective_confidence is not None:
                source = (
                    " (the connector contract default)" if from_contract else ""
                )
                confidence_note = (
                    " Its service-account user was created with confidence level "
                    f"{effective_confidence}{source}."
                )
            return (
                f"Deployed connector '{node.get('name')}' (id: {node.get('id')})."
                f"{confidence_note} It starts in 'stopped' state — call "
                "action='start' to enable it."
            )

        if action == "update_config":
            if not connector_id:
                return "connector_id is required for action 'update_config'."
            if not parsed_config:
                return "config (JSON of parameter->value pairs) is required for 'update_config'."
            if not name:
                return (
                    "name is required for action 'update_config' (the connector's "
                    "current name/title — get it from action='get')."
                )
            hidden = sorted(
                str(k)
                for k, v in parsed_config.items()
                if isinstance(v, str) and v.strip() in _HIDDEN_VALUE_PLACEHOLDERS
            )
            if hidden:
                return (
                    f"config carries the placeholder action='get' prints for a hidden "
                    f"value ({', '.join(hidden)}); nothing was written. Leave a secret "
                    "out to keep its stored value, or pass its new value."
                )
            # OpenCTI rebuilds the whole configuration on every edit, so the
            # stored one is read first: the merge re-sends every setting the
            # caller did not change, and connector_user_id (required by
            # EditManagedConnectorInput) defaults to the connector's own user.
            cur, cerr = await _gql(_CONNECTOR_CONFIG_QUERY, {"id": connector_id})
            if cerr and "manager_contract_definition" in cerr:
                cur, cerr = await _gql(_CONNECTOR_CONFIG_QUERY_NO_CONTRACT, {"id": connector_id})
            if cerr:
                return cerr
            current = cur.get("connector")
            if not current:
                return f"Connector {connector_id} not found; nothing was written."
            effective_user = user_id or current.get("connector_user_id")
            if not effective_user:
                return "user_id is required for action 'update_config' (the connector's data-creation user)."
            properties, required = _contract_schema(current.get("manager_contract_definition"))
            to_send, report = _merge_contract_configuration(
                current.get("manager_contract_configuration"),
                properties,
                parsed_config,
                replace=bool(replace),
                required=required,
            )
            inp = {
                "id": connector_id,
                "name": name.strip(),
                "title": name.strip(),
                "connector_user_id": effective_user,
                "manager_contract_configuration": [{"key": k, "value": v} for k, v in to_send.items()],
            }
            query = (
                "mutation EditConnector($input: EditManagedConnectorInput!) {"
                "  managedConnectorEdit(input: $input) { id name } }"
            )
            data, err = await _gql(query, {"input": inp})
            if err:
                return err
            node = data.get("managedConnectorEdit") or {}
            return _config_update_report(node, report, replace=bool(replace))

        if action in ("start", "stop"):
            if not connector_id:
                return f"connector_id is required for action '{action}'."
            status = "starting" if action == "start" else "stopping"
            query = (
                "mutation SetStatus($input: RequestConnectorStatusInput!) {"
                "  updateConnectorRequestedStatus(input: $input) {"
                "    id name manager_requested_status } }"
            )
            data, err = await _gql(
                query, {"input": {"id": connector_id, "status": status}}
            )
            if err:
                return err
            node = data.get("updateConnectorRequestedStatus") or {}
            verb = "starting" if action == "start" else "stopping"
            return f"Connector '{node.get('name', connector_id)}' is now {verb}."

        if action == "delete":
            if not connector_id:
                return "connector_id is required for action 'delete'."
            query = "mutation DeleteConnector($id: ID!) { deleteConnector(id: $id) }"
            _, err = await _gql(query, {"id": connector_id})
            if err:
                return err
            return f"Deleted connector {connector_id}."

        return (
            f"Unknown action '{action}'. Valid actions: list, get, logs, deploy, "
            "update_config, start, stop, delete."
        )

    executor.register("query_opencti_catalog", query_opencti_catalog)
    executor.register("manage_opencti_connector", manage_opencti_connector)

    return [
        ToolDefinition(
            name="query_opencti_catalog",
            description=(
                "Browse the OpenCTI Enterprise Edition connector catalog (the "
                "connectors that can be deployed via the Composer). action='list' "
                "lists catalogs; action='contracts' lists available connectors "
                "(optionally filtered by catalog_id or a search term); action='get' "
                "(with slug) shows one connector's configuration parameters — "
                "including which are required and which are secrets — so you know "
                "what to ask the user for before deploying. Returns the "
                "manager_contract_image needed by manage_opencti_connector deploy."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "contracts", "get"],
                        "default": "list",
                    },
                    "catalog_id": {
                        "type": "string",
                        "description": "Restrict 'contracts' to one catalog id.",
                    },
                    "slug": {
                        "type": "string",
                        "description": "Connector slug for action='get'.",
                    },
                    "search": {
                        "type": "string",
                        "description": "Filter connectors by title/slug/use-case for action='contracts'.",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="manage_opencti_connector",
            description=(
                "Deploy and manage OpenCTI managed (catalog/Composer) connectors. "
                "Actions: 'list' (deployed managed connectors), 'get' "
                "(connector_id), 'logs' (connector_id + tail: its recent "
                "Composer logs), 'deploy' (name + manager_contract_image + "
                "catalog_id from the catalog + config JSON of the connector's "
                "parameters + user_id, or automatic_user=true with a service-account "
                "name as user_id), "
                "'update_config' (connector_id + name + config: merged with the "
                "stored configuration, omitted settings and secrets kept, an empty "
                "value resets a setting to its default; replace=true sends config "
                "alone), 'start'/'stop' "
                "(connector_id), 'delete' (connector_id). Requires Enterprise "
                "Edition with a running connector manager (XTM Composer); secret "
                "values are encrypted server-side. Always confirm required "
                "parameters with query_opencti_catalog get before deploying, and "
                "ask the user for any secret values (e.g. API keys). When deploying "
                "with automatic_user, the new service-account user is given the "
                "connector contract's default confidence level automatically unless "
                "you pass confidence_level."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "list",
                            "get",
                            "logs",
                            "deploy",
                            "update_config",
                            "start",
                            "stop",
                            "delete",
                        ],
                    },
                    "connector_id": {"type": "string"},
                    "name": {
                        "type": "string",
                        "description": "Connector display name (deploy/update_config).",
                    },
                    "manager_contract_image": {
                        "type": "string",
                        "description": "Container image from the catalog (deploy).",
                    },
                    "catalog_id": {
                        "type": "string",
                        "description": (
                            "Catalog id that owns the connector (deploy, REQUIRED "
                            "server-side). Get it from query_opencti_catalog "
                            "action='get'. If omitted it is resolved from the "
                            "manager_contract_image."
                        ),
                    },
                    "config": {
                        "type": "string",
                        "description": (
                            "JSON object of the connector's configuration "
                            "parameters, e.g. {\"ALIENVAULT_API_KEY\": \"...\"}. "
                            "Keys come from query_opencti_catalog get."
                        ),
                    },
                    "user_id": {
                        "type": "string",
                        "description": (
                            "OpenCTI user id the connector ingests as. With "
                            "automatic_user=true, pass a service-account name "
                            "instead and OpenCTI creates the user."
                        ),
                    },
                    "automatic_user": {
                        "type": "boolean",
                        "description": "Create an on-the-fly service-account user from user_id (deploy).",
                        "default": False,
                    },
                    "confidence_level": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 100,
                        "description": (
                            "Confidence level (0-100) for the auto-created "
                            "service-account user (automatic_user=true). Leave it "
                            "unset to use the connector contract's default "
                            "(max_confidence_level), which is what OpenCTI does — "
                            "do NOT invent a value. Only set this to deliberately "
                            "override the contract default."
                        ),
                    },
                    "tail": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": _LOG_TAIL_MAX,
                        "default": _LOG_TAIL_DEFAULT,
                        "description": "Log lines to return, newest kept (logs).",
                    },
                    "replace": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "update_config: send config alone; every omitted "
                            "setting goes back to its default (secrets kept)."
                        ),
                    },
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
    ]
