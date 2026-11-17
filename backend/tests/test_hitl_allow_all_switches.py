"""The two wholesale suppression switches (FR-023, FR-024, FR-025).

These sit ABOVE the per-tool decision table and answer "does anything pause at
all": an administrator's platform-wide kill switch, and one user's own
allow-all. The feature shipped with no tests, so every property below was
unverified: the fail-closed reads, the parity between chat and scheduled runs,
and the recording-only gate that keeps FR-020 true while nothing pauses.

The gate that pauses nothing must still SAY why. Returning no gate at all
would leave a blank outcome, and a blank is reserved for the one state that has
to stay legible — a gate that failed to install.
"""

from __future__ import annotations

import uuid

import pytest

from app.services.hitl_whitelist import (
    build_approval_gate,
    make_recording_gate,
    resolve_gate_suppression,
)
from app.tools.approval import OUTCOME_APPROVALS_DISABLED, OUTCOME_USER_ALLOWS_ALL

_USER = uuid.uuid4()


class _FakeDB:
    """Answers the two scalar reads by the column each statement selects.

    Keyed on the column rather than on call order: the resolver reads the
    platform switch first today, and a test that silently depended on that
    ordering would keep passing if the reads were swapped.
    """

    def __init__(self, *, platform=None, allows_all=None, install_default=None, raises: str | None = None):
        self._values = {
            "tool_approvals_enabled": platform,
            "approve_all_tools": allows_all,
            "approve_tool_calls_default": install_default,
        }
        self._raises = raises
        self.rolled_back = False

    async def scalar(self, stmt):
        column = stmt.column_descriptions[0]["name"]
        if column == self._raises:
            raise RuntimeError(f"{column} is unreadable")
        return self._values[column]

    async def rollback(self):
        self.rolled_back = True


class _Call:
    def __init__(self, call_id="call-1", name="delete_entity"):
        self.id = call_id
        self.name = name


# --- resolve_gate_suppression: the single resolution point (FR-025) --------


@pytest.mark.asyncio
async def test_neither_switch_set_means_the_gate_runs_normally():
    assert await resolve_gate_suppression(_FakeDB(platform=True, allows_all=False), _USER) is None


@pytest.mark.asyncio
async def test_a_missing_settings_row_reads_as_approvals_on():
    """A fresh install has never saved platform settings. The absence must mean
    the default (gating), not "switched off" — the whole switch is safe only
    because its unset state is the safe one."""
    assert await resolve_gate_suppression(_FakeDB(platform=None, allows_all=False), _USER) is None


@pytest.mark.asyncio
async def test_the_platform_kill_switch_suppresses_for_everyone():
    outcome = await resolve_gate_suppression(_FakeDB(platform=False, allows_all=False), _USER)
    assert outcome == OUTCOME_APPROVALS_DISABLED


@pytest.mark.asyncio
async def test_a_users_allow_all_suppresses_their_own_calls():
    outcome = await resolve_gate_suppression(_FakeDB(platform=True, allows_all=True), _USER)
    assert outcome == OUTCOME_USER_ALLOWS_ALL


@pytest.mark.asyncio
async def test_the_platform_switch_names_itself_when_both_are_set():
    """Both suppress, so the question is only which party the trail blames. An
    administrator's policy is the broader fact and must not be recorded as the
    user's choice."""
    outcome = await resolve_gate_suppression(_FakeDB(platform=False, allows_all=True), _USER)
    assert outcome == OUTCOME_APPROVALS_DISABLED


# --- Fail-closed (FR-023) --------------------------------------------------


@pytest.mark.asyncio
async def test_an_unreadable_platform_switch_gates_normally():
    """Not knowing a switch was turned off must never be read as it being off.
    An outage recorded as policy is the one failure mode here."""
    db = _FakeDB(platform=False, raises="tool_approvals_enabled")
    assert await resolve_gate_suppression(db, _USER) is None
    assert db.rolled_back, "a failed read must not leave the session unusable"


@pytest.mark.asyncio
async def test_an_unreadable_user_preference_gates_normally():
    db = _FakeDB(platform=True, allows_all=True, raises="approve_all_tools")
    assert await resolve_gate_suppression(db, _USER) is None
    assert db.rolled_back


# --- Identity shapes the surfaces actually pass ----------------------------


@pytest.mark.asyncio
async def test_an_anonymous_caller_holds_no_preference():
    """The public embed has no user. It must still honour the platform switch,
    and must not crash looking up a preference nobody owns."""
    assert await resolve_gate_suppression(_FakeDB(platform=True, allows_all=True), None) is None
    assert (
        await resolve_gate_suppression(_FakeDB(platform=False, allows_all=True), None)
        == OUTCOME_APPROVALS_DISABLED
    )


@pytest.mark.asyncio
async def test_a_user_id_arriving_as_a_string_is_accepted():
    """Chat passes the id as a string, runs pass a UUID. One of them silently
    matching nothing would un-suppress half the surfaces."""
    outcome = await resolve_gate_suppression(_FakeDB(platform=True, allows_all=True), str(_USER))
    assert outcome == OUTCOME_USER_ALLOWS_ALL


@pytest.mark.asyncio
async def test_a_malformed_user_id_gates_normally():
    assert await resolve_gate_suppression(_FakeDB(platform=True, allows_all=True), "not-a-uuid") is None


# --- The recording-only gate (FR-020, FR-025) ------------------------------


def test_the_recording_gate_never_pauses_but_stamps_the_outcome():
    sink: dict[str, str] = {}
    gate = make_recording_gate(OUTCOME_USER_ALLOWS_ALL, sink)
    assert gate(_Call("call-1")) is None, "a suppressed gate must never pause"
    assert sink == {"call-1": OUTCOME_USER_ALLOWS_ALL}


def test_the_recording_gate_does_not_overwrite_a_recorded_verdict():
    """A resumed call carries a decision a human actually made. Restamping it
    as auto-run would rewrite the trail to say nobody was asked."""
    sink = {"call-1": "approved"}
    make_recording_gate(OUTCOME_APPROVALS_DISABLED, sink)(_Call("call-1"))
    assert sink == {"call-1": "approved"}


def test_the_recording_gate_tolerates_no_sink():
    assert make_recording_gate(OUTCOME_APPROVALS_DISABLED, None)(_Call()) is None


# --- build_approval_gate installs it (FR-025) ------------------------------


@pytest.mark.asyncio
async def test_a_suppressed_gate_is_installed_rather_than_omitted():
    """`None` means "install nothing", which is how a caller pays nothing when
    HITL is off. Suppression must not reuse that signal, or the call runs with
    no outcome at all and looks exactly like a gate that failed to install."""
    sink: dict[str, str] = {}
    gate = await build_approval_gate(
        _FakeDB(platform=False),
        None,
        user_id=_USER,
        hitl_enabled=True,
        outcome_sink=sink,
    )
    assert gate is not None
    gate(_Call("call-9"))
    assert sink == {"call-9": OUTCOME_APPROVALS_DISABLED}


@pytest.mark.asyncio
async def test_hitl_off_reads_only_the_platform_switch():
    """With HITL off the user's allow-all is never read: there is nothing for it
    to suppress. The platform switch is, because it alone silences the tools
    that always ask; approvals available, nothing is recorded."""

    class _PlatformOnly:
        reads = 0

        async def scalar(self, _stmt):
            self.reads += 1
            if self.reads > 1:
                raise AssertionError("only the platform switch is read when HITL is off")
            return True

    db = _PlatformOnly()
    sink: dict[str, str] = {}
    gate = await build_approval_gate(db, None, user_id=_USER, hitl_enabled=False, outcome_sink=sink)
    assert gate(_Call("call-1")) is None
    assert sink == {}
    assert db.reads == 1


@pytest.mark.asyncio
async def test_hitl_off_with_the_platform_switch_off_records_why_each_call_ran():
    sink: dict[str, str] = {}
    gate = await build_approval_gate(
        _FakeDB(platform=False, allows_all=False), None, user_id=_USER, hitl_enabled=False, outcome_sink=sink
    )
    assert gate(_Call("call-1")) is None
    assert sink == {"call-1": OUTCOME_APPROVALS_DISABLED}


# --- Parity between the entry points (FR-025) ------------------------------


def test_chat_and_scheduled_runs_resolve_the_switches_at_the_same_point():
    """The defect this closes: the assignment engine builds its gate inline, so
    a switch honoured in chat and missed in a scheduled run would be invisible
    until an unattended run paused that should not have."""
    import inspect

    from app.services import assignment_engine, hitl_whitelist

    assert "resolve_gate_suppression" in inspect.getsource(hitl_whitelist.build_approval_gate)
    engine = inspect.getsource(assignment_engine)
    assert "resolve_gate_suppression" in engine
    assert "make_recording_gate" in engine
    for column in ("tool_approvals_enabled", "approve_all_tools"):
        assert column not in engine, (
            f"the engine reads {column} itself — that inline copy is exactly "
            "what drifts; there must be one resolver"
        )


def test_request_human_input_is_never_gated_and_so_never_suppressed():
    """FR-023 exempts the agent's own question from these switches. It holds
    because the tool ships on the default whitelist, so it was never gated —
    not because suppression special-cases it."""
    from app.services.hitl_whitelist_default import SHIPPED_DEFAULT_WHITELIST

    assert "request_human_input" in SHIPPED_DEFAULT_WHITELIST


# --- The endpoints that set them (FR-023, FR-024, FR-025) ------------------


class _ApiDB(_FakeDB):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.committed = False
        self.deleted: list = []

    async def delete(self, obj):
        self.deleted.append(obj)

    async def commit(self):
        self.committed = True


@pytest.fixture
def audited(monkeypatch):
    """Capture audit writes; both switches must leave a trail (FR-025)."""
    from app.api.v1 import tool_approvals as api

    entries: list[dict] = []

    async def _log(_db, **kw):
        entries.append(kw)

    monkeypatch.setattr(api, "log_audit", _log)
    return entries


@pytest.mark.asyncio
async def test_enabling_allow_all_keeps_the_standing_preferences(audited):
    """FR-024: the per-tool rows are moot while it is on, not gone. Deleting
    them would make switching back a silent downgrade — the user would return
    to being asked about tools they had already trusted."""
    from types import SimpleNamespace

    from app.api.v1.tool_approvals import ApprovalModeRequest, set_my_approval_mode

    user = SimpleNamespace(id=_USER, approve_all_tools=False, impersonated_by=None)
    db = _ApiDB()

    result = await set_my_approval_mode(
        body=ApprovalModeRequest(approve_all_tools=True),
        request=SimpleNamespace(client=None, headers={}),
        user=user,
        db=db,
    )

    assert result == {"approve_all_tools": True}
    assert user.approve_all_tools is True
    assert db.deleted == [], "standing preferences must survive the switch"
    assert db.committed
    assert len(audited) == 1
    assert audited[0]["details"]["approve_all_tools"] is True


@pytest.mark.asyncio
async def test_setting_allow_all_to_its_current_value_writes_no_audit_entry(audited):
    """An unchanged setting is not an event. A trail padded with no-ops is one
    a reader stops reading."""
    from types import SimpleNamespace

    from app.api.v1.tool_approvals import ApprovalModeRequest, set_my_approval_mode

    user = SimpleNamespace(id=_USER, approve_all_tools=True, impersonated_by=None)
    await set_my_approval_mode(
        body=ApprovalModeRequest(approve_all_tools=True),
        request=SimpleNamespace(client=None, headers={}),
        user=user,
        db=_ApiDB(),
    )
    assert audited == []


@pytest.mark.asyncio
async def test_the_profile_read_reports_both_switches(monkeypatch):
    """The screen has to explain why nothing pauses even when the user's own
    mode is off — otherwise an administrator's kill switch looks like a bug."""
    from types import SimpleNamespace

    from app.api.v1 import tool_approvals as api

    async def _none(_db, _user_id):
        return []

    async def _not_in_use(_db):
        return False

    monkeypatch.setattr(api, "list_preferences", _none)
    monkeypatch.setattr(api, "list_recently_invalidated", _none)
    monkeypatch.setattr(api, "approvals_in_use", _not_in_use)

    payload = await api.list_my_tool_approvals(
        user=SimpleNamespace(id=_USER, approve_all_tools=True),
        db=_ApiDB(platform=False, install_default=False),
    )
    assert payload["approve_all_tools"] is True
    assert payload["tool_approvals_enabled"] is False
    assert payload["approve_tool_calls_default"] is False
    assert payload["approvals_in_use"] is False


@pytest.mark.asyncio
async def test_a_missing_settings_row_reports_approvals_as_on(monkeypatch):
    from types import SimpleNamespace

    from app.api.v1 import tool_approvals as api

    async def _none(_db, _user_id):
        return []

    async def _not_in_use(_db):
        return False

    monkeypatch.setattr(api, "list_preferences", _none)
    monkeypatch.setattr(api, "list_recently_invalidated", _none)
    monkeypatch.setattr(api, "approvals_in_use", _not_in_use)

    payload = await api.list_my_tool_approvals(
        user=SimpleNamespace(id=_USER, approve_all_tools=False),
        db=_ApiDB(platform=None),
    )
    assert payload["tool_approvals_enabled"] is True
    # Opt-in: no row yet reads as the default its row is created with, off.
    assert payload["approve_tool_calls_default"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["false", "true", 0, 1, None, "off"])
async def test_the_platform_switch_refuses_anything_that_is_not_a_boolean(
    monkeypatch, value
):
    """`"false"` is truthy. Coercing it would switch OFF the one control that
    pauses destructive tools, on a payload that looks like it asked for the
    opposite."""
    from types import SimpleNamespace

    from fastapi import HTTPException

    from app.api.v1 import platform_settings_helpers
    from app.api.v1 import settings as settings_api

    ps = SimpleNamespace(tool_approvals_enabled=True)

    async def _load(_db):
        return ps

    monkeypatch.setattr(platform_settings_helpers, "get_platform_settings", _load)

    with pytest.raises(HTTPException) as excinfo:
        await settings_api.update_platform_settings_endpoint(
            body={"tool_approvals_enabled": value},
            request=SimpleNamespace(client=None, headers={}),
            user=SimpleNamespace(id=_USER, role="admin"),
            db=_ApiDB(),
        )

    assert excinfo.value.status_code == 400
    assert ps.tool_approvals_enabled is True, "the stored value must be untouched"
