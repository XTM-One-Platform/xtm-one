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

"""Browser profile sessions: the worker's keep-alive sweep, on every reconciler cycle.

No execution is ever created for this adapter: it exists for its periodic
hook, so the keep-alive runs on every worker whatever feature uses the
profiles. The sweep opens a browser, so it never runs on the reconciler's own
cycle and at most every ``keepalive.SWEEP_INTERVAL_SECONDS`` per process.
"""

from __future__ import annotations

import asyncio
import time

from app.models.orchestration import OrchestrationExecution
from app.services.orchestration_adapters.base import AdapterContext, AdapterResult, OrchestrationAdapter

ADAPTER_NAME = "browser_profile_sessions"


class BrowserProfileSessionsAdapter(OrchestrationAdapter):
    name = ADAPTER_NAME
    _sweep_task: asyncio.Task | None = None
    #: ``time.monotonic()`` counts from an arbitrary origin (the host's boot):
    #: the first cycle sweeps whatever its value.
    _last_sweep: float | None = None

    async def execute(self, ctx: AdapterContext, execution: OrchestrationExecution) -> AdapterResult:
        return AdapterResult.failed(f"{self.name} runs no executions", retryable=False)

    async def periodic(self, ctx: AdapterContext) -> None:
        from app.services.browser_profiles.keepalive import SWEEP_INTERVAL_SECONDS, keepalive_sweep

        if self._sweep_task is not None and not self._sweep_task.done():
            return
        now = time.monotonic()
        if self._last_sweep is not None and now - self._last_sweep < SWEEP_INTERVAL_SECONDS:
            return
        self._last_sweep = now
        self._sweep_task = asyncio.create_task(keepalive_sweep())
