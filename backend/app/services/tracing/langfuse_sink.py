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

"""Langfuse tracing sink adapter.

The Langfuse lifecycle is still driven by ``services/langfuse_tracing.py``.
This sink exists so the dispatcher can keep a dual-sink contract without
changing the already-working Langfuse path.
"""

from __future__ import annotations

from app.services.tracing.base import SpanData, TraceData


class LangfuseSink:
    """No-op compatibility sink.

    Langfuse observations are emitted directly at trace/span lifecycle points.
    ``flush_trace`` intentionally does not replay the payload.
    """

    async def flush_trace(self, trace: TraceData, spans: list[SpanData]) -> None:
        return None

    async def flush_score(self, score: dict) -> None:
        return None

    async def close(self) -> None:
        return None
