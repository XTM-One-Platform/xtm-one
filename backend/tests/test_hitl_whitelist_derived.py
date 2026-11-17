"""Reads of what a deployment connects are exempt by classification.

``search_opencti`` ran without asking while ``search_notion`` paused, for no
reason anyone could state except which registry the tool lived in. The platform
half already derived its exempt reads from the classifier; this applies the same
rule to the integrations and MCP servers a deployment has actually connected.

Two boundaries are what these tests exist to hold: nothing unconnected is
exempted, and channel bot tools are never derived — reading somebody's Slack is
a different decision from reading platform data, and not the classifier's.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import hitl_whitelist_derived as derived
from app.services.tool_classification import infer_tool_type


class _DB:
    """Answers the two queries in order: integration providers, then MCP tools."""

    def __init__(self, providers=("notion",), mcp_tools=(), server_id="srv-1"):
        self._answers = [
            [(p,) for p in providers],
            [(server_id, list(mcp_tools))],
        ]

    async def execute(self, _stmt):
        rows = self._answers.pop(0)
        return SimpleNamespace(all=lambda: rows)


@pytest.fixture(autouse=True)
def _fresh_cache():
    derived.reset_cache()
    yield
    derived.reset_cache()


# --- the classifier ---------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["get_or_create_entity", "search_and_delete", "list_or_archive_items"]
)
def test_a_conjunction_naming_a_write_beats_the_leading_read_verb(name):
    """The rule that made auto-exemption unsafe: these open with a read verb.

    Widening the derivation without this would have exempted a tool that
    creates, deletes or archives, because it happens to start with ``get`` or
    ``search``.
    """
    assert infer_tool_type(name) == "write"


@pytest.mark.parametrize(
    "name",
    [
        "get_opengrc_crq_run",
        "list_opengrc_crq_run_history",
        "get_openaev_run_scope",
        "search_opencti",
    ],
)
def test_a_write_token_used_as_a_noun_is_still_a_read(name):
    """Why the rule stays narrow.

    "any write token anywhere" would re-gate these — ``run`` is a noun in every
    one — which is what the leading-verb precedence exists to prevent.
    """
    assert infer_tool_type(name) == "read"


# --- the derivation ---------------------------------------------------------


@pytest.mark.asyncio
async def test_only_connected_providers_contribute():
    """A provider nobody connected exempts nothing: its tools do not exist here."""
    names = await derived.connected_read_tools(_DB(providers=("notion",)))

    assert names, "a connected provider should contribute its reads"
    assert all("notion" in n or not n.startswith("create") for n in names)

    none = await derived.connected_read_tools(_DB(providers=()))
    assert none == frozenset()


@pytest.mark.asyncio
async def test_writes_of_a_connected_provider_stay_gated():
    names = await derived.connected_read_tools(_DB(providers=("notion",)))

    assert not [n for n in names if infer_tool_type(n) == "write"]


@pytest.mark.asyncio
async def test_mcp_reads_are_derived_from_what_a_server_discovered():
    db = _DB(
        providers=(),
        mcp_tools=[{"name": "search_wiki"}, {"name": "delete_wiki_page"}],
    )

    names = await derived.connected_read_tools(db)

    assert "search_wiki" in names
    assert "delete_wiki_page" not in names


@pytest.mark.asyncio
async def test_channel_bot_tools_are_never_derived():
    """``read_slack_bot_messages`` classifies as a read and is one — of a
    person's conversation. That is a decision for a human, on the record, not
    one the classifier gets to make from a verb."""
    assert infer_tool_type("read_slack_bot_messages") == "read"

    names = await derived.connected_read_tools(_DB(providers=("slack_bot",)))

    assert "read_slack_bot_messages" not in names
    assert "list_discord_bot_channels" not in names


@pytest.mark.asyncio
async def test_a_hand_gated_tool_is_never_handed_back():
    """A hand-gate outranks the classifier, here as in the platform layer.

    ``download_opencti_file`` classifies as a read and is one — of an arbitrary
    third-party document pulled into the model's context, which is exactly why
    somebody gated it by hand. Registering an OpenCTI platform also creates an
    integration row for it, so without this filter the widened derivation
    reversed that decision on every such deployment, silently.
    """
    from app.services.hitl_whitelist_platform import HAND_GATED

    assert HAND_GATED, "the guard is meaningless if the set is empty"

    names = await derived.connected_read_tools(_DB(providers=("opencti",)))

    assert names, "opencti should still contribute its other reads"
    assert not (names & set(HAND_GATED))


@pytest.mark.asyncio
async def test_a_personal_mcp_server_cannot_widen_the_deployment_whitelist():
    """Binding one needs no administrator, and this set is deployment-wide.

    An unscoped read let any user un-gate a name for everybody by declaring a
    read-shaped tool on a server of their own — and the whitelist matches by
    name, so the name they chose need not have been theirs.
    """
    import app.services.hitl_whitelist_derived as mod

    captured = {}

    class _Capture(_DB):
        async def execute(self, stmt):
            captured.setdefault("sql", []).append(str(stmt))
            return await super().execute(stmt)

    await mod.connected_read_tools(_Capture(providers=()))

    mcp_query = next(q for q in captured["sql"] if "mcp_servers" in q)
    assert "is_company_managed" in mcp_query
    assert "enabled" in mcp_query


@pytest.mark.asyncio
async def test_a_company_managed_mcp_server_cannot_derive_a_channel_bot_tool():
    """The module's other promised boundary, on the branch that honoured neither.

    The integration branch excludes bots structurally — ``CHANNEL_BOT_TOOLS`` is
    a separate registry from ``PROVIDER_TOOLS`` — so ``providers=()`` tests can
    never reach this. A remote MCP server picks its own names.
    """
    bot_tool = "read_slack_bot_messages"
    assert infer_tool_type(bot_tool) == "read", "the classifier must want it back"
    assert bot_tool in derived.excluded_from_derivation()

    db = _DB(providers=(), mcp_tools=[{"name": bot_tool}, {"name": "search_wiki"}])

    names = await derived.connected_read_tools(db)

    assert "search_wiki" in names
    assert bot_tool not in names


@pytest.mark.asyncio
async def test_a_company_managed_mcp_server_cannot_reverse_a_hand_gate():
    """The company-managed branch is admin-gated, but not name-gated.

    ``discovered_tools`` comes from a remote endpoint, so its names are not the
    deployment's to choose even when the registration is. The whitelist matches
    by bare name, so a server declaring ``download_opencti_file`` un-gated that
    tool for every caller on every surface — not just its own.
    """
    from app.services.hitl_whitelist_platform import HAND_GATED

    gated = "download_opencti_file"
    assert gated in HAND_GATED
    assert infer_tool_type(gated) == "read", "the classifier must want it back"

    db = _DB(providers=(), mcp_tools=[{"name": gated}, {"name": "search_wiki"}])

    names = await derived.connected_read_tools(db)

    assert "search_wiki" in names, "the server's other reads still contribute"
    assert gated not in names


@pytest.mark.asyncio
async def test_the_reads_are_resolved_once_per_session():
    """``default_membership`` runs on every gate resolution; these two reads
    answer identically within one request, and a process-wide cache would be
    wrong rather than merely slow — a provider connected a moment ago would
    keep gating."""
    db = _DB(providers=("notion",))
    db.info = {}

    first = await derived.connected_read_tools(db)
    calls_after_first = len(db._answers)
    second = await derived.connected_read_tools(db)

    assert second == first
    assert len(db._answers) == calls_after_first, "the second call re-queried"


def test_a_failed_builder_is_not_memoised_as_a_provider_with_no_tools():
    """``build_provider_tools`` stores ``[]`` for a builder that raised, which
    is indistinguishable from a provider with no tools. Memoising that gated the
    provider's reads for the life of the process."""
    import app.services.integration_tools as it

    def _boom(*_a, **_k):
        raise RuntimeError("introspection failed")

    original = dict(it.PROVIDER_TOOLS)
    it.PROVIDER_TOOLS["notion"] = _boom
    try:
        derived.provider_read_tools()
        assert "notion" not in derived._PROVIDER_READS_CACHE, (
            "a failed provider must not be cached as having no tools"
        )
        assert "notion" in derived._PROVIDER_RETRY_AFTER, (
            "it must be scheduled for retry instead"
        )
    finally:
        it.PROVIDER_TOOLS.clear()
        it.PROVIDER_TOOLS.update(original)
        derived.reset_cache()


def test_one_broken_provider_does_not_re_introspect_the_healthy_ones():
    """The cost the old "complete passes only" rule paid.

    Nothing was memoised unless every provider built, so a single persistently
    broken provider turned a cached lookup into a full re-introspection of all
    of them on every gate resolution — once per agent turn, per assignment run,
    and twice per ``GET /hitl-whitelist``.
    """
    import app.services.integration_tools as it

    calls: dict[str, int] = {"ok": 0, "broken": 0}

    def _ok(*_a, **_k):
        calls["ok"] += 1
        return []

    def _boom(*_a, **_k):
        calls["broken"] += 1
        raise RuntimeError("introspection failed")

    original = dict(it.PROVIDER_TOOLS)
    it.PROVIDER_TOOLS.clear()
    it.PROVIDER_TOOLS.update({"healthy": _ok, "broken": _boom})
    try:
        derived.reset_cache()
        derived.provider_read_tools()
        derived.provider_read_tools()
        derived.provider_read_tools()

        assert calls["ok"] == 1, "a provider that built is introspected once"
        assert calls["broken"] == 1, "a failed one waits out its retry window"
    finally:
        it.PROVIDER_TOOLS.clear()
        it.PROVIDER_TOOLS.update(original)
        derived.reset_cache()


def test_a_failed_provider_is_retried_once_its_window_expires():
    """Bounded staleness: the failure is usually transient, so it must not gate
    that provider's reads until the pod restarts."""
    import app.services.integration_tools as it

    calls = {"n": 0}

    def _boom(*_a, **_k):
        calls["n"] += 1
        raise RuntimeError("introspection failed")

    original = dict(it.PROVIDER_TOOLS)
    it.PROVIDER_TOOLS.clear()
    it.PROVIDER_TOOLS["broken"] = _boom
    try:
        derived.reset_cache()
        derived.provider_read_tools()
        assert calls["n"] == 1

        # Expire the window rather than sleeping through it.
        derived._PROVIDER_RETRY_AFTER["broken"] = 0.0
        derived.provider_read_tools()
        assert calls["n"] == 2, "the window expired, so it is retried"
    finally:
        it.PROVIDER_TOOLS.clear()
        it.PROVIDER_TOOLS.update(original)
        derived.reset_cache()
