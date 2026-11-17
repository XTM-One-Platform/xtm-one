"""LLM providers that are never used for some groups and users.

A regulation can forbid a vendor for part of the staff (employees of one
jurisdiction) while the rest of the company keeps it. An exclusion keeps those
people off a provider whatever it grants; their AI work resolves to the
providers left to them (typically a locally hosted one), or is refused. These
tests pin the rule, its precedence over grants, the person a no-user call is
resolved for, the platform-level capabilities that refuse an excluded person's
content (embeddings, image generation, knowledge base documents, stored
memories), and the group deletion that would silently lift an exclusion.
"""

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.llm import provider_access, router
from app.llm.provider_access import (
    EXCLUDED_MODEL_MESSAGE,
    NO_ACCOUNT,
    NO_USABLE_PROVIDER_MESSAGE,
    ProviderAccessError,
    ProviderAudience,
    ProviderNotUsableError,
    acting_for,
    acting_user_id,
    apply_provider_access,
    bind_acting_user,
    ensure_not_excluded,
    is_excluded,
    provider_usable_by,
    routing_for,
)

HK = str(uuid.uuid4())  # the group a regulation keeps off a vendor
TEAM_A = str(uuid.uuid4())
HANA = str(uuid.uuid4())  # in HK (and in team A)
ALICE = str(uuid.uuid4())  # in team A only
CAROL = str(uuid.uuid4())  # in no group

AUDIENCES = {
    HANA: ProviderAudience(user_id=HANA, group_ids=frozenset({HK, TEAM_A})),
    ALICE: ProviderAudience(user_id=ALICE, group_ids=frozenset({TEAM_A})),
    CAROL: ProviderAudience(user_id=CAROL),
}


def _cfg(
    provider="anthropic",
    models=("claude-sonnet-4-6",),
    *,
    groups=None,
    users=None,
    restricted=None,
    excluded_groups=None,
    excluded_users=None,
    name=None,
):
    if restricted is None:
        restricted = bool(groups or users)
    return SimpleNamespace(
        id=uuid.uuid4(),
        provider=provider,
        display_name=name,
        available_models=list(models),
        default_model=models[0] if models else None,
        access_restricted=restricted,
        shared_group_ids=list(groups) if groups else None,
        shared_user_ids=list(users) if users else None,
        excluded_group_ids=list(excluded_groups) if excluded_groups else None,
        excluded_user_ids=list(excluded_users) if excluded_users else None,
        is_default=False,
        created_at=None,
    )


def _instance(cfg):
    """A provider instance standing for *cfg*."""
    return SimpleNamespace(
        config=cfg,
        provider_name=cfg.provider,
        max_tools=None,
        context_window=None,
        context_windows=None,
        max_output_tokens=None,
        max_output_tokens_overrides=None,
    )


# ── the rule ─────────────────────────────────────────────────────────────


def test_an_excluded_group_keeps_its_members_off_an_open_provider():
    cfg = _cfg(excluded_groups=[HK])
    assert not provider_usable_by(cfg, AUDIENCES[HANA])
    assert provider_usable_by(cfg, AUDIENCES[ALICE])
    assert provider_usable_by(cfg, AUDIENCES[CAROL])
    assert provider_usable_by(cfg, NO_ACCOUNT)


def test_an_excluded_person_is_kept_off_by_name():
    cfg = _cfg(excluded_users=[CAROL.upper()])
    assert not provider_usable_by(cfg, AUDIENCES[CAROL])
    assert provider_usable_by(cfg, AUDIENCES[ALICE])


def test_an_exclusion_wins_over_every_grant():
    # Hana is granted through team A AND by name, and excluded through HK.
    cfg = _cfg(groups=[TEAM_A], users=[HANA], excluded_groups=[HK])
    assert not provider_usable_by(cfg, AUDIENCES[HANA])
    assert provider_usable_by(cfg, AUDIENCES[ALICE])


def test_an_exclusion_holds_whether_or_not_the_provider_is_restricted():
    for restricted in (False, True):
        cfg = _cfg(groups=[HK], restricted=restricted, excluded_groups=[HK])
        assert is_excluded(cfg, AUDIENCES[HANA])
        assert not provider_usable_by(cfg, AUDIENCES[HANA])


def test_routing_withholds_an_excluded_provider_and_says_why():
    claude = _cfg(excluded_groups=[HK])
    local = _cfg(provider="ollama", models=("llama-4",))
    routing = routing_for(AUDIENCES[HANA], [claude, local])
    assert routing.usable == [local]
    assert routing.is_withheld(claude.id)
    assert routing.excluded_ids == frozenset({str(claude.id)})
    # An agent pinned to Claude is not Hana's model: its tier is skipped.
    assert routing.is_reserved("claude-sonnet-4-6")
    assert routing.owner_for("claude-sonnet-4-6") is None
    assert routing.owner_for("llama-4") is local
    # Asking for access would not help her: the wording says so.
    assert routing.reserved_model_message("claude-sonnet-4-6") == EXCLUDED_MODEL_MESSAGE.format(
        model="claude-sonnet-4-6"
    )


def test_a_model_also_reserved_for_another_team_keeps_the_access_wording():
    claude = _cfg(excluded_groups=[HK])
    team_b = _cfg(groups=[str(uuid.uuid4())])
    routing = routing_for(AUDIENCES[HANA], [claude, team_b])
    assert "reserved for other groups" in routing.reserved_model_message("claude-sonnet-4-6")


# ── the person a no-user call is made for ───────────────────────────────


class _GroupRows:
    def __init__(self, ids):
        self._ids = ids

    def scalars(self):
        return self

    def all(self):
        return [uuid.UUID(i) for i in self._ids]


class _GroupSession:
    """Answers the group-membership lookup of whoever is asked about."""

    def __init__(self, memberships):
        self._memberships = memberships
        self.asked_for: list[str] = []

    async def execute(self, statement):
        user = str(statement.whereclause.right.value)
        self.asked_for.append(user)
        return _GroupRows(self._memberships.get(user, []))


def test_a_no_user_call_with_nobody_bound_is_no_one():
    async def _run():
        return await provider_access.load_provider_audience(_GroupSession({}), None)

    assert asyncio.run(_run()) is NO_ACCOUNT


def test_a_no_user_call_is_resolved_as_the_person_the_context_acts_for():
    session = _GroupSession({HANA: [HK]})

    async def _run():
        bind_acting_user(HANA)
        return await provider_access.load_provider_audience(session, None)

    audience = asyncio.run(_run())
    assert audience == ProviderAudience(user_id=HANA, group_ids=frozenset({HK}))
    assert session.asked_for == [HANA]


def test_an_explicit_user_wins_over_the_binding():
    session = _GroupSession({ALICE: [TEAM_A]})

    async def _run():
        bind_acting_user(HANA)
        return await provider_access.load_provider_audience(session, ALICE)

    assert asyncio.run(_run()).user_id == ALICE


def test_a_binding_ignores_what_is_not_a_user_and_scopes_restore_it():
    async def _run():
        bind_acting_user("anonymous")
        assert acting_user_id() is None
        bind_acting_user(uuid.UUID(HANA))
        assert acting_user_id() == HANA
        with acting_for(CAROL):
            assert acting_user_id() == CAROL
        with acting_for(None):
            assert acting_user_id() is None
        return acting_user_id()

    assert asyncio.run(_run()) == HANA
    # Nothing leaks out of the task that bound it.
    assert acting_user_id() is None


def test_resolving_a_model_binds_the_caller_for_the_calls_that_follow(monkeypatch):
    async def _platform_settings(_db):
        return SimpleNamespace(agent_default_model=None, agent_default_provider_id=None)

    monkeypatch.setattr("app.api.v1.platform_settings_helpers.get_platform_settings", _platform_settings)
    monkeypatch.setattr(router, "_get_cached_provider", _instance)

    async def _load(_db, user_id, *, with_groups=True):
        uid = provider_access.canonical_id(user_id) or acting_user_id()
        return AUDIENCES.get(uid or "", NO_ACCOUNT)

    monkeypatch.setattr(provider_access, "load_provider_audience", _load)
    claude = _cfg(excluded_groups=[HK])
    local = _cfg(provider="ollama", models=("llama-4",))

    async def _run():
        session = _Session([claude, local])
        first, _ = await router.get_llm_for_user(session, HANA, model_override="claude-sonnet-4-6")
        # A later call of the same work with no user of its own (compaction,
        # memory) is still Hana's: Claude stays out.
        second, _ = await router.get_llm_for_user(session, None, model_override="claude-sonnet-4-6")
        return first, second

    first, second = asyncio.run(_run())
    assert _primary(first).config is local
    assert _primary(second).config is local


# ── the router, for an excluded caller ──────────────────────────────────


class _Rows:
    def __init__(self, rows=()):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def one_or_none(self):
        return None


class _Session:
    """Answers every read of the provider configs with *configs*, any other
    read (platform settings, General Assistant, preferences) with nothing."""

    def __init__(self, configs):
        self._configs = list(configs)

    async def execute(self, statement):
        from app.models.llm_config import LLMProviderConfig

        descriptions = getattr(statement, "column_descriptions", None) or [{}]
        if descriptions[0].get("entity") is LLMProviderConfig:
            return _Rows(self._configs)
        return _Rows()


@pytest.fixture
def audiences(monkeypatch):
    async def _load(_db, user_id, *, with_groups=True):
        uid = provider_access.canonical_id(user_id) or acting_user_id()
        return AUDIENCES.get(uid or "", NO_ACCOUNT)

    monkeypatch.setattr(provider_access, "load_provider_audience", _load)


@pytest.fixture
def instances(monkeypatch):
    async def _platform_settings(_db):
        return SimpleNamespace(agent_default_model=None, agent_default_provider_id=None)

    monkeypatch.setattr("app.api.v1.platform_settings_helpers.get_platform_settings", _platform_settings)
    monkeypatch.setattr(router, "_get_cached_provider", _instance)


def _run_for(user_id, configs, **kwargs):
    return asyncio.run(router.get_llm_for_user(_Session(configs), user_id, **kwargs))


def _primary(provider):
    """The provider a run starts on, failover wrapper or not."""
    return getattr(provider, "primary", provider)


def test_an_agent_pinned_to_claude_runs_on_the_local_model_for_an_excluded_user(audiences, instances):
    claude = _cfg(excluded_groups=[HK])
    local = _cfg(provider="ollama", models=("llama-4",))
    provider, model = _run_for(
        HANA, [claude, local], model_override="claude-sonnet-4-6", provider_id_override=claude.id
    )
    assert _primary(provider).config is local
    assert model != "claude-sonnet-4-6"
    # Everyone else keeps Claude on the same agent.
    provider, model = _run_for(ALICE, [claude, local], model_override="claude-sonnet-4-6")
    assert _primary(provider).config is claude
    assert model == "claude-sonnet-4-6"


def test_an_excluded_user_never_fails_over_onto_the_excluded_provider(audiences, instances):
    claude = _cfg(excluded_groups=[HK])
    openai = _cfg(provider="openai", models=("gpt-5",), excluded_groups=[HK])
    local = _cfg(provider="ollama", models=("llama-4",))
    backup = _cfg(provider="mistral", models=("mistral-large-3",))
    provider, _ = _run_for(HANA, [claude, openai, local, backup], model_override="claude-sonnet-4-6")
    assert isinstance(provider, router.FailoverLLMProvider)
    assert provider.primary.config is local
    assert provider._fallback_configs == [backup]


def test_a_user_every_provider_excludes_is_refused(audiences, instances):
    claude = _cfg(excluded_groups=[HK])
    openai = _cfg(provider="openai", models=("gpt-5",), excluded_users=[HANA])
    with pytest.raises(ProviderNotUsableError) as refused:
        _run_for(HANA, [claude, openai], model_override="claude-sonnet-4-6")
    assert str(refused.value) == NO_USABLE_PROVIDER_MESSAGE


def test_an_explicit_excluded_model_is_refused_with_the_exclusion_wording(audiences, instances):
    claude = _cfg(excluded_groups=[HK])
    local = _cfg(provider="ollama", models=("llama-4",))
    session = _Session([claude, local])
    with pytest.raises(ProviderNotUsableError) as refused:
        asyncio.run(router.resolve_provider_for_model(session, "claude-sonnet-4-6", user_id=HANA))
    assert str(refused.value) == EXCLUDED_MODEL_MESSAGE.format(model="claude-sonnet-4-6")


# ── platform-level capabilities ─────────────────────────────────────────


def test_an_excluded_persons_content_is_refused_by_a_platform_capability(audiences):
    embedder = _cfg(provider="openai", models=("text-embedding-3-small",), excluded_groups=[HK])

    async def _for(user_id=None, bound=None):
        if bound:
            bind_acting_user(bound)
        await ensure_not_excluded(None, embedder, "Embedding", user_id)

    with pytest.raises(ProviderNotUsableError, match=r"^Embedding is not available to your account"):
        asyncio.run(_for(HANA))
    with pytest.raises(ProviderNotUsableError):
        asyncio.run(_for(bound=HANA))
    asyncio.run(_for(ALICE))
    asyncio.run(_for(bound=ALICE))
    asyncio.run(_for())  # nobody: platform-level work


def test_a_restriction_alone_never_refuses_a_platform_capability(audiences):
    # Embeddings stay platform-level for restricted providers: one
    # knowledge base, one embedding model, whoever reads it.
    embedder = _cfg(provider="openai", models=("text-embedding-3-small",), groups=[str(uuid.uuid4())])
    asyncio.run(ensure_not_excluded(None, embedder, "Embedding", HANA))


def _platform_settings(**kwargs):
    base = {
        "embedding_dimensions": None,
        "embedding_provider_id": None,
        "embedding_provider": None,
        "embedding_model": None,
        "image_provider_id": None,
        "image_provider": None,
        "image_model": None,
    }
    return SimpleNamespace(**{**base, **kwargs})


def test_the_embedding_provider_refuses_an_excluded_person(audiences, monkeypatch):
    embedder = _cfg(provider="openai", models=("text-embedding-3-small",), excluded_groups=[HK])

    async def _settings(_db):
        return _platform_settings(embedding_provider_id=embedder.id, embedding_model="text-embedding-3-small")

    monkeypatch.setattr(router, "_get_platform_settings", _settings)
    monkeypatch.setattr(router, "_get_cached_provider", _instance)

    def _resolve(user_id):
        return asyncio.run(router.get_embedding_provider(_Session([embedder]), user_id=user_id))

    with pytest.raises(ProviderNotUsableError, match="Embedding is not available"):
        _resolve(HANA)
    provider, model, provider_id = _resolve(ALICE)
    assert provider.config is embedder
    assert model == "text-embedding-3-small"
    assert provider_id == embedder.id


def test_the_image_provider_refuses_an_excluded_person(audiences, monkeypatch):
    imager = _cfg(provider="openai", models=("gpt-image-2",), excluded_users=[HANA])

    async def _settings(_db):
        return _platform_settings(image_provider_id=imager.id, image_model="gpt-image-2")

    monkeypatch.setattr(router, "_get_platform_settings", _settings)
    monkeypatch.setattr(router, "_get_cached_provider", _instance)

    async def _resolve(bound):
        bind_acting_user(bound)
        return await router.get_image_provider(_Session([imager]))

    with pytest.raises(ProviderNotUsableError, match="Image generation is not available"):
        asyncio.run(_resolve(HANA))
    provider, model = asyncio.run(_resolve(ALICE))
    assert provider.config is imager
    assert model == "gpt-image-2"


# ── knowledge base documents ────────────────────────────────────────────


def test_a_document_job_carries_whom_it_embeds_for():
    from app.services.kb_helpers import document_job_kwargs

    assert document_job_kwargs("doc") == {"document_id": "doc"}
    assert document_job_kwargs("doc", HANA) == {"document_id": "doc", "on_behalf_of": HANA}


def test_a_document_is_queued_for_the_person_the_context_acts_for(monkeypatch):
    from app.services import kb_helpers

    queued = []

    class _Queue:
        async def enqueue(self, job):
            queued.append(job)
            return job

    monkeypatch.setattr("app.worker.queue", _Queue())
    checked = []

    async def _not_excluded(doc_ids, person):
        checked.append((doc_ids, person))
        return False

    monkeypatch.setattr(kb_helpers, "refuse_documents_for_excluded_person", _not_excluded)

    async def _run():
        bind_acting_user(HANA)
        return await kb_helpers.enqueue_document_processing("doc-1", embedding_ready=True)

    assert asyncio.run(_run()) is True
    assert checked == [(["doc-1"], HANA)]
    assert [job.kwargs for job in queued] == [{"document_id": "doc-1", "on_behalf_of": HANA}]


def test_an_excluded_persons_documents_are_refused_before_they_are_queued(monkeypatch):
    """Refused at enqueue time, while the person is known: the documents leave
    ``pending`` at once, so a sweeper re-enqueue - which knows no person -
    never finds them."""
    from app.services import kb_helpers
    from app.services.kb_document_errors import EMBEDDING_NOT_AVAILABLE_MESSAGE

    docs = [
        SimpleNamespace(id=uuid.uuid4(), status="pending", chunk_count=0, error_message=None)
        for _ in range(2)
    ]

    class _DocSession:
        commits = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def execute(self, _statement):
            return _Rows(docs)

        async def commit(self):
            _DocSession.commits += 1

    resolved_for = []

    async def _embedding_provider(_db, *, user_id=None):
        resolved_for.append(user_id)
        raise ProviderNotUsableError("excluded")

    queued = []

    class _Queue:
        async def enqueue(self, job):
            queued.append(job)
            return job

    monkeypatch.setattr(kb_helpers, "async_session", lambda: _DocSession())
    monkeypatch.setattr(router, "get_embedding_provider", _embedding_provider)
    monkeypatch.setattr(kb_helpers, "schedule_embedding_retry", lambda doc, *, retryable: setattr(doc, "retry", retryable))
    monkeypatch.setattr("app.worker.queue", _Queue())

    async def _resolve_state():
        return True, None

    monkeypatch.setattr(kb_helpers, "_resolve_embedding_enqueue_state", _resolve_state)

    async def _run():
        bind_acting_user(HANA)
        await kb_helpers.enqueue_documents([str(d.id) for d in docs])

    asyncio.run(_run())
    assert resolved_for == [HANA]
    assert queued == []
    assert [(d.status, d.error_message, d.retry) for d in docs] == [
        ("error", EMBEDDING_NOT_AVAILABLE_MESSAGE, False)
    ] * 2
    # Recorded, so a re-embed - which binds no one - checks them again.
    assert [d.added_by_user_id for d in docs] == [uuid.UUID(HANA)] * 2
    assert _DocSession.commits == 1


def test_a_document_queued_for_no_one_is_not_checked():
    from app.services import kb_helpers

    assert asyncio.run(kb_helpers.refuse_documents_for_excluded_person(["doc"], None)) is False


def test_the_document_error_for_an_excluded_author_is_a_platform_sentence():
    from app.services.kb_document_errors import (
        EMBEDDING_NOT_AVAILABLE_MESSAGE,
        DocumentErrorCode,
        public_document_error,
    )

    assert public_document_error(EMBEDDING_NOT_AVAILABLE_MESSAGE) == (
        DocumentErrorCode.EMBEDDING_NOT_AVAILABLE,
        EMBEDDING_NOT_AVAILABLE_MESSAGE,
    )


# ── stored memories ──────────────────────────────────────────────────────


def test_reembedding_leaves_an_excluded_persons_memories_alone(monkeypatch):
    from app.services import persistent_memory

    hana_entry = SimpleNamespace(
        id=uuid.uuid4(), user_id=uuid.UUID(HANA), content="Hana's fact", embedding=None,
        embedding_model=None, embedding_dimensions=None, created_at=None,
    )
    alice_entry = SimpleNamespace(
        id=uuid.uuid4(), user_id=uuid.UUID(ALICE), content="Alice's fact", embedding=None,
        embedding_model=None, embedding_dimensions=None, created_at=None,
    )
    agent_entry = SimpleNamespace(
        id=uuid.uuid4(), user_id=None, content="agent-level fact", embedding=None,
        embedding_model=None, embedding_dimensions=None, created_at=None,
    )

    class _PageDB:
        commit = AsyncMock()

        def in_transaction(self):
            return False

        async def execute(self, _stmt):
            return _Rows([hana_entry, alice_entry, agent_entry])

    provider = SimpleNamespace(embed=AsyncMock(return_value=[0.1] * 8))
    monkeypatch.setattr(
        persistent_memory, "_resolve_embedding_target", AsyncMock(return_value=(provider, "m", 8))
    )

    def _reembed(excluded):
        for entry in (hana_entry, alice_entry, agent_entry):
            entry.embedding = None
        monkeypatch.setattr(persistent_memory, "_embedding_excluded_users", AsyncMock(return_value=excluded))
        count = asyncio.run(persistent_memory.reembed_stale_memories(_PageDB(), agent_id=str(uuid.uuid4())))
        return count, [e.embedding is not None for e in (hana_entry, alice_entry, agent_entry)]

    assert _reembed(frozenset({HANA})) == (2, [False, True, True])
    # Who is excluded could not be read: no person's memory is sent.
    assert _reembed(None) == (1, [False, False, True])


def test_the_users_an_embedding_provider_excludes_include_its_groups_members():
    embedder = _cfg(provider="openai", excluded_groups=[HK], excluded_users=[CAROL])

    class _Session2:
        def __init__(self):
            self.calls = 0

        async def execute(self, _statement):
            self.calls += 1
            if self.calls == 1:
                return _Rows([embedder])
            return _Rows([uuid.UUID(HANA)])

    people = asyncio.run(provider_access.excluded_user_ids(_Session2(), embedder.id))
    assert people == frozenset({HANA, CAROL})


# ── writing an exclusion ─────────────────────────────────────────────────


@pytest.fixture
def known_ids(monkeypatch):
    known: set[str] = set()

    async def _missing(_db, _model, ids):
        return [i for i in ids if i not in known]

    monkeypatch.setattr(provider_access, "_missing_ids", _missing)
    return known


def _write(cfg, **kwargs):
    kwargs = {"access_restricted": None, "group_ids": None, "user_ids": None, **kwargs}
    return asyncio.run(apply_provider_access(None, cfg, **kwargs))


def test_excluding_writes_the_lists_and_leaves_the_restriction_alone(known_ids):
    known_ids.update({HK, CAROL})
    cfg = _cfg()
    change = _write(cfg, excluded_group_ids=[HK.upper(), HK], excluded_user_ids=[CAROL])
    assert cfg.access_restricted is False
    assert cfg.excluded_group_ids == [HK]
    assert cfg.excluded_user_ids == [CAROL]
    assert change["before"]["excluded_group_ids"] == []
    assert change["after"] == {
        "access_restricted": False,
        "shared_group_ids": [],
        "shared_user_ids": [],
        "excluded_group_ids": [HK],
        "excluded_user_ids": [CAROL],
    }


def test_an_unknown_excluded_group_is_refused_and_nothing_is_written(known_ids):
    cfg = _cfg()
    with pytest.raises(ProviderAccessError, match="Unknown group"):
        _write(cfg, excluded_group_ids=[HK])
    assert cfg.excluded_group_ids is None


def test_clearing_an_exclusion_stores_none(known_ids):
    cfg = _cfg(excluded_groups=[HK], excluded_users=[CAROL])
    change = _write(cfg, excluded_group_ids=[], excluded_user_ids=[])
    assert cfg.excluded_group_ids is None
    assert cfg.excluded_user_ids is None
    assert change["before"]["excluded_group_ids"] == [HK]


def test_omitted_exclusions_are_kept(known_ids):
    known_ids.add(TEAM_A)
    cfg = _cfg(excluded_groups=[HK])
    _write(cfg, access_restricted=True, group_ids=[TEAM_A])
    assert cfg.excluded_group_ids == [HK]


def test_the_exclusion_bound_is_its_own(known_ids):
    many = [str(uuid.uuid4()) for _ in range(provider_access.MAX_AUDIENCE_IDS)]
    known_ids.update(many)
    cfg = _cfg()
    # 500 exclusions are allowed next to grants: each list has its own bound.
    known_ids.add(TEAM_A)
    _write(cfg, access_restricted=True, group_ids=[TEAM_A], excluded_group_ids=many)
    assert len(cfg.excluded_group_ids) == provider_access.MAX_AUDIENCE_IDS
    known_ids.update({CAROL, ALICE})
    with pytest.raises(ProviderAccessError, match="at most"):
        _write(cfg, excluded_user_ids=[CAROL, ALICE])


# ── group and user deletion ──────────────────────────────────────────────


class _DeletionSession:
    def __init__(self, group, configs):
        self.group = group
        self.configs = configs
        self.locked_group = False

    async def execute(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        from app.models.group import Group
        from app.models.llm_config import LLMProviderConfig

        if entity is Group:
            if statement._for_update_arg is not None:
                self.locked_group = True
            return _Rows([self.group])
        if entity is LLMProviderConfig:
            assert self.locked_group, "providers read before the group was locked"
            return _Rows(self.configs)
        raise AssertionError(f"unexpected read of {entity}")


def test_a_group_an_exclusion_names_cannot_be_deleted(monkeypatch):
    from app.api.v1 import groups as groups_module
    from app.models.group import Group

    group = Group(id=uuid.UUID(HK), name="HK users", quota_config=None)
    claude = _cfg(name="Claude (global)", excluded_groups=[HK])
    pruned = AsyncMock()
    monkeypatch.setattr(groups_module, "prune_group_grants", pruned)

    with pytest.raises(HTTPException) as refused:
        asyncio.run(
            groups_module.delete_group(
                group_id=HK,
                request=SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={}),
                admin=SimpleNamespace(id=uuid.uuid4()),
                db=_DeletionSession(group, [claude, _cfg(provider="ollama")]),
                after_commit=None,
            )
        )
    assert refused.value.status_code == 409
    assert "'Claude (global)'" in refused.value.detail
    pruned.assert_not_awaited()


def test_a_deleted_user_is_pruned_from_exclusions():
    keep = str(uuid.uuid4())
    naming = _cfg(excluded_users=[CAROL.upper(), keep])
    alone = _cfg(excluded_users=[CAROL])
    other = _cfg(excluded_users=[keep])

    class _Configs:
        async def execute(self, _statement):
            return _Rows([naming, alone, other])

    assert asyncio.run(provider_access.prune_user_exclusions(_Configs(), CAROL)) == 2
    assert naming.excluded_user_ids == [keep]
    assert alone.excluded_user_ids is None
    assert other.excluded_user_ids == [keep]


def test_a_merged_groups_exclusions_follow_its_members():
    merged, target = str(uuid.uuid4()), str(uuid.uuid4())
    both = _cfg(excluded_groups=[merged.upper(), target])
    only = _cfg(excluded_groups=[merged, HK])
    other = _cfg(excluded_groups=[HK])

    class _Configs:
        async def execute(self, _statement):
            return _Rows([both, only, other])

    assert asyncio.run(provider_access.carry_group_exclusions(_Configs(), merged, target)) == 2
    assert both.excluded_group_ids == [target]
    assert only.excluded_group_ids == [target, HK]
    assert other.excluded_group_ids == [HK]


# ── the request binds its user ───────────────────────────────────────────


def test_an_authenticated_request_binds_its_user(monkeypatch):
    from app.api import deps

    user = SimpleNamespace(id=uuid.UUID(HANA))

    async def _authenticate(_token, _db, _request):
        return user

    monkeypatch.setattr(deps, "_authenticate_bearer", _authenticate)

    async def _run():
        resolved = await deps.get_current_user_ungated(SimpleNamespace(credentials="t"), None, None)
        return resolved, acting_user_id()

    assert asyncio.run(_run()) == (user, HANA)


def test_an_mcp_request_binds_its_user(monkeypatch):
    """/mcp/xtm runs ``search_knowledge`` with no model resolved first, so the
    authentication itself must bind the person."""
    from app.api.v1 import mcp_auth

    user = SimpleNamespace(id=uuid.UUID(HANA))

    async def _oauth(*_args, **_kwargs):
        return None

    async def _api_key(*_args, **_kwargs):
        return user

    monkeypatch.setattr(mcp_auth, "try_oauth_token", _oauth)
    monkeypatch.setattr(mcp_auth, "resolve_api_key", _api_key)
    monkeypatch.setattr(mcp_auth, "looks_like_api_key", lambda _token: True)
    request = SimpleNamespace(headers={"Authorization": "Bearer fcp-test"})

    async def _run():
        resolved = await mcp_auth.authenticate_user(request, None, "mcp/xtm")
        return resolved, acting_user_id()

    assert asyncio.run(_run()) == (user, HANA)


def test_reembedding_reads_the_platform_target_whoever_is_bound(monkeypatch):
    from app.services import persistent_memory

    seen = []

    async def _target(_db):
        seen.append(acting_user_id())
        return None, None, None

    monkeypatch.setattr(persistent_memory, "_resolve_embedding_target", _target)

    async def _run():
        bind_acting_user(HANA)
        return await persistent_memory.reembed_stale_memories(None, agent_id=str(uuid.uuid4()))

    assert asyncio.run(_run()) == 0
    assert seen == [None]


# ── review ───────────────────────────────────────────────────────────────


def test_a_reembedding_is_attributed_to_no_one(monkeypatch):
    """A reprocess or a platform-wide reset re-embeds documents others added:
    the person whose request triggered it is not bound for the jobs, which
    then embed for each knowledge base's owner."""
    from app.services import kb_helpers

    queued_for = []

    async def _enqueue(doc_ids):
        queued_for.append((list(doc_ids), acting_user_id()))

    monkeypatch.setattr(kb_helpers, "enqueue_documents", _enqueue)

    async def _run():
        bind_acting_user(HANA)
        await kb_helpers.enqueue_reembedding(["a", "b"])
        return acting_user_id()

    assert asyncio.run(_run()) == HANA  # the request keeps its own binding
    assert queued_for == [(["a", "b"], None)]


def test_a_memory_search_an_excluded_person_cannot_run_is_refused(monkeypatch):
    from app.services import persistent_memory

    async def _embedding_provider(_db, *, user_id=None):
        raise ProviderNotUsableError("Embedding is not available to your account.")

    monkeypatch.setattr(router, "get_embedding_provider", _embedding_provider)

    with pytest.raises(ProviderNotUsableError):
        asyncio.run(
            persistent_memory.search_agent_memories(None, agent_id=str(uuid.uuid4()), query="q")
        )
    # A write keeps storing without a vector.
    assert asyncio.run(persistent_memory.embed_text_with_model(None, "fact")) == (None, None)


def test_the_memory_search_tool_says_it_was_refused(monkeypatch):
    from app.services import persistent_memory
    from app.services.builtin_tools import memory as memory_tool

    async def _refused(*_args, **_kwargs):
        raise ProviderNotUsableError("Embedding is not available to your account.")

    async def _no_provider():
        return None

    class _Session:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(persistent_memory, "search_agent_memories", _refused)
    monkeypatch.setattr(memory_tool, "_resolve_provider_model", _no_provider)
    monkeypatch.setattr("app.core.database.async_session", lambda: _Session())
    _definitions, executor = memory_tool.build_memory_search_tool(str(uuid.uuid4()), HANA)
    answer = asyncio.run(executor._handlers["search_memory"](query="q"))
    assert answer == "Embedding is not available to your account."


def test_self_heal_compresses_a_scope_as_that_scopes_person(monkeypatch):
    """Not as whichever run triggered the self-heal."""
    from app.services import agent_memory, persistent_memory

    resolved_for = []

    async def _zero(*_args, **_kwargs):
        return 0

    async def _llm(_db, user_id):
        resolved_for.append(provider_access.canonical_id(user_id) or acting_user_id())
        return SimpleNamespace(), "m"

    async def _compress(*_args, **_kwargs):
        return False

    class _Redis:
        async def set(self, *_args, **_kwargs):
            return True

        async def expire(self, *_args, **_kwargs):
            return True

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def commit(self):
            return None

    async def _redis():
        return _Redis()

    for name in ("prune_old_memories", "enforce_memory_cap", "consolidate_agent_facts", "reembed_stale_memories"):
        monkeypatch.setattr(persistent_memory, name, _zero)
    monkeypatch.setattr(router, "get_llm_for_user", _llm)
    monkeypatch.setattr(agent_memory, "compress_medium_term_memory", _compress)
    monkeypatch.setattr("app.core.redis.redis_client", _redis)
    monkeypatch.setattr("app.core.database.async_session", lambda: _Session())

    async def _run(scope):
        bind_acting_user(HANA)  # the run that triggered it
        return await persistent_memory.auto_heal_agent_memory(str(uuid.uuid4()), scope_id=scope)

    assert asyncio.run(_run(None)) is True
    assert asyncio.run(_run(ALICE)) is True
    assert resolved_for == [None, ALICE]


def test_an_excluded_persons_memory_is_stored_without_a_retry_or_a_warning(caplog):
    """No vector by design is not a failure: one attempt, INFO, stored."""
    from unittest.mock import Mock, patch

    from app.services import persistent_memory as pm

    db = SimpleNamespace(add=Mock(), flush=AsyncMock(), execute=AsyncMock())
    embed = AsyncMock(side_effect=ProviderNotUsableError("excluded"))
    with patch.object(pm, "embed_text_with_model", new=embed), caplog.at_level("INFO", logger=pm.logger.name):
        entry = asyncio.run(
            pm.persist_memory(db, agent_id=str(uuid.uuid4()), content="Hana's fact", memory_type="fact")
        )

    assert embed.await_count == 1
    assert embed.await_args.kwargs["refuse_excluded"] is True
    assert entry.embedding is None
    db.add.assert_called_once_with(entry)
    levels = [r.levelname for r in caplog.records if "without embedding" in r.getMessage()]
    assert levels == ["INFO"]


def test_a_refused_semantic_memory_search_still_answers_its_keyword_matches(monkeypatch):
    """The text half reaches no provider: it answers, and the refusal of the
    semantic half travels in a header."""
    from datetime import UTC, datetime

    from fastapi import Response

    from app.api.v1 import agent_memory
    from app.services import persistent_memory

    agent = SimpleNamespace(id=uuid.uuid4())
    hit = SimpleNamespace(
        id=uuid.uuid4(), content="Samuel prefers YAML", memory_type="fact", source="chat",
        created_at=datetime(2026, 10, 6, tzinfo=UTC), subject_key=None, superseded_by_id=None,
    )

    async def _agent(*_args, **_kwargs):
        return agent

    async def _refused(*_args, **_kwargs):
        raise ProviderNotUsableError("Embedding is not available to your account.")

    class _Db:
        async def execute(self, _statement):
            # The text search reads each entry with whether it still holds.
            return _Rows([(hit, False)])

    monkeypatch.setattr(agent_memory, "get_user_agent", _agent)
    monkeypatch.setattr(agent_memory, "_is_memory_restricted", lambda *_args: False)
    monkeypatch.setattr(persistent_memory, "search_agent_memories", _refused)
    response = Response()

    results = asyncio.run(
        agent_memory.search_agent_memory(
            agent_id=str(agent.id),
            response=response,
            query="Samuel",
            user=SimpleNamespace(id=uuid.UUID(HANA), is_admin=False),
            db=_Db(),
            limit=10,
            _ai_gate=None,
        )
    )

    assert [(r["id"], r["match_type"]) for r in results] == [(str(hit.id), "text")]
    assert response.headers[agent_memory.SEMANTIC_SEARCH_REFUSED_HEADER] == (
        "Embedding is not available to your account."
    )
