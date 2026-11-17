"""Tests for the live per-iteration cockpit activity stream (frozen-cockpit fix).

The autonomous orchestrator cockpit's dimmed "thinking" window only ever rebuilt
its text from recorded NARRATION / DECISION / TOOL_ACTION timeline events, which
the LLM authors about once per ~80 iterations - so it sat frozen for minutes
across a long run. The fix streams a deterministic, per-iteration NARRATION line
from the worker via ``_post_autonomous_event`` (a FRESH per-call operator JWT),
independent of whether the LLM narrates.

These cover the deterministic caption table, the throttle / dedup / skip rules of
the callback, that a callback error is swallowed, and that the callback is wired
end to end from ``run_autonomous_cycle`` through ``run_agent_chat`` into
``run_agent_loop``.
"""

from __future__ import annotations

import asyncio
import inspect
import json

from app.services import autonomous_pentest as ap

# ── Deterministic caption table (pure) ────────────────────────────────────────


def test_caption_maps_known_tools_to_friendly_phrases():
    caption = ap._iteration_activity_caption
    assert caption(["manage_openaev_threat_arsenal"], False) == (
        "Authoring threat-arsenal payloads"
    )
    assert caption(["search_openaev_injector_contracts"], False) == (
        "Searching the arsenal for reusable actions"
    )
    assert caption(["get_openaev_injector_contract"], False) == (
        "Searching the arsenal for reusable actions"
    )
    assert caption(["resolve_openaev_capabilities"], False) == (
        "Resolving installed capabilities"
    )
    assert caption(["author_openaev_attack_path_step"], False) == (
        "Authoring the attack path"
    )
    assert caption(["evaluate_openaev_attack_path"], False) == (
        "Evaluating the attack path"
    )
    assert caption(["read_openaev_attack_path_state"], False) == (
        "Reviewing scope and current state"
    )
    assert caption(["web_fetch"], False) == "Researching a technique"
    assert caption(["search_memory"], False) == "Recalling prior tradecraft"
    assert caption(["set_shared_state"], False) == "Updating the working plan"
    assert caption(["consume_openaev_directives"], False) == (
        "Checking for operator steering"
    )
    assert caption(["load_tools"], False) == "Loading its toolset"


def test_caption_picks_the_most_salient_tool_when_several_ran():
    # Authoring / evaluating beat reads + bookkeeping, whatever order they ran in.
    caption = ap._iteration_activity_caption
    assert caption(
        ["read_openaev_attack_path_state", "author_openaev_attack_path_step"], False
    ) == "Authoring the attack path"
    assert caption(
        ["get_shared_state", "consume_openaev_directives", "evaluate_openaev_attack_path"],
        False,
    ) == "Evaluating the attack path"
    assert caption(
        ["set_shared_state", "search_openaev_injector_contracts"], False
    ) == "Searching the arsenal for reusable actions"


def test_caption_skips_final_answer_and_record_event_only_iterations():
    # A final-answer turn has no tools; a record_openaev_event-only iteration is
    # its own timeline row already - both must produce NO live line.
    caption = ap._iteration_activity_caption
    assert caption([], False) is None
    assert caption(["record_openaev_event"], False) is None


def test_caption_still_fires_when_record_event_runs_alongside_real_tool():
    # record_openaev_event is dropped from salience, but the OTHER tool still
    # drives a caption (the iteration did real work too).
    assert ap._iteration_activity_caption(
        ["record_openaev_event", "web_fetch"], False
    ) == "Researching a technique"


def test_caption_had_error_uses_retry_adjust_flavour():
    # An errored iteration reads as "adjusting", not a stale authoring line -
    # regardless of which tool ran. The wording stays generic ("a tool error")
    # because had_error is true for ANY failed tool result, not only a rejected
    # write.
    caption = ap._iteration_activity_caption
    assert caption(["author_openaev_attack_path_step"], True) == (
        "Adjusting after a tool error"
    )
    assert caption(["manage_openaev_threat_arsenal"], True) == (
        "Adjusting after a tool error"
    )
    # But a record-only / empty iteration is still skipped even when flagged.
    assert caption(["record_openaev_event"], True) is None
    assert caption([], True) is None


def test_caption_falls_back_to_humanized_unknown_tool():
    # An unmapped tool is humanized, never surfaced as a raw snake_case id.
    assert ap._iteration_activity_caption(["frobnicate_the_widget"], False) == (
        "Frobnicate the widget"
    )
    assert ap._humanize_tool_name("get_openaev_run_scope") == "Get openaev run scope"


def test_caption_maps_extended_orchestrator_tools():
    # Extended caption set: real orchestrator tools that would otherwise
    # fall back to a humanized snake_case id ("Search openaev asset groups",
    # "Consult agent", ...) now get a curated, operator-legible caption.
    caption = ap._iteration_activity_caption
    assert caption(["promote_openaev_finding_to_asset"], False) == (
        "Promoting a finding to an asset"
    )
    assert caption(["set_openaev_run_scope"], False) == "Updating the run scope"
    assert caption(["update_openaev_run_status"], False) == "Updating the run status"
    assert caption(["consult_agent"], False) == "Consulting a specialist agent"
    assert caption(["transfer_to_agent"], False) == (
        "Handing over to a specialist agent"
    )
    assert caption(["spawn_background_task"], False) == "Delegating a background task"
    assert caption(["query_openaev_attack_path"], False) == "Reading the attack path"
    assert caption(["search_openaev_findings"], False) == "Reviewing findings"
    assert caption(["search_openaev_assets"], False) == "Searching assets in scope"
    assert caption(["search_openaev_asset_groups"], False) == (
        "Searching asset groups in scope"
    )
    assert caption(["search_openaev_teams"], False) == "Searching teams in scope"
    assert caption(["search_openaev_players"], False) == "Searching players in scope"
    assert caption(["search_knowledge"], False) == "Searching knowledge"
    assert caption(["store_knowledge"], False) == "Storing knowledge"
    assert caption(["store_memory"], False) == "Saving learned tradecraft"
    # A new authoring-tier tool still outranks a new read when both ran.
    assert caption(
        ["search_openaev_findings", "promote_openaev_finding_to_asset"], False
    ) == "Promoting a finding to an asset"


def test_every_priority_tool_has_a_caption():
    # _iteration_activity_caption indexes _ACTIVITY_TOOL_CAPTIONS by each name in
    # _ACTIVITY_TOOL_PRIORITY, so a priority entry with no caption would KeyError
    # at runtime. Guard the two tables against drift.
    missing = [
        n for n in ap._ACTIVITY_TOOL_PRIORITY if n not in ap._ACTIVITY_TOOL_CAPTIONS
    ]
    assert missing == [], f"priority tools missing a caption: {missing}"


# ── Callback behaviour: post shape, skip, throttle, dedup, swallow ────────────


def _capture_events(monkeypatch) -> list[dict]:
    events: list[dict] = []

    async def _capture(payload, body):
        events.append(body)

    monkeypatch.setattr(ap, "_post_autonomous_event", _capture)
    return events


async def _drain(callback) -> None:
    """Await any background post the callback dispatched.

    The callback posts in the background (never awaited inline) so a slow
    OpenAEV callback URL cannot stall the agent loop, so a test must drain the
    in-flight task before asserting the event was posted.
    """
    tasks = list(getattr(callback, "inflight_tasks", ()) or ())
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


_PAYLOAD = {
    "openaev_run_id": "run-1",
    "user_id": "user-1",
    "callback_base_url": "https://oaev.example",
}


async def test_callback_posts_a_live_narration_event(monkeypatch):
    events = _capture_events(monkeypatch)
    callback = ap._build_live_activity_callback(_PAYLOAD)

    await callback(7, ["author_openaev_attack_path_step"], False)
    await _drain(callback)

    assert len(events) == 1
    body = events[0]
    assert body["type"] == "NARRATION"
    assert body["title"] == "Authoring the attack path"
    assert body["content"] == "Authoring the attack path"
    data = json.loads(body["data"])
    # SHARED CONTRACT: data JSON carries live == true and the iteration number.
    assert data["live"] is True
    assert data["iteration"] == 7


async def test_callback_posts_nothing_for_record_event_only_iteration(monkeypatch):
    events = _capture_events(monkeypatch)
    callback = ap._build_live_activity_callback(_PAYLOAD)

    await callback(3, ["record_openaev_event"], False)
    await _drain(callback)

    assert events == []


async def test_callback_dedupes_unchanged_caption_within_the_window(monkeypatch):
    events = _capture_events(monkeypatch)
    clock = {"t": 1000.0}
    monkeypatch.setattr(ap, "_monotonic", lambda: clock["t"])
    callback = ap._build_live_activity_callback(_PAYLOAD)

    await callback(1, ["author_openaev_attack_path_step"], False)
    await _drain(callback)
    # Same caption, only 2s later (< the 5s window) -> no second post.
    clock["t"] += 2.0
    await callback(2, ["update_openaev_attack_path_step"], False)
    await _drain(callback)

    assert len(events) == 1


async def test_callback_reposts_unchanged_caption_after_the_interval(monkeypatch):
    events = _capture_events(monkeypatch)
    clock = {"t": 1000.0}
    monkeypatch.setattr(ap, "_monotonic", lambda: clock["t"])
    callback = ap._build_live_activity_callback(_PAYLOAD)

    await callback(1, ["author_openaev_attack_path_step"], False)
    # Drain so the first post is no longer in flight (else the second is
    # load-shed-dropped rather than reposted).
    await _drain(callback)
    # Same caption, but past the throttle window -> the "still working" repost.
    clock["t"] += ap.LIVE_ACTIVITY_MIN_INTERVAL_SECONDS + 0.5
    await callback(2, ["author_openaev_attack_path_step"], False)
    await _drain(callback)

    assert len(events) == 2
    assert [json.loads(e["data"])["iteration"] for e in events] == [1, 2]


async def test_callback_posts_immediately_on_a_changed_caption(monkeypatch):
    events = _capture_events(monkeypatch)
    clock = {"t": 1000.0}
    monkeypatch.setattr(ap, "_monotonic", lambda: clock["t"])
    callback = ap._build_live_activity_callback(_PAYLOAD)

    await callback(1, ["author_openaev_attack_path_step"], False)
    # Drain so the first post is not still in flight (load-shed drops overlap).
    await _drain(callback)
    # A different caption within the window still posts right away.
    clock["t"] += 0.2
    await callback(2, ["web_fetch"], False)
    await _drain(callback)

    assert len(events) == 2
    assert events[0]["title"] == "Authoring the attack path"
    assert events[1]["title"] == "Researching a technique"


async def test_callback_load_sheds_while_a_post_is_in_flight(monkeypatch):
    # A slow/hung post must not pile up tasks or block: while one is in flight,
    # a changed caption is dropped (not queued) and the throttle state is left
    # untouched so it is retried once the in-flight post drains.
    events = _capture_events(monkeypatch)
    clock = {"t": 1000.0}
    monkeypatch.setattr(ap, "_monotonic", lambda: clock["t"])
    callback = ap._build_live_activity_callback(_PAYLOAD)

    await callback(1, ["author_openaev_attack_path_step"], False)
    # A different caption, but the first post is still in flight -> dropped.
    clock["t"] += 0.2
    await callback(2, ["web_fetch"], False)
    assert len(callback.inflight_tasks) == 1

    await _drain(callback)
    assert len(events) == 1
    assert events[0]["title"] == "Authoring the attack path"

    # Once drained, the dropped caption is retried on the next iteration.
    await callback(3, ["web_fetch"], False)
    await _drain(callback)
    assert [e["title"] for e in events] == [
        "Authoring the attack path",
        "Researching a technique",
    ]


async def test_callback_swallows_a_post_failure(monkeypatch):
    # A callback error must NEVER break the agent loop that drives it - and a
    # background post failure must not surface as an unretrieved-task error.
    async def _boom(payload, body):
        raise RuntimeError("network down")

    monkeypatch.setattr(ap, "_post_autonomous_event", _boom)
    callback = ap._build_live_activity_callback(_PAYLOAD)

    # Must not raise, either at dispatch or when the background post is reaped.
    await callback(1, ["author_openaev_attack_path_step"], False)
    await _drain(callback)


# ── End-to-end wiring: cycle -> run_agent_chat -> run_agent_loop ──────────────


def test_run_agent_loop_accepts_and_invokes_the_activity_callback():
    from app.tools.agent_loop import run_agent_loop

    params = inspect.signature(run_agent_loop).parameters
    assert "on_iteration_activity" in params
    src = inspect.getsource(run_agent_loop)
    # Invoked (awaited) once per iteration, guarded by try/except.
    assert "await on_iteration_activity(" in src
    # had_error is derived from a MONOTONIC error tally, not
    # ``sum(tool_error_counts.values())``: the latter tracks consecutive-failure
    # streaks and resets on success, so a cleared streak plus a fresh error nets
    # to zero and would hide the error from the activity signal.
    assert "_iter_had_error = tool_errors_observed > _activity_err_seen" in src


def test_run_agent_chat_forwards_the_activity_callback_to_the_loop():
    from app.services.agent_chat import run_agent_chat

    params = inspect.signature(run_agent_chat).parameters
    assert "on_iteration_activity" in params
    src = inspect.getsource(run_agent_chat)
    assert "on_iteration_activity=on_iteration_activity" in src


async def test_cycle_wires_a_working_live_activity_callback_into_agent_chat(monkeypatch):
    # The cycle must hand run_agent_chat a real, working on_iteration_activity
    # callback (not None): capture it, then invoke it and assert it streams a
    # NARRATION live event through the genuine _build_live_activity_callback.
    import app.core.database as database
    import app.services.agent_chat as agent_chat
    import app.services.shared_state as shared_state

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(shared_state, "delete_state", _noop)
    monkeypatch.setattr(shared_state, "get_state", _noop)

    class _FakeSession:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(database, "async_session", lambda: _FakeSession())
    monkeypatch.setattr(ap, "_read_openaev_run_status", _noop)
    monkeypatch.setattr(ap, "_read_openaev_run_scope", _noop)

    async def _fake_load_agent(db, slug):
        return object()

    monkeypatch.setattr(ap, "_load_orchestrator_agent", _fake_load_agent)
    monkeypatch.setattr(ap, "_apply_per_run_agent_policy", lambda db, agent, payload: None)
    monkeypatch.setattr(
        ap,
        "_build_cycle_prompt",
        lambda payload, cycle, live_scope=None, no_progress_streak=0: "prompt",
    )
    # Keep the immediate liveness prime + heartbeat off the network.
    monkeypatch.setattr(ap, "_post_openaev_run_heartbeat", _noop)
    monkeypatch.setattr(ap, "_post_openaev_run_status", _noop)

    async def _fake_heartbeat_loop(*args, **kwargs):
        return None

    monkeypatch.setattr(ap, "_heartbeat_loop", _fake_heartbeat_loop)

    posted: list[dict] = []

    async def _capture_post(payload, body):
        posted.append(body)

    monkeypatch.setattr(ap, "_post_autonomous_event", _capture_post)

    captured: dict = {}

    async def _fake_run_agent_chat(*args, **kwargs):
        captured.update(kwargs)
        return ("final answer", None)

    monkeypatch.setattr(agent_chat, "run_agent_chat", _fake_run_agent_chat)

    await ap.run_autonomous_cycle(
        payload={
            "openaev_run_id": "run-live",
            "user_id": "user-1",
            "callback_base_url": "https://oaev.example",
        },
        cycle=1,
    )

    callback = captured.get("on_iteration_activity")
    assert callable(callback), "cycle must wire a live-activity callback, not None"

    # Drive the captured callback and assert it posts a live NARRATION - proving
    # the cycle wired the real factory end to end (not a no-op placeholder).
    posted.clear()
    await callback(4, ["evaluate_openaev_attack_path"], False)
    await _drain(callback)
    assert len(posted) == 1
    assert posted[0]["type"] == "NARRATION"
    assert posted[0]["title"] == "Evaluating the attack path"
    assert json.loads(posted[0]["data"]) == {"live": True, "iteration": 4}
