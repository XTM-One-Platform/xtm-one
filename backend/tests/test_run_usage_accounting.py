"""Tests for run usage accounting and quota metering (OBS-3 / OBS-5 / OBS-6).

Covers three invariants from the observability assessment:

* **OBS-3** — nested handover calls (consult / transfer) must NOT re-meter
  the agentic quota: the parent request was already checked and incremented,
  so one user turn that fans out to N consults must count as exactly one
  quota unit.  Background sub-agent tasks (``sub_agent_task`` handover type)
  keep metering — they are real autonomous work units.
* **OBS-5 / OBS-6** — after the agent loop completes, the accumulated token
  usage and the Langfuse trace id are persisted on the ``AssignmentRun``.
* Accounting is best-effort: a failure inside the accounting helper must
  never fail the run.
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.agent_chat import (
    QUOTA_EXCEEDED_MESSAGE,
    enforce_agentic_quota,
    enforce_agentic_quota_ex,
)
from app.services.assignment_engine import apply_run_usage_accounting
from app.services.langfuse_tracing import get_trace_id, usage_token_totals
from app.services.quota import QuotaExceededError

SERVICES_DIR = Path(__file__).resolve().parents[1] / "app" / "services"

# Sentinel standing in for the AsyncSession contract of
# ``enforce_agentic_quota`` — the monkeypatched quota functions ignore it,
# but passing a real object keeps the tests aligned with the production
# signature (no ``db=None`` drift).
DB_SENTINEL = cast(AsyncSession, SimpleNamespace(name="db-sentinel"))


# ---------------------------------------------------------------------------
# OBS-3: quota metering guard
# ---------------------------------------------------------------------------


def _patch_quota(monkeypatch) -> dict[str, int]:
    """Replace the quota service with call recorders; return the counters."""
    calls = {"check": 0, "increment": 0}

    async def _check(_db, _uid):
        calls["check"] += 1

    # ``receipt_token`` mirrors the production signature: a charge that names a
    # work item (a background sub-agent task) leaves a durable receipt so the
    # orchestration engine can credit the action back if it abandons that work.
    # A stub missing the kwarg makes the metering block look like a quota-backend
    # outage, which is a confusing way to fail.
    async def _increment(
        _uid, period="monthly", db=None, agent_id=None, receipt_token=None
    ):
        calls["increment"] += 1

    monkeypatch.setattr("app.services.quota.check_agentic_quota", _check)
    monkeypatch.setattr("app.services.quota.increment_agentic_usage", _increment)
    return calls


@pytest.mark.asyncio
async def test_meter_quota_false_makes_no_quota_calls(monkeypatch):
    """A nested handover (meter_quota=False) must not check or increment."""
    calls = _patch_quota(monkeypatch)

    result = await enforce_agentic_quota(
        DB_SENTINEL, str(uuid.uuid4()), meter_quota=False
    )

    assert result is None
    assert calls == {"check": 0, "increment": 0}


@pytest.mark.asyncio
async def test_meter_quota_true_increments_exactly_once(monkeypatch):
    calls = _patch_quota(monkeypatch)

    result = await enforce_agentic_quota(DB_SENTINEL, str(uuid.uuid4()))

    assert result is None
    assert calls == {"check": 1, "increment": 1}


@pytest.mark.asyncio
async def test_quota_exceeded_returns_refusal_without_increment(monkeypatch):
    calls = {"increment": 0}

    async def _check(_db, _uid):
        raise QuotaExceededError("agentic", 10, 10, "monthly")

    # ``receipt_token`` mirrors the production signature: a charge that names a
    # work item (a background sub-agent task) leaves a durable receipt so the
    # orchestration engine can credit the action back if it abandons that work.
    # A stub missing the kwarg makes the metering block look like a quota-backend
    # outage, which is a confusing way to fail.
    async def _increment(
        _uid, period="monthly", db=None, agent_id=None, receipt_token=None
    ):
        calls["increment"] += 1

    monkeypatch.setattr("app.services.quota.check_agentic_quota", _check)
    monkeypatch.setattr("app.services.quota.increment_agentic_usage", _increment)

    result = await enforce_agentic_quota(DB_SENTINEL, str(uuid.uuid4()))

    assert result == QUOTA_EXCEEDED_MESSAGE
    assert calls["increment"] == 0


@pytest.mark.asyncio
async def test_quota_backend_failure_proceeds_unmetered(monkeypatch):
    """Copilot mode: a quota backend failure must not block execution
    (fail-open + log)."""

    async def _check(_db, _uid):
        raise RuntimeError("redis down")

    monkeypatch.setattr("app.core.config.settings.platform_mode", "copilot")
    monkeypatch.setattr("app.services.quota.check_agentic_quota", _check)

    result = await enforce_agentic_quota(DB_SENTINEL, str(uuid.uuid4()))

    assert result is None


@pytest.mark.asyncio
async def test_quota_backend_failure_refuses_in_xtm_one(monkeypatch):
    """XTM One mode: quotas are what the licence sells, so a quota backend
    failure refuses the request whatever QUOTA_FAIL_MODE says."""
    from app.services.quota import QUOTA_UNAVAILABLE_MESSAGE

    async def _check(_db, _uid):
        raise RuntimeError("redis down")

    monkeypatch.setattr("app.core.config.settings.platform_mode", "xtm_one")
    monkeypatch.setattr("app.core.config.settings.quota_fail_mode", "open")
    monkeypatch.setattr("app.services.quota.check_agentic_quota", _check)
    out: dict = {}

    refusal, metered = await enforce_agentic_quota_ex(DB_SENTINEL, str(uuid.uuid4()), quota_out=out)

    assert (refusal, metered) == (QUOTA_UNAVAILABLE_MESSAGE, False)
    assert out == {"actions_consumed": 0, "reason": "quota_unavailable"}


@pytest.mark.parametrize(("platform_mode", "fail_mode"), [("xtm_one", "open"), ("copilot", "closed")])
@pytest.mark.asyncio
async def test_an_accountless_user_id_is_charged_platform_wide_never_taken_for_a_store_failure(
    monkeypatch, platform_mode, fail_mode
):
    """The global MCP endpoint's platform key runs as ``mcp-global``: no
    account, so the platform-wide tiers alone are checked and charged -
    and never refused as if the quota store were down, which quotas
    failing closed would do."""
    seen: list[tuple[str, object]] = []

    async def _check(_db, uid):
        seen.append(("check", uid))

    async def _increment(uid, period="monthly", db=None, agent_id=None, receipt_token=None):
        seen.append(("increment", uid))

    monkeypatch.setattr("app.services.quota.check_agentic_quota", _check)
    monkeypatch.setattr("app.services.quota.increment_agentic_usage", _increment)
    monkeypatch.setattr("app.core.config.settings.platform_mode", platform_mode)
    monkeypatch.setattr("app.core.config.settings.quota_fail_mode", fail_mode)
    out: dict = {}

    refusal, metered = await enforce_agentic_quota_ex(DB_SENTINEL, "mcp-global", quota_out=out)

    assert (refusal, metered) == (None, True)
    assert out == {"actions_consumed": 1, "reason": "charged_no_account"}
    assert seen == [("check", None), ("increment", None)]


@pytest.mark.asyncio
async def test_evaluation_skips_quota_and_accountless_turns_are_metered(monkeypatch):
    calls = _patch_quota(monkeypatch)

    assert (
        await enforce_agentic_quota(
            DB_SENTINEL, str(uuid.uuid4()), evaluation_mode=True
        )
        is None
    )
    assert calls == {"check": 0, "increment": 0}

    # No account behind the turn: the platform-wide tiers still apply.
    assert await enforce_agentic_quota(DB_SENTINEL, "anonymous") is None
    assert await enforce_agentic_quota(DB_SENTINEL, None) is None
    assert calls == {"check": 2, "increment": 2}


# ---------------------------------------------------------------------------
# quota_out reporting — consumption attached to trace metadata for
# observability (Trace Explorer platform-action display)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quota_out_reports_charged(monkeypatch):
    _patch_quota(monkeypatch)
    out: dict = {}

    result = await enforce_agentic_quota(
        DB_SENTINEL, str(uuid.uuid4()), quota_out=out
    )

    assert result is None
    assert out == {"actions_consumed": 1, "reason": "charged"}


@pytest.mark.asyncio
async def test_quota_out_reports_metered_by_parent(monkeypatch):
    _patch_quota(monkeypatch)
    out: dict = {}

    result = await enforce_agentic_quota(
        DB_SENTINEL, str(uuid.uuid4()), meter_quota=False, quota_out=out
    )

    assert result is None
    assert out == {"actions_consumed": 0, "reason": "metered_by_parent"}


@pytest.mark.asyncio
async def test_quota_out_reports_quota_exceeded(monkeypatch):
    async def _check(_db, _uid):
        raise QuotaExceededError("agentic", 10, 10, "monthly")

    monkeypatch.setattr("app.services.quota.check_agentic_quota", _check)
    out: dict = {}

    result = await enforce_agentic_quota(
        DB_SENTINEL, str(uuid.uuid4()), quota_out=out
    )

    assert result == QUOTA_EXCEEDED_MESSAGE
    assert out == {"actions_consumed": 0, "reason": "quota_exceeded"}


@pytest.mark.asyncio
async def test_quota_out_reports_unmetered_backend_error(monkeypatch):
    async def _check(_db, _uid):
        raise RuntimeError("redis down")

    # Fail-open exists in Copilot / dev mode only.
    monkeypatch.setattr("app.core.config.settings.platform_mode", "copilot")
    monkeypatch.setattr("app.services.quota.check_agentic_quota", _check)
    out: dict = {}

    result = await enforce_agentic_quota(
        DB_SENTINEL, str(uuid.uuid4()), quota_out=out
    )

    assert result is None
    assert out == {"actions_consumed": 0, "reason": "unmetered_error"}


@pytest.mark.asyncio
async def test_quota_out_reports_anonymous_and_evaluation(monkeypatch):
    _patch_quota(monkeypatch)

    out_anon: dict = {}
    await enforce_agentic_quota(DB_SENTINEL, "anonymous", quota_out=out_anon)
    assert out_anon == {"actions_consumed": 1, "reason": "charged_no_account"}

    out_eval: dict = {}
    await enforce_agentic_quota(
        DB_SENTINEL, str(uuid.uuid4()), evaluation_mode=True, quota_out=out_eval
    )
    assert out_eval == {"actions_consumed": 0, "reason": "evaluation"}


# ---------------------------------------------------------------------------
# OBS-3: source-level pin — every nested consult/transfer call site must
# pass meter_quota=False (sub_agent_task handovers keep the default True).
# ---------------------------------------------------------------------------

_NESTED_HANDOVER_TYPES = {"consult", "transfer"}
_CALL_SITE_FILES = ("agent_chat.py", "assignment_engine.py", "general_chat.py")


def _run_agent_chat_calls(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        # ``_run_helper_turn`` is agent_chat's pass-through to ``run_agent_chat``
        # for consult and transfer helpers, which keeps their ledger.
        if name in ("run_agent_chat", "_run_helper_turn"):
            yield node


def _handover_type(call: ast.Call) -> str | None:
    """Return the literal ``"type"`` value of a handover_context dict kwarg."""
    for kw in call.keywords:
        if kw.arg != "handover_context" or not isinstance(kw.value, ast.Dict):
            continue
        for key, value in zip(kw.value.keys, kw.value.values, strict=True):
            if (
                isinstance(key, ast.Constant)
                and key.value == "type"
                and isinstance(value, ast.Constant)
            ):
                return value.value
    return None


def test_nested_handover_call_sites_disable_quota_metering():
    """Every consult/transfer ``run_agent_chat`` call passes meter_quota=False.

    Pins OBS-3 at the source level: a future nested handover call site that
    forgets ``meter_quota=False`` will trip this assertion at CI time.
    """
    nested_sites = 0
    for filename in _CALL_SITE_FILES:
        path = SERVICES_DIR / filename
        for call in _run_agent_chat_calls(path):
            handover_type = _handover_type(call)
            if handover_type not in _NESTED_HANDOVER_TYPES:
                continue
            nested_sites += 1
            meter_kw = next(
                (kw for kw in call.keywords if kw.arg == "meter_quota"), None
            )
            assert (
                meter_kw is not None
                and isinstance(meter_kw.value, ast.Constant)
                and meter_kw.value.value is False
            ), (
                f"{filename}: nested '{handover_type}' run_agent_chat call at "
                f"line {call.lineno} must pass meter_quota=False — the parent "
                "request was already metered (OBS-3)"
            )
    # 2 sites each in agent_chat.py, assignment_engine.py, general_chat.py.
    assert nested_sites >= 6, (
        f"expected at least 6 nested handover call sites, found {nested_sites} "
        "— did a consult/transfer call site move?"
    )


def test_sub_agent_background_tasks_keep_metering():
    """Background sub-agent tasks are real autonomous work — they must NOT
    pass meter_quota=False."""
    path = SERVICES_DIR / "sub_agent.py"
    sub_agent_sites = 0
    for call in _run_agent_chat_calls(path):
        if _handover_type(call) != "sub_agent_task":
            continue
        sub_agent_sites += 1
        meter_kw = next(
            (kw for kw in call.keywords if kw.arg == "meter_quota"), None
        )
        assert meter_kw is None, (
            "sub_agent.py: background task run_agent_chat call must keep the "
            "default meter_quota=True (autonomous work units are metered)"
        )
    assert sub_agent_sites >= 1


# ---------------------------------------------------------------------------
# OBS-5 / OBS-6: token + trace id persistence on AssignmentRun
# ---------------------------------------------------------------------------


class _FakeSpan:
    def __init__(self, trace_id: str | None = None, span_id: str | None = None):
        if trace_id is not None:
            self.trace_id = trace_id
        if span_id is not None:
            self.id = span_id


def _fake_run() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        langfuse_trace_id=None,
        llm_trace_id=None,
        total_input_tokens=None,
        total_output_tokens=None,
    )


def test_accounting_persists_tokens_and_trace_id_anthropic_keys():
    run = _fake_run()
    usage = {
        "input_tokens": 100,
        "output_tokens": 25,
        "cache_read_input_tokens": 50,
    }
    ctx = {"trace": _FakeSpan(trace_id="tr-abc123")}

    apply_run_usage_accounting(run, usage, ctx)

    # Cache tokens count toward input (mirrors Langfuse cost accounting).
    assert run.total_input_tokens == 150
    assert run.total_output_tokens == 25
    assert run.langfuse_trace_id == "tr-abc123"
    assert run.llm_trace_id is None


def test_accounting_persists_tokens_openai_keys():
    run = _fake_run()
    usage = {"prompt_tokens": 200, "completion_tokens": 40}

    apply_run_usage_accounting(run, usage, None)

    assert run.total_input_tokens == 200
    assert run.total_output_tokens == 40
    assert run.langfuse_trace_id is None
    assert run.llm_trace_id is None


def test_accounting_noop_without_usage_or_tracing():
    run = _fake_run()

    apply_run_usage_accounting(run, None, None)

    assert run.total_input_tokens is None
    assert run.total_output_tokens is None
    assert run.langfuse_trace_id is None
    assert run.llm_trace_id is None


def test_accounting_trace_id_falls_back_to_span_id():
    assert get_trace_id({"trace": _FakeSpan(span_id="obs-42")}) == "obs-42"
    assert get_trace_id({"trace": None}) is None
    assert get_trace_id(None) is None


def test_usage_token_totals_handles_empty():
    assert usage_token_totals(None) == (0, 0)
    assert usage_token_totals({}) == (0, 0)


class _ExplodingUsage:
    """Usage object whose access raises — simulates a malformed provider dict."""

    def __bool__(self) -> bool:
        return True

    def get(self, *_args, **_kwargs):
        raise RuntimeError("boom")


def test_accounting_failure_never_raises():
    """Accounting must be best-effort: a failure inside the helper must not
    propagate (it would otherwise fail an already-completed run)."""
    run = _fake_run()

    apply_run_usage_accounting(run, _ExplodingUsage(), {"trace": _FakeSpan("t")})

    assert run.total_input_tokens is None
    assert run.total_output_tokens is None
    # The trace id is persisted BEFORE usage normalization, so a malformed
    # provider usage dict must not lose the deep link (it matters most
    # exactly when something else went wrong).
    assert run.langfuse_trace_id == "t"
    assert run.llm_trace_id is None


def test_accounting_persists_local_trace_id() -> None:
    run = _fake_run()
    local_id = uuid.uuid4()

    apply_run_usage_accounting(
        run,
        {"prompt_tokens": 1, "completion_tokens": 1},
        {"local_trace_id": local_id},
    )

    assert run.llm_trace_id == local_id


# ---------------------------------------------------------------------------
# quota_out vs the ``metered`` flag: the two signals come out of the same gate
# (execution telemetry + quota) and must never disagree, or the Trace Explorer
# would claim an action was charged that the refund path did not add back.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "patch_check", "platform_mode", "expected_metered", "expected_reason"),
    [
        ({}, None, None, True, "charged"),
        ({"meter_quota": False}, None, None, False, "metered_by_parent"),
        ({"evaluation_mode": True}, None, None, False, "evaluation"),
        ({}, "exceeded", None, False, "quota_exceeded"),
        ({}, "boom", "copilot", False, "unmetered_error"),
        ({}, "boom", "xtm_one", False, "quota_unavailable"),
    ],
)
@pytest.mark.asyncio
async def test_quota_out_actions_consumed_agrees_with_metered_flag(
    monkeypatch, kwargs, patch_check, platform_mode, expected_metered, expected_reason
):
    _patch_quota(monkeypatch)
    if platform_mode is not None:
        monkeypatch.setattr("app.core.config.settings.platform_mode", platform_mode)

    if patch_check == "exceeded":

        async def _check(_db, _uid):
            raise QuotaExceededError("agentic", 10, 10, "monthly")

        monkeypatch.setattr("app.services.quota.check_agentic_quota", _check)
    elif patch_check == "boom":

        async def _check(_db, _uid):
            raise RuntimeError("redis down")

        monkeypatch.setattr("app.services.quota.check_agentic_quota", _check)

    out: dict = {}
    _, metered = await enforce_agentic_quota_ex(
        DB_SENTINEL, str(uuid.uuid4()), quota_out=out, **kwargs
    )

    assert metered is expected_metered
    assert out["reason"] == expected_reason
    # The invariant: 1 action recorded iff the counters were incremented.
    assert out["actions_consumed"] == (1 if expected_metered else 0)


@pytest.mark.asyncio
async def test_unmetered_branches_do_not_record_execution_telemetry(monkeypatch):
    """``meter_quota=False`` / evaluation runs count no execution either.

    The telemetry recorder sits after those guards on purpose (an anonymous
    turn IS an execution, a nested handover is NOT), so guard reordering would
    silently double-count fan-out.
    """
    _patch_quota(monkeypatch)
    recorded: list[str] = []

    async def _record(source: str) -> None:
        recorded.append(source)

    monkeypatch.setattr(
        "app.services.agent_chat.record_agentic_execution", _record
    )

    await enforce_agentic_quota_ex(
        DB_SENTINEL, str(uuid.uuid4()), meter_quota=False
    )
    await enforce_agentic_quota_ex(
        DB_SENTINEL, str(uuid.uuid4()), evaluation_mode=True
    )
    assert recorded == []

    # An anonymous turn IS a real execution, counted once, and is charged to
    # the platform-wide tiers.
    out: dict = {}
    await enforce_agentic_quota_ex(DB_SENTINEL, "anonymous", quota_out=out)
    assert len(recorded) == 1
    assert out == {"actions_consumed": 1, "reason": "charged_no_account"}
