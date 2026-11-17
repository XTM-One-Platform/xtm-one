"""Secrets read from enterprise vaults: the one way a secret field is read where it is used.

A secret field holds a value or a ``vault://<provider>/<path>#<key>``
reference (``app/core/secret_refs.py``). Code that USES a secret reads it
here - :func:`reveal_field` for a loaded row, :func:`reveal_ciphertext` for a
column selected on its own, :func:`reveal_json_field` for a JSON document of
values (MCP headers and environment) - and gets the value, a reference
resolved through its provider. ``decrypt_value`` refuses a reference, and
``tests/test_vault_secret_readers.py`` fails the build on a reader of a
secret column that bypasses these helpers.

A reference that cannot be resolved raises :class:`SecretUnavailableError`,
whose message is generic; the reason goes to the server log, to the entity
(:func:`entity_failures`, shown on its page) and to the Security Audit.
"""

from __future__ import annotations

import json
import time
from collections.abc import Collection, Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from app.core.secret_refs import (
    COMPOUND_SEPARATOR,
    SecretReference,
    SecretReferenceSyntaxError,
    holds_secret_reference,
    is_secret_reference,
    parse_secret_reference,
    secret_reference_problem,
    secret_references_in,
)
from app.services.vault.config import ADMIN_PRINCIPAL, GrantPolicy, VaultPrincipal
from app.services.vault.errors import (
    REASON_DESCRIPTIONS,
    VAULT_UNAVAILABLE_DETAIL,
    SecretUnavailableError,
    VaultReadError,
)
from app.services.vault.failures import (
    SecretOwner,
    clear_failure,
    entity_failures,
    recent_failures,
    record_failure,
    reference_fingerprint,
)
from app.services.vault.resolver import configured_provider_names, get_resolver, log_vault_configuration
from app.services.vault.variable_refs import (
    holds_variable_reference,
    resolve_variable_references,
    takes_variable_references,
    variable_reference_problem,
)

__all__ = [
    "ADMIN_PRINCIPAL",
    "REASON_DESCRIPTIONS",
    "VAULT_UNAVAILABLE_DETAIL",
    "SecretOwner",
    "SecretUnavailableError",
    "StoredReferences",
    "VaultPrincipal",
    "configured_provider_names",
    "entity_failures",
    "get_resolver",
    "holds_any_reference",
    "invalidate_entity_secrets",
    "log_vault_configuration",
    "owner_of",
    "recent_failures",
    "reference_access_problem",
    "reference_fingerprint",
    "reference_problem",
    "reference_writer_problem",
    "resolve_secret_text",
    "retained_reference_writer_problem",
    "reveal_ciphertext",
    "reveal_field",
    "reveal_json_field",
    "scan_stored_references",
    "secret_fields_problem",
    "secret_fields_writer_problem",
    "stored_reference_entries",
    "stored_reference_providers",
    "stored_references_by_field",
    "variable_reference_problem",
]

#: The kind an entity's failures are recorded under, by table.
ENTITY_KINDS: dict[str, str] = {
    "integrations": "integration",
    "mcp_servers": "mcp_server",
    "llm_provider_configs": "llm_provider",
    "variables": "variable",
    "channel_bots": "channel_bot",
    "sso_config": "sso",
    "langfuse_config": "langfuse",
    "browser_profiles": "browser_profile",
    "oauth_defaults": "oauth_default",
}

#: The secret columns that accept a vault reference, by table (the write paths check
#: ``reference_problem`` on exactly these; every other secret column refuses one).
#: ``tests/test_vault_reference_coverage.py`` holds every encrypted column of every
#: model to being listed here or exempted with its reason.
REFERENCE_COLUMNS: dict[str, tuple[str, ...]] = {
    "integrations": ("api_key_enc", "client_secret_enc"),
    "mcp_servers": ("headers_enc", "env_enc", "client_secret_enc"),
    "llm_provider_configs": ("api_key_enc",),
    "variables": ("value_enc",),
    "channel_bots": ("api_key_enc", "client_secret_enc", "extra_config_enc"),
    "sso_config": ("oidc_client_secret_enc",),
    "langfuse_config": ("public_key_enc", "secret_key_enc"),
    "browser_profiles": ("credentials_enc",),
    # Also the key LinkedIn signs its webhook deliveries with: the signature gates read it
    # through ``api/v1/webhooks.py`` ``reveal_webhook_client_secret``.
    "oauth_defaults": ("client_secret_enc",),
}

#: The reference columns holding a JSON document, whose string values (nested ones included)
#: may each be a reference (:func:`reveal_json_field`); the others hold one text. A browser
#: profile's credentials are read one by one (``browser_profiles.profiles.resolved_snapshot``).
#: ``extra_config_enc`` is a JSON document on every table that has it; only the tables listing
#: it in :data:`REFERENCE_COLUMNS` accept a reference there.
JSON_REFERENCE_COLUMNS = frozenset({"headers_enc", "env_enc", "credentials_enc", "extra_config_enc"})


def owner_of(entity: Any, column: str) -> SecretOwner:
    """Who a secret column belongs to, for the failure record: ``integration`` / id / ``api_key``."""
    table = getattr(entity, "__tablename__", "") or type(entity).__name__.lower()
    return SecretOwner(
        kind=ENTITY_KINDS.get(table, table),
        id=str(getattr(entity, "id", "") or ""),
        field=column.removesuffix("_enc"),
    )


#: When this process last cleared an owner's failure record: a success clears it at
#: most once per cache period (the record is shared, the reads are frequent).
_cleared_at: dict[SecretOwner, float] = {}


async def _resolve(reference: SecretReference, owner: SecretOwner | None) -> str:
    resolver = get_resolver()
    try:
        value = await resolver.read(reference)
    except VaultReadError as exc:
        if owner is not None:
            _cleared_at.pop(owner, None)
            await record_failure(owner, reference.provider, exc.reason, reference)
        raise SecretUnavailableError(exc.reason) from None
    if owner is not None:
        now = time.monotonic()
        last = _cleared_at.get(owner)
        if last is None or now - last >= max(resolver.cache_seconds_for(reference.provider), 1.0):
            _cleared_at[owner] = now
            await clear_failure(owner)
    return value


async def resolve_secret_text(text: str, owner: SecretOwner | None = None) -> str:
    """*text* with its vault reference(s) resolved: the whole value, or each ``||`` segment that is one."""
    if not holds_secret_reference(text):
        return text
    try:
        references = secret_references_in(text)
    except SecretReferenceSyntaxError:
        # Not recorded: the entity and the audit diagnose it from the stored value (scan_stored_references).
        raise SecretUnavailableError("invalid_reference") from None
    if COMPOUND_SEPARATOR not in text:
        return await _resolve(references[0], owner)
    parts = []
    for part in text.split(COMPOUND_SEPARATOR):
        parts.append(await _resolve(parse_secret_reference(part), owner) if is_secret_reference(part) else part)
    return COMPOUND_SEPARATOR.join(parts)


async def reveal_ciphertext(ciphertext: str | None, owner: SecretOwner | None = None) -> str | None:
    """The secret a stored ciphertext holds, a reference resolved; ``None`` for an empty column."""
    if not ciphertext:
        return None
    from app.core.security import decrypt_stored_value

    return await resolve_secret_text(decrypt_stored_value(ciphertext), owner)


async def reveal_field(entity: Any, column: str) -> str | None:
    """The secret in *entity*'s *column* (``api_key_enc``), a reference resolved; ``None`` when empty.

    An integration credential may instead hold ``{{var_id.<uuid>}}`` placeholders
    naming secret variables, resolved here with the integration owner's
    visibility (``vault/variable_refs.py``). Only in the stored text: a value read
    from a vault is never searched for placeholders.
    """
    ciphertext = getattr(entity, column)
    owner = owner_of(entity, column)
    if not ciphertext or not takes_variable_references(entity, column):
        return await reveal_ciphertext(ciphertext, owner)
    from app.core.security import decrypt_stored_value

    text = decrypt_stored_value(ciphertext)
    if holds_variable_reference(text) and not holds_secret_reference(text):
        return await resolve_variable_references(text, entity, owner)
    return await resolve_secret_text(text, owner)


async def _resolve_values(value: Any, owner: SecretOwner) -> Any:
    if isinstance(value, str):
        return await resolve_secret_text(value, owner)
    if isinstance(value, dict):
        return {key: await _resolve_values(item, owner) for key, item in value.items()}
    if isinstance(value, list):
        return [await _resolve_values(item, owner) for item in value]
    return value


async def reveal_json_field(entity: Any, column: str, *, without: Collection[str] = ()) -> Any:
    """The JSON document in *entity*'s *column*, every string value that is a reference resolved.

    *without* names top-level keys, matched case-insensitively, dropped before
    anything is resolved: a value the caller replaces is never read from its vault.
    """
    from app.core.security import decrypt_json

    document = decrypt_json(getattr(entity, column))
    if document is None:
        return None
    if without and isinstance(document, dict):
        dropped = {key.lower() for key in without}
        document = {key: value for key, value in document.items() if not (isinstance(key, str) and key.lower() in dropped)}
    return await _resolve_values(document, owner_of(entity, column))


def _reference_texts(value: Any) -> Iterator[str]:
    """The strings in *value* holding a reference, at any depth of a JSON document, as :func:`_resolve_values` reads them."""
    if isinstance(value, str):
        if holds_secret_reference(value):
            yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _reference_texts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _reference_texts(item)


def _scan_column(entity: Any, column: str) -> tuple[list[SecretReference], bool]:
    """Every reference *entity*'s *column* holds, as the readers resolve it, and whether it holds one that does not parse."""
    from app.core.security import decrypt_stored_value

    ciphertext = getattr(entity, column, None)
    if not ciphertext:
        return [], False
    try:
        text = decrypt_stored_value(ciphertext)
    except Exception:
        return [], False
    stored: Any = text
    if column in JSON_REFERENCE_COLUMNS:
        try:
            stored = json.loads(text)
        except ValueError:
            return [], False
    found: list[SecretReference] = []
    malformed = False
    for value in _reference_texts(stored):
        try:
            found.extend(secret_references_in(value))
        except SecretReferenceSyntaxError:
            malformed = True
    return found, malformed


def stored_reference_entries(entity: Any, column: str) -> list[tuple[str | None, SecretReference]]:
    """Every reference *entity*'s *column* holds, with the top-level entry of a JSON document holding it (``None`` for a text column)."""
    from app.core.security import decrypt_stored_value

    ciphertext = getattr(entity, column, None)
    if not ciphertext:
        return []
    try:
        text = decrypt_stored_value(ciphertext)
    except Exception:
        return []
    texts: list[tuple[str | None, str]] = []
    if column in JSON_REFERENCE_COLUMNS:
        try:
            document = json.loads(text)
        except ValueError:
            return []
        if isinstance(document, dict):
            texts = [(str(key), found) for key, value in document.items() for found in _reference_texts(value)]
    else:
        texts = [(None, text)]
    entries: list[tuple[str | None, SecretReference]] = []
    for entry, value in texts:
        try:
            entries.extend((entry, reference) for reference in secret_references_in(value))
        except SecretReferenceSyntaxError:
            continue
    return entries


def _stored_references(entity: Any, column: str) -> list[SecretReference]:
    """Every reference *entity*'s *column* holds, as the readers resolve it; one that does not parse is skipped."""
    return _scan_column(entity, column)[0]


@dataclass(frozen=True)
class StoredReferences:
    """What an entity's secret fields hold that is read from a vault, by field."""

    #: ``{field: references}`` for each column holding at least one reference.
    by_field: dict[str, list[SecretReference]]
    #: The fields holding a value that starts with ``vault://`` but is not a valid reference
    #: (stored before the prefix was reserved): every use of them fails until it is replaced.
    malformed: list[str]


def scan_stored_references(entity: Any, columns: Iterable[str]) -> StoredReferences:
    """The references *entity*'s *columns* hold, and the fields holding one that does not parse (never its text)."""
    by_field: dict[str, list[SecretReference]] = {}
    malformed: list[str] = []
    for column in columns:
        references, unparsed = _scan_column(entity, column)
        name = column.removesuffix("_enc")
        if references:
            by_field[name] = references
        if unparsed:
            malformed.append(name)
    return StoredReferences(by_field=by_field, malformed=malformed)


def stored_references_by_field(entity: Any, columns: Iterable[str]) -> dict[str, list[SecretReference]]:
    """``{field: references}`` for each column holding at least one reference."""
    return scan_stored_references(entity, columns).by_field


def stored_reference_providers(entity: Any, columns: Iterable[str]) -> dict[str, list[str]]:
    """``{field: providers}`` for each column holding a reference: what a response may say about it.

    An MCP header or environment document may reference several providers: each is listed once.
    """
    return {
        field: sorted({reference.provider for reference in references})
        for field, references in stored_references_by_field(entity, columns).items()
    }


def retained_reference_writer_problem(
    entity: Any, *, replaced: Collection[str] = (), principal: VaultPrincipal
) -> str | None:
    """Why the caller may not re-point *entity* while it keeps references they could not write; ``None`` when they may.

    A reference makes the platform send the secret wherever its entity points
    (an integration's base URL or token endpoint, an MCP server's URL or
    command). Changing where it points is therefore held to the rule of writing
    the reference (an environment provider's ``WRITERS`` / ``PATHS``, a
    database provider's grants): a caller the provider does not admit at that
    path must replace, in the same edit, every field still holding such a
    reference (*replaced*: the columns the edit rewrites). The caller decides
    which edits re-point the entity; this only says whether one may.
    """
    if principal.is_admin:
        return None
    resolver = get_resolver()
    for column in REFERENCE_COLUMNS.get(getattr(entity, "__tablename__", ""), ()):
        if column in replaced:
            continue
        for reference in _stored_references(entity, column):
            policy = resolver.policy(reference.provider)
            if policy.admits(principal, reference.path):
                continue
            who = "you may not write" if isinstance(policy, GrantPolicy) else "which only administrators may write"
            return (
                f"{column.removesuffix('_enc')} holds a reference to vault provider '{reference.provider}', "
                f"{who}: only an administrator may change where this sends it, "
                "unless the same edit replaces that field"
            )
    return None


async def invalidate_entity_secrets(entity: Any, columns: Iterable[str]) -> None:
    """Forget the cached values of the references *entity* holds, so the next use reads the vault again.

    Called when an upstream service refused a credential: a secret rotated in
    the vault is then picked up at once instead of after the cache time.
    """
    for column in columns:
        for reference in _stored_references(entity, column):
            get_resolver().invalidate(reference)


def reference_problem(value: Any) -> str | None:
    """Why *value* cannot be written to a secret field that accepts references; ``None`` when it can.

    A JSON document (MCP headers, environment) is checked value by value, at any depth.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            if problem := reference_problem(item):
                return f"{key}: {problem}"
        return None
    if isinstance(value, list):
        for item in value:
            if problem := reference_problem(item):
                return problem
        return None
    if not holds_secret_reference(value):
        return None
    if problem := secret_reference_problem(value, configured_provider_names()):
        return problem
    resolver = get_resolver()
    for reference in secret_references_in(value):
        if reference.provider in resolver.disabled:
            return f"vault provider '{reference.provider}' is disabled"
        if not resolver.policy(reference.provider).allows_path(reference.path):
            return f"the path is outside the paths vault provider '{reference.provider}' allows"
        if problem := resolver.path_problem(reference):
            return problem
    return None


def reference_access_problem(reference: SecretReference, principal: VaultPrincipal) -> str | None:
    """Why *principal* may not name *reference*; ``None`` when they may (a 403 where it is written)."""
    policy = get_resolver().policy(reference.provider)
    if not policy.usable_by(principal):
        if isinstance(policy, GrantPolicy):
            return f"you may not use vault provider '{reference.provider}'"
        return f"only administrators may store a reference to vault provider '{reference.provider}'"
    if not policy.admits(principal, reference.path):
        return f"the path is outside the paths you may use on vault provider '{reference.provider}'"
    return None


def reference_writer_problem(value: Any, *, principal: VaultPrincipal) -> str | None:
    """Why the caller may not store the vault reference(s) *value* holds; ``None`` when they may.

    The provider decides: an environment provider's ``WRITERS`` setting
    (administrators only unless it says ``all``), a database provider's grants
    (administrators, then everyone, a group or a user, each for its path
    prefixes). A reference makes the platform send the secret wherever the
    entity holding it points, so who may write one is the provider's owner's
    choice. A malformed reference is left to :func:`reference_problem`.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            if problem := reference_writer_problem(item, principal=principal):
                return f"{key}: {problem}"
        return None
    if isinstance(value, list):
        for item in value:
            if problem := reference_writer_problem(item, principal=principal):
                return problem
        return None
    if not isinstance(value, str) or not holds_secret_reference(value):
        return None
    try:
        references = secret_references_in(value)
    except SecretReferenceSyntaxError:
        return None
    for reference in references:
        if problem := reference_access_problem(reference, principal):
            return problem
    return None


def secret_fields_writer_problem(fields: dict[str, Any], *, principal: VaultPrincipal) -> str | None:
    """``field: reason`` for the first secret field holding a reference the caller may not store, or ``None``."""
    for name, value in fields.items():
        if problem := reference_writer_problem(value, principal=principal):
            return f"{name}: {problem}"
    return None


#: What a refusal of a reference in an unsupported field says.
UNSUPPORTED_REFERENCE = "vault references are not accepted in this field"


def holds_any_reference(value: Any) -> bool:
    """Whether *value* (a string, or a JSON document's string values) is or holds a vault reference."""
    if isinstance(value, str):
        return holds_secret_reference(value)
    if isinstance(value, dict):
        return any(holds_any_reference(item) for item in value.values())
    if isinstance(value, list):
        return any(holds_any_reference(item) for item in value)
    return False


def secret_fields_problem(accepting: dict[str, Any], refusing: dict[str, Any] | None = None) -> str | None:
    """Why a write's secret fields cannot be stored: ``field: reason``, or ``None``.

    *accepting* are the fields that take a value or a vault reference (a bad
    reference, or one to an undeclared provider, is refused); *refusing* the
    fields that take a value only.
    """
    for name, value in accepting.items():
        if problem := reference_problem(value):
            return f"{name}: {problem}"
    for name, value in (refusing or {}).items():
        if holds_any_reference(value):
            return f"{name}: {UNSUPPORTED_REFERENCE}"
    return None
