"""The lab's scripted stream (``lab:resuming``): the stub model replays a live ``turn_resuming`` frame.

lab.py is standard library only and starts the stores when imported, so its functions are
compiled one by one from its syntax tree. The contract tests pin the Redis keys, the channel and
the frame against the backend's own helpers: a rename on either side fails here, not in a review.
dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / ".xtm-one" / "lab" / "lab.py"


def _lab_namespace(*names: str, **globals_: object) -> dict:
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    body = [
        node
        for node in tree.body
        if (isinstance(node, ast.FunctionDef) and node.name in names)
        or (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in node.targets))
        or (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id in names)
    ]
    namespace: dict = {"json": json, **globals_}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(LAB), "exec"), namespace)  # noqa: S102 - the repo's own file
    return namespace


def test_an_ordinary_message_is_not_a_script():
    parse = _lab_namespace("parse_lab_script", "LAB_SCRIPT_MAX_HOLD_SECONDS")["parse_lab_script"]
    assert parse("hello") is None
    assert parse(None) is None
    assert parse("please lab:resuming") is None


def test_the_resuming_script_reads_its_seconds_its_counter_and_its_cause():
    parse = _lab_namespace("parse_lab_script", "LAB_SCRIPT_MAX_HOLD_SECONDS")["parse_lab_script"]
    assert parse("lab:resuming") == {
        "script": "resuming",
        "seconds": 90,
        "resume": 2,
        "of": 100,
        "cause": "stop",
        "cut": False,
    }
    assert parse("  LAB:resuming 75 3/20 recycle ") == {
        "script": "resuming",
        "seconds": 75,
        "resume": 3,
        "of": 20,
        "cause": "recycle",
        "cut": False,
    }
    # ``cut`` stores the rows of a real cut and resume; it is never read as the cause.
    assert parse("lab:resuming 20 1/100 stop cut") == {
        "script": "resuming",
        "seconds": 20,
        "resume": 1,
        "of": 100,
        "cause": "stop",
        "cut": True,
    }
    assert parse("lab:resuming CUT recycle")["cause"] == "recycle"
    # Held under the agent loop's 120 s stream idle watchdog, whatever is asked.
    assert parse("lab:resuming 600")["seconds"] == 110
    # A counter past its cap reads as the cap; a malformed one keeps the default.
    assert parse("lab:resuming 9/4")["resume"] == 4
    assert parse("lab:resuming 0/4")["of"] == 100


def test_the_hold_stays_under_the_stream_idle_watchdog():
    from app.tools import agent_loop

    hold = _lab_namespace("LAB_SCRIPT_MAX_HOLD_SECONDS")["LAB_SCRIPT_MAX_HOLD_SECONDS"]
    assert 60 <= hold < agent_loop._STREAM_IDLE_TIMEOUT_SECS


def test_the_last_user_message_is_read_from_text_and_parts():
    last = _lab_namespace("last_user_text")["last_user_text"]
    assert last({"messages": [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]}) == "a"
    assert (
        last({"messages": [{"role": "user", "content": [{"type": "text", "text": "lab:resuming"}]}]}) == "lab:resuming"
    )
    assert last({}) == ""


def test_the_frame_is_published_and_journaled_where_the_backend_reads_it():
    from app.core.utils import chat_processing_key
    from app.services import chat_turn_stream
    from app.services.cut_turn_resume import RESUMING_FRAME_TYPE

    conversation, token = "c0ffee", "turn-token-1"
    store = {chat_processing_key(conversation): token}
    calls: list[tuple] = []

    def redis_command(*args):
        calls.append(args)
        if args[0] == "KEYS":
            return [k for k in store if k.startswith(args[1].rstrip("*"))]
        if args[0] == "GET":
            return store.get(args[1])
        return 1

    namespace = _lab_namespace("announce_resuming", "_SCRIPTED_TURNS", redis_command=redis_command)
    script = {"script": "resuming", "seconds": 90, "resume": 3, "of": 20, "cause": "recycle"}
    assert namespace["announce_resuming"](script) == [conversation]

    state_key = chat_turn_stream.journal_keys(conversation, token)[0]
    hset = next(c for c in calls if c[0] == "HSET")
    assert hset[1:3] == (state_key, "resuming")
    journaled = chat_turn_stream._read_resuming(hset[3])
    assert journaled == {
        "type": RESUMING_FRAME_TYPE,
        "cause": "recycle",
        "resume": 3,
        "of": 20,
        "cut_at": journaled["cut_at"],
    }

    publish = next(c for c in calls if c[0] == "PUBLISH")
    assert publish[1] == chat_turn_stream.turn_channel(conversation)
    (frame,) = json.loads(publish[2])
    assert frame["type"] in chat_turn_stream.RELAYED_FRAME_TYPES
    assert frame["turn"] == token and isinstance(frame["seq"], int)
    assert set(chat_turn_stream.TRANSPORT_KEYS) <= set(frame)
    # A frame of the turn's own origin is never relayed back to it: the stub's origin is its own.
    assert frame["origin"] == "lab-stub-model"

    # A second model call of the same turn (a tool step, a retry) announces nothing again.
    calls.clear()
    assert namespace["announce_resuming"](script) == []
    assert not any(c[0] in ("HSET", "PUBLISH") for c in calls)


def test_a_cut_script_stores_its_interrupted_reply_before_the_frame():
    conversation, token = "c0ffee", "turn-token-2"
    store = {f"chat:processing:{conversation}": token}
    order: list[str] = []

    def redis_command(*args):
        if args[0] == "KEYS":
            return list(store)
        if args[0] == "GET":
            return store.get(args[1])
        order.append(args[0])
        return 1

    namespace = _lab_namespace("announce_resuming", "_SCRIPTED_TURNS", redis_command=redis_command)
    seen: list[tuple] = []

    def before_publish(conv, frame):
        order.append("store")
        seen.append((conv, frame["cut_at"]))

    script = {"script": "resuming", "seconds": 20, "resume": 1, "of": 100, "cause": "stop", "cut": True}
    assert namespace["announce_resuming"](script, before_publish) == [conversation]
    # The page reads the thread back on the frame: the row is stored first.
    assert order == ["store", "HSET", "PUBLISH"]
    assert seen[0][0] == conversation and seen[0][1]


def _seed_resumed_reply():
    import importlib.util

    spec = importlib.util.spec_from_file_location("seed_resumed_reply", LAB.parent / "seed_resumed_reply.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_stored_cut_and_resume_render_as_resumed_by_itself():
    """The rows ``lab:resuming ... cut`` stores are served as the platform's own cut and resume."""
    import datetime
    import uuid
    from types import SimpleNamespace

    from app.api.v1.chat import _build_message_response
    from app.services.cut_turn_resume import CutTurn, resumed_from_meta

    seed = _seed_resumed_reply()

    def row(meta: dict, content: str = "the answer"):
        return SimpleNamespace(
            id=uuid.uuid4(),
            conversation_id=uuid.uuid4(),
            role="assistant",
            content=content,
            channel="web",
            channel_message_id=None,
            sequence=2,
            created_at=datetime.datetime.now(datetime.UTC),
            metadata_json=json.dumps(meta),
            llm_trace_id=None,
        )

    cut = _build_message_response(row(seed.interrupted_meta("stop")))
    assert cut.interrupted is True
    assert [entry.name for entry in cut.tool_call_trace] == [seed.CUT_TOOL]
    assert cut.resumed_from is None

    meta = seed.resumed_meta("stop", 1, 100, "2026-11-07T12:00:00+00:00")
    answer = _build_message_response(row(meta))
    assert answer.interrupted is None
    assert answer.resumed_from is not None
    assert answer.resumed_from.model_dump() == {
        "cause": "stop",
        "resume": 1,
        "of": 100,
        "cut_at": "2026-11-07T12:00:00+00:00",
    }
    # The same keys as a real resume's reply.
    real = resumed_from_meta(
        CutTurn(
            conversation_id="c",
            user_id="u",
            user_message_id="m",
            turn_id=None,
            cause="stop",
            resumes=0,
            cut_at="2026-11-07T12:00:00+00:00",
        ),
        resumed_as=None,
    )
    assert set(meta["resumed_from"]) == set(real["resumed_from"])


# ── lab:wait and lab:cut-wait: the wait card and the resumed cut wait from the backend ──────────


def _parse():
    return _lab_namespace(
        "parse_lab_script", "LAB_SCRIPT_MAX_HOLD_SECONDS", "LAB_WAIT_DEFAULT_SECONDS", "LAB_WAIT_MAX_SECONDS"
    )["parse_lab_script"]


def test_the_wait_scripts_read_their_seconds_and_reason():
    parse = _parse()
    assert parse("lab:wait") == {"script": "wait", "seconds": 60, "reason": "Lab wait"}
    assert parse("lab:wait 60 CI on the PR") == {"script": "wait", "seconds": 60, "reason": "CI on the PR"}
    assert parse("LAB:wait 9000")["seconds"] == 300
    assert parse("lab:wait 0 x")["seconds"] == 1
    assert parse("lab:cut-wait 30") == {"script": "cut-wait", "seconds": 30}
    assert parse("lab:cut-wait") == {"script": "cut-wait", "seconds": 60}
    assert parse("lab:waiting") is None
    assert parse("please lab:wait") is None


def test_the_wait_call_is_a_real_wait_tool_call():
    from app.tools.wait_policy import WAIT_TOOL_NAME, parse_until_task_ids

    ns = _lab_namespace("wait_call_chunks", "LAB_WAIT_CALL_ID")
    watched = "0b7c1d0e-5f3a-4c55-9b0e-2a7f1c3d4e5f"
    first, last = ns["wait_call_chunks"]({"script": "wait", "seconds": 60, "reason": "CI on the PR"}, watched)
    (call,) = first["choices"][0]["delta"]["tool_calls"]
    assert call["function"]["name"] == WAIT_TOOL_NAME
    arguments = json.loads(call["function"]["arguments"])
    assert arguments == {"duration": "60", "reason": "CI on the PR", "until": watched}
    assert parse_until_task_ids(arguments["until"]) == [watched]
    assert last["choices"][0]["finish_reason"] == "tool_calls"
    # Nothing to watch: no until, the pause runs on its timer.
    first, _ = ns["wait_call_chunks"]({"script": "wait", "seconds": 5, "reason": "r"}, None)
    assert "until" not in json.loads(first["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"])


def test_only_the_call_answering_the_person_is_scripted():
    ns = _lab_namespace("last_role", "offered_tools")
    assert ns["last_role"]({"messages": [{"role": "user", "content": "lab:wait"}]}) == "user"
    assert ns["last_role"]({"messages": [{"role": "user"}, {"role": "assistant"}, {"role": "tool"}]}) == "tool"
    assert ns["last_role"]({}) == ""
    assert ns["last_role"]({"messages": [{"role": "system"}]}) == ""
    tools = {"tools": [{"type": "function", "function": {"name": "wait"}}, {"type": "function"}]}
    assert ns["offered_tools"](tools) == {"wait"}


#: The tail of the first model request of a real lab chat turn, as the stub model received it
#: (xtm-one #9300 retest, lab from main 932e3c0a): the agent loop appends its turn notes as system
#: messages after the person's message, and the request offers the ``wait`` tool.
_REAL_FIRST_CALL = {
    "stream": True,
    "messages": [
        {"role": "system", "content": "You are CTEM Assistant."},
        {"role": "user", "content": "lab:wait 60 CI on the PR"},
        {"role": "system", "content": "To use background task output: PREFER `collect_task_results` ..."},
        {"role": "system", "content": "[Waiting] After spawning background work, keep calling independent tools ..."},
        {"role": "system", "content": "\u2500\u2500 Available tool groups (loaded on demand) \u2500\u2500\n..."},
    ],
    "tools": [
        {"type": "function", "function": {"name": "browser"}},
        {"type": "function", "function": {"name": "wait"}},
    ],
}


def test_the_real_first_call_of_a_chat_turn_is_scripted():
    ns = _lab_namespace(
        "last_role",
        "offered_tools",
        "last_user_text",
        "parse_lab_script",
        "LAB_SCRIPT_MAX_HOLD_SECONDS",
        "LAB_WAIT_DEFAULT_SECONDS",
        "LAB_WAIT_MAX_SECONDS",
    )
    assert ns["parse_lab_script"](ns["last_user_text"](_REAL_FIRST_CALL)) == {
        "script": "wait",
        "seconds": 60,
        "reason": "CI on the PR",
    }
    assert ns["last_role"](_REAL_FIRST_CALL) == "user"
    assert "wait" in ns["offered_tools"](_REAL_FIRST_CALL)
    # The turn's next call ends on the wait's result, then the loop's notes: not scripted again.
    second = {
        **_REAL_FIRST_CALL,
        "messages": [
            *_REAL_FIRST_CALL["messages"][:2],
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call_lab_wait"}]},
            {"role": "tool", "tool_call_id": "call_lab_wait", "content": "waited"},
            {"role": "system", "content": "a loop note"},
        ],
    }
    assert ns["last_role"](second) == "tool"


def test_an_ended_seeded_run_is_not_watched():
    """#11220: a force-failed seeded run in ``until`` ended the wait before its card was drawn."""
    watched = _lab_namespace("lab_watched_run")["lab_watched_run"]
    run = "0b7c1d0e-5f3a-4c55-9b0e-2a7f1c3d4e5f"
    assert watched(run, lambda _id: "running") == run
    assert watched(run, lambda _id: "failed") is None
    assert watched(run, lambda _id: None) is None
    assert watched(None, lambda _id: "running") is None


def test_the_wait_answer_says_whether_the_card_was_drawn():
    answer = _lab_namespace("wait_answer", "LAB_CALL_RESULT_MAX_CHARS")["wait_answer"]
    request = {
        "messages": [
            {"role": "user", "content": "lab:wait 30 X"},
            {"role": "tool", "tool_call_id": "call_lab_wait", "content": "Waited 30s."},
        ]
    }
    text, paused = answer(request)
    assert paused and text == "Lab script: the wait answered: Waited 30s."
    request["messages"][-1]["content"] = "No wait needed \u2014 run x: failed."
    assert answer(request)[1] is False
    assert answer({"messages": [{"role": "user"}]}) == ("Lab script: no wait result reached this call.", False)


def test_the_watched_run_is_the_seeded_running_one(tmp_path):
    running = _lab_namespace("lab_running_run", Path=Path)["lab_running_run"]
    path = tmp_path / "fleet_runs.json"
    assert running(path) is None
    path.write_text(json.dumps(["a", "b", "c"]))
    assert running(path) == "c"
    path.write_text("not json")
    assert running(path) is None


def _seed_cut_wait():
    import importlib.util

    spec = importlib.util.spec_from_file_location("seed_cut_wait", LAB.parent / "seed_cut_wait.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_journaled_cut_wait_is_one_the_resumed_turn_continues():
    from app.services import turn_checkpoint
    from app.tools.wait_policy import WaitBudget, cut_wait_remaining, cut_wait_to_continue

    seed = _seed_cut_wait()
    messages = turn_checkpoint._completed_rounds(turn_checkpoint._messages(seed.entries(30, 1000.0)))
    assert len(messages) == 2
    found = cut_wait_to_continue(messages)
    assert found is not None and found[0] == 1
    assert found[1].wait_cut["granted"] == 30.0
    # The card's reason is read from the cut call's arguments.
    assert messages[0].tool_calls[0].arguments["reason"] == seed.REASON
    assert cut_wait_remaining(found[1].wait_cut, now=1005.0, budget=WaitBudget()) == pytest.approx(25.0)


#: The resumed turn's first model request after a ``lab:cut-wait 30``, as the stub received it (xtm-one
#: #9337, lab from main 6f53a051): NOT streamed; the replayed cut round, the platform's resume note,
#: then the person's message appended again.
_RESUMED_CUT_WAIT_CALL = {
    "messages": [
        {"role": "system", "content": "You are CTEM Assistant."},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call_lab_cut_wait", "type": "function", "function": {"name": "wait", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "call_lab_cut_wait", "content": "Waited 31s. Remaining wait budget ..."},
        {"role": "assistant", "content": "[Resumed: this turn continues one that was interrupted ...]"},
        {"role": "user", "content": "lab:cut-wait 30"},
    ],
}


def test_the_resumed_cut_wait_call_is_held_then_answered():
    ns = _lab_namespace(
        "answers_cut_wait",
        "last_role",
        "LAB_CUT_WAIT_CALL_ID",
        "LAB_CUT_WAIT_ANALYZE_HOLD_SECONDS",
        "LAB_SCRIPT_MAX_HOLD_SECONDS",
    )
    assert ns["LAB_CUT_WAIT_CALL_ID"] == _seed_cut_wait().CALL_ID
    assert 0 < ns["LAB_CUT_WAIT_ANALYZE_HOLD_SECONDS"] < ns["LAB_SCRIPT_MAX_HOLD_SECONDS"]
    assert ns["answers_cut_wait"](_RESUMED_CUT_WAIT_CALL)
    # Why the scripted branch never ran: the call is not streamed (and ends on the person's message).
    assert not _RESUMED_CUT_WAIT_CALL.get("stream")
    assert ns["last_role"](_RESUMED_CUT_WAIT_CALL) == "user"
    # The first call of the turn, and a later call after another tool, are not held.
    assert not ns["answers_cut_wait"]({"messages": [{"role": "user", "content": "lab:cut-wait 30"}]})
    later = [
        *_RESUMED_CUT_WAIT_CALL["messages"],
        {"role": "assistant", "tool_calls": [{"id": "call_2"}]},
        {"role": "tool", "tool_call_id": "call_2"},
    ]
    assert not ns["answers_cut_wait"]({"messages": later})
    assert not ns["answers_cut_wait"]({})


def test_the_cut_is_queued_with_a_cause_the_sweep_resumes_at_once():
    from app.services import cut_turn_resume

    assert cut_turn_resume.resumable_cause("recycle")
    source = (LAB.parent / "seed_cut_wait.py").read_text(encoding="utf-8")
    assert 'cause="recycle"' in source


def _fleet_prompt(level: str) -> str:
    """The ``[Fleet]`` system block the backend writes for a fleet chat at *level*."""
    from types import SimpleNamespace

    from app.services.fleet_studio import FleetStudioContext, format_fleet_studio_prompt

    fleet = SimpleNamespace(
        id="5f0c3a1e-8b2d-4e6f-9a7b-1c2d3e4f5a6b",
        name="Lab fleet",
        autonomy_level=level,
        is_builtin=False,
    )
    ctx = FleetStudioContext(
        fleet=fleet,
        can_edit=True,
        workspace_name="Lab",
        orchestrator_name=None,
        members=(),
        member_count=0,
    )
    return format_fleet_studio_prompt(ctx)


def _fleet_ns() -> dict:
    return _lab_namespace(
        "parse_lab_script",
        "bound_fleet",
        "fleet_call",
        "fleet_call_chunks",
        "last_user_text",
        "LAB_AUTONOMY_LEVELS",
        "LAB_SCRIPT_MAX_HOLD_SECONDS",
        "LAB_WAIT_DEFAULT_SECONDS",
        "LAB_WAIT_MAX_SECONDS",
    )


def test_the_fleet_scripts_are_parsed():
    parse = _fleet_ns()["parse_lab_script"]
    assert parse("lab:fleet-pause") == {"script": "fleet-pause"}
    assert parse("LAB:fleet-raise now") == {"script": "fleet-raise"}
    assert parse("please lab:fleet-pause") is None


def test_the_bound_fleet_is_read_from_the_backend_fleet_prompt():
    ns = _fleet_ns()
    request = {
        "messages": [
            {"role": "system", "content": "You are the agent." + _fleet_prompt("medium")},
            {"role": "user", "content": "lab:fleet-raise"},
        ]
    }
    assert ns["bound_fleet"](request) == ("5f0c3a1e-8b2d-4e6f-9a7b-1c2d3e4f5a6b", "medium")
    # An ordinary chat carries no [Fleet] block: nothing is bound.
    assert ns["bound_fleet"]({"messages": [{"role": "system", "content": "fleet_id: x"}]}) is None
    assert ns["bound_fleet"]({}) is None


def test_the_lab_autonomy_order_is_the_backend_one():
    from app.services.fleet_autonomy import AUTONOMY_LEVELS

    assert _fleet_ns()["LAB_AUTONOMY_LEVELS"] == AUTONOMY_LEVELS


def test_the_fleet_calls_reach_the_card_answers():
    from app.services.builtin_tool_aliases import BUILTIN_GROUP_ALIASES
    from app.services.fleet_autonomy import is_raise

    ns = _fleet_ns()
    fleet = ("5f0c3a1e-8b2d-4e6f-9a7b-1c2d3e4f5a6b", "full_gates")
    name, arguments = ns["fleet_call"]({"script": "fleet-pause"}, fleet)
    # No confirm: the tool answers the "Not applied" summary the confirm card reads.
    assert (name, arguments) == ("set_fleet_automations", {"fleet_id": fleet[0], "action": "pause"})
    name, arguments = ns["fleet_call"]({"script": "fleet-raise"}, fleet)
    assert name == "update_fleet" and arguments["fleet_id"] == fleet[0]
    assert is_raise("full_gates", arguments["autonomy_level"])
    assert ns["fleet_call"]({"script": "fleet-raise"}, (fleet[0], "low"))[1]["autonomy_level"] == "autonomous"
    # Already autonomous: no level above, nothing called.
    assert ns["fleet_call"]({"script": "fleet-raise"}, (fleet[0], "autonomous")) is None
    assert {"set_fleet_automations", "update_fleet"} <= set(BUILTIN_GROUP_ALIASES["fleets"][1])
    first, last = ns["fleet_call_chunks"]("update_fleet", {"fleet_id": fleet[0], "autonomy_level": "medium"})
    (call,) = first["choices"][0]["delta"]["tool_calls"]
    assert call["function"]["name"] == "update_fleet"
    assert json.loads(call["function"]["arguments"])["autonomy_level"] == "medium"
    assert last["choices"][0]["finish_reason"] == "tool_calls"


def _call_ns() -> dict:
    return _lab_namespace(
        "parse_lab_script", "parse_lab_call", "called_tool_answer", "fleet_call_chunks", "last_role",
        "LAB_CALL_RESULT_MAX_CHARS", "LAB_SCRIPT_MAX_HOLD_SECONDS", "LAB_WAIT_DEFAULT_SECONDS",
        "LAB_WAIT_MAX_SECONDS", "LAB_CHAT_SCRIPTS",
    )


def test_the_call_script_reads_a_tool_name_and_json_arguments():
    parse = _call_ns()["parse_lab_script"]
    assert parse("lab:call create_opencti_note") == {"script": "call", "tool": "create_opencti_note", "arguments": {}}
    assert parse('LAB:call create_opencti_note {"content": "a b", "n": 1}') == {
        "script": "call", "tool": "create_opencti_note", "arguments": {"content": "a b", "n": 1},
    }
    # Nothing is called on a missing name, a name that is not one, or arguments that are not an object.
    for text in ("lab:call", "lab:call {}", "lab:call note [1]", "lab:call note {broken"):
        script = parse(text)
        assert script["script"] == "call" and "tool" not in script and "lab:call <tool_name>" in script["error"]
    assert parse("please lab:call note") is None


def test_the_call_is_streamed_by_name_then_its_result_is_quoted():
    ns = _call_ns()
    first, last = ns["fleet_call_chunks"]("create_opencti_note", {})
    (call,) = first["choices"][0]["delta"]["tool_calls"]
    assert call["function"]["name"] == "create_opencti_note" and json.loads(call["function"]["arguments"]) == {}
    assert last["choices"][0]["finish_reason"] == "tool_calls"
    after = {
        "messages": [
            {"role": "user", "content": "lab:call create_opencti_note {}"},
            {"role": "assistant", "tool_calls": [call]},
            {"role": "tool", "tool_call_id": call["id"], "content": "Refused: the OpenCTI integration is\nread-only."},
        ]
    }
    # The next model call of the turn ends on the tool's result: it is answered, never called again.
    assert ns["last_role"](after) == "tool"
    assert ns["called_tool_answer"](after) == "Lab script: the tool answered: Refused: the OpenCTI integration is read-only."
    long = {"messages": [{"role": "tool", "content": [{"type": "text", "text": "x" * 5000}]}]}
    assert ns["called_tool_answer"](long).endswith("x" * 10 + " [cut]")
    assert ns["called_tool_answer"]({"messages": []}) == "Lab script: no tool result reached this call."


def test_an_unstreamed_call_with_tools_runs_the_call_script_alone():
    # An assignment run's model calls are not streamed (#11304): lab:call runs there, nothing else does.
    ns = _lab_namespace(
        "unstreamed_script",
        "called_tool_completion",
        "parse_lab_script",
        "parse_lab_call",
        "last_user_text",
        "parse_lab_stream",
        "parse_lab_echo",
        "LAB_SCRIPT_MAX_HOLD_SECONDS",
        "LAB_WAIT_DEFAULT_SECONDS",
        "LAB_WAIT_MAX_SECONDS",
        "LAB_STREAM_DEFAULTS",
        "LAB_STREAM_MAX_BURSTS",
        "LAB_STREAM_MAX_GAP_MS",
        "LAB_ECHO_MAX_CHARS",
    )
    tools = [{"type": "function", "function": {"name": "list_work_items"}}]

    def request(text: str, **extra: object) -> dict:
        return {"messages": [{"role": "system", "content": "s"}, {"role": "user", "content": text}], **extra}

    assert ns["unstreamed_script"](request("lab:call list_work_items {}", tools=tools)) == {
        "script": "call",
        "tool": "list_work_items",
        "arguments": {},
    }
    # An assignment run's rendered prompt: the trigger block follows the script line (#11304).
    rendered = "lab:call list_work_items {}\n\n--- Trigger Information ---\nTrigger Context:\n{\n  \"type\": \"manual\"\n}"
    assert ns["unstreamed_script"](request(rendered, tools=tools)) == {
        "script": "call",
        "tool": "list_work_items",
        "arguments": {},
    }
    rendered_no_args = "lab:call list_work_items\n\n--- Trigger Information ---\n{}"
    assert ns["unstreamed_script"](request(rendered_no_args, tools=tools))["arguments"] == {}
    assert ns["parse_lab_call"](["list_work_items", '{"limit": 2} trailing'])["arguments"] == {"limit": 2}
    assert "error" in ns["parse_lab_call"](["list_work_items", "not json"])
    assert "error" in ns["parse_lab_call"](["list_work_items", "[1]"])
    # No tool offered (a title or gate side call), or another script: answered as before.
    assert ns["unstreamed_script"](request("lab:call list_work_items")) is None
    for text in ("lab:wait 5", "lab:stream", "lab:echo\nhi", "lab:resuming 1", "hello"):
        assert ns["unstreamed_script"](request(text, tools=tools)) is None
    completion = ns["called_tool_completion"]("list_work_items", {"limit": 1})
    (choice,) = completion["choices"]
    assert completion["object"] == "chat.completion" and choice["finish_reason"] == "tool_calls"
    (call,) = choice["message"]["tool_calls"]
    assert call["function"]["name"] == "list_work_items" and json.loads(call["function"]["arguments"]) == {"limit": 1}


def test_the_stub_reads_the_call_script_on_an_unstreamed_call():
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    stub = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "StubModel")
    source = ast.get_source_segment(LAB.read_text(encoding="utf-8"), stub) or ""
    assert "else unstreamed_script(request)" in source
    assert "self._send(called_tool_completion(" in source


def test_the_call_script_is_named_in_the_start_log():
    assert any(name.startswith("lab:call ") for name in _call_ns()["LAB_CHAT_SCRIPTS"])


def _restart_ns(**globals_: object) -> dict:
    import sys
    import threading
    from pathlib import Path

    return _lab_namespace(
        "parse_lab_script",
        "restart_step",
        "restart_start_chunks",
        "message_text",
        "backend_argv",
        "backend_spawn_env",
        "restart_resumed_answer",
        "LAB_SCRIPT_MAX_HOLD_SECONDS",
        "LAB_WAIT_DEFAULT_SECONDS",
        "LAB_WAIT_MAX_SECONDS",
        "LAB_BACKEND_GRACE_SECONDS",
        "LAB_RESTART_DEFAULT_HOLD_SECONDS",
        "LAB_RESTART_MAX_HOLD_SECONDS",
        "LAB_RESTART_MAX_RESUMED_SECONDS",
        "LAB_RESTART_CALL_ID",
        "LAB_RESTART_TOOLS",
        "LAB_RESUMED_NOTE_PREFIX",
        "LAB_CHAT_SCRIPTS",
        "_RESTART",
        threading=threading,
        sys=sys,
        CHECKOUT=Path("/checkout"),
        API_PORT=8100,
        backend_env=lambda: {"LOG_LEVEL": "warning"},
        **globals_,
    )


def test_the_restart_script_reads_its_hold():
    parse = _restart_ns()["parse_lab_script"]
    assert parse("lab:restart-mid-turn") == {"script": "restart-mid-turn", "hold": 5, "resumed": 0}
    assert parse("LAB:restart-mid-turn 20") == {"script": "restart-mid-turn", "hold": 20, "resumed": 0}
    assert parse("lab:restart-mid-turn 900")["hold"] == 60
    assert parse("please lab:restart-mid-turn") is None
    assert any(name.startswith("lab:restart-mid-turn") for name in _restart_ns()["LAB_CHAT_SCRIPTS"])


def test_the_restart_turn_starts_then_stops_then_answers_its_resume():
    from app.llm.base import ChatMessage
    from app.services.turn_checkpoint import ResumedWork, resume_note
    from app.tools.base import ToolCall

    ns = _restart_ns()
    step = ns["restart_step"]
    first = {"messages": [{"role": "system", "content": "rules"}, {"role": "user", "content": "lab:restart-mid-turn"}]}
    assert step(first) == "start"
    *pieces, call_chunk, last = ns["restart_start_chunks"]({"search_knowledge"})
    assert "".join(p["choices"][0]["delta"]["content"] for p in pieces).startswith("Lab script:")
    (call,) = call_chunk["choices"][0]["delta"]["tool_calls"]
    assert call["id"] == ns["LAB_RESTART_CALL_ID"] and call["function"]["name"] == "search_knowledge"
    assert last["choices"][0]["finish_reason"] == "tool_calls"
    # The read-only tools it calls exist, with the one argument it sends.
    from app.services.builtin_tools import kb, memory

    for module, name in ((memory, "search_memory"), (kb, "search_knowledge")):
        assert f'name="{name}"' in Path(module.__file__).read_text(encoding="utf-8")
    after_call = {
        "messages": [
            *first["messages"],
            {"role": "assistant", "tool_calls": [call]},
            {"role": "tool", "tool_call_id": call["id"], "content": "no result"},
        ]
    }
    assert step(after_call) == "stop"
    # The restarted process replays the round, then the platform's own resume note: the turn is answered.
    replayed = [
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id=call["id"], name="search_knowledge", arguments={"query": "x"})],
        )
    ]
    note = resume_note(ResumedWork(messages=replayed))
    assert note is not None and note.content.startswith(ns["LAB_RESUMED_NOTE_PREFIX"])
    resumed = {
        "messages": [
            *after_call["messages"],
            {"role": "assistant", "content": note.content},
            {"role": "user", "content": "lab:restart-mid-turn"},
        ]
    }
    assert step(resumed) == "resumed"
    # A resumed call without a note while a restart's cut turn waits is answered, never stopped again.
    assert step(after_call, True) == "resumed"
    ns["_RESTART"].update(stopped_at="14:00:00Z", back_at="14:00:09Z", pending=True)
    ns["log"] = lambda line: None
    answer = ns["restart_resumed_answer"]()
    assert "14:00:00Z" in answer and "14:00:09Z" in answer and ns["_RESTART"]["pending"] is False


def test_the_restarted_backend_drains_like_production_and_reports_another_build():
    from app.core import worker_shutdown
    from app.services import cut_turn_resume

    ns = _restart_ns()
    argv = ns["backend_argv"]()
    assert argv[argv.index("--timeout-graceful-shutdown") + 1] == str(ns["LAB_BACKEND_GRACE_SECONDS"])
    assert "lab_asgi:application" in argv
    first, restarted = ns["backend_spawn_env"](0), ns["backend_spawn_env"](1)
    grace = worker_shutdown.graceful_timeout(first)
    assert grace == ns["LAB_BACKEND_GRACE_SECONDS"]
    # The grace leaves the stopped backend time to cut the turn well before the stub stops holding its call.
    assert grace < 60 and "APP_VERSION" not in first
    # The restarted process is another build: the stop's same-build defer does not hold the resume.
    assert restarted["APP_VERSION"] != "0.0.0-dev" and cut_turn_resume.resumable_cause("stop")


def test_the_restart_replaces_the_backend_outside_the_lab_watch():
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    restart = ast.unparse(functions["restart_backend"])
    # Out of CHILDREN before the signal, so the lab's watch does not stop the lab when the backend exits.
    assert restart.index("CHILDREN.remove(old)") < restart.index("old.send_signal(signal.SIGTERM)")
    assert restart.index("exited.set()") < restart.index("time.sleep(hold)") < restart.index("spawn('backend'")
    assert "backend stopped at" in restart and "backend back at" in restart
    assert "resumed turn answered" in ast.unparse(functions["restart_resumed_answer"])
    assert "BACKEND['process'] = spawn('backend', backend_argv()" in ast.unparse(functions["main"])
