"""The live agentic-action counter of the web chat.

A chat turn is charged one agentic action per metered request, before its agent
loop runs. The composer's quota counter shows that charge as the in-progress
``(+N)`` segment, so both chat runners announce it to the turn's live viewers
the moment it happens: an ``agentic_action`` event through ``on_event``, which
the WS handler forwards as a frame type of its own and the turn relay fans out
and journals as a running total (a resumed socket replays it). Nothing is
announced when nothing was charged, the OpenCTI / OpenAEV embed never receives
it (``test_platform_chat_stream_filter``), and every client released before it
ignores the frame, where a ``status`` would land on their working label.
"""

from __future__ import annotations

import pathlib
import uuid
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.services import agent_chat
from app.services.agent_chat import AGENTIC_ACTION_STATUS, announce_agentic_action

FRONTEND = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "src"
CHARGED = {"actions_consumed": 1, "reason": "charged"}
FRAME = {"status": AGENTIC_ACTION_STATUS, "actions": 1}


class _Stop(BaseException):
    """Ends a runner the moment the frame under test is emitted.

    A ``BaseException`` on purpose: the announce swallows delivery failures
    (``Exception``) and must not swallow the test's stop.
    """


def _recorder(events: list[dict], *, stop_on_frame: bool = False):
    async def _on_event(event: dict) -> None:
        events.append(event)
        if stop_on_frame and event.get("status") == AGENTIC_ACTION_STATUS:
            raise _Stop

    return _on_event


class TestAnnounce:
    async def test_a_charge_is_announced_once(self):
        events: list[dict] = []
        await announce_agentic_action(_recorder(events), dict(CHARGED))
        assert events == [FRAME]

    @pytest.mark.parametrize(
        "quota_out",
        [
            pytest.param({}, id="not-metered-at-all"),
            pytest.param({"actions_consumed": 0, "reason": "metered_by_parent"}, id="nested-handover"),
            pytest.param({"actions_consumed": 0, "reason": "quota_exceeded"}, id="refused"),
            pytest.param({"actions_consumed": 0, "reason": "unmetered_error"}, id="quota-backend-down"),
        ],
    )
    async def test_nothing_is_announced_when_nothing_was_charged(self, quota_out):
        events: list[dict] = []
        await announce_agentic_action(_recorder(events), quota_out)
        assert events == []

    async def test_nobody_listening_is_a_no_op(self):
        await announce_agentic_action(None, dict(CHARGED))

    async def test_a_delivery_failure_never_breaks_the_turn(self):
        async def _broken(_event: dict) -> None:
            raise RuntimeError("socket gone")

        await announce_agentic_action(_broken, dict(CHARGED))


class TestPeriod:
    """A charge names the quota period it was counted in, so a turn running past
    a period rollover never counts yesterday's action in today's total."""

    async def test_the_charge_returns_the_period_it_was_counted_in(self, monkeypatch):
        from unittest.mock import AsyncMock

        from app.services import quota as quota_module

        monkeypatch.setattr(quota_module, "redis_client", AsyncMock(return_value=object()))
        monkeypatch.setattr(
            quota_module, "_resolve_agentic_periods", AsyncMock(return_value=("daily", []))
        )
        monkeypatch.setattr(quota_module, "_count_global", AsyncMock())
        monkeypatch.setattr(quota_module, "_merge_legacy_usage_key", AsyncMock())
        monkeypatch.setattr(quota_module, "_increment", AsyncMock(return_value=1))

        class _FrozenClock(datetime):
            """The last second of a day: a key read twice here would span two days."""

            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 9, 27, 23, 59, 59, tzinfo=tz)

        monkeypatch.setattr(quota_module, "datetime", _FrozenClock)
        period = await quota_module.increment_agentic_usage(uuid.uuid4())
        assert period == "2026-09-27"

    @pytest.mark.parametrize(
        ("charged_period", "frame"),
        [
            pytest.param("2026-09-27", {**FRAME, "period": "2026-09-27"}, id="named"),
            pytest.param(None, FRAME, id="unknown"),
        ],
    )
    async def test_metering_records_the_period_and_the_announce_carries_it(
        self, monkeypatch, charged_period, frame
    ):
        from unittest.mock import AsyncMock

        from app.services import quota as quota_module

        monkeypatch.setattr(agent_chat, "record_agentic_execution", AsyncMock())
        monkeypatch.setattr(quota_module, "check_agentic_quota", AsyncMock())
        monkeypatch.setattr(
            quota_module, "increment_agentic_usage", AsyncMock(return_value=charged_period)
        )
        out: dict = {}
        assert await agent_chat.enforce_agentic_quota_ex(
            None, str(uuid.uuid4()), quota_out=out
        ) == (None, True)
        assert out.get("period") == charged_period
        events: list[dict] = []
        await announce_agentic_action(_recorder(events), out)
        assert events == [frame]


def _agent():
    return SimpleNamespace(id=uuid.uuid4(), name="Counter")


async def _run_agent_turn(on_event):
    from app.tools.base import FileContext

    return await agent_chat.run_agent_chat(
        None,
        str(uuid.uuid4()),
        _agent(),
        [],
        "hello",
        on_event=on_event,
        file_context=FileContext(),
    )


class TestAgentChat:
    async def test_a_turn_announces_its_charge_right_after_metering(self, monkeypatch):
        async def _charged(_db, _user_id, *, quota_out=None, **_kwargs):
            quota_out.update(CHARGED)
            return None, True

        monkeypatch.setattr(agent_chat, "enforce_agentic_quota_ex", _charged)
        events: list[dict] = []
        with pytest.raises(_Stop):
            await _run_agent_turn(_recorder(events, stop_on_frame=True))
        assert events == [FRAME]

    async def test_a_refused_turn_announces_nothing(self, monkeypatch):
        async def _refused(_db, _user_id, *, quota_out=None, **_kwargs):
            quota_out.update({"actions_consumed": 0, "reason": "quota_exceeded"})
            return agent_chat.QUOTA_EXCEEDED_MESSAGE, False

        monkeypatch.setattr(agent_chat, "enforce_agentic_quota_ex", _refused)
        events: list[dict] = []
        reply, _ = await _run_agent_turn(_recorder(events))
        assert reply == agent_chat.QUOTA_EXCEEDED_MESSAGE
        assert events == []


async def _run_general_turn(on_event):
    from app.services import general_chat

    return await general_chat.run_general_chat(
        None, str(uuid.uuid4()), [], "hello", on_event=on_event
    )


class TestGeneralChat:
    async def test_a_turn_announces_its_charge_right_after_metering(self, monkeypatch):
        async def _charged(_db, _user_id, *, quota_out=None, **_kwargs):
            quota_out.update(CHARGED)
            return None

        monkeypatch.setattr(agent_chat, "enforce_agentic_quota", _charged)
        events: list[dict] = []
        with pytest.raises(_Stop):
            await _run_general_turn(_recorder(events, stop_on_frame=True))
        assert events == [FRAME]

    async def test_a_refused_turn_announces_nothing(self, monkeypatch):
        async def _refused(_db, _user_id, *, quota_out=None, **_kwargs):
            quota_out.update({"actions_consumed": 0, "reason": "quota_exceeded"})
            return agent_chat.QUOTA_EXCEEDED_MESSAGE

        monkeypatch.setattr(agent_chat, "enforce_agentic_quota", _refused)
        events: list[dict] = []
        reply, _ = await _run_general_turn(_recorder(events))
        assert reply == agent_chat.QUOTA_EXCEEDED_MESSAGE
        assert events == []


class TestRelay:
    def test_the_charge_reaches_every_viewer_as_a_frame_of_its_own(self):
        # How a resume replays it (a total kept out of the trimmed status
        # list) is pinned next to the journal, in ``test_chat_turn_fanout``.
        from app.services import chat_turn_stream

        assert AGENTIC_ACTION_STATUS == chat_turn_stream.AGENTIC_ACTION_STATUS
        frame = chat_turn_stream.event_frame(FRAME)
        assert frame == {"type": chat_turn_stream.AGENTIC_ACTION_FRAME, "actions": 1}
        assert frame["type"] in chat_turn_stream.RELAYED_FRAME_TYPES
        # Every client released before it (the web chat, the Office add-in,
        # the voice dialog) ignores a frame type it does not know; a status
        # would land on their working label.
        assert frame["type"] != "status"
        assert chat_turn_stream.event_frame({"status": "tool_start"}) == {
            "type": "status",
            "status": "tool_start",
        }


class TestWebClient:
    def test_the_frame_is_spelled_the_same_on_both_sides(self):
        from app.services import chat_turn_stream

        source = (FRONTEND / "lib" / "agentic-usage.ts").read_text(encoding="utf-8")
        assert f'export const AGENTIC_ACTION_FRAME = "{chat_turn_stream.AGENTIC_ACTION_FRAME}";' in source

    def test_the_stream_reads_it_as_a_frame_of_its_own(self):
        # The panel's turn frames reach the page through ``useTurnFrames``: the
        # charge is counted and the frame goes no further, so it never lands
        # on the working label as a status would.
        hook = (FRONTEND / "pages" / "chat-panel" / "useTurnFrames.ts").read_text(encoding="utf-8")
        charge = hook[hook.index("if (type === AGENTIC_ACTION_FRAME) {") :]
        charge = charge[: charge.index("\n      }\n")]
        assert "setTurnCharges((prev) => addTurnCharge(prev, charge));" in charge
        assert "return;" in charge
        assert "AGENTIC_ACTION" not in hook[hook.index("const status = frame.status;") :]

    def test_a_relayed_turn_keeps_its_charges_and_a_late_charge_rereads_the_total(self):
        # A reloaded page or a second tab follows a turn another view runs:
        # its charges are flagged relayed. A charge landing after the turn
        # ended (a Stop does not refund it) still re-reads the total.
        hook = (FRONTEND / "pages" / "chat-panel" / "useTurnFrames.ts").read_text(encoding="utf-8")
        charge = hook[hook.index("if (type === AGENTIC_ACTION_FRAME) {") :]
        charge = charge[: charge.index("\n      }\n")]
        assert "info.relayed || frame.relayed === true," in charge
        assert 'void refetchAfterCharge(qc, ["my-quotas"]);' in charge

    def test_every_turn_end_refreshes_the_counter(self):
        hook = (FRONTEND / "pages" / "chat-panel" / "useTurnFrames.ts").read_text(encoding="utf-8")
        # The panel says when its turn ends (``onTurnRunningChange``).
        end = hook[hook.index("const onTurnRunningChange = useCallback(") :]
        end = end[: end.index("TURN_END_REFETCH_DELAY_MS);")]
        assert "setTurnCharges(NO_TURN_CHARGES);" in end
        assert 'void qc.invalidateQueries({ queryKey: ["my-quotas"] });' in end
        # Which frames end a turn is the panel's rule (done, error, cancelled,
        # session_expired), never re-derived by the page.
        assert "function endsTurn(" not in hook
