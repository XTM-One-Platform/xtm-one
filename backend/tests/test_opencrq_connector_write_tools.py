"""configure_opencrq_connector and run_opencrq_connector send OpenCRQ's own connector writes.

Procedures named relative to ``back/src/features/app-router`` of the OpenCRQ repository:
integrations/integrations-router.ts ``configure``, opencti/opencti-router.ts ``configure`` and
``setEnabled``, connectors/connectors-router.ts ``setSchedule``, ``syncNow``, ``reconnectStream``
and ``resetState``. No gate is added on the XTM One side: OpenCRQ checks the
manage-security capability and its refusal is the tool's answer.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest

from app.integrations.providers.opencrq import _common as opencrq_common
from app.integrations.providers.opencrq import build_tools, connectors
from app.tools.base import CallableToolExecutor, ToolCall

BASE = "https://demo.opencrq.io"
TOKEN = f"{BASE}||test-token"
TENANT = "89c70e9a-8317-4da7-8765-1b9ba004c32c"
ACCOUNT = "0b7c2f7e-1d2a-4c55-9a8e-7f1e2d3c4b5a"


@contextlib.contextmanager
def _recorded(answers: dict[str, Any] | None = None) -> Iterator[list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []
    answers = answers or {}

    async def fake(
        base_url: str,
        headers: Any,
        procedure: str,
        action: str,
        *,
        input: Any = None,
        mutation: bool = False,
        tenant_id: str = "",
        client: Any = None,
    ) -> tuple[Any, Any]:
        calls.append({"procedure": procedure, "input": input, "mutation": mutation, "tenant_id": tenant_id})
        answer = answers.get(procedure)
        if isinstance(answer, str) and answer.startswith("Error:"):
            return None, answer
        return answer, None

    with patch.object(connectors, "_trpc", fake), patch.object(opencrq_common, "_trpc", fake):
        yield calls


async def _run(tool: str, /, **arguments: Any) -> str:
    executor = CallableToolExecutor()
    build_tools(TOKEN, executor)
    result = await executor.execute_tool(ToolCall(id="call-1", name=tool, arguments={"tenant_id": TENANT, **arguments}))
    return result.content


def _writes(calls: list[dict[str, Any]]) -> list[tuple[str, Any]]:
    return [(c["procedure"], c["input"]) for c in calls if c["mutation"]]


def test_both_tools_are_built() -> None:
    executor = CallableToolExecutor()
    names = {t.name for t in build_tools(TOKEN, executor)}
    assert {"configure_opencrq_connector", "run_opencrq_connector"} <= names


@pytest.mark.asyncio
async def test_binds_an_existing_account_through_integrations_configure() -> None:
    with _recorded({"integrations.configure": {"serviceAccountId": ACCOUNT}}) as calls:
        out = await _run("configure_opencrq_connector", source="nist", service_account_id=ACCOUNT)
    assert _writes(calls) == [("integrations.configure", {"source": "nist", "serviceAccountId": ACCOUNT})]
    assert all(c["tenant_id"] == TENANT for c in calls if c["mutation"])
    assert f"service account {ACCOUNT} bound" in out


@pytest.mark.asyncio
async def test_creates_the_account_when_named() -> None:
    with _recorded({"integrations.configure": {"serviceAccountId": ACCOUNT}}) as calls:
        out = await _run("configure_opencrq_connector", source="drata", new_service_account_name="Drata reader")
    assert _writes(calls) == [
        ("integrations.configure", {"source": "drata", "newServiceAccount": {"name": "Drata reader"}})
    ]
    assert "created as Drata reader" in out


@pytest.mark.asyncio
async def test_opencti_binds_with_its_stream_then_switches_on() -> None:
    with _recorded({"opencti.configure": {"serviceAccountId": ACCOUNT}}) as calls:
        out = await _run(
            "configure_opencrq_connector",
            source="opencti",
            service_account_id=ACCOUNT,
            stream_id="live-1",
            enabled=True,
        )
    assert _writes(calls) == [
        ("opencti.configure", {"serviceAccountId": ACCOUNT, "streamId": "live-1"}),
        ("opencti.setEnabled", {"enabled": True}),
    ]
    assert "switched on" in out


@pytest.mark.asyncio
async def test_sets_a_weekly_schedule() -> None:
    with _recorded() as calls:
        out = await _run(
            "configure_opencrq_connector",
            source="openaev",
            cadence="weekly",
            at_minute=120,
            weekday=1,
            time_zone="Europe/Paris",
        )
    assert _writes(calls) == [
        (
            "connectors.setSchedule",
            {"source": "openaev", "cadence": "weekly", "atMinute": 120, "weekday": 1, "timeZone": "Europe/Paris"},
        )
    ]
    assert "schedule weekly" in out


@pytest.mark.asyncio
async def test_hourly_schedule_sends_no_time() -> None:
    with _recorded() as calls:
        await _run("configure_opencrq_connector", source="cisa", cadence="hourly")
    assert _writes(calls) == [
        (
            "connectors.setSchedule",
            {"source": "cisa", "cadence": "hourly", "atMinute": None, "weekday": None, "timeZone": None},
        )
    ]


@pytest.mark.asyncio
async def test_a_refusal_names_what_was_already_applied() -> None:
    with _recorded(
        {
            "integrations.configure": {"serviceAccountId": ACCOUNT},
            "connectors.setSchedule": "Error: OpenCRQ set connector schedule failed: FORBIDDEN",
        }
    ) as calls:
        out = await _run(
            "configure_opencrq_connector",
            source="nist",
            service_account_id=ACCOUNT,
            cadence="hourly",
        )
    assert [p for p, _ in _writes(calls)] == ["integrations.configure", "connectors.setSchedule"]
    assert "FORBIDDEN" in out
    assert "Already applied: service account" in out


@pytest.mark.parametrize(
    "arguments, expected",
    [
        ({"source": "jira"}, "source must be"),
        ({"source": "nist"}, "Nothing to change"),
        ({"source": "nist", "service_account_id": ACCOUNT, "new_service_account_name": "x"}, "not both"),
        ({"source": "opencti", "service_account_id": ACCOUNT}, "needs stream_id"),
        ({"source": "nist", "stream_id": "s"}, "opencti only"),
        ({"source": "nist", "enabled": False}, "no on/off switch"),
        ({"source": "opencti", "cadence": "daily"}, "no run schedule"),
        ({"source": "nist", "cadence": "monthly"}, "cadence must be"),
        ({"source": "nist", "cadence": "daily"}, "needs at_minute"),
        ({"source": "nist", "cadence": "weekly", "at_minute": 5, "time_zone": "UTC"}, "needs weekday"),
        ({"source": "nist", "cadence": "hourly", "at_minute": 5}, "takes no at_minute"),
    ],
)
@pytest.mark.asyncio
async def test_configure_answers_a_wrong_argument_without_writing(arguments: dict[str, Any], expected: str) -> None:
    with _recorded() as calls:
        out = await _run("configure_opencrq_connector", **arguments)
    assert expected in out
    assert _writes(calls) == []


@pytest.mark.parametrize(
    "source, action, procedure",
    [
        ("nist", "sync_now", "connectors.syncNow"),
        ("opencti", "reconnect_stream", "connectors.reconnectStream"),
        ("drata", "reset_state", "connectors.resetState"),
    ],
)
@pytest.mark.asyncio
async def test_run_sends_the_action_procedure(source: str, action: str, procedure: str) -> None:
    with _recorded({procedure: {"jobId": "job-7"} if action == "sync_now" else None}) as calls:
        out = await _run("run_opencrq_connector", source=source, action=action)
    assert _writes(calls) == [(procedure, {"source": source})]
    assert "done" in out
    if action == "sync_now":
        assert "Run id: job-7" in out


@pytest.mark.parametrize(
    "arguments, expected",
    [
        ({"source": "nist", "action": "explode"}, "action must be"),
        ({"source": "opencti", "action": "sync_now"}, "live stream"),
        ({"source": "nist", "action": "reconnect_stream"}, "holds no stream"),
    ],
)
@pytest.mark.asyncio
async def test_run_answers_a_wrong_argument_without_writing(arguments: dict[str, Any], expected: str) -> None:
    with _recorded() as calls:
        out = await _run("run_opencrq_connector", **arguments)
    assert expected in out
    assert _writes(calls) == []
