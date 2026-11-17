"""Tests for the user-facing turn-exhaustion digest and reasoning capture.

Covers the UX fixes:

* ``summarise_tool_results`` — the emergency digest shown when the loop
  exhausts its budget must be human-readable: no internal truncation
  markers, humanized tool names, repeated identical results collapsed,
  friendly lead-in and closing.
* ``ReasoningAccumulator`` — reasoning prose persisted for the
  reasoning-details panel keeps the head AND a rolling tail, so the final
  wind-down reasoning survives the cap on long turns instead of being
  silently dropped.
"""

import asyncio
import time

from app.api.v1.chat import _REASONING_META_MAX, ReasoningAccumulator
from app.llm.base import ChatMessage, FileRef
from app.tools.agent_loop import (
    EMPTY_RESPONSE_FALLBACK,
    _humanise_tool_name,
    recover_empty_response,
    summarise_tool_results,
    summarise_tool_results_resolved,
)
from app.tools.base import ToolCall


def _tool_msg(name: str, content: str) -> ChatMessage:
    return ChatMessage(role="tool", content=content, name=name)


# ---------------------------------------------------------------------------
# summarise_tool_results
# ---------------------------------------------------------------------------


def test_digest_never_quotes_truncation_markers():
    """Internal context-management stubs must not leak into the digest."""
    msgs = [
        _tool_msg(
            "list_runs",
            "[Truncated — originally 2,066 chars. Showing last 160 chars.] "
            "…ST",
        ),
        _tool_msg("get_run", "Run abc completed successfully with 3 actions."),
    ]

    digest = summarise_tool_results(msgs)

    assert "[Truncated" not in digest
    assert "Showing last" not in digest
    # The stubbed result carried no useful tail — it counts as omitted.
    assert "could not be included" in digest
    assert "Run abc completed successfully" in digest


def test_digest_keeps_useful_tail_of_truncated_result():
    tail = (
        "the HarfangLab pull request already carried the feature and "
        "integrations labels, so no update was needed there."
    )
    msgs = [
        _tool_msg(
            "get_run",
            f"[Truncated — originally 5,000 chars. Showing last 200 chars.] …{tail}",
        ),
    ]

    digest = summarise_tool_results(msgs)

    assert "[Truncated" not in digest
    assert tail in digest
    # The elided beginning is signalled with a leading ellipsis.
    assert "… the HarfangLab" in digest


def test_digest_omits_all_curation_stub_shapes():
    """All three context-curation stub shapes ("[Pruned tool result …]",
    "[Duplicate …]", "[Earlier …]") are internal artifacts — the digest
    must count them as omitted, never quote them verbatim."""
    msgs = [
        _tool_msg(
            "list_runs",
            "[Pruned tool result — originally 9,000 chars. Content omitted "
            "to stay within context budget.]",
        ),
        _tool_msg(
            "list_runs",
            "[Duplicate list_runs result collapsed — identical output "
            "(2,000 chars) appears again later in the conversation; see "
            "the most recent copy.]",
        ),
        _tool_msg(
            "get_run",
            "[Earlier get_run result (5,000 chars) cleared to keep the "
            "working context focused — it is stale and re-fetchable. "
            "Re-call the tool only if you still need this exact data.]",
        ),
        _tool_msg("web_fetch", "Real content about the topic."),
    ]

    digest = summarise_tool_results(msgs)

    assert "[Pruned" not in digest
    assert "[Duplicate" not in digest
    assert "[Earlier" not in digest
    assert "re-fetchable" not in digest
    assert "3 tool result(s) could not be included" in digest
    assert "Real content about the topic." in digest


def test_digest_collapses_repeated_identical_results():
    msgs = [_tool_msg("list_runs", "No runs found.") for _ in range(6)]

    digest = summarise_tool_results(msgs)

    assert digest.count("No runs found.") == 1
    assert "identical result 6 times" in digest


def test_digest_does_not_collapse_results_sharing_a_prefix():
    """De-duplication keys on the FULL content, not the truncated preview —
    two long results sharing the same first ~600 chars but diverging later
    must stay separate entries."""
    shared_prefix = "run inventory: " + ("x" * 700)
    msgs = [
        _tool_msg("list_runs", shared_prefix + " tail-one"),
        _tool_msg("list_runs", shared_prefix + " tail-two"),
    ]

    digest = summarise_tool_results(msgs)

    assert "times" not in digest
    assert digest.count("**List runs**") == 2


def test_digest_humanizes_tool_names():
    msgs = [_tool_msg("list_assignment_runs", "Found 3 runs.")]

    digest = summarise_tool_results(msgs)

    assert "**List assignment runs**" in digest
    assert "list_assignment_runs" not in digest


def test_digest_lead_in_and_closing_are_friendly():
    msgs = [_tool_msg("search_memory", "Nothing stored about this topic.")]

    digest = summarise_tool_results(msgs)

    first_line = digest.splitlines()[0]
    assert "summary of what was completed this turn" in first_line
    assert "wasn't able to finish" not in digest
    assert "ask me to continue" in digest.splitlines()[-1]


def test_digest_lead_in_is_a_failure_when_every_result_is_an_error():
    msgs = [
        _tool_msg("upload_notion_file", "Error: file is not available in this conversation"),
        _tool_msg("delete_notion_block", "Failed to delete block: archived"),
    ]

    digest = summarise_tool_results(msgs)

    first_line = digest.splitlines()[0]
    assert "stopped before a final answer" in first_line
    assert "wasn't able to finish" not in digest
    assert "ask me to continue" in digest.splitlines()[-1]


def test_digest_filters_wait_plumbing_instead_of_quoting_it():
    """A wait-budget death must not surface as a digest of wait() strings."""
    pending = "11111111-1111-4111-8111-111111111111"
    msgs = [
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(
                    id="w1",
                    name="wait",
                    arguments={"duration": "5m", "until": pending},
                )
            ],
        ),
        _tool_msg("wait", "Waited 5m. Remaining wait budget for this run: 0s."),
        _tool_msg("wait", "Waited 5m. Remaining wait budget for this run: 0s."),
        _tool_msg("check_task_status", f"{pending[:12]}: running"),
        _tool_msg("load_tools", "Loaded group opencti"),
    ]

    digest = summarise_tool_results(msgs)

    assert "I wasn't able to finish" not in digest
    assert "Waited 5m" not in digest
    assert "Loaded group" not in digest
    # The task is counted, never quoted by id: this transcript has no
    # spawn result to name it from.
    assert pending not in digest
    assert "1 background task is still running" in digest
    # Reworded to a clean auto-resume promise, not a manual dead-end.
    assert "automatically" in digest


def test_digest_names_the_pending_task_from_its_spawn_in_the_transcript():
    """When the transcript still holds the spawn exchange, the wait-budget
    message names the task by agent + description, not by id.

    The production shape: one spawn followed by a dozen wait rounds, so the
    spawn result has left the digest's recent-results window (only plumbing
    is recent) while the whole transcript still carries the label.
    """
    pending = "11111111-1111-4111-8111-111111111111"
    msgs = [
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(
                    id="s1",
                    name="spawn_background_task",
                    arguments={"agent_id": "research-analyst", "task": "Leak analysis"},
                )
            ],
        ),
        ChatMessage(
            role="tool",
            name="spawn_background_task",
            tool_call_id="s1",
            content=(
                "Background task spawned successfully.\n"
                f"Task ID: {pending}\nTarget agent: Research Analyst\nStatus: pending\n"
            ),
        ),
    ]
    for i in range(12):
        msgs.append(
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=[
                    ToolCall(
                        id=f"w{i}",
                        name="wait",
                        arguments={"duration": "5m", "until": pending},
                    )
                ],
            )
        )
        msgs.append(
            _tool_msg("wait", f"Waited 5m. Remaining wait budget for this run: {55 - 5 * i}m.")
        )

    digest = summarise_tool_results(msgs)

    assert "Waited 5m" not in digest
    assert pending not in digest
    assert '**Research Analyst** - "Leak analysis"' in digest
    assert "as soon as it finishes" in digest


def _spawn_exchange(call_id: str, task_id: str, *, agent: str, task: str) -> list[ChatMessage]:
    return [
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(
                    id=call_id,
                    name="spawn_background_task",
                    arguments={"agent_id": "irrelevant", "task": task},
                )
            ],
        ),
        ChatMessage(
            role="tool",
            name="spawn_background_task",
            tool_call_id=call_id,
            content=(
                "Background task spawned successfully.\n"
                f"Task ID: {task_id}\nTarget agent: {agent}\nStatus: pending\n"
            ),
        ),
    ]


def test_digest_names_each_task_with_its_own_description_under_repeated_ids():
    """Gemini mints ``call_{name}_{index}`` per response, so two turns that
    each open with a spawn share ``call_spawn_background_task_0``. The digest
    must still name the OLDER pending task with its own description, not the
    newer turn's. Same production shape as above: enough wait rounds
    that only plumbing is recent and the park label carries the message."""
    older = "11111111-1111-4111-8111-111111111111"
    newer = "33333333-3333-4333-8333-333333333333"
    msgs = [
        *_spawn_exchange(
            "call_spawn_background_task_0", older, agent="Research Analyst", task="Leak analysis"
        ),
        ChatMessage(role="assistant", content="Started the analysis."),
        ChatMessage(role="user", content="also the PDFs, then wait for the analysis"),
        *_spawn_exchange(
            "call_spawn_background_task_0", newer, agent="Report Writer", task="Build the PDFs"
        ),
        *_wait_rounds(older, 12),
    ]

    digest = summarise_tool_results(msgs)

    assert '**Research Analyst** - "Leak analysis"' in digest
    assert "Build the PDFs" not in digest
    assert older not in digest and newer not in digest


def _wait_rounds(pending: str, n: int) -> list[ChatMessage]:
    msgs: list[ChatMessage] = []
    for i in range(n):
        msgs.append(
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=[
                    ToolCall(
                        id=f"w{i}",
                        name="wait",
                        arguments={"duration": "5m", "until": pending},
                    )
                ],
            )
        )
        msgs.append(_tool_msg("wait", "Waited 5m. Remaining wait budget for this run: 0s."))
    return msgs


def _labelled_condition(pending: str, *, calls: list[list[str]]):
    """A wait condition whose ``.labels`` hook names *pending* and records
    what it was asked for."""

    async def _condition(ids):
        return False, {tid: "running" for tid in ids}

    async def _labels(ids):
        calls.append(list(ids))
        return {
            pending: {
                "agent_name": "Research Analyst",
                "task_description": "Leak analysis",
            }
        }

    _condition.labels = _labels
    return _condition


async def test_resolved_digest_names_a_task_the_transcript_cannot():
    """The spawn result has been pruned to a curation stub, so the transcript
    carries no label: the in-loop digest falls back to the wait condition
    exactly like the live park does — the two never diverge."""
    pending = "11111111-1111-4111-8111-111111111111"
    msgs = [
        ChatMessage(
            role="tool",
            name="spawn_background_task",
            tool_call_id="s1",
            content="[Pruned tool result — originally 240 chars. Content omitted.]",
        ),
        *_wait_rounds(pending, 3),
    ]
    asked: list[list[str]] = []

    # The sync form can only count the task ...
    sync_digest = summarise_tool_results(msgs)
    assert "1 background task is still running" in sync_digest
    assert pending not in sync_digest

    # ... the resolved form names it, through the condition.
    digest = await summarise_tool_results_resolved(
        msgs, _labelled_condition(pending, calls=asked)
    )
    assert asked == [[pending]]
    assert pending not in digest
    assert '**Research Analyst** - "Leak analysis"' in digest
    assert "Waited 5m" not in digest
    assert "[Pruned" not in digest


async def test_resolved_digest_never_queries_when_it_has_findings():
    """A digest with real findings is not a park: the label lookup must not
    run (it is a database read) and the text is the ordinary digest."""
    pending = "11111111-1111-4111-8111-111111111111"
    msgs = [
        _tool_msg("search_knowledge", "Found the Metabase pod logs."),
        *_wait_rounds(pending, 2),
    ]
    asked: list[list[str]] = []

    digest = await summarise_tool_results_resolved(
        msgs, _labelled_condition(pending, calls=asked)
    )
    assert asked == []
    assert digest == summarise_tool_results(msgs)
    assert "Found the Metabase pod logs" in digest

    # No condition at all (anonymous embed turn): same digest, no crash.
    assert await summarise_tool_results_resolved(msgs, None) == digest
    park = await summarise_tool_results_resolved(_wait_rounds(pending, 2), None)
    assert "1 background task is still running" in park


async def test_resolved_digest_degrades_to_the_transcript_when_the_lookup_stalls():
    """The label read is bounded: a pool acquisition that never returns must
    cost the label, not the message. Past the bound the text is exactly the
    synchronous, transcript-only digest; a prompt resolver still names the
    task; a zero budget (deadline nearly spent) never enters the resolver."""
    pending = "11111111-1111-4111-8111-111111111111"
    msgs = [
        ChatMessage(
            role="tool",
            name="spawn_background_task",
            tool_call_id="s1",
            content="[Pruned tool result — originally 240 chars. Content omitted.]",
        ),
        *_wait_rounds(pending, 3),
    ]
    never = asyncio.Event()
    entered: list[list[str]] = []

    async def _condition(ids):
        return False, {tid: "running" for tid in ids}

    async def _hanging(ids):
        entered.append(list(ids))
        await never.wait()
        return {pending: {"agent_name": "Research Analyst", "task_description": "x"}}

    _condition.labels = _hanging

    started = time.monotonic()
    digest = await summarise_tool_results_resolved(msgs, _condition, lookup_timeout=0.05)
    assert time.monotonic() - started < 1
    assert entered == [[pending]]
    assert digest == summarise_tool_results(msgs)
    assert "1 background task is still running" in digest
    assert pending not in digest

    entered.clear()
    digest = await summarise_tool_results_resolved(msgs, _condition, lookup_timeout=0)
    assert entered == [], "no budget, no database read"
    assert digest == summarise_tool_results(msgs)

    asked: list[list[str]] = []
    digest = await summarise_tool_results_resolved(
        msgs, _labelled_condition(pending, calls=asked), lookup_timeout=0.05
    )
    assert asked == [[pending]]
    assert '**Research Analyst** - "Leak analysis"' in digest


async def test_recover_empty_response_names_the_parked_task():
    """The caller-side backstop (agent_chat / general_chat) resolves labels
    through the wait condition it already holds; without one it degrades to
    the count, and with nothing to digest to the generic fallback."""
    pending = "11111111-1111-4111-8111-111111111111"
    msgs = [ChatMessage(role="user", content="wait for it"), *_wait_rounds(pending, 2)]
    asked: list[list[str]] = []

    text = await recover_empty_response(
        msgs,
        context="test",
        conversation_id=None,
        model=None,
        wait_condition=_labelled_condition(pending, calls=asked),
    )
    assert asked == [[pending]]
    assert '**Research Analyst** - "Leak analysis"' in text
    assert pending not in text

    text = await recover_empty_response(msgs, context="test", conversation_id=None, model=None)
    assert "1 background task is still running" in text
    assert pending not in text

    assert (
        await recover_empty_response(
            [ChatMessage(role="user", content="hi")],
            context="test",
            conversation_id=None,
            model=None,
        )
        == EMPTY_RESPONSE_FALLBACK
    )


def test_digest_stays_empty_for_plain_pacing_waits_without_until():
    """Plumbing-only results WITHOUT named task ids must not claim the run
    is waiting on background tasks — a plain wait(duration) pacing call
    (e.g. polling an external API) has no unfinished background tasks."""
    msgs = [
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(id="w1", name="wait", arguments={"duration": "30s"})
            ],
        ),
        _tool_msg("wait", "Waited 30s."),
        _tool_msg("load_tools", "Loaded group opencti"),
    ]

    assert summarise_tool_results(msgs) == ""


def test_digest_keeps_real_findings_when_mixed_with_plumbing():
    msgs = [
        _tool_msg("wait", "Waited 5m."),
        _tool_msg("search_knowledge", "Found the Metabase pod logs."),
    ]

    digest = summarise_tool_results(msgs)

    assert "Waited 5m" not in digest
    assert "Found the Metabase pod logs" in digest
    assert "summary of what was completed this turn" in digest


def test_digest_omits_suffixed_plumbing_names():
    """Integration-prefixed wait / load_tools must not leak into the digest
    the way a bare ``wait`` is already filtered."""
    msgs = [
        _tool_msg("notion_wait", "Waited 5m. Remaining wait budget for this run: 25m."),
        _tool_msg("opencti_load_tools", "Loaded group opencti"),
        _tool_msg("search_knowledge", "Found the Metabase pod logs."),
    ]

    digest = summarise_tool_results(msgs)

    assert "Waited 5m" not in digest
    assert "Loaded group" not in digest
    assert "Found the Metabase pod logs" in digest


def test_digest_names_generate_file_from_file_ref():
    """``generate_file`` returns only ``[[FILE:uuid]]``. The digest must
    show the filename, not an empty preview after the marker is consumed
    by the chat renderer."""
    file_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    msgs = [
        ChatMessage(
            role="tool",
            name="generate_file",
            content=f"[[FILE:{file_id}]]",
            files=[
                FileRef(
                    file_id=file_id,
                    filename="Filigran-ITS-212-Rapport-judiciaire-FR.pdf",
                    content_type="application/pdf",
                    size=62700,
                    s3_object_name="generated/report.pdf",
                )
            ],
        )
    ]

    digest = summarise_tool_results(msgs)

    assert "[[FILE:" not in digest
    assert "Filigran-ITS-212-Rapport-judiciaire-FR.pdf" in digest
    assert "Generate file" in digest
    assert "summary of what was completed this turn" in digest


def test_humanise_tool_name():
    assert _humanise_tool_name("list_runs") == "List runs"
    assert _humanise_tool_name("web-fetch") == "Web fetch"
    assert _humanise_tool_name("") == "Tool"


# ---------------------------------------------------------------------------
# ReasoningAccumulator
# ---------------------------------------------------------------------------


def test_reasoning_accumulator_plain_accumulation_under_cap():
    acc = ReasoningAccumulator()
    acc.add("first chunk. ")
    acc.add("second chunk.")

    assert acc.render() == "first chunk. second chunk."


def test_reasoning_accumulator_preserves_tail_over_cap():
    """The FINAL reasoning (wind-down) must survive the cap — previously
    the head-only cap dropped everything after 20K chars, losing exactly
    the part that explains why a long turn stopped."""
    acc = ReasoningAccumulator()
    acc.add("x" * (ReasoningAccumulator.HEAD_MAX + 50_000))
    ending = "Turn budget is running low. I will summarize what I found."
    acc.add(ending)

    rendered = acc.render()
    assert rendered.endswith(ending)
    assert ReasoningAccumulator.ELISION.strip() in rendered
    assert len(rendered) <= _REASONING_META_MAX


def test_reasoning_accumulator_no_elision_marker_when_nothing_dropped():
    acc = ReasoningAccumulator()
    acc.add("x" * ReasoningAccumulator.HEAD_MAX)
    acc.add("tail that fits")

    rendered = acc.render()
    assert "elided" not in rendered
    assert rendered.endswith("tail that fits")


def test_reasoning_accumulator_reset():
    acc = ReasoningAccumulator()
    acc.add("x" * (_REASONING_META_MAX * 2))
    acc.reset()

    assert acc.render() == ""
    acc.add("fresh")
    assert acc.render() == "fresh"
