# SPDX-License-Identifier: Apache-2.0

"""Client secrets of an identity provider's app registrations (integration seam).

An integration whose identity provider issues OAuth apps can declare that the
platform may mint (and rotate) a client secret on one of those apps with an
app-only credential the platform already holds: an integration of that
provider in API key mode, never a person's portal token. The provider registers
an :class:`AppSecretMinter` at import time; the administrator tool
``create_integration_app_secret`` (``app.services.builtin_tools.platform_admin_credentials``)
finds the minter here, reads the credential through it and stores the new
secret straight into the platform, so no generic layer imports a provider.

A provider without a minter is simply not offered. Errors are sentences naming
what is missing, written by the provider that knows what its API needs.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.integration import Integration


@dataclass(frozen=True)
class AppRegistration:
    """An app registration: its application (client) id and its object id."""

    app_id: str
    object_id: str
    display_name: str


@dataclass(frozen=True)
class MintedSecret:
    """A client secret just added. ``value`` is for storing only: never served, never logged."""

    key_id: str
    value: str
    expires_at: str
    display_name: str


#: The app-only token of a credential integration, or the sentence naming what it lacks.
CredentialReader = Callable[["Integration"], Awaitable[tuple[str | None, str | None]]]
AppFinder = Callable[..., Awaitable[tuple[AppRegistration | None, str | None]]]
SecretAdder = Callable[..., Awaitable[tuple[MintedSecret | None, str | None]]]
SecretRemover = Callable[[str, AppRegistration, str], Awaitable[str | None]]


@dataclass(frozen=True)
class AppSecretMinter:
    """What a provider declares to let the platform mint secrets on its apps.

    * ``provider``: the catalog key of the integration holding the app-only credential.
    * ``display_name``: how answers name the identity provider.
    * ``max_days``: the longest lifetime the identity provider accepts.
    * ``missing_credential``: the sentence answered when no credential exists.
    * ``read_credential(row)``: the token to call the provider's API with.
    * ``find_app(token, app_id=, object_id=)``, ``add_secret(token, app, display_name=, days=)``,
      ``remove_secret(token, app, key_id)``: the API calls.
    """

    provider: str
    display_name: str
    max_days: int
    missing_credential: str
    read_credential: CredentialReader
    find_app: AppFinder
    add_secret: SecretAdder
    remove_secret: SecretRemover


_MINTERS: dict[str, AppSecretMinter] = {}


def register_app_secret_minter(minter: AppSecretMinter) -> None:
    """Register *minter* for its provider. Idempotent (last wins)."""
    _MINTERS[minter.provider] = minter


def resolve_minter(provider: str = "") -> tuple[AppSecretMinter | None, str | None]:
    """The minter named by *provider*, or the only one registered; else the error sentence."""
    if provider:
        minter = _MINTERS.get(provider)
        if minter is None:
            known = ", ".join(sorted(_MINTERS)) or "none"
            return None, f"Error: provider '{provider}' cannot mint app secrets (providers that can: {known})."
        return minter, None
    if len(_MINTERS) == 1:
        return next(iter(_MINTERS.values())), None
    if not _MINTERS:
        return None, "Error: no integration provider on this platform can mint app secrets."
    return None, (
        "Error: several providers can mint app secrets "
        f"({', '.join(sorted(_MINTERS))}); name one as credential_provider."
    )
