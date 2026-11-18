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

"""Minting the bearer this platform presents to a partner, per target.

Three targets, three credential flows, one behaviour once a token exists — so
this module is the only place they differ, and everything downstream sees a
header dict (FR-045).

**Why one file rather than one flow per provider module.** The property that
matters must be checkable in a single read: all three mint a SHORT-LIVED
bearer from a stored non-interactive credential, and none of them stores a
bearer directly (FR-010). The providers still own what the operator TYPES —
their catalog fields — which is the half that genuinely differs per provider.

No new auth SDK for the token exchanges themselves. ``authlib``, ``pyjwt``
and ``cryptography`` are already dependencies and cover both a signed
service-account assertion and a client-credentials exchange; ``msal`` would
add a transitive tree for one of them.

``google-auth`` is the one exception, and only for ONE job: resolving the
PLATFORM's own Google identity when impersonating (see
:func:`_ambient_google_token`). Its Application Default Credentials
resolution order — metadata server, key file, external account, developer
login — is precisely the wheel not to reinvent, and its external-account
branch is how a federated deployment would arrive here with no further code.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final
from urllib.parse import quote

from app.a2a.client.destinations import DestinationRefusedError, assert_allowed_async
from app.a2a.client.failures import CREDENTIAL_CANNOT_MINT, Classification
from app.a2a.providers import (
    ENTRA_A2A_PROVIDER,
    GOOGLE_A2A_PROVIDER,
    OAUTH_A2A_PROVIDER,
)
from app.core.security import decrypt_json, decrypt_value
from app.services.vault import (
    SecretUnavailableError,
    invalidate_entity_secrets,
    reveal_field,
    reveal_json_field,
)

logger = logging.getLogger(__name__)

class CredentialError(Exception):
    """Stored credentials could not produce a token.

    DURABLE by construction (FR-010): a credential that cannot mint will not
    mint on the next run either, so this must never reach the circuit
    breaker's counter — it makes the registration unusable with a reason an
    operator can act on.
    """

    classification: Classification = CREDENTIAL_CANNOT_MINT


@dataclass(frozen=True)
class MintedToken:
    value: str
    expires_at: float

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.value}"}


async def _config(integration) -> dict[str, Any]:
    """The integration's extra config, every vault reference in it resolved, or an empty dict.

    The reader of the credentials a mint sends (#6080): a client secret, a
    service account key or a tenant held in a vault is read from it. A referenced
    secret the vault cannot give is a ``CredentialError`` with the vault's
    generic message, never the reference or a value. A ciphertext that cannot
    be READ is treated as absent rather than as an error to surface verbatim —
    the message could carry ciphertext, and an unreadable credential is the
    same operator problem as a missing one (constitution Principle II).
    """
    if not getattr(integration, "extra_config_enc", None):
        return {}
    try:
        data = await reveal_json_field(integration, "extra_config_enc")
    except SecretUnavailableError as exc:
        raise CredentialError(str(exc)) from None
    except Exception:
        logger.warning("a2a.client: unreadable extra_config for integration %s", integration.id)
        return {}
    return data if isinstance(data, dict) else {}


def _stored_config(integration) -> dict[str, Any]:
    """The integration's extra config as stored, for the card URLs alone, or an empty dict.

    Never a credential: a value held in a vault stays a reference here, and the
    mint flows read the credentials through :func:`_config`.
    """
    try:
        data = decrypt_json(integration.extra_config_enc)
    except Exception:
        logger.warning(
            "a2a.client: unreadable extra_config for integration %s", integration.id
        )
        return {}
    return data if isinstance(data, dict) else {}


def configured_card_urls(integration) -> list[str]:
    """The card locations the operator entered, in order, de-duplicated.

    Public because it is not a credential and two modules need it: this one
    reads the rest of the ``extra_config`` to mint a token, and the target
    reconciler needs the URLs. Decryption stays in this module so
    ``extra_config_enc`` is read in one place.

    Plural because a partner exposes as many agents as it likes and each
    publishes its own card. Accepts commas or new lines as separators, which
    is what lets the ordinary extra-field text input carry a list without a
    bespoke widget.

    ``card_url`` is accepted as a singular fallback spelling so a
    hand-configured registration is not silently emptied.
    """
    config = _stored_config(integration)
    raw = config.get("card_urls")
    if not raw or (isinstance(raw, str) and not raw.strip()):
        raw = config.get("card_url")
    if isinstance(raw, list):
        candidates = [str(item) for item in raw]
    elif isinstance(raw, str):
        candidates = re.split(r"[,\n]", raw)
    else:
        return []

    out: list[str] = []
    for candidate in candidates:
        url = candidate.strip()
        if url and url not in out:
            out.append(url)
    return out


def _secret(enc: str | None) -> str | None:
    if not enc:
        return None
    try:
        return decrypt_value(enc)
    except Exception:
        return None


async def _client_secret(integration) -> str | None:
    """The stored client secret, a vault reference resolved; ``None`` when unreadable."""
    try:
        return await reveal_field(integration, "client_secret_enc")
    except SecretUnavailableError as exc:
        raise CredentialError(str(exc)) from None
    except Exception:
        return None


async def _forget_refused_client_secret(integration, status_code: int) -> None:
    """A token endpoint refusing the client (``invalid_client`` is a 400 or a 401): a vault-held secret is read again next time."""
    if status_code in (400, 401):
        await invalidate_entity_secrets(integration, ("client_secret_enc",))


async def _guard_token_endpoint(url: str, *, what: str) -> None:
    """Refuse a token endpoint this platform must not reach, before any request.

    Operator-supplied, and the POST to it carries the client secret: the same
    request-forgery surface as a card URL. A credential error, because the
    endpoint they typed is what the operator has to fix.
    """
    try:
        await assert_allowed_async(url)
    except DestinationRefusedError as exc:
        raise CredentialError(
            f"The {what} is not a destination this platform will send a "
            f"credential to — {exc}"
        ) from exc


async def _mint_oauth_client_credentials(integration, http_client) -> MintedToken:
    """Exchange a client id and secret for a bearer.

    The target is any partner accepting OAuth client-credentials — including
    **another XTM One deployment**, whose inbound surface issues exactly this
    credential shape. That makes it the one target testable end to end with no
    vendor account, and the one that exercises both directions of this
    feature at once.
    """
    cfg = await _config(integration)
    client_id = _secret(integration.client_id_enc) or cfg.get("client_id")
    client_secret = await _client_secret(integration) or cfg.get("client_secret")
    token_url = integration.token_url or cfg.get("token_url")
    if not (client_id and client_secret and token_url):
        raise CredentialError("Client id, secret and token URL are all required.")
    await _guard_token_endpoint(token_url, what="token URL")

    response = await http_client.post(
        token_url,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if response.status_code != 200:
        await _forget_refused_client_secret(integration, response.status_code)
        raise CredentialError(_token_refusal_detail(response, client_id))
    return _token_from_response(response)


#: The OAuth ``error`` values a token endpoint may answer (RFC 6749 §5.2), the
#: only ones echoed into a detail: anything else the partner sends is free text
#: an operator should not be shown as ours.
_OAUTH_TOKEN_ERRORS = frozenset(
    {
        "invalid_request",
        "invalid_client",
        "invalid_grant",
        "unauthorized_client",
        "unsupported_grant_type",
        "invalid_scope",
    }
)


def _token_refusal_detail(response: Any, client_id: str) -> str:
    """Why the partner refused a token, naming what the operator can check (#105).

    The OAuth error code (``invalid_client`` is what an XTM One partner answers
    for an unknown client id AND for a wrong secret alike) and the client id
    presented, which is an identifier, never the secret. With the client id in
    the detail, an operator compares it with the partner's in one glance.
    """
    error = None
    try:
        body = response.json()
        if isinstance(body, dict) and body.get("error") in _OAUTH_TOKEN_ERRORS:
            error = body["error"]
    except Exception:
        error = None
    said = f" ({error})" if error else ""
    hint = (
        " Check that the client id and the secret are the pair the partner issued "
        "most recently: a regenerate on the partner's side may have changed one of them."
        if error == "invalid_client" or response.status_code == 401
        else ""
    )
    return f"Partner token endpoint returned HTTP {response.status_code}{said} for client id {client_id}.{hint}"


async def _mint_entra_client_credentials(integration, http_client) -> MintedToken:
    """Present OUR OWN Entra credentials to Azure Foundry.

    The mirror image of the inbound ``entra_a2a`` mode, which VERIFIES a
    partner's Entra token against pinned facts. Same credential family,
    opposite direction, and not one stored field in common — which is why the
    catalog asks for different fields per direction.
    """
    cfg = await _config(integration)
    tenant_id = str(cfg.get("tenant_id") or "").strip()
    client_id = _secret(integration.client_id_enc) or cfg.get("client_id")
    client_secret = await _client_secret(integration) or cfg.get("client_secret")
    if not (tenant_id and client_id and client_secret):
        raise CredentialError("Tenant, client id and client secret are all required.")

    # Constant host, so nothing for the destination guard; the operator-typed
    # tenant lands in the path and is escaped rather than interpolated.
    response = await http_client.post(
        f"https://login.microsoftonline.com/{quote(tenant_id, safe='')}/oauth2/v2.0/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": cfg.get("scope") or "https://ai.azure.com/.default",
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if response.status_code != 200:
        await _forget_refused_client_secret(integration, response.status_code)
        raise CredentialError(f"Entra returned HTTP {response.status_code}.")
    return _token_from_response(response)


#: The impersonation exchange. A constant rather than anything derived from
#: operator input: the only configurable part of this mode is WHICH service
#: account to become.
_IAM_CREDENTIALS_URL = (
    "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
    "{email}:generateAccessToken"
)

#: The two outbound Google credential modes, as the catalog's
#: ``credential_mode`` dropdown spells them. A registration with none
#: recorded is read by inference instead.
MODE_IMPERSONATION: Final = "impersonation"
MODE_KEY: Final = "key"

_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"

#: The platform's OWN Google credential. Module-level because it is a fact
#: about the deployment rather than about any integration: every impersonation
#: starts from the same identity, and resolving it per delegation would be a
#: metadata round trip per delegation.
_ambient_credentials: Any = None
_ambient_lock = asyncio.Lock()


async def _ambient_google_token() -> str:
    """A bearer for the platform's own Google identity.

    Application Default Credentials, which resolves - in order - a workload
    identity from the metadata server (the deployment shape this mode exists
    for), a key file named by ``GOOGLE_APPLICATION_CREDENTIALS``, an
    external-account (Workload Identity Federation) configuration, or a
    developer's ``gcloud auth application-default login``.

    ``google-auth`` refreshes the credential itself once resolved, so the lock
    guards resolution and refresh rather than every read: two delegations
    starting together must not race two metadata fetches.

    Blocking - ``google-auth``'s transport is synchronous - so it runs off the
    event loop.
    """
    global _ambient_credentials

    def _resolve() -> str:
        global _ambient_credentials
        import google.auth
        from google.auth.transport.requests import Request

        if _ambient_credentials is None:
            _ambient_credentials, _project = google.auth.default(
                scopes=[_CLOUD_PLATFORM_SCOPE]
            )
        if not _ambient_credentials.valid:
            _ambient_credentials.refresh(Request())
        return str(_ambient_credentials.token)

    async with _ambient_lock:
        try:
            return await asyncio.to_thread(_resolve)
        except Exception as exc:
            # The most consequential message in this module, because the fix is
            # not on the integration at all: it is the deployment that has no
            # Google identity, and an operator reading "cannot mint a token"
            # would go looking at the fields they typed.
            raise CredentialError(
                "This deployment has no Google identity to impersonate from "
                f"({type(exc).__name__}). Impersonation needs the platform "
                "itself to run with Application Default Credentials - a "
                "workload identity, or GOOGLE_APPLICATION_CREDENTIALS - or a "
                "service account key on the integration instead."
            ) from exc


def _impersonation_target_problem(target: str) -> str | None:
    """Why *target* cannot be a service account address, if it cannot.

    Deliberately loose. A key check would be "ends with
    ``.iam.gserviceaccount.com``", and that would refuse the default accounts
    Google itself mints - ``PROJECT@appspot.gserviceaccount.com``,
    ``NUMBER-compute@developer.gserviceaccount.com`` - which are perfectly
    valid targets. So this only rejects what could never be an address at all,
    and lets Google's 404 be the authority on existence.
    """
    if len(target) > 320:
        return "The service account address is implausibly long."
    if any(c.isspace() for c in target):
        return "The service account address contains whitespace."
    local, _, domain = target.partition("@")
    if not local or not domain or "." not in domain:
        return (
            "The account to impersonate must be a service account email "
            "address, e.g. agent-caller@my-project.iam.gserviceaccount.com."
        )
    return None


async def _mint_google_impersonated(
    integration, http_client, target: str
) -> MintedToken:
    """Borrow the platform's identity to become *target*, and take its token.

    Keyless by construction, which is the whole point: nothing is stored on
    either side, the partner grants
    ``roles/iam.serviceAccountTokenCreator`` to this platform's identity and
    revokes by removing it, and every mint lands in their own audit log.

    It exists because ``constraints/iam.disableServiceAccountKeyCreation`` is
    Google's recommended baseline and is enforced by default in many
    organisations. Under it the key mode below cannot be configured at all -
    not misconfigured, unconfigurable - so a customer with an ordinary
    security posture had no way to be reached outbound.
    """
    problem = _impersonation_target_problem(target)
    if problem:
        raise CredentialError(problem)

    base = await _ambient_google_token()
    response = await http_client.post(
        _IAM_CREDENTIALS_URL.format(email=quote(target, safe="@")),
        json={"scope": [_CLOUD_PLATFORM_SCOPE], "lifetime": "3600s"},
        headers={"Authorization": f"Bearer {base}"},
    )
    if response.status_code != 200:
        # Google's OWN message, always, ahead of anything inferred from the
        # status. A 403 here usually means the impersonation permission is
        # missing - but it can also mean the iamcredentials API is disabled, or
        # that a user credential needs a quota project, and a fixed sentence
        # about IAM roles would send an operator to fix the wrong thing. So the
        # advice is offered as advice, and the authority is quoted.
        said = _google_error_message(response)
        detail = f" - {said}" if said else ""
        if response.status_code == 403:
            raise CredentialError(
                f"Google refused to impersonate {target}{detail}. Most often "
                "this platform's identity lacks "
                "roles/iam.serviceAccountTokenCreator on that service account, "
                "granted at the partner's end; it can also mean the "
                "iamcredentials API is not enabled on its project."
            )
        if response.status_code == 404:
            raise CredentialError(
                f"Google does not know the service account {target}{detail}."
            )
        raise CredentialError(
            f"Google returned HTTP {response.status_code} for the "
            f"impersonation exchange{detail}."
        )
    return _token_from_iam_response(response)


async def _mint_google_assertion(integration, http_client, key: Any) -> MintedToken:
    """Sign a service-account assertion and exchange it for a scoped token.

    Vertex accepts neither of the other two providers' flows: it wants a JWT
    signed with the service account's own key, exchanged at Google's token
    endpoint for a ``cloud-platform``-scoped access token. That is the whole
    reason ``google_a2a`` exists as a third catalog entry rather than as a
    direction on an existing one.
    """
    import jwt  # pyjwt, already a dependency

    if isinstance(key, str):
        import json

        try:
            key = json.loads(key)
        except ValueError as exc:
            raise CredentialError("Service account key is not valid JSON.") from exc
    if not isinstance(key, dict) or not key.get("private_key"):
        raise CredentialError("A service account key with a private_key is required.")

    now = int(time.time())
    token_uri = str(key.get("token_uri") or "https://oauth2.googleapis.com/token")
    # Operator-supplied via the pasted key JSON; guarded like the token URL.
    await _guard_token_endpoint(token_uri, what="key's token_uri")
    try:
        assertion = jwt.encode(
            {
                "iss": key["client_email"],
                "scope": _CLOUD_PLATFORM_SCOPE,
                "aud": token_uri,
                "iat": now,
                "exp": now + 3600,
            },
            key["private_key"],
            algorithm="RS256",
        )
    except Exception as exc:
        raise CredentialError(f"Could not sign the assertion: {type(exc).__name__}") from exc

    response = await http_client.post(
        token_uri,
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if response.status_code != 200:
        raise CredentialError(f"Google returned HTTP {response.status_code}.")
    return _token_from_response(response)


async def _mint_google(integration, http_client) -> MintedToken:
    """Two credential modes for one target, chosen by what is configured.

    A key signs its own assertion; impersonation borrows the platform's
    identity. Both mint a short-lived ``cloud-platform`` bearer from a stored
    non-interactive credential, so FR-010 holds either way - and impersonation
    holds it better, since the thing stored is an address rather than a secret.

    **Exactly one.** Refusing both is deliberate - see the inference below.
    """
    cfg = await _config(integration)
    mode = str(cfg.get("credential_mode") or "").strip().lower()
    target = str(cfg.get("impersonate_service_account") or "").strip()
    key = cfg.get("service_account_json")

    # An EXPLICIT choice wins, and says so when the matching field is empty.
    # The form asks for the mode and hides the other field, so a leftover value
    # from a previous mode must not decide anything - which is what the
    # inference below would have let it do.
    if mode == MODE_IMPERSONATION:
        if not target:
            raise CredentialError(
                "This registration is set to impersonate, but no service "
                "account to impersonate is configured."
            )
        return await _mint_google_impersonated(integration, http_client, target)
    if mode == MODE_KEY:
        if not key:
            raise CredentialError(
                "This registration is set to use a service account key, but "
                "no key JSON is configured."
            )
        return await _mint_google_assertion(integration, http_client, key)

    # No mode recorded: inferred from what is filled in, refusing both rather
    # than ranking them - an operator who filled in a key, switched to
    # impersonation and left the key behind would otherwise keep minting under
    # the key while believing otherwise, and the partner's audit log would
    # name an identity nobody expected to see there.
    if target and key:
        raise CredentialError(
            "Configure a service account key JSON or an account to "
            "impersonate, not both. Clear whichever this registration is not "
            "using."
        )
    if target:
        return await _mint_google_impersonated(integration, http_client, target)
    if key:
        return await _mint_google_assertion(integration, http_client, key)
    raise CredentialError(
        "Google outbound needs either a service account key JSON or a service "
        "account to impersonate."
    )


def _google_error_message(response) -> str:
    """What Google said went wrong, if it said anything readable.

    Google's API frontend answers ``{"error": {"code", "message", "status"}}``.
    Quoting it is not decoration: several causes share one status, and the only
    party that knows which applies is the one that refused.
    """
    try:
        error = (response.json() or {}).get("error")
    except Exception:
        return ""
    if isinstance(error, dict):
        message = error.get("message") or error.get("status")
        if message:
            return str(message).strip().rstrip(".")
    return ""


def _token_from_iam_response(response) -> MintedToken:
    """Read a ``generateAccessToken`` reply.

    Its own reader because the field names are not OAuth's: proto-JSON
    ``accessToken`` and an RFC 3339 ``expireTime``, where the OAuth reader
    below looks for ``access_token`` and ``expires_in``. Reusing it would find
    no token and report a misconfiguration that is not there.
    """
    try:
        data = response.json()
    except Exception as exc:
        raise CredentialError(
            "The impersonation exchange did not return JSON."
        ) from exc
    token = data.get("accessToken")
    if not token:
        raise CredentialError(
            "The impersonation exchange returned no accessToken."
        )

    # An hour is the API's own default lifetime, so a reply whose expiry cannot
    # be read is re-minted on that assumption rather than treated as broken.
    expires_at = time.time() + 3600.0
    expiry = data.get("expireTime")
    if isinstance(expiry, str):
        try:
            expires_at = (
                datetime.strptime(expiry, "%Y-%m-%dT%H:%M:%SZ")
                .replace(tzinfo=UTC)
                .timestamp()
            )
        except ValueError:
            logger.info("a2a.client: unreadable expireTime %r, assuming an hour", expiry)
    return MintedToken(value=str(token), expires_at=expires_at)


def _token_from_response(response) -> MintedToken:
    """Read a token response, refusing anything that is not one.

    An endpoint answering 200 with no token is a misconfiguration that would
    otherwise surface later as an unexplained partner 401 — much further from
    its cause.
    """
    try:
        data = response.json()
    except Exception as exc:
        raise CredentialError("Token endpoint did not return JSON.") from exc
    token = data.get("access_token")
    if not token:
        raise CredentialError("Token endpoint returned no access_token.")
    try:
        lifetime = float(data.get("expires_in", 3600))
    except (TypeError, ValueError):
        # The token is fine; ``expires_in`` only says when to stop using it.
        logger.info(
            "a2a.client: unreadable expires_in %r, assuming an hour",
            data.get("expires_in"),
        )
        lifetime = 3600.0
    return MintedToken(value=str(token), expires_at=time.time() + lifetime)


#: Provider -> flow. Adding a target means adding one entry and one function,
#: which is the seam that keeps "all three mint short-lived" checkable.
_FLOWS = {
    OAUTH_A2A_PROVIDER: _mint_oauth_client_credentials,
    ENTRA_A2A_PROVIDER: _mint_entra_client_credentials,
    GOOGLE_A2A_PROVIDER: _mint_google,
}


async def mint_token(integration, http_client) -> MintedToken:
    """Mint a bearer for this integration's target.

    Raises :class:`CredentialError` — always durable — when the stored
    credentials cannot produce one. That is distinguishable from an
    unreachable partner (transient) and from a partner rejecting a token we
    DID mint (durable, different reason), which is the three-way split FR-010
    requires.
    """
    flow = _FLOWS.get(integration.provider)
    if flow is None:
        raise CredentialError(
            f"{integration.provider} has no outbound credential flow."
        )
    return await flow(integration, http_client)


#: Minted bearers for their own lifetime, keyed by the credential MATERIAL so
#: a rotated secret is a miss rather than a stale hit. Process memory, never
#: persisted: FR-010 is about what is stored.
_TOKENS: dict[str, MintedToken] = {}
_EXPIRY_MARGIN_SECONDS: Final = 60.0


def _cache_key(integration) -> str:
    return "|".join(
        str(getattr(integration, field, None))
        for field in (
            "id",
            "provider",
            "client_id_enc",
            "client_secret_enc",
            "extra_config_enc",
            "token_url",
        )
    )


def forget_token(integration) -> None:
    """Drop the cached bearer, so the next call mints one.

    For a partner that rejected it: a token revoked at the partner's end is
    otherwise presented again until it expires.
    """
    _TOKENS.pop(_cache_key(integration), None)


async def outbound_headers(integration, http_client, *, fresh: bool = False) -> dict[str, str]:
    """The Authorization header to present to this partner.

    Cached for the token's lifetime less a margin — a mint is a network round
    trip, and for Google impersonation an extra ``iamcredentials`` exchange,
    per call otherwise. ``fresh`` bypasses the cache for an on-demand
    verification, which must test the credential rather than a token minted
    before the operator changed anything.
    """
    key = _cache_key(integration)
    now = time.time()
    token = None if fresh else _TOKENS.get(key)
    if token is None or token.expires_at - now < _EXPIRY_MARGIN_SECONDS:
        for stale in [k for k, t in _TOKENS.items() if t.expires_at - now < _EXPIRY_MARGIN_SECONDS]:
            del _TOKENS[stale]
        token = _TOKENS[key] = await mint_token(integration, http_client)
        # A token was minted: the integration's status follows its credential
        # (#12346). Only on a real mint, never on a cached token.
        from app.a2a.client.credential_state import record_mint_success

        await record_mint_success(integration, force=fresh)
    return token.headers()
