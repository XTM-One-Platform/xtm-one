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

"""Trace parity validation helpers for PostgreSQL/ClickHouse cutover checks."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID


@dataclass(slots=True, frozen=True)
class TraceMetricCounts:
    total_traces: int
    total_spans: int
    total_scores: int


@dataclass(slots=True, frozen=True)
class TraceMetricParity:
    left: TraceMetricCounts
    right: TraceMetricCounts

    @property
    def traces_match(self) -> bool:
        return self.left.total_traces == self.right.total_traces

    @property
    def spans_match(self) -> bool:
        return self.left.total_spans == self.right.total_spans

    @property
    def scores_match(self) -> bool:
        return self.left.total_scores == self.right.total_scores

    @property
    def is_match(self) -> bool:
        return self.traces_match and self.spans_match and self.scores_match


@dataclass(slots=True, frozen=True)
class TracePayloadMismatch:
    trace_id: str
    left_hash: str
    right_hash: str


@dataclass(slots=True, frozen=True)
class TracePayloadParity:
    requested_sample_size: int
    sampled_trace_ids: list[str]
    matched_trace_ids: list[str]
    missing_in_left: list[str]
    missing_in_right: list[str]
    mismatches: list[TracePayloadMismatch]

    @property
    def is_match(self) -> bool:
        return (
            not self.missing_in_left
            and not self.missing_in_right
            and not self.mismatches
            and len(self.sampled_trace_ids) == len(self.matched_trace_ids)
        )


@dataclass(slots=True, frozen=True)
class TraceStoreParityReport:
    left_store: str
    right_store: str
    start_at: datetime
    end_at: datetime
    metrics: TraceMetricParity
    sampled_payloads: TracePayloadParity | None

    @property
    def is_match(self) -> bool:
        if self.sampled_payloads is None:
            return self.metrics.is_match
        return self.metrics.is_match and self.sampled_payloads.is_match


class TraceParityStore(Protocol):
    async def fetch_counts(self, *, start_at: datetime, end_at: datetime) -> TraceMetricCounts:
        pass

    async def fetch_trace_ids(self, *, start_at: datetime, end_at: datetime) -> list[str]:
        pass

    async def fetch_trace_payloads(
        self,
        *,
        trace_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        pass


def select_deterministic_trace_sample(
    trace_ids: list[str],
    *,
    sample_size: int,
    seed: str = "trace-parity",
) -> list[str]:
    if sample_size <= 0 or not trace_ids:
        return []
    ranked = sorted(
        (
            hashlib.sha256(f"{seed}:{trace_id}".encode()).hexdigest(),
            trace_id,
        )
        for trace_id in set(trace_ids)
    )
    return [trace_id for _score, trace_id in ranked[:sample_size]]


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime | date | UUID):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"Unsupported payload type: {type(value)!r}")


def payload_fingerprint(payload: dict[str, Any]) -> str:
    canonical_payload = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )
    return hashlib.sha256(canonical_payload.encode()).hexdigest()


class TraceStoreParityValidator:
    def __init__(
        self,
        *,
        left_store: TraceParityStore,
        right_store: TraceParityStore,
        left_name: str = "postgres",
        right_name: str = "clickhouse",
    ) -> None:
        self._left_store = left_store
        self._right_store = right_store
        self._left_name = left_name
        self._right_name = right_name

    async def validate(
        self,
        *,
        start_at: datetime,
        end_at: datetime,
        sample_size: int = 0,
        sample_seed: str = "trace-parity",
    ) -> TraceStoreParityReport:
        left_counts = await self._left_store.fetch_counts(start_at=start_at, end_at=end_at)
        right_counts = await self._right_store.fetch_counts(start_at=start_at, end_at=end_at)
        metrics = TraceMetricParity(left=left_counts, right=right_counts)

        sampled_payloads: TracePayloadParity | None = None
        if sample_size > 0:
            sampled_payloads = await self._validate_payload_sample(
                start_at=start_at,
                end_at=end_at,
                sample_size=sample_size,
                sample_seed=sample_seed,
            )

        return TraceStoreParityReport(
            left_store=self._left_name,
            right_store=self._right_name,
            start_at=start_at,
            end_at=end_at,
            metrics=metrics,
            sampled_payloads=sampled_payloads,
        )

    async def _validate_payload_sample(
        self,
        *,
        start_at: datetime,
        end_at: datetime,
        sample_size: int,
        sample_seed: str,
    ) -> TracePayloadParity:
        left_ids = await self._left_store.fetch_trace_ids(start_at=start_at, end_at=end_at)
        right_ids = await self._right_store.fetch_trace_ids(start_at=start_at, end_at=end_at)
        sample_ids = select_deterministic_trace_sample(
            [*left_ids, *right_ids],
            sample_size=sample_size,
            seed=sample_seed,
        )
        if not sample_ids:
            return TracePayloadParity(
                requested_sample_size=sample_size,
                sampled_trace_ids=[],
                matched_trace_ids=[],
                missing_in_left=[],
                missing_in_right=[],
                mismatches=[],
            )

        left_payloads = await self._left_store.fetch_trace_payloads(trace_ids=sample_ids)
        right_payloads = await self._right_store.fetch_trace_payloads(trace_ids=sample_ids)
        matched_trace_ids: list[str] = []
        missing_in_left: list[str] = []
        missing_in_right: list[str] = []
        mismatches: list[TracePayloadMismatch] = []

        for trace_id in sample_ids:
            left_payload = left_payloads.get(trace_id)
            right_payload = right_payloads.get(trace_id)
            if left_payload is None:
                missing_in_left.append(trace_id)
                continue
            if right_payload is None:
                missing_in_right.append(trace_id)
                continue
            left_hash = payload_fingerprint(left_payload)
            right_hash = payload_fingerprint(right_payload)
            if left_hash == right_hash:
                matched_trace_ids.append(trace_id)
            else:
                mismatches.append(
                    TracePayloadMismatch(
                        trace_id=trace_id,
                        left_hash=left_hash,
                        right_hash=right_hash,
                    )
                )

        return TracePayloadParity(
            requested_sample_size=sample_size,
            sampled_trace_ids=sample_ids,
            matched_trace_ids=matched_trace_ids,
            missing_in_left=missing_in_left,
            missing_in_right=missing_in_right,
            mismatches=mismatches,
        )
