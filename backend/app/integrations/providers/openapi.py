# SPDX-License-Identifier: Apache-2.0

"""OpenAPI / Swagger integration — any REST API described by a spec, as tools.

The integration's API is not known in advance: each ``openapi`` integration
carries its own converted document (``IntegrationOpenAPISpec``, written by
``app.services.openapi_import``). :func:`build_tools` turns its selected
operations into tools, each served by the shared declarative HTTP executor —
no code is generated or run per API.

Credentials (always from the integration, never from the spec):

* ``api_key`` — an API key or bearer token, or ``username:password`` for HTTP
  Basic; placed per the chosen auth option (header / query / cookie / bearer
  / basic, see ``openapi_import.security``);
* ``oauth`` — a user signs in (authorization code, implicit, or device
  authorization with a code); the access token is refreshed like every OAuth
  integration's when a refresh token came with it, and a 401 re-mints it once;
* ``client_credentials`` — OAuth2 machine-to-machine: client id + secret +
  token URL on the integration;
* ``oauth_password`` — the OAuth2 password grant: ``username:password`` in the
  API key, an optional client id / secret, the token URL. Both grants fetch
  and cache the access token per configuration through
  ``app.integrations.oauth_token_grants``;
* ``none`` — nothing is sent (the ``mutualTLS`` option: the certificate is
  the credential).

A client certificate (``extra_config``, ``app.integrations.client_certificate``)
is presented on every call and every token request, whatever the option.

The base URL is the integration's ``extra_config.base_url`` (pre-filled from
the spec's servers, editable). Because every URL here is user-supplied, each
request runs behind the destination guard: private networks are reachable,
loopback, link-local and cloud-metadata endpoints are not.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import ssl
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.core.parse_offload import run_parse
from app.core.security import decrypt_json, decrypt_value
from app.integrations.client_certificate import (
    CERTIFICATE_KEYS,
    ClientCertificateError,
    certificate_id,
    certificate_info,
    client_ssl_context,
)
from app.integrations.connectivity import (
    ProbeContext,
    ProbeResult,
    register_connectivity,
)
from app.integrations.oauth_token_grants import (
    TokenGrantConfig,
    TokenGrantError,
    get_grant_token,
)
from app.services.declarative_http_executor import (
    make_declarative_handler,
    make_invalid_spec_handler,
    spec_is_read,
    validate_execution_spec,
)
from app.services.openapi_import.schema import clean_text
from app.services.openapi_import.security import (
    AUTH_TYPE_CLIENT_CREDENTIALS,
    AUTH_TYPE_OAUTH_PASSWORD,
)
from app.services.openapi_import.service import (
    OPENAPI_PROVIDER,
    effective_tools,
    get_spec,
    pick_probe_tool,
)
from app.services.vault import SecretUnavailableError, reveal_field
from app.services.web_fetch import guarded_client_options
from app.tools.base import CallableToolExecutor, ToolCall, ToolDefinition

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.integration import Integration

logger = logging.getLogger(__name__)

#: Non-secret token of an integration whose access token is fetched per call
#: (client credentials, password grant): ``_get_token`` only says "present".
GRANT_TOKEN = "__token_grant__"  # noqa: S105 - marker, not a secret
#: The auth types whose access token comes from a token grant.
TOKEN_GRANT_AUTH_TYPES = frozenset({AUTH_TYPE_CLIENT_CREDENTIALS, AUTH_TYPE_OAUTH_PASSWORD})

_SOURCE = f"integration:{OPENAPI_PROVIDER}"
#: The probe's own ceiling, inside the dispatcher's (15 s): a token fetch plus
#: one call must finish, or the verdict must say which one hung.
_PROBE_CALL_TIMEOUT = 12
_PROBE_MESSAGE_CHARS = 300
#: An expiring client certificate is logged at most this often per integration.
_EXPIRY_LOG_INTERVAL_SECONDS = 24 * 3600
_expiry_logged: dict[str, float] = {}


@dataclass
class OpenAPIRuntime:
    """What :func:`build_tools` needs about one integration, pre-loaded."""

    tools: list[dict[str, Any]]
    api_title: str
    extra_config: dict[str, Any]
    auth_type: str
    token_grant: TokenGrantConfig | None = None
    #: Why the token grant is unusable (shown on every call).
    credentials_error: str | None = None
    #: The client certificate presented on every connection (mutual TLS).
    ssl_context: ssl.SSLContext | None = None
    #: Why the stored client certificate is unusable: every call refuses.
    certificate_error: str | None = None


async def token_grant_for(
    integration: Integration,
    *,
    ssl_context: ssl.SSLContext | None = None,
    certificate: str = "",
) -> TokenGrantConfig:
    """The token grant of a client-credentials or password-grant integration.

    The client secret and the password grant's login may be vault references:
    they are read through the vault. Raises :class:`TokenGrantError` when a
    part is missing, ``SecretUnavailableError`` when the vault cannot be read.
    """
    client_id = decrypt_value(integration.client_id_enc) if integration.client_id_enc else None
    client_secret = await reveal_field(integration, "client_secret_enc")
    if integration.auth_type == AUTH_TYPE_OAUTH_PASSWORD:
        return TokenGrantConfig.password_grant(
            integration.token_url,
            await reveal_field(integration, "api_key_enc"),
            client_id,
            client_secret,
            integration.scopes,
            ssl_context=ssl_context,
            certificate_id=certificate,
        )
    return TokenGrantConfig.client_credentials(
        integration.token_url,
        client_id,
        client_secret,
        integration.scopes,
        ssl_context=ssl_context,
        certificate_id=certificate,
    )


def decrypt_extra(integration: Integration) -> dict[str, Any]:
    """An integration's ``extra_config`` (empty when absent or unreadable)."""
    if not integration.extra_config_enc:
        return {}
    try:
        decoded = decrypt_json(integration.extra_config_enc)
    except Exception:
        logger.warning("Failed to decrypt extra_config for OpenAPI integration %s", integration.id)
        return {}
    return decoded if isinstance(decoded, dict) else {}


def integration_ssl_context(integration: Integration) -> ssl.SSLContext | None:
    """The client certificate context of *integration*, or None.

    For the token requests made outside the tools (code exchange, refresh,
    device authorization). Raises :class:`ClientCertificateError`.
    """
    return client_ssl_context(decrypt_extra(integration))


def token_client_options(integration: Integration) -> dict[str, object]:
    """``httpx.AsyncClient`` options for an OpenAPI integration's OAuth requests.

    Its token URL is user-supplied: the connection is pinned to an address
    the destination guard admits (behind a proxy, each request's URL is
    checked before it is sent), and the client certificate is presented.
    An unusable certificate is left out (the server then says what it needs).
    """
    try:
        context = integration_ssl_context(integration)
    except ClientCertificateError:
        context = None
    return guarded_client_options(allow_private=True, verify=context or True)


async def load_openapi_runtime(
    db: AsyncSession, integration: Integration
) -> OpenAPIRuntime | None:
    """Load an integration's runtime view; ``None`` when no spec is attached."""
    spec = await get_spec(db, integration.id, runtime_only=True)
    if spec is None:
        return None
    extra = decrypt_extra(integration)
    ssl_context: ssl.SSLContext | None = None
    certificate_error: str | None = None
    try:
        # X.509 parsing, a key's passphrase KDF, temporary files: off the loop.
        ssl_context = await asyncio.to_thread(client_ssl_context, extra)
    except ClientCertificateError as exc:
        certificate_error = str(exc)
    else:
        if ssl_context is not None:
            _log_expiring_certificate(integration, extra)
    token_grant: TokenGrantConfig | None = None
    credentials_error: str | None = None
    if integration.auth_type in TOKEN_GRANT_AUTH_TYPES:
        try:
            token_grant = await token_grant_for(
                integration, ssl_context=ssl_context, certificate=certificate_id(extra)
            )
        except (TokenGrantError, SecretUnavailableError) as exc:
            credentials_error = str(exc)
    # Deep copies of every selected tool (thousands for a large API) on each
    # agent turn: on the parse executor, not the event loop.
    tools = await run_parse(functools.partial(effective_tools, spec.active_tools, spec.auth_option))
    return OpenAPIRuntime(
        tools=tools,
        api_title=spec.api_title or "OpenAPI",
        # The certificate reaches the transport only, never a request.
        extra_config={k: v for k, v in extra.items() if k not in CERTIFICATE_KEYS},
        auth_type=integration.auth_type,
        token_grant=token_grant,
        credentials_error=credentials_error,
        ssl_context=ssl_context,
        certificate_error=certificate_error,
    )


def _log_expiring_certificate(integration: Integration, extra: dict[str, Any]) -> None:
    """Warn, once a day per integration, that its client certificate nears its end."""
    info = certificate_info(extra)
    if info is None or not info.expires_soon:
        return
    key = str(integration.id)
    now = time.monotonic()
    last = _expiry_logged.get(key)
    if last is not None and now - last < _EXPIRY_LOG_INTERVAL_SECONDS:
        return
    _expiry_logged[key] = now
    logger.warning(
        "The client certificate of OpenAPI integration %s (%s) expires on %s: "
        "replace it in the integration's API Spec tab before then.",
        integration.id,
        info.subject,
        info.not_after[:10],
    )


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    *,
    openapi_runtime: OpenAPIRuntime | None = None,
    refresh_fn: Any = None,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Register the integration's selected operations as tools.

    Without ``openapi_runtime`` (catalog introspection with a dummy token, or
    an integration whose spec is missing) there is nothing to build.
    """
    runtime = openapi_runtime
    if runtime is None:
        return []

    token_provider = None
    tool_refresh = None
    if runtime.auth_type in TOKEN_GRANT_AUTH_TYPES:
        config = runtime.token_grant
        error = runtime.credentials_error or "The OAuth token request is not configured."

        async def _grant_token() -> str:
            if config is None:
                raise TokenGrantError(error)
            return await get_grant_token(config)

        async def _grant_token_refresh() -> str | None:
            if config is None:
                return None
            return await get_grant_token(config, force_refresh=True)

        token_provider = _grant_token
        tool_refresh = _grant_token_refresh
    elif runtime.auth_type == "oauth":
        tool_refresh = refresh_fn

    definitions: list[ToolDefinition] = []
    for tool in runtime.tools:
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            continue
        spec = tool["execution"]
        errors = validate_execution_spec(spec, tool_index=0)
        if runtime.certificate_error:
            executor.register(name, _refusing_handler(runtime.api_title, runtime.certificate_error))
        elif errors:
            logger.warning(
                "OpenAPI tool '%s' has an invalid execution spec: %s", name, "; ".join(errors)
            )
            executor.register(
                name,
                make_invalid_spec_handler(
                    name,
                    errors,
                    source="imported API spec",
                    remedy="Re-import the spec from the integration's API spec tab.",
                ),
            )
        else:
            executor.register(
                name,
                make_declarative_handler(
                    spec=spec,
                    tool_name=name,
                    token=token,
                    extra_config=runtime.extra_config,
                    provider_label=runtime.api_title,
                    token_provider=token_provider,
                    refresh_fn=tool_refresh,
                    enforce_destination_guard=True,
                    ssl_context=runtime.ssl_context,
                ),
            )
        definitions.append(
            ToolDefinition(
                name=name,
                description=str(tool.get("description") or name),
                input_schema=tool.get("input_schema") or {"type": "object", "properties": {}},
                source=_SOURCE,
                # A read operation offers to_workspace_path: a large answer
                # (a task log, an export) lands whole in the sandbox workspace.
                workspace_ingest=not errors and not runtime.certificate_error and spec_is_read(spec),
            )
        )
    return definitions


def _refusing_handler(api_title: str, reason: str) -> Any:
    """A tool that refuses to call: its client certificate cannot be used."""
    from app.tools.base import ToolResult

    async def _refuse(**_kwargs: Any) -> ToolResult:
        return ToolResult(
            tool_call_id="",
            content=(
                f"Error: {api_title} is not called because its client certificate cannot "
                f"be used: {reason} Replace the certificate in the integration's API Spec tab."
            ),
            is_error=True,
        )

    return _refuse


# ---------------------------------------------------------------------------
# Connectivity probe
# ---------------------------------------------------------------------------


async def check_connectivity(ctx: ProbeContext) -> ProbeResult:
    """Call one argument-less GET operation through the real tool path.

    The whole runtime is exercised — credential resolution, OAuth or
    client-credentials token, auth placement, destination guard — so a green
    result means an agent's call would work. Only reads are ever sent.
    """
    integration = ctx.integration
    if ctx.db is None or integration is None:
        return ProbeResult.failed(f"{ctx.name} can only be tested from its integration page.")
    spec = await get_spec(ctx.db, integration.id, runtime_only=True)
    if spec is None:
        return ProbeResult.failed(
            "No API specification is attached to this integration. Import one from its API Spec tab."
        )
    tools = effective_tools(spec.active_tools, spec.auth_option)
    if not tools:
        return ProbeResult.degraded(
            "No operation is enabled, so agents get no tool from this integration. "
            "Select operations in the API Spec tab."
        )
    probe = pick_probe_tool(tools)
    if probe is None:
        return ProbeResult.degraded(
            f"{len(tools)} operation(s) enabled, but none can be called without "
            "arguments, so no request was sent. Use Test on an operation in the tools list."
        )

    from app.services.integration_tools import BindingBasis, ToolBindings, build_integration_tools

    label = f"{probe['execution'].get('method')} {probe['execution'].get('path')}"
    try:
        _defs, executor = await build_integration_tools(
            ctx.db,
            ToolBindings(
                {OPENAPI_PROVIDER: str(integration.id)},
                basis=BindingBasis.CALLER_CHECKED,
                reason="the connection probe of the integration being tested",
            ),
            context={
                "source": "connectivity_test",
                "integration": integration.name,
                # The tester's account: their own on an integration shared
                # without its connected account.
                "credential_user_id": ctx.acting_user_id,
            },
        )
    except Exception:
        logger.warning(
            "Building tools for the connectivity test of %s failed", integration.id, exc_info=True
        )
        return ProbeResult.failed("The integration's tools could not be built. Check its configuration.")
    if probe["name"] not in executor.tool_names:
        return ProbeResult.failed(
            f"The credentials are missing or unusable, so {label} was not sent. "
            "Check the integration's configuration."
        )
    try:
        result = await asyncio.wait_for(
            executor.execute_tool(ToolCall(id="connectivity", name=probe["name"], arguments={})),
            timeout=_PROBE_CALL_TIMEOUT,
        )
    except TimeoutError:
        return ProbeResult.failed(f"{label} did not answer within {_PROBE_CALL_TIMEOUT} seconds.")
    if result.is_error:
        detail = clean_text(str(result.content).removeprefix("Error:"), _PROBE_MESSAGE_CHARS)
        return ProbeResult.failed(f"{label} failed: {detail}")
    return ProbeResult.ok(f"{ctx.name} answered {label}.")


register_connectivity(OPENAPI_PROVIDER, check_connectivity)
