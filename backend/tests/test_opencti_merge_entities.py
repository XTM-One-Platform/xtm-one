"""Tests for the OpenCTI ``merge_opencti_entities`` tool.

Merge is the one knowledge mutation that must NOT go through the draft gate:
OpenCTI's ``stixEdit.merge`` ignores draft context entirely, so staging it in a
draft would claim "queued for human review" while irreversibly rewriting the
live graph. These tests pin that decision plus the guards that replace it —
including the human-in-the-loop escalation that lets an autonomous run merge
only after a human has verifiably approved that exact operation.

The input-validation tests return before any network call. The escalation tests
stub ``_graphql`` (never real HTTP) and assert on whether the merge mutation was
reached at all.
"""

from app.api.v1.mcp_platform import OPENCTI_MCP_TOOLS
from app.integrations.providers.opencti import build_tools
from app.integrations.providers.opencti import knowledge as knowledge_mod
from app.integrations.providers.opencti._common import _DRAFT_REQUIRED_TOOLS
from app.services.integration_tools import hitl_history_approves
from app.tools.base import HITL_APPROVAL_KEYWORD, CallableToolExecutor, ToolCall

TOKEN = "https://demo.opencti.io||test-token"
TOOL = "merge_opencti_entities"


def _build(
    scope_headers=None,
    autonomous=False,
    direct_mutations=False,
    hitl_approval_fn=None,
):
    executor = CallableToolExecutor()
    tools = build_tools(
        TOKEN,
        executor,
        scope_headers=scope_headers or {},
        autonomous=autonomous,
        direct_mutations=direct_mutations,
        hitl_approval_fn=hitl_approval_fn,
    )
    return executor, tools


async def _call(executor, **arguments):
    result = await executor.execute_tool(ToolCall(id="merge-test", name=TOOL, arguments=arguments))
    return result


# ── Registration ─────────────────────────────────────────────────────────


def test_merge_tool_is_registered_with_a_handler():
    executor, tools = _build()
    by_name = {t.name: t for t in tools}
    assert TOOL in by_name, "merge tool definition missing"
    assert TOOL in executor._handlers, "merge tool has no executor handler"

    schema = by_name[TOOL].input_schema
    assert schema["type"] == "object"
    assert set(schema["required"]) == {"target_id", "source_ids"}
    assert schema["properties"]["source_ids"]["type"] == "array"
    assert by_name[TOOL].source == "integration:opencti"


def test_merge_description_warns_it_cannot_be_undone():
    _, tools = _build()
    description = next(t for t in tools if t.name == TOOL).description
    assert "CANNOT BE UNDONE" in description
    assert "SAME TYPE" in description


# ── The draft gate must not apply ─────────────────────────────────────────


def test_merge_is_not_draft_gated():
    """OpenCTI cannot stage a merge in a draft, so it must stay off the gate."""
    assert TOOL not in _DRAFT_REQUIRED_TOOLS


def test_merge_description_carries_no_draft_gate_instructions():
    # Built without a draft, gated tools get the "go find or create a draft"
    # instructions appended. Merge must not pick those up: it would send the
    # agent off to create a draft that cannot make the merge reviewable. The
    # description does still *mention* drafts, to say they do not apply.
    _, tools = _build()
    description = next(t for t in tools if t.name == TOOL).description
    assert "use_opencti_draft" not in description
    assert "create_opencti_draft" not in description
    assert "cannot stage a merge in a draft" in description


async def test_merge_returns_validation_error_not_the_draft_gate():
    executor, _ = _build()  # no scope_headers -> no active draft
    result = await _call(executor, target_id="", source_ids=["x"])
    assert not result.is_error, result.content
    assert "draft" not in result.content.lower()
    assert "target_id" in result.content


def test_merge_is_unreachable_over_the_platform_mcp_surface():
    """OPENCTI_MCP_TOOLS fails closed; merge is deliberately not listed."""
    assert TOOL not in OPENCTI_MCP_TOOLS


# ── Guards that replace the draft gate ───────────────────────────────────


async def test_merge_refused_in_autonomous_runs_without_hitl():
    """No HITL callback means no human anywhere in the path -> refuse."""
    executor, _ = _build(autonomous=True)
    result = await _call(executor, target_id="t-1", source_ids=["s-1"])
    assert not result.is_error, result.content
    assert "autonomous" in result.content.lower()
    assert "refused" in result.content.lower()
    assert "human-in-the-loop" in result.content.lower()


async def test_merge_rejects_missing_target():
    executor, _ = _build()
    result = await _call(executor, target_id="   ", source_ids=["s-1"])
    assert "target_id" in result.content


async def test_merge_rejects_self_merge():
    executor, _ = _build()
    result = await _call(executor, target_id="same-id", source_ids=["same-id"])
    assert "itself" in result.content.lower()


async def test_merge_rejects_empty_sources():
    executor, _ = _build()
    result = await _call(executor, target_id="t-1", source_ids=[])
    assert "at least one entity ID" in result.content


async def test_merge_rejects_non_list_sources():
    executor, _ = _build()
    result = await _call(executor, target_id="t-1", source_ids=42)
    assert "must be a list" in result.content


async def test_merge_caps_participants_at_four(monkeypatch):
    calls: list = []
    for sid in ("s-3", "s-4"):
        monkeypatch.setitem(_TYPES, sid, "Country")
        monkeypatch.setitem(_NAMES, sid, sid)
    _patch_graphql(monkeypatch, calls)
    executor, _ = _build()
    result = await _call(
        executor,
        target_id="t-1",
        source_ids=["s-1", "s-2", "s-3", "s-4"],
    )
    assert "Refusing to merge 5 entities" in result.content
    assert not any("MergeEntities" in q for q, _ in calls)


async def test_merge_bounds_the_preflight_before_any_lookup(monkeypatch):
    """Aliases collapse after resolution, but the number of lookups is bounded."""
    calls: list = []
    _patch_graphql(monkeypatch, calls)
    executor, _ = _build()
    result = await _call(
        executor,
        target_id="t-1",
        source_ids=[f"s-{i}" for i in range(1, 9)],
    )
    assert calls == [], "no id may be looked up past the preflight bound"
    # Nine ids may be two entities spelled nine ways: the refusal is about the
    # lookups, not about a merge of nine entities.
    assert "Refusing to look up 9 distinct ids" in result.content
    assert "limited to 4 entities" in result.content
    assert "entities at once" not in result.content


async def test_merge_coerces_a_bare_string_source_id():
    """A single id passed as a string is one source, not a list of characters."""
    executor, _ = _build()
    # Same value as the target: only correct coercion makes this a self-merge.
    # Splitting "same-id" into characters would instead reach the network.
    result = await _call(executor, target_id="same-id", source_ids="same-id")
    assert "itself" in result.content.lower()


async def test_merge_deduplicates_repeated_source_ids(monkeypatch):
    """Repeated ids collapse before the cap is counted."""
    for sid in ("s-3", "s-4"):
        monkeypatch.setitem(_TYPES, sid, "Country")
        monkeypatch.setitem(_NAMES, sid, sid)
    _patch_graphql(monkeypatch)
    executor, _ = _build()
    result = await _call(
        executor,
        target_id="t-1",
        source_ids=["s-1", "s-1", "s-2", "s-3", "s-4"],
    )
    # Six ids in, one of them a repeat: the count must reflect the four distinct
    # sources plus the target, not the raw list length.
    assert "Refusing to merge 5 entities" in result.content


# ── HITL escalation in autonomous runs ───────────────────────────────────


_TYPES = {"t-1": "Country", "s-1": "Country", "s-2": "Country"}
_NAMES = {"t-1": "France", "s-1": "France ", "s-2": "FRANCE"}
# Other ids OpenCTI resolves to the same entity (standard_id / stix id).
_ALIASES: dict[str, str] = {
    "identity--t-1": "t-1",
    "identity--s-1": "s-1",
}


def _patch_graphql(monkeypatch, calls=None):
    """Resolve preflight lookups locally and record every query issued."""

    async def _fake_graphql(client, base_url, headers, query, variables=None):
        if calls is not None:
            calls.append((query, variables))
        if "MergePreflight" in query:
            raw = (variables or {}).get("id")
            eid = _ALIASES.get(raw, raw)
            if eid not in _TYPES:
                return {}, None
            return {
                "stixCoreObject": {
                    "id": eid,
                    "standard_id": f"identity--{eid}",
                    "entity_type": _TYPES[eid],
                    "representative": {"main": _NAMES[eid]},
                }
            }, None
        if "MergeEntities" in query:
            return {"stixEdit": {"merge": {"id": "t-1", "entity_type": "Country"}}}, None
        return {}, None

    monkeypatch.setattr(knowledge_mod, "_graphql", _fake_graphql)


def test_merge_action_code_is_stable_and_scoped():
    code = knowledge_mod._merge_action_code("t-1", ["s-1", "s-2"])
    # Deterministic, and independent of the order sources were supplied in.
    assert code == knowledge_mod._merge_action_code("t-1", ["s-2", "s-1"])
    # A different operation must not share the code, or one approval would
    # authorise another merge.
    assert code != knowledge_mod._merge_action_code("t-1", ["s-1"])
    assert code != knowledge_mod._merge_action_code("s-1", ["t-1", "s-2"])
    assert code.startswith("M-")


async def test_merge_escalates_for_approval_when_hitl_enabled(monkeypatch):
    calls: list = []
    _patch_graphql(monkeypatch, calls)

    async def _never_approved(code):
        return False

    executor, _ = _build(autonomous=True, hitl_approval_fn=_never_approved)
    result = await _call(executor, target_id="t-1", source_ids=["s-1"])

    expected_code = knowledge_mod._merge_action_code("t-1", ["s-1"])
    assert "approval required" in result.content.lower()
    assert expected_code in result.content
    assert HITL_APPROVAL_KEYWORD in result.content
    assert "request_human_input" in result.content
    # The human is told the whole contract, so an ambiguous reply is not repeated.
    # A question does not void an approval, so it is not forbidden.
    assert "naming no other code in that reply" in result.content
    assert "asking no question" not in result.content
    # The plan must name the real entities, so the human approves something
    # concrete rather than an opaque pair of IDs.
    assert "France" in result.content
    assert "Country" in result.content
    # Critically: nothing was merged.
    assert not any("MergeEntities" in q for q, _ in calls)


async def test_merge_proceeds_once_a_human_approved(monkeypatch):
    calls: list = []
    _patch_graphql(monkeypatch, calls)
    seen: list = []

    async def _approved(code):
        seen.append(code)
        return True

    executor, _ = _build(autonomous=True, hitl_approval_fn=_approved)
    result = await _call(executor, target_id="t-1", source_ids=["s-1"])

    assert seen == [knowledge_mod._merge_action_code("t-1", ["s-1"])]
    assert any("MergeEntities" in q for q, _ in calls)
    assert "Merged 1 duplicate Country entity" in result.content


async def test_merge_blocks_on_type_mismatch_before_asking_for_approval(monkeypatch):
    """A merge OpenCTI would reject must not reach a human as a question."""
    calls: list = []
    monkeypatch.setitem(_TYPES, "s-2", "City")
    _patch_graphql(monkeypatch, calls)

    async def _never_approved(code):
        raise AssertionError("approval must not be requested for an invalid merge")

    executor, _ = _build(autonomous=True, hitl_approval_fn=_never_approved)
    result = await _call(executor, target_id="t-1", source_ids=["s-2"])

    assert "same type" in result.content.lower()
    assert not any("MergeEntities" in q for q, _ in calls)


async def test_merge_rejects_self_merge_through_an_alias(monkeypatch):
    """The target's standard_id and its internal id are one entity."""
    calls: list = []
    _patch_graphql(monkeypatch, calls)
    executor, _ = _build()
    result = await _call(executor, target_id="identity--t-1", source_ids=["t-1"])
    assert "itself" in result.content.lower()
    assert "'t-1' is the target entity itself" in result.content
    assert not any("MergeEntities" in q for q, _ in calls)


async def test_merge_collapses_aliases_of_one_source(monkeypatch):
    calls: list = []
    _patch_graphql(monkeypatch, calls)
    executor, _ = _build()
    result = await _call(
        executor, target_id="t-1", source_ids=["s-1", "identity--s-1", "s-2"]
    )
    merges = [v for q, v in calls if "MergeEntities" in q]
    assert merges == [{"id": "t-1", "sourceIds": ["s-1", "s-2"]}]
    assert "Merged 2 duplicate Country entities" in result.content
    assert "'identity--s-1' is the same entity as another source" in result.content


async def test_merge_targets_the_resolved_internal_id(monkeypatch):
    """A target given by an alias is merged into by its internal id."""
    calls: list = []
    _patch_graphql(monkeypatch, calls)
    executor, _ = _build()
    await _call(executor, target_id="identity--t-1", source_ids=["s-1"])
    merges = [v for q, v in calls if "MergeEntities" in q]
    assert merges == [{"id": "t-1", "sourceIds": ["s-1"]}]


async def test_merge_cap_counts_resolved_entities_not_aliases(monkeypatch):
    """Four raw sources that are only three entities stay under the cap."""
    calls: list = []
    monkeypatch.setitem(_TYPES, "s-3", "Country")
    monkeypatch.setitem(_NAMES, "s-3", "Fr")
    _patch_graphql(monkeypatch, calls)
    executor, _ = _build()
    result = await _call(
        executor,
        target_id="t-1",
        source_ids=["s-1", "identity--s-1", "s-2", "s-3"],
    )
    assert "Refusing" not in result.content
    assert any("MergeEntities" in q for q, _ in calls)


async def test_merge_hitl_plan_and_code_use_resolved_entities(monkeypatch):
    calls: list = []
    _patch_graphql(monkeypatch, calls)

    async def _never_approved(code):
        return False

    executor, _ = _build(autonomous=True, hitl_approval_fn=_never_approved)
    result = await _call(
        executor, target_id="identity--t-1", source_ids=["identity--s-1", "s-1"]
    )
    # The code is the same however the participants were spelled.
    assert knowledge_mod._merge_action_code("t-1", ["s-1"]) in result.content
    assert result.content.count("(Country, ") == 2  # target + one source
    # The plan names the entities the code and the mutation name.
    assert "Keep: 'France' (Country, t-1)" in result.content
    assert "  - 'France ' (Country, s-1)" in result.content
    assert not any("MergeEntities" in q for q, _ in calls)


async def test_merge_mutation_names_the_resolved_internal_ids(monkeypatch):
    """``stixEdit(id:)`` gets the entities the preflight resolved, not the
    spelling it was given: OpenCTI resolves every id it receives again, and a
    stix id two duplicates both carry would not name the entity the plan and
    the approval code named."""
    calls: list = []
    _patch_graphql(monkeypatch, calls)
    executor, _ = _build()
    result = await _call(
        executor, target_id="identity--t-1", source_ids=["identity--s-1", "s-2"]
    )
    merges = [v for q, v in calls if "MergeEntities" in q]
    assert merges == [{"id": "t-1", "sourceIds": ["s-1", "s-2"]}]
    assert "Merged 2 duplicate Country entities" in result.content


async def test_interactive_runs_do_not_require_the_hitl_code(monkeypatch):
    """Interactive chat has a real user; approval happens in conversation."""
    calls: list = []
    _patch_graphql(monkeypatch, calls)
    executor, _ = _build(autonomous=False)
    result = await _call(executor, target_id="t-1", source_ids=["s-1"])
    assert any("MergeEntities" in q for q, _ in calls)
    assert "approval required" not in result.content.lower()


# ── Consent matching ─────────────────────────────────────────────────────

CODE = "M-DEADBEEF"


def _answered(response):
    return [{"resolution": "answered", "response": response, "question": "q"}]


def test_consent_requires_both_keyword_and_code():
    assert hitl_history_approves(_answered(f"APPROVE {CODE}"), CODE)
    assert hitl_history_approves(_answered(f"APPROVE {CODE} - go ahead"), CODE)
    # "approved" is the natural thing to type, and cannot be negated into being.
    assert hitl_history_approves(_answered(f"Approved {CODE}"), CODE)
    # Case-insensitive on both halves: a human retyping the code in lower case
    # must not be rejected.
    assert hitl_history_approves(_answered(f"approve {CODE.lower()}"), CODE)
    # Keyword without the code: approves nothing specific.
    assert not hitl_history_approves(_answered("APPROVE"), CODE)
    # Code without the keyword.
    assert not hitl_history_approves(_answered(CODE), CODE)
    # An approval for a different operation must not carry over.
    assert not hitl_history_approves(_answered(f"APPROVE {CODE}"), "M-0000FFFF")


def test_consent_rejects_negations_that_contain_the_keyword():
    """The decisive cases: no phrasing that reverses the meaning may pass.

    A bare substring search accepts "DISAPPROVE", and even a whole-word search
    accepts "do not approve" — anchoring the keyword to the start of the reply
    is what rules both out.
    """
    assert not hitl_history_approves(_answered(f"DISAPPROVE {CODE}"), CODE)
    assert not hitl_history_approves(_answered(f"disapproved {CODE}"), CODE)
    assert not hitl_history_approves(_answered(f"do not approve {CODE}"), CODE)
    assert not hitl_history_approves(_answered(f"unapproved {CODE}"), CODE)
    assert not hitl_history_approves(_answered(f"never approve {CODE}"), CODE)
    assert not hitl_history_approves(_answered(f"no - {CODE} is wrong"), CODE)


def test_consent_is_bound_to_the_code_next_to_the_keyword():
    """A reply naming a second code without its own APPROVE approves neither."""
    other = "M-0000FFFF"
    for reply in (f"APPROVE {CODE} - but definitely NOT {other}", f"APPROVE {CODE} and {other}"):
        assert not hitl_history_approves(_answered(reply), CODE)
        assert not hitl_history_approves(_answered(reply), other)
    assert not hitl_history_approves(_answered(f"APPROVE - NOT {other}"), other)
    # A code that merely starts with the approved one is a different code.
    assert not hitl_history_approves(_answered(f"APPROVE {CODE}0"), CODE)
    assert not hitl_history_approves(_answered(f"APPROVE {CODE}\u00e9"), CODE)
    assert not hitl_history_approves(_answered(f"APPROVE {CODE}_x"), CODE)
    # Punctuation between keyword and code, and a pasted backtick, still pass.
    assert hitl_history_approves(_answered(f"APPROVE: {CODE}"), CODE)
    assert hitl_history_approves(_answered(f"`APPROVE {CODE}`"), CODE)


def test_consent_rejects_refusals_and_dismissals():
    assert not hitl_history_approves(_answered(f"no, do not {CODE}"), CODE)
    assert not hitl_history_approves(
        [{"resolution": "dismissed", "response": f"APPROVE {CODE}"}], CODE
    )
    assert not hitl_history_approves([], CODE)
    assert not hitl_history_approves(None, CODE)
    assert not hitl_history_approves(_answered(f"APPROVE {CODE}"), "")


def test_consent_scans_all_history_entries():
    history = [
        {"resolution": "answered", "response": "what are these?"},
        {"resolution": "dismissed", "response": None},
        {"resolution": "answered", "response": f"APPROVE {CODE}"},
    ]
    assert hitl_history_approves(history, CODE)
