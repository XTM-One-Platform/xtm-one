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

"""The sandbox workspace sweep, on the worker's reconciler cycle.

No execution is ever created for this adapter: it exists for its periodic
hook. Each process asks every ``gc.ATTEMPT_INTERVAL_SECONDS`` and
``gc.take_lease`` answers yes once per ``gc.SWEEP_INTERVAL_SECONDS`` for the
whole install, or at once when a sweep was cut short; the sweep runs in a task
of its own so the reconciler cycle never waits on the object store. The same
pass deletes the agentic coding lab pods that ended or sat unused past their
idle time (``labs.reap``, hygiene only, when ``AGENTIC_CODING`` is on).
"""

from __future__ import annotations

import asyncio
import time

from app.models.orchestration import OrchestrationExecution
from app.services.orchestration_adapters.base import AdapterContext, AdapterResult, OrchestrationAdapter

ADAPTER_NAME = "sandbox_workspace_sweep"


class SandboxWorkspaceSweepAdapter(OrchestrationAdapter):
    name = ADAPTER_NAME
    _sweep_task: asyncio.Task | None = None
    _last_sweep: float | None = None

    async def execute(self, ctx: AdapterContext, execution: OrchestrationExecution) -> AdapterResult:
        return AdapterResult.failed(f"{self.name} runs no executions", retryable=False)

    async def periodic(self, ctx: AdapterContext) -> None:
        from app.services.sandbox_workspace.gc import ATTEMPT_INTERVAL_SECONDS

        if self._sweep_task is not None and not self._sweep_task.done():
            return
        now = time.monotonic()
        if self._last_sweep is not None and now - self._last_sweep < ATTEMPT_INTERVAL_SECONDS:
            return
        self._last_sweep = now
        self._sweep_task = asyncio.create_task(self._run(), name="sandbox-workspace-sweep")

    @staticmethod
    async def _run() -> None:
        from app.services.sandbox_workspace import labs
        from app.services.sandbox_workspace.gc import sweep, take_lease

        if await take_lease():
            await sweep()
            if labs.labs_enabled():
                await labs.reap_logged()
