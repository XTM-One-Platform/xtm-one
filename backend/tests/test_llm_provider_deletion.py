"""Deleting a provider names and pins what replaces it.

Deleting a config used to null every reference to it and leave each inherited
route to whichever surviving config an unordered query returned first. These
tests pin the deterministic replacement rule, what each route becomes, the
agreement between the preview and what the delete applies, and the ordered
implicit fallback. The SQL half (which stored rows a deletion touches) is
covered against a real database in ``test_llm_provider_deletion_postgres.py``.
"""

from __future__ import annotations

import dataclasses
import inspect
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.llm import router
from app.llm.openai_provider import OpenAIProvider
from app.models.agent import Agent
from app.models.llm_config import LLMProviderConfig
from app.models.user import User
from app.services.llm_provider_deletion import (
    MAX_LISTED_NAMES,
    ReplacementError,
    apply_deletion_impact,
    compute_deletion_impact,
    listed_default_model,
    pick_image_target,
    pick_replacement,
    replacement_candidates,
    replacement_model_for,
)

_T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _cfg(
    provider: str = "openai",
    *,
    models: list[str] | None = None,
    default: str | None = None,
    created: int = 0,
    display_name: str | None = None,
    cid: uuid.UUID | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=cid or uuid.uuid4(),
        provider=provider,
        display_name=display_name,
        default_model=default,
        available_models=models,
        created_at=_T0 + timedelta(minutes=created),
    )


def _ps(**overrides) -> SimpleNamespace:
    fields = {
        "agent_default_provider": None,
        "agent_default_provider_id": None,
        "agent_default_model": None,
        "embedding_provider": None,
        "embedding_provider_id": None,
        "embedding_model": None,
        "image_provider": None,
        "image_provider_id": None,
        "image_model": None,
        "routing_model": None,
        "routing_provider_id": None,
        "fallback_model": None,
        "fallback_provider_id": None,
        "decision_model": None,
        "decision_provider_id": None,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _ga(provider=None, provider_id=None, model=None) -> SimpleNamespace:
    return SimpleNamespace(llm_provider=provider, llm_provider_id=provider_id, llm_model=model)


# ── The replacement rule ─────────────────────────────────────────────────────


def test_candidates_are_the_chat_capable_survivors_oldest_first():
    newest = _cfg("anthropic", models=["claude-sonnet-5"], created=3)
    oldest = _cfg("openai", models=["gpt-5.1"], created=1)
    middle = _cfg("mistral", models=["mistral-large-latest"], created=2)

    ordered = replacement_candidates([newest, oldest, middle])

    assert [c.id for c in ordered] == [oldest.id, middle.id, newest.id]
    assert pick_replacement([newest, middle, oldest]).id == oldest.id


def test_a_creation_tie_is_broken_by_id_like_the_settings_list():
    second = _cfg("openai", models=["gpt-5.1"], cid=uuid.UUID(int=2))
    first = _cfg("anthropic", models=["claude-sonnet-5"], cid=uuid.UUID(int=1))

    assert [c.id for c in replacement_candidates([second, first])] == [first.id, second.id]


def test_embedding_only_and_image_only_configs_never_take_over_chat():
    tei = _cfg("tei", models=["bge-m3"], created=0)
    image_only = _cfg("openai", models=["gpt-image-1"], created=1)
    chat = _cfg("anthropic", models=["claude-sonnet-5"], created=2)

    assert [c.id for c in replacement_candidates([tei, image_only, chat])] == [chat.id]
    assert pick_replacement([tei, image_only, chat]).id == chat.id


def test_no_chat_capable_survivor_means_no_replacement():
    assert pick_replacement([]) is None
    assert pick_replacement([_cfg("tei", models=["bge-m3"])]) is None


def test_an_explicit_replacement_wins_over_the_rule():
    oldest = _cfg("anthropic", models=["claude-sonnet-5"], created=0)
    chosen = _cfg("openai", models=["gpt-5.1"], created=5)

    assert pick_replacement([oldest, chosen], replacement_id=chosen.id).id == chosen.id


@pytest.mark.parametrize("which", ["deleted", "unknown", "embedding_only", "image_only"])
def test_an_unusable_explicit_replacement_is_refused(which):
    deleted = _cfg("openai", models=["gpt-5.1"])
    tei = _cfg("tei", models=["bge-m3"])
    image_only = _cfg("openai", models=["gpt-image-1"])
    chat = _cfg("anthropic", models=["claude-sonnet-5"])
    requested = {
        "deleted": deleted.id,
        "unknown": uuid.uuid4(),
        "embedding_only": tei.id,
        "image_only": image_only.id,
    }[which]

    with pytest.raises(ReplacementError):
        pick_replacement([tei, image_only, chat], replacement_id=requested, deleted_id=deleted.id)


def test_the_replacement_model_is_the_config_default_chat_model():
    assert router.chat_default_model(_cfg(models=["gpt-4o", "gpt-5.1"], default="gpt-5.1")) == "gpt-5.1"
    # A non-chat declared default is skipped, as ``build_provider`` does.
    assert router.chat_default_model(_cfg(models=["gpt-image-1", "gpt-4o"], default="gpt-image-1")) == "gpt-4o"
    assert router.chat_default_model(_cfg(models=["gpt-image-1"])) is None
    # No catalog at all: the provider class default is what a model-less call runs.
    assert router.chat_default_model(_cfg("openai", models=None)) == OpenAIProvider.default_model


def test_a_route_keeps_its_model_when_the_replacement_offers_it():
    replacement = _cfg("openai", models=["gpt-4o", "gpt-5.1"], default="gpt-4o")

    assert replacement_model_for(replacement, "gpt-5.1") == ("gpt-5.1", True)


def test_a_route_falls_back_to_the_replacement_default_model():
    replacement = _cfg("openai", models=["gpt-4o", "gpt-5.1"], default="gpt-4o")

    assert replacement_model_for(replacement, "claude-sonnet-5") == ("gpt-4o", False)
    assert replacement_model_for(replacement, None) == ("gpt-4o", False)
    # Only a listed chat model is kept: an image model is never a chat route's model.
    image_listed = _cfg("openai", models=["gpt-4o", "gpt-image-1"], default="gpt-4o")
    assert replacement_model_for(image_listed, "gpt-image-1") == ("gpt-4o", False)
    # A catalog-less config lists nothing to land on, even a model its class
    # could serve: it is never picked for a route carrying a model.
    assert replacement_model_for(_cfg("openai", models=None), "gpt-5.1") == (None, False)


def test_a_route_that_keeps_nothing_lands_on_a_model_the_replacement_lists():
    # Its own default model when it lists it...
    assert replacement_model_for(_cfg("openrouter", models=["a/x", "b/y"], default="b/y"), "gpt-5.1") == (
        "b/y",
        False,
    )
    # ...else its first listed chat model: a default left out of the catalog is
    # one the router would re-route to that model's owner.
    gateway = _cfg("openrouter", models=["gpt-image-1", "openai/gpt-4o", "openai/gpt-5.1"], default="gpt-4o")
    assert replacement_model_for(gateway, "claude-sonnet-5") == ("openai/gpt-4o", False)
    assert listed_default_model(gateway) == "openai/gpt-4o"


def test_a_config_listing_no_model_is_offered_only_for_a_model_less_route():
    listing = _cfg("anthropic", models=["claude-sonnet-5"], created=1)
    catalog_less = _cfg("azure_openai", models=None, created=0)

    assert replacement_candidates([catalog_less, listing]) == [catalog_less, listing]
    assert replacement_candidates([catalog_less, listing], needs_model=True) == [listing]
    assert pick_replacement([catalog_less, listing], needs_model=True) is listing
    with pytest.raises(ReplacementError, match="lists no model"):
        pick_replacement([catalog_less, listing], replacement_id=catalog_less.id, needs_model=True)


# ── The image rule ───────────────────────────────────────────────────────────


def test_the_image_default_follows_the_replacement_when_it_can_serve_the_model():
    replacement = _cfg("openai", models=["gpt-5.1"], created=4)
    older_openai = _cfg("openai", models=["gpt-4o"], created=0)

    target = pick_image_target([older_openai, replacement], replacement=replacement, image_model="gpt-image-2")

    assert target == (replacement, "gpt-image-2")


def test_the_image_default_keeps_its_model_on_another_survivor_that_serves_it():
    replacement = _cfg("anthropic", models=["claude-sonnet-5"], created=0)
    openai = _cfg("openai", models=["gpt-4o"], created=1)

    assert pick_image_target([replacement, openai], replacement=replacement, image_model="gpt-image-2") == (
        openai,
        "gpt-image-2",
    )


def test_the_image_default_falls_back_to_a_builtin_image_model():
    replacement = _cfg("anthropic", models=["claude-sonnet-5"], created=0)
    gemini = _cfg("gemini", models=["gemini-3.8-flash"], created=1)

    assert pick_image_target([replacement, gemini], replacement=replacement, image_model="gpt-image-2") == (
        gemini,
        router.IMAGE_DEFAULTS["gemini"],
    )


def test_no_image_capable_survivor_clears_the_image_default():
    survivors = [_cfg("anthropic", models=["claude-sonnet-5"]), _cfg("tei", models=["gpt-image-2"])]

    assert pick_image_target(survivors, replacement=survivors[0], image_model="gpt-image-2") is None


def test_a_config_listing_the_image_model_but_unable_to_generate_images_is_skipped():
    """A LiteLLM proxy can list ``gpt-image-1`` among its models, but its provider
    class does not generate images: moving the image default there would break
    image generation, where the surviving OpenAI config can serve it."""
    replacement = _cfg("litellm", models=["gpt-4o", "gpt-image-1"], created=0)
    openai = _cfg("openai", models=["gpt-4o"], created=1)

    assert pick_image_target([replacement, openai], replacement=replacement, image_model="gpt-image-1") == (
        openai,
        "gpt-image-1",
    )
    assert pick_image_target([replacement], replacement=replacement, image_model="gpt-image-1") is None


def test_an_env_owned_image_model_is_only_kept_never_switched():
    """With ``DEFAULT_IMAGE_MODEL`` set alone, the boot seed rewrites the model and
    not its provider: a move to another model would come back at the next restart
    as that model on a provider that cannot serve it."""
    replacement = _cfg("anthropic", models=["claude-sonnet-5"], created=0)
    gemini = _cfg("gemini", models=["gemini-3.8-flash"], created=1)
    openai = _cfg("openai", models=["gpt-4o"], created=2)

    assert (
        pick_image_target([replacement, gemini], replacement=replacement, image_model="gpt-image-1", keep_model_only=True)
        is None
    )
    assert pick_image_target(
        [replacement, gemini, openai], replacement=replacement, image_model="gpt-image-1", keep_model_only=True
    ) == (openai, "gpt-image-1")


@pytest.mark.asyncio
async def test_the_impact_clears_an_env_owned_image_model_it_cannot_keep():
    fleet = _fleet()
    gemini = _cfg("gemini", models=["gemini-3.8-flash"], created=1)
    db = _Session([fleet.deleted, fleet.anthropic, gemini])

    def image_route(env_managed_fields):
        return compute_deletion_impact(
            db,
            fleet.deleted,
            platform_settings=fleet.ps,
            general_assistant=fleet.ga,
            env_managed_fields=env_managed_fields,
        )

    assert (await image_route({"image_model"})).image_default.action == "cleared"
    # Owned by the environment as a whole pair, the move is reverted as a whole.
    both = await image_route({"image_provider", "image_provider_id", "image_model"})
    assert (both.image_default.action, both.image_default.after.provider_id) == ("reseeded", gemini.id)


# ── The embedding fallback the preview names ─────────────────────────────────


def test_embedding_fallback_is_the_preferred_provider_then_its_newest_config():
    old_openai = _cfg("openai", models=["gpt-4o"], created=0)
    new_openai = _cfg("openai", models=["gpt-5.1"], created=2)
    gemini = _cfg("gemini", models=["gemini-3.8-flash"], created=1)

    config, model = router.implicit_embedding_fallback(router.oldest_first([gemini, new_openai, old_openai]))

    assert config is new_openai
    assert model == router.EMBEDDING_DEFAULTS["openai"]
    assert router.implicit_embedding_fallback([_cfg("anthropic", models=["claude-sonnet-5"])]) is None


# ── The impact, end to end over a stub session ──────────────────────────────


class _Result:
    def __init__(self, rows=None, scalar=None):
        self._rows = rows or []
        self._scalar = scalar

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._rows))

    def scalar_one(self):
        return self._scalar


class _Session:
    """Answers the impact's queries by what they select.

    The WHERE clauses are not evaluated here - *agents* / *users* are the rows
    the orphan clause would match; the Postgres test checks the clauses.
    """

    def __init__(self, configs, *, agents=(), users=(), indexed_kbs=0):
        self.configs = list(configs)
        self.agents = list(agents)
        self.users = list(users)
        self.indexed_kbs = indexed_kbs
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        column = statement.column_descriptions[0]
        tables = {table.name for table in statement.get_final_froms()}
        if column["entity"] is LLMProviderConfig and column["name"] == "LLMProviderConfig":
            return _Result(rows=router.oldest_first(self.configs))
        if column["name"] == "count":
            if "agents" in tables:
                return _Result(scalar=len(self.agents))
            if "users" in tables:
                return _Result(scalar=len(self.users))
            if "knowledge_bases" in tables:
                return _Result(scalar=self.indexed_kbs)
        if column["entity"] is Agent:
            return _Result(rows=sorted(self.agents)[:MAX_LISTED_NAMES])
        if column["entity"] is User:
            return _Result(rows=sorted(self.users)[:MAX_LISTED_NAMES])
        raise AssertionError(f"unexpected statement: {statement}")


def _fleet():
    """A deleted OpenAI config serving every route, and three survivors."""
    tei = _cfg("tei", models=["bge-m3"], created=-1)
    anthropic = _cfg("anthropic", models=["claude-sonnet-5"], created=0, display_name="Claude")
    backup = _cfg("openai", models=["gpt-4o"], created=1, display_name="Backup")
    deleted = _cfg(
        "openai",
        models=["gpt-5.1", "gpt-image-1", "text-embedding-3-small"],
        default="gpt-5.1",
        created=2,
        display_name="Production",
    )
    ps = _ps(
        agent_default_provider="openai",
        agent_default_provider_id=deleted.id,
        agent_default_model="gpt-5.1",
        image_provider="openai",
        image_provider_id=deleted.id,
        image_model="gpt-image-1",
        embedding_provider="openai",
        embedding_provider_id=deleted.id,
        embedding_model="text-embedding-3-small",
        routing_model="gpt-5.1",
        routing_provider_id=deleted.id,
        decision_model="clef-flash",
        decision_provider_id=deleted.id,
    )
    ga = _ga("openai", deleted.id, "gpt-5.1")
    return SimpleNamespace(tei=tei, anthropic=anthropic, backup=backup, deleted=deleted, ps=ps, ga=ga)


@pytest.mark.asyncio
async def test_every_route_of_the_deleted_config_is_named_before_anything_is_written():
    fleet = _fleet()
    db = _Session(
        [fleet.deleted, fleet.backup, fleet.anthropic, fleet.tei],
        agents=["Triage", "Analyst"],
        users=["Ada"],
        indexed_kbs=3,
    )

    impact = await compute_deletion_impact(
        db, fleet.deleted, platform_settings=fleet.ps, general_assistant=fleet.ga
    )

    # The rule: the oldest chat-capable survivor (the older TEI config cannot chat).
    assert impact.replacement.provider_id == fleet.anthropic.id
    assert impact.replacement.model == "claude-sonnet-5"
    assert impact.replacement_explicit is False
    assert impact.default_replacement_id == fleet.anthropic.id
    assert [c.provider_id for c in impact.candidates] == [fleet.anthropic.id, fleet.backup.id]

    assert impact.agent_default.action == "reseeded"
    assert impact.agent_default.after.provider_id == fleet.anthropic.id
    assert impact.agent_default.after.model == "claude-sonnet-5"
    assert impact.general_assistant.action == "reseeded"
    assert impact.general_assistant.after.provider_id == fleet.anthropic.id
    assert impact.agent_default.before.display_name == "Production"

    # Anthropic cannot serve gpt-image-1; the backup OpenAI config can.
    assert impact.image_default.action == "reseeded"
    assert impact.image_default.after.provider_id == fleet.backup.id
    assert impact.image_default.after.model == "gpt-image-1"

    embedding = impact.embedding_default
    assert embedding.action == "cleared"
    assert embedding.after is None
    assert embedding.fallback.provider_id == fleet.backup.id
    assert embedding.fallback.model == router.EMBEDDING_DEFAULTS["openai"]
    assert embedding.fallback_same_model is False
    assert embedding.knowledge_base_count == 3

    assert impact.routing_model.action == "cleared"
    assert impact.fallback_model.action == "unchanged"
    assert impact.decision_model.action == "cleared"
    assert impact.decision_model.env_managed is False
    assert (impact.agents.count, impact.agents.names) == (2, ["Analyst", "Triage"])
    assert (impact.users.count, impact.users.names) == (1, ["Ada"])
    assert impact.chat_available is True


@pytest.mark.asyncio
async def test_an_explicit_replacement_takes_the_chat_and_image_routes():
    fleet = _fleet()
    db = _Session([fleet.deleted, fleet.backup, fleet.anthropic, fleet.tei])

    impact = await compute_deletion_impact(
        db,
        fleet.deleted,
        replacement_id=fleet.backup.id,
        platform_settings=fleet.ps,
        general_assistant=fleet.ga,
    )

    assert impact.replacement_explicit is True
    assert impact.default_replacement_id == fleet.anthropic.id
    assert impact.agent_default.after.provider_id == fleet.backup.id
    # Backup does not offer gpt-5.1: both routes take its default chat model.
    assert impact.agent_default.after.model == "gpt-4o"
    assert impact.agent_default.model_kept is False
    assert impact.general_assistant.after.model == "gpt-4o"
    assert impact.general_assistant.model_kept is False
    assert impact.image_default.after.provider_id == fleet.backup.id
    assert impact.image_default.model_kept is True


@pytest.mark.asyncio
async def test_an_explicit_replacement_offering_the_model_keeps_it():
    fleet = _fleet()
    fleet.backup.available_models = ["gpt-4o", "gpt-5.1"]
    db = _Session([fleet.deleted, fleet.backup, fleet.anthropic, fleet.tei])

    impact = await compute_deletion_impact(
        db,
        fleet.deleted,
        replacement_id=fleet.backup.id,
        platform_settings=fleet.ps,
        general_assistant=fleet.ga,
    )

    for route in (impact.agent_default, impact.general_assistant):
        assert route.action == "reseeded"
        assert (route.after.provider_id, route.after.model) == (fleet.backup.id, "gpt-5.1")
        assert route.model_kept is True
    # The replacement itself is still described by its own default model.
    assert impact.replacement.model == "gpt-4o"


@pytest.mark.asyncio
async def test_the_rule_picked_replacement_keeps_a_model_it_offers():
    fleet = _fleet()
    fleet.backup.available_models = ["gpt-4o", "gpt-5.1"]
    # Without the older Anthropic config, the rule picks Backup.
    db = _Session([fleet.deleted, fleet.backup, fleet.tei])

    impact = await compute_deletion_impact(
        db, fleet.deleted, platform_settings=fleet.ps, general_assistant=fleet.ga
    )

    assert impact.replacement_explicit is False
    assert impact.agent_default.after.model == "gpt-5.1"
    assert impact.agent_default.model_kept is True
    assert impact.general_assistant.after.model == "gpt-5.1"


@pytest.mark.asyncio
async def test_each_chat_route_decides_on_its_own_model():
    fleet = _fleet()
    fleet.backup.available_models = ["gpt-4o", "gpt-5.1"]
    fleet.deleted.available_models = ["gpt-5.1", "o3-pro"]
    fleet.ps.agent_default_model = "o3-pro"
    db = _Session([fleet.deleted, fleet.backup])

    impact = await compute_deletion_impact(
        db, fleet.deleted, platform_settings=fleet.ps, general_assistant=fleet.ga
    )

    # Backup offers the assistant's gpt-5.1 but not the platform default's o3-pro.
    assert (impact.agent_default.after.model, impact.agent_default.model_kept) == ("gpt-4o", False)
    assert (impact.general_assistant.after.model, impact.general_assistant.model_kept) == (
        "gpt-5.1",
        True,
    )


@pytest.mark.asyncio
async def test_routes_on_other_configs_are_left_alone():
    fleet = _fleet()
    ps = _ps(
        agent_default_provider="anthropic",
        agent_default_provider_id=fleet.anthropic.id,
        agent_default_model="claude-sonnet-5",
    )
    db = _Session([fleet.deleted, fleet.anthropic])

    impact = await compute_deletion_impact(
        db, fleet.deleted, platform_settings=ps, general_assistant=_ga()
    )

    assert impact.agent_default.action == "unchanged"
    assert impact.agent_default.after.provider_id == fleet.anthropic.id
    # "Agent default" (no model of its own) follows the platform default.
    assert impact.general_assistant.action == "unchanged"
    assert impact.general_assistant.before is None
    assert impact.embedding_default.action == "unchanged"


@pytest.mark.asyncio
async def test_a_stale_name_without_a_model_is_cleared_not_invented():
    fleet = _fleet()
    ps = _ps(agent_default_provider="openai")
    db = _Session([fleet.deleted, fleet.anthropic])

    impact = await compute_deletion_impact(db, fleet.deleted, platform_settings=ps, general_assistant=None)

    assert impact.agent_default.action == "cleared"
    assert impact.agent_default.after is None


@pytest.mark.asyncio
async def test_a_reference_without_a_model_moves_its_pin_and_stays_model_less():
    fleet = _fleet()
    ps = _ps(agent_default_provider="openai", agent_default_provider_id=fleet.deleted.id)
    db = _Session([fleet.deleted, fleet.anthropic])

    impact = await compute_deletion_impact(db, fleet.deleted, platform_settings=ps, general_assistant=None)

    assert impact.agent_default.action == "reseeded"
    assert impact.agent_default.after.provider_id == fleet.anthropic.id
    assert impact.agent_default.after.model is None


@pytest.mark.asyncio
async def test_deleting_the_last_chat_provider_reseeds_nothing():
    fleet = _fleet()
    db = _Session([fleet.deleted, fleet.tei])

    impact = await compute_deletion_impact(
        db, fleet.deleted, platform_settings=fleet.ps, general_assistant=fleet.ga
    )

    assert impact.chat_available is False
    assert impact.replacement is None
    assert impact.agent_default.action == "cleared"
    assert impact.general_assistant.action == "cleared"
    assert impact.image_default.action == "cleared"
    assert impact.embedding_default.fallback is None


@pytest.mark.asyncio
async def test_a_survivor_serving_the_embedding_model_keeps_the_embedding_default():
    """The same model is served by a survivor: the default moves there, kept,
    instead of being cleared while embeddings fall back to that very model."""
    fleet = _fleet()
    fleet.ps.embedding_model = router.EMBEDDING_DEFAULTS["openai"]
    db = _Session([fleet.deleted, fleet.backup])

    impact = await compute_deletion_impact(
        db, fleet.deleted, platform_settings=fleet.ps, general_assistant=None
    )

    route = impact.embedding_default
    assert route.action == "reseeded"
    assert route.model_kept is True
    assert (route.after.provider_id, route.after.model) == (
        fleet.backup.id,
        router.EMBEDDING_DEFAULTS["openai"],
    )


@pytest.mark.asyncio
async def test_the_embedding_default_never_moves_to_another_model():
    fleet = _fleet()
    fleet.ps.embedding_model = "text-embedding-3-small"
    # Backup embeds, but text-embedding-3-large by default and lists no 3-small.
    db = _Session([fleet.deleted, fleet.backup])

    impact = await compute_deletion_impact(db, fleet.deleted, platform_settings=fleet.ps, general_assistant=None)

    assert impact.embedding_default.action == "cleared"


@pytest.mark.asyncio
async def test_routing_and_fallback_keep_their_model_on_a_survivor_offering_it():
    fleet = _fleet()
    fleet.backup.available_models = ["gpt-4o", "gpt-5.1"]
    fleet.ps.routing_model = "gpt-5.1"
    fleet.ps.routing_provider_id = fleet.deleted.id
    fleet.ps.fallback_model = "gpt-5.1"
    fleet.ps.fallback_provider_id = fleet.deleted.id
    fleet.ps.decision_model = "o3-pro"
    fleet.ps.decision_provider_id = fleet.deleted.id
    db = _Session([fleet.deleted, fleet.backup])

    impact = await compute_deletion_impact(db, fleet.deleted, platform_settings=fleet.ps, general_assistant=None)
    ps = _ps()
    apply_deletion_impact(impact, platform_settings=ps, general_assistant=None)

    assert (ps.routing_model, ps.routing_provider_id) == ("gpt-5.1", fleet.backup.id)
    assert (ps.fallback_model, ps.fallback_provider_id) == ("gpt-5.1", fleet.backup.id)
    # Nobody offers o3-pro: cleared, never moved to another model.
    assert impact.decision_model.action == "cleared"
    assert (ps.decision_model, ps.decision_provider_id) == (None, None)


@pytest.mark.asyncio
async def test_apply_writes_the_reseeded_routes_with_their_provider_id():
    fleet = _fleet()
    db = _Session([fleet.deleted, fleet.backup, fleet.anthropic])
    impact = await compute_deletion_impact(
        db, fleet.deleted, platform_settings=fleet.ps, general_assistant=fleet.ga
    )
    # What the cleanup leaves behind before the re-seed.
    ps = _ps()
    ga = _ga()

    apply_deletion_impact(impact, platform_settings=ps, general_assistant=ga)

    assert (ps.agent_default_provider, ps.agent_default_provider_id, ps.agent_default_model) == (
        "anthropic",
        fleet.anthropic.id,
        "claude-sonnet-5",
    )
    assert (ga.llm_provider, ga.llm_provider_id, ga.llm_model) == (
        "anthropic",
        fleet.anthropic.id,
        "claude-sonnet-5",
    )
    assert (ps.image_provider_id, ps.image_model) == (fleet.backup.id, "gpt-image-1")
    # Never switched: the embedding default stays as the cleanup left it.
    assert (ps.embedding_provider, ps.embedding_provider_id, ps.embedding_model) == (None, None, None)


@pytest.mark.asyncio
async def test_the_audit_details_record_where_traffic_moved():
    fleet = _fleet()
    db = _Session([fleet.deleted, fleet.backup, fleet.anthropic], agents=["Triage"])

    impact = await compute_deletion_impact(
        db, fleet.deleted, platform_settings=fleet.ps, general_assistant=fleet.ga
    )
    details = impact.audit_details()

    assert details["provider_id"] == str(fleet.deleted.id)
    assert details["replacement"]["provider_id"] == str(fleet.anthropic.id)
    assert details["replacement"]["model"] == "claude-sonnet-5"
    assert details["replacement"]["chosen_by"] == "rule"
    assert details["replacement"]["is_rule_default"] is True
    assert details["routes"]["agent_default"]["action"] == "reseeded"
    assert details["routes"]["agent_default"]["before"]["provider_id"] == str(fleet.deleted.id)
    assert details["routes"]["agent_default"]["model_kept"] is False
    assert details["routes"]["image_default"]["after"]["provider_id"] == str(fleet.backup.id)
    assert details["routes"]["image_default"]["model_kept"] is True
    assert details["routes"]["routing_model"]["model_kept"] is None
    assert details["routes"]["decision_model"]["action"] == "cleared"
    assert details["routes"]["embedding_default"]["action"] == "cleared"
    assert details["agents_reset"] == 1


@pytest.mark.asyncio
async def test_the_fingerprint_is_what_the_deletion_applies_not_who_picked_it():
    fleet = _fleet()
    configs = [fleet.deleted, fleet.backup, fleet.anthropic, fleet.tei]

    async def fingerprint(replacement_id=None, **session):
        impact = await compute_deletion_impact(
            _Session(configs, **session),
            fleet.deleted,
            replacement_id=replacement_id,
            platform_settings=fleet.ps,
            general_assistant=fleet.ga,
        )
        return impact.fingerprint()

    by_rule = await fingerprint()
    # The dialog previews the rule's pick and confirms it by name.
    assert await fingerprint(fleet.anthropic.id) == by_rule
    # What only the dialog reads is not part of it.
    assert await fingerprint(indexed_kbs=7) == by_rule
    # Another replacement, or another agent to reset, is another deletion.
    assert await fingerprint(fleet.backup.id) != by_rule
    assert await fingerprint(agents=["Triage"]) != by_rule


@pytest.mark.asyncio
async def test_a_survivor_listing_no_model_is_withheld_from_a_route_carrying_one():
    fleet = _fleet()
    catalog_less = _cfg("azure_openai", models=None, created=-2, display_name="Azure")
    db = _Session([fleet.deleted, catalog_less, fleet.backup])

    impact = await compute_deletion_impact(db, fleet.deleted, platform_settings=fleet.ps, general_assistant=fleet.ga)

    # The oldest survivor lists nothing: the defaults run on gpt-5.1 and land on Backup.
    assert impact.replacement.provider_id == fleet.backup.id
    assert (impact.agent_default.after.provider_id, impact.agent_default.after.model) == (fleet.backup.id, "gpt-4o")
    assert [t.provider_id for t in impact.unlisted] == [catalog_less.id]
    assert [c.provider_id for c in impact.candidates] == [fleet.backup.id]
    assert impact.chat_available is True

    # A default pinned without a model holds on any chat-capable config.
    model_less = await compute_deletion_impact(
        _Session([fleet.deleted, catalog_less, fleet.backup]),
        fleet.deleted,
        platform_settings=_ps(agent_default_provider="openai", agent_default_provider_id=fleet.deleted.id),
        general_assistant=None,
    )
    assert model_less.replacement.provider_id == catalog_less.id
    assert model_less.unlisted == ()


@pytest.mark.asyncio
async def test_with_no_chat_capable_survivor_the_preview_names_where_chat_still_lands():
    """``get_llm_provider`` still sends chat to a config listing only non-chat
    models (on its provider's built-in chat model): the preview says so rather
    than that chat stops."""
    fleet = _fleet()
    image_only = _cfg("openai", models=["gpt-image-1"], created=3, display_name="Images")

    landing = await compute_deletion_impact(
        _Session([fleet.deleted, fleet.tei, image_only]), fleet.deleted, platform_settings=fleet.ps, general_assistant=fleet.ga
    )
    assert landing.chat_available is False
    assert (landing.implicit_chat_fallback.provider_id, landing.implicit_chat_fallback.model) == (
        image_only.id,
        OpenAIProvider.default_model,
    )
    assert router.implicit_chat_config([fleet.tei, image_only]) is image_only

    # Only an embedding-only config left: chat really stops.
    stopped = await compute_deletion_impact(
        _Session([fleet.deleted, fleet.tei]), fleet.deleted, platform_settings=fleet.ps, general_assistant=fleet.ga
    )
    assert stopped.implicit_chat_fallback is None

    # Where chat lands is part of what the confirm accepted. A survivor that
    # serves neither images nor embeddings changes nothing else in the impact,
    # so losing it between preview and confirm must still invalidate the preview.
    no_image_no_embedding = _cfg("anthropic", models=["gpt-image-1"], created=3, display_name="Other")
    with_landing = await compute_deletion_impact(
        _Session([fleet.deleted, fleet.tei, no_image_no_embedding]),
        fleet.deleted,
        platform_settings=fleet.ps,
        general_assistant=fleet.ga,
    )
    assert with_landing.implicit_chat_fallback.provider_id == no_image_no_embedding.id
    assert with_landing.fingerprint() != stopped.fingerprint()


@pytest.mark.asyncio
async def test_with_no_chat_default_the_preview_says_where_implicit_chat_moves():
    """No chat default stored: chat goes to ``router.implicit_chat_config``, and
    deleting that config moves the traffic with nothing stored changing."""
    fleet = _fleet()
    oldest = _cfg("openai", models=["gpt-5.1"], created=-2, display_name="Oldest")
    configs = [oldest, fleet.tei, fleet.anthropic, fleet.backup]
    assert router.implicit_chat_config(configs) is oldest

    impact = await compute_deletion_impact(
        _Session(configs), oldest, platform_settings=_ps(), general_assistant=_ga()
    )
    assert impact.implicit_chat_moves is True
    assert impact.previous_implicit_chat.provider_id == oldest.id
    assert (impact.implicit_chat_fallback.provider_id, impact.implicit_chat_fallback.model) == (
        fleet.anthropic.id,
        "claude-sonnet-5",
    )
    assert router.implicit_chat_config(configs[1:]) is fleet.anthropic
    assert impact.audit_details()["implicit_chat"]["to"]["provider_id"] == str(fleet.anthropic.id)
    assert impact.fingerprint() != dataclasses.replace(impact, implicit_chat_moves=False).fingerprint()

    # Deleting a config chat does not go to today: nothing moves.
    other = await compute_deletion_impact(
        _Session(configs), fleet.backup, platform_settings=_ps(), general_assistant=_ga()
    )
    assert other.implicit_chat_moves is False
    assert other.implicit_chat_fallback is None
    assert other.audit_details()["implicit_chat"] is None

    # A stored chat default decides where chat goes: nothing implicit moves.
    stored = await compute_deletion_impact(
        _Session(configs),
        oldest,
        platform_settings=_ps(),
        general_assistant=_ga("anthropic", fleet.anthropic.id, "claude-sonnet-5"),
    )
    assert stored.implicit_chat_moves is False


@pytest.mark.asyncio
async def test_the_preview_announces_where_implicit_embeddings_move():
    fleet = _fleet()
    gemini = _cfg("gemini", models=["gemini-3.8-flash"], created=3)
    # No embedding default stored: embeddings fall back to OpenAI > Gemini > ...
    ps = _ps(agent_default_provider="anthropic", agent_default_provider_id=fleet.anthropic.id, agent_default_model="claude-sonnet-5")

    moving = await compute_deletion_impact(
        _Session([fleet.deleted, fleet.anthropic, gemini], indexed_kbs=2), fleet.deleted, platform_settings=ps, general_assistant=None
    )
    embedding = moving.embedding_default
    assert (embedding.action, embedding.fallback_moves, embedding.affected) == ("unchanged", True, True)
    assert embedding.previous_fallback.provider_id == fleet.deleted.id
    assert (embedding.fallback.provider_id, embedding.fallback.model) == (gemini.id, router.EMBEDDING_DEFAULTS["gemini"])
    assert embedding.fallback_same_model is False
    assert embedding.knowledge_base_count == 2
    assert moving.fingerprint() != (
        await compute_deletion_impact(
            _Session([fleet.deleted, fleet.anthropic, gemini, _cfg("openai", models=["gpt-4o"], created=5)], indexed_kbs=2),
            fleet.deleted,
            platform_settings=ps,
            general_assistant=None,
        )
    ).fingerprint()

    # Embeddings fall back to the newer OpenAI config, not the deleted one: nothing moves.
    newer_openai = _cfg("openai", models=["gpt-4o"], created=5)
    staying = await compute_deletion_impact(
        _Session([fleet.deleted, newer_openai, gemini]), fleet.deleted, platform_settings=ps, general_assistant=None
    )
    assert (staying.embedding_default.fallback_moves, staying.embedding_default.affected) == (False, False)


# ── The HTTP layer: an unusable replacement is a 422 ────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", ["not-a-uuid", "unknown", "deleted", "embedding_only"])
async def test_the_endpoints_refuse_an_unusable_replacement_with_a_422(requested):
    from app.api.v1.settings import _llm_provider_deletion_impact

    fleet = _fleet()
    db = _Session([fleet.deleted, fleet.anthropic, fleet.tei])
    replacement = {
        "not-a-uuid": "not-a-uuid",
        "unknown": str(uuid.uuid4()),
        "deleted": str(fleet.deleted.id),
        "embedding_only": str(fleet.tei.id),
    }[requested]

    with pytest.raises(HTTPException) as exc:
        await _llm_provider_deletion_impact(
            db, fleet.deleted, replacement, platform_settings=fleet.ps, general_assistant=fleet.ga
        )

    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_the_preview_is_blocked_when_models_are_env_only(monkeypatch):
    from app.api.v1.settings import get_llm_provider_deletion_impact
    from app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "models_env_providing_only", True)

    with pytest.raises(HTTPException) as exc:
        await get_llm_provider_deletion_impact(
            provider_id=str(uuid.uuid4()), replacement_provider_id=None, user=SimpleNamespace(), db=None
        )

    assert exc.value.status_code == 403


# ── Defence in depth: the implicit readers are order-stable ────────────────


def test_provider_configs_are_selected_in_settings_order():
    sql = str(router.select_provider_configs().compile())

    assert "ORDER BY llm_provider_configs.created_at, llm_provider_configs.id" in sql


class _CapturingDb:
    def __init__(self, configs):
        self.configs = configs
        self.statements = []

    async def execute(self, statement):
        self.statements.append(str(statement.compile()))
        return _Result(rows=self.configs)


@pytest.mark.asyncio
async def test_the_implicit_chat_pick_reads_configs_in_settings_order(monkeypatch):
    chat = _cfg("anthropic", models=["claude-sonnet-5"])
    db = _CapturingDb([chat])
    monkeypatch.setattr(router, "_get_cached_provider", lambda config: config)

    picked = await router.get_llm_provider("user", db)

    assert picked is chat
    assert "ORDER BY llm_provider_configs.created_at, llm_provider_configs.id" in db.statements[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("reader", ["get_embedding_provider", "get_image_provider"])
async def test_the_embedding_and_image_fallbacks_read_configs_in_settings_order(monkeypatch, reader):
    db = _CapturingDb([_cfg("openai", models=["gpt-4o"])])

    async def _ps_loader(_db):
        return _ps(embedding_dimensions=None)

    monkeypatch.setattr(router, "_get_platform_settings", _ps_loader)
    monkeypatch.setattr(router, "_get_cached_provider", lambda config: SimpleNamespace(config=config))

    await getattr(router, reader)(db)

    assert "ORDER BY llm_provider_configs.created_at, llm_provider_configs.id" in db.statements[0]


def test_the_user_resolution_loads_configs_in_settings_order():
    """``get_llm_for_user`` hands its list to ``get_llm_provider`` and builds the
    failover chain from it, so the list itself must come from the ordered query."""
    source = inspect.getsource(router.get_llm_for_user)

    assert "select_provider_configs()" in source
    assert "select(LLMProviderConfig)" not in source


@pytest.mark.asyncio
async def test_an_azure_config_without_an_image_model_is_not_an_image_fallback(monkeypatch):
    """Azure image models are deployment names with no built-in default: the
    fallback used to raise a ``KeyError`` instead of saying none is configured."""
    db = _CapturingDb([_cfg("azure_openai", models=["gpt-4o"])])

    async def _ps_loader(_db):
        return _ps()

    monkeypatch.setattr(router, "_get_platform_settings", _ps_loader)

    with pytest.raises(ValueError, match="image generation"):
        await router.get_image_provider(db)
