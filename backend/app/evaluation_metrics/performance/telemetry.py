# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""The caller-supplied telemetry contract and its public validation boundary.

Everything here is what the caller **already captured**; the package reads no clock and
performs no I/O.

Two rules govern the model:

* ``None`` means *not captured* and yields ``unavailable``; an observed empty value (``()`` for
  calls, ``0`` for a count) is a measurement and yields ``scored``.
* Malformed input to :func:`validate_telemetry` becomes a structured error, not an exception,
  so a caller never wraps metric calls in ``try``.
"""

from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Self

from pydantic import (
    BaseModel,
    ConfigDict,
    ValidationError,
    field_validator,
    model_validator,
)

from app.evaluation_metrics.core.contracts import MetricError, MetricStatus
from app.evaluation_metrics.performance.results import PerformanceResult


class CallKind(StrEnum):
    """Whether a call was made to a model or to a tool."""

    model = "model"
    tool = "tool"


class CallScope(StrEnum):
    """Whether a call belongs to the agent or to evaluation overhead."""

    agent = "agent"
    overhead = "overhead"


class CallOutcome(StrEnum):
    """The closed set of recorded call outcomes.

    An **absent** outcome means the status was not captured and is never assumed successful;
    an unrecognized value is malformed telemetry.
    """

    success = "success"
    error = "error"
    timeout = "timeout"
    cancelled = "cancelled"


class TokenConvention(StrEnum):
    """How the raw provider token buckets relate to one another."""

    separate = "separate"
    inclusive = "inclusive"


def _non_negative_duration(value: float | None) -> float | None:
    if value is None:
        return None
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError("duration must be a finite number of milliseconds")
    if value < 0:
        raise ValueError("duration must be non-negative")
    return value


def _non_negative_count(value: object) -> object:
    # Runs in "before" mode: Pydantic would otherwise coerce ``True`` to ``1``.
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("count must be a non-boolean integer")
    if value < 0:
        raise ValueError("count must be non-negative")
    return value





class _Strict(BaseModel):
    """Frozen and closed: an unknown field is malformed telemetry, not silently ignored."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class CallRecord(_Strict):
    """One model or tool call as recorded by the caller."""

    kind: CallKind
    scope: CallScope
    name: str | None = None
    duration_ms: float | None = None
    outcome: CallOutcome | None = None
    error_type: str | None = None

    @field_validator("duration_ms")
    @classmethod
    def _check_duration(cls, value: float | None) -> float | None:
        return _non_negative_duration(value)


class TokenUsage(_Strict):
    """The **raw** provider token buckets, exactly as reported.

    The normalized uncached-input figure is derived by :func:`normalize_token_usage`; the
    caller is never asked to pre-compute a value whose meaning depends on the convention.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_write_input_tokens: int | None = None
    reasoning_output_tokens: int | None = None
    convention: TokenConvention

    @field_validator(
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_write_input_tokens",
        "reasoning_output_tokens",
        mode="before",
    )
    @classmethod
    def _check_count(cls, value: object) -> object:
        return _non_negative_count(value)

    @model_validator(mode="after")
    def _check_convention(self) -> Self:
        if self.convention is TokenConvention.inclusive:
            cached = (self.cache_read_input_tokens or 0) + (
                self.cache_write_input_tokens or 0
            )
            if cached > (self.input_tokens or 0):
                raise ValueError(
                    "under the 'inclusive' convention the cache buckets cannot exceed "
                    "input_tokens"
                )
        return self


class ModelTokenUsage(_Strict):
    """One serving model's share of an execution's agent usage.

    An execution served by several models is priced model by model, and its buckets are
    normalized per model before they are added: two providers' cache conventions cannot be
    summed raw.
    """

    model: str | None = None
    usage: TokenUsage


class NormalizedTokenUsage(_Strict):
    """Canonical token usage derived once at the validation boundary."""

    uncached_input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_write_input_tokens: int | None = None
    reasoning_output_tokens: int | None = None
    total_tokens: int | None = None


def sum_normalized_usage(parts: Sequence[NormalizedTokenUsage]) -> NormalizedTokenUsage:
    """Add normalized usages bucket by bucket; a bucket no part captured stays ``None``."""

    def _add(attr: str) -> int | None:
        values = [getattr(part, attr) for part in parts]
        return None if all(v is None for v in values) else sum(v or 0 for v in values)

    return NormalizedTokenUsage(
        uncached_input_tokens=_add("uncached_input_tokens"),
        output_tokens=_add("output_tokens"),
        cache_read_input_tokens=_add("cache_read_input_tokens"),
        cache_write_input_tokens=_add("cache_write_input_tokens"),
        reasoning_output_tokens=_add("reasoning_output_tokens"),
        total_tokens=_add("total_tokens"),
    )


def normalize_token_usage(usage: TokenUsage) -> NormalizedTokenUsage:
    """Apply the declared convention so no token is ever counted twice.

    Args:
        usage: The raw provider buckets plus their convention.

    Returns:
        The canonical usage. ``uncached_input_tokens`` is the raw input under the ``separate``
        convention, and input minus the cache buckets under ``inclusive``. ``total_tokens`` is
        ``None`` when no bucket was captured — never ``0``. The reasoning bucket is a
        diagnostic and is excluded from the total.
    """
    cache_read = usage.cache_read_input_tokens
    cache_write = usage.cache_write_input_tokens
    if usage.input_tokens is None:
        uncached = None
    elif usage.convention is TokenConvention.separate:
        uncached = usage.input_tokens
    else:
        uncached = usage.input_tokens - (cache_read or 0) - (cache_write or 0)

    buckets = (uncached, usage.output_tokens, cache_read, cache_write)
    total = None if all(b is None for b in buckets) else sum(b or 0 for b in buckets)
    return NormalizedTokenUsage(
        uncached_input_tokens=uncached,
        output_tokens=usage.output_tokens,
        cache_read_input_tokens=cache_read,
        cache_write_input_tokens=cache_write,
        reasoning_output_tokens=usage.reasoning_output_tokens,
        total_tokens=total,
    )


class ExecutionTelemetry(_Strict):
    """One execution's already-captured observations."""

    wall_clock_ms: float | None = None
    ttft_ms: float | None = None
    calls: Sequence[CallRecord] | None = None
    agent_token_usage: TokenUsage | None = None
    #: Set instead of ``agent_token_usage`` when more than one model served the
    #: agent's calls; each part is normalized and priced on its own.
    agent_token_usage_by_model: Sequence[ModelTokenUsage] | None = None
    overhead_token_usage: TokenUsage | None = None
    retry_count: int | None = None

    @field_validator("wall_clock_ms", "ttft_ms")
    @classmethod
    def _check_duration(cls, value: float | None) -> float | None:
        return _non_negative_duration(value)

    @field_validator("retry_count", mode="before")
    @classmethod
    def _check_count(cls, value: object) -> object:
        return _non_negative_count(value)

    @field_validator("calls")
    @classmethod
    def _freeze_calls(
        cls, value: Sequence[CallRecord] | None
    ) -> Sequence[CallRecord] | None:
        # None (uncaptured) must survive; an observed sequence becomes a tuple.
        return None if value is None else tuple(value)

    @field_validator("agent_token_usage_by_model")
    @classmethod
    def _freeze_usage_parts(
        cls, value: Sequence[ModelTokenUsage] | None
    ) -> Sequence[ModelTokenUsage] | None:
        return None if value is None else tuple(value)

    def agent_usage_parts(self) -> tuple[ModelTokenUsage, ...] | None:
        """The agent usage as per-model parts: the parts given, or the single usage as one.

        ``None`` when agent usage was not captured at all.
        """
        if self.agent_token_usage_by_model is not None:
            return tuple(self.agent_token_usage_by_model)
        if self.agent_token_usage is not None:
            return (ModelTokenUsage(usage=self.agent_token_usage),)
        return None

    def agent_calls(self, kind: CallKind | None = None) -> tuple[CallRecord, ...] | None:
        """Return agent-scoped calls, or ``None`` when the sequence was never captured.

        Args:
            kind: Optional filter restricting the result to model or tool calls.

        Returns:
            The matching agent-scoped calls, or ``None`` if ``calls`` was uncaptured.
        """
        if self.calls is None:
            return None
        return tuple(
            call
            for call in self.calls
            if call.scope is CallScope.agent and (kind is None or call.kind is kind)
        )


class TelemetryValidation(_Strict):
    """The outcome of validating a raw caller mapping.

    Exactly one of ``telemetry`` and ``error`` is populated.
    """

    telemetry: ExecutionTelemetry | None = None
    error: MetricError | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if (self.telemetry is None) == (self.error is None):
            raise ValueError("exactly one of 'telemetry' or 'error' must be populated")
        return self

    def invalid_results(self) -> dict[str, PerformanceResult]:
        """Return an ``invalid`` result per emitted metric key, carrying the validation error.

        Returns:
            A mapping from stable metric key to an ``invalid`` result. Empty when validation
            succeeded.
        """
        if self.error is None:
            return {}
        from app.evaluation_metrics.performance.metrics import METRIC_METADATA

        return {
            metadata.key: PerformanceResult(
                status=MetricStatus.invalid,
                value=None,
                error=self.error,
                metadata=metadata,
            )
            for metadata in METRIC_METADATA
        }


def _locations(exc: ValidationError) -> list[str]:
    return sorted(
        ".".join(str(part) for part in error["loc"]) or "<root>" for error in exc.errors()
    )


def validate_telemetry(raw: object) -> TelemetryValidation:
    """Validate a raw caller mapping into typed telemetry.

    The boundary between unvalidated caller data and the pure metric API. It accepts any
    input and never raises on malformation, hence ``raw: object``.

    Args:
        raw: Already-captured telemetry in unvalidated form.

    Returns:
        A :class:`TelemetryValidation` carrying either the validated telemetry or a
        structured ``malformed_telemetry`` error (also for a non-mapping).
    """
    if not isinstance(raw, Mapping):
        return TelemetryValidation(
            error=MetricError(
                code="malformed_telemetry",
                message="telemetry must be a mapping",
                locations=["<root>"],
            )
        )
    try:
        return TelemetryValidation(telemetry=ExecutionTelemetry.model_validate(dict(raw)))
    except ValidationError as exc:
        return TelemetryValidation(
            error=MetricError(
                code="malformed_telemetry",
                message=f"{exc.error_count()} telemetry validation error(s)",
                locations=_locations(exc),
            )
        )


__all__ = [
    "CallKind",
    "CallOutcome",
    "CallRecord",
    "CallScope",
    "ExecutionTelemetry",
    "ModelTokenUsage",
    "NormalizedTokenUsage",
    "TelemetryValidation",
    "TokenConvention",
    "TokenUsage",
    "normalize_token_usage",
    "sum_normalized_usage",
    "validate_telemetry",
]
