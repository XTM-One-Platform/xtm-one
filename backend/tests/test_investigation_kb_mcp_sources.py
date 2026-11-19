"""Knowledge-base and MCP tool sources, and the citations they produce.

Before this, "a source" and "an HTTP endpoint" were the same thing: a
`SourceDefinition` carried a `url_template` and nothing else. A knowledge base
and an MCP tool are both sources an analyst would put in a pack, and neither
has a URL to fetch.

What is under test is mostly the CITATION, because that is the part a reader
acts on:

* a knowledge-base hit cites the DOCUMENT, by name — not the chunk, and not
  the `source_id` UUID the search actually returns;
* an MCP result cites the SERVER and the TOOL that answered;
* neither carries an `href`, because neither has an address the reader is
  guaranteed to be able to open, and the block renders an href-less citation
  as text rather than as a dead link.

The MCP server is mocked in-process. A test that needed a real MCP server
would be testing the server; what matters here is the mapping from what a tool
returns onto a step's status and citation, which is entirely ours.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.services.investigation_orchestrator import Evidence, SourceOutcome
from app.services.investigation_packs import SOURCE_KINDS, SourceDefinition


def _async_runtime(build):
    """``build_mcp_runtime`` is a coroutine (it reads vault references)."""

    async def _build(server, _credential=None):
        return build(server)

    return _build


# ── The source kinds themselves ─────────────────────────────────────────────


def test_a_knowledge_base_source_needs_a_knowledge_base() -> None:
    """A KB source naming no KB would search nothing and report `empty`.

    That is indistinguishable from a corpus which genuinely holds no match —
    exactly the confusion the status vocabulary exists to prevent — so it is
    refused at construction instead.
    """
    with pytest.raises(ValueError, match="names no knowledge base"):
        SourceDefinition(name="Corpus", subject_types=("cve",), kind="knowledge_base")


def test_an_mcp_source_needs_a_server_and_a_tool() -> None:
    with pytest.raises(ValueError, match="names no server/tool"):
        SourceDefinition(name="Tool", subject_types=("cve",), kind="mcp_tool")


def test_an_http_source_still_needs_a_template() -> None:
    with pytest.raises(ValueError, match="carries no url_template"):
        SourceDefinition(name="Web", subject_types=("cve",))


def test_only_http_sources_have_a_url() -> None:
    """`build_url` refuses rather than inventing one.

    The orchestrator used to key its de-duplication on the built URL; letting
    a KB source return an empty string there would have collapsed every
    non-HTTP source into a single identity.
    """
    kb = SourceDefinition(
        name="Corpus", subject_types=("cve",), kind="knowledge_base",
        knowledge_base_ids=("11111111-1111-1111-1111-111111111111",),
    )
    with pytest.raises(ValueError, match="it has no URL"):
        kb.build_url("CVE-2024-3400")


def test_every_kind_describes_what_it_was_asked() -> None:
    """The step's stored `query` must not be blank for two kinds out of three.

    "What did this source get asked?" is the first question an analyst has
    about a surprising result.
    """
    http = SourceDefinition(
        name="NVD", subject_types=("cve",), url_template="https://x.test/{value}"
    )
    kb = SourceDefinition(
        name="Corpus", subject_types=("cve",), kind="knowledge_base",
        knowledge_base_ids=("11111111-1111-1111-1111-111111111111",),
    )
    mcp = SourceDefinition(
        name="Lookup", subject_types=("cve",), kind="mcp_tool",
        mcp_server_id="22222222-2222-2222-2222-222222222222",
        mcp_tool_name="lookup_cve", mcp_argument="cve_id",
    )
    assert http.describe("CVE-1") == "https://x.test/CVE-1"
    assert "knowledge base" in kb.describe("CVE-1")
    assert kb.describe("CVE-1").endswith("'CVE-1'")
    assert mcp.describe("CVE-1") == "lookup_cve(cve_id='CVE-1')"


def test_identity_is_kind_aware() -> None:
    """Two KB sources are only the same if they name the same corpora.

    Keying on `url_template` (as the first version did) made every non-HTTP
    source share the empty string, so a pack with two different knowledge
    bases would have planned only one of them.
    """
    a = SourceDefinition(
        name="A", subject_types=("cve",), kind="knowledge_base",
        knowledge_base_ids=("11111111-1111-1111-1111-111111111111",),
    )
    b = SourceDefinition(
        name="B", subject_types=("cve",), kind="knowledge_base",
        knowledge_base_ids=("33333333-3333-3333-3333-333333333333",),
    )
    same_as_a = SourceDefinition(
        name="A again", subject_types=("cve",), kind="knowledge_base",
        knowledge_base_ids=("11111111-1111-1111-1111-111111111111",),
    )
    assert a.identity() != b.identity()
    assert a.identity() == same_as_a.identity()
    # Pinned so a new kind cannot be added without deciding what makes two of
    # them the same source — the bug this guards is a kind whose identity
    # collapses into another's and silently de-duplicates it away.
    # The two infrastructure kinds decide theirs in
    # `test_investigation_disinfo_pack.py`, `opencti` in
    # `test_investigation_opencti.py`.
    assert SOURCE_KINDS == (
        "http",
        "knowledge_base",
        "mcp_tool",
        "web_search",
        "site_fingerprint",
        "passive_dns",
        "opencti",
    )


# ── The MCP querier, against a mocked server ────────────────────────────────


class _FakeManager:
    """An MCP manager that answers from a script, in-process.

    Mirrors only what the querier uses: connect, list tools, execute, close.
    """

    def __init__(self, *, tools=("lookup_cve",), result=None, raises=None):
        self._tools = tools
        self._result = result
        self._raises = raises
        self.disconnected = False
        self.calls: list = []

    async def connect_server(self, _runtime):
        if isinstance(self._raises, Exception) and self._tools is None:
            raise self._raises
        return [SimpleNamespace(name=name) for name in (self._tools or ())]

    async def execute_tool(self, call):
        self.calls.append(call)
        if self._raises is not None:
            raise self._raises
        return self._result

    async def disconnect_all(self):
        self.disconnected = True


def _install_mcp(monkeypatch, manager, *, server=None):
    """Point the querier at the fake manager and a fake server row."""
    import app.services.investigation_queriers as mod

    srv = server if server is not None else SimpleNamespace(
        id=uuid.uuid4(), name="Mock CTI", enabled=True
    )
    if srv is not None and not hasattr(srv, "investigation_tool_name"):
        srv.investigation_tool_name = "lookup_cve"

    class _DB:
        async def get(self, _model, _pk, **_kw):
            return srv

    monkeypatch.setattr(
        "app.tools.mcp_client.MCPManager", lambda: manager, raising=True
    )
    monkeypatch.setattr(
        "app.api.v1.helpers.build_mcp_runtime",
        _async_runtime(lambda _s: SimpleNamespace()),
        raising=True,
    )
    return mod.make_mcp_querier(_DB()), srv


def _mcp_source(server_id: str) -> SourceDefinition:
    return SourceDefinition(
        name="CTI lookup", subject_types=("cve",), kind="mcp_tool",
        mcp_server_id=server_id, mcp_tool_name="lookup_cve", mcp_argument="cve_id",
    )


@pytest.mark.asyncio
async def test_a_withdrawn_designation_stops_the_call(monkeypatch) -> None:
    """Re-authorised at call time: a designation withdrawn while the run was
    queued must not let it call the tool."""
    manager = _FakeManager(result=SimpleNamespace(content="x", is_error=False))
    srv = SimpleNamespace(
        id=uuid.uuid4(), name="Mock CTI", enabled=True, investigation_tool_name=None
    )
    querier, _ = _install_mcp(monkeypatch, manager, server=srv)

    outcome = await querier(_mcp_source(str(srv.id)), "CVE-2024-3400", "q")

    assert outcome.error_type == "not_found"
    assert outcome.detail_code == "source.mcp_tool_missing"
    assert manager.calls == []


@pytest.mark.asyncio
async def test_a_pinned_argument_leaving_other_required_inputs_is_refused(monkeypatch) -> None:
    """The call would only fail the tool's own validation: said up front."""
    tool = SimpleNamespace(
        name="lookup_cve",
        input_schema={
            "type": "object",
            "properties": {"cve_id": {"type": "string"}, "repository": {"type": "string"}},
            "required": ["cve_id", "repository"],
        },
    )
    manager = _FakeManager(result=SimpleNamespace(content="x", is_error=False))
    manager.connect_server = _connect_returning([tool])
    querier, srv = _install_mcp(monkeypatch, manager)

    outcome = await querier(_mcp_source(str(srv.id)), "CVE-2024-3400", "q")

    assert outcome.detail_code == "source.mcp_argument_ambiguous"
    assert manager.calls == []


def _connect_returning(tools):
    async def _connect(_runtime):
        return tools

    return _connect


@pytest.mark.asyncio
async def test_a_server_the_caller_can_no_longer_see_is_not_called(monkeypatch) -> None:
    import app.services.investigation_queriers as mod

    manager = _FakeManager(result=SimpleNamespace(content="x", is_error=False))
    srv = SimpleNamespace(
        id=uuid.uuid4(), name="Mock CTI", enabled=True, investigation_tool_name="lookup_cve"
    )

    class _UnsharedDB:
        async def get(self, _model, _pk, **_kw):
            return srv

        async def execute(self, _stmt):
            # The caller's groups, then the visibility probe: nothing visible.
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: []),
                scalar_one_or_none=lambda: None,
            )

    _install_mcp(monkeypatch, manager, server=srv)
    querier = mod.make_mcp_querier(_UnsharedDB(), user_id=str(uuid.uuid4()))

    outcome = await querier(_mcp_source(str(srv.id)), "CVE-2024-3400", "q")

    assert outcome.detail_code == "source.mcp_server_missing"
    assert manager.calls == []


@pytest.mark.asyncio
async def test_an_mcp_answer_cites_the_server_and_the_tool(monkeypatch) -> None:
    manager = _FakeManager(
        result=SimpleNamespace(content="CVE-2024-3400: PAN-OS command injection", is_error=False)
    )
    querier, srv = _install_mcp(monkeypatch, manager)
    outcome = await querier(_mcp_source(str(srv.id)), "CVE-2024-3400", "q")

    assert outcome.error_type is None
    assert outcome.findings_count == 1
    assert outcome.evidence == (
        Evidence(
            kind="tool_result",
            label="Mock CTI · lookup_cve",
            # The tool's own output, retained: a report that must cite has
            # nothing to ground a sentence in otherwise, and would invent it.
            quote="CVE-2024-3400: PAN-OS command injection",
        ),
    )
    # No href: an MCP result has no address, and the block renders it as text.
    assert outcome.evidence[0].href is None
    # And no span: there is no prose of ours for it to index into.
    assert outcome.evidence[0].anchored is False
    # The subject reached the tool under the pack's declared argument name.
    assert manager.calls[0].arguments == {"cve_id": "CVE-2024-3400"}
    assert manager.disconnected, "the connection must be closed even on success"


@pytest.mark.asyncio
async def test_an_mcp_tool_that_answers_nothing_is_empty_not_an_error(monkeypatch) -> None:
    """The same distinction the web querier draws for a page that said nothing.

    The tool works; the subject is simply not in it. Calling that an error
    would put a red step next to a source that behaved correctly.
    """
    manager = _FakeManager(result=SimpleNamespace(content="   ", is_error=False))
    querier, srv = _install_mcp(monkeypatch, manager)
    outcome = await querier(_mcp_source(str(srv.id)), "CVE-2024-3400", "q")

    assert outcome.error_type is None
    assert outcome.findings_count == 0
    # Still cited: the tool DID answer, and which one answered is provenance
    # worth keeping even when the answer was empty.
    assert outcome.evidence[0].label == "Mock CTI · lookup_cve"


@pytest.mark.asyncio
async def test_a_tool_that_refuses_keeps_its_own_message(monkeypatch) -> None:
    manager = _FakeManager(
        result=SimpleNamespace(content="rate limit exceeded, retry in 60s", is_error=True)
    )
    querier, srv = _install_mcp(monkeypatch, manager)
    outcome = await querier(_mcp_source(str(srv.id)), "CVE-2024-3400", "q")

    assert outcome.error_type == "invalid_response"
    assert "rate limit exceeded" in (outcome.detail or "")


@pytest.mark.asyncio
async def test_a_missing_tool_is_not_found_not_unreachable(monkeypatch) -> None:
    """Connected, but the pack names a tool the server does not offer.

    Distinguished from a connection failure because the remediation differs:
    one is a pack to update, the other a server to bring back.
    """
    manager = _FakeManager(tools=("something_else",))
    querier, srv = _install_mcp(monkeypatch, manager)
    outcome = await querier(_mcp_source(str(srv.id)), "CVE-2024-3400", "q")

    assert outcome.error_type == "not_found"
    assert "lookup_cve" in (outcome.detail or "")


@pytest.mark.asyncio
async def test_a_disabled_server_is_unreachable(monkeypatch) -> None:
    srv = SimpleNamespace(id=uuid.uuid4(), name="Mock CTI", enabled=False)
    querier, _ = _install_mcp(monkeypatch, _FakeManager(), server=srv)
    outcome = await querier(_mcp_source(str(srv.id)), "CVE-2024-3400", "q")

    assert outcome.error_type == "unreachable"
    assert "disabled" in (outcome.detail or "")


@pytest.mark.asyncio
async def test_a_timeout_maps_to_timeout_and_still_disconnects(monkeypatch) -> None:
    """The taxonomy matters: `timeout` and `unreachable` remediate differently."""
    manager = _FakeManager(raises=TimeoutError("took too long"))
    querier, srv = _install_mcp(monkeypatch, manager)
    outcome = await querier(_mcp_source(str(srv.id)), "CVE-2024-3400", "q")

    assert outcome.error_type == "timeout"
    assert manager.disconnected, "a failed step must not leak the connection"


@pytest.mark.asyncio
async def test_a_server_that_vanished_is_not_found(monkeypatch) -> None:
    """A pack naming a deleted server is a pack problem, not a network one."""
    import app.services.investigation_queriers as mod

    class _EmptyDB:
        async def get(self, _model, _pk, **_kw):
            return None

    querier = mod.make_mcp_querier(_EmptyDB())
    outcome = await querier(_mcp_source(str(uuid.uuid4())), "CVE-2024-3400", "q")
    assert outcome.error_type == "not_found"
    assert "no longer exists" in (outcome.detail or "")


# ── Routing ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_dispatcher_routes_by_kind() -> None:
    import app.services.investigation_queriers as mod

    seen: list[str] = []

    def _recorder(tag):
        async def _q(source, subject, query):
            seen.append(tag)
            return SourceOutcome(findings_count=1)
        return _q

    dispatcher = mod.make_dispatching_querier(SimpleNamespace(), http_querier=_recorder("http"))
    http = SourceDefinition(name="Web", subject_types=("cve",), url_template="https://x.test/{value}")
    await dispatcher(http, "CVE-1", "q")
    assert seen == ["http"]


@pytest.mark.asyncio
async def test_an_unknown_kind_fails_one_step_not_the_run() -> None:
    """A pack naming a kind this build cannot answer is a configuration
    problem, and should show as one failed source rather than a dead run."""
    import app.services.investigation_queriers as mod

    dispatcher = mod.make_dispatching_querier(SimpleNamespace())
    rogue = SourceDefinition(name="Web", subject_types=("cve",), url_template="https://x.test/{value}")
    object.__setattr__(rogue, "kind", "telepathy")
    outcome = await dispatcher(rogue, "CVE-1", "q")
    assert outcome.error_type == "invalid_response"
    assert "telepathy" in (outcome.detail or "")


# ── The knowledge-base querier ──────────────────────────────────────────────


def _kb_source(*kb_ids: str) -> SourceDefinition:
    return SourceDefinition(
        name="Threat corpus", subject_types=("cve",), kind="knowledge_base",
        knowledge_base_ids=kb_ids or ("11111111-1111-1111-1111-111111111111",),
    )


class _FakeDocsDB:
    """A session that answers the document-name resolve, and nothing else."""

    def __init__(self, names: dict[uuid.UUID, str]):
        self._names = names
        self.queries = 0

    async def execute(self, _stmt):
        self.queries += 1
        rows = [(doc_id, name) for doc_id, name in self._names.items()]
        return SimpleNamespace(all=lambda: rows)


def _install_kb(monkeypatch, *, hits, names, embed=None, user_id=None, captured=None):
    import app.services.investigation_queriers as mod

    captured = {} if captured is None else captured

    async def _provider_embed(_text, model=None):
        return [0.0] * 8

    provider = SimpleNamespace(embed=_provider_embed)

    async def _get_embedding_provider(_db):
        if embed is not None:
            raise embed
        return provider, "text-embedding-3-large", None

    async def _hybrid(*_a, **kw):
        if isinstance(hits, Exception):
            raise hits
        # Honours `top_k`, because the whole cap question is what comes back
        # when MORE than the cap exists. A stub that ignored it would make the
        # distinction untestable — which is how the off-by-one got here.
        captured["top_k"] = kw.get("top_k")
        captured["user_id"] = kw.get("user_id")
        captured["knowledge_base_ids"] = kw.get("knowledge_base_ids")
        return hits[: kw.get("top_k", len(hits))]

    monkeypatch.setattr("app.llm.router.get_embedding_provider", _get_embedding_provider, raising=True)
    monkeypatch.setattr("app.services.hybrid_search.hybrid_search_embeddings", _hybrid, raising=True)
    db = _FakeDocsDB(names)
    db.captured = captured
    _route_sessions_to(monkeypatch, db)
    return mod.make_knowledge_base_querier(SimpleNamespace(), user_id=user_id), db


def _route_sessions_to(monkeypatch, db) -> None:
    """The querier reads on a short session of its own: hand it *db*."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _session():
        yield db

    monkeypatch.setattr("app.core.database.async_session", _session, raising=True)


@pytest.fixture(autouse=True)
def _sessions_never_reach_a_database(monkeypatch) -> None:
    _route_sessions_to(monkeypatch, SimpleNamespace())


def _hit(source_id: uuid.UUID):
    """A `HybridResult`-shaped row: it carries a `source_id` and NO name."""
    return SimpleNamespace(id=str(uuid.uuid4()), content="…", source_id=str(source_id))


@pytest.mark.asyncio
async def test_a_knowledge_base_hit_cites_the_document_by_name(monkeypatch) -> None:
    """The citation resolves the name; it does not print the id.

    `HybridResult` carries a `source_id` and no filename, so citing it
    directly would render every knowledge-base citation as a bare UUID —
    technically provenance, and useless to the analyst it is for.
    """
    doc = uuid.uuid4()
    querier, _db = _install_kb(
        monkeypatch, hits=[_hit(doc)], names={doc: "APT29-campaign-2024.pdf"}
    )
    outcome = await querier(_kb_source(), "CVE-2024-3400", "q")

    assert outcome.error_type is None
    assert outcome.findings_count == 1
    assert outcome.evidence == (
        Evidence(
            kind="document",
            label="APT29-campaign-2024.pdf",
            # The matched passage, retained for the same reason: a synthesis
            # given only "this document matched" has nothing to cite.
            quote="…",
        ),
    )
    # No href: the reader may not have access to the knowledge base at all.
    assert outcome.evidence[0].href is None
    assert outcome.evidence[0].anchored is False


@pytest.mark.asyncio
async def test_passages_from_one_document_count_once(monkeypatch) -> None:
    """Distinct DOCUMENTS are counted, not chunks.

    Three passages from one report are one source of evidence; reporting
    "3 findings" for them would inflate the coverage the block shows.
    """
    doc = uuid.uuid4()
    querier, _ = _install_kb(
        monkeypatch, hits=[_hit(doc), _hit(doc), _hit(doc)], names={doc: "one-report.pdf"}
    )
    outcome = await querier(_kb_source(), "CVE-2024-3400", "q")

    assert outcome.findings_count == 1
    assert len(outcome.evidence) == 1
    # And one query resolved the whole page, rather than one per hit.
    assert outcome.evidence[0].label == "one-report.pdf"


@pytest.mark.asyncio
async def test_a_corpus_with_no_match_is_empty_not_an_error(monkeypatch) -> None:
    querier, _ = _install_kb(monkeypatch, hits=[], names={})
    outcome = await querier(_kb_source(), "CVE-2024-3400", "q")

    assert outcome.error_type is None
    assert outcome.findings_count == 0
    assert outcome.evidence == ()


@pytest.mark.asyncio
async def test_no_embedding_provider_is_unreachable_not_invalid(monkeypatch) -> None:
    """Nothing about the corpus is wrong — the thing that turns a question
    into a vector is simply not answering, and the remediation differs."""
    querier, _ = _install_kb(
        monkeypatch, hits=[], names={}, embed=RuntimeError("no embedding provider configured")
    )
    outcome = await querier(_kb_source(), "CVE-2024-3400", "q")

    assert outcome.error_type == "unreachable"
    assert "embedding provider" in (outcome.detail or "")


@pytest.mark.asyncio
async def test_a_failing_search_is_invalid_response(monkeypatch) -> None:
    querier, _ = _install_kb(monkeypatch, hits=RuntimeError("pgvector exploded"), names={})
    outcome = await querier(_kb_source(), "CVE-2024-3400", "q")
    assert outcome.error_type == "invalid_response"


@pytest.mark.asyncio
async def test_a_capped_read_is_degraded_not_complete(monkeypatch) -> None:
    """More than `KB_TOP_K` passages exist, so the corpus WAS sampled.

    Reporting that as a clean `completed` would say the corpus was read when it
    was not.
    """
    import app.services.investigation_queriers as mod

    docs = [uuid.uuid4() for _ in range(mod.KB_TOP_K + 3)]
    querier, _ = _install_kb(
        monkeypatch,
        hits=[_hit(d) for d in docs],
        names={d: f"doc-{i}.pdf" for i, d in enumerate(docs)},
    )
    outcome = await querier(_kb_source(), "CVE-2024-3400", "q")

    assert outcome.partial is True
    assert "holds more" in (outcome.detail or "")
    assert outcome.detail_code == "source.kb_capped"
    assert outcome.detail_params == {"top_k": mod.KB_TOP_K}
    assert outcome.findings_count == mod.KB_TOP_K, (
        "the extra passage is a probe for 'is there more', not a result"
    )


@pytest.mark.asyncio
async def test_a_corpus_read_to_the_last_passage_is_not_degraded(monkeypatch) -> None:
    """Exactly `KB_TOP_K` passages exist and all of them came back.

    That corpus was read COMPLETELY, and calling it `degraded` with "the corpus
    holds more" is a lie in the direction that matters: the whole point of this
    taxonomy is that `degraded` means something, and a false degraded costs the
    same credibility as a false success. The boundary is the bug — one
    document chunked into exactly `KB_TOP_K` passages hits it.
    """
    import app.services.investigation_queriers as mod

    docs = [uuid.uuid4() for _ in range(mod.KB_TOP_K)]
    querier, _ = _install_kb(
        monkeypatch,
        hits=[_hit(d) for d in docs],
        names={d: f"doc-{i}.pdf" for i, d in enumerate(docs)},
    )
    outcome = await querier(_kb_source(), "CVE-2024-3400", "q")

    assert outcome.partial is False
    assert outcome.detail is None
    assert outcome.detail_code is None
    assert outcome.findings_count == mod.KB_TOP_K


@pytest.mark.asyncio
async def test_one_document_chunked_to_the_cap_is_not_degraded(monkeypatch) -> None:
    """The review's own example: `results` are CHUNKS, `findings_count` is
    DOCUMENTS. One report split into `KB_TOP_K` passages filled the page
    without the corpus holding anything else."""
    import app.services.investigation_queriers as mod

    doc = uuid.uuid4()
    querier, _ = _install_kb(
        monkeypatch,
        hits=[_hit(doc) for _ in range(mod.KB_TOP_K)],
        names={doc: "one-report.pdf"},
    )
    outcome = await querier(_kb_source(), "CVE-2024-3400", "q")

    assert outcome.findings_count == 1
    assert outcome.partial is False


@pytest.mark.asyncio
async def test_a_document_deleted_mid_search_is_still_cited(monkeypatch) -> None:
    """The passage genuinely came from somewhere.

    Dropping it would understate what the step read; naming it "deleted
    document" says what is true without inventing a title.
    """
    doc = uuid.uuid4()
    querier, _ = _install_kb(monkeypatch, hits=[_hit(doc)], names={})
    outcome = await querier(_kb_source(), "CVE-2024-3400", "q")

    assert outcome.findings_count == 1
    assert outcome.evidence[0].label == "deleted document"


# ── Placing the subject in an MCP tool's arguments ──────────────────────────
#
# A designation names a TOOL. Which of its parameters the subject belongs in is
# read from the tool's own schema, and the interesting cases are the ones where
# that cannot be answered — because the alternative to refusing is putting the
# subject into a parameter chosen by coincidence, on a tool that may do
# anything.


def _arg(schema):
    from app.services.investigation_queriers import _subject_argument

    return _subject_argument(schema)


def test_the_single_required_text_parameter_takes_the_subject() -> None:
    assert _arg({
        "properties": {"indicator": {"type": "string"}, "limit": {"type": "integer"}},
        "required": ["indicator"],
    }) == "indicator"


def test_two_required_text_parameters_place_nothing() -> None:
    """Which one is the subject is exactly what this refuses to guess.

    `lookup(repository, query)` would otherwise receive the subject as the
    repository — a well-formed call that asks the wrong question and reports
    its answer as evidence.
    """
    assert _arg({
        "properties": {"repository": {"type": "string"}, "query": {"type": "string"}},
        "required": ["repository", "query"],
    }) is None


def test_a_required_non_text_parameter_places_nothing() -> None:
    """The call would fail on the missing parameter anyway; failing here says why."""
    assert _arg({
        "properties": {"query": {"type": "string"}, "board_id": {"type": "integer"}},
        "required": ["query", "board_id"],
    }) is None
    assert _arg({
        "properties": {"since": {"type": "integer"}},
        "required": ["since"],
    }) is None


def test_an_optional_parameter_is_used_only_when_it_is_the_only_one() -> None:
    """Tools that require nothing are common; two optional strings still tie."""
    assert _arg({"properties": {"q": {"type": "string"}}, "required": []}) == "q"
    assert _arg({
        "properties": {"q": {"type": "string"}, "lang": {"type": "string"}}
    }) is None


def test_an_untyped_or_union_parameter_counts_as_text() -> None:
    """JSON Schema leaves `type` optional, and a union may include string.

    Treating either as "not text" would refuse tools that work perfectly well.
    """
    assert _arg({"properties": {"q": {}}, "required": ["q"]}) == "q"
    assert _arg({
        "properties": {"q": {"type": ["string", "null"]}}, "required": ["q"]
    }) == "q"


def test_a_missing_or_malformed_schema_places_nothing() -> None:
    for schema in (None, {}, {"properties": None}, {"properties": []}, "nonsense"):
        assert _arg(schema) is None


def test_a_real_fastmcp_schema_resolves() -> None:
    """The shape an actual MCP SDK server emits, verbatim.

    Every other case here is hand-written, which proves the rules and not that
    they match what a real server sends. This schema was captured from
    `scripts/mock-cti-mcp-server.py` over a real stdio connection — note the
    `title` keys FastMCP adds, which a stricter reading of "properties" would
    have tripped over.
    """
    assert _arg({
        "properties": {"indicator": {"title": "Indicator", "type": "string"}},
        "required": ["indicator"],
        "title": "lookup_indicatorArguments",
        "type": "object",
    }) == "indicator"


def test_an_unpinned_source_says_the_argument_is_decided_later() -> None:
    """The plan is rendered before the tool is called, so `describe` must not
    name an argument the source has not chosen."""
    source = SourceDefinition(
        name="Registry", subject_types=("cve",), kind="mcp_tool",
        mcp_server_id=str(uuid.uuid4()), mcp_tool_name="lookup",
    )
    assert source.describe("CVE-2024-3400") == "lookup(…='CVE-2024-3400')"

    pinned = SourceDefinition(
        name="Registry", subject_types=("cve",), kind="mcp_tool",
        mcp_server_id=str(uuid.uuid4()), mcp_tool_name="lookup", mcp_argument="q",
    )
    assert pinned.describe("CVE-2024-3400") == "lookup(q='CVE-2024-3400')"


# ── Multi-hop: the pages a search found, read for themselves ────────────────
#
# The loop has always supported leads and no querier produced any, so the
# iterative half of "iterative multi-hop research" was never exercised on a
# real run. A lead here is another SOURCE for the same subject — so the useful
# ones are the pages the provider cited: it read them and gave back a sentence
# each, and fetching them gives the run their own text to quote.


def _search_answer(*pairs):
    from app.services.web_search import SearchAnswer, SearchCitation

    return SearchAnswer(
        text="answer",
        citations=tuple(SearchCitation(url=u, title=t) for u, t in pairs),
    )


def test_the_most_cited_pages_become_second_hop_steps() -> None:
    """Ranked by how often the answer leaned on each page — the provider's
    own signal about which ones carried the answer."""
    from app.services.investigation_queriers import _pages_worth_reading

    leads = _pages_worth_reading(
        _search_answer(
            ("https://minor.example/", "Minor"),
            ("https://major.example/", "Major"),
            ("https://major.example/", "Major"),
        )
    )
    assert [lead.name for lead in leads] == ["Major", "Minor"]
    assert all(lead.kind == "http" for lead in leads)


def test_the_number_of_second_hop_steps_is_bounded() -> None:
    """Every lead spends an iteration a planned source would have had."""
    from app.services.investigation_queriers import MAX_SEARCH_LEADS, _pages_worth_reading

    answer = _search_answer(*[(f"https://p{i}.example/", f"P{i}") for i in range(20)])
    assert len(_pages_worth_reading(answer)) == MAX_SEARCH_LEADS


def test_a_url_containing_braces_survives_becoming_a_source() -> None:
    """`build_url` runs `str.format` over the template.

    A tracking parameter carrying `{` would raise there, or worse be swallowed
    as a field name — turning a real citation into a step that fetches a
    mangled URL.
    """
    from app.services.investigation_queriers import _pages_worth_reading

    lead = _pages_worth_reading(_search_answer(("https://x.example/?a={b}", "X")))[0]
    assert lead.build_url("APT29") == "https://x.example/?a={b}"


def test_an_answer_with_no_citations_produces_no_leads() -> None:
    from app.services.investigation_queriers import _pages_worth_reading

    assert _pages_worth_reading(_search_answer()) == ()


def test_the_leads_are_distinct_sources_so_the_loop_can_dedupe_them() -> None:
    """The orchestrator de-duplicates on `identity()`; leads that collapsed
    into one would silently drop pages."""
    from app.services.investigation_queriers import _pages_worth_reading

    leads = _pages_worth_reading(
        _search_answer(("https://a.example/", "A"), ("https://b.example/", "B"))
    )
    assert len({lead.identity() for lead in leads}) == 2

# ── A source that never answers ─────────────────────────────────────────────
#
# The iteration budget counts ITERATIONS, not seconds, so nothing else in the
# loop bounds a source that simply hangs. Before `SOURCE_CALL_TIMEOUT` these
# two awaits were unbounded and the `except TimeoutError` arms beside them were
# dead code — nothing on the connect/execute path could raise it.
#
# For MCP this is worse than a stuck coroutine: connecting to a stdio transport
# spawns a subprocess, and a run wedged on the connect leaves it parented to
# the worker.


class _HangingManager(_FakeManager):
    """Never returns from whichever call is asked to hang."""

    def __init__(self, *, hang_on: str, **kw):
        super().__init__(**kw)
        self._hang_on = hang_on

    async def _forever(self):
        await asyncio.Event().wait()  # pragma: no cover — cancelled by wait_for

    async def connect_server(self, runtime):
        if self._hang_on == "connect":
            await self._forever()
        return await super().connect_server(runtime)

    async def execute_tool(self, call):
        if self._hang_on == "execute":
            await self._forever()
        return await super().execute_tool(call)


@pytest.mark.parametrize("hang_on", ["connect", "execute"])
@pytest.mark.asyncio
async def test_an_mcp_server_that_never_answers_times_out(
    monkeypatch, hang_on
) -> None:
    import app.services.investigation_queriers as mod

    monkeypatch.setattr(mod, "SOURCE_CALL_TIMEOUT", 0.05)
    manager = _HangingManager(
        hang_on=hang_on,
        result=SimpleNamespace(content="never", is_error=False),
    )
    querier, srv = _install_mcp(monkeypatch, manager)

    outcome = await asyncio.wait_for(
        querier(_mcp_source(str(srv.id)), "CVE-2024-3400", "q"), timeout=5
    )

    assert outcome.error_type == "timeout"
    assert outcome.detail_code == "source.timed_out"
    assert manager.disconnected is True, (
        "the connection must be closed even when the call was abandoned — "
        "a stdio transport leaves a subprocess behind otherwise"
    )


@pytest.mark.asyncio
async def test_an_embedding_provider_that_never_answers_times_out(
    monkeypatch,
) -> None:
    import app.services.investigation_queriers as mod

    monkeypatch.setattr(mod, "SOURCE_CALL_TIMEOUT", 0.05)

    async def _hang(_text, model=None):
        await asyncio.Event().wait()  # pragma: no cover

    async def _provider(_db):
        return SimpleNamespace(embed=_hang), "m", None

    monkeypatch.setattr(
        "app.llm.router.get_embedding_provider", _provider, raising=True
    )
    querier = mod.make_knowledge_base_querier(SimpleNamespace())

    outcome = await asyncio.wait_for(
        querier(_kb_source(), "CVE-2024-3400", "q"), timeout=5
    )
    assert outcome.error_type == "timeout"
    assert outcome.detail_code == "source.timed_out"


@pytest.mark.asyncio
async def test_a_knowledge_base_search_that_never_returns_times_out(
    monkeypatch,
) -> None:
    import app.services.investigation_queriers as mod

    monkeypatch.setattr(mod, "SOURCE_CALL_TIMEOUT", 0.05)

    async def _hang(*_a, **_k):
        await asyncio.Event().wait()  # pragma: no cover

    monkeypatch.setattr(
        "app.services.hybrid_search.hybrid_search_embeddings", _hang, raising=True
    )

    async def _provider(_db):
        async def _embed(_t, model=None):
            return [0.0] * 8

        return SimpleNamespace(embed=_embed), "m", None

    monkeypatch.setattr(
        "app.llm.router.get_embedding_provider", _provider, raising=True
    )
    querier = mod.make_knowledge_base_querier(SimpleNamespace())

    outcome = await asyncio.wait_for(
        querier(_kb_source(), "CVE-2024-3400", "q"), timeout=5
    )
    assert outcome.error_type == "timeout"


# ── Third-party text never reaches the step verbatim ────────────────────────

_SECRET = "Bearer sk-live-AbCdEf0123456789ZyXwVu"


@pytest.mark.asyncio
async def test_a_tool_error_echoing_a_credential_is_redacted(monkeypatch) -> None:
    """`result.content` is an arbitrary string from a third-party server, and
    error strings from HTTP-backed MCP tools routinely echo the failing
    request. It lands in `status_detail`, which is persisted on the step and
    rendered in the block — so truncation is no defence: a bearer token is well
    inside the cap."""
    manager = _FakeManager(
        result=SimpleNamespace(
            content=f'401 Unauthorized: {{"Authorization": "{_SECRET}"}}',
            is_error=True,
        )
    )
    querier, srv = _install_mcp(monkeypatch, manager)

    outcome = await querier(_mcp_source(str(srv.id)), "CVE-2024-3400", "q")

    assert outcome.error_type == "invalid_response"
    assert "sk-live-AbCdEf0123456789ZyXwVu" not in (outcome.detail or "")
    assert "redacted" in (outcome.detail or "")
    # ...and it is still a useful diagnostic, not a blank.
    assert "401" in (outcome.detail or "")


@pytest.mark.asyncio
async def test_an_exception_echoing_a_credential_is_redacted(monkeypatch) -> None:
    """The same exposure by the other route. The exception path was NOT already
    sanitised — `format_exception_message` returns `str(exc)` unchanged; its
    job is to make an EMPTY exception message useful, not to redact one. Both
    paths go through the redaction now."""
    manager = _FakeManager(
        tools=("lookup_cve",),
        raises=RuntimeError(f'upstream rejected: {{"Authorization": "{_SECRET}"}}'),
    )
    querier, srv = _install_mcp(monkeypatch, manager)

    outcome = await querier(_mcp_source(str(srv.id)), "CVE-2024-3400", "q")

    assert outcome.error_type == "invalid_response"
    assert "sk-live-AbCdEf0123456789ZyXwVu" not in (outcome.detail or "")


# ── The corpus read is re-filtered at query time ────────────────────────────


class _VisibilityDB(_FakeDocsDB):
    """Answers, in order: the caller's groups, the knowledge bases they may
    see among the source's, then the document-name resolve."""

    def __init__(self, visible: list[str], names: dict[uuid.UUID, str]):
        super().__init__(names)
        self._visible = visible

    async def execute(self, stmt):
        self.queries += 1
        if self.queries == 1:
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))
        if self.queries == 2:
            return SimpleNamespace(scalars=lambda: iter(self._visible))
        return await super().execute(stmt)


@pytest.mark.asyncio
async def test_the_search_covers_the_corpora_the_caller_may_see(monkeypatch) -> None:
    """Authorization already happens when the pack is selected; the query
    re-applies the knowledge bases' own visibility (own, shared, company),
    never the embeddings' owner, which would hide every corpus shared with
    the caller."""
    captured: dict = {}
    doc = uuid.uuid4()
    kept, unshared = str(uuid.uuid4()), str(uuid.uuid4())
    querier, _ = _install_kb(
        monkeypatch, hits=[_hit(doc)], names={doc: "d.pdf"},
        user_id=str(uuid.uuid4()), captured=captured,
    )
    _route_sessions_to(monkeypatch, _VisibilityDB([kept], {doc: "d.pdf"}))

    outcome = await querier(_kb_source((kept, unshared)), "CVE-2024-3400", "q")

    assert outcome.findings_count == 1
    assert captured["knowledge_base_ids"] == [kept]
    assert captured["user_id"] is None


@pytest.mark.asyncio
async def test_a_corpus_the_caller_can_no_longer_see_is_not_searched(monkeypatch) -> None:
    captured: dict = {}
    querier, _ = _install_kb(
        monkeypatch, hits=[], names={}, user_id=str(uuid.uuid4()), captured=captured,
    )
    _route_sessions_to(monkeypatch, _VisibilityDB([], {}))

    outcome = await querier(_kb_source(), "CVE-2024-3400", "q")

    assert outcome.error_type == "not_found"
    assert outcome.detail_code == "source.kb_unavailable"
    assert "knowledge_base_ids" not in captured, "nothing may be searched"


# ── Placing the subject, when the schema does not say where ─────────────────


@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        ({"properties": {"q": {"type": "string"}}}, "q"),
        # Two candidates: ambiguous, so it refuses rather than picking one.
        (
            {"properties": {"a": {"type": "string"}, "b": {"type": "string"}}},
            None,
        ),
        # A non-text property does not compete, so one text field still wins.
        (
            {"properties": {"q": {"type": "string"}, "n": {"type": "integer"}}},
            "q",
        ),
        ({"properties": {}}, None),
        (None, None),
        # An absent `type` is treated as text. True as JSON Schema, and a
        # deliberate decision rather than an accident: a hand-rolled server
        # that declares a property with no type is common, and refusing every
        # one of them would make the feature unusable against exactly the
        # servers it is aimed at. Pinned so a change to it is a decision too.
        ({"properties": {"anything": {}}}, "anything"),
        # ...but an untyped property does NOT rescue an ambiguous schema.
        ({"properties": {"a": {}, "b": {"type": "string"}}}, None),
    ],
)
def test_where_the_subject_goes_is_read_or_refused(schema, expected) -> None:
    import app.services.investigation_queriers as mod

    assert mod._subject_argument(schema) == expected


# ── A SUCCESSFUL answer is third-party text too ─────────────────────────────
#
# The error path was redacted; the success path was not. The argument for
# redacting does not depend on `is_error`: an HTTP-backed tool that echoes its
# request on a 2xx puts an Authorization header into the quote.
#
# And a secret in `quote` travels much further than one in `status_detail`.
# That sits in a column. This is persisted to `investigations.report_sources`,
# interpolated verbatim into the synthesis prompt sent to a third-party model
# provider, and rendered into the chat transcript.


@pytest.mark.asyncio
async def test_a_successful_tool_result_is_redacted_before_it_is_quoted(
    monkeypatch,
) -> None:
    manager = _FakeManager(
        result=SimpleNamespace(
            content=(
                "Lookup succeeded. Request was: "
                f'{{"Authorization": "{_SECRET}"}} — 3 records found.'
            ),
            is_error=False,
        )
    )
    querier, srv = _install_mcp(monkeypatch, manager)

    outcome = await querier(_mcp_source(str(srv.id)), "CVE-2024-3400", "q")

    assert outcome.error_type is None, "this is the SUCCESS path"
    quote = outcome.evidence[0].quote or ""
    assert "sk-live-AbCdEf0123456789ZyXwVu" not in quote
    assert "3 records found" in quote, "the passage must still be citable"


@pytest.mark.asyncio
async def test_a_corpus_passage_is_redacted_before_it_is_quoted(monkeypatch) -> None:
    """Lower risk than an arbitrary MCP server, and not zero: ingested
    documents routinely carry pasted credentials."""
    doc = uuid.uuid4()
    hit = SimpleNamespace(
        id=str(uuid.uuid4()),
        content=f'Runbook step 4: export TOKEN="{_SECRET}" then retry.',
        source_id=str(doc),
    )
    querier, _ = _install_kb(monkeypatch, hits=[hit], names={doc: "runbook.pdf"})

    outcome = await querier(_kb_source(), "CVE-2024-3400", "q")

    quote = outcome.evidence[0].quote or ""
    assert "sk-live-AbCdEf0123456789ZyXwVu" not in quote
    assert "Runbook step 4" in quote


# ── A page title is text the page chose ─────────────────────────────────────


def test_a_lead_title_cannot_plant_a_citation_marker() -> None:
    """`render_sources_queried` prints the name as `- **{name}** — …` with no
    escaping, so a title carrying `[7]` puts a fake marker in the one section
    this feature designs to be incapable of lying — and `reconcile_citations`
    validates it if the number is in range."""
    from app.services.investigation_queriers import _lead_name

    cleaned = _lead_name("Threat report [7] — **verified**")
    for ch in "[]*":
        assert ch not in cleaned
    assert "Threat report" in cleaned, "the label must still be readable"


def test_a_lead_title_cannot_break_the_line_it_is_printed_on() -> None:
    from app.services.investigation_queriers import _lead_name

    assert "\n" not in _lead_name("First line\nSecond line")
    assert "`" not in _lead_name("A `code` title")
    assert _lead_name("   ") == ""


def test_a_lead_whose_url_cannot_build_is_skipped() -> None:
    """The `except ValueError` around the constructor could never fire —
    `SourceDefinition` validates nothing there — so the documented "skipped
    rather than failing the step" described a guard that did not exist. The
    probe makes it real."""
    from types import SimpleNamespace

    from app.services.investigation_queriers import _pages_worth_reading

    answer = SimpleNamespace(
        citations=(
            SimpleNamespace(url="https://ok.example/{a}", title="Fine"),
            SimpleNamespace(url="https://ok.example/plain", title="Also fine"),
        )
    )
    leads = _pages_worth_reading(answer)
    # Both survive: the brace doubling is what makes the first one buildable.
    assert len(leads) == 2
    for lead in leads:
        assert lead.build_url("probe")

def test_a_truncated_passage_ends_at_a_boundary() -> None:
    """A quote ending "the campaign began in Febr" is offered to the model as
    a complete excerpt, and the model cannot tell it was cut — so it can build
    a claim on half a fact and cite it correctly."""
    from app.services.investigation_queriers import MAX_PASSAGE_CHARS, _passage

    out = _passage("The campaign began in February and ran for six weeks. " * 30)
    assert len(out) <= MAX_PASSAGE_CHARS
    assert out.endswith("."), f"cut mid-token: ...{out[-30:]!r}"


def test_a_passage_within_the_cap_is_untouched() -> None:
    from app.services.investigation_queriers import _passage

    assert _passage("A short passage.") == "A short passage."


def test_a_passage_with_no_boundary_still_fits() -> None:
    """A wall of characters has nowhere to cut back to; the cap still holds."""
    from app.services.investigation_queriers import MAX_PASSAGE_CHARS, _passage

    assert len(_passage("x" * (MAX_PASSAGE_CHARS + 50))) == MAX_PASSAGE_CHARS
