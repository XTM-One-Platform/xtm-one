# SPDX-License-Identifier: Apache-2.0

"""What deleting an LLM provider config does to every route that used it.

Deleting a config nulls every stored reference to it
(``_cleanup_orphaned_model_references`` in ``api/v1/settings.py``). Left at
that, each inherited route fell through to whichever surviving config an
unordered query returned first, so traffic moved to another provider's
credentials, bill and governance with nothing said and nothing recorded. This
module decides, once, what each route becomes, and the Settings confirmation
(``GET /settings/llm-providers/{id}/deletion-impact``) and the delete itself
read the same answer:

* the platform default model (``PlatformSettings.agent_default_*``) and the
  General Assistant chat model (``GeneralAssistantConfig.llm_*``) move to ONE
  replacement config - the admin's pick, else :func:`pick_replacement` - and
  are pinned to it by id rather than re-derived on every call; each keeps its
  model when the replacement lists it, else the replacement's own default model
  if listed, else its first listed model (:func:`replacement_model_for`) - a
  model the router keeps the route on, so traffic lands where the preview says;
  a config listing no model never takes a route that carries one;
* the image default moves to a surviving config that can serve images
  (:func:`pick_image_target`), keeping its model where one can serve it, and
  is cleared when none can;
* the embedding default moves only to a surviving config that serves the SAME
  embedding model (:func:`pick_embedding_target`), the stored dimensions kept,
  so the stored vectors stay comparable and nothing is re-embedded; with no
  such config it is cleared and never switched to another model (another
  embedding model makes every knowledge base's stored vectors incomparable
  with the queries, so choosing one stays an explicit admin act), and the
  administrators are told (:func:`notify_admins_embedding_cleared`);
* the routing, fallback and decision models move to a surviving config that
  offers the same model (:func:`pick_same_model_target`), and are cleared
  when none does;
* agents and user preferences pinned to the config are reset, so they
  inherit the defaults above;
* the knowledge base chunks it embedded keep their vectors; their provider id
  is nulled after the deletion commits, in bounded batches, by the scheduler
  (:func:`clear_deleted_embedding_provider_references`).

The orphan predicates live here too, so the preview and the cleanup cannot
disagree on which stored choices a deletion touches.

A deletion decides from the configs it reads: the delete locks every config row
before it reads them, and the confirmation hands back the
:meth:`DeletionImpact.fingerprint` it showed, which the delete refuses when
what it would apply is no longer that. Every other write of a provider
reference takes the same lock (:func:`lock_provider_configs`, shared for the
agent writes) before it reads what it writes, and refuses a new reference to a
config that does not exist (:func:`provider_reference_problem`).
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import time
import uuid
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.base import BaseLLMProvider
from app.llm.gateway_embedding_default import adoptable_gateway_default, byollm_keeps_it_empty
from app.llm.router import (
    CHATLESS_PROVIDERS,
    EMBEDDING_DEFAULTS,
    IMAGE_DEFAULTS,
    PROVIDER_MAP,
    _config_can_serve_model,
    _config_has_chat_capable_model,
    chat_default_model,
    implicit_chat_config,
    implicit_embedding_fallback,
    is_chat_capable_model,
    oldest_first,
    select_provider_configs,
)
from app.models.agent import Agent
from app.models.knowledge_base import KnowledgeBase
from app.models.llm_config import LLMProviderConfig
from app.models.user import User

logger = logging.getLogger(__name__)

# How many agent / user names the confirmation lists before "and N more".
MAX_LISTED_NAMES = 10

RouteAction = Literal["unchanged", "reseeded", "cleared"]


class ReplacementError(ValueError):
    """The requested replacement config cannot take over the deleted config's routes."""


# ---------------------------------------------------------------------------
# Serializing the writes of a provider reference with the deletions
# ---------------------------------------------------------------------------


# Single-key ``pg_advisory_xact_lock(int8)`` space, like the agent-delete and
# DCR registration keys: ``int.from_bytes(b"llmprov", "big")``, written out in
# the statements (pinned by a test) so they carry no bound parameter.
_PROVIDER_CONFIGS_LOCK_KEY = int.from_bytes(b"llmprov", "big")
_ADVISORY_LOCK = {
    False: text("SELECT pg_advisory_xact_lock(30518514778533750)"),
    True: text("SELECT pg_advisory_xact_lock_shared(30518514778533750)"),
}

# ``query_canceled`` (the statement timeout) and ``lock_not_available`` (a
# ``lock_timeout``): the wait for the lock ran out, nothing else went wrong.
_QUERY_CANCELED = "57014"
_LOCK_NOT_AVAILABLE = "55P03"
_LOCK_WAIT_EXPIRED = frozenset({_QUERY_CANCELED, _LOCK_NOT_AVAILABLE})

PROVIDER_LOCK_BUSY_DETAIL = (
    "Another LLM provider change is in progress (a provider being deleted or "
    "edited, or the platform default models being saved). Try again in a moment."
)


class ProviderLockBusyError(HTTPException):
    """The provider config lock was not granted before the statement timeout.

    An ``HTTPException`` so every route taking the lock answers 503 without a
    handler of its own; callers outside a request (the agent builder tool)
    catch it and hand back ``detail``.
    """

    def __init__(self) -> None:
        super().__init__(
            status_code=503,
            detail=PROVIDER_LOCK_BUSY_DETAIL,
            headers={"Retry-After": "5"},
        )


def _sqlstate(exc: DBAPIError) -> str | None:
    """The SQLSTATE of a database error: the one detector of this module."""
    orig = getattr(exc, "orig", None)
    return getattr(orig, "pgcode", None) or getattr(orig, "sqlstate", None)


async def lock_provider_configs(db: AsyncSession, *, shared: bool = False) -> None:
    """Serialize with provider deletions until the transaction ends.

    Exclusive for a deletion, a catalog edit (the environment seeds included)
    and the platform defaults save; ``shared`` for an agent's model choice
    (create, update, duplicate, import, catalog install) and a provider
    creation, so those never wait on each other and still wait on a deletion or
    a catalog edit. Two locks, always in this order: a transaction advisory
    lock, which also covers a config inserted after the row scan, then every
    provider config row in id order (``FOR NO KEY UPDATE`` / ``FOR SHARE``).
    Taken before anything the write changes is read or written:
    a row read first would be stale once the lock is granted, and a row written
    first would be locked in the opposite order to the deletion, which updates
    it after locking the configs.

    ``FOR NO KEY UPDATE`` is the weakest row lock that still stops every other
    update or delete of a config row, which is all the rows are locked for;
    ``FOR UPDATE`` would also exclude ``FOR KEY SHARE``, the lock a foreign key
    check onto the configs takes, and nothing here needs that excluded. No
    foreign key points at the configs since the embeddings one was dropped;
    an install still carrying it from before that migration keeps its
    embedding writes flowing under a deletion or a defaults save. A wait past
    the statement timeout raises :class:`ProviderLockBusyError` (503) instead
    of the raw cancellation.
    """
    try:
        await db.execute(_ADVISORY_LOCK[shared])
        await db.execute(
            select(LLMProviderConfig.id)
            .order_by(LLMProviderConfig.id)
            .with_for_update(read=shared, key_share=not shared)
        )
    except DBAPIError as exc:
        if _sqlstate(exc) in _LOCK_WAIT_EXPIRED:
            raise ProviderLockBusyError() from exc
        raise


async def provider_reference_problem(
    db: AsyncSession,
    provider_id: uuid.UUID | None,
    *,
    stored: uuid.UUID | None,
    label: str,
    user_id: object = None,
    scoped: bool = False,
) -> str | None:
    """Why *provider_id* cannot be stored for *label*, or ``None`` when it can.

    Only a reference the write adds or changes is checked: one already stored
    and sent back unchanged is not the write's doing. The row found stays
    locked ``FOR SHARE`` until the write commits (already held when
    :func:`lock_provider_configs` saw it). *scoped* also refuses a provider
    restricted to groups and users *user_id* is not among: a
    non-admin picks from the providers they may use.
    """
    if provider_id is None or provider_id == stored:
        return None
    try:
        exists = (
            await db.execute(
                select(LLMProviderConfig.id)
                .where(LLMProviderConfig.id == provider_id)
                .with_for_update(read=True)
            )
        ).scalar_one_or_none()
    except DBAPIError as exc:
        if _sqlstate(exc) in _LOCK_WAIT_EXPIRED:
            raise ProviderLockBusyError() from exc
        raise
    if exists is None:
        return (
            f"The provider selected for {label} no longer exists: it was deleted, "
            "or the page is out of date. Reload and pick a provider again."
        )
    if scoped:
        from app.llm.provider_access import RESTRICTED_PROVIDER_MESSAGE, provider_id_usable_by

        if not await provider_id_usable_by(db, provider_id, user_id):
            return RESTRICTED_PROVIDER_MESSAGE.format(label=label)
    return None


# ---------------------------------------------------------------------------
# Which stored choices a deletion (or a catalog shrink) orphans
# ---------------------------------------------------------------------------


def reference_points_at_deleted(
    stored_provider: str | None,
    stored_provider_id: uuid.UUID | None,
    deleted_provider: str,
    deleted_provider_id: uuid.UUID | None,
) -> bool:
    """Whether a stored (provider, reference) pair named the config being deleted.

    A stored reference is decisive: several configs share a provider name, so
    matching on the name as well would wipe a choice pinned to a config that
    still exists. The name is only consulted when no reference was
    saved, which is every choice made before the reference columns existed.
    """
    if stored_provider_id is not None and deleted_provider_id is not None:
        return stored_provider_id == deleted_provider_id
    return stored_provider == deleted_provider


def reference_pins_elsewhere(
    stored_provider_id: uuid.UUID | None,
    changed_provider_id: uuid.UUID | None,
) -> bool:
    """Whether a stored reference pins the choice to some other config.

    ``removed_models`` only ever describes the changed config's catalog, so a
    model leaving it says nothing about a choice pinned to a config that still
    offers it. With no reference stored there is nothing to pin, and the
    model clause keeps its pre-reference behaviour.
    """
    return (
        stored_provider_id is not None
        and changed_provider_id is not None
        and stored_provider_id != changed_provider_id
    )


def pair_is_orphaned(
    provider: str | None,
    provider_id: uuid.UUID | None,
    model: str | None,
    *,
    provider_name: str,
    removed_models: Collection[str],
    provider_deleted: bool,
    changed_provider_id: uuid.UUID | None,
) -> bool:
    """Whether a stored (provider, reference, model) choice loses its config.

    True when the deleted config is the one the choice names, or when its model
    left the changed config's catalog and no reference pins it elsewhere.
    """
    if provider_deleted and reference_points_at_deleted(
        provider, provider_id, provider_name, changed_provider_id
    ):
        return True
    return bool(
        model
        and model in removed_models
        and not reference_pins_elsewhere(provider_id, changed_provider_id)
    )


def model_reference_is_orphaned(
    model: str | None,
    reference_id: uuid.UUID | None,
    *,
    removed_models: Collection[str],
    provider_deleted: bool,
    changed_provider_id: uuid.UUID | None,
) -> bool:
    """:func:`pair_is_orphaned` for a choice stored as (model, reference) only.

    The routing and fallback models carry no provider name: a deletion orphans
    them through the reference alone.
    """
    if (
        model
        and model in removed_models
        and not reference_pins_elsewhere(reference_id, changed_provider_id)
    ):
        return True
    return (
        provider_deleted
        and changed_provider_id is not None
        and reference_id == changed_provider_id
    )


def orphaned_reference_clause(
    provider_column: Any,
    provider_id_column: Any,
    model_column: Any,
    *,
    provider_name: str,
    removed_models: Collection[str],
    provider_deleted: bool,
    changed_provider_id: uuid.UUID | None,
) -> Any | None:
    """:func:`pair_is_orphaned` as a SQL ``WHERE`` clause over three columns.

    ``None`` when neither a deletion nor a catalog shrink is described, so the
    caller has nothing to look for.
    """
    conditions = []
    if provider_deleted:
        if changed_provider_id is not None:
            conditions.append(
                or_(
                    provider_id_column == changed_provider_id,
                    and_(
                        provider_id_column.is_(None),
                        provider_column == provider_name,
                    ),
                )
            )
        else:
            conditions.append(provider_column == provider_name)
    if removed_models:
        model_condition = model_column.in_(removed_models)
        if changed_provider_id is not None:
            model_condition = and_(
                model_condition,
                or_(
                    provider_id_column.is_(None),
                    provider_id_column == changed_provider_id,
                ),
            )
        conditions.append(model_condition)
    if not conditions:
        return None
    return or_(*conditions)


# An agent's model choice as a deletion reads it, in the (provider, reference,
# model) order of :func:`orphaned_reference_clause`. Writing any of them is a
# provider reference write: a provider name alone is reset by name.
AGENT_MODEL_FIELDS: tuple[str, str, str] = ("llm_provider", "llm_provider_id", "llm_model")


def orphaned_agent_clause(**kwargs: Any) -> Any | None:
    """Agents whose own model choice the deletion / shrink orphans."""
    return orphaned_reference_clause(
        *(getattr(Agent, field) for field in AGENT_MODEL_FIELDS), **kwargs
    )


def orphaned_user_clause(**kwargs: Any) -> Any | None:
    """Users whose preferred model the deletion / shrink orphans."""
    return orphaned_reference_clause(
        User.preferred_llm_provider,
        User.preferred_llm_provider_id,
        User.preferred_llm_model,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# The replacement rule
# ---------------------------------------------------------------------------


UNLISTED_REPLACEMENT_REASON = (
    "The replacement provider lists no model, so it cannot take over a default "
    "that runs on a model."
)


def listed_chat_models(config: LLMProviderConfig) -> list[str]:
    """The chat models *config* lists in ``available_models``, in catalog order.

    A model a config lists is one the router keeps a pinned route on
    (``_config_can_serve_model``); an unlisted one - a ``default_model`` left
    out of the catalog, a provider class default - is re-routed to whichever
    config owns it, so a route re-seeded on it would not land where the
    confirmation said.
    """
    models = getattr(config, "available_models", None) or []
    return [m for m in dict.fromkeys(models) if m and is_chat_capable_model(m)]


def listed_default_model(config: LLMProviderConfig) -> str | None:
    """The model a route moved onto *config* takes when it keeps nothing of its own.

    The config's own ``default_model`` when it lists it, else its first listed
    chat model; ``None`` when it lists none.
    """
    listed = listed_chat_models(config)
    default_model = getattr(config, "default_model", None)
    if default_model in listed:
        return default_model
    return listed[0] if listed else None


def offered_model(config: LLMProviderConfig) -> str | None:
    """The model the confirmation shows next to a candidate replacement.

    :func:`listed_default_model`; for a config that lists no model (offered
    only when no moving route carries one), the model a model-less call on it
    runs (:func:`chat_default_model`).
    """
    return listed_default_model(config) or chat_default_model(config)


def replacement_candidates(
    configs: Iterable[LLMProviderConfig],
    *,
    needs_model: bool = False,
) -> list[LLMProviderConfig]:
    """The configs that can take over chat traffic, in the order they are offered.

    Chat-capable configs - not embedding-only, exposing a chat model
    (``_config_has_chat_capable_model``) - whose default chat model can be
    named, in the order Settings lists them: oldest first, tie-broken by id.
    When a moving route runs on a model (*needs_model*), only configs that
    list one: a config listing none cannot hold it.
    """
    return [
        cfg
        for cfg in oldest_first(configs)
        if cfg.provider not in CHATLESS_PROVIDERS
        and _config_has_chat_capable_model(cfg)
        and chat_default_model(cfg)
        and (not needs_model or listed_chat_models(cfg))
    ]


def pick_replacement(
    survivors: Iterable[LLMProviderConfig],
    *,
    replacement_id: uuid.UUID | None = None,
    deleted_id: uuid.UUID | None = None,
    needs_model: bool = False,
) -> LLMProviderConfig | None:
    """The config the deleted config's chat routes move to.

    The admin's pick when one is given - refused with :class:`ReplacementError`
    unless it is a surviving config that can serve chat and, when a moving route
    runs on a model (*needs_model*), lists one - else the first of
    :func:`replacement_candidates`. ``None`` when no surviving config qualifies.
    """
    survivors = list(survivors)
    candidates = replacement_candidates(survivors, needs_model=needs_model)
    if replacement_id is None:
        return candidates[0] if candidates else None
    if deleted_id is not None and replacement_id == deleted_id:
        raise ReplacementError(
            "The replacement provider cannot be the provider being deleted."
        )
    chosen = next((cfg for cfg in survivors if cfg.id == replacement_id), None)
    if chosen is None:
        raise ReplacementError("The replacement provider does not exist.")
    if all(cfg.id != chosen.id for cfg in candidates):
        if chosen.id in {cfg.id for cfg in replacement_candidates(survivors)}:
            raise ReplacementError(UNLISTED_REPLACEMENT_REASON)
        raise ReplacementError(
            "The replacement provider offers no chat model, so it cannot take "
            "over the chat defaults."
        )
    return chosen


def replacement_model_for(
    replacement: LLMProviderConfig, previous_model: str | None
) -> tuple[str | None, bool]:
    """The model a chat route runs on once it moves to *replacement*.

    ``(model, kept)``: the model the route used before when the replacement
    lists it, so the move changes the credentials and nothing else; otherwise
    the replacement's own default model if it lists it, else its first listed
    chat model (:func:`listed_default_model`). Always a model the replacement
    lists, so the router keeps the route on it; ``None`` only for a config that
    lists none, which is never picked for a route carrying a model.
    """
    if previous_model and previous_model in listed_chat_models(replacement):
        return previous_model, True
    return listed_default_model(replacement), False


def admin_rows_first(configs: Iterable[LLMProviderConfig]) -> list[LLMProviderConfig]:
    """*configs* oldest first, the rows an administrator created before the env-provisioned ones.

    An env-provisioned config disappears again when its variable is removed
    (the boot prune), so a default moved onto it can be orphaned at the next
    restart; an administrator's row stays until an administrator deletes it.
    """
    ordered = oldest_first(configs)
    return [cfg for cfg in ordered if not getattr(cfg, "is_from_env", False)] + [
        cfg for cfg in ordered if getattr(cfg, "is_from_env", False)
    ]


def pick_same_model_target(
    survivors: Iterable[LLMProviderConfig],
    model: str | None,
    *,
    inferred: bool = False,
) -> LLMProviderConfig | None:
    """The surviving config a model-only default (routing, fallback, decision) moves to.

    The first config, administrator rows first (:func:`admin_rows_first`),
    that lists *model* in its catalog; with *inferred*, also one the model's
    name resolves to (``_config_can_serve_model``), the rule the fallback
    model's own write path accepts. ``None`` when no survivor offers it: the
    default is then cleared, never moved to another model.
    """
    if not model:
        return None
    for cfg in admin_rows_first(survivors):
        if cfg.provider in CHATLESS_PROVIDERS:
            continue
        if model in (getattr(cfg, "available_models", None) or []):
            return cfg
        if inferred and _config_can_serve_model(cfg, model):
            return cfg
    return None


def _embeds(config: LLMProviderConfig) -> bool:
    """Whether *config*'s provider class implements embeddings at all."""
    provider_cls = PROVIDER_MAP.get(config.provider)
    return provider_cls is not None and getattr(provider_cls, "embed", None) is not BaseLLMProvider.embed


def pick_embedding_target(
    survivors: Iterable[LLMProviderConfig], embedding_model: str | None
) -> LLMProviderConfig | None:
    """The surviving config the embedding default moves to: one serving the SAME model.

    Administrator rows first (:func:`admin_rows_first`), among configs whose
    provider embeds: the first that lists *embedding_model* in its catalog or
    whose provider's built-in embedding model it is (``EMBEDDING_DEFAULTS``,
    the model :func:`implicit_embedding_fallback` would embed with). The
    stored dimensions are kept, so the vectors already indexed stay comparable
    and nothing is re-embedded. ``None`` when no survivor serves that model.
    """
    if not embedding_model:
        return None
    for cfg in admin_rows_first(survivors):
        if not _embeds(cfg):
            continue
        if embedding_model in (getattr(cfg, "available_models", None) or []):
            return cfg
        if EMBEDDING_DEFAULTS.get(cfg.provider) == embedding_model:
            return cfg
    return None


def _generates_images(config: LLMProviderConfig) -> bool:
    """Whether *config*'s provider class implements image generation at all."""
    provider_cls = PROVIDER_MAP.get(config.provider)
    return provider_cls is not None and provider_cls.generate_image is not BaseLLMProvider.generate_image


def pick_image_target(
    survivors: Iterable[LLMProviderConfig],
    *,
    replacement: LLMProviderConfig | None,
    image_model: str | None,
    keep_model_only: bool = False,
) -> tuple[LLMProviderConfig, str] | None:
    """Where the image default moves when its config is deleted.

    Only configs whose provider generates images are considered (a gateway
    listing an image model id does not), the replacement first, then the other
    survivors oldest first: the first that can serve the same image model keeps
    it (``_config_can_serve_model``: listed in its catalog, or named for its
    vendor), else, unless *keep_model_only*, the first whose provider has a
    built-in image model (``IMAGE_DEFAULTS``) takes that model. ``None`` when
    no surviving config can take it.
    """
    ordered = oldest_first(survivors)
    if replacement is not None:
        ordered = [replacement, *(cfg for cfg in ordered if cfg.id != replacement.id)]
    ordered = [cfg for cfg in ordered if _generates_images(cfg)]
    if image_model:
        for cfg in ordered:
            if _config_can_serve_model(cfg, image_model):
                return cfg, image_model
    if keep_model_only:
        return None
    for cfg in ordered:
        builtin = IMAGE_DEFAULTS.get(cfg.provider)
        if builtin:
            return cfg, builtin
    return None


# ---------------------------------------------------------------------------
# The impact
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RouteTarget:
    """A model on a provider config, as a route stores it."""

    provider: str | None
    provider_id: uuid.UUID | None
    display_name: str | None
    model: str | None

    @classmethod
    def of(cls, config: LLMProviderConfig, model: str | None) -> RouteTarget:
        return cls(config.provider, config.id, config.display_name, model)

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "provider_id": str(self.provider_id) if self.provider_id else None,
            "display_name": self.display_name,
            "model": self.model,
        }


@dataclass(frozen=True)
class RouteImpact:
    """What one inherited route becomes."""

    action: RouteAction
    before: RouteTarget | None
    after: RouteTarget | None
    # Owned by a ``DEFAULT_*`` environment variable, which the boot seed
    # re-applies at the next restart.
    env_managed: bool = False
    # For a re-seeded route: True when it keeps the model it used before, on
    # the new config; False when it moves to that config's default model.
    model_kept: bool | None = None

    @property
    def affected(self) -> bool:
        return self.action != "unchanged"

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "before": self.before.as_dict() if self.before else None,
            "after": self.after.as_dict() if self.after else None,
            "model_kept": self.model_kept,
            "env_managed": self.env_managed,
        }


@dataclass(frozen=True)
class EmbeddingImpact(RouteImpact):
    """The embedding route: kept on a survivor serving the same model, else cleared, and where embeddings go meanwhile."""

    # What ``get_embedding_provider`` falls back to once the default is cleared,
    # or, with no default stored, once the deleted config is gone.
    fallback: RouteTarget | None = None
    # Knowledge bases holding indexed chunks - what a new embedding model re-embeds.
    knowledge_base_count: int = 0
    # No embedding default is stored and embeddings fall back to the deleted
    # config today: the deletion moves them to *fallback* with nothing stored
    # changing. *previous_fallback* is where they go today.
    fallback_moves: bool = False
    previous_fallback: RouteTarget | None = None

    @property
    def affected(self) -> bool:
        return self.action != "unchanged" or self.fallback_moves

    @property
    def fallback_same_model(self) -> bool:
        """Whether the fallback embeds with the model the stored vectors were built with."""
        previous = self.previous_fallback if self.fallback_moves else self.before
        return bool(
            self.fallback
            and previous
            and previous.model
            and self.fallback.model == previous.model
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            **super().as_dict(),
            "fallback": self.fallback.as_dict() if self.fallback else None,
            "knowledge_base_count": self.knowledge_base_count,
            "fallback_moves": self.fallback_moves,
            "previous_fallback": self.previous_fallback.as_dict() if self.previous_fallback else None,
        }


@dataclass(frozen=True)
class ReferenceGroup:
    """Agents or users whose own choice points at the deleted config."""

    count: int
    names: list[str]


@dataclass(frozen=True)
class DeletionImpact:
    """Everything a deletion changes, decided before anything is written."""

    deleted: RouteTarget
    replacement: RouteTarget | None
    replacement_explicit: bool
    default_replacement_id: uuid.UUID | None
    candidates: list[RouteTarget]
    general_assistant: RouteImpact
    agent_default: RouteImpact
    image_default: RouteImpact
    embedding_default: EmbeddingImpact
    routing_model: RouteImpact
    fallback_model: RouteImpact
    decision_model: RouteImpact
    agents: ReferenceGroup
    users: ReferenceGroup
    # Whether a surviving config can still serve chat (a candidate or not).
    chat_available: bool = False
    # Chat-capable survivors withheld as the replacement because they list no
    # model and a moving route runs on one.
    unlisted: tuple[RouteTarget, ...] = ()
    # Where a chat call that no stored default decides lands after the deletion
    # (``router.implicit_chat_config``), set when that changes: with no
    # chat-capable survivor (a config listing only non-chat models, called on
    # its provider's built-in chat model; ``None`` when chat stops), or when no
    # chat default is stored and the deleted config is where chat goes today.
    implicit_chat_fallback: RouteTarget | None = None
    implicit_chat_moves: bool = False
    previous_implicit_chat: RouteTarget | None = None

    def routes(self) -> dict[str, RouteImpact]:
        """Every route the deletion decides, by the name the preview and the audit use."""
        return {
            "agent_default": self.agent_default,
            "general_assistant": self.general_assistant,
            "image_default": self.image_default,
            "embedding_default": self.embedding_default,
            "routing_model": self.routing_model,
            "fallback_model": self.fallback_model,
            "decision_model": self.decision_model,
        }

    def fingerprint(self) -> str:
        """A digest of what the deletion applies, the same for the preview and the delete.

        The Settings confirmation sends back the one it showed and the delete
        refuses when it no longer matches, so nothing is applied on the strength
        of a preview another change has overtaken. Who picked the replacement is
        left out (the dialog's first read lets the rule pick, its confirm names
        the same config), and so is what only the dialog reads (the knowledge
        base count, the names listed).
        """
        payload = {
            "deleted": str(self.deleted.provider_id),
            "replacement": self.replacement.as_dict() if self.replacement else None,
            "routes": {name: RouteImpact.as_dict(route) for name, route in self.routes().items()},
            "embedding_fallback": (
                self.embedding_default.fallback.as_dict() if self.embedding_default.fallback else None
            ),
            "embedding_fallback_moves": self.embedding_default.fallback_moves,
            "chat_available": self.chat_available,
            "implicit_chat_fallback": (
                self.implicit_chat_fallback.as_dict() if self.implicit_chat_fallback else None
            ),
            "implicit_chat_moves": self.implicit_chat_moves,
            "agents_reset": self.agents.count,
            "users_reset": self.users.count,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()

    def audit_details(self) -> dict[str, Any]:
        """The replacement applied per route and the reset counts, for the audit log."""
        return {
            "provider": self.deleted.provider,
            "provider_id": str(self.deleted.provider_id),
            "display_name": self.deleted.display_name,
            "replacement": (
                {
                    **self.replacement.as_dict(),
                    # "admin": named in the request (the Settings dialog always
                    # sends the replacement it showed); "rule": picked here.
                    "chosen_by": "admin" if self.replacement_explicit else "rule",
                    "is_rule_default": self.replacement.provider_id == self.default_replacement_id,
                }
                if self.replacement
                else None
            ),
            "routes": {name: route.as_dict() for name, route in self.routes().items()},
            "implicit_chat": (
                {
                    "moves": self.implicit_chat_moves,
                    "from": self.previous_implicit_chat.as_dict() if self.previous_implicit_chat else None,
                    "to": self.implicit_chat_fallback.as_dict() if self.implicit_chat_fallback else None,
                }
                if self.implicit_chat_moves or self.implicit_chat_fallback
                else None
            ),
            "agents_reset": self.agents.count,
            "users_reset": self.users.count,
        }


def _chat_route(
    *,
    orphaned: bool,
    before: RouteTarget | None,
    replacement: LLMProviderConfig | None,
    env_managed: bool,
    reference_routes: bool,
) -> RouteImpact:
    """A chat default moved to the replacement, pinned by id.

    It keeps its model when the replacement lists it, else lands on a model
    the replacement lists (:func:`replacement_model_for`). A route
    that named the config without routing anything (a stale provider name and
    no model) is cleared as before: re-seeding it would invent a default the
    admin never had. Where a reference routes on its own (*reference_routes*),
    a route pinned with no model moves its pin and stays model-less, so the
    tiers below it keep applying exactly as they did.
    """
    if not orphaned:
        return RouteImpact("unchanged", before, before, env_managed)
    routed = before is not None and (
        bool(before.model) or (reference_routes and before.provider_id is not None)
    )
    if not routed or replacement is None:
        return RouteImpact("cleared", before, None, env_managed)
    if not before.model:
        return RouteImpact("reseeded", before, RouteTarget.of(replacement, None), env_managed)
    model, kept = replacement_model_for(replacement, before.model)
    return RouteImpact(
        "reseeded", before, RouteTarget.of(replacement, model), env_managed, model_kept=kept
    )


def _cleared_or_unchanged(
    orphaned: bool, before: RouteTarget | None, env_managed: bool
) -> RouteImpact:
    if orphaned:
        return RouteImpact("cleared", before, None, env_managed)
    return RouteImpact("unchanged", before, before, env_managed)


async def _reference_group(
    db: AsyncSession, model: Any, name_column: Any, clause: Any
) -> ReferenceGroup:
    if clause is None:
        return ReferenceGroup(0, [])
    count = (
        await db.execute(select(func.count()).select_from(model).where(clause))
    ).scalar_one()
    if not count:
        return ReferenceGroup(0, [])
    names = (
        await db.execute(
            select(name_column).where(clause).order_by(name_column).limit(MAX_LISTED_NAMES)
        )
    ).scalars().all()
    return ReferenceGroup(int(count), [name for name in names if name])


async def compute_deletion_impact(
    db: AsyncSession,
    config: LLMProviderConfig,
    *,
    replacement_id: uuid.UUID | None = None,
    platform_settings: Any | None,
    general_assistant: Any | None,
    env_managed_fields: Collection[str] = (),
) -> DeletionImpact:
    """What deleting *config* does, with nothing written.

    *platform_settings* and *general_assistant* are the singletons as they
    stand before the deletion (``None`` when absent). Raises
    :class:`ReplacementError` for a *replacement_id* that cannot take over.
    """
    configs = list((await db.execute(select_provider_configs())).scalars().all())
    survivors = [cfg for cfg in configs if cfg.id != config.id]
    by_id = {cfg.id: cfg for cfg in configs}
    by_id.setdefault(config.id, config)

    def target(
        provider: str | None, provider_id: uuid.UUID | None, model: str | None
    ) -> RouteTarget | None:
        if not (provider or provider_id or model):
            return None
        known = by_id.get(provider_id) if provider_id is not None else None
        return RouteTarget(
            provider or (known.provider if known else None),
            provider_id,
            known.display_name if known else None,
            model,
        )

    change = {
        "removed_models": set(config.available_models or []),
        "provider_deleted": True,
        "changed_provider_id": config.id,
    }
    pair_orphaned = functools.partial(pair_is_orphaned, provider_name=config.provider, **change)
    reference_orphaned = functools.partial(model_reference_is_orphaned, **change)
    ps = platform_settings
    ga = general_assistant

    def pair_route(
        provider_attr: str, provider_id_attr: str, model_attr: str
    ) -> tuple[bool, RouteTarget | None]:
        if ps is None:
            return False, None
        stored = (
            getattr(ps, provider_attr, None),
            getattr(ps, provider_id_attr, None),
            getattr(ps, model_attr, None),
        )
        return pair_orphaned(*stored), target(*stored)

    agent_orphaned, agent_before = pair_route(
        "agent_default_provider", "agent_default_provider_id", "agent_default_model"
    )
    ga_stored = (
        (ga.llm_provider, getattr(ga, "llm_provider_id", None), ga.llm_model)
        if ga is not None
        else (None, None, None)
    )
    ga_orphaned = ga is not None and pair_orphaned(*ga_stored)
    ga_before = target(*ga_stored)

    # A chat route moving on a model can only land on a config that lists one.
    needs_model = (agent_orphaned and bool(agent_before and agent_before.model)) or (
        ga_orphaned and bool(ga_before and ga_before.model)
    )
    replacement_config = pick_replacement(
        survivors, replacement_id=replacement_id, deleted_id=config.id, needs_model=needs_model
    )
    replacement = (
        RouteTarget.of(replacement_config, offered_model(replacement_config))
        if replacement_config is not None
        else None
    )
    chat_capable = replacement_candidates(survivors)
    candidates = replacement_candidates(survivors, needs_model=needs_model)
    candidate_ids = {cfg.id for cfg in candidates}
    unlisted = tuple(
        RouteTarget.of(cfg, None) for cfg in chat_capable if cfg.id not in candidate_ids
    )

    agent_default = _chat_route(
        orphaned=agent_orphaned,
        before=agent_before,
        replacement=replacement_config,
        env_managed="agent_default_model" in env_managed_fields,
        # ``get_llm_for_user`` pins the primary config to
        # ``agent_default_provider_id`` even when no model is stored.
        reference_routes=True,
    )

    general_assistant_route = _chat_route(
        orphaned=ga_orphaned,
        before=ga_before,
        replacement=replacement_config,
        env_managed="llm_model" in env_managed_fields,
        # With no model of its own the assistant follows the platform default:
        # its reference is only read alongside its model.
        reference_routes=False,
    )

    image_orphaned, image_before = pair_route("image_provider", "image_provider_id", "image_model")
    image_env = "image_model" in env_managed_fields
    image_target = (
        pick_image_target(
            survivors,
            replacement=replacement_config,
            image_model=image_before.model,
            # ``DEFAULT_IMAGE_MODEL`` alone: the boot seed rewrites the model and
            # not its provider, so only a config serving that model can keep it.
            keep_model_only=image_env and "image_provider_id" not in env_managed_fields,
        )
        if image_orphaned and image_before is not None and image_before.model
        else None
    )
    if image_target is not None:
        image_config, image_model = image_target
        image_default = RouteImpact(
            "reseeded",
            image_before,
            RouteTarget.of(image_config, image_model),
            image_env,
            model_kept=image_model == image_before.model,
        )
    else:
        image_default = _cleared_or_unchanged(image_orphaned, image_before, image_env)

    embedding_orphaned, embedding_before = pair_route(
        "embedding_provider", "embedding_provider_id", "embedding_model"
    )
    embedding_env = "embedding_model" in env_managed_fields

    async def indexed_knowledge_bases() -> int:
        indexed = (
            await db.execute(
                select(func.count()).select_from(KnowledgeBase).where(KnowledgeBase.chunk_count > 0)
            )
        ).scalar_one()
        return int(indexed or 0)

    # With no embedding default stored, ``get_embedding_provider`` falls back
    # to a config by its own rule: deleting that config moves embeddings with
    # nothing stored changing, so the move is read off the same rule.
    stored_embedding = (
        getattr(ps, "embedding_provider", None) if ps is not None else None,
        getattr(ps, "embedding_model", None) if ps is not None else None,
    )
    no_embedding_default = ps is None or not (
        getattr(ps, "embedding_provider_id", None) or stored_embedding[0]
    )
    current_fallback = (
        implicit_embedding_fallback(
            configs, embedding_provider=stored_embedding[0], embedding_model=stored_embedding[1]
        )
        if no_embedding_default and not embedding_orphaned
        else None
    )
    async def survivors_embedding_fallback(fallback):
        if fallback is not None or await byollm_keeps_it_empty(db):
            return fallback
        # As ``get_embedding_provider`` does next: an AI Gateway's flagged model, where it re-embeds nothing.
        return await adoptable_gateway_default(db, survivors, dimensions=getattr(ps, "embedding_dimensions", None))

    embedding_target = (
        pick_embedding_target(survivors, embedding_before.model)
        if embedding_orphaned and embedding_before is not None
        else None
    )
    if embedding_target is not None and embedding_before is not None:
        # A survivor serves the same model: the default moves to it with its
        # model and dimensions, so the stored vectors stay comparable and no
        # knowledge base is re-embedded.
        embedding_default = EmbeddingImpact(
            "reseeded",
            embedding_before,
            RouteTarget.of(embedding_target, embedding_before.model),
            embedding_env,
            model_kept=True,
            knowledge_base_count=await indexed_knowledge_bases(),
        )
    elif embedding_orphaned:
        fallback = await survivors_embedding_fallback(implicit_embedding_fallback(survivors))
        embedding_default = EmbeddingImpact(
            "cleared",
            embedding_before,
            None,
            embedding_env,
            fallback=RouteTarget.of(*fallback) if fallback is not None else None,
            knowledge_base_count=await indexed_knowledge_bases(),
        )
    elif current_fallback is not None and current_fallback[0].id == config.id:
        fallback = implicit_embedding_fallback(
            survivors, embedding_provider=stored_embedding[0], embedding_model=stored_embedding[1]
        )
        if not stored_embedding[1]:
            fallback = await survivors_embedding_fallback(fallback)
        embedding_default = EmbeddingImpact(
            "unchanged",
            embedding_before,
            embedding_before,
            embedding_env,
            fallback=RouteTarget.of(*fallback) if fallback is not None else None,
            knowledge_base_count=await indexed_knowledge_bases(),
            fallback_moves=True,
            previous_fallback=RouteTarget.of(*current_fallback),
        )
    else:
        embedding_default = EmbeddingImpact("unchanged", embedding_before, embedding_before, embedding_env)

    def reference_route(model_attr: str, reference_attr: str, *, inferred: bool = False) -> RouteImpact:
        model = getattr(ps, model_attr, None) if ps is not None else None
        reference = getattr(ps, reference_attr, None) if ps is not None else None
        orphaned = ps is not None and reference_orphaned(model, reference)
        before = target(None, reference, model)
        env_managed = model_attr in env_managed_fields
        moved_to = (
            pick_same_model_target(survivors, model, inferred=inferred) if orphaned else None
        )
        if moved_to is not None:
            # Another config offers the same model: the default keeps it there.
            return RouteImpact("reseeded", before, RouteTarget.of(moved_to, model), env_managed, model_kept=True)
        return _cleared_or_unchanged(orphaned, before, env_managed)

    # With no chat default stored, a call no agent or user choice decides goes
    # to ``router.implicit_chat_config``: deleting that config moves the
    # traffic with nothing stored changing, so the move is read off the same rule.
    previous_implicit_chat = (
        _implicit_chat_landing(configs) if agent_before is None and ga_before is None else None
    )
    implicit_chat_moves = (
        previous_implicit_chat is not None and previous_implicit_chat.provider_id == config.id
    )

    clause_args = {"provider_name": config.provider, **change}
    return DeletionImpact(
        deleted=RouteTarget.of(config, None),
        replacement=replacement,
        replacement_explicit=replacement_id is not None,
        default_replacement_id=candidates[0].id if candidates else None,
        candidates=[RouteTarget.of(cfg, offered_model(cfg)) for cfg in candidates],
        general_assistant=general_assistant_route,
        agent_default=agent_default,
        image_default=image_default,
        embedding_default=embedding_default,
        routing_model=reference_route("routing_model", "routing_provider_id"),
        # The fallback model's write path accepts a model resolved by name.
        fallback_model=reference_route("fallback_model", "fallback_provider_id", inferred=True),
        decision_model=reference_route("decision_model", "decision_provider_id"),
        agents=await _reference_group(db, Agent, Agent.name, orphaned_agent_clause(**clause_args)),
        users=await _reference_group(db, User, User.full_name, orphaned_user_clause(**clause_args)),
        chat_available=bool(chat_capable),
        unlisted=unlisted,
        implicit_chat_fallback=(
            _implicit_chat_landing(survivors) if implicit_chat_moves or not chat_capable else None
        ),
        implicit_chat_moves=implicit_chat_moves,
        previous_implicit_chat=previous_implicit_chat if implicit_chat_moves else None,
    )


def _implicit_chat_landing(configs: list[LLMProviderConfig]) -> RouteTarget | None:
    """Where ``get_llm_provider`` sends a chat call no stored default decides, among *configs*."""
    if not configs:
        return None
    landing = implicit_chat_config(oldest_first(configs))
    if landing is None or landing.provider in CHATLESS_PROVIDERS:
        return None
    provider_cls = PROVIDER_MAP.get(landing.provider)
    model = chat_default_model(landing) or getattr(provider_cls, "default_model", None)
    return RouteTarget.of(landing, model)


def apply_deletion_impact(
    impact: DeletionImpact,
    *,
    platform_settings: Any | None,
    general_assistant: Any | None,
) -> None:
    """Write the re-seeded routes, once the cleanup has nulled the orphaned ones.

    Every route the impact re-seeds was orphaned by the same predicate the
    cleanup applies, so this only ever fills a field the cleanup just emptied,
    and writes the provider id with it so the replacement stays pinned.
    """
    ps = platform_settings
    ga = general_assistant
    if impact.agent_default.action == "reseeded" and ps is not None:
        after = impact.agent_default.after
        ps.agent_default_provider = after.provider
        ps.agent_default_provider_id = after.provider_id
        ps.agent_default_model = after.model
    if impact.general_assistant.action == "reseeded" and ga is not None:
        after = impact.general_assistant.after
        ga.llm_provider = after.provider
        ga.llm_provider_id = after.provider_id
        ga.llm_model = after.model
    if impact.image_default.action == "reseeded" and ps is not None:
        after = impact.image_default.after
        ps.image_provider = after.provider
        ps.image_provider_id = after.provider_id
        ps.image_model = after.model
    embedding_after = impact.embedding_default.after
    if impact.embedding_default.action == "reseeded" and ps is not None and embedding_after is not None:
        # Same model, so ``embedding_dimensions`` is left as stored.
        after = embedding_after
        ps.embedding_provider = after.provider
        ps.embedding_provider_id = after.provider_id
        ps.embedding_model = after.model
    for model_attr, reference_attr, route in (
        ("routing_model", "routing_provider_id", impact.routing_model),
        ("fallback_model", "fallback_provider_id", impact.fallback_model),
        ("decision_model", "decision_provider_id", impact.decision_model),
    ):
        if route.action == "reseeded" and ps is not None and route.after is not None:
            setattr(ps, model_attr, route.after.model)
            setattr(ps, reference_attr, route.after.provider_id)


#: ``Activity.extra_data.notification_source`` of the notice below.
EMBEDDING_CLEARED_NOTIFICATION_SOURCE = "embedding_default_cleared"


async def notify_admins_embedding_cleared(db: AsyncSession, impact: DeletionImpact, *, reason: str) -> int:
    """Tell every active administrator that a deletion cleared the embedding default.

    Sent only when the embedding default ran on a model and no surviving
    config serves it, so it was cleared: until an administrator picks one
    again, embeddings fall back to another model or stop, and indexing stalls.
    One notification-bell entry per administrator, in *db* (committed with the
    deletion). Returns how many were written; never raises.
    """
    route = impact.embedding_default
    if route.action != "cleared" or route.before is None or not route.before.model:
        return 0
    try:
        from app.services.notify import notify_web_user

        admin_ids = (
            (await db.execute(select(User.id).where(User.role == "admin", User.is_active.is_(True)))).scalars().all()
        )
        deleted = impact.deleted.display_name or impact.deleted.provider or "a provider"
        title = (
            f"The embedding default {route.before.model} was cleared: pick the embedding model "
            "again in Settings > AI and models > Models"
        )
        summary = (
            f"Provider '{deleted}' was deleted ({reason}) and no remaining provider serves "
            f"{route.before.model}. Knowledge base indexing waits until an embedding default is set."
        )
        for admin_id in admin_ids:
            await notify_web_user(
                db,
                str(admin_id),
                title=title,
                summary=summary,
                source_label="AI and models",
                extra_data={
                    "notification_source": EMBEDDING_CLEARED_NOTIFICATION_SOURCE,
                    "field": "embedding_model",
                    "previous_model": route.before.model,
                    "deleted_provider_id": str(impact.deleted.provider_id),
                    "path": "/settings",
                },
            )
        return len(admin_ids)
    except Exception as exc:
        from app.core.utils import format_exception_message

        logger.warning("Embedding cleared notice could not be sent: %s", format_exception_message(exc))
        return 0


# ---------------------------------------------------------------------------
# Chunks embedded by a deleted config
# ---------------------------------------------------------------------------

# Rows per UPDATE. Nulling an indexed column is not a HOT update, so every row
# is re-inserted into each index of ``embeddings`` (the HNSW ones included):
# kept far below what the 30 s ``statement_timeout`` allows.
EMBEDDING_REFERENCE_BATCH_SIZE = 1000
# Wall-clock budget of one sweep; the next scheduler tick carries on.
EMBEDDING_REFERENCE_SWEEP_SECONDS = 15.0

# The distinct provider ids ``embeddings`` holds, read off its index one value
# at a time (a loose index scan), keeping those no config carries any more.
_DANGLING_EMBEDDING_PROVIDER_IDS = text(
    """
    WITH RECURSIVE ids(id) AS (
        (SELECT embedding_provider_id FROM embeddings
         WHERE embedding_provider_id IS NOT NULL
         ORDER BY embedding_provider_id LIMIT 1)
        UNION ALL
        SELECT (SELECT e.embedding_provider_id FROM embeddings e
                WHERE e.embedding_provider_id > ids.id
                ORDER BY e.embedding_provider_id LIMIT 1)
        FROM ids WHERE ids.id IS NOT NULL
    )
    SELECT id FROM ids
    WHERE id IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM llm_provider_configs c WHERE c.id = ids.id)
    """
)

# Rows another transaction holds are skipped, never waited on: an ingestion
# rewriting a document's chunks is not blocked, and cannot deadlock with this.
# Each batch checks again that no config carries the id, under its own
# snapshot: the list above was read at the start of the sweep.
_CLEAR_EMBEDDING_PROVIDER_BATCH = text(
    """
    UPDATE embeddings SET embedding_provider_id = NULL
    WHERE id IN (
        SELECT id FROM embeddings
        WHERE embedding_provider_id = :provider_id
          AND NOT EXISTS (
              SELECT 1 FROM llm_provider_configs c WHERE c.id = :provider_id
          )
        LIMIT :batch_size
        FOR UPDATE SKIP LOCKED
    )
    """
)


# Transaction-scoped, so the pooled connection gets its default back at commit.
_LOCAL_STATEMENT_TIMEOUT = text("SELECT set_config('statement_timeout', :timeout_ms, true)")


async def clear_deleted_embedding_provider_references(
    db: AsyncSession,
    *,
    batch_size: int = EMBEDDING_REFERENCE_BATCH_SIZE,
    time_budget: float = EMBEDDING_REFERENCE_SWEEP_SECONDS,
) -> int:
    """Null ``embeddings.embedding_provider_id`` where the config no longer exists.

    What the ``ON DELETE SET NULL`` cascade did inside the deletion, moved out
    of it: batches of *batch_size* rows, each committed, within *time_budget*
    seconds - every statement and every commit runs under a
    ``statement_timeout`` of what is left of it, and a cancelled one ends the
    sweep; the next tick carries on.
    Also covers a chunk an ingestion wrote for a config deleted meanwhile.
    Returns the number of rows cleared.
    """
    deadline = time.monotonic() + time_budget

    async def limit_to_budget():
        # PostgreSQL times each command, the COMMIT included, against the
        # setting in force when it starts: set again before every one of them.
        # Floored at 1 ms: 0 would mean no timeout at all to PostgreSQL.
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        await db.execute(_LOCAL_STATEMENT_TIMEOUT, {"timeout_ms": str(max(remaining_ms, 1))})

    async def bounded(statement, params=None):
        await limit_to_budget()
        return await db.execute(statement, params or {})

    async def bounded_commit():
        await limit_to_budget()
        await db.commit()

    cleared = 0
    provider_ids: list[uuid.UUID] = []
    try:
        if time.monotonic() >= deadline:
            return 0
        provider_ids = list((await bounded(_DANGLING_EMBEDDING_PROVIDER_IDS)).scalars())
        await bounded_commit()
        for provider_id in provider_ids:
            while time.monotonic() < deadline:
                result = await bounded(
                    _CLEAR_EMBEDDING_PROVIDER_BATCH,
                    {"provider_id": provider_id, "batch_size": batch_size},
                )
                await bounded_commit()
                rows = int(result.rowcount or 0)
                cleared += rows
                if rows == 0:
                    break
            else:
                break
    except DBAPIError as exc:
        if _sqlstate(exc) != _QUERY_CANCELED:
            raise
        await db.rollback()
        logger.warning(
            "Deleted-provider embedding reference cleanup reached its %.0f s budget "
            "after clearing %d chunk(s); the next scheduler tick carries on",
            time_budget,
            cleared,
        )
    if cleared:
        logger.info(
            "Cleared the provider reference of %d chunk(s) embedded by %d deleted "
            "provider config(s)",
            cleared,
            len(provider_ids),
        )
    return cleared
