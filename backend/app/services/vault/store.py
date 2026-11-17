"""The database half of the vault providers.

``secret_vault_providers`` holds the providers an administrator connected in
Settings > Access and security > Secret vaults. Every process keeps them in its resolver
(``resolver.SecretResolver.apply_database_providers``):

* :func:`refresh_vault_providers` reads them on a session of its own and
  applies them. Called at startup (before the web process accepts traffic,
  when the worker starts), after every administrator change, and every
  :data:`~app.core.security_options.REFRESH_INTERVAL_SECONDS` by the security
  options refresh loop in every web and worker process - which is what makes
  a change on one replica apply on the others. A failed read keeps the
  providers already applied.
* :func:`vault_principal` is who a caller is for a provider's grants.

The credentials are decrypted here, into the resolver's memory only.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.core.utils import format_exception_message
from app.services.vault.config import (
    ADMIN_PRINCIPAL,
    VaultConfigError,
    VaultPrincipal,
    VaultProviderConfig,
    database_config,
)
from app.services.vault.resolver import get_resolver

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.secret_vault_provider import SecretVaultProvider

logger = logging.getLogger(__name__)

_last_refresh_failed = False


def stored_credentials(row: SecretVaultProvider) -> dict[str, str]:
    """A provider row's credentials, decrypted (``{}`` when none is stored)."""
    from app.core.security import decrypt_json

    document = decrypt_json(row.credentials_enc)
    return {str(key): str(value) for key, value in document.items()} if isinstance(document, dict) else {}


def encrypt_credentials(credentials: dict[str, str]) -> str | None:
    """The ``credentials_enc`` of *credentials*: ``None`` when there is none to store."""
    import json

    from app.core.security import encrypt_value

    kept = {key: value for key, value in credentials.items() if value}
    return encrypt_value(json.dumps(kept, sort_keys=True)) if kept else None


def config_of_row(row: SecretVaultProvider, *, credentials: dict[str, Any] | None = None) -> VaultProviderConfig:
    """The configuration a provider row declares; :class:`VaultConfigError` for one it cannot use."""
    return database_config(
        name=row.name,
        type=row.type,
        settings=row.settings or {},
        credentials=stored_credentials(row) if credentials is None else credentials,
        id=str(row.id),
        display_name=row.display_name,
        enabled=bool(row.enabled),
        grants=row.grants or [],
        browsable=bool(row.browsable),
        cache_seconds=row.cache_seconds,
    )


async def load_database_configs(db: AsyncSession) -> list[VaultProviderConfig]:
    """Every stored provider's configuration; a row that cannot be used is logged and left out."""
    from app.models.secret_vault_provider import SecretVaultProvider

    rows = (await db.execute(select(SecretVaultProvider).order_by(SecretVaultProvider.name))).scalars().all()
    configs: list[VaultProviderConfig] = []
    for row in rows:
        try:
            configs.append(config_of_row(row))
        except (VaultConfigError, ValueError) as exc:
            # A stored row is validated when written: this is a row edited outside the API.
            logger.error("Vault provider '%s' stored in the database is not usable: %s", row.name, exc)
    return configs


async def refresh_vault_providers() -> bool:
    """Read the stored providers on a session of its own and apply them in this process; False when the database did not answer."""
    global _last_refresh_failed
    from app.core.database import async_session

    resolver = get_resolver()
    # Taken before the read: a change committed after this point applies with a later ticket.
    ticket = resolver.snapshot_ticket()
    try:
        async with async_session() as db:
            configs = await load_database_configs(db)
    except (SQLAlchemyError, OSError) as exc:
        if not _last_refresh_failed:
            logger.warning(
                "Could not read the vault providers (%s); keeping the ones this process already had",
                format_exception_message(exc),
            )
        _last_refresh_failed = True
        return False
    _last_refresh_failed = False
    resolver.apply_database_providers(configs, ticket=ticket)
    return True


def _user_id(user: Any) -> str | None:
    ident = getattr(user, "id", None)
    return str(ident) if ident is not None else None


def principal_from(user: Any, group_ids: Any = None) -> VaultPrincipal:
    """:func:`vault_principal` from group ids a route already read (``None``: none known)."""
    if getattr(user, "is_admin", False):
        return VaultPrincipal(is_admin=True, user_id=_user_id(user))
    return VaultPrincipal(
        is_admin=False,
        user_id=_user_id(user),
        group_ids=frozenset(str(group_id) for group_id in group_ids or ()),
    )


def _groups_matter() -> bool:
    """Whether a provider grants a group: only then is a caller's group membership read."""
    from app.services.vault.config import GrantPolicy

    return any(
        isinstance(policy, GrantPolicy) and any(grant.principal == "group" for grant in policy.grants)
        for policy in get_resolver().policies.values()
    )


async def vault_principal(db: AsyncSession, user: Any) -> VaultPrincipal:
    """Who *user* is for the grants of a vault provider: an administrator, or their id and groups."""
    if user is None:
        return VaultPrincipal(is_admin=False)
    if getattr(user, "is_admin", False) or not _groups_matter():
        return principal_from(user)
    from app.api.v1.helpers import get_user_group_ids

    return principal_from(user, await get_user_group_ids(db, user.id))


__all__ = [
    "ADMIN_PRINCIPAL",
    "config_of_row",
    "encrypt_credentials",
    "load_database_configs",
    "principal_from",
    "refresh_vault_providers",
    "stored_credentials",
    "vault_principal",
]
