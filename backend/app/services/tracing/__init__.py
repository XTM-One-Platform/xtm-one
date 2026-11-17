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

"""Tracing pricing helpers."""

from app.services.tracing.backfill import TraceBackfillCursor, TraceBackfillResult, TraceBackfillService
from app.services.tracing.base import SpanData, TraceData, TracingBackend
from app.services.tracing.clickhouse_sink import ClickHouseSink
from app.services.tracing.cost_calculator import ModelPrice, calculate_costs, resolve_model_price
from app.services.tracing.dispatcher import (
    OVERFLOW_KEY,
    TracingDispatcher,
    get_tracing_dispatcher,
    init_tracing_dispatcher,
    shutdown_tracing_dispatcher,
    submit_trace_for_flush,
)
from app.services.tracing.langfuse_sink import LangfuseSink
from app.services.tracing.local_sink import LocalPGSink
from app.services.tracing.parity_validation import (
    TraceMetricCounts,
    TraceMetricParity,
    TraceParityStore,
    TracePayloadMismatch,
    TracePayloadParity,
    TraceStoreParityReport,
    TraceStoreParityValidator,
    payload_fingerprint,
    select_deterministic_trace_sample,
)
from app.services.tracing.price_loader import (
    ModelPriceSeed,
    ModelPricesPayload,
    load_model_prices_payload,
    sync_model_prices,
)
from app.services.tracing.store import (
    MirrorTracingSink,
    TraceStoreNotSupportedError,
    build_trace_read_store,
    build_trace_write_sink,
    ensure_trace_read_provider_supported,
    get_trace_store_provider,
)

__all__ = [
    "OVERFLOW_KEY",
    "ClickHouseSink",
    "LangfuseSink",
    "LocalPGSink",
    "MirrorTracingSink",
    "ModelPrice",
    "ModelPriceSeed",
    "ModelPricesPayload",
    "SpanData",
    "TraceBackfillCursor",
    "TraceBackfillResult",
    "TraceBackfillService",
    "TraceData",
    "TraceMetricCounts",
    "TraceMetricParity",
    "TraceParityStore",
    "TracePayloadMismatch",
    "TracePayloadParity",
    "TraceStoreNotSupportedError",
    "TraceStoreParityReport",
    "TraceStoreParityValidator",
    "TracingBackend",
    "TracingDispatcher",
    "build_trace_read_store",
    "build_trace_write_sink",
    "calculate_costs",
    "ensure_trace_read_provider_supported",
    "get_trace_store_provider",
    "get_tracing_dispatcher",
    "init_tracing_dispatcher",
    "load_model_prices_payload",
    "payload_fingerprint",
    "resolve_model_price",
    "select_deterministic_trace_sample",
    "shutdown_tracing_dispatcher",
    "submit_trace_for_flush",
    "sync_model_prices",
]
