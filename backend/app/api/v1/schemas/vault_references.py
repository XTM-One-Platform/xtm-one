# SPDX-License-Identifier: Apache-2.0

"""Request validation for secret fields and vault references.

A field that accepts a ``vault://<provider>/<path>#<key>`` reference refuses a
malformed one, one naming a provider this platform does not know or that is
disabled, or one outside the paths its provider allows, when it is written (a
422 naming the field), rather than failing every time it is used; a caller the
provider does not let use it at that path gets a 403
(:func:`require_vault_writer`). Any other field refuses a reference outright.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

from fastapi import HTTPException
from pydantic import BaseModel, PrivateAttr, ValidationInfo, field_validator

from app.services.vault import (
    ENTITY_KINDS,
    REASON_DESCRIPTIONS,
    REFERENCE_COLUMNS,
    UNSUPPORTED_REFERENCE,
    VaultPrincipal,
    holds_any_reference,
    reference_fingerprint,
    reference_problem,
    scan_stored_references,
    secret_fields_writer_problem,
    stored_reference_entries,
    variable_reference_problem,
)
from app.services.vault.failures import failure_matches, failures_by_entity
from app.services.vault.resolver import get_resolver
from app.services.vault.variable_refs import stored_variable_references, variable_reference


class VaultSecretFailure(BaseModel):
    """The last failed read of a referenced secret: which field, which provider, why - never a path."""

    field: str
    provider: str
    reason: str
    description: str
    at: str


class VaultSecretLocation(BaseModel):
    """Where a stored reference reads its secret, as the picker shows it: never the value.

    ``path`` and ``key`` are given only to a viewer who may use that path of
    the provider; anyone else learns the provider alone.
    """

    provider: str
    provider_display_name: str
    path: str | None = None
    key: str | None = None
    #: For a JSON document (MCP headers, environment): the entry holding the reference.
    entry: str | None = None


class VaultSecretStatus(BaseModel):
    """What an entity's response says about its vault references."""

    #: ``{field: providers}`` for each secret field holding a reference (an MCP document may name several).
    references: dict[str, list[str]]
    #: The secret fields holding a value that starts with ``vault://`` but is not a valid reference:
    #: every use of them fails until it is replaced. Never the value.
    invalid_references: list[str] = []
    #: The fields whose reference could not be read lately.
    failures: list[VaultSecretFailure] = []
    #: ``{field: locations}``: where each reference reads, for the viewer's secret picker.
    locations: dict[str, list[VaultSecretLocation]] = {}
    #: ``{field: variable ids}`` for each credential reading a secret variable: ids only, never a value.
    variables: dict[str, list[str]] = {}
    #: Per field, the fingerprints of the references it holds: a failure is shown only for one of them.
    _fingerprints: dict[str, frozenset[str]] = PrivateAttr(default_factory=dict)


def _locations(entity: Any, column: str, principal: VaultPrincipal) -> list[VaultSecretLocation]:
    resolver = get_resolver()
    found: list[VaultSecretLocation] = []
    for entry, reference in stored_reference_entries(entity, column):
        config = resolver.config_of(reference.provider)
        shown = resolver.policy(reference.provider).admits(principal, reference.path)
        found.append(
            VaultSecretLocation(
                provider=reference.provider,
                provider_display_name=config.display_name if config is not None and config.display_name else reference.provider,
                path=reference.path if shown else None,
                key=reference.key if shown else None,
                entry=entry,
            )
        )
    return found


def vault_status(entity: Any, principal: VaultPrincipal | None = None) -> VaultSecretStatus | None:
    """The references *entity*'s secret fields hold, valid or not (``None`` when none does); failures are added by the route.

    With *principal* (the viewer, when they may edit the entity), each
    reference's location is added for the secret picker: its path and key only
    where that viewer may use them.
    """
    columns = REFERENCE_COLUMNS.get(getattr(entity, "__tablename__", ""), ())
    stored = scan_stored_references(entity, columns)
    variables = stored_variable_references(entity)
    if not (stored.by_field or stored.malformed or variables):
        return None
    status = VaultSecretStatus(
        references={field: sorted({ref.provider for ref in refs}) for field, refs in stored.by_field.items()},
        invalid_references=stored.malformed,
        variables=variables,
    )
    if principal is not None:
        status.locations = {
            field: locations
            for column in columns
            if (field := column.removesuffix("_enc")) in stored.by_field
            and (locations := _locations(entity, column, principal))
        }
    fingerprints = {
        field: {reference_fingerprint(ref) for ref in refs} for field, refs in stored.by_field.items()
    }
    for field, ids in variables.items():
        fingerprints.setdefault(field, set()).update(reference_fingerprint(variable_reference(vid)) for vid in ids)
    status._fingerprints = {field: frozenset(prints) for field, prints in fingerprints.items()}
    return status


class _HasVaultStatus(Protocol):
    id: Any
    vault_secrets: VaultSecretStatus | None


async def attach_vault_failures(responses: Sequence[_HasVaultStatus], table: str) -> None:
    """Add to each response holding a reference the failures recorded for the references it still holds (one Redis round trip)."""
    holders = [response for response in responses if response.vault_secrets is not None]
    if not holders:
        return
    failures = await failures_by_entity(ENTITY_KINDS[table], [str(response.id) for response in holders])
    for response in holders:
        status = response.vault_secrets
        if status is None:
            continue
        status.failures = [
            VaultSecretFailure(
                field=entry["field"],
                provider=entry["provider"],
                reason=entry["reason"],
                description=REASON_DESCRIPTIONS.get(entry["reason"], ""),
                at=datetime.fromtimestamp(entry["at"], UTC).isoformat(),
            )
            for entry in failures.get(str(response.id), [])
            if failure_matches(entry, status._fingerprints)
        ]


async def require_vault_writer(db: Any, user: Any, **fields: Any) -> None:
    """Refuse (403, naming the field) a write storing a reference the caller may not store.

    The provider decides: an environment provider's ``WRITERS`` setting
    (administrators only by default), a database provider's grants. Checked
    on the route, where the caller is known, after the request body has passed
    :func:`vault_reference_validator`. The caller's groups are read only when
    a field holds a reference.
    """
    if not any(holds_any_reference(value) for value in fields.values()):
        return
    from app.services.vault.store import vault_principal

    principal = await vault_principal(db, user)
    if problem := secret_fields_writer_problem(fields, principal=principal):
        raise HTTPException(status_code=403, detail=problem)


async def require_secret_variable_references(db: Any, fields: dict[str, Any], *, viewer_ids: list[Any]) -> None:
    """Refuse (400, naming the field) a credential referencing a variable that is not a secret one every viewer may see."""
    if problem := await variable_reference_problem(db, fields, viewer_ids=viewer_ids):
        raise HTTPException(status_code=400, detail=problem)


def vault_reference_validator(*fields: str) -> Any:
    """A field validator for secret fields that take a value or a vault reference."""

    def _check_references(cls: type, value: object, info: ValidationInfo) -> object:
        if problem := reference_problem(value):
            raise ValueError(problem)
        return value

    return field_validator(*fields)(_check_references)


def no_vault_reference_validator(*fields: str) -> Any:
    """A field validator refusing a vault reference in fields that take a value only."""

    def _refuse_references(cls: type, value: object, info: ValidationInfo) -> object:
        if holds_any_reference(value):
            raise ValueError(UNSUPPORTED_REFERENCE)
        return value

    return field_validator(*fields)(_refuse_references)
