"""Tradecraft memory: the durable, target-free class that survives a run purge.

The whole feature rests on one bargain: tradecraft may outlive the engagement that
produced it ONLY because it can never carry that engagement's targets. These tests pin
both halves - the guard that enforces target-freedom, and the purge exemption that makes
the class durable - plus the bounds that keep a cross-run pool from inflating the system
prompt forever.
"""

from __future__ import annotations

import inspect

import pytest
from fastapi import HTTPException

from app.services import persistent_memory as pm

# ── The target-free guard ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "content",
    [
        "Host 10.0.0.5 is vulnerable to CVE-2021-26855",
        "scanme.internal.example is reachable on the flat network",
        "Proof retrieved from https://target.example/admin",
        "Operator contact is soc-lead@example.org",
        "Simulation 2cffad3a-0001-4078-b0e2-ef74274022c3 already holds the seed step",
        "The C2 listener answered on fe80::1c2d:3e4f:5a6b:7c8d",
    ],
)
def test_target_bearing_content_is_refused(content):
    """Anything naming a target is refused - this is the safety boundary.

    A leaked host or entity id would be recalled by every later engagement, which is
    precisely the "resumes a dead attack path and harvests phantom verdicts" failure the
    run purge exists to prevent.
    """
    assert pm.tradecraft_leak_reason(content) is not None


@pytest.mark.parametrize(
    "content",
    [
        "honey.scanme.sh answered the probe",
        "the box at internal.corp.py is unpatched",
        "notes.example.md hosts the wiki",
        "payload staged on files.example.zip",
    ],
)
def test_hostnames_ending_in_a_file_extension_are_still_refused(content):
    """A file-extension escape hatch would wave real hosts straight through.

    ``.sh``, ``.py``, ``.md`` and ``.zip`` are all live TLDs, so "it looks like a script"
    is not evidence that a dotted name is not a host - ``honey.scanme.sh`` is the
    example used in the run-purge docstring itself.
    """
    assert pm.tradecraft_leak_reason(content) == "a hostname or domain"


@pytest.mark.parametrize(
    "content",
    [
        "The loopback listener came up on ::1",
        "Callback observed from fe80::1",
        "Beacon reached 2001:db8::1 during the test",
    ],
)
def test_compressed_ipv6_literals_are_refused(content):
    """Compressed forms are the common way to write IPv6 and are fully routable.

    A regex covering only the fully-expanded form would let every one of these through.
    """
    assert pm.tradecraft_leak_reason(content) == "an IPv6 address"


@pytest.mark.parametrize(
    "content",
    [
        "The arsenal has no Linux persistence action for this technique; record a GAP.",
        "A parser emitting 'text' never satisfies a downstream filter on 'username'.",
        "Use STDOUT, never STD_OUT - the near-miss enum token is rejected by the API.",
        "Update an existing arsenal action in place instead of creating a v2 copy.",
        "The daily sync window starts at 12:30 and contends with long scans.",
    ],
)
def test_generalizable_lessons_are_accepted(content):
    """A lesson phrased as a general rule must pass.

    The clock time is deliberate: parsing candidates with ``ipaddress`` instead of
    pattern-matching them is what keeps "12:30" from reading as an address.
    """
    assert pm.tradecraft_leak_reason(content) is None


def test_file_names_are_refused_as_the_accepted_cost_of_no_escape_hatch():
    """Documents the deliberate false positive so nobody "fixes" it with an allow-list.

    Refusing "config.yaml" is the price of refusing "honey.scanme.sh"; a lesson can say
    "the config file" instead, whereas a leaked host is permanent.
    """
    assert pm.tradecraft_leak_reason("Values live in config.yaml") == "a hostname or domain"


def test_refusal_message_tells_the_agent_how_to_rephrase():
    """A bare "no" would make the model retry the same write or drop the lesson."""
    message = pm.tradecraft_refusal_message("a hostname or domain")

    assert "a hostname or domain" in message
    assert "generalizable" in message.lower()


def test_rejection_exception_carries_the_actionable_message():
    error = pm.TradecraftRejectedError("an IP address")

    assert error.reason == "an IP address"
    assert "an IP address" in str(error)


def test_skip_fork_production_memory_is_refused():
    """The row that taught 'prefer NetExec, avoid a Payload Creator fork'.

    That lesson is injected into every later run, so the orchestrator keeps
    skipping interesting payload-based catalog actions. Refuse it.
    """
    content = (
        "Atomic Red Team contracts for host-local credential access return "
        "Produced findings: none and cannot feed into findings-based chained "
        "scenarios. Prefer the equivalent NetExec SMB/LDAP modules, avoiding "
        "an unnecessary Payload Creator fork."
    )
    assert pm.tradecraft_skip_fork_reason(content) == "a skip-fork lesson"
    message = pm.tradecraft_skip_fork_message()
    assert "fork candidates" in message.lower()
    error = pm.TradecraftRejectedError("a skip-fork lesson", message=message)
    assert error.reason == "a skip-fork lesson"
    assert "fork candidates" in str(error).lower()


def test_skip_fork_correct_fork_lesson_is_accepted():
    """A lesson that names the fork must still pass the skip-fork detector."""
    content = (
        "When a payload-based catalog action produces none, it cannot feed a "
        "findings-based chained scenario until you fork it and add REGEX "
        "parsers on the copy. A native injector that already emits findings "
        "is reuse of that contract, not a substitute."
    )
    assert pm.tradecraft_skip_fork_reason(content) is None
    assert pm.tradecraft_leak_reason(content) is None


def test_skip_fork_prefer_forking_lesson_is_accepted():
    """'prefer forking ...; avoid updating the original' must NOT be refused.

    It contains prefer + avoid + fork, but 'avoid' governs the update, not the
    fork - forking is exactly what the lesson recommends, so the detector must
    key on an avoid/skip verb governing 'fork', not on the three tokens
    appearing anywhere.
    """
    content = (
        "Prefer forking payload-based dead-ends that produce none - duplicate "
        "them and add REGEX parsers on the copy; avoid updating the shared "
        "original in place, which poisons the library."
    )
    assert pm.tradecraft_skip_fork_reason(content) is None
    assert pm.tradecraft_leak_reason(content) is None


def test_skip_fork_is_enforced_in_persist_memory_before_insert():
    """Pinned at the write funnel so no path can store the skip-fork lesson."""
    src = inspect.getsource(pm.persist_memory)
    after_lock = src[src.index("_lock_tradecraft_pool(db, agent_id)") :]
    before_lock = src[: src.index("_lock_tradecraft_pool(db, agent_id)")]

    assert "tradecraft_skip_fork_reason(content)" in before_lock
    assert "tradecraft_skip_fork_reason(content)" in after_lock
    assert after_lock.index("tradecraft_skip_fork_reason(content)") < after_lock.index(
        "db.add(entry)"
    )


# ── Enforcement lives in persist_memory, not in the tool layer ───────────────


def test_run_context_is_required_at_the_write_funnel_not_just_the_tool():
    """The chat gate only covers the caller it is built for.

    ``persist_memory`` is the single write funnel, so checking authorization only
    where the chat tool is assembled would leave every other caller - an assignment
    path today, a new tool tomorrow - able to publish an unscoped cross-user row
    from outside any engagement.
    """
    src = inspect.getsource(pm.persist_memory)

    assert "in_tradecraft_run_context()" in src
    assert "TradecraftNotAuthorizedError" in src
    # Authorization before the content is validated or persisted.
    assert src.index("in_tradecraft_run_context()") < src.index(
        "tradecraft_leak_reason("
    )


def test_unauthorized_refusal_is_actionable():
    """A bare denial would just make the agent retry the same write."""
    error = pm.TradecraftNotAuthorizedError()

    assert "authorized run" in str(error)
    assert "'learned'" in str(error)


@pytest.mark.asyncio
async def test_tradecraft_write_outside_a_run_is_refused_by_the_funnel():
    """Proven through the tool, with the capability flag deliberately granted.

    ``allow_tradecraft=True`` isolates the funnel check: even a caller the tool
    layer would let through is refused when no engagement is in scope.
    """
    from app.services.builtin_tools import build_memory_store_tool
    from app.tools.base import ToolCall

    assert not pm.in_tradecraft_run_context()

    _defs, executor = build_memory_store_tool(
        "agent-1", "user-1", source="chat", allow_tradecraft=True
    )
    result = await executor.execute_tool(
        ToolCall(
            id="call-1",
            name="store_memory",
            arguments={
                "content": "A generalizable lesson",
                "memory_type": pm.TRADECRAFT_MEMORY_TYPE,
            },
        )
    )

    assert "authorized run" in result.content


def test_guard_and_agent_level_scope_are_enforced_in_persist_memory():
    """Pinned at the source so no future write path can bypass either rule.

    ``persist_memory`` is the single funnel every caller goes through. If the guard were
    enforced in the ``store_memory`` tool instead, a second caller (an assignment path, a
    future tool) could write a leaky or user-scoped tradecraft row and the purge would
    then preserve it forever.
    """
    src = inspect.getsource(pm.persist_memory)

    assert "TRADECRAFT_MEMORY_TYPE" in src
    assert "tradecraft_leak_reason" in src
    assert "TradecraftRejectedError" in src
    # Tradecraft is knowledge about the platform, never about the operator whose run
    # happened to learn it, so the user scope is dropped on write.
    assert "user_id = None" in src


def test_tradecraft_is_deduplicated_like_facts():
    """Without dedup the same lesson accretes once per run until it fills the cap."""
    src = inspect.getsource(pm.persist_memory)

    assert 'memory_type in ("fact", TRADECRAFT_MEMORY_TYPE)' in src


def test_dedup_never_compares_across_memory_classes():
    """A tradecraft lesson must only be deduplicated against other tradecraft."""
    signature = inspect.signature(pm._is_duplicate_fact)

    assert signature.parameters["memory_type"].default == "fact"
    assert "memory_type == memory_type" in inspect.getsource(pm._is_duplicate_fact)


# ── Bounds: durable must not mean unbounded ──────────────────────────────────


def test_tradecraft_is_exempt_from_age_pruning():
    """A lesson learned six months ago is still true.

    ``prune_old_memories`` only ages out ``fact`` / ``learned`` / ``state``;
    this pins that tradecraft was not quietly added to that list.
    """
    src = inspect.getsource(pm.prune_old_memories)

    assert '["fact", "learned", STATE_MEMORY_TYPE]' in src
    assert "TRADECRAFT_MEMORY_TYPE" not in src
    assert pm.TRADECRAFT_MEMORY_TYPE not in src


def test_tradecraft_is_capped_at_write_time():
    """Age-pruning exempt means nothing else would ever bound the pool."""
    assert pm._MAX_TRADECRAFT_PER_AGENT > 0

    persist_src = inspect.getsource(pm.persist_memory)
    assert "_enforce_tradecraft_cap" in persist_src

    cap_src = inspect.getsource(pm._enforce_tradecraft_cap)
    # Oldest-first eviction: keep the newest N, delete the rest.
    assert "created_at.desc()" in cap_src
    assert "_MAX_TRADECRAFT_PER_AGENT" in cap_src


# ── Durability: the purge exemption ──────────────────────────────────────────


def test_clear_agent_memories_wipes_everything_by_default():
    """Agent deletion and the operator's "clear all memory" must stay total.

    The preserve hook is opt-in for the engagement purge only; defaulting it on would
    make a deleted agent leave rows behind.
    """
    signature = inspect.signature(pm.clear_agent_memories)

    assert signature.parameters["preserve_memory_types"].default is None


def test_clear_agent_memories_filters_only_when_asked():
    src = inspect.getsource(pm.clear_agent_memories)

    assert "if preserve_memory_types:" in src
    assert "notin_" in src


# ── Who may write it: agent-level tradecraft is a cross-user publish ─────────


def test_only_designated_builtin_agents_may_write_tradecraft():
    """A tradecraft row is unscoped, so it lands in every other user's prompt.

    Offering the class on every memory-enabled agent would let any user of a shared or
    company-managed agent ask it to publish arbitrary text to all the others. The
    target-pattern guard rejects hosts and ids but is not an authorization boundary.
    """
    from app.services.builtin_tools import is_tradecraft_writer_agent

    assert is_tradecraft_writer_agent(
        "openaev-attack-path-orchestrator", is_builtin=True
    )
    # A user-created agent cannot claim the slug: the builtin flag is required.
    assert not is_tradecraft_writer_agent(
        "openaev-attack-path-orchestrator", is_builtin=False
    )
    assert not is_tradecraft_writer_agent("openaev-payload-creator", is_builtin=True)
    assert not is_tradecraft_writer_agent("some-user-agent", is_builtin=True)
    assert not is_tradecraft_writer_agent(None, is_builtin=True)


@pytest.mark.asyncio
async def test_tradecraft_write_is_refused_for_an_undesignated_agent():
    """Refused, not silently downgraded to a user-scoped write.

    Goes through the executor so the refusal is proven on the path the model
    actually takes, and reaches it before any DB session is opened.
    """
    from app.services.builtin_tools import build_memory_store_tool
    from app.tools.base import ToolCall

    _defs, executor = build_memory_store_tool("agent-1", "user-1", source="chat")
    result = await executor.execute_tool(
        ToolCall(
            id="call-1",
            name="store_memory",
            arguments={
                "content": "A general lesson",
                "memory_type": pm.TRADECRAFT_MEMORY_TYPE,
            },
        )
    )

    assert "cannot write tradecraft" in result.content


def test_tradecraft_is_advertised_only_where_it_can_be_used():
    """Naming the class to an agent that may not use it just invites a refusal."""
    from app.services.builtin_tools import build_memory_store_tool

    ungated, _ = build_memory_store_tool("agent-1", "user-1")
    gated, _ = build_memory_store_tool("agent-1", "user-1", allow_tradecraft=True)

    ungated_desc = ungated[0].input_schema["properties"]["memory_type"]["description"]
    gated_desc = gated[0].input_schema["properties"]["memory_type"]["description"]

    assert "tradecraft" not in ungated_desc
    assert "tradecraft" in gated_desc


def test_chat_requires_both_agent_identity_and_a_trusted_run_context():
    """Identity alone is not enough: the agent is reachable from other chat paths.

    Pinned at the call site because a caller-passed flag is one forgotten argument
    away from the leak.
    """
    from app.services import agent_chat

    src = inspect.getsource(agent_chat)

    assert "is_tradecraft_writer_agent(" in src
    assert "in_tradecraft_run_context()" in src
    assert "allow_tradecraft=_allow_tradecraft" in src


# ── The class name is model-supplied, so it is normalised before every check ─


class _NoDb:
    """Session stub that fails loudly - a refusal must precede any DB work."""

    async def execute(self, *args, **kwargs):
        raise AssertionError("the guard must refuse before touching the database")

    def add(self, obj):
        raise AssertionError("the guard must refuse before touching the database")

    async def flush(self):
        raise AssertionError("the guard must refuse before touching the database")


@pytest.mark.parametrize("spelling", ["Tradecraft", "TRADECRAFT", " tradecraft "])
def test_memory_type_variants_canonicalise_to_the_tradecraft_class(spelling):
    """Every gate on the class is an exact string comparison.

    ``store_memory`` takes ``memory_type`` straight from the model, so a variant that
    is not canonicalised opts out of all of them at once: the ``allow_tradecraft``
    authorization gate, the target guard, the forced agent-level scope, dedup, the
    write-time cap, and the purge's ``preserve_memory_types`` list.
    """
    assert pm.normalize_memory_type(spelling) == pm.TRADECRAFT_MEMORY_TYPE


@pytest.mark.asyncio
@pytest.mark.parametrize("spelling", ["Tradecraft", " tradecraft "])
async def test_case_variant_tradecraft_is_refused_for_an_undesignated_agent(spelling):
    """Capitalising the argument must not defeat the authorization gate.

    On the raw comparison this write skipped the gate, then skipped the target guard
    in the store, and landed as a class of its own that nothing prunes or caps.
    """
    from app.services.builtin_tools import build_memory_store_tool
    from app.tools.base import ToolCall

    _defs, executor = build_memory_store_tool("agent-1", "user-1", source="chat")
    result = await executor.execute_tool(
        ToolCall(
            id="call-case-variant",
            name="store_memory",
            arguments={"content": "A general lesson", "memory_type": spelling},
        )
    )

    assert "cannot write tradecraft" in result.content


@pytest.mark.asyncio
async def test_case_variant_tradecraft_still_hits_the_target_guard():
    """And inside a run the target guard still applies to the variant.

    The store is the single funnel, so it normalises for itself rather than trusting
    the tool to have done it. A run context is established because the funnel checks
    authorization before content - without one the variant is refused for not being
    in a run, which would prove nothing about normalisation reaching the guard.
    """
    token = pm.set_tradecraft_run_context([])
    try:
        with pytest.raises(pm.TradecraftRejectedError):
            await pm.persist_memory(
                _NoDb(),
                agent_id="2cffad3a-0001-4078-b0e2-ef74274022c3",
                content="Host 10.0.0.5 is PROVEN vulnerable",
                memory_type="Tradecraft",
            )
    finally:
        pm.reset_tradecraft_run_context(token)


@pytest.mark.asyncio
async def test_case_variant_tradecraft_is_authorized_like_the_canonical_spelling():
    """Normalisation must reach the authorization gate too, not only the guard.

    A capitalised spelling that skipped the gate would be an unscoped cross-user
    write from outside any engagement.
    """
    assert not pm.in_tradecraft_run_context()

    with pytest.raises(pm.TradecraftNotAuthorizedError):
        await pm.persist_memory(
            _NoDb(),
            agent_id="2cffad3a-0001-4078-b0e2-ef74274022c3",
            content="A generalizable lesson",
            memory_type="Tradecraft",
        )


def test_cap_eviction_order_is_deterministic():
    """Two lessons written in one transaction share a ``created_at``.

    Without a tiebreaker the kept set is whatever the planner happens to return, so
    the same write can evict a different lesson on a retry.
    """
    cap_src = inspect.getsource(pm._enforce_tradecraft_cap)

    assert "AgentMemoryEntry.id.desc()" in cap_src


class _StubResult:
    """Minimal ``Result`` stand-in for the resolver's single query."""

    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _CapturingSession:
    """Records the statements ``_resolve_platform_agent`` compiles."""

    def __init__(self, agent):
        self._agent = agent
        self.statements: list = []

    async def execute(self, statement, *_args, **_kwargs):
        self.statements.append(statement)
        return _StubResult(self._agent)


class _FakeAgent:
    """Minimal agent row: the resolver only reads ``.slug`` off the result."""

    def __init__(self, slug):
        self.slug = slug


def _filters_on_disable_chat(statement) -> bool:
    """True when the statement's WHERE clause carries the chat filter.

    Read off the WHERE clause specifically: ``select(Agent)`` lists every column
    including ``disable_chat``, so searching the whole statement would pass no
    matter what the resolver filters on.
    """
    return "disable_chat IS NOT true" in str(statement.whereclause)


async def test_a_named_generation_agent_resolves_even_when_disable_chat():
    """``disable_chat`` means "hidden from the picker", not "never by slug".

    The OpenAEV generation agents (phishing landing page / email, ...) are
    ``disable_chat`` precisely so a human does not pick them by hand, yet OpenAEV
    invokes them by slug on every "Generate with AI". The slug branch must return
    the named agent as-is; filtering ``disable_chat`` here is what answered a
    phishing-page request with an unrelated company-managed chat agent.
    """
    from app.api.v1 import platform

    agent = _FakeAgent("aev-phishing-landing-page-generator")
    session = _CapturingSession(agent=agent)

    resolved = await platform._resolve_platform_agent(
        session, "aev-phishing-landing-page-generator"
    )

    assert resolved is agent
    # Exactly one query - the slug lookup - and it does NOT exclude disable_chat.
    assert len(session.statements) == 1
    assert not _filters_on_disable_chat(session.statements[0])


async def test_a_named_slug_that_misses_is_a_404_never_a_substitution():
    """An explicit slug is a precise request; a miss must fail loudly.

    Falling through to "the first company-managed chat agent" is the incident: a
    phishing-page request came back answered by the OpenCTI Dashboard Creator on
    one instance and the XTM One Assistant on another (whatever agent happened to
    sort first). The company-managed fallback query must never run for a slug.
    """
    from app.api.v1 import platform

    session = _CapturingSession(agent=None)
    with pytest.raises(HTTPException):
        await platform._resolve_platform_agent(
            session, "aev-phishing-landing-page-generator"
        )

    assert len(session.statements) == 1


async def test_the_run_only_orchestrator_is_barred_from_the_chat_resolver():
    """The one ``disable_chat`` agent chat must never drive, even named explicitly.

    It has a run-only contract (OpenAEV write surface + tradecraft writer)
    and is resolved for runs through the dedicated ``_resolve_orchestrator_agent``.
    Naming it on the user-facing chat endpoint is a 404, not a chat session.
    """
    from app.api.v1 import platform
    from app.services.autonomous_pentest import DEFAULT_ORCHESTRATOR_SLUG

    session = _CapturingSession(agent=_FakeAgent(DEFAULT_ORCHESTRATOR_SLUG))
    with pytest.raises(HTTPException):
        await platform._resolve_platform_agent(session, DEFAULT_ORCHESTRATOR_SLUG)


async def test_the_orchestrator_stays_resolvable_for_its_own_run_endpoints():
    """The orchestrator IS `disable_chat`; the run endpoints resolve it on purpose.

    They use a dedicated resolver rather than the chat one, because the chat
    resolver answers the built-in orchestrator's slug with a 404, as it does an
    unknown slug; its fallback to the first company-managed chat agent only runs
    when no agent is named. The dedicated resolver is an exact match or a 404,
    and it does not filter on ``disable_chat``.
    """
    from app.api.v1 import platform

    session = _CapturingSession(agent=object())
    await platform._resolve_orchestrator_agent(
        session, "openaev-attack-path-orchestrator"
    )

    assert not _filters_on_disable_chat(session.statements[0])
    # One statement only: an exact match, never a fallback.
    assert len(session.statements) == 1

    # Pinned at the call sites too - both run endpoints must use it.
    src = inspect.getsource(platform)
    assert src.count("_resolve_orchestrator_agent(") >= 3


async def test_a_missing_orchestrator_is_a_404_not_a_different_agent():
    """The chat fallback is exactly what must not happen for a run.

    The purge already treats the 404 as "no memory to clear", and the run start
    failing loudly beats it running under an agent that cannot drive it.
    """
    from app.api.v1 import platform

    session = _CapturingSession(agent=None)
    with pytest.raises(HTTPException):
        await platform._resolve_orchestrator_agent(session, "missing-orchestrator")

    assert len(session.statements) == 1


def test_an_existing_conversation_only_bars_the_run_only_orchestrator():
    """A conversation's own `agent_id` is honoured ahead of the request slug.

    It is set at creation (or repointed by a transfer). Only the run-only
    orchestrator is excluded there, matching the slug branch of
    ``_resolve_platform_agent``; a conversation opened against a ``disable_chat``
    GENERATION agent must keep resolving to it. Excluding every ``disable_chat``
    agent here is what re-broke generation for any session-backed chatbot call.
    """
    from app.api.v1 import platform

    src = inspect.getsource(platform.send_platform_chat_message)

    # The conversation fast-path bars the orchestrator, not all disable_chat.
    assert "is_distinct_from(DEFAULT_ORCHESTRATOR_SLUG)" in src
    assert "_AgentModel.disable_chat.isnot(True)" not in src


async def test_the_no_slug_fallback_still_excludes_disable_chat_agents():
    """With no caller intent the fallback must stay chat-only.

    Nothing about who asked reaches it, so a backend-run-only surface (any
    ``disable_chat`` agent) must never be handed back as the default. The
    ``disable_chat`` exclusion therefore stays on the no-slug fallback branch even
    though the explicit-slug branch no longer applies it.
    """
    from app.api.v1 import platform

    session = _CapturingSession(agent=None)
    with pytest.raises(HTTPException):
        await platform._resolve_platform_agent(session, None)

    assert _filters_on_disable_chat(session.statements[-1])


# ── Unstructured targets: caught from the run's own scope, not by heuristics ──


def test_single_label_hosts_are_refused_when_the_run_declares_them():
    """`dc01` and `DESKTOP-1` are just words; only the run knows they are targets.

    No lexical rule separates them from the domain's own vocabulary, so the run
    supplies its identifiers and those exact strings are refused.
    """
    token = pm.set_tradecraft_run_context(["dc01", "DESKTOP-1"])
    try:
        assert pm.tradecraft_leak_reason("dc01 is vulnerable to the relay") == (
            "a target from the current run's scope"
        )
        # Case-insensitive, and matched on a boundary rather than as a substring.
        assert pm.tradecraft_leak_reason("desktop-1 fell over") is not None
        # A lesson that merely shares a fragment is still fine.
        assert pm.tradecraft_leak_reason("Prefer dc01x-style naming") is None
    finally:
        pm.reset_tradecraft_run_context(token)


def test_an_in_scope_person_is_refused_by_the_same_mechanism():
    """Closes the case no name heuristic could: a person who IS a run target."""
    token = pm.set_tradecraft_run_context(["Alice Smith"])
    try:
        assert pm.tradecraft_leak_reason("Alice Smith fell for the pretext") is not None
    finally:
        pm.reset_tradecraft_run_context(token)


def test_domain_vocabulary_is_never_refused_as_a_target():
    """The reason the second layer is scope-driven rather than a heuristic."""
    token = pm.set_tradecraft_run_context(["dc01"])
    try:
        for lesson in (
            "Emit ipv4, never text, for anything downstream filters on.",
            "Use ps1 for the PowerShell variant and STDOUT for the parser mode.",
            "T1059 execution needs an output parser or nothing chains.",
        ):
            assert pm.tradecraft_leak_reason(lesson) is None
    finally:
        pm.reset_tradecraft_run_context(token)


def test_targets_adopted_mid_cycle_are_added_to_the_guard():
    """The seeded set is a cycle-start snapshot, but the perimeter moves.

    The standard first cycle opens with an empty allow-list and adopts targets only
    after the operator answers the scoping question, so a lesson written later in
    that same cycle would otherwise be free to name the new host.
    """
    token = pm.set_tradecraft_run_context([])
    try:
        assert pm.tradecraft_leak_reason("dc01 refused the relay") is None

        pm.extend_tradecraft_run_context(["dc01"])

        assert pm.tradecraft_leak_reason("dc01 refused the relay") is not None
    finally:
        pm.reset_tradecraft_run_context(token)


def test_extending_never_drops_the_targets_already_known():
    """A later scope read returning a narrower perimeter must not un-guard the rest."""
    token = pm.set_tradecraft_run_context(["dc01"])
    try:
        pm.extend_tradecraft_run_context(["DESKTOP-1"])

        assert pm.tradecraft_leak_reason("dc01 is noisy") is not None
        assert pm.tradecraft_leak_reason("DESKTOP-1 is noisy") is not None
    finally:
        pm.reset_tradecraft_run_context(token)


def test_extending_outside_a_run_never_creates_a_context():
    """Holding a context is what authorizes the write, so extend must not grant one."""
    assert not pm.in_tradecraft_run_context()

    pm.extend_tradecraft_run_context(["dc01"])

    assert not pm.in_tradecraft_run_context()


def test_a_malformed_scope_response_is_not_a_resolved_perimeter():
    """A 200 is not proof the read succeeded.

    `_read_openaev_run_scope` returns whatever JSON object came back, so an
    `isinstance(view, dict)` test would accept `{}` and unlock tradecraft with an
    empty target set - authorized to write, with nothing to match unstructured
    targets against.
    """
    from app.services.autonomous_pentest import scope_view_is_usable

    assert not scope_view_is_usable(None)
    assert not scope_view_is_usable({})
    assert not scope_view_is_usable({"allowlist": "oops"})
    assert not scope_view_is_usable({"allowlist": [], "denylist": {"a": 1}})

    # A present but empty allow-list IS a real answer: the run has no perimeter
    # yet, and the mid-cycle sync fills it in as the agent resolves one.
    assert scope_view_is_usable({"allowlist": []})
    assert scope_view_is_usable({"allowlist": [{"name": "dc01"}], "denylist": []})


def test_a_scope_list_of_unreadable_entries_is_not_a_resolved_perimeter():
    """The container having the right shape is not enough.

    A list of `{}` or `42` passes any container check while harvesting no target,
    which lands in the same authorized-with-an-empty-guard state as a malformed
    container - and one unreadable entry among good ones means the perimeter was
    only partly understood, which is not a perimeter.
    """
    from app.services.autonomous_pentest import scope_view_is_usable

    assert not scope_view_is_usable({"allowlist": [{}]})
    assert not scope_view_is_usable({"allowlist": [42]})
    assert not scope_view_is_usable({"allowlist": [""]})
    assert not scope_view_is_usable({"allowlist": [{"name": "   "}]})
    assert not scope_view_is_usable({"allowlist": [{"name": "dc01"}, {}]})
    assert not scope_view_is_usable({"allowlist": [{"name": "dc01"}], "denylist": [{}]})


def test_an_entity_rule_carrying_only_its_uuid_is_not_a_resolved_perimeter():
    """For an entity rule the `id` is a UUID, which is not the leakable identifier.

    The guard's own pattern already refuses a UUID; the single-label host behind it
    lives only in the resolved name. Accepting the id would declare the perimeter
    readable on the strength of the one identifier that cannot leak, without the
    one that can. A MANUAL / CSV rule is the opposite case: there the id IS the raw
    IP / CIDR / hostname.
    """
    from app.services.autonomous_pentest import scope_view_is_usable

    unresolved = {"type": "ASSETS", "id": "2cffad3a-0001-4078-b0e2-ef74274022c3"}
    assert not scope_view_is_usable({"allowlist": [unresolved]})

    assert scope_view_is_usable({"allowlist": [{"type": "MANUAL", "id": "dc01"}]})
    assert scope_view_is_usable({"allowlist": [{"source": "CSV", "id": "10.0.0.5"}]})
    assert scope_view_is_usable({"allowlist": [{**unresolved, "name": "dc01"}]})


def test_a_scope_view_without_an_allowlist_is_not_a_resolved_perimeter():
    """The allow-list is the authoritative perimeter; its ABSENCE is a malformed read.

    A well-formed OpenAEV scope view always carries an allowlist (the
    AutonomousScopeView DTO initializes it to a list), so a response with only a
    denylist - or no allowlist key at all - is a partial or wrong read, not a
    resolved empty perimeter. Treating it as usable would seed the guard from the
    carve-outs alone (or from nothing) while leaving the in-scope hosts
    unharvested, then authorize the write against that empty guard - the fail-open
    this validator exists to prevent.
    """
    from app.services.autonomous_pentest import scope_view_is_usable

    # allowlist absent entirely -> not a resolved perimeter, whatever the denylist.
    assert not scope_view_is_usable({"denylist": []})
    assert not scope_view_is_usable({"denylist": [{"name": "payroll-srv"}]})

    # A present allowlist is required; the denylist stays optional but must be a
    # list when it is there.
    assert scope_view_is_usable({"allowlist": []})
    assert scope_view_is_usable({"allowlist": [], "denylist": []})
    assert not scope_view_is_usable({"allowlist": [], "denylist": "oops"})


def test_scope_replacement_suspends_writes_until_the_new_scope_is_confirmed():
    """The window around a perimeter change must be closed, not merely resynced.

    Tool calls run concurrently, so a write racing the replacement would be checked
    against the outgoing scope; and if the confirming re-read fails, continuing
    against a knowingly stale set is the fail-open this layer exists to avoid.
    """
    from app.integrations.providers.openaev import autonomous as oaev

    src = inspect.getsource(oaev)

    assert "_suspend_tradecraft()" in src
    assert "_resume_tradecraft()" in src
    # Suspended before the PUT, so a concurrent write cannot slip through the gap.
    assert src.index("_suspend_tradecraft()") < src.index('"set run scope"')


def test_an_ambiguous_scope_put_failure_still_confirms_before_resuming():
    """A PUT error does not prove the perimeter is unchanged.

    ``_api`` reports a timeout or a dropped connection exactly like a rejection,
    so OpenAEV may have committed the replacement before the failure surfaced.
    Resuming on the error path would authorize a write against the OUTGOING target
    set, and a host the run had just taken on could be persisted forever.
    """
    from app.integrations.providers.openaev import autonomous as oaev

    src = inspect.getsource(oaev)
    scope_src = src[src.index("async def set_openaev_run_scope") :]
    scope_src = scope_src[: scope_src.index("executor.register(")]

    # Exactly one re-authorization site, and it is gated on the confirming read.
    assert scope_src.count("_resume_tradecraft()") == 1
    assert scope_src.index('"read run scope"') < scope_src.index("_resume_tradecraft()")
    # The error path falls through to that confirmation instead of returning early.
    assert scope_src.index("_resume_tradecraft()") < scope_src.index("return err")


def test_overlapping_scope_replacements_stay_suspended_until_the_last_confirms():
    """Tool calls run under `asyncio.gather`, so two replacements can overlap.

    A boolean flag let the first one to finish re-authorize writes while the second
    was still rewriting the perimeter - reopening the window the suspension exists
    to close.
    """
    token = pm.set_tradecraft_run_context(["dc01"])
    try:
        pm.suspend_tradecraft_writes()
        pm.suspend_tradecraft_writes()
        assert not pm.in_tradecraft_run_context()

        pm.resume_tradecraft_writes()
        # The second replacement is still in flight.
        assert not pm.in_tradecraft_run_context()

        pm.resume_tradecraft_writes()
        assert pm.in_tradecraft_run_context()
    finally:
        pm.reset_tradecraft_run_context(token)


def test_one_unconfirmed_replacement_keeps_writes_off_for_the_rest_of_the_cycle():
    """Fail-closed has to survive a CONCURRENT replacement that did confirm."""
    token = pm.set_tradecraft_run_context(["dc01"])
    try:
        pm.suspend_tradecraft_writes()  # replacement A
        pm.suspend_tradecraft_writes()  # replacement B
        pm.resume_tradecraft_writes()  # A confirmed; B never will

        assert not pm.in_tradecraft_run_context()
    finally:
        pm.reset_tradecraft_run_context(token)


def test_a_stray_resume_cannot_lift_a_later_suspension():
    """Clamped at zero, or an unpaired resume banks credit against a real one."""
    token = pm.set_tradecraft_run_context(["dc01"])
    try:
        pm.resume_tradecraft_writes()
        pm.resume_tradecraft_writes()
        pm.suspend_tradecraft_writes()

        assert not pm.in_tradecraft_run_context()
    finally:
        pm.reset_tradecraft_run_context(token)


def test_an_in_flight_write_is_revalidated_against_the_current_perimeter():
    """Checking once on entry only covers writes that START after a change.

    `persist_memory` awaits an embedding round-trip between its entry checks and
    the insert, and tool calls are concurrent, so a scope replacement can land in
    that window: the content was cleared against a target set that is no longer
    the run's. Both the authorization and the leak check are repeated after the
    lock, which is the last await before the row is added.
    """
    src = inspect.getsource(pm.persist_memory)
    after_lock = src[src.index("_lock_tradecraft_pool(db, agent_id)") :]

    assert "in_tradecraft_run_context()" in after_lock
    assert "tradecraft_leak_reason(content)" in after_lock
    assert "tradecraft_skip_fork_reason(content)" in after_lock
    # Re-validated BEFORE the row is created, not merely before the commit.
    assert after_lock.index("tradecraft_leak_reason(content)") < after_lock.index(
        "db.add(entry)"
    )
    assert after_lock.index("tradecraft_skip_fork_reason(content)") < after_lock.index(
        "db.add(entry)"
    )


def test_suspend_and_resume_are_visible_across_tasks():
    """Same reason the target set is mutable: children hold a copy of the context."""
    token = pm.set_tradecraft_run_context(["dc01"])
    try:
        assert pm.in_tradecraft_run_context()

        pm.suspend_tradecraft_writes()
        assert not pm.in_tradecraft_run_context()

        pm.resume_tradecraft_writes()
        assert pm.in_tradecraft_run_context()
    finally:
        pm.reset_tradecraft_run_context(token)


def test_suspend_outside_a_run_is_a_no_op():
    assert not pm.in_tradecraft_run_context()

    pm.suspend_tradecraft_writes()
    pm.resume_tradecraft_writes()

    # Resuming must never manufacture the authorization a run context confers.
    assert not pm.in_tradecraft_run_context()


def test_scope_view_harvest_is_shared_with_the_scope_tools():
    """One definition of "what counts as a target" for seeding and for mid-cycle sync."""
    from app.services.autonomous_pentest import scope_view_identifiers

    names = scope_view_identifiers(
        {"allowlist": [{"name": "dc01"}], "denylist": [{"name": "payroll-srv"}]}
    )

    assert names == {"dc01", "payroll-srv"}
    # Tolerates the shapes a failed or malformed read produces.
    assert scope_view_identifiers(None) == set()
    assert scope_view_identifiers({"allowlist": "not-a-list"}) == set()


def test_scope_tools_sync_the_guard_after_reading_and_after_writing():
    from app.integrations.providers.openaev import autonomous as oaev

    src = inspect.getsource(oaev)

    # Reading the perimeter and replacing it are both moments the guard must learn
    # about; the write path re-reads because it only holds ids, not resolved names.
    assert src.count("_sync_tradecraft_guard(") >= 3
    assert "extend_tradecraft_run_context" in src


def test_run_context_is_absent_by_default_and_restored_on_reset():
    """Leaking context across tasks would apply one run's targets to another."""
    assert not pm.in_tradecraft_run_context()

    token = pm.set_tradecraft_run_context(["dc01"])
    assert pm.in_tradecraft_run_context()
    pm.reset_tradecraft_run_context(token)

    assert not pm.in_tradecraft_run_context()


def test_run_context_tolerates_a_malformed_scope_payload():
    """A bad scope read must not abort a cycle; the lexical guard still applies."""
    token = pm.set_tradecraft_run_context(None)
    try:
        assert pm.in_tradecraft_run_context()
        assert pm.tradecraft_leak_reason("A general lesson") is None
        assert pm.tradecraft_leak_reason("Host 10.0.0.5 is down") is not None
    finally:
        pm.reset_tradecraft_run_context(token)


def test_short_declared_targets_are_still_refused():
    """A declared target is a target however short its name.

    These come from the authoritative scope, so matching one is never a heuristic
    false positive - dropping `db` or a person named `Li` below a length floor
    would make exactly those permanently storable.
    """
    token = pm.set_tradecraft_run_context(["db", "Li"])
    try:
        assert pm.tradecraft_leak_reason("Check the db before chaining") is not None
        assert pm.tradecraft_leak_reason("Li clicked the link") is not None
        # Still boundary-matched, so an unrelated word is untouched.
        assert pm.tradecraft_leak_reason("Rebuild the dbschema index") is None
    finally:
        pm.reset_tradecraft_run_context(token)


@pytest.mark.asyncio
async def test_mid_cycle_targets_reach_sibling_tool_tasks():
    """The agent loop runs each tool in its own child task.

    A child inherits a COPY of the context, so a scope tool that REBOUND the
    ContextVar would update only its own copy and the next tool call would still
    be matching the cycle-start snapshot. Binding one set per cycle and mutating
    it in place is what makes the discovery visible across tool calls.
    """
    import asyncio

    token = pm.set_tradecraft_run_context([])
    try:

        async def scope_tool() -> None:
            pm.extend_tradecraft_run_context(["dc01"])

        async def store_memory_tool() -> str | None:
            return pm.tradecraft_leak_reason("dc01 refused the relay")

        # Separate child tasks, exactly as the loop dispatches successive tools.
        await asyncio.create_task(scope_tool())
        leak = await asyncio.create_task(store_memory_tool())

        assert leak == "a target from the current run's scope"
    finally:
        pm.reset_tradecraft_run_context(token)


def test_autonomous_cycle_sets_and_always_clears_the_run_context():
    from app.services import autonomous_pentest as ap

    src = inspect.getsource(ap.run_autonomous_cycle)

    assert "set_tradecraft_run_context(" in src
    assert "reset_tradecraft_run_context(tradecraft_token)" in src
    # Cleared in the finally, so an overrun or cancelled cycle cannot leave a
    # stale target list attached to the worker task.
    assert src.index("finally:") < src.index("reset_tradecraft_run_context(")


def test_scope_harvest_collects_names_from_both_lists_and_tolerates_junk():
    from app.services.autonomous_pentest import _run_target_identifiers

    names = _run_target_identifiers(
        {"scope": [{"name": "snapshot-host"}, "bare-string", 42]},
        {
            "allowlist": [{"name": "dc01"}, {"hostname": "DESKTOP-1"}],
            # The deny-list is harvested too: a carve-out is still a real machine.
            "denylist": [{"label": "payroll-srv"}],
            "unexpected": "ignored",
        },
    )

    assert {"dc01", "DESKTOP-1", "payroll-srv", "snapshot-host", "bare-string"} <= names


def test_an_unresolved_scope_does_not_unlock_tradecraft():
    """No enumerable targets means no tradecraft - not tradecraft with no guard.

    The live scope read is best-effort, and the snapshot it falls back to carries
    `{type, id}` entries whose ids are entity UUIDs, so it holds no names. Seeding
    the context from it during an OpenAEV outage would authorize the write while
    leaving the unstructured-target layer with nothing to match against, which is
    the only combination that can persist a `dc01` forever.

    RC-6(b) adds ONE carve-out and it must not weaken this: a plan_mode
    (scenario-authoring) run unlocks tradecraft too, but it has no live perimeter
    to resolve and enumerates no unstructured host, so the single-label matcher
    going inert there cannot persist a `dc01`. The gate therefore still REQUIRES
    a usable scope on a LIVE run (`plan_mode` is False there, so the OR collapses
    back to `scope_view_is_usable(live_scope)` - an OpenAEV outage still fails
    closed), and the lexical guard refuses addresses/FQDNs/URLs/ids/emails
    regardless of the target set on either path.
    """
    from app.services import autonomous_pentest as ap

    src = inspect.getsource(ap.run_autonomous_cycle)

    # Shape-checked, not merely "the read returned a dict": a malformed 200 would
    # otherwise seed an empty set and authorize the write with nothing to match.
    # The live-run requirement stays; the only relaxation is the plan_mode branch.
    assert "scope_view_is_usable(live_scope) or plan_mode" in src
    assert "if tradecraft_token is not None:" in src


def test_scope_harvest_reads_the_key_the_run_payload_actually_carries():
    """The snapshot lives under `payload['scope']`, not `payload['scope_targets']`.

    It is the only scope left when the live read fails - an explicitly tolerated
    fallback - so harvesting the wrong key would leave the whole unstructured
    layer inert exactly when it is the only one left, while the write stays
    unlocked.  A MANUAL rule carries its raw hostname under `id`.
    """
    from app.services.autonomous_pentest import _run_target_identifiers

    names = _run_target_identifiers(
        {"scope": [{"type": "MANUAL", "id": "dc01"}]}, live_scope=None
    )

    assert "dc01" in names


# ── Out-of-process scope edits ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_out_of_process_scope_edits_reach_the_guard_via_resync(monkeypatch):
    """An operator's Scope-tab edit lands in OpenAEV with no in-process tool call.

    The in-process sync only fires when the orchestrator itself reads or replaces
    scope; an operator editing the Scope tab changes OpenAEV out of process. The
    heartbeat re-reads the live scope on its cadence and teaches the guard the new
    target in place, so a lesson written after the edit is refused even though the
    orchestrator never called a scope tool.
    """
    from app.services import autonomous_pentest as ap

    async def _fake_read(_payload):
        return {"allowlist": [{"name": "dc02"}], "denylist": []}

    monkeypatch.setattr(ap, "_read_openaev_run_scope", _fake_read)

    token = pm.set_tradecraft_run_context([])
    try:
        # Not yet a target: it was added out of process after the cycle started.
        assert pm.tradecraft_leak_reason("dc02 refused the relay") is None

        await ap._sync_run_scope_into_guard({"openaev_run_id": "r"})

        assert pm.tradecraft_leak_reason("dc02 refused the relay") is not None
    finally:
        pm.reset_tradecraft_run_context(token)


@pytest.mark.asyncio
async def test_out_of_process_resync_never_unlocks_a_failed_closed_cycle(monkeypatch):
    """A cycle that could not enumerate its targets at the start stays closed.

    The resync only ever EXTENDS an existing context; it must never establish one,
    or an OpenAEV outage at cycle start followed by a later readable scope would
    silently unlock the write mid-cycle - the exact fail-open the cycle-start
    gating exists to prevent.
    """
    from app.services import autonomous_pentest as ap

    async def _fake_read(_payload):
        return {"allowlist": [{"name": "dc02"}]}

    monkeypatch.setattr(ap, "_read_openaev_run_scope", _fake_read)

    assert not pm.in_tradecraft_run_context()
    await ap._sync_run_scope_into_guard({"openaev_run_id": "r"})
    assert not pm.in_tradecraft_run_context()


def test_heartbeat_resyncs_scope_into_the_guard_out_of_band():
    """The heartbeat is what carries an out-of-process Scope-tab edit to the guard."""
    from app.services import autonomous_pentest as ap

    cycle_src = inspect.getsource(ap.run_autonomous_cycle)
    # Context established BEFORE the heartbeat task is created, so the task shares
    # the same target-set object and can extend it in place.
    assert cycle_src.index("set_tradecraft_run_context(") < cycle_src.index(
        "_heartbeat_loop("
    )
    # ...and only when a context actually exists (a fail-closed cycle syncs nothing).
    assert "sync_tradecraft=tradecraft_token is not None" in cycle_src

    loop_src = inspect.getsource(ap._heartbeat_loop)
    assert "_sync_run_scope_into_guard(payload)" in loop_src

    # Add-only and shape-checked: it extends the guard, never replaces or clears,
    # and only from a scope view the harvester can actually read.
    sync_src = inspect.getsource(ap._sync_run_scope_into_guard)
    assert "extend_tradecraft_run_context" in sync_src
    assert "scope_view_is_usable" in sync_src


# ── Concurrency ──────────────────────────────────────────────────────────────


def test_tradecraft_write_is_serialized_per_agent():
    """Two concurrent runs share one orchestrator.

    Without serialization both can observe "no duplicate", both insert, and both
    evaluate the cap without seeing the other's row.
    """
    src = inspect.getsource(pm.persist_memory)
    assert "_lock_tradecraft_pool" in src
    # Taken before the dedup probe, or the race it closes is still open.
    assert src.index("_lock_tradecraft_pool") < src.index("_is_duplicate_fact")
    # ...but AFTER the embedding, so the lock (and the pooled connection its
    # statement checks out) is never held across an outbound HTTP round-trip.
    assert src.index("embed_text_with_model") < src.index("_lock_tradecraft_pool")

    lock_src = inspect.getsource(pm._lock_tradecraft_pool)
    # Transaction-scoped: releases on commit/rollback, no unlock path to forget.
    assert "pg_advisory_xact_lock" in lock_src
