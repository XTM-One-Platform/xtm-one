# SPDX-License-Identifier: Apache-2.0

"""Who may use an integration's stored OAuth token.

An OAuth integration's ``access_token_enc`` / ``refresh_token_enc`` belong to
whoever connected it. Anyone but the owner needs the owner's consent,
``oauth_share_acknowledged``. Sharing the integration (group, per-user grant or
company-managed) records that consent when its holder chooses to - the UI's
default, after its notice - so everyone it is shared with uses the connected
account; the opt-out keeps it shared without the account - stored as
``credential_mode = "per_user"``: the owner keeps their token, and everyone
else authorizes their own (``integration_user_credentials``) - and a
REST request that makes no choice keeps the baseline
(:func:`requested_share_choice`). :func:`apply_oauth_share_consent` is the one
write rule. Non-OAuth credentials (API keys, bot tokens, platform-managed JWTs)
are shared by design and pass.

The rule has one definition, in two forms that must agree:

* :func:`oauth_credentials_usable_clause` - a SQLAlchemy clause for ``.where()``
* :func:`oauth_credentials_usable` - the same test on an already-loaded row

Both take the ACTING user - the person the token would be used for. Work run
on behalf of an agent (polls, webhooks, the public widget, anonymous channel
users) acts as the agent's owner, :func:`agent_acting_user_id`. With no acting
user (an owner-less company-managed agent) only non-OAuth or acknowledged
integrations are usable.

Every module under ``backend/app`` that reads a stored integration token is
pinned, with the reason it is safe, by ``tests/test_integration_credential_gate.py``.

A credential a KB sync source, a skill source or a user's General Assistant
list was bound to is used by that entity's runs as it stands: nothing is
withheld at run time. An agent's bindings and a flow Integration node are
used only by a run whose person may use them, or that the agent lends
(:func:`resolve_agent_integration_bindings`, :func:`shadow_invisible_bindings`);
an agent binding whose integration the agent's owner can no longer see is
orphaned and used by no run of the agent (:func:`drop_orphaned_bindings`). The
rule applies where
a person chooses: the write that binds a credential, the REST tooling, and a
chat turn's session selection. A refusal is never silent: REST routes answer
403 with ``OAUTH_CREDENTIALS_NOT_SHARED_DETAIL``, which says how the token gets
shared, and a session selection the rule narrows says so
(:func:`resolve_session_integration_bindings` ``refused=``, rendered by
:func:`format_unshared_oauth_prompt_note`).
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterable
from typing import Any

from fastapi import HTTPException
from sqlalchemy import ColumnElement, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import safe_uuid
from app.models.integration import Integration

logger = logging.getLogger(__name__)

#: The opt-out switch in the integration's Configuration card: the
#: integration stays shared without its owner's token
#: (``keep_oauth_credentials_on_share: false``). Off by default.
WITHHOLD_CREDENTIALS_SWITCH = "Don't share my connected account"

#: The 403 detail for a person who acts with their own account on an integration
#: (``credential_mode = "per_user"``) and has not connected it.
OWN_ACCOUNT_NOT_CONNECTED_DETAIL = (
    "Each person connects their own account to this integration, and you have "
    "not connected yours. Connect it from the integration's page."
)

OAUTH_CREDENTIALS_NOT_SHARED_DETAIL = (
    "This integration uses its owner's personal OAuth credentials, which they "
    "have not shared. Its owner or an admin can share them by saving the "
    f"integration's configuration with '{WITHHOLD_CREDENTIALS_SWITCH}' turned "
    "off; otherwise, connect your own integration."
)

#: ``missing_integration_bindings`` reason for a binding refused by the rule.
OAUTH_NOT_SHARED_REASON = "oauth_credentials_not_shared"

#: ``missing_integration_bindings`` reason for a binding a chat turn did not use
#: because its user cannot see the integration at all. Such an entry
#: carries the provider and never the name: the name is not the user's to read.
INTEGRATION_NOT_VISIBLE_REASON = "integration_not_visible"

#: ``missing_integration_bindings`` reason for an integration whose people each
#: connect their own account (``credential_mode = "per_user"``) when the
#: acting person has not connected theirs. The entry carries the name: the
#: person can see the integration, and its page is where they connect.
OWN_ACCOUNT_NOT_CONNECTED_REASON = "own_account_not_connected"

#: ``missing_integration_bindings`` reason for an agent binding whose integration
#: the agent's OWNER can no longer see (a share revoked, the owner removed from
#: the group): the binding is orphaned, and no run of the agent uses it -
#: chats, assignments, the public link, channels - until the owner's access is
#: restored or the binding removed (:func:`drop_orphaned_bindings`).
OWNER_LOST_ACCESS_REASON = "owner_lost_access"

#: ``missing_integration_bindings`` reason for a bound integration whose stored
#: extra_config lacks a key its provider needs for its current settings (catalog
#: ``extra_field_required_when``, #9898): it yields no credential, so no tool.
#: The entry names the missing keys; the turn says so to the model
#: (:func:`format_invalid_integration_prompt_note`) and the owners are told once
#: (``services/integration_invalid_notice.py``), never a silent absence.
INTEGRATION_INVALID_REASON = "integration_invalid"

#: ``missing_integration_bindings`` reason for a bound integration that holds no
#: usable credential for another reason than a missing configuration key: it
#: was never signed in, its token expired and could not be refreshed, or its
#: secret was removed. It yields no tool; the turn says so to the model
#: (:func:`format_invalid_integration_prompt_note`), never a silent absence.
INTEGRATION_NOT_CONNECTED_REASON = "integration_not_connected"

#: ``missing_integration_bindings`` reason for an integration a chat turn's
#: session selection named on a company-managed agent that is not
#: company-managed (a person's own integration): a company-managed agent is the
#: administrators' and uses company-managed integrations only, the rule its
#: bindings follow (``agent_bindings.validate_company_managed_bindings``).
NOT_COMPANY_MANAGED_REASON = "not_company_managed"

#: Why a chat turn does not use an integration for its user: both are said to
#: the user (:func:`unavailable_integrations_notice`) and to the model
#: (:func:`format_unshared_oauth_prompt_note`), never a silent absence.
WITHHELD_REASONS = (
    OAUTH_NOT_SHARED_REASON,
    INTEGRATION_NOT_VISIBLE_REASON,
    OWN_ACCOUNT_NOT_CONNECTED_REASON,
)

#: ``Integration.credential_mode``: the account stored on the integration is
#: lent to the people it is shared with (``SHARED_CREDENTIAL_MODE``), or the
#: integration is shared without it and each person authorizes their own
#: (``PER_USER_CREDENTIAL_MODE``; ``services/integration_user_credentials.py``).
SHARED_CREDENTIAL_MODE = "shared"
PER_USER_CREDENTIAL_MODE = "per_user"

#: Assistant message metadata key and ``done`` frame key: the integrations the
#: turn did not use for its user, and why (:func:`unavailable_integrations_notice`).
UNAVAILABLE_INTEGRATIONS_META_KEY = "unavailable_integrations"

_LABEL_MAX_CHARS = 100

def normalize_acting_user_id(acting_user_id: Any) -> uuid.UUID | None:
    """The acting user as a UUID; ``None`` when anonymous, missing or unparseable."""
    if acting_user_id is None:
        return None
    if isinstance(acting_user_id, uuid.UUID):
        return acting_user_id
    try:
        return uuid.UUID(str(acting_user_id))
    except (ValueError, TypeError, AttributeError):
        return None


def agent_acting_user_id(agent: Any) -> uuid.UUID | None:
    """The identity work run on behalf of *agent* acts as: its owner, if any."""
    return normalize_acting_user_id(getattr(agent, "user_id", None))


def lent_integration_ids(agent: Any) -> set[str]:
    """Canonical ids of the integrations *agent* lends to the people it is
    shared with (``Agent.integration_lending``), restricted to the ones it
    still binds.

    A lent integration is its owner's personal one, used in a member's chat
    with THIS agent only: it is never visible to the member
    (:func:`visible_integration_ids` ignores lending), never bindable by them
    elsewhere, and its credential is never shown. The run-time paths that
    honour it (``integration_tools.shadow_invisible_bindings``,
    :func:`resolve_session_integration_bindings`) also require the row to
    still belong to the agent's owner, so a transferred integration stops
    being lent.
    """
    lending = getattr(agent, "integration_lending", None)
    if not isinstance(lending, dict):
        return set()
    lent = canonical_integration_ids(lending.get("integration_ids") or [])
    return lent & canonical_integration_ids(flatten_binding_ids(getattr(agent, "integration_bindings", None)))


async def shared_agent_personal_integrations(db: AsyncSession, agent: Any) -> list[dict[str, Any]]:
    """The owner's personal integrations a shared *agent* binds that the people
    it is shared with would not get otherwise: ``{id, name, provider}`` each,
    in name order.

    The agent settings page reads them from the agent itself, because an
    editor who is not the owner cannot read the owner's personal rows: the
    banner that says what the people it is shared with get must still show
    them its state. An agent shared with no group and no person lists none.
    Company-managed rows are left out, and so is a row shared with every group
    and person the agent is shared with (it is already theirs); a row only some
    of them hold is listed, since the others still use the owner's.
    """
    groups = {str(g) for g in (getattr(agent, "shared_group_ids", None) or [])}
    people = {str(u) for u in (getattr(agent, "shared_user_ids", None) or [])}
    owner = normalize_acting_user_id(getattr(agent, "user_id", None))
    bound = sorted(canonical_integration_ids(flatten_binding_ids(getattr(agent, "integration_bindings", None))))
    if not (groups or people) or owner is None or not bound:
        return []
    rows = (
        await db.execute(
            select(
                Integration.id,
                Integration.name,
                Integration.provider,
                Integration.shared_group_ids,
                Integration.shared_user_ids,
            ).where(
                Integration.id.in_([uuid.UUID(i) for i in bound]),
                Integration.user_id == owner,
                Integration.is_company_managed.is_(False),
            )
        )
    ).all()
    return sorted(
        (
            {"id": str(row.id), "name": row.name, "provider": row.provider}
            for row in rows
            if not (
                groups <= {str(g) for g in (row.shared_group_ids or [])}
                and people <= {str(u) for u in (row.shared_user_ids or [])}
            )
        ),
        key=lambda item: (item["name"] or "").lower(),
    )


#: 400 detail: a lent integration must be one the agent binds.
LENT_INTEGRATION_NOT_BOUND_DETAIL = (
    "An agent can only lend an integration it is bound to. Bind it first, then lend it."
)

#: 403 detail: only the owner of both the agent and the integration lends it.
LENT_INTEGRATION_NOT_OWNED_DETAIL = (
    "Only the owner of an agent can lend integrations through it, and only their own "
    "personal integrations."
)


async def checked_integration_lending(
    db: AsyncSession,
    *,
    actor_id: Any,
    agent_owner_id: Any,
    integration_ids: Iterable[Any] | None,
    bindings: Any,
    now_iso: str,
) -> dict[str, Any] | None:
    """The ``Agent.integration_lending`` value a request to lend *integration_ids*
    through an agent stores, or a 400 / 403.

    Empty lends nothing (``None``). Every id must be bound by the agent
    (*bindings*), and the caller must own the agent and every integration
    lent: lending is the owner's choice about their own credential, never an
    editor's or an admin's on someone else's behalf.
    """
    wanted = sorted(canonical_integration_ids(integration_ids or []))
    if not wanted:
        return None
    if wanted and not set(wanted) <= canonical_integration_ids(flatten_binding_ids(bindings)):
        raise HTTPException(status_code=400, detail=LENT_INTEGRATION_NOT_BOUND_DETAIL)
    actor = normalize_acting_user_id(actor_id)
    if actor is None or actor != normalize_acting_user_id(agent_owner_id):
        raise HTTPException(status_code=403, detail=LENT_INTEGRATION_NOT_OWNED_DETAIL)
    owned = (
        await db.execute(
            select(Integration.id).where(
                Integration.id.in_([uuid.UUID(i) for i in wanted]),
                Integration.user_id == actor,
            )
        )
    ).scalars().all()
    if {str(i) for i in owned} != set(wanted):
        raise HTTPException(status_code=403, detail=LENT_INTEGRATION_NOT_OWNED_DETAIL)
    return {"integration_ids": wanted, "set_by": str(actor), "set_at": now_iso}


def oauth_credentials_usable_clause(acting_user_id: Any) -> ColumnElement[bool]:
    """SQL form: rows whose stored token *acting_user_id* may use.

    A ``per_user`` integration lends its stored credential - OAuth
    token or API key - to nobody but its owner: anyone else acts with their own
    (``integration_user_credentials.resolve_token_holder``).
    """
    shared_mode = Integration.credential_mode != PER_USER_CREDENTIAL_MODE
    allowed: list[ColumnElement[bool]] = [
        and_(Integration.auth_type != "oauth", shared_mode),
        and_(Integration.oauth_share_acknowledged.is_(True), shared_mode),
    ]
    acting = normalize_acting_user_id(acting_user_id)
    if acting is not None:
        allowed.append(Integration.user_id == acting)
    return or_(*allowed)


def oauth_credentials_usable(integration: Any, acting_user_id: Any) -> bool:
    """Python form of :func:`oauth_credentials_usable_clause` for a loaded row.

    Accepts an ``Integration`` or anything exposing ``auth_type``, ``user_id``
    and ``oauth_share_acknowledged`` (a selected-columns ``Row``), and
    ``credential_mode`` when it may be ``per_user``.
    """
    shared_mode = getattr(integration, "credential_mode", None) != PER_USER_CREDENTIAL_MODE
    if shared_mode and getattr(integration, "auth_type", None) != "oauth":
        return True
    if shared_mode and getattr(integration, "oauth_share_acknowledged", False) is True:
        return True
    acting = normalize_acting_user_id(acting_user_id)
    owner = normalize_acting_user_id(getattr(integration, "user_id", None))
    return acting is not None and owner == acting


def require_oauth_credentials_usable(
    integration: Any, acting_user_id: Any, *, is_admin: bool = False
) -> None:
    """Refuse with a 403 when *acting_user_id* may not use the stored token.

    *is_admin* keeps the admin tooling (connectivity test, tool test,
    elements, webhooks, operations, binding) working: an
    admin already sees and manages every integration. Not on a ``per_user``
    integration: its stored account is its holder's only, and an admin
    acts with their own like anyone else
    (``integration_user_credentials.usable_credential_holder``).
    """
    if is_admin and getattr(integration, "credential_mode", None) != PER_USER_CREDENTIAL_MODE:
        return
    if not oauth_credentials_usable(integration, acting_user_id):
        raise HTTPException(status_code=403, detail=OAUTH_CREDENTIALS_NOT_SHARED_DETAIL)


def is_shared_integration(integration: Any) -> bool:
    """Whether *integration* is shared: company-managed, or granted to a group or a user."""
    return bool(
        getattr(integration, "is_company_managed", False)
        or getattr(integration, "shared_group_ids", None)
        or getattr(integration, "shared_user_ids", None)
    )


SHARE_CONSENT_REFUSED_DETAIL = (
    "Only this integration's owner, the person whose account it is connected "
    "with, or an admin can share its connected account with the people it is "
    "shared with. Ask one of them to save the integration, or connect your own "
    "account."
)


class ShareConsentRefusedError(Exception):
    """An editor asked to share a connected account that is not theirs to share."""

    def __init__(self) -> None:
        super().__init__(SHARE_CONSENT_REFUSED_DETAIL)


def may_consent_to_share(integration: Any, actor_id: Any, *, is_admin: bool) -> bool:
    """Whether *actor_id* may consent to sharing *integration*'s connected account.

    ``oauth_share_acknowledged`` stands for the token owner's consent, so it is
    theirs to give: the integration's owner, the person the stored token was
    issued to (``oauth_token_owner_id``), or an admin. A ``config``
    grantee edits the integration and may widen who can see it, but never hands
    someone else's personal token to that audience.
    """
    if is_admin:
        return True
    actor = normalize_acting_user_id(actor_id)
    if actor is None:
        return False
    return actor in {
        normalize_acting_user_id(getattr(integration, "user_id", None)),
        normalize_acting_user_id(getattr(integration, "oauth_token_owner_id", None)),
    }


def apply_oauth_share_consent(
    integration: Any,
    *,
    share_credentials: bool | None,
    sharing_changed: bool,
    may_consent: bool = True,
    implicit_consent: bool = True,
) -> None:
    """Settle whether a shared OAuth integration shares its connected account.

    Once an OAuth integration is shared (group, per-user grant, company-managed),
    everyone it is shared with acts using the account it is connected with, so
    sharing it records the owner's consent (``oauth_share_acknowledged``).

    *share_credentials* is the request's explicit choice, ``None`` when it made
    none. ``False`` is the opt-out: the integration stays shared, the
    owner's own account stays theirs and is never lent - re-authorizing it
    does not lend it either (a token a previous owner left, is
    removed) - and everyone else it is shared with authorizes their own
    account on it (``credential_mode = "per_user"``,
    ``integration_user_credentials``). ``True`` lends it again
    (``credential_mode = "shared"``); the caller then deletes the people's own
    accounts (``integration_user_credentials.apply_share_choice``).
    *sharing_changed* says the request shared the integration or changed how
    it is shared.

    An edit that does neither (a rename) acknowledges a shared row still holding
    a token nobody consented for, and never removes it. A row without a token
    keeps its state: it has nothing to share, and its owner may have opted out.
    An integration that is not shared, or not OAuth, is personal again.

    *may_consent* (:func:`may_consent_to_share`) is False for an editor who is
    not the account holder. Their write never turns the consent on: an
    explicit ``share_credentials=True`` that would is refused
    (:class:`ShareConsentRefusedError`), an implicit one (a share, an edit of
    a shared row holding a token) saves with the consent left off. A consent
    already given, the opt-out and un-sharing apply as for the owner.

    *implicit_consent* False (the REST routes): only an
    explicit ``share_credentials=True`` records the consent; ``None`` leaves
    it as it is. The caller maps an omitted choice on a request that shares
    an account not yet consented to ``False``, the opt-out. An implicit
    consent never ends an opt-out: only an explicit choice does.
    """
    if getattr(integration, "auth_type", None) != "oauth" or not is_shared_integration(integration):
        integration.oauth_share_acknowledged = False
        integration.credential_mode = SHARED_CREDENTIAL_MODE
        return
    if share_credentials is False:
        integration.oauth_share_acknowledged = False
        integration.credential_mode = PER_USER_CREDENTIAL_MODE
        # The owner's own account stays theirs. One kept from a previous owner
        # is nobody's to keep: it goes, as every opt-out's did.
        drop_previous_owner_token(integration)
        return
    if integration.oauth_share_acknowledged is True:
        return
    if not may_consent:
        if share_credentials:
            raise ShareConsentRefusedError()
        return
    opted_out = getattr(integration, "credential_mode", None) == PER_USER_CREDENTIAL_MODE
    if share_credentials or (
        implicit_consent
        and not opted_out
        and (sharing_changed or getattr(integration, "access_token_enc", None))
    ):
        integration.oauth_share_acknowledged = True
        integration.credential_mode = SHARED_CREDENTIAL_MODE


def drop_previous_owner_token(integration: Any) -> bool:
    """Remove a token a previous owner left from an integration shared
    without its connected account. Returns whether one was removed.

    In that mode the stored account is its owner's only; one connected by
    somebody else is nobody's to use, and keeping it would leave the new owner
    unable to connect their own.
    """
    if not getattr(integration, "oauth_token_owner_id", None):
        return False
    integration.access_token_enc = None
    integration.refresh_token_enc = None
    integration.token_expires_at = None
    integration.oauth_token_owner_id = None
    integration.status = "configured" if getattr(integration, "client_id_enc", None) else "disconnected"
    return True


def requested_share_choice(
    choice: bool | None, integration: Any, *, sharing_changed: bool
) -> bool | None:
    """What a REST request's ``keep_oauth_credentials_on_share`` means.

    An explicit value is the choice. Omitted (``None``), the baseline meaning
    holds: a request that shares an account nobody consented for opts out (the
    account stays its holder's, everyone else authorizes their own),
    and any other request leaves the consent as it is. The REST routes and the sharing preview both read it here,
    and pass the result to :func:`apply_oauth_share_consent` with
    ``implicit_consent=False``.
    """
    if choice is not None:
        return bool(choice)
    if sharing_changed and not getattr(integration, "oauth_share_acknowledged", False):
        return False
    return None


def _holds_oauth_token(integration: Any) -> bool:
    return getattr(integration, "auth_type", None) == "oauth" and bool(
        getattr(integration, "access_token_enc", None)
        or getattr(integration, "refresh_token_enc", None)
    )


def record_token_owner_on_transfer(integration: Any, previous_owner_id: Any) -> bool:
    """Keep track of whose OAuth token an integration holds when its owner changes.

    An owner transfer changes ``user_id`` and keeps the stored token, which was
    connected by someone else: the previous owner, or whoever connected it
    before them. ``oauth_token_owner_id`` records that person; it is cleared
    when the token turns out to be the new owner's own (a transfer back) and on
    a row holding no OAuth token. Nothing is refused: the integration keeps
    working as before, and the integration page tells its owner the connected
    account is not theirs (:func:`oauth_token_owner_differs`).

    Returns whether the stored token is now someone else's than the owner's.
    """
    if not _holds_oauth_token(integration):
        integration.oauth_token_owner_id = None
        return False
    token_owner = normalize_acting_user_id(
        getattr(integration, "oauth_token_owner_id", None)
    ) or normalize_acting_user_id(previous_owner_id)
    owner = normalize_acting_user_id(getattr(integration, "user_id", None))
    integration.oauth_token_owner_id = None if token_owner == owner else token_owner
    return integration.oauth_token_owner_id is not None


def oauth_token_owner_differs(integration: Any) -> bool:
    """Whether the stored OAuth token was connected by someone else than the owner."""
    token_owner = normalize_acting_user_id(getattr(integration, "oauth_token_owner_id", None))
    if token_owner is None or not _holds_oauth_token(integration):
        return False
    return token_owner != normalize_acting_user_id(getattr(integration, "user_id", None))


def refused_binding(
    integration: Any, *, binding_key: str | None = None
) -> dict[str, str]:
    """The ``missing_integration_bindings`` entry for a binding the rule refused."""
    entry = {
        "binding_key": binding_key or integration.provider,
        "provider": integration.provider,
        "integration_id": str(integration.id),
        "reason": OAUTH_NOT_SHARED_REASON,
    }
    name = (getattr(integration, "name", None) or "").strip()
    if name:
        entry["integration_name"] = name
    return entry


def own_account_not_connected_binding(
    integration: Any, *, binding_key: str | None = None
) -> dict[str, str]:
    """The ``missing_integration_bindings`` entry for a ``per_user`` integration
    on which the acting person has not connected their own account."""
    entry = refused_binding(integration, binding_key=binding_key)
    entry["reason"] = OWN_ACCOUNT_NOT_CONNECTED_REASON
    return entry


def invalid_config_binding(
    integration: Any, missing: Iterable[str], *, binding_key: str | None = None
) -> dict[str, Any]:
    """The ``missing_integration_bindings`` entry for a bound integration its
    stored configuration leaves without a credential (:data:`INTEGRATION_INVALID_REASON`)."""
    entry: dict[str, Any] = refused_binding(integration, binding_key=binding_key)
    entry["reason"] = INTEGRATION_INVALID_REASON
    entry["missing_keys"] = sorted({str(key) for key in missing if key})
    return entry


def not_connected_binding(integration: Any, *, binding_key: str | None = None) -> dict[str, Any]:
    """The ``missing_integration_bindings`` entry for a bound integration that
    yielded no credential while its configuration is complete
    (:data:`INTEGRATION_NOT_CONNECTED_REASON`)."""
    entry: dict[str, Any] = refused_binding(integration, binding_key=binding_key)
    entry["reason"] = INTEGRATION_NOT_CONNECTED_REASON
    return entry


def format_invalid_integration_prompt_note(missing_bindings: Iterable[Any] | None) -> str:
    """Tool Availability note for a turn that lost integrations to an invalid
    configuration (each integration by its label with the keys it lacks) or to
    a missing credential (each integration by its label, not connected).

    Empty unless one was lost, so a healthy turn carries no text (the contract
    of :func:`format_unshared_oauth_prompt_note`). Labels and keys are
    ``json.dumps``-escaped so a name cannot add prompt lines.
    """
    lines: list[str] = []
    seen: set[str] = set()
    unconnected: list[str] = []
    for entry in missing_bindings or []:
        if isinstance(entry, dict) and entry.get("reason") == INTEGRATION_NOT_CONNECTED_REASON:
            label = json.dumps(refused_binding_label(entry))
            if label not in unconnected:
                unconnected.append(label)
            continue
        if not isinstance(entry, dict) or entry.get("reason") != INTEGRATION_INVALID_REASON:
            continue
        label = refused_binding_label(entry)
        keys = [
            refused_binding_label({"provider": key})
            for key in entry.get("missing_keys") or []
            if isinstance(key, str) and key
        ]
        line = f"{json.dumps(label)} (integration invalid: {', '.join(json.dumps(k) for k in keys) or 'configuration'} missing)"
        if line in seen:
            continue
        seen.add(line)
        lines.append(line)
    note = ""
    if lines:
        note += (
            "\n\n**Tool Availability Notice**: The integration(s) "
            + ", ".join(lines)
            + " are bound but yield no tools on this turn: their stored configuration "
            "lacks a key their provider needs. If the request needs them, say so: the "
            "integration's owner sets the key on the integration's page (or with "
            "update_integration). Their owners have been notified."
        )
    if unconnected:
        note += (
            "\n\n**Tool Availability Notice**: The integration(s) "
            + ", ".join(unconnected)
            + " are bound but yield no tools on this turn: they hold no usable "
            "credential (never signed in, or the sign-in expired or was revoked). "
            "If the request needs them, say so: the integration's owner connects it "
            "again from the integration's page."
        )
    return note


def unshared_oauth_bindings(missing_bindings: Iterable[Any] | None) -> list[dict]:
    """The entries of *missing_bindings* the rule refused."""
    return [
        entry
        for entry in missing_bindings or []
        if isinstance(entry, dict) and entry.get("reason") == OAUTH_NOT_SHARED_REASON
    ]


def refused_binding_label(entry: dict) -> str:
    """One-line label for a refused binding: the integration's name, else its provider.

    Names are user-controlled, so control characters are flattened and the
    label is bounded before it reaches a notice or a prompt.
    """
    raw = str(entry.get("integration_name") or entry.get("provider") or "unknown")
    cleaned = " ".join(
        "".join(ch if ch.isprintable() else " " for ch in raw).split()
    )
    return cleaned[:_LABEL_MAX_CHARS] or "unknown"


def format_unshared_oauth_prompt_note(missing_bindings: Iterable[Any] | None) -> str:
    """Tool Availability note for a chat turn that lost integrations to the rule.

    Empty unless a binding was withheld, so a healthy turn carries no text (the
    same contract as ``format_mcp_unavailable_prompt``); without it the model
    cannot tell a withheld integration from one it never had. A binding whose
    integration the user cannot see (:data:`INTEGRATION_NOT_VISIBLE_REASON`) is
    named by its provider only. Labels are ``json.dumps``-escaped so a name
    cannot add prompt lines.
    """
    entries = [entry for entry in missing_bindings or [] if isinstance(entry, dict)]
    labels = sorted({refused_binding_label(entry) for entry in unshared_oauth_bindings(entries)})
    hidden = sorted(
        {
            refused_binding_label({"provider": entry.get("provider")})
            for entry in entries
            if entry.get("reason") == INTEGRATION_NOT_VISIBLE_REASON
        }
    )
    own = sorted(
        {
            refused_binding_label(entry)
            for entry in entries
            if entry.get("reason") == OWN_ACCOUNT_NOT_CONNECTED_REASON
        }
    )
    note = ""
    if labels:
        names = ", ".join(json.dumps(label) for label in labels)
        note += (
            f"\n\n**Tool Availability Notice**: The integration(s) {names} are not "
            "available on this turn: they use their owner's personal OAuth "
            "credentials, which the owner has not shared. If the request needs "
            "them, say so: the integration's owner or an admin can share "
            f"them ('{WITHHOLD_CREDENTIALS_SWITCH}' off), or the user can connect their own."
        )
    if hidden:
        providers = ", ".join(json.dumps(label) for label in hidden)
        note += (
            f"\n\n**Tool Availability Notice**: The {providers} integration(s) of "
            "this conversation are not available on this turn: they are not "
            "shared with the user. If the request needs them, say so: the user "
            "can connect their own, or ask the integration's owner to share it."
        )
    if own:
        names = ", ".join(json.dumps(label) for label in own)
        note += (
            f"\n\n**Tool Availability Notice**: The integration(s) {names} are not "
            "available on this turn: each person connects their own account to "
            "them, and the user has not connected theirs. If the request needs "
            "them, say so: the user can connect their account from the "
            "integration's page."
        )
    return note + format_orphaned_binding_prompt_note(entries)


def format_orphaned_binding_prompt_note(missing_bindings: Iterable[Any] | None) -> str:
    """Tool Availability note for the bindings :func:`drop_orphaned_bindings`
    withheld: empty unless one was, providers ``json.dumps``-escaped."""
    providers = sorted(
        {
            refused_binding_label({"provider": entry.get("provider")})
            for entry in missing_bindings or []
            if isinstance(entry, dict) and entry.get("reason") == OWNER_LOST_ACCESS_REASON
        }
    )
    if not providers:
        return ""
    names = ", ".join(json.dumps(p) for p in providers)
    return (
        f"\n\n**Tool Availability Notice**: The {names} integration(s) bound to "
        "this agent are not available: the agent's owner no longer has access "
        "to them. If the request needs them, say so: the integration's owner "
        "can share it with the agent's owner again, or the binding is removed."
    )


def invisible_binding(
    provider: Any, integration_id: Any, *, binding_key: str | None = None
) -> dict[str, str]:
    """The ``missing_integration_bindings`` entry for a binding whose
    integration its user cannot see: the provider, never the name."""
    return {
        "binding_key": binding_key or str(provider),
        "provider": str(provider),
        "integration_id": str(integration_id),
        "reason": INTEGRATION_NOT_VISIBLE_REASON,
    }


async def invisible_integration_bindings(
    db: AsyncSession, user_id: Any, integration_ids: Iterable[Any]
) -> list[dict[str, str]]:
    """:func:`invisible_binding` entries, in the order given, for the
    integrations among *integration_ids* that exist and that *user_id* cannot
    see (:func:`visible_integration_ids`): a chat turn never uses them for that
    user, and says so. An id naming no integration withholds
    nothing and is not reported."""
    acting = normalize_acting_user_id(user_id)
    parsed = list(
        dict.fromkeys(
            p for p in (safe_uuid(str(i)) for i in integration_ids or [] if i) if p is not None
        )
    )
    if acting is None or not parsed:
        return []
    visible = await visible_integration_ids(db, acting, parsed)
    hidden = [p for p in parsed if str(p) not in visible]
    if not hidden:
        return []
    result = await db.execute(
        select(Integration.id, Integration.provider).where(Integration.id.in_(hidden))
    )
    provider_by_id = {str(row.id): row.provider for row in result.all()}
    return [
        invisible_binding(provider_by_id[str(p)], p) for p in hidden if str(p) in provider_by_id
    ]


def unavailable_integrations_notice(
    missing_bindings: Iterable[Any] | None,
) -> list[dict[str, str]]:
    """What a reply tells its user about the integrations its turn did not use
    for them (:data:`UNAVAILABLE_INTEGRATIONS_META_KEY`): per integration,
    once, its provider, the reason (:data:`WITHHELD_REASONS`) and - only for one
    they can see - its name. Entries withheld for any other reason are left to
    their own notices."""
    notice: list[dict[str, str]] = []
    seen: set[Any] = set()
    for entry in missing_bindings or []:
        if not isinstance(entry, dict) or entry.get("reason") not in WITHHELD_REASONS:
            continue
        key = entry.get("integration_id") or (entry.get("provider"), entry.get("reason"))
        if key in seen:
            continue
        seen.add(key)
        item = {
            "provider": refused_binding_label({"provider": entry.get("provider")}),
            "reason": str(entry["reason"]),
        }
        if entry["reason"] in (OAUTH_NOT_SHARED_REASON, OWN_ACCOUNT_NOT_CONNECTED_REASON) and str(
            entry.get("integration_name") or ""
        ).strip():
            item["name"] = refused_binding_label(entry)
        # The page where the person connects their own account.
        if entry["reason"] == OWN_ACCOUNT_NOT_CONNECTED_REASON and entry.get("integration_id"):
            item["integration_id"] = str(entry["integration_id"])
        notice.append(item)
    return notice


#: Why a run does not carry an integration, as ``load_tools`` says it when a
#: tool name it is asked for resolves to nothing (#11866): the model then
#: hears the reason instead of "Unknown tool name(s) ignored".
_UNAVAILABLE_REASON_TEXT = {
    OAUTH_NOT_SHARED_REASON: "it uses its owner's personal OAuth credentials, which are not shared",
    INTEGRATION_NOT_VISIBLE_REASON: "the acting user cannot see it",
    OWN_ACCOUNT_NOT_CONNECTED_REASON: "the acting user has not connected their own account to it",
    OWNER_LOST_ACCESS_REASON: "the agent's owner no longer has access to it",
    INTEGRATION_INVALID_REASON: "its stored configuration lacks a key its provider needs",
    INTEGRATION_NOT_CONNECTED_REASON: "it holds no usable credential (never signed in, or the sign-in expired or was revoked)",
    NOT_COMPANY_MANAGED_REASON: (
        "it is a personal integration and this agent is company-managed (company-managed integrations only)"
    ),
}


def unavailable_integration_reasons(missing_bindings: Iterable[Any] | None) -> dict[str, str]:
    """Label -> reason for each integration a run left out (``missing_integration_bindings``).

    The input of ``CompositeToolExecutor.withhold_integrations``: the tool gate
    names them when a ``load_tools`` request matches no tool, so a tool an
    earlier turn had is explained, never "unknown". A binding the user cannot
    see is labelled by its provider only (the name is not theirs to read);
    an entry with an unknown reason is left out.
    """
    reasons: dict[str, str] = {}
    for entry in missing_bindings or []:
        if not isinstance(entry, dict):
            continue
        text = _UNAVAILABLE_REASON_TEXT.get(str(entry.get("reason") or ""))
        if text is None:
            continue
        if entry.get("reason") in (INTEGRATION_NOT_VISIBLE_REASON, OWNER_LOST_ACCESS_REASON):
            label = refused_binding_label({"provider": entry.get("provider")})
        else:
            label = refused_binding_label(entry)
        reasons.setdefault(label, text)
    return reasons


def clean_unavailable_integrations(items: Any) -> list[dict[str, str]]:
    """*items* as a reply stores and serves them: the well-formed
    :func:`unavailable_integrations_notice` items, each once (a nested consult
    or transfer turn reports into the same list), anything else dropped."""
    cleaned: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or item.get("reason") not in WITHHELD_REASONS:
            continue
        provider = item.get("provider")
        if not isinstance(provider, str) or not provider:
            continue
        name = item.get("name")
        name = name if isinstance(name, str) and name else ""
        key = (provider, str(item["reason"]), name)
        if key in seen:
            continue
        seen.add(key)
        clean = {"provider": provider, "reason": str(item["reason"])}
        if name:
            clean["name"] = name
        integration_id = safe_uuid(str(item.get("integration_id") or ""))
        if integration_id is not None and item["reason"] == OWN_ACCOUNT_NOT_CONNECTED_REASON:
            clean["integration_id"] = str(integration_id)
        cleaned.append(clean)
    return cleaned


def _flatten_session_ids(session_integration_ids: Iterable[Any]) -> list[str]:
    flat: list[str] = []
    for item in session_integration_ids or []:
        if isinstance(item, str):
            if item:
                flat.append(item)
        elif isinstance(item, (list, tuple)):
            for sub in item:
                if isinstance(sub, str) and sub:
                    flat.append(sub)
                elif isinstance(sub, (list, tuple)):
                    flat.extend(s for s in sub if isinstance(s, str) and s)
    return flat


def flatten_binding_ids(bindings: Any) -> list[str]:
    """Every id of a provider -> ids bindings map, in order, as stored."""
    ids: list[str] = []
    if not isinstance(bindings, dict):
        return ids
    for value in bindings.values():
        items = value if isinstance(value, (list, tuple)) else [value]
        ids.extend(item for item in items if isinstance(item, str) and item)
    return ids


async def visible_integration_ids(
    db: AsyncSession,
    user_id: Any,
    integration_ids: Iterable[Any],
    *,
    user_group_ids: list[str] | None = None,
) -> set[str]:
    """Canonical ids among *integration_ids* naming an integration *user_id* can see.

    The three-tier rule (own, user grant, group share, company-managed not
    ``access_restricted``) with no admin bypass: the integration list a user
    picks from is built the same way, so an admin is offered, and may bind, what
    any user in their place would be. *user_group_ids*, when the caller has
    them, saves reading them again.
    """
    from app.api.v1.helpers import get_user_group_ids, user_visible_filter

    acting = normalize_acting_user_id(user_id)
    parsed = {p for p in (safe_uuid(str(i)) for i in integration_ids if i) if p is not None}
    if acting is None or not parsed:
        return set()
    group_ids = user_group_ids if user_group_ids is not None else await get_user_group_ids(db, acting)
    result = await db.execute(
        select(Integration).where(
            Integration.id.in_(parsed),
            user_visible_filter(Integration, acting, group_ids),
        )
    )
    return {str(integration.id) for integration in result.scalars().all()}


#: People per statement in :func:`users_seeing_integration`.
_VISIBILITY_BATCH = 200


async def users_seeing_integration(
    db: AsyncSession, integration_id: Any, user_ids: Iterable[Any]
) -> set[uuid.UUID]:
    """The people among *user_ids* who see the integration, by the rule of
    :func:`visible_integration_ids`: their groups in one read, the answer in one
    query (one ``user_visible_filter`` arm per person)."""
    from sqlalchemy import cast, literal, union_all
    from sqlalchemy.dialects.postgresql import UUID as PG_UUID

    from app.api.v1.helpers import user_visible_filter
    from app.models.group import GroupMember

    iid = normalize_acting_user_id(integration_id)
    uids = list(dict.fromkeys(u for u in (normalize_acting_user_id(x) for x in user_ids) if u is not None))
    if iid is None or not uids:
        return set()
    groups: dict[uuid.UUID, list[str]] = {uid: [] for uid in uids}
    memberships = await db.execute(
        select(GroupMember.user_id, GroupMember.group_id).where(GroupMember.user_id.in_(uids))
    )
    for uid, gid in memberships.all():
        groups[uid].append(str(gid))
    seeing: set[uuid.UUID] = set()
    # Bounded statements: each arm binds the person and their groups. One
    # union_all even for a single arm, so the statement stays a plain read.
    for start in range(0, len(uids), _VISIBILITY_BATCH):
        arms = [
            select(cast(literal(str(uid)), PG_UUID(as_uuid=True)).label("user_id")).where(
                Integration.id == iid, user_visible_filter(Integration, uid, groups[uid])
            )
            for uid in uids[start : start + _VISIBILITY_BATCH]
        ]
        result = await db.execute(union_all(*arms))
        seeing.update(result.scalars().all())
    return seeing


async def orphaned_binding_ids(
    db: AsyncSession,
    owner_id: Any,
    integration_ids: Iterable[Any],
    *,
    owner_group_ids: list[str] | None = None,
) -> set[str]:
    """Canonical ids among *integration_ids* that exist and that *owner_id*
    can no longer see (:func:`visible_integration_ids`): an agent of that owner
    bound to one holds an orphaned binding. No owner (an owner-less
    company-managed agent) orphans nothing: its bindings are judged where they
    are written."""
    owner = normalize_acting_user_id(owner_id)
    wanted = canonical_integration_ids(integration_ids)
    if owner is None or not wanted:
        return set()
    visible = await visible_integration_ids(db, owner, wanted, user_group_ids=owner_group_ids)
    hidden = wanted - visible
    if not hidden:
        return set()
    existing = await db.execute(
        select(Integration.id).where(Integration.id.in_([uuid.UUID(i) for i in hidden]))
    )
    return {str(i) for i in existing.scalars().all()}


async def agent_orphaned_integrations(db: AsyncSession, agent: Any) -> list[dict[str, Any]]:
    """The integrations *agent* binds that its owner can no longer see
    (:func:`orphaned_binding_ids`), ``{id, name, provider}`` each in name
    order: the agent page flags them, since its runs refuse their tools. A
    company-managed agent orphans nothing."""
    if getattr(agent, "is_company_managed", False):
        return []
    ids = await orphaned_binding_ids(
        db, getattr(agent, "user_id", None), flatten_binding_ids(getattr(agent, "integration_bindings", None))
    )
    if not ids:
        return []
    rows = (
        await db.execute(
            select(Integration.id, Integration.name, Integration.provider).where(
                Integration.id.in_([uuid.UUID(i) for i in ids])
            )
        )
    ).all()
    return sorted(
        ({"id": str(row.id), "name": row.name, "provider": row.provider} for row in rows),
        key=lambda item: (item["name"] or "").lower(),
    )


async def drop_orphaned_bindings(
    db: AsyncSession,
    agent: Any,
    effective_bindings: dict[str, str],
    binding_origin_map: dict[str, str] | None = None,
    *,
    withheld: list[dict] | None = None,
    origins: Iterable[str] | None = None,
) -> list[str]:
    """Drop from *effective_bindings* (flattened, mutated in place with
    *binding_origin_map*) every binding whose integration *agent*'s owner can
    no longer see, whoever the run acts for: the owner bound it, the owner's
    access is gone, so the agent stops lending it (:data:`OWNER_LOST_ACCESS_REASON`).

    *origins*, when given with *binding_origin_map*, limits the judgement to
    the keys whose origin is one of them: only the agent's own bindings are
    the owner's; an assignment override (written by whoever may edit the
    assignment, an admin included) or a per-user template pick is judged where
    it is written, never here.

    *withheld*, when given, receives one ``missing_integration_bindings``
    entry per dropped binding (provider and id, never the name), so the run
    says what it did not use. Returns the dropped binding keys, sorted.
    """
    # A company-managed agent keeps its creator as owner for audit only: its
    # bindings are the company's, judged where they are written.
    if not effective_bindings or getattr(agent, "is_company_managed", False):
        return []
    judged = dict(effective_bindings)
    if origins is not None and binding_origin_map is not None:
        allowed = set(origins)
        judged = {k: v for k, v in judged.items() if binding_origin_map.get(k) in allowed}
    if not judged:
        return []
    orphaned = await orphaned_binding_ids(db, getattr(agent, "user_id", None), judged.values())
    if not orphaned:
        return []
    dropped: list[str] = []
    for key, integration_id in list(judged.items()):
        canonical = canonical_integration_ids([integration_id])
        if not canonical or not canonical <= orphaned:
            continue
        effective_bindings.pop(key, None)
        if binding_origin_map is not None:
            binding_origin_map.pop(key, None)
        dropped.append(key)
        if withheld is not None:
            withheld.append(
                {
                    "binding_key": key,
                    "provider": key.split("#", 1)[0],
                    "integration_id": next(iter(canonical)),
                    "reason": OWNER_LOST_ACCESS_REASON,
                }
            )
    return sorted(dropped)


async def resolve_session_integration_bindings(
    db: AsyncSession,
    user_id: Any,
    session_integration_ids: Iterable[Any],
    *,
    refused: list[dict[str, str]] | None = None,
    lent_ids: set[str] | None = None,
    lent_owner_id: Any = None,
    company_managed_only: bool = False,
) -> dict[str, list[str]]:
    """Provider -> integration ids for a chat turn's ``session_integration_ids``.

    The chat UI sends the full set of integrations enabled for the message, and
    it replaces the configured bindings. Only connected integrations the
    chatting user can see AND whose stored token they may use are kept, so a
    crafted payload can neither reach a colleague's private integration nor
    borrow a shared one's personal OAuth token.

    *refused*, when given, receives a ``missing_integration_bindings`` entry
    for each requested integration the user can see but whose token they may
    not use, so the turn can say so instead of silently lacking the tools.

    *lent_ids* and *lent_owner_id* (the chatted agent's
    :func:`lent_integration_ids` and owner) keep a requested integration the
    agent lends: still its owner's, not ``per_user``, used for this turn of
    this agent only.

    *company_managed_only* (the chatted agent is company-managed) keeps
    company-managed integrations only, the rule the agent's bindings follow
    at write time: a person's own integration never reaches a company-managed
    agent through the session selection, and nothing is lent (the agent's
    creator is its owner for audit only). Each one left out is told in
    *refused* (:data:`NOT_COMPANY_MANAGED_REASON`).
    """
    from app.api.v1.helpers import get_user_group_ids, user_visible_filter

    acting = normalize_acting_user_id(user_id)
    if acting is None:
        return {}
    session_uuids = [
        parsed
        for raw in _flatten_session_ids(session_integration_ids)
        for parsed in [safe_uuid(raw)]
        if parsed is not None
    ]
    if not session_uuids:
        return {}
    group_ids = await get_user_group_ids(db, acting)
    from app.services.integration_user_credentials import connected_for_clause

    visible = [
        connected_for_clause(acting),
        user_visible_filter(Integration, acting, group_ids),
    ]
    not_company: list[str] = []
    if company_managed_only:
        personal = (
            (
                await db.execute(
                    select(Integration).where(
                        Integration.id.in_(session_uuids),
                        Integration.is_company_managed.isnot(True),
                        *visible,
                    )
                )
            )
            .scalars()
            .all()
        )
        for integration in personal:
            not_company.append(str(integration.id))
            if refused is not None:
                entry = refused_binding(integration)
                entry["reason"] = NOT_COMPANY_MANAGED_REASON
                refused.append(entry)
        # access_restricted: honoured - user_visible_filter in *visible* keeps a restricted row to its grantees.
        visible.append(Integration.is_company_managed.is_(True))
        lent_ids = None
    result = await db.execute(
        select(Integration).where(
            Integration.id.in_(session_uuids),
            *visible,
            oauth_credentials_usable_clause(acting),
        )
    )
    bindings: dict[str, list[str]] = {}
    kept: set[str] = set()
    for integration in result.scalars().all():
        bindings.setdefault(integration.provider, []).append(str(integration.id))
        kept.add(str(integration.id))
    dropped = list(dict.fromkeys(u for u in session_uuids if str(u) not in kept and str(u) not in not_company))
    logger.info(
        "Session integrations resolved: acting=%s requested=%d kept=%s "
        "not_company_managed=%s other_drops=%s company_managed_only=%s",
        acting,
        len(session_uuids),
        sorted(kept),
        not_company,
        [str(u) for u in dropped],
        company_managed_only,
    )
    lent_owner = normalize_acting_user_id(lent_owner_id)
    lent_dropped = [u for u in dropped if str(u) in (lent_ids or set())]
    if lent_dropped and lent_owner is not None:
        lent_rows = (
            await db.execute(
                select(Integration).where(
                    Integration.id.in_(lent_dropped),
                    Integration.user_id == lent_owner,
                    Integration.credential_mode != PER_USER_CREDENTIAL_MODE,
                )
            )
        ).scalars().all()
        for integration in lent_rows:
            bindings.setdefault(integration.provider, []).append(str(integration.id))
            kept.add(str(integration.id))
        dropped = [u for u in dropped if str(u) not in kept]
    if not dropped:
        return bindings
    withheld = (
        await db.execute(select(Integration).where(Integration.id.in_(dropped), *visible))
    ).scalars().all()
    # A ``per_user`` integration is the person's to use once they
    # connected their own account on it; the tool build then acts with it.
    from app.services.integration_user_credentials import connected_user_ids

    per_user = [i for i in withheld if i.credential_mode == PER_USER_CREDENTIAL_MODE]
    connected = await connected_user_ids(db, [i.id for i in per_user], acting)
    for integration in withheld:
        if str(integration.id) in connected:
            bindings.setdefault(integration.provider, []).append(str(integration.id))
        elif refused is not None and integration.credential_mode == PER_USER_CREDENTIAL_MODE:
            refused.append(own_account_not_connected_binding(integration))
        elif refused is not None and not oauth_credentials_usable(integration, acting):
            refused.append(refused_binding(integration))
    return bindings


async def session_integrations_withheld(
    db: AsyncSession,
    user_id: Any,
    session_integration_ids: Iterable[Any],
    resolved: dict[str, list[str]],
    *,
    refused: Iterable[dict],
) -> list[dict]:
    """Everything a chat turn's session selection lost to who may use it:
    the *refused* entries of :func:`resolve_session_integration_bindings`
    (*resolved* is what it returned) - a token the user may not use - and the
    ids it named that the user cannot see (:func:`invisible_integration_bindings`),
    which the resolver drops without a word."""
    kept = canonical_integration_ids(flatten_binding_ids(resolved))
    lost = [
        raw
        for raw in _flatten_session_ids(session_integration_ids)
        if not canonical_integration_ids([raw]) & kept
    ]
    return [*refused, *await invisible_integration_bindings(db, user_id, lost)]


#: Node config keys that hold integration ids (a flow's integration node and
#: integration-event trigger hold one, a capability.integration node a list).
FLOW_INTEGRATION_FIELDS = ("integration_id", "integration_ids")


def canonical_integration_ids(values: Iterable[Any]) -> set[str]:
    """The canonical UUID strings among *values*; anything unparseable is skipped."""
    return {str(p) for p in (safe_uuid(str(v)) for v in values if v) if p is not None}


def added_integration_bindings(
    bindings: Any, stored_ids: Iterable[Any]
) -> dict[str, list[str]] | None:
    """The part of a provider -> ids bindings map an entity did not hold yet.

    Ids are kept as sent, so a malformed new one is still reported by the
    caller's validation; an id matching a stored one in any spelling is left
    out. ``None`` when the request adds nothing.
    """
    if not isinstance(bindings, dict):
        return None
    stored = canonical_integration_ids(stored_ids)
    added: dict[str, list[str]] = {}
    for key, value in bindings.items():
        items = value if isinstance(value, (list, tuple)) else [value]
        new = [
            item
            for item in items
            if isinstance(item, str)
            and item
            and not (canonical_integration_ids([item]) & stored)
        ]
        if new:
            added[str(key)] = new
    return added or None


def flow_integration_refs(graph: Any) -> list[tuple[Any, str, str]]:
    """``(node_id, field, integration_id)`` for every integration id a flow graph holds."""
    refs: list[tuple[Any, str, str]] = []
    if not isinstance(graph, dict):
        return refs
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        config = node.get("config") or {}
        if not isinstance(config, dict):
            continue
        for field in FLOW_INTEGRATION_FIELDS:
            value = config.get(field)
            items = value if isinstance(value, (list, tuple)) else [value]
            refs.extend(
                (node.get("id"), field, item) for item in items if isinstance(item, str) and item
            )
    return refs


async def integration_ids_writer_cannot_see(
    db: AsyncSession,
    user_id: Any,
    *,
    is_admin: bool,
    ids: Iterable[Any],
    stored: Iterable[Any] = (),
    ignore_missing: bool = False,
) -> set[str]:
    """The ids a save SETS that its writer cannot see (canonical form).

    Only an id absent from *stored* - the value the entity holds before the
    save - is a new write: an id someone else put there earlier is never
    re-checked, so an unrelated edit, or an unchanged id sent back, always
    saves. Admins may set any integration. Visibility is the three-tier rule
    with no admin bypass (:func:`visible_integration_ids`); an id naming no
    integration cannot be seen either, unless *ignore_missing*: a reference
    whose run reports or falls back on a missing integration (an imported
    flow, a pipeline ref) keeps saving one, as it always did.
    """
    if is_admin:
        return set()
    new = canonical_integration_ids(ids) - canonical_integration_ids(stored)
    if not new:
        return set()
    refused = new - await visible_integration_ids(db, user_id, new)
    if refused and ignore_missing:
        existing = (
            await db.execute(
                select(Integration).where(
                    Integration.id.in_([uuid.UUID(i) for i in refused])
                )
            )
        ).scalars().all()
        refused = {str(row.id) for row in existing}
    return refused


#: Agent fields holding a pipeline tool ref: an integration ref pins
#: an ``integration_id`` the pipeline loads with its stored credentials.
PIPELINE_TOOL_REF_FIELDS = ("prepare_tool_ref", "finalize_tool_ref")


def pipeline_ref_integration_ids(*refs: Any) -> list[str]:
    """The ``integration_id`` values pinned by pipeline tool refs."""
    return [
        ref["integration_id"]
        for ref in refs
        if isinstance(ref, dict)
        and ref.get("source") == "integration"
        and isinstance(ref.get("integration_id"), str)
        and ref["integration_id"]
    ]


PIPELINE_REF_NOT_VISIBLE_DETAIL = (
    "You cannot pin this integration in the pipeline: it is not shared with "
    "you. Pick one you can see, or ask its owner to share it."
)


async def pipeline_refs_refusal(
    db: AsyncSession, user_id: Any, *, is_admin: bool, refs: Iterable[Any], stored: Iterable[Any]
) -> str | None:
    """Why a save may not pin these pipeline integrations, ``None`` when it may.

    A pinned integration runs with its stored credentials whoever the turn's
    user is: without this check an agent's owner could pin a colleague's
    integration id, share the agent with them, and run with their
    credentials. Pins the agent already held, and an id naming no integration
    (the pipeline falls back to scope resolution), save as before.
    """
    refused = await integration_ids_writer_cannot_see(
        db,
        user_id,
        is_admin=is_admin,
        ids=pipeline_ref_integration_ids(*refs),
        stored=pipeline_ref_integration_ids(*stored),
        ignore_missing=True,
    )
    return PIPELINE_REF_NOT_VISIBLE_DETAIL if refused else None


async def require_pipeline_refs_visible(
    db: AsyncSession, user_id: Any, *, is_admin: bool, refs: Iterable[Any], stored: Iterable[Any]
) -> None:
    """:func:`pipeline_refs_refusal` as a 403 for a REST route."""
    refusal = await pipeline_refs_refusal(
        db, user_id, is_admin=is_admin, refs=refs, stored=stored
    )
    if refusal:
        raise HTTPException(status_code=403, detail=refusal)
