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

"""The OpenSandbox pool reaper, on the worker's reconciler cycle.

No execution is ever created for this adapter: it exists for its periodic
hook. Each process asks every ``sandbox_pool_reaper.ATTEMPT_INTERVAL_SECONDS``
and ``take_turn`` answers yes once a minute for the whole install, or at once
after a pooled create timed out; the reap runs in a task of its own so the
reconciler cycle never waits on the API server. Off while
``OPENSANDBOX_K8S_NAMESPACE`` is unset.
"""

from __future__ import annotations

import asyncio
import time

from app.models.orchestration import OrchestrationExecution
from app.services.orchestration_adapters.base import AdapterContext, AdapterResult, OrchestrationAdapter

ADAPTER_NAME = "sandbox_pool_reaper"


class SandboxPoolReaperAdapter(OrchestrationAdapter):
    name = ADAPTER_NAME
    _task: asyncio.Task[None] | None = None
    _last_attempt: float | None = None
    _last_local_reap: float | None = None

    async def execute(self, ctx: AdapterContext, execution: OrchestrationExecution) -> AdapterResult:
        return AdapterResult.failed(f"{self.name} runs no executions", retryable=False)

    async def periodic(self, ctx: AdapterContext) -> None:
        from app.services import sandbox_pool_reaper as reaper

        if not reaper.enabled():
            return
        if self._task is not None and not self._task.done():
            return
        now = time.monotonic()
        if self._last_attempt is not None and now - self._last_attempt < reaper.ATTEMPT_INTERVAL_SECONDS:
            return
        self._last_attempt = now
        self._task = asyncio.create_task(self._run(), name="sandbox-pool-reaper")

    async def _run(self) -> None:
        from app.services import sandbox_pool_reaper as reaper

        turn = await reaper.take_turn()
        if turn is None:
            # Redis down: this process reaps on its own minute.
            now = time.monotonic()
            if self._last_local_reap is not None and now - self._last_local_reap < reaper.INTERVAL_SECONDS:
                return
            self._last_local_reap = now
        elif not turn:
            return
        await reaper.reap_logged()
