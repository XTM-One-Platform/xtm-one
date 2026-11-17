"""Unit tests for app.services.sandbox_call_logging.log_sandbox_call().

Covers (spec 003-sandbox-call-logging):
- Field presence/omission rules (user_id, failure_reason, CPU/RAM/tte_ms)
- The bounded-timeout-to-null-metrics fallback (FR-007/FR-007a)
- Correct log level per outcome
"""

from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.sandbox_call_logging import (
    log_execution_errors,
    log_sandbox_call,
    log_sandbox_create_failure,
)

_LOGGER_NAME = "app.services.sandbox_call_logging"
_MISSING = object()


def _sandbox_call_records(caplog):
    return [r for r in caplog.records if r.name == _LOGGER_NAME and r.getMessage() == "sandbox_call"]


def _field(record: logging.LogRecord, key: str):
    return getattr(record, key, _MISSING)


def _fake_metrics(
    cpu_used_percentage: float = 12.5,
    cpu_count: float = 1.0,
    memory_used_in_mib: float = 64.0,
    memory_total_in_mib: float = 128.0,
) -> SimpleNamespace:
    return SimpleNamespace(
        cpu_used_percentage=cpu_used_percentage,
        cpu_count=cpu_count,
        memory_used_in_mib=memory_used_in_mib,
        memory_total_in_mib=memory_total_in_mib,
    )


def _fake_execution_complete(execution_time_in_millis: int = 842) -> SimpleNamespace:
    return SimpleNamespace(execution_time_in_millis=execution_time_in_millis)


async def test_success_record_includes_all_fields(caplog):
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)
    sandbox = AsyncMock()
    sandbox.get_metrics = AsyncMock(return_value=_fake_metrics())
    user_id = uuid.uuid4()

    await log_sandbox_call(
        usage_type="code_interpreter",
        outcome="success",
        user_id=user_id,
        sandbox=sandbox,
        execution_complete=_fake_execution_complete(),
        egress_allow_list=["b.example.com", "a.example.com"],
    )

    records = _sandbox_call_records(caplog)
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.INFO
    assert _field(record, "usage_type") == "code_interpreter"
    assert _field(record, "outcome") == "success"
    assert _field(record, "user_id") == str(user_id)
    assert _field(record, "cpu_used_percentage") == 12.5
    assert _field(record, "cpu_count") == 1.0
    assert _field(record, "memory_used_in_mib") == 64.0
    assert _field(record, "memory_total_in_mib") == 128.0
    assert _field(record, "tte_ms") == 842
    assert _field(record, "egress_default_action") == "deny"
    assert _field(record, "egress_allow_list") == ["a.example.com", "b.example.com"]
    assert _field(record, "failure_reason") is _MISSING


async def test_user_id_omitted_when_none(caplog):
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)

    await log_sandbox_call(usage_type="custom_tool_js", outcome="success")

    record = _sandbox_call_records(caplog)[0]
    assert _field(record, "user_id") is _MISSING


async def test_failure_reason_present_only_on_failure(caplog):
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)

    await log_sandbox_call(
        usage_type="custom_tool_python",
        outcome="failure",
        failure_reason="sandbox_create_failed",
    )

    record = _sandbox_call_records(caplog)[0]
    assert record.levelno == logging.WARNING
    assert _field(record, "outcome") == "failure"
    assert _field(record, "failure_reason") == "sandbox_create_failed"


async def test_failure_reason_absent_when_not_given_on_success(caplog):
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)

    await log_sandbox_call(usage_type="code_interpreter", outcome="success")

    record = _sandbox_call_records(caplog)[0]
    assert _field(record, "failure_reason") is _MISSING


async def test_resource_and_tte_fields_omitted_when_not_provided(caplog):
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)

    await log_sandbox_call(
        usage_type="code_interpreter",
        outcome="failure",
        failure_reason="sandbox_create_error",
    )

    record = _sandbox_call_records(caplog)[0]
    for key in (
        "cpu_used_percentage",
        "cpu_count",
        "memory_used_in_mib",
        "memory_total_in_mib",
        "tte_ms",
    ):
        assert _field(record, key) is _MISSING


async def test_metrics_fetch_timeout_falls_back_to_null_resource_fields(caplog):
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)
    sandbox = AsyncMock()
    sandbox.get_metrics = AsyncMock(side_effect=TimeoutError)

    await log_sandbox_call(
        usage_type="code_interpreter",
        outcome="success",
        sandbox=sandbox,
        execution_complete=_fake_execution_complete(500),
    )

    record = _sandbox_call_records(caplog)[0]
    assert _field(record, "outcome") == "success"
    assert _field(record, "tte_ms") == 500
    for key in (
        "cpu_used_percentage",
        "cpu_count",
        "memory_used_in_mib",
        "memory_total_in_mib",
    ):
        assert _field(record, key) is _MISSING


async def test_metrics_fetch_sandbox_exception_falls_back_to_null_resource_fields(caplog):
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)
    from opensandbox.exceptions.sandbox import SandboxException

    sandbox = AsyncMock()
    sandbox.get_metrics = AsyncMock(side_effect=SandboxException("boom"))

    await log_sandbox_call(usage_type="code_interpreter", outcome="success", sandbox=sandbox)

    record = _sandbox_call_records(caplog)[0]
    assert _field(record, "cpu_used_percentage") is _MISSING


async def test_no_metrics_fetch_attempted_when_sandbox_not_given(caplog):
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)

    await log_sandbox_call(
        usage_type="code_interpreter",
        outcome="failure",
        failure_reason="sandbox_ready_timeout",
    )

    record = _sandbox_call_records(caplog)[0]
    assert _field(record, "failure_reason") == "sandbox_ready_timeout"
    assert _field(record, "cpu_used_percentage") is _MISSING


async def test_egress_fields_always_present_and_sorted(caplog):
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)

    await log_sandbox_call(
        usage_type="code_interpreter",
        outcome="success",
        egress_allow_list=frozenset({"z.example.com", "a.example.com"}),
    )

    record = _sandbox_call_records(caplog)[0]
    assert _field(record, "egress_default_action") == "deny"
    assert _field(record, "egress_allow_list") == ["a.example.com", "z.example.com"]


async def test_egress_allow_list_empty_by_default(caplog):
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)

    await log_sandbox_call(usage_type="code_interpreter", outcome="success")

    record = _sandbox_call_records(caplog)[0]
    assert _field(record, "egress_allow_list") == []


async def test_field_names_identical_across_usage_types(caplog):
    """FR-008: the extra-field key set must be byte-for-byte identical across
    all three usage_type values, so records can be aggregated across call
    sites (/speckit-analyze finding A2 — previously validated only manually
    via quickstart.md)."""
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)
    sandbox = AsyncMock()
    sandbox.get_metrics = AsyncMock(return_value=_fake_metrics())

    # Standard LogRecord attributes we don't care about for this comparison.
    _stdlib_keys = set(logging.LogRecord("x", logging.INFO, "", 0, "", (), None).__dict__)

    key_sets = {}
    for usage_type in ("code_interpreter", "custom_tool_js", "custom_tool_python"):
        caplog.clear()
        await log_sandbox_call(
            usage_type=usage_type,
            outcome="success",
            user_id=uuid.uuid4(),
            sandbox=sandbox,
            execution_complete=_fake_execution_complete(),
            egress_allow_list=["a.example.com"],
        )
        record = _sandbox_call_records(caplog)[0]
        key_sets[usage_type] = frozenset(record.__dict__) - _stdlib_keys

    distinct_key_sets = set(key_sets.values())
    assert len(distinct_key_sets) == 1, key_sets


# --- log_sandbox_create_failure() -------------------------------------------


async def test_create_failure_classifies_ready_timeout(caplog):
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)
    from opensandbox.exceptions.sandbox import SandboxReadyTimeoutException

    await log_sandbox_create_failure(
        usage_type="code_interpreter",
        user_id=None,
        exc=SandboxReadyTimeoutException("never ready"),
    )

    record = _sandbox_call_records(caplog)[0]
    assert _field(record, "outcome") == "failure"
    assert _field(record, "failure_reason") == "sandbox_ready_timeout"


async def test_create_failure_classifies_other_sandbox_exception(caplog):
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)
    from opensandbox.exceptions.sandbox import SandboxException

    await log_sandbox_create_failure(
        usage_type="custom_tool_js",
        user_id=None,
        exc=SandboxException("image not found"),
    )

    record = _sandbox_call_records(caplog)[0]
    assert _field(record, "failure_reason") == "sandbox_create_failed"


async def test_create_failure_classifies_generic_exception(caplog):
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)

    await log_sandbox_create_failure(
        usage_type="custom_tool_python",
        user_id=None,
        exc=ValueError("boom"),
    )

    record = _sandbox_call_records(caplog)[0]
    assert _field(record, "failure_reason") == "sandbox_create_error"


async def test_create_failure_never_attempts_metrics(caplog):
    """No `sandbox` object exists yet at Sandbox.create() failure time, so
    there is nothing to fetch metrics from — resource/TTE fields absent."""
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)

    await log_sandbox_create_failure(usage_type="code_interpreter", user_id=None, exc=ValueError("boom"))

    record = _sandbox_call_records(caplog)[0]
    for key in ("cpu_used_percentage", "cpu_count", "memory_used_in_mib", "memory_total_in_mib", "tte_ms"):
        assert _field(record, key) is _MISSING


# --- log_execution_errors() -------------------------------------------------


async def test_execution_errors_guard_logs_and_reraises_on_exception(caplog):
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)
    original_exc = RuntimeError("execd connection dropped")

    with pytest.raises(RuntimeError) as excinfo:
        async with log_execution_errors(usage_type="code_interpreter", user_id=None):
            raise original_exc

    assert excinfo.value is original_exc
    record = _sandbox_call_records(caplog)[0]
    assert _field(record, "outcome") == "failure"
    assert _field(record, "failure_reason") == "execution_error"
    for key in ("cpu_used_percentage", "cpu_count", "memory_used_in_mib", "memory_total_in_mib", "tte_ms"):
        assert _field(record, key) is _MISSING


async def test_execution_errors_guard_logs_nothing_on_success(caplog):
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)

    async with log_execution_errors(usage_type="code_interpreter", user_id=None):
        pass  # the guarded block completed without raising

    assert not _sandbox_call_records(caplog)
