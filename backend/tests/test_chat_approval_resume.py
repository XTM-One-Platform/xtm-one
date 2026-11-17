"""Resuming a chat turn that ended at a gated tool call (FR-002c1/c2/c4).

The distinction under test is the one FR-002c3 draws: this is not the mid-turn
channel with a second request bolted on. The process that ran the paused turn
may be gone, so everything the resume needs has to come off the stored message.
"""

from __future__ import annotations

import json
import types

import pytest

from app.services import chat_approval_resume as resume


def _proposal(call_id: str, tool: str = "create_agent", **args) -> dict:
    return {
        "tool_call_id": call_id,
        "tool_name": tool,
        "arguments": args or {"name": "triage"},
        "input_schema": {"type": "object"},
    }


def _message(metadata: dict | None) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id="00000000-0000-0000-0000-000000000001",
        metadata_json=json.dumps(metadata) if metadata is not None else None,
    )


# --- rebuilding the call the reviewer approved ----------------------------


def test_the_gated_assistant_message_is_rebuilt_from_the_proposals():
    """The paused turn could not persist it, so the proposals are the only
    record of what the model asked to do. Without rebuilding it the tool results
    have no ``tool_use`` blocks to pair with, which is the orphaning strict-mode
    providers reject.
    """
    messages: list = []
    resume.seed_gated_assistant_message(
        messages, [_proposal("call_1"), _proposal("call_2", "create_template")]
    )

    assert len(messages) == 1
    seeded = messages[0]
    assert seeded.role == "assistant"
    assert [c.id for c in seeded.tool_calls] == ["call_1", "call_2"]
    assert [c.name for c in seeded.tool_calls] == ["create_agent", "create_template"]
    assert seeded.tool_calls[0].arguments == {"name": "triage"}
    assert seeded.content == "", (
        "replaying the prose the user saw would tell the model, in its own "
        "voice, that it had already given up"
    )


def test_the_rebuilt_message_preserves_proposal_order():
    """``_apply_approval_decisions`` appends one result per call in proposal
    order, and the pairing is positional in every provider format."""
    messages: list = []
    ids = [f"call_{i}" for i in range(5)]
    resume.seed_gated_assistant_message(messages, [_proposal(i) for i in ids])

    assert [c.id for c in messages[0].tool_calls] == ids


def test_a_proposal_set_with_no_call_ids_seeds_nothing():
    """Rather than an assistant message with zero tool calls, which would be a
    turn the model has to interpret as having said nothing."""
    messages: list = []
    resume.seed_gated_assistant_message(messages, [{"tool_name": "create_agent"}])

    assert messages == []


# --- which verdicts count -------------------------------------------------


def test_only_recognised_approvals_bypass_the_gate():
    """A verdict the transport let through — a typo, a stale client's spelling —
    must not become consent on the way past. The allow-list is the same one the
    applier uses.
    """
    proposals = [_proposal("a"), _proposal("b"), _proposal("c"), _proposal("d")]
    decisions = {
        "a": {"decision": "approve"},
        "b": {"decision": "approve_always"},
        "c": {"decision": "reject"},
        "d": {"decision": "aprove"},  # typo: must not approve
    }

    assert resume.approved_ids(proposals, decisions) == {"a", "b"}


def test_a_partial_decision_set_is_refused_and_names_what_is_missing():
    """Part-applying would leave the assistant message with unmatched
    ``tool_use`` blocks — the exact orphaning the batch pre-screen prevents."""
    proposals = [_proposal("a"), _proposal("b"), _proposal("c")]

    with pytest.raises(resume.PartialDecisionSetError) as caught:
        resume.require_complete(proposals, {"a": {"decision": "approve"}})

    assert set(caught.value.missing) == {"b", "c"}


def test_a_complete_decision_set_passes():
    proposals = [_proposal("a"), _proposal("b")]
    resume.require_complete(
        proposals, {"a": {"decision": "approve"}, "b": {"decision": "reject"}}
    )


def test_decisions_are_indexed_by_call_id_and_junk_is_dropped():
    normalized = resume.normalize_decisions(
        [
            {"tool_call_id": "a", "decision": "approve"},
            {"call_id": "b", "decision": "reject"},
            {"decision": "approve"},  # no id — unusable
            "not a dict",
            None,
        ]
    )

    assert set(normalized) == {"a", "b"}


# --- what counts as pending ----------------------------------------------


def test_unreadable_metadata_reads_as_nothing_pending():
    """A corrupted row must not 500 every send to that conversation. The
    fail-closed reading of a proposal we cannot parse is that nothing is
    pending, so nothing runs."""
    message = types.SimpleNamespace(id="x", metadata_json="{not json")

    assert resume._load_metadata(message) == {}


def test_an_answered_set_is_not_pending_again():
    """A duplicate submission must find nothing to apply, or an approved call
    runs twice."""
    message = _message({"pending_approvals": [_proposal("a")], "approvals_retired": "answered"})

    metadata = resume._load_metadata(message)
    assert metadata.get(resume.RETIRED_KEY) == "answered"


def test_marking_answered_keeps_the_record_but_drops_the_pending_set():
    """Marked rather than deleted: the message stays as the record of what was
    asked, and nothing is answerable twice."""
    message = _message({"pending_approvals": [_proposal("a")], "other": "kept"})

    resume.mark_answered(message)

    stored = json.loads(message.metadata_json)
    assert resume.PENDING_KEY not in stored
    assert stored[resume.RETIRED_KEY] == "answered"
    assert stored["other"] == "kept", "unrelated metadata must survive"


# --- FR-002c4: no clock, and the record survives -------------------------


def test_nothing_in_the_resume_path_expires_a_proposal_on_a_clock():
    """FR-002c4, and FR-015e before it: the wait is on a person. A timeout here
    would silently discard a decision somebody was in the middle of making.
    """
    import inspect
    import re

    source = inspect.getsource(resume)
    # Word boundaries, and code lines only: "settled" in a docstring contains
    # "ttl", which is how the first version of this test failed.
    code = "\n".join(
        line.split("#", 1)[0] for line in source.splitlines() if line.strip()
    )
    for forbidden in (r"timedelta", r"expires_at", r"time\.time", r"\bttl\b", r"asyncio\.sleep"):
        assert not re.search(forbidden, code, re.IGNORECASE), (
            f"{forbidden} suggests a clock, and a proposal is bounded by the "
            "conversation moving on rather than by elapsed time"
        )


def test_retiring_keeps_the_proposals_so_the_surface_can_show_them_expired():
    """FR-002c4 requires the retired proposal to stay visible marked expired. If
    retirement deleted the proposals there would be nothing left to render, and
    a gated call that never ran would vanish from the conversation.
    """
    import inspect

    source = inspect.getsource(resume.retire_pending)
    assert "RETIRED_KEY" in source
    assert "pop(" not in source and "del " not in source, (
        "retirement adds a marker; deleting the proposals would leave the "
        "expired state with nothing to show"
    )


# --- the wiring, on both chat services -----------------------------------


@pytest.mark.parametrize("service", ["agent_chat", "general_chat"])
def test_both_chat_services_accept_and_apply_a_resume(service: str):
    """``general_chat`` is a parallel implementation of ``agent_chat``, and an earlier
    bug happened because a capability was wired into one sibling only. Assert both,
    or the next omission is invisible again.
    """
    import importlib
    import inspect

    module = importlib.import_module(f"app.services.{service}")
    entry = getattr(module, f"run_{service}")
    params = inspect.signature(entry).parameters
    assert "resume_proposals" in params
    assert "resume_decisions" in params

    source = inspect.getsource(entry)
    assert "seed_gated_assistant_message(" in source, (
        "without the rebuilt assistant message the applied tool results have "
        "no tool_use blocks to pair with"
    )
    assert "_apply_approval_decisions(" in source, (
        "the decision applier is shared with the run path on purpose; a second "
        "copy here is what Principle VIII forbids"
    )


@pytest.mark.parametrize("service", ["agent_chat", "general_chat"])
def test_a_resume_adds_no_empty_user_message(service: str):
    """The turn being continued already has its user message. Appending an empty
    one both misrepresents the conversation and trips the providers that reject
    an empty content block.
    """
    import importlib
    import inspect

    module = importlib.import_module(f"app.services.{service}")
    source = inspect.getsource(getattr(module, f"run_{service}"))

    assert "if user_message or not resume_proposals:" in source


def test_the_rest_endpoint_resumes_refuses_and_retires():
    """All three behaviours live in one endpoint, and each has a different
    failure mode if dropped: no resume and the proposals are unanswerable, no
    refusal and a stale decision runs a call twice, no retirement and a
    day-old proposal stays live after the conversation moved on.
    """
    import inspect

    from app.api.v1 import chat

    source = inspect.getsource(chat.send_message)
    assert "load_pending(" in source
    assert "require_complete(" in source
    assert "retire_for_new_turn(" in source, (
        "retirement goes through the shared helper, which carries the "
        "best-effort guard so no caller can omit it"
    )
    assert "status_code=409" in source, (
        "a decision for a settled proposal must be refused, not applied"
    )
    assert "status_code=422" in source, (
        "a partial decision set is a client bug worth naming precisely"
    )
    assert "mark_answered_by_id(" in source, (
        "without this the same decision set can be submitted twice and the "
        "approved call runs twice"
    )


def test_the_rest_request_carries_decisions():
    """The field is what makes a resume expressible at all on this surface."""
    assert "decisions" in __import__(
        "app.api.v1.chat", fromlist=["SendMessageRequest"]
    ).SendMessageRequest.model_fields


def test_an_approved_call_does_not_gate_again_on_the_resumed_turn():
    """The gate is installed on the resumed turn too. Without the approved ids
    it would stop at the very call that was just signed off, and the user would
    be asked the same question forever.
    """
    import inspect

    from app.api.v1 import chat
    from app.services import general_chat

    assert "approved_call_ids=" in inspect.getsource(chat.send_message)
    assert "approved_call_ids=" in inspect.getsource(general_chat.run_general_chat)


# --- Phase 19: retirement reaches every surface, refusals leave a trace ------


def test_retirement_clears_both_stores_not_just_the_message():
    """A pending set lives in two places — the assistant message (written by a
    turn that ended) and Redis (written by a live-turn surface, and read back by
    the conversation endpoint). Retiring only the message left the socket's
    prompt answerable: abandon it, type something else, reload, and it came back
    as live because the conversation response reads Redis.
    """
    import inspect

    source = inspect.getsource(resume.retire_pending)
    assert "get_pending_approvals" in source, (
        "the socket's proposals live only in Redis; without reading them there "
        "is nothing to carry onto the message and nothing to render as expired"
    )
    assert "clear_pending_approvals" in source, (
        "leaving the Redis key set means the next conversation read serves a "
        "retired proposal as though it were still answerable"
    )


def test_retirement_survives_a_redis_outage():
    """Retirement runs on the send path. A Redis failure must not stop a user
    sending their next message."""
    import inspect

    source = inspect.getsource(resume.retire_pending)
    assert source.count("except Exception") >= 2, (
        "both the read and the clear have to be non-fatal"
    )


def test_every_send_path_retires_through_the_shared_helper():
    """Three surfaces can carry a pending approval: REST, the web chat's
    streamed send and the platform stream. Assert the helper *and* all three
    callers: a fourth surface arrives with the chat channels, and it
    must not be able to skip this quietly.
    """
    import inspect

    from app.api.v1 import chat, platform

    chat_source = inspect.getsource(chat)
    assert chat_source.count("retire_for_new_turn(") == 2, (
        "REST and the web chat's streamed send must both retire"
    )
    assert 'surface="rest"' in chat_source and 'surface="web_stream"' in chat_source

    platform_source = inspect.getsource(platform.send_platform_chat_message)
    assert "retire_for_new_turn(" in platform_source, (
        "the platform stream holds its proposals in the same Redis key, so "
        "without this it serves an abandoned prompt as answerable"
    )


def test_the_shared_helpers_are_the_only_place_the_rules_live():
    """Both rules were duplicated per call site and each duplication missed a
    different surface. The guards belong in the helpers, so a caller cannot get
    them wrong by omission.
    """
    import inspect

    assert "except Exception" in inspect.getsource(resume.retire_for_new_turn), (
        "retirement is housekeeping: a failure must not cost the user the "
        "message they just sent, and that guard belongs here, not at each call"
    )
    assert "except Exception" in inspect.getsource(resume.record_refused_decision), (
        "an audit write that failed must not turn the 404/409/422 it describes "
        "into a 500"
    )


def test_every_refused_decision_is_recorded():
    """FR-008a requires a refusal be recorded, "never silently ignored", and
    SC-017 requires it be attributable. Ownership was always enforced — the
    conversation SELECT is scoped to the user — but a refused attempt left
    nothing behind at all.
    """
    import inspect

    from app.api.v1 import chat, platform

    chat_source = inspect.getsource(chat.send_message)
    assert chat_source.count("record_refused_decision(") == 3, (
        "three refusal paths on this surface: another user's conversation, a "
        "settled proposal set, and a partial one"
    )
    assert "not_the_paused_user" in chat_source
    assert "no_pending_proposal" in chat_source
    assert "partial_decision_set" in chat_source

    # The web chat and the platform stream answer through one helper, which
    # records both of their refusals.
    from app.api.v1 import chat_web_stream

    for route in (chat_web_stream.approve_web_chat_tools, platform.approve_platform_chat_tools):
        assert "apply_approval_decisions(" in inspect.getsource(route)
    platform_source = inspect.getsource(platform.approval_conversation) + inspect.getsource(
        platform.apply_approval_decisions
    )
    assert platform_source.count("record_refused_decision(") == 2, (
        "a decision is refused on ownership and on there being no turn "
        "awaiting approval; a malformed id is neither and must not be recorded "
        "as one"
    )
    assert 'reason="not_the_paused_user"' in platform_source
    assert 'reason="no_pending_proposal"' in platform_source


def test_a_malformed_conversation_id_is_not_recorded_as_an_ownership_refusal():
    """A false entry in a security trail is worse than a missing one: there is
    no conversation to own, so "belonging to another user" would be untrue."""
    import inspect

    from app.api.v1 import platform

    source = inspect.getsource(platform.approval_conversation)
    malformed = source.split("if conv_uuid is None:")[1].split("query = ")[0]
    assert "record_refused_decision" not in malformed


# --- FR-017b: an impersonated grant names both identities ------------------


def test_every_surface_forwards_the_impersonating_administrator():
    """A standing approval granted while impersonating must be attributed to
    both the impersonated user and the administrator who acted (FR-017b), and
    only the call site knows which administrator that is.

    REST was the surface that did not forward it, and the omission was harmless
    until the resume path made "approve always" reachable there — at which point
    an administrator's consent started being recorded as the impersonated user's
    own. Asserted per surface rather than per module: this is exactly the shape
    of gap that a count over the whole file hides.
    """
    import inspect

    from app.api.v1 import chat, platform

    rest = inspect.getsource(chat.send_message)
    assert rest.count("impersonated_by=") == 2, (
        "both REST branches grant standing approvals now — the agent and the "
        "general assistant"
    )

    socket = inspect.getsource(chat.run_chat_connection)
    assert "impersonated_by=_turn_impersonated_by" in socket

    stream = inspect.getsource(platform.send_platform_chat_message)
    assert "impersonated_by=" in stream


def test_the_claim_is_read_from_the_token_not_invented():
    """``deps.py`` puts the claim on the request's user; a call site that
    computed it some other way would be guessing at who is accountable."""
    import inspect

    from app.api.v1 import chat

    rest = inspect.getsource(chat.send_message)
    assert 'getattr(user, "impersonated_by", None)' in rest


# --- what /code-review found: the shape the contract documents must work -----


def test_the_documented_resume_shape_validates():
    """The contract says decisions with no content. The schema required content,
    so every client following the contract would have been refused with a 422
    before a line of resume logic ran — the whole path was unreachable as
    documented.
    """
    from app.api.v1.chat import SendMessageRequest

    request = SendMessageRequest(
        decisions=[{"tool_call_id": "a", "decision": "approve"}]
    )
    assert request.content is None


def test_a_plain_send_still_requires_content():
    """Making content optional must not let an empty message through."""
    import pytest as _pytest
    from pydantic import ValidationError

    from app.api.v1.chat import SendMessageRequest

    with _pytest.raises(ValidationError):
        SendMessageRequest()


def test_text_and_decisions_together_are_refused():
    """A resume continues a turn that already has its user message, so text
    arriving with decisions has nowhere to be stored. It used to reach the model
    and vanish: absent from the transcript, absent from the next turn's history,
    with the response echoing the *previous* user message.
    """
    import pytest as _pytest
    from pydantic import ValidationError

    from app.api.v1.chat import SendMessageRequest

    with _pytest.raises(ValidationError):
        SendMessageRequest(
            content="and also do this",
            decisions=[{"tool_call_id": "a", "decision": "approve"}],
        )


def test_the_endpoint_sends_the_normalized_content_not_the_raw_body():
    """``message_content`` blanks the text on a resume, but the services were
    handed ``body.content`` directly, so the blanking did nothing."""
    import inspect

    from app.api.v1 import chat

    source = inspect.getsource(chat.send_message)
    marker = 'message_content = "" if _resume_proposals else (body.content or "")'
    assert marker in source
    after = source.split(marker, 1)[1]
    assert "body.content" not in after, (
        "every use after the normalization must read the normalized value; the "
        "services were handed the raw body, so blanking it on a resume did "
        "nothing"
    )


def test_a_resume_refuses_to_fold_and_refuses_files():
    """Folding hands a running turn the *text*; a resume has none, so its
    decisions were dropped while the client was told 202. Files have no message
    to attach to and were being re-parented onto the previous turn's."""
    import inspect

    from app.api.v1 import chat

    source = inspect.getsource(chat.send_message)
    assert "_rest_turn_token is None and _resume_proposals" in source, (
        "a resume must refuse rather than fold"
    )

    # The file guard lives on the schema, so a malformed resume is refused
    # before any handler logic runs.
    import pytest as _pytest
    from pydantic import ValidationError

    from app.api.v1.chat import SendMessageRequest

    with _pytest.raises(ValidationError):
        SendMessageRequest(
            decisions=[{"tool_call_id": "a", "decision": "approve"}],
            file_ids=["00000000-0000-0000-0000-000000000001"],
        )


def test_retirement_happens_only_once_the_turn_is_ours():
    """Retiring before the fold check wiped the pending set of somebody else's
    in-flight turn, so the prompt vanished from a reload while that turn sat
    waiting for it."""
    import inspect

    from app.api.v1 import chat

    source = inspect.getsource(chat.send_message)
    fold_at = source.index("_rest_fold_response(")
    # Matched on the call name alone. The first version pinned the whole call
    # including its arguments and broke the moment ruff reflowed it onto three
    # lines — a test that fails on formatting is measuring the wrong thing.
    retire_at = source.index("retire_for_new_turn(")
    assert retire_at > fold_at, (
        "retirement must come after the fold check, not before it"
    )


def test_the_platform_stream_does_not_retire_a_turn_still_in_flight():
    """The platform stream takes no turn lock, so it has no fold to wait for.

    A browser turn paused on the same conversation holds the lock and keeps its
    prompt in the Redis key a retirement clears: retiring regardless wiped the
    only copy of that prompt while the turn went on waiting for it.
    """
    import inspect

    from app.api.v1 import platform

    source = inspect.getsource(platform.send_platform_chat_message)
    guard_at = source.index("is_turn_active(")
    retire_at = source.index("retire_for_new_turn(db")
    assert guard_at < retire_at
    assert "if not _turn_in_flight:" in source[guard_at:retire_at]


def test_the_answered_mark_survives_a_rollback_inside_the_turn():
    """The gate builder rolls the session back on a failed platform read and
    swallows the error, expiring every ORM instance. Holding the message across
    the turn meant a 500 *after* the approved tool had run, with the set still
    answerable — so a retry ran it twice."""
    import inspect

    from app.api.v1 import chat

    source = inspect.getsource(chat.send_message)
    assert "_resume_message_id" in source
    assert "mark_answered_by_id(" in source, (
        "the message must be re-fetched after the turn, not carried through it"
    )


def test_an_orphaned_redis_set_does_not_relabel_a_settled_message():
    """When the paused turn never persisted a message, the latest assistant
    message belongs to an *earlier* turn. Stamping the Redis proposals there
    rendered "needed your approval and was never run" under a message whose
    approval was given and whose tool ran.
    """
    import inspect

    source = inspect.getsource(resume.retire_pending)
    assert "already_settled" in source
    assert "orphaned pending approval" in source


def test_both_pending_stores_are_merged_not_dropped():
    """Both stores can hold a set. Dropping the second retired it with no record
    anywhere, so nothing could render it expired."""
    import inspect

    source = inspect.getsource(resume.retire_pending)
    assert "merged" in source and "merged.update(" in source


def test_one_filter_decides_what_counts_as_a_proposal():
    """``_apply_approval_decisions`` appends one tool message per proposal it is
    given, including an id-less one — producing a ``tool_result`` with no
    matching ``tool_use``, the orphaning the completeness rule prevents. Only
    visible with one bad entry among good ones, so the filter has to be shared.
    """
    assert resume.usable(
        [{"tool_call_id": "a"}, {"tool_name": "x"}, "junk", None]
    ) == [{"tool_call_id": "a"}]

    import inspect

    assert "usable(" in inspect.getsource(resume.load_pending)
    assert "usable(" in inspect.getsource(resume.retire_pending)


def test_the_platform_ownership_refusal_needs_a_real_conversation():
    """Without an existence check any authenticated caller could write arbitrary
    "belonging to another user" rows into the security trail by posting
    decisions for ids that do not exist."""
    import inspect

    from app.api.v1 import platform

    source = inspect.getsource(platform.approval_conversation)
    owner_branch = source.split("scalar_one_or_none() is None:")[1]
    assert "_exists" in owner_branch, (
        "a trail full of invented refusals is worse than one missing an entry"
    )


# --- A documented resume a client cannot reach is not a resume -------------


def test_a_rest_client_is_given_the_call_ids_it_must_name():
    """``decisions`` names ``tool_call_id``s, and REST had no way to learn one.

    The proposals were written to the assistant message's metadata and nowhere
    else: ``SendMessageResponse`` had no field for them, ``MessageResponse``
    exposes only ``expired_approvals``, and the Redis copy the conversation
    endpoint reads is written by the socket and embed paths only. So a REST
    caller got "I need approval before running: X" as prose, and the resume
    documented in ``contracts/chat-approval-api.md`` could not be performed.
    """
    import inspect

    from app.api.v1.chat import SendMessageResponse, send_message

    assert "pending_approvals" in SendMessageResponse.model_fields

    # Same list into both sinks, so the copy the client is shown can never
    # drift from the copy ``load_pending`` resumes from. The client's copy shows
    # a secret variable value as the display text; its call ids are the same.
    source = inspect.getsource(send_message)
    assert 'pending_approvals"] = _rest_approvals_out' in source
    assert "pending_approvals=redact_known_secrets_for_display(_rest_approvals_out)" in source


def test_a_rejected_call_never_reaches_the_gate_bypass():
    """``approved_call_ids`` is not a hint — ``build_approval_gate`` returns
    before ``evaluate_gate`` for anything in it. The General Assistant built the
    set from every proposal regardless of verdict, so a call the reviewer
    *rejected* sat on a bypass list. Inert only because nothing re-presents that
    id; one refactor away from mattering.
    """
    from app.services.chat_approval_resume import approved_ids

    proposals = [{"tool_call_id": "yes"}, {"tool_call_id": "no"}]
    decisions = {
        "yes": {"decision": "approve"},
        "no": {"decision": "reject", "rejection_reason": "not this one"},
    }

    assert approved_ids(proposals, decisions) == {"yes"}


def test_no_surface_bypasses_the_gate_on_an_unfiltered_proposal_set():
    """Both places that BUILD the bypass set must resolve it through the
    allow-list. A set comprehension over the raw proposals is the shape that
    regressed in ``run_general_chat``.

    ``run_agent_chat`` is deliberately not in this list: it forwards a set its
    caller resolved, and is asserted below not to build one of its own.
    """
    import inspect

    from app.api.v1.chat import send_message
    from app.services.general_chat import run_general_chat

    for entry in (send_message, run_general_chat):
        source = inspect.getsource(entry)
        assert "approved_call_ids=" in source, entry.__name__
        assert "approved_ids(" in source, (
            f"{entry.__name__} builds approved_call_ids without the is_approval "
            "allow-list, so a rejected verdict becomes a gate bypass"
        )


def test_the_agent_path_only_forwards_the_set_it_was_given():
    """One resolution per resume. If this ever builds its own set it has to
    answer the allow-list question too, and the test above will not notice.
    """
    import inspect

    from app.services.agent_chat import run_agent_chat

    source = inspect.getsource(run_agent_chat)
    assert "approved_call_ids=approved_call_ids" in source
    assert "tool_call_id" not in source.split("approved_call_ids=approved_call_ids")[0][-400:]


def test_the_run_resume_bypass_uses_the_allow_list_too():
    """``run.approval_decisions`` is untyped JSON, and this set skips the gate
    outright. A negative test (`!= "reject"`) hands a verdict spelled
    "rejected" a free pass — the exact bug ``is_approval``'s docstring names.
    The chat paths were corrected first; the run path is the fourth call site.
    """
    import inspect

    from app.services import assignment_engine

    source = inspect.getsource(assignment_engine.execute_assignment_run)
    resume = source.split("_approved_ids = {")[1][:400]
    assert '!= "reject"' not in resume
    assert "_is_approval(" in resume


def test_the_general_assistant_can_be_resumed_over_rest_like_an_agent():
    """Both halves or neither. The GA had the resume half and no propose half:
    its paused turn unwound into prose and stored no proposals, so
    ``load_pending`` never returned any, a client could not name a
    ``tool_call_id``, and the whole callee-side implementation in
    ``general_chat`` was unreachable — a complete-looking resume with no way in.

    Deleting the caller arguments alone made that worse rather than better, so
    the propose half is wired instead: same shape as the agent branch, which
    also pairs ``no_approval_channel_message`` with the structured set.
    """
    import inspect

    from app.api.v1.chat import send_message

    source = inspect.getsource(send_message)
    ga_call = source.split("run_general_chat(")[1].split("\n            )")[0]
    assert "resume_proposals=_resume_proposals" in ga_call
    assert "resume_decisions=_resume_decisions" in ga_call

    # The propose half: without this the arguments above are unreachable again.
    ga_pause = source.split("except _GeneralChatPaused as _ga_hitl")[1][:1200]
    assert "_rest_approvals_out.extend(" in ga_pause
