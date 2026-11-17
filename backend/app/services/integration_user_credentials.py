# SPDX-License-Identifier: Apache-2.0

"""Each person's own account on a ``per_user`` OAuth integration.

The one module that answers which row holds the credential an integration uses
for a person (:func:`resolve_token_holder`), loads a holder back from its
reference, settles the share choice and writes a person's credential. Contract:
``dev-docs/integration-user-credentials.md``.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decrypt_json, encrypt_value
from app.models.integration import Integration
from app.models.integration_user_credential import IntegrationUserCredential
from app.services.integration_credentials import (
    PER_USER_CREDENTIAL_MODE as CREDENTIAL_MODE_PER_USER,
)
from app.services.integration_credentials import (
    SHARED_CREDENTIAL_MODE as CREDENTIAL_MODE_SHARED,
)
from app.services.integration_credentials import normalize_acting_user_id

logger = logging.getLogger(__name__)

__all__ = ["CREDENTIAL_MODE_PER_USER", "CREDENTIAL_MODE_SHARED"]

#: Prefix of a person's credential in :func:`holder_ref`; an integration's own
#: reference is its bare id, as every refresh callback was keyed before.
USER_CREDENTIAL_REF_PREFIX = "user-credential:"

#: ``credential`` value of an OAuth sign-in state whose tokens are the signing-in
#: person's own credential (``build_authorize_url_for_integration``).
USER_CREDENTIAL_STATE = "user"

#: ``IntegrationUserCredential.status`` values a holder is used with.
USABLE_STATUSES = frozenset({"connected", "error"})


def is_per_user(integration: Any) -> bool:
    """Whether *integration* lets each person connect their own account."""
    return getattr(integration, "credential_mode", None) == CREDENTIAL_MODE_PER_USER


def credential_holder_id(integration: Any) -> uuid.UUID | None:
    """Whose account the integration's own credential is.

    Whoever connected a token an owner transfer kept (``oauth_token_owner_id``),
    else the owner. ``None`` for an owner-less company-managed row:
    in ``per_user`` mode nobody then uses the integration's own credential.
    """
    return normalize_acting_user_id(
        getattr(integration, "oauth_token_owner_id", None)
    ) or normalize_acting_user_id(getattr(integration, "user_id", None))


def holds_integration_credential(integration: Any, acting_user_id: Any) -> bool:
    """Whether *acting_user_id* is the holder of the integration's own credential."""
    acting = normalize_acting_user_id(acting_user_id)
    return acting is not None and acting == credential_holder_id(integration)


def needs_own_credential(integration: Any, acting_user_id: Any) -> bool:
    """Whether *acting_user_id* acts with their own credential on *integration*."""
    return is_per_user(integration) and not holds_integration_credential(
        integration, acting_user_id
    )


def signs_in_own_account(user: Any, integration: Any) -> bool:
    """Whether *user*'s OAuth sign-in on *integration* is for their own credential.

    On a ``per_user`` OAuth integration, everyone but the holder of the
    integration's own credential signs in for themselves. An integration with
    no holder yet (an owner-less company-managed one) is first connected by
    whoever may authorize it (``can_authorize_integration``), who becomes its
    holder, as on a ``shared`` one.
    """
    if not is_per_user(integration) or getattr(integration, "auth_type", None) != "oauth":
        return False
    holder = credential_holder_id(integration)
    if holder is not None:
        return holder != normalize_acting_user_id(getattr(user, "id", None))
    from app.api.v1.helpers import can_authorize_integration

    return not can_authorize_integration(user, integration)


# ---------------------------------------------------------------------------
# Holders
# ---------------------------------------------------------------------------


def is_user_credential(holder: Any) -> bool:
    return bool(getattr(holder, "is_user_credential", False))


def integration_of(holder: Any) -> Any:
    """The integration a holder belongs to (the holder itself for an integration row).

    What a step keyed by the integration's identity reads: the OAuth client
    secret and its vault record, the relay route, the client certificate.
    """
    return holder.integration if is_user_credential(holder) else holder


def holder_ref(holder: Any) -> str:
    """The reference a holder's refresh callbacks, lock and back-off are keyed by."""
    if is_user_credential(holder):
        return f"{USER_CREDENTIAL_REF_PREFIX}{holder.id}"
    return str(holder.id)


def binding_integration_id(holder: Any) -> str:
    """The id of the integration a holder serves, as bindings name it."""
    return str(holder.integration_id if is_user_credential(holder) else holder.id)


async def load_token_holder(db: AsyncSession, ref: str) -> Any:
    """The holder *ref* (:func:`holder_ref`) names, as stored now; ``None`` when gone."""
    if ref.startswith(USER_CREDENTIAL_REF_PREFIX):
        try:
            cid = uuid.UUID(ref.removeprefix(USER_CREDENTIAL_REF_PREFIX))
        except ValueError:
            return None
        return await db.get(IntegrationUserCredential, cid)
    try:
        iid = uuid.UUID(ref)
    except ValueError:
        return None
    return await db.get(Integration, iid)


async def get_user_credential(
    db: AsyncSession, integration_id: Any, user_id: Any
) -> IntegrationUserCredential | None:
    """*user_id*'s own credential on the integration, if they connected one."""
    iid = normalize_acting_user_id(integration_id)
    uid = normalize_acting_user_id(user_id)
    if iid is None or uid is None:
        return None
    result = await db.execute(
        select(IntegrationUserCredential).where(
            IntegrationUserCredential.integration_id == iid,
            IntegrationUserCredential.user_id == uid,
        )
    )
    return result.scalar_one_or_none()


def credential_connected(credential: Any) -> bool:
    """Whether a person's credential holds an account the runs can use.

    The one test: :func:`usable_credential_clause` is its SQL form.
    """
    if credential is None or credential.status not in USABLE_STATUSES:
        return False
    return bool(credential.access_token_enc or credential.refresh_token_enc)


def usable_credential_clause():
    """SQL form of :func:`credential_connected`."""
    from sqlalchemy import and_, or_

    return and_(
        IntegrationUserCredential.status.in_(sorted(USABLE_STATUSES)),
        or_(
            IntegrationUserCredential.access_token_enc.isnot(None),
            IntegrationUserCredential.refresh_token_enc.isnot(None),
        ),
    )


def holder_connected(holder: Any) -> bool:
    """Whether a token holder's account is connected: a person's credential by
    :func:`credential_connected`, an integration row by its status."""
    if is_user_credential(holder):
        return credential_connected(holder)
    return holder is not None and holder.status == "connected"


def connected_for_clause(acting_user_id: Any):
    """SQL: the integration is connected, or *acting_user_id* connected their own
    account on it (``per_user``) - the status a reader acting as them goes by,
    before :func:`resolve_token_holder` picks the holder. Never the holder's
    status alone: the people's own accounts work while it is disconnected."""
    from sqlalchemy import and_, exists, or_

    connected = Integration.status == "connected"
    acting = normalize_acting_user_id(acting_user_id)
    if acting is None:
        return connected
    return or_(
        connected,
        and_(
            Integration.credential_mode == CREDENTIAL_MODE_PER_USER,
            exists().where(
                IntegrationUserCredential.integration_id == Integration.id,
                IntegrationUserCredential.user_id == acting,
                usable_credential_clause(),
            ),
        ),
    )


async def _visible_to(
    db: AsyncSession, integration_id: Any, user_id: uuid.UUID, group_ids: list[str] | None = None
) -> bool:
    """Whether *user_id* sees the integration, three-tier with no admin bypass
    (``visible_integration_ids``): a person's own account is theirs where anyone
    in their place could have one."""
    from app.services.integration_credentials import visible_integration_ids

    return bool(await visible_integration_ids(db, user_id, [integration_id], user_group_ids=group_ids))


async def visible_per_user_integration(db: AsyncSession, integration_id: Any, user_id: Any) -> Integration | None:
    """The ``per_user`` integration *integration_id* if *user_id* sees it (three-tier), else ``None``."""
    iid, uid = normalize_acting_user_id(integration_id), normalize_acting_user_id(user_id)
    if iid is None or uid is None:
        return None
    integration = await db.get(Integration, iid)
    if integration is None or not is_per_user(integration) or not await _visible_to(db, iid, uid):
        return None
    return integration


async def may_connect_own_account(db: AsyncSession, user: Any, integration: Any) -> bool:
    """Whether *user* may connect their own account on *integration*: their
    sign-in is for themselves (:func:`signs_in_own_account`) and they see it,
    three-tier - the rule the callback and the resolver apply too, so nobody
    is offered a connection that would never be used."""
    if not signs_in_own_account(user, integration):
        return False
    uid = normalize_acting_user_id(getattr(user, "id", None))
    return uid is not None and await _visible_to(db, integration.id, uid)


async def resolve_token_holder(
    db: AsyncSession,
    integration: Integration,
    acting_user_id: Any,
    *,
    user_group_ids: list[str] | None = None,
) -> Any:
    """The row holding the credential *integration* uses for *acting_user_id*.

    *user_group_ids*, the acting user's groups when the caller has them,
    saves reading them again.

    ``shared`` mode: the integration (the personal-token rule is applied where
    a person chooses a credential, ``integration_credentials``). ``per_user``
    mode: the integration for its holder; for anyone else who can see it, their
    connected credential; otherwise ``None`` - never the holder's credential
    for someone else, and nothing without an acting person.
    """
    if not is_per_user(integration):
        return integration
    acting = normalize_acting_user_id(acting_user_id)
    if acting is None:
        return None
    if holds_integration_credential(integration, acting):
        return integration
    credential = await get_user_credential(db, integration.id, acting)
    if not credential_connected(credential):
        return None
    if not await _visible_to(db, integration.id, acting, user_group_ids):
        return None
    return credential


def reader_clause(acting_user_id: Any):
    """SQL form for a background reader acting as *acting_user_id*: the rows it
    may read a credential from - the personal-token rule's, or a ``per_user``
    one, whose holder :func:`resolve_token_holder` then picks for that person."""
    from sqlalchemy import or_

    from app.services.integration_credentials import oauth_credentials_usable_clause

    return or_(
        oauth_credentials_usable_clause(acting_user_id),
        Integration.credential_mode == CREDENTIAL_MODE_PER_USER,
    )


async def usable_credential_holder(
    db: AsyncSession, integration: Integration, user_id: Any, *, is_admin: bool = False
) -> Any:
    """The holder the REST tooling acts with for *user_id* (Test, pickers, operations).

    ``shared`` mode: the integration, once the personal-token rule allows it
    (``require_oauth_credentials_usable``, admins included). ``per_user`` mode:
    the caller's own credential (the integration's for its holder), admins
    included; a 403 that says how to connect one otherwise.
    """
    from fastapi import HTTPException

    from app.services.integration_credentials import (
        OWN_ACCOUNT_NOT_CONNECTED_DETAIL,
        require_oauth_credentials_usable,
    )

    if not is_per_user(integration):
        require_oauth_credentials_usable(integration, user_id, is_admin=is_admin)
        return integration
    holder = await resolve_token_holder(db, integration, user_id)
    if holder is None:
        raise HTTPException(status_code=403, detail=OWN_ACCOUNT_NOT_CONNECTED_DETAIL)
    return holder


async def connected_user_ids(db: AsyncSession, integration_ids: Iterable[Any], user_id: Any) -> set[str]:
    """The integrations among *integration_ids* on which *user_id* connected their account."""
    credentials = await user_credentials_by_integration(db, integration_ids, user_id)
    return {iid for iid, credential in credentials.items() if credential_connected(credential)}


async def user_credentials_by_integration(
    db: AsyncSession, integration_ids: Iterable[Any], user_id: Any
) -> dict[str, IntegrationUserCredential]:
    """*user_id*'s credential on each of *integration_ids* that has one."""
    uid = normalize_acting_user_id(user_id)
    ids = [i for i in (normalize_acting_user_id(x) for x in integration_ids) if i is not None]
    if uid is None or not ids:
        return {}
    result = await db.execute(
        select(IntegrationUserCredential).where(
            IntegrationUserCredential.integration_id.in_(ids),
            IntegrationUserCredential.user_id == uid,
        )
    )
    return {str(c.integration_id): c for c in result.scalars().all()}


async def connected_user_counts(db: AsyncSession, integration_ids: Iterable[Any]) -> dict[str, int]:
    """How many people have a usable account of their own on each integration
    (:func:`credential_connected`); the managers' list shows every row."""
    ids = [i for i in (normalize_acting_user_id(x) for x in integration_ids) if i is not None]
    if not ids:
        return {}
    result = await db.execute(
        select(IntegrationUserCredential.integration_id, func.count())
        .where(IntegrationUserCredential.integration_id.in_(ids), usable_credential_clause())
        .group_by(IntegrationUserCredential.integration_id)
    )
    return {str(iid): int(count) for iid, count in result.all()}


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


async def delete_user_credentials(
    db: AsyncSession, integration_id: Any, *, user_ids: Iterable[Any] | None = None
) -> int:
    """Delete the people's credentials on an integration (all, or *user_ids*'). Returns the count."""
    iid = normalize_acting_user_id(integration_id)
    if iid is None:
        return 0
    statement = delete(IntegrationUserCredential).where(IntegrationUserCredential.integration_id == iid)
    if user_ids is not None:
        uids = [u for u in (normalize_acting_user_id(x) for x in user_ids) if u is not None]
        if not uids:
            return 0
        statement = statement.where(IntegrationUserCredential.user_id.in_(uids))
    result = await db.execute(statement)
    return int(result.rowcount or 0)


async def prune_unreachable_user_credentials(db: AsyncSession, integration: Integration) -> int:
    """Delete the credentials of people who can no longer see *integration*.

    Called where its sharing changes. The resolver checks visibility at use as
    well, so a group membership removed elsewhere never lends a stale row.
    """
    from app.services.integration_credentials import users_seeing_integration

    result = await db.execute(
        select(IntegrationUserCredential.user_id).where(
            IntegrationUserCredential.integration_id == integration.id
        )
    )
    user_ids = list(result.scalars().all())
    if not user_ids:
        return 0
    seeing = await users_seeing_integration(db, integration.id, user_ids)
    lost = [uid for uid in user_ids if uid not in seeing]
    if not lost:
        return 0
    return await delete_user_credentials(db, integration.id, user_ids=lost)


async def apply_share_choice(db: AsyncSession, integration: Integration, **consent: Any) -> None:
    """``apply_oauth_share_consent`` (the one write of the choice), then the people's
    own accounts go when the integration lends its account again or stops being
    shared: they authorized them for their own use, not to keep after a share
    that no longer asks for them."""
    from app.services.integration_credentials import apply_oauth_share_consent

    was_per_user = is_per_user(integration)
    apply_oauth_share_consent(integration, **consent)
    if was_per_user and not is_per_user(integration) and integration.id is not None:
        await delete_user_credentials(db, integration.id)


async def upsert_user_credential(
    db: AsyncSession, integration: Integration, user_id: Any
) -> IntegrationUserCredential:
    """*user_id*'s credential row on *integration*, created when missing (not flushed)."""
    uid = normalize_acting_user_id(user_id)
    if uid is None:
        raise ValueError("a person's credential needs a user")
    credential = await get_user_credential(db, integration.id, uid)
    if credential is None:
        credential = IntegrationUserCredential(
            id=uuid.uuid4(), integration_id=integration.id, user_id=uid, integration=integration
        )
        db.add(credential)
    return credential


def store_tokens(credential: IntegrationUserCredential, *, access_token: str, refresh_token: str | None) -> None:
    """Store the tokens a person's own sign-in produced on their credential."""
    credential.access_token_enc = encrypt_value(access_token)
    # A provider may omit the refresh token on re-consent: the stored one is
    # this same person's, so it is kept.
    if refresh_token:
        credential.refresh_token_enc = encrypt_value(refresh_token)
    credential.status = "connected"


# ---------------------------------------------------------------------------
# Account-derived configuration
# ---------------------------------------------------------------------------


def account_config_keys(provider: str | None) -> frozenset[str]:
    """The ``extra_config`` keys a sign-in learns about its account (the Facebook
    pages, the Jira sites...), as the provider's catalog entry declares them."""
    from app.api.v1.integration_catalog import PROVIDER_CATALOG

    return frozenset(PROVIDER_CATALOG.get(provider or "", {}).get("account_config_keys", ()))


def _decrypted(value_enc: str | None) -> dict:
    try:
        extra = decrypt_json(value_enc) if value_enc else None
    except Exception:
        extra = None
    return dict(extra) if isinstance(extra, dict) else {}


def configuration_extra(integration: Any) -> dict:
    """The integration's ``extra_config`` without what its own sign-in learned
    about its account: the base of a person's credential, which never reads
    another account's pages, sites or installations."""
    keys = account_config_keys(getattr(integration, "provider", None))
    return {key: value for key, value in _decrypted(integration.extra_config_enc).items() if key not in keys}


def account_extra(holder: Any) -> dict:
    """The holder's ``extra_config`` (a person's: the integration's configuration with their account's keys)."""
    return _decrypted(holder.extra_config_enc)


def save_account_extra(holder: Any, extra: dict) -> None:
    """Store what a sign-in learned about its account.

    On an integration row, its ``extra_config``. On a person's credential, its
    account's keys, and any other key that differs from the integration's
    configuration, so that configuration is never copied and a later edit of it
    still applies.
    """
    if not is_user_credential(holder):
        holder.extra_config_enc = encrypt_value(json.dumps(extra))
        return
    keys = account_config_keys(holder.provider)
    base = configuration_extra(holder.integration)
    own = {key: value for key, value in extra.items() if key in keys or base.get(key) != value}
    holder.account_config_enc = encrypt_value(json.dumps(own)) if own else None
