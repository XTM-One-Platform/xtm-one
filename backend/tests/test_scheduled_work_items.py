"""Scheduled work items: the rules, without a database.

The dispatch tick, the run and the claim race against a real PostgreSQL are in
``test_scheduled_work_items_postgres.py``. Here: the time rules (an offset is
required, the daylight saving change of 25 Oct 2026 in Europe/Paris), the
action, the derived state, the always-on tools and their deny-list immunity,
and the walls between the scheduler and the generic work-item paths.
"""

from __future__ import annotations

import ast
import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

import app.services.work_item as wi_service
from app.services import scheduled_work_items as swi
from app.services.builtin_tools import apply_tool_deny_list, build_scheduling_tools
from app.services.builtin_tools.scheduling import SCHEDULING_TOOL_NAMES, refuse_core_tool_denials
from app.services.company_managed_writes import agent_writer
from tests import app_sources

APP_DIR = Path(__file__).resolve().parents[1] / "app"
PARIS = "Europe/Paris"
#: Before the 25 Oct 2026 daylight saving change, so every time below is ahead.
NOW = datetime(2026, 10, 7, 10, 0, tzinfo=UTC)


def _scheduled(**overrides):
    now = datetime.now(UTC)
    base = dict(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        is_company_managed=False,
        owner_agent_id=uuid.uuid4(),
        created_by_agent_id=None,
        title="Post the EMT reminder",
        description=None,
        payload={
            "action": {
                "mode": "exact",
                "tool": "send_teams_channel_message",
                "arguments": {"channel_id": "19:abc", "message": "Test <at>EMT</at>"},
                "target": "Testing XTM One",
            }
        },
        status="open",
        priority="medium",
        correlation_key=None,
        due_at=now + timedelta(hours=1),
        claimed_by_agent_id=None,
        claimed_at=None,
        source="agent",
        result=None,
        last_activity_at=now,
        created_at=now,
        updated_at=now,
        action_mode="exact",
        run_id=None,
        conversation_id=None,
        outcome_code=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ── Time ─────────────────────────────────────────────────────────────────────


def test_a_time_without_an_offset_is_refused_never_read_as_utc():
    with pytest.raises(swi.ScheduledWorkItemError) as caught:
        swi.parse_due_at("2026-10-08T09:00:00", tz_name=PARIS, now=NOW)
    message = str(caught.value)
    assert "never read as UTC" in message
    # The fix is spelled out in the requester's zone AND in UTC.
    assert "2026-10-08T09:00:00+02:00" in message
    assert "07:00 UTC" in message


def test_a_date_alone_has_no_offset_either():
    with pytest.raises(swi.ScheduledWorkItemError, match="no UTC offset"):
        swi.parse_due_at("2026-10-08", tz_name=PARIS, now=NOW)


def test_z_and_an_explicit_offset_are_the_same_instant():
    assert (
        swi.parse_due_at("2026-10-08T07:00:00Z", now=NOW).at
        == swi.parse_due_at("2026-10-08T09:00:00+02:00", now=NOW).at
    )


def test_a_past_time_is_refused_with_local_time_and_utc():
    with pytest.raises(swi.ScheduledWorkItemError) as caught:
        swi.parse_due_at("2026-10-07T11:00:00+02:00", tz_name=PARIS, now=NOW)
    message = str(caught.value)
    assert "not in the future" in message
    assert "Europe/Paris" in message and "UTC" in message


def test_beyond_the_horizon_is_refused(monkeypatch):
    monkeypatch.setattr(swi.settings, "scheduled_work_item_max_horizon_days", 30)
    with pytest.raises(swi.ScheduledWorkItemError, match="more than 30 days"):
        swi.parse_due_at("2026-12-25T09:00:00+01:00", now=NOW)


# The daylight saving change of Sunday 25 Oct 2026 in Europe/Paris: at 03:00
# CEST (UTC+2) the clocks go back to 02:00 CET (UTC+1).


def test_dst_the_offset_written_decides_the_instant():
    before = swi.parse_due_at("2026-10-24T09:00:00+02:00", tz_name=PARIS, now=NOW)
    after = swi.parse_due_at("2026-10-25T09:00:00+01:00", tz_name=PARIS, now=NOW)
    assert before.at == datetime(2026, 10, 24, 7, 0, tzinfo=UTC)
    assert after.at == datetime(2026, 10, 25, 8, 0, tzinfo=UTC)
    assert swi.describe_time(before.at, PARIS) == (
        "Sat 24 Oct 2026 09:00 Europe/Paris (CEST, UTC+02:00) = 07:00 UTC"
    )
    assert swi.describe_time(after.at, PARIS) == (
        "Sun 25 Oct 2026 09:00 Europe/Paris (CET, UTC+01:00) = 08:00 UTC"
    )
    assert swi.offset_note(before.given, PARIS) is None
    assert swi.offset_note(after.given, PARIS) is None


def test_dst_a_summer_offset_after_the_change_is_said():
    # Written with last week's offset: the instant is 07:00 UTC = 08:00 CET.
    due = swi.parse_due_at("2026-10-25T09:00:00+02:00", tz_name=PARIS, now=NOW)
    assert due.at == datetime(2026, 10, 25, 7, 0, tzinfo=UTC)
    note = swi.offset_note(due.given, PARIS)
    assert note is not None
    assert "UTC+02:00 is not Europe/Paris time on 2026-10-25" in note
    assert "runs at 08:00 Europe/Paris time" in note


def test_dst_a_wall_time_the_change_repeats_must_be_disambiguated():
    with pytest.raises(swi.ScheduledWorkItemError) as caught:
        swi.parse_due_at("2026-10-25T02:30:00", tz_name=PARIS, now=NOW)
    message = str(caught.value)
    assert "happens twice" in message
    assert "2026-10-25T02:30:00+02:00 is 00:30 UTC" in message
    assert "2026-10-25T02:30:00+01:00 is 01:30 UTC" in message


def test_dst_a_wall_time_the_spring_change_skips_does_not_exist():
    with pytest.raises(swi.ScheduledWorkItemError, match="does not exist in Europe/Paris"):
        swi.parse_due_at("2027-03-28T02:30:00", tz_name=PARIS, now=NOW)


def test_without_a_time_zone_times_are_said_in_utc():
    assert swi.describe_time(datetime(2026, 10, 25, 8, 0, tzinfo=UTC), None) == (
        "Sun 25 Oct 2026 08:00 UTC"
    )


# ── The action ───────────────────────────────────────────────────────────────


def test_an_exact_action_keeps_its_arguments_as_given():
    body = "Bonjour <at>EMT</at>,\n\nLe point de 9h est maintenu. \u2014 Samuel"
    action = swi.build_action(
        "exact", tool="send_teams_channel_message", arguments={"message": body}
    )
    assert action == {
        "mode": "exact",
        "tool": "send_teams_channel_message",
        "arguments": {"message": body},
    }


def test_arguments_given_as_a_json_string_are_parsed_once():
    action = swi.build_action("exact", tool="t", arguments='{"message": "Hi"}')
    assert action["arguments"] == {"message": "Hi"}


@pytest.mark.parametrize(
    ("mode", "kwargs", "match"),
    [
        ("exact", {"tool": "", "arguments": {}}, "name of the tool"),
        ("exact", {"tool": "t", "arguments": ["not", "an", "object"]}, "JSON object"),
        ("instruction", {"prompt": "  "}, "needs a prompt"),
        ("later", {}, "mode must be"),
    ],
    ids=["exact-no-tool", "exact-list-arguments", "instruction-empty", "unknown-mode"],
)
def test_an_invalid_action_is_refused(mode, kwargs, match):
    with pytest.raises(swi.ScheduledWorkItemError, match=match):
        swi.build_action(mode, **kwargs)


def test_the_preview_is_the_longest_text_or_the_prompt():
    action = swi.action_of(_scheduled().payload)
    assert swi.action_preview(action) == "Test <at>EMT</at>"
    assert swi.action_preview({"mode": "instruction", "prompt": "Summarise  the\nweek"}) == (
        "Summarise the week"
    )


def test_the_preview_is_capped_on_the_server_with_an_ellipsis():
    # One line of a schedule row: never an unbounded string.
    cap = swi.PREVIEW_CHARS
    whole = "x" * cap
    assert swi.action_preview({"mode": "instruction", "prompt": whole}) == whole
    cut = swi.action_preview({"mode": "instruction", "prompt": "word " * 500})
    assert len(cut) == cap
    assert cut.endswith("\u2026")
    assert "  " not in cut
    exact = swi.action_preview({"mode": "exact", "tool": "send_message", "arguments": {"text": "y" * 5000}})
    assert len(exact) == cap
    assert exact == "y" * (cap - 1) + "\u2026"


def test_a_generic_write_cannot_carry_an_action():
    with pytest.raises(swi.ScheduledWorkItemError, match="reserved for scheduled work items"):
        swi.refuse_generic_action_write({"action": {"mode": "instruction", "prompt": "x"}})
    swi.refuse_generic_action_write({"source_run_id": "abc"})
    swi.refuse_generic_action_write(None)


@pytest.mark.asyncio
async def test_the_generic_create_refuses_an_action_before_writing(monkeypatch):
    def _no_session():
        raise AssertionError("nothing may be written")

    monkeypatch.setattr("app.core.database.async_session", _no_session)
    with pytest.raises(swi.ScheduledWorkItemError):
        await wi_service.create_work_item(
            user_id=str(uuid.uuid4()),
            title="Sneaky",
            payload={"action": {"mode": "instruction", "prompt": "do it"}},
            writer=agent_writer(is_company_managed=False),
        )


# ── State ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("overrides", "state"),
    [
        ({"due_at": NOW + timedelta(minutes=5)}, "scheduled"),
        ({"due_at": NOW - timedelta(minutes=5)}, "due"),
        ({"due_at": NOW - timedelta(minutes=20)}, "missed"),
        ({"status": "claimed"}, "running"),
        ({"status": "in_progress"}, "running"),
        ({"status": "done"}, "done"),
        ({"status": "cancelled"}, "cancelled"),
        ({"status": "blocked", "outcome_code": swi.OUTCOME_BINDING}, "blocked"),
        ({"status": "blocked", "outcome_code": swi.OUTCOME_MISSED}, "missed"),
    ],
    ids=["scheduled", "due", "missed-unswept", "claimed", "in-progress", "done", "cancelled", "blocked", "missed"],
)
def test_the_state_follows_the_item(monkeypatch, overrides, state):
    monkeypatch.setattr(swi.settings, "scheduled_work_item_grace_minutes", 15)
    assert swi.schedule_state(_scheduled(**overrides), NOW) == state


def test_an_ordinary_item_has_no_scheduled_state():
    assert swi.schedule_state(_scheduled(action_mode=None), NOW) is None
    assert swi.scheduled_view(_scheduled(action_mode=None)) is None


def test_a_scheduled_item_past_its_time_is_never_stale():
    item = _scheduled(due_at=datetime.now(UTC) - timedelta(days=2))
    assert wi_service.is_work_item_stale(item) is False
    serialized = wi_service.serialize_work_item(item)
    assert serialized["is_stale"] is False
    assert serialized["scheduled"]["state"] == "missed"
    assert serialized["scheduled"]["tool"] == "send_teams_channel_message"
    assert serialized["scheduled"]["target"] == "Testing XTM One"


def test_the_stale_count_leaves_scheduled_items_out():
    sql = str(
        wi_service._stale_sql_clause(datetime.now(UTC)).compile(dialect=postgresql.dialect())
    ).lower()
    assert "action_mode is null" in sql


def test_the_markers_are_appended_once():
    item_id = str(uuid.uuid4())
    text = swi.append_markers("Scheduled.", [item_id, item_id])
    assert text == f"Scheduled.\n\n[[WORK_ITEM:{item_id}]]"
    assert swi.append_markers(text, [item_id]) == text


def test_the_marker_matches_the_channel_pattern():
    from app.channels.base import _WORKITEM_MARKER_RE, strip_artifact_markers, strip_channel_work_item_markers

    item_id = uuid.uuid4()
    text = f"Done.\n\n{swi.marker(item_id)}"
    assert _WORKITEM_MARKER_RE.search(text).group(1) == str(item_id)
    assert strip_channel_work_item_markers(text) == "Done."
    assert strip_artifact_markers(text) == "Done."


# ── The always-on tools ──────────────────────────────────────────────────────


def _tools(**kwargs):
    return build_scheduling_tools(
        user_id=str(uuid.uuid4()), agent_id=str(uuid.uuid4()), agent_name="EMT Assistant", **kwargs
    )


def test_every_agent_gets_four_core_tools_and_nothing_to_route_or_claim():
    definitions, executor = _tools()
    assert {d.name for d in definitions} == SCHEDULING_TOOL_NAMES
    assert all(d.core for d in definitions)
    assert set(executor.tool_names) == SCHEDULING_TOOL_NAMES
    offered = {d.name for d in definitions}
    assert not offered & {"claim_work_item", "complete_work_item", "create_work_item", "update_work_item"}
    schedule = next(d for d in definitions if d.name == "schedule_work_item")
    # The owner is the calling agent: there is nothing to route an item with.
    assert "owner_agent_id" not in schedule.input_schema["properties"]


def test_a_deny_list_never_removes_a_scheduling_tool():
    definitions, _ = _tools()
    kept = apply_tool_deny_list(definitions, sorted(SCHEDULING_TOOL_NAMES))
    assert {d.name for d in kept} == SCHEDULING_TOOL_NAMES


def test_a_deny_list_naming_a_scheduling_tool_is_refused():
    with pytest.raises(ValueError, match="cannot be denied"):
        refuse_core_tool_denials(["delete_all_issues", "cancel_scheduled_work_item"])
    assert refuse_core_tool_denials(["delete_all_issues"]) == ["delete_all_issues"]
    assert refuse_core_tool_denials(None) is None


def test_a_deny_list_is_normalized_and_bounded_alike_for_every_writer():
    from app.services.builtin_tools.scheduling import (
        MAX_TOOL_DENY_LIST_ENTRIES,
        MAX_TOOL_DENY_LIST_NAME_LENGTH,
        normalize_tool_deny_list,
    )

    assert normalize_tool_deny_list(None) is None
    assert normalize_tool_deny_list([]) == []
    assert normalize_tool_deny_list(["", "  "]) == []
    assert normalize_tool_deny_list([" send_email ", "delete_note", "send_email"]) == [
        "send_email",
        "delete_note",
    ]
    names = [f"tool_{i}" for i in range(MAX_TOOL_DENY_LIST_ENTRIES)]
    assert normalize_tool_deny_list(names) == names
    with pytest.raises(ValueError, match="at most 500 tool names"):
        normalize_tool_deny_list([*names, "one_more"])
    assert normalize_tool_deny_list(["x" * MAX_TOOL_DENY_LIST_NAME_LENGTH]) == [
        "x" * MAX_TOOL_DENY_LIST_NAME_LENGTH
    ]
    with pytest.raises(ValueError, match="at most 256 characters"):
        normalize_tool_deny_list(["x" * (MAX_TOOL_DENY_LIST_NAME_LENGTH + 1)])
    with pytest.raises(ValueError, match="cannot be denied"):
        normalize_tool_deny_list([" schedule_work_item "])


@pytest.mark.parametrize("schema", ["CreateAgentRequest", "UpdateAgentRequest"])
def test_the_agent_api_refuses_to_deny_a_scheduling_tool(schema):
    from app.api.v1.schemas import agent as agent_schemas

    model = getattr(agent_schemas, schema)
    body = {"name": "EMT"} if schema == "CreateAgentRequest" else {}
    with pytest.raises(ValidationError, match="cannot be denied"):
        model(**body, tool_deny_list=["schedule_work_item"])
    assert model(**body, tool_deny_list=["send_email"]).tool_deny_list == ["send_email"]


def test_chat_offers_the_tools_whatever_the_built_in_selection():
    """The wiring is not behind any ``builtin_tool_ids`` test: an agent with
    no ``work_items`` (or no built-in tool at all) still schedules."""
    path = APP_DIR / "services" / "agent_chat.py"
    tree = app_sources.tree(path)
    guards = []
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and any(
            isinstance(child, ast.Call)
            and getattr(child.func, "id", None) == "build_scheduling_tools"
            for child in ast.walk(node)
            if child is not node.test
        ):
            guards.append(ast.unparse(node.test))
    assert guards, "build_scheduling_tools is not called from run_agent_chat"
    innermost = guards[-1]
    assert "builtin_tool_ids" not in innermost
    assert "is_anonymous_user" in innermost and "identity_from_platform" in innermost


@pytest.mark.asyncio
async def test_the_kill_switch_refuses_to_schedule_but_lists(monkeypatch):
    monkeypatch.setattr(swi, "scheduling_enabled", lambda: False)

    async def _no_items(**_kwargs):
        return []

    async def _no_tz(_user_id):
        return None

    monkeypatch.setattr(swi, "list_scheduled_work_items", _no_items)
    monkeypatch.setattr("app.services.builtin_tools.scheduling._timezone", _no_tz)
    _, executor = _tools()
    schedule = executor.handler("schedule_work_item")
    answer = await schedule(title="x", due_at="2030-01-01T09:00:00+01:00", mode="instruction", prompt="y")
    assert answer.startswith("Error: Scheduling is disabled by the administrator.")
    reschedule = executor.handler("reschedule_scheduled_work_item")
    assert "disabled by the administrator" in await reschedule(
        work_item_id=str(uuid.uuid4()), due_at="2030-01-01T09:00:00+01:00"
    )
    listing = await executor.handler("list_scheduled_work_items")()
    assert "disabled by the administrator" in listing


@pytest.mark.asyncio
async def test_a_time_without_offset_reaches_the_model_as_an_error(monkeypatch):
    monkeypatch.setattr(swi, "scheduling_enabled", lambda: True)

    async def _paris(_user_id):
        return PARIS

    async def _no_create(**_kwargs):
        raise AssertionError("nothing may be created")

    monkeypatch.setattr("app.services.builtin_tools.scheduling._timezone", _paris)
    monkeypatch.setattr(swi, "create_scheduled_work_item", _no_create)
    _, executor = _tools()
    answer = await executor.handler("schedule_work_item")(
        title="x", due_at="2030-01-08T09:00:00", mode="instruction", prompt="y"
    )
    assert answer.startswith("Error: due_at '2030-01-08T09:00:00' has no UTC offset")
    assert "+01:00" in answer and "08:00 UTC" in answer


@pytest.mark.asyncio
async def test_a_scheduled_item_is_announced_with_its_card(monkeypatch):
    monkeypatch.setattr(swi, "scheduling_enabled", lambda: True)
    # The wall clock is not the test's: keep 2030 within the horizon.
    monkeypatch.setattr(swi.settings, "scheduled_work_item_max_horizon_days", 36500)
    item_id = str(uuid.uuid4())

    async def _paris(_user_id):
        return PARIS

    async def _create(**kwargs):
        assert kwargs["action"] == {
            "mode": "instruction",
            "prompt": "Summarise the week",
            "notify": False,
        }
        assert kwargs["conversation_id"] == "conv-1"
        return {
            "id": item_id,
            "title": kwargs["title"],
            "due_at": kwargs["due"].at.isoformat(),
            "result": None,
            "scheduled": {"mode": "instruction", "state": "scheduled", "preview": "Summarise the week"},
        }

    monkeypatch.setattr("app.services.builtin_tools.scheduling._timezone", _paris)
    monkeypatch.setattr(swi, "create_scheduled_work_item", _create)
    sink: list[str] = []
    _, executor = _tools(conversation_id="conv-1", item_sink=sink)
    answer = await executor.handler("schedule_work_item")(
        title="Weekly summary",
        due_at="2030-01-11T17:00:00+01:00",
        mode="instruction",
        prompt="Summarise the week",
    )
    assert answer.startswith("Scheduled **Weekly summary**")
    assert "Fri 11 Jan 2030 17:00 Europe/Paris (CET, UTC+01:00) = 16:00 UTC" in answer
    assert sink == [item_id]


# ── Who the outcome notifies ─────────────────────────────────────────────────


async def _schedule_capturing_action(monkeypatch, *, caller_is_person=None, **call):
    monkeypatch.setattr(swi, "scheduling_enabled", lambda: True)
    monkeypatch.setattr(swi.settings, "scheduled_work_item_max_horizon_days", 36500)
    captured: dict = {}

    async def _no_tz(_user_id):
        return None

    async def _create(**kwargs):
        captured.update(kwargs["action"])
        return {"id": str(uuid.uuid4()), "title": kwargs["title"], "due_at": kwargs["due"].at.isoformat(), "scheduled": {}}

    monkeypatch.setattr("app.services.builtin_tools.scheduling._timezone", _no_tz)
    monkeypatch.setattr(swi, "create_scheduled_work_item", _create)
    kwargs = {} if caller_is_person is None else {"caller_is_person": caller_is_person}
    _, executor = _tools(**kwargs)
    answer = await executor.handler("schedule_work_item")(
        title="Retest", due_at="2030-01-11T17:00:00+01:00", mode="instruction", prompt="Retest it", **call
    )
    assert answer.startswith("Scheduled"), answer
    return captured


@pytest.mark.asyncio
async def test_a_one_off_an_agent_schedules_in_a_run_is_silent_by_default(monkeypatch):
    action = await _schedule_capturing_action(monkeypatch)
    assert action["notify"] is False
    assert swi.notifies_person(_scheduled(payload={"action": action})) is False


@pytest.mark.asyncio
async def test_a_one_off_a_person_asks_in_chat_notifies_by_default(monkeypatch):
    action = await _schedule_capturing_action(monkeypatch, caller_is_person=True)
    assert action["notify"] is True
    assert swi.notifies_person(_scheduled(payload={"action": action})) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("caller_is_person", [True, False])
async def test_escalate_to_user_notifies_from_chat_and_from_a_run(monkeypatch, caller_is_person):
    action = await _schedule_capturing_action(
        monkeypatch, caller_is_person=caller_is_person, notify=False, escalate_to_user=True
    )
    assert action["escalate_to_user"] is True
    assert swi.notifies_person(_scheduled(payload={"action": action})) is True


@pytest.mark.asyncio
async def test_an_explicit_notify_wins_over_the_default(monkeypatch):
    action = await _schedule_capturing_action(monkeypatch, notify=True)
    assert action["notify"] is True


def test_an_item_that_needs_a_human_notifies_even_when_silent():
    item = _scheduled(payload={"action": {"mode": "instruction", "prompt": "x", "notify": False}}, needs_human=True)
    assert swi.notifies_person(item) is True


def test_an_item_stored_before_the_flag_notifies_only_from_a_conversation():
    legacy = {"action": {"mode": "instruction", "prompt": "x"}}
    assert swi.notifies_person(_scheduled(payload=legacy, conversation_id=None)) is False
    assert swi.notifies_person(_scheduled(payload=legacy, conversation_id=uuid.uuid4())) is True


def test_an_item_stored_before_the_flag_by_an_agent_is_silent_unless_it_escalates():
    legacy = {"action": {"mode": "instruction", "prompt": "x"}}
    by_agent = {"created_by_agent_id": uuid.uuid4(), "conversation_id": uuid.uuid4()}
    assert swi.notifies_person(_scheduled(payload=legacy, **by_agent)) is False
    assert swi.notifies_person(_scheduled(payload=legacy, needs_human=True, **by_agent)) is True
    escalated = {"action": {"mode": "instruction", "prompt": "x", "escalate_to_user": True}}
    assert swi.notifies_person(_scheduled(payload=escalated, **by_agent)) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("running_notify", [False, None])
async def test_a_one_off_scheduled_in_a_silent_one_offs_run_is_silent(monkeypatch, running_notify):
    # The run of a one-off looks like a person's turn (no assignment, no
    # parent of its own): the one it schedules still inherits its silence,
    # and one stored without the flag counts as silent.
    running = {"mode": "instruction", "prompt": "check again"}
    if running_notify is not None:
        running["notify"] = running_notify
    with swi.running_one_off(running):
        action = await _schedule_capturing_action(monkeypatch, caller_is_person=True)
    assert action["notify"] is False
    assert swi.notifies_person(_scheduled(payload={"action": action})) is False


@pytest.mark.asyncio
async def test_a_one_off_scheduled_in_a_notifying_one_offs_run_inherits_notify(monkeypatch):
    with swi.running_one_off({"mode": "instruction", "prompt": "x", "notify": True}):
        action = await _schedule_capturing_action(monkeypatch, caller_is_person=False)
    assert action["notify"] is True


@pytest.mark.asyncio
async def test_explicit_flags_win_inside_a_one_offs_run(monkeypatch):
    with swi.running_one_off({"mode": "instruction", "prompt": "x", "notify": False}):
        explicit = await _schedule_capturing_action(monkeypatch, caller_is_person=True, notify=True)
        escalated = await _schedule_capturing_action(monkeypatch, caller_is_person=True, escalate_to_user=True)
    assert explicit["notify"] is True
    assert escalated["notify"] is False
    assert swi.notifies_person(_scheduled(payload={"action": escalated})) is True


@pytest.mark.asyncio
async def test_the_one_off_context_reaches_tasks_the_turn_starts_and_ends_with_it():
    async def _default_in_a_task():
        return swi.default_notify(True)

    with swi.running_one_off({"mode": "instruction", "prompt": "x", "notify": False}):
        assert await asyncio.create_task(_default_in_a_task()) is False
    assert swi.default_notify(True) is True
    assert swi.default_notify(False) is False


def test_the_runner_marks_the_instruction_turn_as_a_one_offs_run():
    source = (APP_DIR / "services" / "scheduled_work_item_runner.py").read_text(encoding="utf-8")
    body = source.split("async def _execute_instruction_body(", 1)[1]
    assert body.index("swi.running_one_off(action)") < body.index("run_agent_chat(")


def test_the_flags_must_be_booleans():
    with pytest.raises(swi.ScheduledWorkItemError, match="notify must be true or false"):
        swi.validate_action({"mode": "instruction", "prompt": "x", "notify": "no"})
    with pytest.raises(swi.ScheduledWorkItemError, match="escalate_to_user must be true or false"):
        swi.validate_action({"mode": "instruction", "prompt": "x", "escalate_to_user": 1})


class _Rows:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _OutcomeSession:
    def __init__(self, item):
        self._item = item
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, _statement):
        return _Rows(self._item)

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action_flags", "notified"),
    [
        ({"notify": False}, False),
        ({"notify": True}, True),
        ({"notify": False, "escalate_to_user": True}, True),
    ],
)
async def test_the_outcome_reaches_the_bell_only_when_it_should(monkeypatch, action_flags, notified):
    item = _scheduled(
        status="done",
        result="Retest PASS",
        owner_agent_id=None,
        payload={"action": {"mode": "instruction", "prompt": "Retest it", **action_flags}},
        needs_human=None,
    )
    calls: list[dict] = []

    async def _notify(_db, user_id, **kwargs):
        calls.append({"user_id": user_id, **kwargs})

    async def _no_user(_db, _uid):
        return None

    async def _no_publish(_item):
        return None

    session = _OutcomeSession(item)
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    monkeypatch.setattr("app.services.notify.notify_web_user", _notify)
    monkeypatch.setattr(swi, "_user", _no_user)
    monkeypatch.setattr(swi, "publish_update", _no_publish)
    await swi.deliver_outcome(item.id)
    assert session.committed
    assert len(calls) == (1 if notified else 0)
    if action_flags.get("escalate_to_user"):
        assert "escalated by Agent" in calls[0]["title"]
        assert calls[0]["extra_data"]["escalated"] is True


def test_the_tool_description_states_the_default_and_the_flags():
    definitions, _ = _tools()
    schedule = next(d for d in definitions if d.name == "schedule_work_item")
    assert {"notify", "escalate_to_user"} <= set(schedule.input_schema["properties"])
    assert "default true when a person asked in chat, false in an agent run" in schedule.description


# ── The walls between the consumers ──────────────────────────────────────────


def test_assignments_and_flows_never_receive_a_scheduled_item():
    source = app_sources.source(APP_DIR / "services" / "work_item.py")
    # The pickup rule the dispatch and the views share leaves them out...
    rule = source.split("def _inbox_clauses", 1)[1].split("\ndef ", 1)[0].split("\nasync def ", 1)[0]
    assert "WorkItem.action_mode.is_(None)" in rule
    # ...and the dispatch reads it.
    body = source.split("async def list_dispatchable_work_items", 1)[1].split("\nasync def ", 1)[0]
    assert "_inbox_clauses(" in body


def test_the_scheduler_receives_only_scheduled_items():
    source = app_sources.source(APP_DIR / "services" / "scheduled_work_item_runner.py")
    body = source.split("async def dispatch_due_scheduled_work_items", 1)[1]
    assert body.count("WorkItem.action_mode.is_not(None)") >= 2


class _OneRow:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _Session:
    def __init__(self, row):
        self._row = row
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, _model, _ident):
        return None

    async def execute(self, _statement):
        return _OneRow(self._row)

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
async def test_a_scheduled_item_cannot_be_claimed(monkeypatch):
    item = _scheduled()
    monkeypatch.setattr("app.core.database.async_session", lambda: _Session(item))
    result = await wi_service.claim_work_item(
        str(item.id),
        agent_id=str(uuid.uuid4()),
        user_id=str(item.user_id),
        writer=agent_writer(is_company_managed=False),
    )
    assert result["claimed"] is False
    assert "platform runs at its time" in result["reason"]


@pytest.mark.asyncio
async def test_a_scheduled_item_cannot_be_completed(monkeypatch):
    item = _scheduled()
    session = _Session(item)
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    with pytest.raises(swi.ScheduledWorkItemError, match="cannot be completed"):
        await wi_service.complete_work_item(
            str(item.id),
            user_id=str(item.user_id),
            writer=agent_writer(is_company_managed=False),
        )
    assert item.status == "open" and session.committed is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [{"owner_agent_id": str(uuid.uuid4())}, {"due_at": "2030-01-01T00:00:00Z"}, {"status": "done"}],
    ids=["re-route", "re-date", "status"],
)
async def test_a_scheduled_item_cannot_be_rerouted_or_redated_generically(monkeypatch, change):
    item = _scheduled()
    session = _Session(item)
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    with pytest.raises(swi.ScheduledWorkItemError, match="scheduled work item"):
        await wi_service.update_work_item(
            str(item.id),
            user_id=str(item.user_id),
            writer=agent_writer(is_company_managed=False),
            **change,
        )
    assert session.committed is False


@pytest.mark.asyncio
async def test_a_waiting_scheduled_item_cannot_be_deleted(monkeypatch):
    item = _scheduled()
    monkeypatch.setattr("app.core.database.async_session", lambda: _Session(item))
    with pytest.raises(swi.ScheduledWorkItemError, match="deleted before it settled"):
        await wi_service.delete_work_item(
            str(item.id), user_id=str(item.user_id), writer=agent_writer(is_company_managed=False)
        )


# ── Who may cancel or reschedule ────────────────────────────────────


def _authority(item, acting, handover_ids=None, handover_types=None):
    return swi.change_authority(
        item,
        acting_agent_id=acting,
        acting_handover_ids=handover_ids,
        acting_handover_types=handover_types,
    )


@pytest.mark.parametrize(
    ("status", "outcome", "due_delta", "handover", "expected"),
    [
        # Waiting to run: only a peer that hands work to the scheduler.
        ("open", None, timedelta(hours=1), None, None),
        ("open", None, timedelta(hours=1), "consult", None),
        ("open", None, timedelta(hours=1), "mandatory_transfer", None),
        ("open", None, timedelta(hours=1), "delegate", swi.AUTHORITY_HANDOVER),
        ("open", None, timedelta(hours=1), "transfer", swi.AUTHORITY_HANDOVER),
        ("open", None, timedelta(hours=1), "legacy", swi.AUTHORITY_HANDOVER),
        # A leftover nobody will run: any agent of the person.
        ("blocked", "interrupted", -timedelta(hours=1), None, swi.AUTHORITY_LEFTOVER),
        ("blocked", "missed", -timedelta(hours=1), "consult", swi.AUTHORITY_LEFTOVER),
        ("open", None, -timedelta(hours=3), None, swi.AUTHORITY_LEFTOVER),
        # Running: nobody but the owner and the person.
        ("claimed", None, -timedelta(minutes=1), None, None),
    ],
)
def test_who_may_change_a_scheduled_item(status, outcome, due_delta, handover, expected):
    item = _scheduled(status=status, outcome_code=outcome, due_at=datetime.now(UTC) + due_delta)
    peer = uuid.uuid4()
    target = str(item.owner_agent_id)
    ids = [target] if handover else []
    if handover == "legacy":
        types: dict[str, str] = {}
    elif handover:
        types = {target: handover}
    else:
        types = {str(uuid.uuid4()): "delegate"}
    assert _authority(item, peer, ids, types) == expected


def test_the_owner_and_the_person_always_may_change_their_scheduled_item():
    item = _scheduled()
    assert _authority(item, item.owner_agent_id) == swi.AUTHORITY_OWNER
    assert _authority(item, None) == swi.AUTHORITY_PERSON


def test_a_handover_to_another_agent_gives_no_authority():
    item = _scheduled()
    other = str(uuid.uuid4())
    assert _authority(item, uuid.uuid4(), [other], {other: "delegate"}) is None


def test_a_target_without_a_type_in_an_explicit_map_runs_as_delegate():
    target = uuid.uuid4()
    assert swi.hands_work_to([str(target)], {str(uuid.uuid4()): "consult"}, target)
    assert not swi.hands_work_to([], {}, target)


# ── The work item an action serves ─────────────────────────────────


def test_an_action_serves_the_work_items_it_names():
    served = uuid.uuid4()
    item = _scheduled(
        title=f"Retest and complete item {served}",
        payload={"action": {"mode": "instruction", "prompt": f"get_work_item {str(served).upper()}"}},
    )
    assert swi.served_work_item_ids(item) == [served]


def test_the_named_work_item_wins_and_the_item_never_serves_itself():
    named = uuid.uuid4()
    item = _scheduled(
        payload={
            "action": {
                "mode": "instruction",
                "prompt": f"see {uuid.uuid4()}",
                swi.SERVES_KEY: str(named),
            }
        }
    )
    assert swi.served_work_item_ids(item) == [named]
    own = _scheduled()
    own.payload = {"action": {"mode": "instruction", "prompt": f"retry {own.id}"}}
    assert swi.served_work_item_ids(own) == []


def test_an_exact_action_serves_the_work_item_its_arguments_name():
    served = uuid.uuid4()
    item = _scheduled(
        payload={
            "action": {
                "mode": "exact",
                "tool": "complete_work_item",
                "arguments": {"work_item_id": str(served), "result": "Retested."},
            }
        }
    )
    assert swi.served_work_item_ids(item) == [served]
    item.payload["action"]["arguments"]["work_item_id"] = str(item.id)
    assert swi.served_work_item_ids(item) == []


def test_a_free_form_argument_naming_an_id_serves_nothing():
    item = _scheduled(
        title="Post the weekly note",
        payload={
            "action": {
                "mode": "exact",
                "tool": "send_message",
                "arguments": {"message": f"see {uuid.uuid4()}", "notes": [str(uuid.uuid4())]},
            }
        },
    )
    assert swi.served_work_item_ids(item) == []


def test_the_reference_list_arguments_serve_their_ids_once():
    first, second = uuid.uuid4(), uuid.uuid4()
    item = _scheduled(
        payload={
            "action": {
                "mode": "exact",
                "tool": "complete_work_items",
                "arguments": {"work_item_ids": [str(first), "c5818ab2", str(second)]},
            }
        }
    )
    item.payload["action"]["arguments"]["parent_work_item_id"] = str(first)
    item.payload["action"]["arguments"]["work_item_ids"].append(str(item.id))
    assert swi.served_work_item_ids(item) == [first, second]


def _exact_with(arguments: dict):
    return _scheduled(
        title="Run the custom tool",
        payload={"action": {"mode": "exact", "tool": "custom_tool", "arguments": arguments}},
    )


def test_a_nested_object_reference_argument_serves_its_id():
    served = uuid.uuid4()
    item = _exact_with({"request": {"work_item_id": str(served)}})
    assert swi.served_work_item_ids(item) == [served]


def test_a_reference_argument_inside_a_list_serves_its_id():
    served = uuid.uuid4()
    item = _exact_with({"items": [{"target_work_item_id": str(served)}, {"note": "x"}]})
    assert swi.served_work_item_ids(item) == [served]
    lookalike = _exact_with({"request": {"notwork_item_id": str(uuid.uuid4())}})
    assert swi.served_work_item_ids(lookalike) == []


def test_nested_free_text_naming_an_id_serves_nothing():
    item = _exact_with({"request": {"message": f"see {uuid.uuid4()}"}})
    assert swi.served_work_item_ids(item) == []


def test_a_structure_deeper_than_the_cap_serves_nothing_and_does_not_raise():
    node: dict = {"work_item_id": str(uuid.uuid4())}
    for _ in range(swi.REFERENCE_SCAN_MAX_DEPTH):
        node = {"next": node}
    assert swi.served_work_item_ids(_exact_with(node)) == []
    looped: dict = {}
    looped["self"] = looped
    assert swi.served_work_item_ids(_exact_with(looped)) == []


def test_the_served_work_item_must_be_an_id():
    action = swi.build_action("instruction", prompt="Retest", serves_work_item_id=str(uuid.uuid4()))
    assert swi.validate_action(action) == "instruction"
    with pytest.raises(swi.ScheduledWorkItemError, match=swi.SERVES_KEY):
        swi.validate_action({**action, swi.SERVES_KEY: "c5818ab2"})


def test_the_bulk_delete_scope_leaves_unsettled_scheduled_items_out():
    for clause in (
        wi_service._deletable_clause(uuid.uuid4(), False),
        wi_service._deletable_clause(uuid.uuid4(), True),
    ):
        sql = str(clause.compile(dialect=postgresql.dialect())).lower()
        assert "action_mode is null" in sql
