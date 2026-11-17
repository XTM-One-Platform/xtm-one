# SPDX-License-Identifier: Apache-2.0

"""Security schemes → the authentication options an OpenAPI integration offers.

A spec declares how its API authenticates (Swagger 2.0
``securityDefinitions``, OpenAPI 3.x ``components.securitySchemes``); an
integration holds ONE credential. This module turns every declared scheme
into an :class:`AuthOption` — supported or not, with the reason — so the
import form can offer the choice and the runtime can place the credential:

======================================  ======================  ==========================
Scheme                                  Integration auth        Credential sent
======================================  ======================  ==========================
``apiKey`` in header / query / cookie   ``api_key``             as declared
``http`` ``bearer``                     ``api_key``             ``Authorization: Bearer``
``http`` ``basic`` / 2.0 ``basic``      ``api_key``             ``Authorization: Basic``
``oauth2`` authorization code           ``oauth``               bearer (redirect sign-in)
``oauth2`` implicit                     ``oauth``               bearer (redirect sign-in)
``oauth2`` device authorization (3.2)   ``oauth``               bearer (sign-in with a code)
``oauth2`` client credentials           ``client_credentials``  bearer (token per client)
``oauth2`` password                     ``oauth_password``      bearer (token per user)
``openIdConnect``                       as its discovered flows
``mutualTLS``                           ``none``                a client certificate
(none)                                  ``none``                nothing
======================================  ======================  ==========================

``openIdConnect`` schemes, and OAuth2 schemes with an ``oauth2MetadataUrl``
(3.2), are read from their discovery document (``discovery.py`` fetches it;
:func:`extract_auth_options` receives it): the endpoints it advertises and
the grants it supports become ordinary OAuth2 options. A client certificate
can also be added to any option (mutual TLS at the transport). HTTP schemes
other than basic and bearer are listed as unsupported. Two synthetic options
are always offered: no authentication, and a manually pasted bearer token —
the escape hatch for a spec that omits its security section, or whose only
scheme is unsupported.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import asdict, dataclass, field, replace
from typing import Any
from urllib.parse import urljoin, urlparse

from app.services.declarative_http_executor import credential_header_allowed
from app.services.openapi_import.schema import RefResolver, UnresolvedRefError

#: RFC 9110 token: what a cookie name may be.
_TOKEN_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")

KIND_NONE = "none"
KIND_API_KEY = "api_key"
KIND_BEARER = "bearer"
KIND_BASIC = "basic"
KIND_OAUTH2_CODE = "oauth2_authorization_code"
KIND_OAUTH2_IMPLICIT = "oauth2_implicit"
KIND_OAUTH2_DEVICE = "oauth2_device"
KIND_OAUTH2_CLIENT_CREDENTIALS = "oauth2_client_credentials"
KIND_OAUTH2_PASSWORD = "oauth2_password"  # noqa: S105 - option kind, not a secret
KIND_MUTUAL_TLS = "mutual_tls"

#: Integration ``auth_type`` for OAuth2 client credentials (machine-to-machine).
AUTH_TYPE_CLIENT_CREDENTIALS = "client_credentials"
#: Integration ``auth_type`` for the OAuth2 password grant (a user's own login).
AUTH_TYPE_OAUTH_PASSWORD = "oauth_password"  # noqa: S105 - auth type name, not a secret

#: Options whose token comes from a user signing in (the integration's
#: ``Authorize`` action), by the way they sign in.
SIGN_IN_KINDS = frozenset({KIND_OAUTH2_CODE, KIND_OAUTH2_IMPLICIT, KIND_OAUTH2_DEVICE})
DEVICE_CODE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

_INTEGRATION_AUTH_TYPES = {
    KIND_NONE: "none",
    KIND_API_KEY: "api_key",
    KIND_BEARER: "api_key",
    KIND_BASIC: "api_key",
    KIND_OAUTH2_CODE: "oauth",
    KIND_OAUTH2_IMPLICIT: "oauth",
    KIND_OAUTH2_DEVICE: "oauth",
    KIND_OAUTH2_CLIENT_CREDENTIALS: AUTH_TYPE_CLIENT_CREDENTIALS,
    KIND_OAUTH2_PASSWORD: AUTH_TYPE_OAUTH_PASSWORD,
    KIND_MUTUAL_TLS: "none",
}

OPTION_NONE = "none"
OPTION_MANUAL_BEARER = "manual_bearer"
_SYNTHETIC_IDS = frozenset({OPTION_NONE, OPTION_MANUAL_BEARER})
#: Flows the OAuth 2.0 security best practice retires: offered, but never
#: the default while another option is supported.
_LEGACY_KINDS = frozenset({KIND_OAUTH2_IMPLICIT, KIND_OAUTH2_PASSWORD})


@dataclass(frozen=True)
class AuthOption:
    """One way the integration can authenticate its calls."""

    id: str
    kind: str
    label: str
    scheme_name: str | None = None
    #: apiKey only: where the key goes, and under which name.
    location: str | None = None
    param_name: str | None = None
    #: OAuth2 only.
    authorize_url: str | None = None
    token_url: str | None = None
    device_authorization_url: str | None = None
    scopes: dict[str, str] = field(default_factory=dict)
    #: Scopes to request by default when not every declared one should be
    #: (OpenID Connect advertises many); ``None`` = all of ``scopes``.
    default_scopes: list[str] | None = None
    description: str | None = None
    supported: bool = True
    unsupported_reason: str | None = None
    #: The scheme is marked ``deprecated`` (OpenAPI 3.2).
    deprecated: bool = False

    @property
    def integration_auth_type(self) -> str:
        """The ``Integration.auth_type`` that stores this option's credential."""
        return _INTEGRATION_AUTH_TYPES.get(self.kind, "api_key")

    def runtime_auth(self) -> dict[str, Any]:
        """The execution-spec ``auth`` block that places the credential."""
        if self.kind in (KIND_NONE, KIND_MUTUAL_TLS):
            return {"type": "none"}
        if self.kind == KIND_API_KEY:
            if self.location == "query":
                return {"type": "query", "param": self.param_name}
            if self.location == "cookie":
                return {"type": "cookie", "cookie": self.param_name}
            return {"type": "header", "header": self.param_name, "prefix": ""}
        if self.kind == KIND_BASIC:
            return {"type": "basic"}
        return {"type": "bearer"}

    def oauth_settings(self) -> dict[str, str | None]:
        """The ``authorize_url``, ``token_url`` and ``scopes`` an integration keeps.

        A device-code option signs in at its device authorization endpoint,
        which the integration keeps as its ``authorize_url``.
        """
        scopes = self.default_scopes if self.default_scopes is not None else list(self.scopes)
        return {
            "authorize_url": (
                self.device_authorization_url if self.kind == KIND_OAUTH2_DEVICE else self.authorize_url
            ),
            "token_url": self.token_url,
            "scopes": " ".join(scopes) or None,
        }

    def credential_param(self) -> tuple[str, str] | None:
        """``(location, name)`` of an API-key parameter the credential fills.

        Such a parameter is removed from the tool arguments at runtime: the
        platform supplies it, and the model must never be asked for it.
        """
        if self.kind == KIND_API_KEY and self.location and self.param_name:
            return self.location, self.param_name
        return None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> AuthOption | None:
        if not isinstance(data, dict) or not isinstance(data.get("id"), str):
            return None
        known = set(cls.__dataclass_fields__)
        values = {k: v for k, v in data.items() if k in known}
        scopes = values.get("scopes")
        values["scopes"] = dict(scopes) if isinstance(scopes, dict) else {}
        defaults = values.get("default_scopes")
        values["default_scopes"] = [str(s) for s in defaults] if isinstance(defaults, list) else None
        try:
            return cls(**values)
        except TypeError:
            return None


def _with_id(option: AuthOption, new_id: str) -> AuthOption:
    return AuthOption(**{**option.to_dict(), "id": new_id})


def absolute_url(url: Any, base: str | None) -> str | None:
    """An absolute http(s) URL, resolving a relative one against *base*.

    A spec-supplied string that is no URL at all (an unclosed IPv6 bracket,
    a bad port) is None, never an exception.
    """
    if not isinstance(url, str) or not url.strip():
        return None
    url = url.strip()
    try:
        parsed = urlparse(url)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            _ = parsed.port
            return url
        if base:
            joined = urljoin(base, url)
            joined_parsed = urlparse(joined)
            if joined_parsed.scheme in ("http", "https") and joined_parsed.netloc:
                _ = joined_parsed.port
                return joined
    except ValueError:
        return None
    return None


def is_secure_oauth_url(url: str) -> bool:
    """Whether an OAuth endpoint a spec names may carry codes, tokens and secrets.

    HTTPS only: a client secret, a password, a device code or a token sent in
    clear text is readable on the path. Plain HTTP only reaches a loopback
    literal, which the outbound policy refuses anyway (the MCP OAuth discovery
    rule, ``mcp_oauth_discovery.is_safe_endpoint``).
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme == "https":
        return bool(parsed.netloc)
    if parsed.scheme != "http":
        return False
    host = parsed.hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _require_secure_endpoints(option: AuthOption) -> AuthOption:
    """*option*, or an unsupported one when an OAuth endpoint it uses is not HTTPS."""
    if not option.supported:
        return option
    insecure = [
        url
        for url in (option.authorize_url, option.token_url, option.device_authorization_url)
        if url and not is_secure_oauth_url(url)
    ]
    if not insecure:
        return option
    return AuthOption(
        id=option.id,
        kind="unsupported",
        label=option.label,
        scheme_name=option.scheme_name,
        supported=False,
        unsupported_reason=(
            f"its OAuth endpoint {insecure[0][:200]} does not use HTTPS, and a sign-in "
            "sends codes, tokens and secrets to it"
        ),
        deprecated=option.deprecated,
    )


def _scopes(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {str(k): (v if isinstance(v, str) else "") for k, v in raw.items()}


def _unsupported(scheme_name: str, label: str, reason: str) -> AuthOption:
    return AuthOption(
        id=f"{scheme_name}",
        kind="unsupported",
        label=label,
        scheme_name=scheme_name,
        supported=False,
        unsupported_reason=reason,
    )


def _options_for_scheme(
    name: str,
    scheme: dict[str, Any],
    *,
    family: str,
    url_base: str | None,
    discovery: dict[str, Any],
) -> list[AuthOption]:
    scheme_type = scheme.get("type")
    description = scheme.get("description") if isinstance(scheme.get("description"), str) else None

    if scheme_type == "apiKey":
        location = scheme.get("in")
        param = scheme.get("name")
        if location not in ("header", "query", "cookie") or not isinstance(param, str) or not param:
            return [_unsupported(name, f"{name} (API key)", "the API key location is not declared")]
        if location == "header" and not credential_header_allowed(param):
            return [
                _unsupported(
                    name,
                    f"{name} (API key)",
                    f"the API key cannot be sent in the '{param[:80]}' header",
                )
            ]
        if location == "cookie" and not _TOKEN_RE.match(param):
            return [_unsupported(name, f"{name} (API key)", "the API key cookie has an invalid name")]
        return [
            AuthOption(
                id=name,
                kind=KIND_API_KEY,
                label=f"API key in {location} '{param}'",
                scheme_name=name,
                location=location,
                param_name=param,
                description=description,
            )
        ]

    if scheme_type == "basic":  # Swagger 2.0
        return [AuthOption(id=name, kind=KIND_BASIC, label="HTTP Basic", scheme_name=name, description=description)]

    if scheme_type == "http":
        http_scheme = str(scheme.get("scheme") or "").lower()
        if http_scheme == "bearer":
            bearer_format = scheme.get("bearerFormat")
            label = "Bearer token" + (f" ({bearer_format})" if isinstance(bearer_format, str) and bearer_format else "")
            return [AuthOption(id=name, kind=KIND_BEARER, label=label, scheme_name=name, description=description)]
        if http_scheme == "basic":
            return [AuthOption(id=name, kind=KIND_BASIC, label="HTTP Basic", scheme_name=name, description=description)]
        return [_unsupported(name, f"{name} (HTTP {http_scheme or '?'})", f"HTTP '{http_scheme}' authentication is not supported")]

    if scheme_type == "oauth2":
        metadata = None
        metadata_url = absolute_url(scheme.get("oauth2MetadataUrl"), url_base)
        if metadata_url:
            metadata = _metadata(discovery.get(metadata_url))
        return _oauth2_options(
            name, scheme, family=family, url_base=url_base, description=description, metadata=metadata
        )

    if scheme_type == "openIdConnect":
        url = absolute_url(scheme.get("openIdConnectUrl"), url_base)
        if not url:
            return [_unsupported(name, f"{name} (OpenID Connect)", "the OpenID Connect discovery URL is missing")]
        found = discovery.get(url)
        metadata = _metadata(found)
        if metadata is None:
            reason = found.get("error") if isinstance(found, dict) and found.get("error") else "it was not fetched"
            return [
                _unsupported(
                    name,
                    f"{name} (OpenID Connect)",
                    f"the OpenID Connect discovery document could not be read: {reason}",
                )
            ]
        options = _discovered_options(name, metadata, description=description, openid=True)
        return options or [
            _unsupported(name, f"{name} (OpenID Connect)", "the discovery document advertises no supported grant")
        ]
    if scheme_type == "mutualTLS":
        return [
            AuthOption(
                id=name,
                kind=KIND_MUTUAL_TLS,
                label="Client certificate (mutual TLS)",
                scheme_name=name,
                description=description,
            )
        ]
    return [_unsupported(name, name, f"security scheme type '{scheme_type}' is not supported")]


def _metadata(found: Any) -> dict[str, Any] | None:
    """A usable discovery document, or None (not fetched, or an error)."""
    if isinstance(found, dict) and not found.get("error") and (
        found.get("authorization_endpoint") or found.get("token_endpoint")
    ):
        return found
    return None


def _endpoint(metadata: dict[str, Any], key: str) -> str | None:
    return absolute_url(metadata.get(key), None)


def _discovered_options(
    name: str,
    metadata: dict[str, Any],
    *,
    description: str | None,
    openid: bool,
) -> list[AuthOption]:
    """OAuth2 options from a discovery document (OpenID Connect or RFC 8414)."""
    authorize_url = _endpoint(metadata, "authorization_endpoint")
    token_url = _endpoint(metadata, "token_endpoint")
    device_url = _endpoint(metadata, "device_authorization_endpoint")
    raw_grants = metadata.get("grant_types_supported")
    # RFC 8414: an omitted list means authorization code and implicit.
    grants = (
        {str(g) for g in raw_grants}
        if isinstance(raw_grants, list)
        else {"authorization_code", "implicit"}
    )
    supported = metadata.get("scopes_supported")
    scopes = {str(s): "" for s in supported if isinstance(s, str)} if isinstance(supported, list) else {}
    default_scopes = None
    if openid:
        default_scopes = ["openid"] + (["offline_access"] if "offline_access" in scopes else [])
        scopes = {"openid": "", **scopes}
    prefix = "OpenID Connect" if openid else "OAuth2"
    common = {"scheme_name": name, "scopes": scopes, "default_scopes": default_scopes, "description": description}
    options: list[AuthOption] = []
    if authorize_url and token_url and "authorization_code" in grants:
        options.append(
            AuthOption(
                id=f"{name}:authorization_code",
                kind=KIND_OAUTH2_CODE,
                label=f"{prefix} sign-in (authorization code)",
                authorize_url=authorize_url,
                token_url=token_url,
                **common,
            )
        )
    if device_url and token_url and DEVICE_CODE_GRANT in grants:
        options.append(
            AuthOption(
                id=f"{name}:device",
                kind=KIND_OAUTH2_DEVICE,
                label=f"{prefix} sign-in with a code (device authorization)",
                token_url=token_url,
                device_authorization_url=device_url,
                **common,
            )
        )
    if token_url and "client_credentials" in grants:
        options.append(
            AuthOption(
                id=f"{name}:client_credentials",
                kind=KIND_OAUTH2_CLIENT_CREDENTIALS,
                label=f"{prefix} client credentials (machine to machine)",
                token_url=token_url,
                **common,
            )
        )
    if token_url and "password" in grants:
        options.append(
            AuthOption(
                id=f"{name}:password",
                kind=KIND_OAUTH2_PASSWORD,
                label=f"{prefix} username and password",
                token_url=token_url,
                **common,
            )
        )
    return options


def _oauth2_options(
    name: str,
    scheme: dict[str, Any],
    *,
    family: str,
    url_base: str | None,
    description: str | None,
    metadata: dict[str, Any] | None = None,
) -> list[AuthOption]:
    # Normalise Swagger 2.0's single "flow" onto 3.x's "flows" map.
    if family == "2.0":
        flow_name = {
            "accessCode": "authorizationCode",
            "application": "clientCredentials",
            "implicit": "implicit",
            "password": "password",
        }.get(str(scheme.get("flow")), str(scheme.get("flow")))
        flows = {
            flow_name: {
                "authorizationUrl": scheme.get("authorizationUrl"),
                "tokenUrl": scheme.get("tokenUrl"),
                "scopes": scheme.get("scopes"),
            }
        }
    else:
        flows = scheme.get("flows") if isinstance(scheme.get("flows"), dict) else {}

    known = metadata or {}
    options: list[AuthOption] = []
    for flow_name, flow in flows.items():
        if not isinstance(flow, dict):
            continue
        scopes = _scopes(flow.get("scopes"))
        # A flow may leave its endpoints to the scheme's metadata document (3.2).
        token_url = absolute_url(flow.get("tokenUrl"), url_base) or _endpoint(known, "token_endpoint")
        if flow_name == "authorizationCode":
            authorize_url = absolute_url(flow.get("authorizationUrl"), url_base) or _endpoint(
                known, "authorization_endpoint"
            )
            if not authorize_url or not token_url:
                options.append(_unsupported(f"{name}:authorization_code", f"{name} (OAuth2 authorization code)", "the flow's authorization or token URL is missing"))
                continue
            options.append(
                AuthOption(
                    id=f"{name}:authorization_code",
                    kind=KIND_OAUTH2_CODE,
                    label="OAuth2 authorization code (users sign in)",
                    scheme_name=name,
                    authorize_url=authorize_url,
                    token_url=token_url,
                    scopes=scopes,
                    description=description,
                )
            )
        elif flow_name == "clientCredentials":
            if not token_url:
                options.append(_unsupported(f"{name}:client_credentials", f"{name} (OAuth2 client credentials)", "the flow's token URL is missing"))
                continue
            options.append(
                AuthOption(
                    id=f"{name}:client_credentials",
                    kind=KIND_OAUTH2_CLIENT_CREDENTIALS,
                    label="OAuth2 client credentials (machine to machine)",
                    scheme_name=name,
                    token_url=token_url,
                    scopes=scopes,
                    description=description,
                )
            )
        elif flow_name == "implicit":
            authorize_url = absolute_url(flow.get("authorizationUrl"), url_base) or _endpoint(
                known, "authorization_endpoint"
            )
            if not authorize_url:
                options.append(_unsupported(f"{name}:implicit", f"{name} (OAuth2 implicit)", "the flow's authorization URL is missing"))
                continue
            options.append(
                AuthOption(
                    id=f"{name}:implicit",
                    kind=KIND_OAUTH2_IMPLICIT,
                    label="OAuth2 implicit (users sign in, no refresh)",
                    scheme_name=name,
                    authorize_url=authorize_url,
                    scopes=scopes,
                    description=description,
                )
            )
        elif flow_name == "password":
            if not token_url:
                options.append(_unsupported(f"{name}:password", f"{name} (OAuth2 password)", "the flow's token URL is missing"))
                continue
            options.append(
                AuthOption(
                    id=f"{name}:password",
                    kind=KIND_OAUTH2_PASSWORD,
                    label="OAuth2 username and password",
                    scheme_name=name,
                    token_url=token_url,
                    scopes=scopes,
                    description=description,
                )
            )
        elif flow_name == "deviceAuthorization":
            device_url = absolute_url(flow.get("deviceAuthorizationUrl"), url_base) or _endpoint(
                known, "device_authorization_endpoint"
            )
            if not device_url or not token_url:
                options.append(
                    _unsupported(
                        f"{name}:device",
                        f"{name} (OAuth2 device authorization)",
                        "the flow's device authorization or token URL is missing",
                    )
                )
                continue
            options.append(
                AuthOption(
                    id=f"{name}:device",
                    kind=KIND_OAUTH2_DEVICE,
                    label="OAuth2 sign-in with a code (device authorization)",
                    scheme_name=name,
                    token_url=token_url,
                    device_authorization_url=device_url,
                    scopes=scopes,
                    description=description,
                )
            )
        else:
            options.append(
                _unsupported(
                    f"{name}:{flow_name}",
                    f"{name} (OAuth2 {flow_name})",
                    f"the OAuth2 '{flow_name}' flow is not a known flow",
                )
            )
    if not flows and metadata:
        options.extend(_discovered_options(name, metadata, description=description, openid=False))
    if not options:
        options.append(_unsupported(name, f"{name} (OAuth2)", "no OAuth2 flow is declared"))
    return options


def security_scheme_definitions(document: dict[str, Any], family: str) -> dict[str, Any]:
    """The raw ``name → scheme`` map of a document (2.0 or 3.x location)."""
    if family == "2.0":
        raw = document.get("securityDefinitions")
    else:
        components = document.get("components")
        raw = components.get("securitySchemes") if isinstance(components, dict) else None
    return raw if isinstance(raw, dict) else {}


def extract_auth_options(
    document: dict[str, Any],
    *,
    family: str,
    resolver: RefResolver,
    url_base: str | None,
    warnings: list[str],
    discovery: dict[str, Any] | None = None,
) -> tuple[list[AuthOption], str]:
    """Every authentication option plus the default one's id.

    *discovery* maps the discovery URLs of ``openIdConnect`` schemes (and of
    OAuth2 ``oauth2MetadataUrl`` values) to their fetched documents, or to
    ``{"error": ...}``.

    The default is the first supported option of the first alternative in
    the document's top-level ``security`` requirement; a requirement list
    whose first alternative is empty (``- {}``) means auth is optional, so
    the default is no authentication. Without a top-level requirement, the
    first supported declared scheme wins.
    """
    options: list[AuthOption] = [
        AuthOption(id=OPTION_NONE, kind=KIND_NONE, label="No authentication"),
    ]
    by_scheme: dict[str, list[AuthOption]] = {}
    for name, raw_scheme in security_scheme_definitions(document, family).items():
        try:
            scheme = resolver.deref(raw_scheme)
        except UnresolvedRefError as exc:
            resolver.record(exc)
            warnings.append(f"Security scheme '{name}' could not be resolved and is ignored.")
            continue
        if not isinstance(scheme, dict):
            continue
        scheme_options = [
            # A scheme may be NAMED like a synthetic option ("none"); its id
            # must still point at it, never at the synthetic one.
            _with_id(o, f"scheme:{o.id}") if o.id in _SYNTHETIC_IDS else o
            for o in map(
                _require_secure_endpoints,
                _options_for_scheme(
                    str(name), scheme, family=family, url_base=url_base, discovery=discovery or {}
                ),
            )
        ]
        if scheme.get("deprecated") is True:
            scheme_options = [
                replace(o, deprecated=True, label=f"{o.label} (deprecated)") for o in scheme_options
            ]
        by_scheme[str(name)] = scheme_options
        options.extend(scheme_options)

    if not any(o.kind == KIND_BEARER for o in options if o.supported):
        options.append(
            AuthOption(
                id=OPTION_MANUAL_BEARER,
                kind=KIND_BEARER,
                label="Bearer token (entered manually)",
            )
        )

    default = _default_option(document.get("security"), by_scheme)
    if default is None:
        declared = sorted((o for o in options if o.supported and o.scheme_name), key=_default_rank)
        default = declared[0].id if declared else OPTION_NONE
    return options, default


def _default_rank(option: AuthOption) -> int:
    """0 for a current option, 1 for a legacy flow, 2 for a deprecated scheme."""
    if option.deprecated:
        return 2
    return 1 if option.kind in _LEGACY_KINDS else 0


def _default_option(requirements: Any, by_scheme: dict[str, list[AuthOption]]) -> str | None:
    """The first option the requirements name, legacy and deprecated ones last."""
    if not isinstance(requirements, list) or not requirements:
        return None
    first = requirements[0]
    if isinstance(first, dict) and not first:
        return OPTION_NONE
    for worst in (0, 1, 2):
        for requirement in requirements:
            if not isinstance(requirement, dict):
                continue
            for scheme_name in requirement:
                for option in by_scheme.get(str(scheme_name), []):
                    if option.supported and _default_rank(option) <= worst:
                        return option.id
    return None


def requirement_schemes(requirements: Any) -> tuple[bool, list[str]]:
    """``(sends_credential, scheme names)`` for an effective security requirement.

    Only an explicit empty requirement (``security: []``) marks an operation
    public — the spec saying "this endpoint takes no credential" (a login or
    health route). An UNDECLARED requirement still sends the configured
    credential: many specs declare their schemes and forget the top-level
    ``security`` list, and an integration whose user entered an API key
    expects it to be used. Optional auth (``- {}`` beside a scheme) sends it
    too — with a credential at hand, the authenticated view is the useful one.
    """
    if not isinstance(requirements, list):
        return True, []
    if not requirements:
        return False, []
    names: list[str] = []
    for requirement in requirements:
        if isinstance(requirement, dict):
            names.extend(str(n) for n in requirement if str(n) not in names)
    return True, names
