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

"""Trace storage provider selection (PostgreSQL / ClickHouse)."""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import settings
from app.services.tracing.base import TracingBackend
from app.services.tracing.clickhouse_sink import ClickHouseSink
from app.services.tracing.local_sink import LocalPGSink
from app.services.tracing.read_store import (
    ClickHouseTraceReadStore,
    PostgresTraceReadStore,
    TraceReadStore,
    TraceStoreProvider,
)


@dataclass(slots=True)
class TraceStoreNotSupportedError(RuntimeError):
    provider: str
    operation: str

    def __str__(self) -> str:  # pragma: no cover - trivial formatting
        return (
            f"Trace store provider '{self.provider}' does not support operation "
            f"'{self.operation}' yet"
        )


class MirrorTracingSink:
    """Write to multiple sinks for transitional provider migrations."""

    def __init__(self, sinks: list[TracingBackend]) -> None:
        self._sinks = sinks

    async def flush_trace(self, trace, spans) -> None:
        for sink in self._sinks:
            await sink.flush_trace(trace, spans)

    async def flush_score(self, score) -> None:
        for sink in self._sinks:
            await sink.flush_score(score)

    async def close(self) -> None:
        for sink in self._sinks:
            await sink.close()


def get_trace_store_provider() -> TraceStoreProvider:
    provider = (settings.tracing_store_provider or "postgres").strip().lower()
    if provider == "clickhouse":
        return "clickhouse"
    return "postgres"


def build_trace_write_sink() -> TracingBackend:
    provider = get_trace_store_provider()
    if provider == "postgres":
        return LocalPGSink()
    if provider == "clickhouse":
        # Keep PostgreSQL writes during the transition so existing read/analytics
        # paths remain consistent while ClickHouse ingestion/backfill is enabled.
        return MirrorTracingSink([ClickHouseSink(), LocalPGSink()])
    raise TraceStoreNotSupportedError(provider=provider, operation="write_ingestion")


def build_trace_read_store() -> TraceReadStore:
    provider = get_trace_store_provider()
    if provider == "clickhouse":
        return ClickHouseTraceReadStore()
    return PostgresTraceReadStore()


def ensure_trace_read_provider_supported() -> None:
    provider = get_trace_store_provider()
    if provider not in {"postgres", "clickhouse"}:
        raise TraceStoreNotSupportedError(provider=provider, operation="trace_explorer_read_api")
