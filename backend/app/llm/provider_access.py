# SPDX-License-Identifier: Apache-2.0

"""Who may use an LLM provider, and which provider a run uses.

An LLM provider config is platform-wide unless an admin restricts it to some
groups and users (``access_restricted`` + ``shared_group_ids`` /
``shared_user_ids``), so a team runs on its own API key and its spend lands on
its own account. This module is the ONE place that rule is read.

A provider serves the CALLING user, never the agent's owner: a shared agent
called by a member of team A runs on team A's provider, called by a member of
team B on team B's. Resolution for a caller:

* **usable** - every unrestricted provider, plus the restricted ones granted to
  the caller directly or through one of their groups. A caller with no account
  (the ``"anonymous"`` sentinel, an assignment run with no user, the platform
  MCP key) only uses unrestricted providers. Admins route like everyone else:
  managing a provider is not billing your own runs to it.
* **team first** - a provider granted to the caller wins over an unrestricted
  one for the same model (or, with no model named, the same vendor). A stored
  reference to an unrestricted provider (an agent's, the platform default's)
  is followed only when no granted provider can serve the model.
* **reserved models** - a model only restricted providers the caller is not
  granted list is not theirs: the tier that names it is skipped, as a
  non-chat model is, and resolution falls through to the next one.
* **withheld references** - a stored reference to a provider the caller may not
  use is read as absent, without the stale-reference warning a deleted one gets.

Failover stays inside the usable set: a run never fails over onto another
team's key. Embeddings and image generation keep their platform-level
configuration.

**Exclusions** are the other direction: a provider is never used for
the groups and users in its ``excluded_group_ids`` / ``excluded_user_ids``,
whatever its restriction grants and whether or not it is restricted at all. It
is how a deployment keeps the people a regulation forbids a vendor for (staff
of a given jurisdiction) off that vendor's models while everyone else keeps
them: the excluded people resolve to the providers left, typically a locally
hosted one, or are refused when none is left. An exclusion follows the PERSON
the AI work is for, so it also holds for the work that carries no user of its
own (compaction, memory consolidation, titles): those resolve as the user the
current context acts for (:func:`bind_acting_user`). Unlike a restriction, it
also holds for embeddings and image generation, which stay platform-level for
everyone else: their provider refuses the content of a person it excludes
(:func:`ensure_not_excluded`) rather than receive it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.models.group import GroupMember

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.llm_config import LLMProviderConfig


NO_USABLE_PROVIDER_MESSAGE = (
    "No LLM provider is available to you. An administrator must grant you or "
    "one of your groups access to a provider in Settings > AI and models > Models."
)

RESERVED_MODEL_MESSAGE = (
    "Model '{model}' is only offered by LLM providers reserved for other groups "
    "and users. An administrator can grant you access in Settings > AI and models > Models."
)

EXCLUDED_MODEL_MESSAGE = (
    "Model '{model}' is only offered by LLM providers that are not available to "
    "your account. An administrator decides who may use each provider in "
    "Settings > AI and models > Models."
)

EXCLUDED_CAPABILITY_MESSAGE = (
    "{capability} is not available to your account: the LLM provider configured "
    "for it is not used for you. An administrator can configure another provider "
    "for it in Settings > AI and models > Models."
)


def ai_disabled_message() -> str:
    """The refusal of a person whose group has AI switched off."""
    from app.services.group_ai_access import AI_DISABLED_BY_GROUP_MESSAGE

    return AI_DISABLED_BY_GROUP_MESSAGE


class ProviderNotUsableError(ValueError):
    """What a request needs is on providers the caller may not use, worded for them.

    A ``ValueError`` so every existing handler still catches it; a handler that
    words its own refusal lets this one through, since "no access" and "no
    such model" call for different remedies.
    """


def canonical_id(value: object) -> str | None:
    """*value* as a canonical UUID string, or ``None`` when it is not one."""
    if value is None:
        return None
    try:
        return str(value if isinstance(value, uuid.UUID) else uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


def canonical_ids(values: Iterable[object] | None) -> list[str]:
    """The canonical UUID strings of *values*, de-duplicated in order."""
    out: list[str] = []
    for value in values or ():
        cid = canonical_id(value)
        if cid is not None and cid not in out:
            out.append(cid)
    return out


@dataclass(frozen=True)
class ProviderAudience:
    """Who is asking: a user id and the groups they belong to.

    ``user_id`` is ``None`` for a caller with no account. ``ai_disabled`` is
    set when one of their groups switches AI off (``Group.ai_disabled``): every
    provider is then excluded for them, whatever it grants.
    """

    user_id: str | None = None
    group_ids: frozenset[str] = frozenset()
    ai_disabled: bool = False


NO_ACCOUNT = ProviderAudience()


def is_restricted(config: Any) -> bool:
    return getattr(config, "access_restricted", False) is True


def has_exclusions(config: Any) -> bool:
    """Whether *config* is never used for some groups or users."""
    return bool(getattr(config, "excluded_group_ids", None) or getattr(config, "excluded_user_ids", None))


def needs_groups(configs: Iterable[Any]) -> bool:
    """Whether resolving *configs* for someone depends on their groups."""
    return any(is_restricted(c) or getattr(c, "excluded_group_ids", None) for c in configs)


def is_excluded(config: Any, audience: ProviderAudience) -> bool:
    """Whether *config* is never used for *audience*, whatever it grants."""
    person = audience.user_id
    if person is None:
        return False
    if audience.ai_disabled:
        return True
    if person in canonical_ids(getattr(config, "excluded_user_ids", None)):
        return True
    return bool(set(canonical_ids(getattr(config, "excluded_group_ids", None))) & audience.group_ids)


def provider_usable_by(config: Any, audience: ProviderAudience) -> bool:
    """Whether *audience* may run on *config*.

    An exclusion wins over every grant: a member of an excluded group is
    excluded even when another of their groups, or they themselves, are granted.
    """
    if is_excluded(config, audience):
        return False
    if not is_restricted(config):
        return True
    if audience.user_id is None:
        return False
    if audience.user_id in canonical_ids(getattr(config, "shared_user_ids", None)):
        return True
    groups = set(canonical_ids(getattr(config, "shared_group_ids", None)))
    return bool(groups & audience.group_ids)


_acting_user: ContextVar[str | None] = ContextVar("llm_acting_user", default=None)


def bind_acting_user(user_id: object) -> None:
    """Record that the AI work of the current context is done for *user_id*.

    Bound where a person is known: an authenticated request
    (``deps.get_current_user_ungated``), the web chat's send, every model
    resolution for a user (``router.get_llm_for_user``), an assignment run
    (scoped by its adapter with :func:`acting_for`, as sub-agent tasks and
    flow runs are). asyncio context is per task and inherited by the tasks it
    spawns, so the binding reaches the calls made later on the same work with
    no user of their own. Ignores a value that is not a UUID
    (``"anonymous"``, ``"system"``): those bind nobody and leave the current
    binding as it is.
    """
    uid = canonical_id(user_id)
    if uid is not None:
        _acting_user.set(uid)


def acting_user_id() -> str | None:
    """The person the current context's AI work is for, if one is bound."""
    return _acting_user.get()


@contextmanager
def acting_for(user_id: object) -> Iterator[None]:
    """Run the enclosed AI work for *user_id*, then restore the binding.

    For background work that serves several people in turn (memory
    maintenance walks one user scope after the other). ``None`` - or any
    value that is not a UUID - binds nobody: the work then carries no person's
    exclusions, as agent-level work does.
    """
    token = _acting_user.set(canonical_id(user_id))
    try:
        yield
    finally:
        _acting_user.reset(token)


async def load_provider_audience(db: AsyncSession, user_id: object, *, with_groups: bool = True) -> ProviderAudience:
    """The audience of *user_id*; its groups only when *with_groups*.

    The group lookup is skipped when no provider is restricted or excludes a
    group, which is every install that does not use either feature. A call
    with no user of its own made for someone (:func:`bind_acting_user`: the
    compaction of their conversation, their memory) is resolved as theirs:
    their grants, their exclusions. With nobody bound, it is a caller
    with no account.

    The groups are loaded whenever a group switches AI off, *with_groups* or not
    (``group_ai_access``): on an install where none does, that is one cached
    empty set and no query.
    """
    from app.services.group_ai_access import disabled_group_ids

    uid = canonical_id(user_id) or acting_user_id()
    if uid is None:
        return NO_ACCOUNT
    disabled = await disabled_group_ids(db)
    if not with_groups and not disabled:
        return ProviderAudience(user_id=uid)
    rows = await db.execute(select(GroupMember.group_id).where(GroupMember.user_id == uuid.UUID(uid)))
    group_ids = frozenset(str(g) for g in rows.scalars().all())
    return ProviderAudience(user_id=uid, group_ids=group_ids, ai_disabled=bool(disabled & group_ids))


async def provider_id_usable_by(db: AsyncSession, provider_id: object, user_id: object) -> bool:
    """Whether *user_id* may run on the provider config *provider_id*.

    A config that does not exist withholds nothing: whether it exists is the
    caller's own check.
    """
    from app.models.llm_config import LLMProviderConfig

    cid = canonical_id(provider_id)
    if cid is None:
        return True
    config = (
        await db.execute(select(LLMProviderConfig).where(LLMProviderConfig.id == uuid.UUID(cid)))
    ).scalar_one_or_none()
    if config is None or not (is_restricted(config) or has_exclusions(config)):
        return True
    return provider_usable_by(config, await load_provider_audience(db, user_id))


async def ensure_not_excluded(db: AsyncSession, config: Any, capability: str, user_id: object = None) -> None:
    """Refuse to send a person's content to a provider that excludes them.

    For a platform-level capability (embeddings, image generation), which
    restrictions leave alone - a knowledge base is shared, so its vectors come
    from one model - but an exclusion does not: the content belongs to
    *user_id*, else to the person the context acts for. Raises
    :class:`ProviderNotUsableError` worded with *capability*.
    """
    if config is None:
        return
    audience = await load_provider_audience(
        db, user_id, with_groups=bool(getattr(config, "excluded_group_ids", None))
    )
    if audience.ai_disabled:
        raise ProviderNotUsableError(ai_disabled_message())
    if has_exclusions(config) and is_excluded(config, audience):
        raise ProviderNotUsableError(EXCLUDED_CAPABILITY_MESSAGE.format(capability=capability))


async def excluded_user_ids(db: AsyncSession, provider_id: object) -> frozenset[str]:
    """Every user the provider config *provider_id* is never used for.

    Those it names, and the members of the groups it names. For background
    work over many people's content at once (re-embedding stored memories),
    which skips theirs instead of resolving each row on its own.
    """
    from app.models.llm_config import LLMProviderConfig

    cid = canonical_id(provider_id)
    if cid is None:
        return frozenset()
    config = (
        await db.execute(select(LLMProviderConfig).where(LLMProviderConfig.id == uuid.UUID(cid)))
    ).scalar_one_or_none()
    if config is None or not has_exclusions(config):
        return frozenset()
    people = set(canonical_ids(getattr(config, "excluded_user_ids", None)))
    groups = canonical_ids(getattr(config, "excluded_group_ids", None))
    if groups:
        rows = await db.execute(
            select(GroupMember.user_id).where(GroupMember.group_id.in_([uuid.UUID(g) for g in groups]))
        )
        people.update(str(u) for u in rows.scalars().all())
    return frozenset(people)


RESTRICTED_PROVIDER_MESSAGE = (
    "The provider selected for {label} is restricted to other groups and users, so you cannot select it."
)


def _lists(config: Any, model: str) -> bool:
    return model in (getattr(config, "available_models", None) or [])


def _newest(configs: Sequence[Any]) -> Any | None:
    from app.llm.router import _config_recency_key

    return max(configs, key=_config_recency_key) if configs else None


@dataclass
class ProviderRouting:
    """The providers one caller may use, and how a run picks among them.

    Every list keeps the order of the configs it was built from (Settings
    order, oldest first) so an implicit pick stays deterministic.
    """

    audience: ProviderAudience
    configs: list[Any]
    usable: list[Any] = field(default_factory=list)
    granted: list[Any] = field(default_factory=list)
    open: list[Any] = field(default_factory=list)
    withheld_ids: frozenset[str] = frozenset()
    # The withheld ones that exclude the caller, not merely reserved
    # for others: asking for access does not help there.
    excluded_ids: frozenset[str] = frozenset()

    def no_usable_message(self) -> str:
        """Why this caller has no provider at all."""
        return ai_disabled_message() if self.audience.ai_disabled else NO_USABLE_PROVIDER_MESSAGE

    def reserved_model_message(self, model: str) -> str:
        """Why *model*, which :meth:`is_reserved`, is refused to this caller."""
        if self.audience.ai_disabled:
            return ai_disabled_message()
        excluded_only = all(
            self.is_withheld(c.id) and canonical_id(c.id) in self.excluded_ids
            for c in self.configs
            if _lists(c, model)
        )
        template = EXCLUDED_MODEL_MESSAGE if excluded_only else RESERVED_MODEL_MESSAGE
        return template.format(model=model)

    @property
    def team_first(self) -> list[Any]:
        """The usable configs, the ones granted to the caller first."""
        return [*self.granted, *self.open]

    def is_withheld(self, config_id: object) -> bool:
        """Whether *config_id* names a provider this caller may not use."""
        cid = canonical_id(config_id)
        return cid is not None and cid in self.withheld_ids

    def is_reserved(self, model: str | None) -> bool:
        """Whether only providers the caller may not use list *model*."""
        if not model or not self.withheld_ids:
            return False
        if any(_lists(c, model) for c in self.usable):
            return False
        return any(_lists(c, model) for c in self.configs if self.is_withheld(c.id))

    def team_config_for(self, model: str | None) -> Any | None:
        """The granted config that should serve *model*, if any.

        One that lists the model; else, when no unrestricted config lists it
        either, one of the vendor its name belongs to.
        """
        if not self.granted or not model:
            return None
        listing = [c for c in self.granted if _lists(c, model)]
        if listing:
            return _newest(listing)
        if any(_lists(c, model) for c in self.open):
            return None
        from app.llm.router import _infer_provider_from_model

        vendor = _infer_provider_from_model(model)
        return _newest([c for c in self.granted if c.provider == vendor])

    def owner_for(self, model: str | None) -> Any | None:
        """Which usable config owns *model*: the team's first."""
        if not model or self.is_reserved(model):
            return None
        team = self.team_config_for(model)
        if team is not None:
            return team
        from app.llm.router import _resolve_model_owner_config

        return _resolve_model_owner_config(self.usable, model)

    def prefer_team(self, config: Any | None, model: str | None) -> Any | None:
        """*config*, or the granted config that should serve its run instead."""
        if config is None or not self.granted:
            return config
        if any(c.id == config.id for c in self.granted):
            return config
        if model:
            return self.team_config_for(model) or config
        return _newest([c for c in self.granted if c.provider == config.provider]) or config

    def choose(self, provider: str, model: str | None = None) -> Any | None:
        """The usable config of *provider* for *model*, the team's first."""
        from app.llm.router import choose_provider_config

        team = [c for c in self.granted if c.provider == provider]
        if team and (
            not model
            or any(_lists(c, model) for c in team)
            or not any(_lists(c, model) for c in self.open if c.provider == provider)
        ):
            return choose_provider_config(team, provider, model)
        return choose_provider_config(self.usable, provider, model)


def routing_for(audience: ProviderAudience, configs: Iterable[Any]) -> ProviderRouting:
    """The routing of *audience* over *configs* (pure: no database)."""
    configs = list(configs)
    routing = ProviderRouting(audience=audience, configs=configs)
    withheld: set[str] = set()
    excluded: set[str] = set()
    for cfg in configs:
        if not provider_usable_by(cfg, audience):
            cid = canonical_id(cfg.id)
            if cid is not None:
                withheld.add(cid)
                if is_excluded(cfg, audience):
                    excluded.add(cid)
            continue
        routing.usable.append(cfg)
        (routing.granted if is_restricted(cfg) else routing.open).append(cfg)
    routing.withheld_ids = frozenset(withheld)
    routing.excluded_ids = frozenset(excluded)
    return routing


async def provider_routing(db: AsyncSession, user_id: object, configs: Iterable[Any]) -> ProviderRouting:
    """The routing of *user_id* over *configs*.

    A call with no user of its own is routed for the person the context acts
    for, if any (:func:`bind_acting_user`), so that person's exclusions hold.
    """
    configs = list(configs)
    audience = await load_provider_audience(db, user_id, with_groups=needs_groups(configs))
    return routing_for(audience, configs)


def usable_model_ids(routing: ProviderRouting) -> set[str]:
    """Every model a usable config lists."""
    return {m for c in routing.usable for m in (getattr(c, "available_models", None) or []) if m}


async def usable_provider_configs(
    db: AsyncSession, user_id: object, providers: Iterable[str]
) -> list[LLMProviderConfig]:
    """The configs of *providers* holding an API key that *user_id* may use.

    For a capability a vendor's key serves directly rather than a model a run
    resolves (speech, web search). Granted to the caller first, then the
    unrestricted ones; within each, the admin's default first, then Settings
    order.
    """
    from app.llm.router import select_provider_configs
    from app.models.llm_config import LLMProviderConfig

    rows = await db.execute(
        select_provider_configs().where(
            LLMProviderConfig.provider.in_(list(providers)),
            LLMProviderConfig.api_key_enc.isnot(None),
        )
    )
    routing = await provider_routing(db, user_id, rows.scalars().all())
    return sorted(routing.team_first, key=lambda c: (not is_restricted(c), not c.is_default))


MAX_AUDIENCE_IDS = 500


class ProviderAccessError(ValueError):
    """A provider access write the platform refuses, worded for the admin."""


def _parse_ids(values: Iterable[object], label: str) -> list[str]:
    ids: list[str] = []
    for value in values:
        cid = canonical_id(value)
        if cid is None:
            raise ProviderAccessError(f"Invalid {label} id: {str(value)[:64]!r}.")
        if cid not in ids:
            ids.append(cid)
            if len(ids) > MAX_AUDIENCE_IDS:
                raise _too_many()
    return ids


def _too_many() -> ProviderAccessError:
    return ProviderAccessError(f"A provider may name at most {MAX_AUDIENCE_IDS} groups and users in all.")


async def _missing_ids(db: AsyncSession, model: Any, ids: list[str]) -> list[str]:
    if not ids:
        return []
    rows = await db.execute(
        select(model.id)
        .where(model.id.in_([uuid.UUID(i) for i in ids]))
        .with_for_update(read=True, key_share=True)
    )
    found = {str(i) for i in rows.scalars().all()}
    return [i for i in ids if i not in found]


async def apply_provider_access(
    db: AsyncSession,
    config: LLMProviderConfig,
    *,
    access_restricted: bool | None,
    group_ids: Iterable[object] | None,
    user_ids: Iterable[object] | None,
    excluded_group_ids: Iterable[object] | None = None,
    excluded_user_ids: Iterable[object] | None = None,
) -> dict[str, Any] | None:
    """Write who may use *config*; ``None`` arguments keep what is stored.

    Raises :class:`ProviderAccessError` for an id that is not a UUID, for more
    than :data:`MAX_AUDIENCE_IDS` groups and users in all (grants and
    exclusions each), or for an id the write adds that names no group / user
    (one already stored stays, so a grantee deleted since never blocks the
    next save). Returns the change for the audit trail, ``None`` when nothing
    changed. The grant lists are kept while the provider is open, so
    restricting it again restores its audience; the exclusion lists apply
    either way.

    An added group or user is locked ``FOR KEY SHARE`` until the write
    commits, and a deletion locks the group or user before it prunes the
    grants (``helpers.prune_group_grants`` / ``prune_user_grants``): a write
    racing the deletion either commits first and is pruned, or waits and is
    refused. The stored lists are current, since every caller reads *config*
    under ``lock_provider_configs``, which the prune's row lock waits on.
    """
    from app.models.group import Group
    from app.models.user import User

    lists = {
        "shared_group_ids": (group_ids, Group, "group"),
        "shared_user_ids": (user_ids, User, "user"),
        "excluded_group_ids": (excluded_group_ids, Group, "group"),
        "excluded_user_ids": (excluded_user_ids, User, "user"),
    }
    before: dict[str, Any] = {"access_restricted": is_restricted(config)}
    for column in lists:
        before[column] = canonical_ids(getattr(config, column, None))
    after = dict(before)
    if access_restricted is not None:
        after["access_restricted"] = bool(access_restricted)
    for column, (values, model, label) in lists.items():
        if values is None:
            continue
        ids = _parse_ids(values, label)
        added = [cid for cid in ids if cid not in before[column]]
        missing = await _missing_ids(db, model, added)
        if missing:
            raise ProviderAccessError(f"Unknown {label}: {missing[0]}.")
        after[column] = ids
    if after == before:
        return None
    if len(after["shared_group_ids"]) + len(after["shared_user_ids"]) > MAX_AUDIENCE_IDS:
        raise _too_many()
    if len(after["excluded_group_ids"]) + len(after["excluded_user_ids"]) > MAX_AUDIENCE_IDS:
        raise _too_many()
    config.access_restricted = after["access_restricted"]
    for column in lists:
        setattr(config, column, after[column] or None)
    return {"before": before, "after": after}


async def providers_excluding_group(db: AsyncSession, group_id: object) -> list[str]:
    """The names of the provider configs that exclude *group_id*.

    A group deletion refuses while it is not empty: deleting the group would
    silently lift the exclusion for all its members, which is a decision about
    who may reach a vendor, not housekeeping. Call it with the group row
    locked (``FOR UPDATE``), so an access write adding the group - which locks
    it ``FOR KEY SHARE`` - either committed before and is seen, or waits.
    """
    from app.llm.router import select_provider_configs

    gid = canonical_id(group_id)
    if gid is None:
        return []
    rows = await db.execute(select_provider_configs())
    return [
        config.display_name or config.provider
        for config in rows.scalars().all()
        if gid in canonical_ids(getattr(config, "excluded_group_ids", None))
    ]


async def carry_group_exclusions(db: AsyncSession, from_group_id: object, to_group_id: object) -> int:
    """Point every exclusion of *from_group_id* at *to_group_id*; rows changed.

    For a group merged into another (SAML group sync, ``auth_sso``): its
    members move to the other group, so must its exclusions, or deleting the
    merged group would silently give them back the provider.
    """
    from app.models.llm_config import LLMProviderConfig

    source, target = canonical_id(from_group_id), canonical_id(to_group_id)
    if source is None or target is None:
        return 0
    rows = await db.execute(
        select(LLMProviderConfig)
        .where(LLMProviderConfig.excluded_group_ids.isnot(None))
        .order_by(LLMProviderConfig.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    changed = 0
    for config in rows.scalars().all():
        ids = canonical_ids(config.excluded_group_ids)
        if source in ids:
            config.excluded_group_ids = canonical_ids(target if i == source else i for i in ids)
            changed += 1
    return changed


async def prune_user_exclusions(db: AsyncSession, user_id: object) -> int:
    """Drop *user_id* from every provider's ``excluded_user_ids``; rows changed.

    For a user deletion (``helpers.prune_user_grants``): the account can no
    longer act, so the exclusion naming it excludes no one.
    """
    from app.models.llm_config import LLMProviderConfig

    uid = canonical_id(user_id)
    if uid is None:
        return 0
    rows = await db.execute(
        select(LLMProviderConfig)
        .where(LLMProviderConfig.excluded_user_ids.isnot(None))
        .order_by(LLMProviderConfig.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    changed = 0
    for config in rows.scalars().all():
        ids = canonical_ids(config.excluded_user_ids)
        if uid in ids:
            config.excluded_user_ids = [i for i in ids if i != uid] or None
            changed += 1
    return changed
