"""Secret variables referenced from an integration credential.

An integration's ``api_key`` or ``client_secret`` may hold one or more
``{{var_id.<uuid>}}`` placeholders naming a **secret** variable, such as
``users API-Key {{var_id.<uuid>}}``. The placeholder is stored as written and
resolved only where the credential is used (:func:`app.services.vault.reveal_field`),
the way a ``vault://`` reference is: the value is never written back, never
cached in clear, and remembered in the known-secrets scope so what a run
stores, traces and logs holds the redaction marker instead.

Visibility is the integration owner's own (``user_visible_filter``, never an
admin bypass; company-wide variables only for an integration with no owner):
the person who wired the credential decides which secret it reads, and a
variable that is deleted, no longer secret, empty or no longer visible fails
the use with the generic "secret unavailable" error, recorded on the
integration like a vault failure.

A text with no placeholder is never touched: a plain key behaves as before.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Mapping
from typing import Any, Final, Literal

from app.core.secret_refs import SecretReference
from app.services.vault.errors import SecretUnavailableError
from app.services.vault.failures import SecretOwner, clear_failure, record_failure

#: The secret columns whose text may hold a ``{{var_id.<uuid>}}`` placeholder, by table.
VARIABLE_REFERENCE_COLUMNS: dict[str, tuple[str, ...]] = {
    "integrations": ("api_key_enc", "client_secret_enc"),
}

#: The reason recorded when a referenced variable cannot be read.
VARIABLE_UNAVAILABLE: Final[Literal["variable_unavailable"]] = "variable_unavailable"

#: The provider name a variable failure is recorded under (no vault provider may be named so: ``_`` first).
VARIABLE_PROVIDER = "variable"

# Same spelling as ``services/variable_resolver.VAR_ID_PATTERN`` (not imported: that
# module imports this package).
_VAR_ID_RE = re.compile(
    r"\{\{var_id\.([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\}\}",
    re.IGNORECASE,
)


def variable_ids_in(text: object) -> list[str]:
    """The variable ids *text* references, lowercase, in order, each once; ``[]`` for a non-string."""
    if not isinstance(text, str):
        return []
    seen: dict[str, None] = {}
    for match in _VAR_ID_RE.finditer(text):
        seen.setdefault(match.group(1).lower(), None)
    return list(seen)


def holds_variable_reference(text: object) -> bool:
    """Whether *text* holds at least one ``{{var_id.<uuid>}}`` placeholder."""
    return isinstance(text, str) and _VAR_ID_RE.search(text) is not None


def takes_variable_references(entity: Any, column: str) -> bool:
    """Whether *entity*'s *column* is a credential that may reference a secret variable."""
    return column in VARIABLE_REFERENCE_COLUMNS.get(getattr(entity, "__tablename__", "") or "", ())


def variable_reference(variable_id: str) -> SecretReference:
    """The pseudo-reference a variable failure is fingerprinted with (never shown)."""
    return SecretReference(provider=VARIABLE_PROVIDER, path=variable_id)


def stored_variable_references(entity: Any) -> dict[str, list[str]]:
    """``{field: variable ids}`` for each credential of *entity* holding a placeholder (ids only, never a value)."""
    from app.core.security import decrypt_stored_value

    found: dict[str, list[str]] = {}
    for column in VARIABLE_REFERENCE_COLUMNS.get(getattr(entity, "__tablename__", "") or "", ()):
        ciphertext = getattr(entity, column, None)
        if not ciphertext:
            continue
        try:
            text = decrypt_stored_value(ciphertext)
        except Exception:  # noqa: S112 - an unreadable column holds no reference to show
            continue
        if ids := variable_ids_in(text):
            found[column.removesuffix("_enc")] = ids
    return found


async def _visible_variables(variable_ids: list[str], owner_id: object) -> dict[str, Any]:
    """``{id: Variable}`` for the referenced variables *owner_id* may see, read on a session of its own."""
    from sqlalchemy import select

    from app.api.v1.helpers import company_wide_visible_filter, get_user_group_ids, user_visible_filter
    from app.core.database import async_session
    from app.models.variable import Variable

    uuids = [uuid.UUID(vid) for vid in variable_ids]
    owner_uuid: uuid.UUID | None
    try:
        owner_uuid = owner_id if isinstance(owner_id, uuid.UUID) else (uuid.UUID(str(owner_id)) if owner_id else None)
    except ValueError:
        owner_uuid = None
    async with async_session() as db:
        if owner_uuid is None:
            visible = company_wide_visible_filter(Variable)
        else:
            visible = user_visible_filter(Variable, owner_uuid, await get_user_group_ids(db, owner_uuid))
        rows = (await db.execute(select(Variable).where(Variable.id.in_(uuids), visible))).scalars().all()
    return {str(row.id).lower(): row for row in rows}


async def _secret_value(variable: Any) -> str:
    """A secret variable's value, its own vault reference resolved; ``""`` when it holds none."""
    from app.services.vault import reveal_field

    return (await reveal_field(variable, "value_enc")) or ""


async def resolve_variable_references(text: str, entity: Any, owner: SecretOwner) -> str:
    """*text* with each ``{{var_id.<uuid>}}`` replaced by the secret variable's value, as *entity*'s owner sees it.

    Raises :class:`SecretUnavailableError` (generic message) when a referenced
    variable is missing, not visible to the owner, not secret or empty; the
    failure is recorded on *owner* (the integration page shows it).
    """
    variable_ids = variable_ids_in(text)
    if not variable_ids:
        return text
    from app.core.redaction import remember_secret_value

    variables = await _visible_variables(variable_ids, getattr(entity, "user_id", None))
    values: dict[str, str] = {}
    for variable_id in variable_ids:
        variable = variables.get(variable_id)
        value = ""
        if variable is not None and variable.is_secret and variable.value_enc:
            try:
                value = await _secret_value(variable)
            except SecretUnavailableError:
                value = ""
        if not value:
            await record_failure(owner, VARIABLE_PROVIDER, VARIABLE_UNAVAILABLE, variable_reference(variable_id))
            raise SecretUnavailableError(VARIABLE_UNAVAILABLE)
        remember_secret_value(variable_id, value)
        values[variable_id] = value
    await clear_failure(owner)
    return _VAR_ID_RE.sub(lambda match: values[match.group(1).lower()], text)


async def visible_secret_flags(db: Any, variable_ids: list[str], viewer: object | None) -> dict[str, bool]:
    """``{id: is_secret}`` for the variables among *variable_ids* that *viewer* may see (``None``: company-wide)."""
    from sqlalchemy import select

    from app.api.v1.helpers import company_wide_visible_filter, get_user_group_ids, user_visible_filter
    from app.models.variable import Variable

    try:
        viewer_uuid = viewer if isinstance(viewer, uuid.UUID) else (uuid.UUID(str(viewer)) if viewer else None)
    except ValueError:
        viewer_uuid = None
    if viewer_uuid is None:
        visible = company_wide_visible_filter(Variable)
    else:
        visible = user_visible_filter(Variable, viewer_uuid, await get_user_group_ids(db, viewer_uuid))
    uuids = [uuid.UUID(vid) for vid in variable_ids]
    rows = (await db.execute(select(Variable.id, Variable.is_secret).where(Variable.id.in_(uuids), visible))).all()
    return {str(row[0]).lower(): bool(row[1]) for row in rows}


async def variable_reference_problem(
    db: Any, fields: Mapping[str, object], *, viewer_ids: Iterable[object | None]
) -> str | None:
    """Why a write storing these credential *fields* is refused, or ``None``.

    Each ``{{var_id.<uuid>}}`` must name an existing **secret** variable that
    every one of *viewer_ids* may see (the caller, and the integration's owner
    who resolves it; ``None`` stands for company-wide visibility). The message
    names the field and the variable id, never a value.
    """
    referenced = {name: variable_ids_in(value) for name, value in fields.items()}
    wanted = sorted({vid for ids in referenced.values() for vid in ids})
    if not wanted:
        return None
    visible_to_all: set[str] | None = None
    secret: dict[str, bool] = {}
    for viewer in dict.fromkeys(viewer_ids):
        flags = await visible_secret_flags(db, wanted, viewer)
        secret.update(flags)
        visible_to_all = set(flags) if visible_to_all is None else visible_to_all & set(flags)
    visible_to_all = visible_to_all or set()
    for name, ids in referenced.items():
        for vid in ids:
            if vid not in visible_to_all:
                return f"{name} references the variable {vid}, which does not exist or is not visible to you and to the integration's owner"
            if not secret.get(vid):
                return f"{name} references the variable {vid}, which is not a secret variable: only a secret variable may be referenced in a credential"
    return None
