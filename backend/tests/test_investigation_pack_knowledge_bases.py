"""Knowledge bases a Deep Investigation pack declares.

A pack names its corpora the way it names its URLs — by a slug stable across
installs, or by tags — and they are resolved for each caller into one source
per knowledge base. What is pinned here: a selector is well-formed or refused,
a corpus the caller cannot see is never claimed, the question the corpus is
asked is the one the step shows, and a passage under the relevance floor is
not a finding.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.models.investigation import DETAIL_CODE_PARAMS
from app.services import investigation_packs as packs
from app.services.investigation_packs import (
    VIGINUM_DOCTRINE,
    KnowledgeBaseSelector,
    SourceDefinition,
    SourcePack,
    disinfo_infrastructure_pack,
    plan_for,
    with_knowledge_bases,
)

KB = "11111111-1111-1111-1111-111111111111"
OTHER_KB = "22222222-2222-2222-2222-222222222222"


def _pack(*selectors: KnowledgeBaseSelector) -> SourcePack:
    return SourcePack(
        slug="method",
        label="Method",
        description="A pack with a method.",
        subject_types=packs.SUBJECT_TYPES,
        sources=(packs.WEB_GENERAL.sources[0],),
        knowledge_bases=selectors,
    )


# ── Selectors ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"slug": "a", "tags": ("b",)},
        {"slug": "a", "query_template": "no subject here"},
        {"slug": "a", "query_template": "{value} {other}"},
        {"slug": "a", "query_template": "{value.__class__}"},
        {"slug": "a", "query_template": "{value[0]}"},
        {"slug": "a", "query_template": "{value!r}"},
        {"slug": "a", "query_template": "{value:>40}"},
        {"slug": "a", "query_template": "{{value}} only escaped"},
        {"slug": "a", "min_similarity": 0},
        {"slug": "a", "min_similarity": 1.5},
        {"slug": "a", "subject_types": ("galaxy",)},
        {"slug": "a", "subject_types": ()},
    ],
)
def test_a_malformed_selector_is_refused(kwargs):
    with pytest.raises(ValueError):
        KnowledgeBaseSelector(**kwargs)


def test_a_template_reads_the_subject_and_nothing_of_it():
    selector = KnowledgeBaseSelector(slug="a", query_template="How to model {value}, and {value} again")
    assert selector.query_template.format(value="x") == "How to model x, and x again"


def test_a_slug_names_the_tag_the_seed_gives_a_built_in_corpus():
    selector = KnowledgeBaseSelector(slug="viginum-fimi-doctrine")
    assert selector.matches(["builtin", "slug:viginum-fimi-doctrine", "fimi"])
    assert selector.matches(["SLUG:Viginum-FIMI-Doctrine"])
    # A corpus merely ABOUT the same thing is not the one named.
    assert not selector.matches(["viginum-fimi-doctrine", "fimi"])
    assert not selector.matches(None)


def test_tags_name_every_corpus_carrying_all_of_them():
    selector = KnowledgeBaseSelector(tags=("fimi", "doctrine"))
    assert selector.matches(["Doctrine", "fimi", "extra"])
    assert not selector.matches(["fimi"])


# ── Expansion for one caller ────────────────────────────────────────────────


def test_each_declared_corpus_becomes_its_own_source_after_the_packs_own():
    selector = KnowledgeBaseSelector(tags=("fimi",), query_template="method for {value}", min_similarity=0.4)
    pack = with_knowledge_bases(_pack(selector), [(selector, [(KB, "Doctrine A"), (OTHER_KB, "Doctrine B")])])
    assert [s.name for s in pack.sources][1:] == ["Doctrine A", "Doctrine B"]
    added = pack.sources[1:]
    assert all(s.kind == "knowledge_base" for s in added)
    assert [s.knowledge_base_ids for s in added] == [(KB,), (OTHER_KB,)]
    assert {s.query_template for s in added} == {"method for {value}"}
    assert {s.min_similarity for s in added} == {0.4}
    # A declared corpus is planned work: the pack can afford it.
    assert pack.max_iterations >= len(pack.sources)


def test_a_corpus_two_selectors_name_is_planned_once():
    first = KnowledgeBaseSelector(slug="a")
    second = KnowledgeBaseSelector(tags=("fimi",))
    pack = with_knowledge_bases(_pack(first, second), [(first, [(KB, "A")]), (second, [(KB, "A")])])
    assert [s.name for s in pack.sources].count("A") == 1


def test_a_corpus_the_caller_cannot_see_is_said_not_claimed():
    pack = with_knowledge_bases(_pack(VIGINUM_DOCTRINE), [(VIGINUM_DOCTRINE, [])])
    assert [s.kind for s in pack.sources] == ["http"]
    assert "Not available to you" in pack.description
    assert "slug:viginum-fimi-doctrine" in pack.description


def test_a_pack_that_declares_nothing_is_returned_unchanged():
    pack = packs.WEB_GENERAL
    assert with_knowledge_bases(pack, []) is pack


def test_the_disinformation_pack_declares_the_viginum_doctrine():
    pack = disinfo_infrastructure_pack(None)
    assert pack.knowledge_bases == (VIGINUM_DOCTRINE,)
    # A campaign or an actor name is now a subject the pack can answer.
    for subject_type in ("campaign", "threat_actor", "unknown"):
        assert subject_type in pack.subject_types
    assert len(pack.subject_types) == len(set(pack.subject_types))
    expanded = with_knowledge_bases(pack, [(VIGINUM_DOCTRINE, [(KB, "VIGINUM FIMI Doctrine")])])
    _planned, executable = plan_for([expanded], "unknown", "Storm-1516")
    assert [s.name for _slug, s in executable] == ["VIGINUM FIMI Doctrine"]
    _planned, executable = plan_for([expanded], "domain", "yerevandaily.am")
    assert "VIGINUM FIMI Doctrine" in [s.name for _slug, s in executable]
    # An IP run models what it found too: the doctrine is the plan's
    # modelling action whatever the subject, run once the neighbours are read.
    _planned, executable = plan_for([expanded], "ip", "91.239.146.131")
    doctrine = [s for _slug, s in executable if s.name == "VIGINUM FIMI Doctrine"]
    assert [s.action for s in doctrine] == ["apply_doctrine"]


# ── The knowledge-base source: question, floor, identity, stored address ───


def _kb_source(**kw) -> SourceDefinition:
    return SourceDefinition(
        name="Doctrine", subject_types=("unknown",), kind="knowledge_base", knowledge_base_ids=(KB,), **kw
    )


def test_the_step_shows_the_question_the_corpus_is_asked():
    source = _kb_source(query_template="How to model {value}", min_similarity=0.5)
    assert source.knowledge_base_query("Storm-1516") == "How to model Storm-1516"
    asked = source.describe("Storm-1516")
    assert "'How to model Storm-1516'" in asked
    assert "cosine similarity >= 0.5" in asked
    # Without a template the subject is searched as it is, as before.
    assert _kb_source().knowledge_base_query("Storm-1516") == "Storm-1516"


def test_a_question_or_a_floor_is_refused_on_another_kind():
    with pytest.raises(ValueError):
        SourceDefinition(name="x", subject_types=("ip",), url_template="https://x/{value}", min_similarity=0.5)
    with pytest.raises(ValueError):
        SourceDefinition(name="x", subject_types=("ip",), url_template="https://x/{value}", query_template="{value}")


def test_two_questions_to_one_corpus_are_two_sources():
    plain = _kb_source()
    asked = _kb_source(query_template="How to model {value}")
    floored = _kb_source(min_similarity=0.5)
    assert len({plain.identity(), asked.identity(), floored.identity()}) == 3


def test_the_question_and_the_floor_survive_the_stored_address():
    source = _kb_source(query_template="How to model {value}", min_similarity=0.5)
    rebuilt = SourceDefinition.from_ref(source.to_ref(), subject_types=("unknown",))
    assert rebuilt.identity() == source.identity()
    # An address written before this change still reads as the plain search.
    legacy = SourceDefinition.from_ref(
        {"kind": "knowledge_base", "name": "Doctrine", "knowledge_base_ids": [KB]}, subject_types=("unknown",)
    )
    assert legacy.identity() == _kb_source().identity()


def test_a_stored_floor_that_is_not_a_number_is_refused():
    ref = {**_kb_source().to_ref(), "min_similarity": "high"}
    with pytest.raises(ValueError):
        SourceDefinition.from_ref(ref, subject_types=("unknown",))


# ── The querier: the question is what is searched, the floor is applied ────


def _hit(similarity: float, *, text: bool = False):
    return SimpleNamespace(
        id=str(uuid.uuid4()),
        content="A Channel object models a website used to spread a narrative, with its first publication date.",
        source_id=str(uuid.uuid4()),
        vector_score=similarity,
        text_rank=1 if text else None,
    )


def _install(monkeypatch, hits):
    import app.services.investigation_queriers as mod

    seen: dict = {}

    async def _embed(text, model=None):
        seen["embedded"] = text
        return [0.0] * 8

    async def _provider(_db):
        return SimpleNamespace(embed=_embed), "text-embedding-3-large", None

    async def _hybrid(*_a, **kw):
        seen["query_text"] = kw.get("query_text")
        seen["top_k"] = kw.get("top_k")
        return hits[: kw.get("top_k", len(hits))]

    class _Docs:
        async def execute(self, _stmt):
            return SimpleNamespace(all=lambda: [])

    @asynccontextmanager
    async def _session():
        yield _Docs()

    monkeypatch.setattr("app.llm.router.get_embedding_provider", _provider, raising=True)
    monkeypatch.setattr("app.services.hybrid_search.hybrid_search_embeddings", _hybrid, raising=True)
    monkeypatch.setattr("app.core.database.async_session", _session, raising=True)
    return mod.make_knowledge_base_querier(SimpleNamespace()), seen


@pytest.mark.asyncio
async def test_the_corpus_is_asked_the_packs_question(monkeypatch):
    querier, seen = _install(monkeypatch, [_hit(0.62)])
    source = _kb_source(query_template="How to model {value}")
    outcome = await querier(source, "Storm-1516", source.describe("Storm-1516"))
    assert seen["embedded"] == seen["query_text"] == "How to model Storm-1516"
    assert outcome.findings_count == 1


@pytest.mark.asyncio
async def test_passages_under_the_floor_are_not_findings(monkeypatch):
    querier, _ = _install(monkeypatch, [_hit(0.31), _hit(0.22)])
    source = _kb_source(min_similarity=0.5)
    outcome = await querier(source, "yerevandaily.am", "q")
    assert outcome.findings_count == 0
    assert outcome.error_type is None
    assert outcome.detail_code == "source.kb_below_floor"
    assert outcome.detail_params == {"floor": 0.5, "examined": 2}
    assert set(outcome.detail_params) == DETAIL_CODE_PARAMS["source.kb_below_floor"]


@pytest.mark.asyncio
async def test_the_floor_keeps_what_reaches_it_and_what_matched_the_question(monkeypatch):
    querier, _ = _install(monkeypatch, [_hit(0.58), _hit(0.2, text=True), _hit(0.3)])
    source = _kb_source(query_template="How to model {value}", min_similarity=0.5)
    outcome = await querier(source, "Storm-1516", "q")
    assert outcome.findings_count == 2


@pytest.mark.asyncio
async def test_a_bare_subject_in_an_unrelated_passage_does_not_pass_the_floor(monkeypatch):
    """One word can sit anywhere: without a question, the floor decides alone."""
    querier, _ = _install(monkeypatch, [_hit(0.58), _hit(0.2, text=True), _hit(0.3)])
    outcome = await querier(_kb_source(min_similarity=0.5), "Storm-1516", "q")
    assert outcome.findings_count == 1
    only_text, _ = _install(monkeypatch, [_hit(0.2, text=True)])
    below = await only_text(_kb_source(min_similarity=0.5), "Storm-1516", "q")
    assert below.findings_count == 0
    assert below.detail_code == "source.kb_below_floor"


@pytest.mark.asyncio
async def test_a_floor_examines_a_wider_pool_than_it_shows(monkeypatch):
    """The floor filters what the fused ranking returned: a relevant passage
    ranked past the first page is found, not reported as nothing relevant."""
    from app.services.investigation_queriers import KB_FLOOR_POOL, KB_TOP_K

    hits = [_hit(0.2) for _ in range(KB_TOP_K + 4)] + [_hit(0.61)]
    querier, seen = _install(monkeypatch, hits)
    outcome = await querier(_kb_source(min_similarity=0.5), "Storm-1516", "q")
    assert seen["top_k"] == KB_FLOOR_POOL + 1
    assert outcome.findings_count == 1
    assert outcome.partial is False, "relevance ran out inside the pool"


@pytest.mark.asyncio
async def test_a_floor_says_the_corpus_holds_more_only_while_relevance_lasts(monkeypatch):
    from app.services.investigation_queriers import KB_FLOOR_POOL, KB_TOP_K

    every_one_relevant = [_hit(0.7) for _ in range(KB_FLOOR_POOL + 1)]
    querier, _ = _install(monkeypatch, every_one_relevant)
    outcome = await querier(_kb_source(min_similarity=0.5), "Storm-1516", "q")
    assert outcome.findings_count == KB_TOP_K
    assert outcome.detail_code == "source.kb_capped"
    assert outcome.partial is True

    few_relevant = [_hit(0.7) for _ in range(3)] + [_hit(0.2) for _ in range(KB_FLOOR_POOL)]
    querier, _ = _install(monkeypatch, few_relevant)
    outcome = await querier(_kb_source(min_similarity=0.5), "Storm-1516", "q")
    assert outcome.findings_count == 3
    assert outcome.detail_code is None and outcome.partial is False


@pytest.mark.asyncio
async def test_without_a_floor_the_search_is_read_as_before(monkeypatch):
    from app.services.investigation_queriers import KB_TOP_K

    querier, seen = _install(monkeypatch, [_hit(0.1)])
    outcome = await querier(_kb_source(), "Storm-1516", "q")
    assert outcome.findings_count == 1
    assert outcome.detail_code is None
    assert seen["top_k"] == KB_TOP_K + 1


@pytest.mark.asyncio
async def test_a_corpus_checked_against_the_caller_is_searched_by_its_id(monkeypatch):
    """The visibility re-check hands back the ids the database stores.

    They are UUID objects, and the search parses every id with `uuid.UUID`,
    which only takes a string: every corpus search made for a caller failed
    with `'UUID' object has no attribute 'replace'` before this.
    """
    import app.services.investigation_queriers as mod

    searched: dict = {}

    async def _embed(text, model=None):
        return [0.0] * 8

    async def _provider(_db):
        return SimpleNamespace(embed=_embed), "text-embedding-3-large", None

    async def _hybrid(*_a, **kw):
        searched["ids"] = [uuid.UUID(kid) for kid in kw["knowledge_base_ids"]]
        return [_hit(0.7)]

    async def _groups(_db, _uid):
        return []

    class _Db:
        async def execute(self, _stmt):
            return SimpleNamespace(scalars=lambda: iter([uuid.UUID(KB)]), all=lambda: [])

    @asynccontextmanager
    async def _session():
        yield _Db()

    monkeypatch.setattr("app.llm.router.get_embedding_provider", _provider, raising=True)
    monkeypatch.setattr("app.services.hybrid_search.hybrid_search_embeddings", _hybrid, raising=True)
    monkeypatch.setattr("app.api.v1.helpers.get_user_group_ids", _groups, raising=True)
    monkeypatch.setattr("app.core.database.async_session", _session, raising=True)
    querier = mod.make_knowledge_base_querier(SimpleNamespace(), user_id=str(uuid.uuid4()))
    outcome = await querier(_kb_source(), "Storm-1516", "q")
    assert outcome.error_type is None, outcome.detail
    assert searched["ids"] == [uuid.UUID(KB)]


# ── The orchestrator resolves a declaring built-in pack for its caller ──────


@pytest.mark.asyncio
async def test_a_built_in_pack_that_declares_corpora_is_resolved_for_the_caller(monkeypatch):
    """The all-built-ins shortcut skips the session; it must not skip this."""
    from app.services import investigation_pack_resolver as resolver
    from app.services.investigation_orchestrator import DeepInvestigationOrchestrator

    declaring = _pack(KnowledgeBaseSelector(slug="doctrine"))
    monkeypatch.setitem(packs.BUILTIN_PACKS, declaring.slug, declaring)
    calls: list = []

    async def _resolve_selected(_db, user, slugs):
        calls.append((user, slugs))
        return [with_knowledge_bases(declaring, [(declaring.knowledge_bases[0], [(KB, "Doctrine")])])]

    monkeypatch.setattr(resolver, "resolve_selected", _resolve_selected)
    orchestrator = DeepInvestigationOrchestrator(SimpleNamespace(), querier=None)

    async def _load_user(_user_id):
        return "the-caller"

    monkeypatch.setattr(orchestrator, "_load_user", _load_user)
    resolved = await orchestrator._resolve_packs("unknown", ["method"], "u")
    assert calls == [("the-caller", ["method"])]
    assert "Doctrine" in [s.name for s in resolved[0].sources]


@pytest.mark.asyncio
async def test_a_recommended_pack_that_declares_corpora_is_resolved_too(monkeypatch):
    from app.services import investigation_orchestrator as orch_mod
    from app.services import investigation_pack_resolver as resolver
    from app.services.investigation_orchestrator import DeepInvestigationOrchestrator

    declaring = _pack(KnowledgeBaseSelector(slug="doctrine"))
    monkeypatch.setattr(orch_mod, "recommend_packs", lambda _t: [declaring])

    async def _expand(_db, user, packs_):
        assert user == "the-caller"
        return [with_knowledge_bases(p, [(p.knowledge_bases[0], [(KB, "Doctrine")])]) for p in packs_]

    monkeypatch.setattr(resolver, "expand_declared_knowledge_bases", _expand)
    orchestrator = DeepInvestigationOrchestrator(SimpleNamespace(), querier=None)

    async def _load_user(_user_id):
        return "the-caller"

    monkeypatch.setattr(orchestrator, "_load_user", _load_user)
    resolved = await orchestrator._resolve_packs("unknown", None, "u")
    assert "Doctrine" in [s.name for s in resolved[0].sources]
