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

"""Orchestration adapters package.

Re-exports the adapter contract + registry, and provides
``register_builtin_adapters()`` to register the three built-in adapters
(assignment, evaluation, sub-agent). Registration is lazy (called by the
reconciler at worker startup) so importing this package never triggers the
heavy service imports the adapters depend on.
"""

from __future__ import annotations

from app.services.orchestration_adapters.base import (
    AdapterContext,
    AdapterResult,
    OrchestrationAdapter,
)
from app.services.orchestration_adapters.registry import (
    AdapterAlreadyRegisteredError,
    AdapterNotFoundError,
    get_adapter,
    list_adapters,
    register_adapter,
    registered_adapters,
)

__all__ = [
    "AdapterAlreadyRegisteredError",
    "AdapterContext",
    "AdapterNotFoundError",
    "AdapterResult",
    "OrchestrationAdapter",
    "get_adapter",
    "list_adapters",
    "register_adapter",
    "register_builtin_adapters",
    "registered_adapters",
]


def register_builtin_adapters() -> None:
    """Register the built-in adapters. Idempotent.

    Only registers adapters whose name isn't already present, so repeat calls
    in the same process (e.g. the reconciler at startup *and* the on-demand
    ``/runs/cleanup-stale`` endpoint) are safe. ``register_adapter`` raises on a
    *different* instance under an existing name, and each call here builds fresh
    instances, so the membership check is what keeps it truly idempotent.
    """
    from app.services.orchestration_adapters.assignment import (
        AssignmentAdapter,
        AssignmentRunWakeSweepAdapter,
    )
    from app.services.orchestration_adapters.autonomous import (
        AutonomousPentestAdapter,
    )
    from app.services.orchestration_adapters.browser_profiles import (
        BrowserProfileSessionsAdapter,
    )
    from app.services.orchestration_adapters.evaluation import EvaluationAdapter
    from app.services.orchestration_adapters.flow import FlowAdapter
    from app.services.orchestration_adapters.sandbox_pool_reaper import SandboxPoolReaperAdapter
    from app.services.orchestration_adapters.sandbox_workspace import SandboxWorkspaceSweepAdapter
    from app.services.orchestration_adapters.scheduled_work_item import (
        ScheduledWorkItemAdapter,
    )
    from app.services.orchestration_adapters.sub_agent import SubAgentAdapter
    from app.services.orchestration_adapters.video_studio import (
        VideoClipAdapter,
        VideoFigureStillsAdapter,
        VideoMusicAdapter,
        VideoPreviewAdapter,
        VideoRenderAdapter,
        VideoStudioWakeSweepAdapter,
        VideoTakeAdapter,
    )

    existing = set(list_adapters())
    for adapter in (
        AssignmentAdapter(),
        AssignmentRunWakeSweepAdapter(),
        AutonomousPentestAdapter(),
        BrowserProfileSessionsAdapter(),
        EvaluationAdapter(),
        FlowAdapter(),
        SandboxPoolReaperAdapter(),
        SandboxWorkspaceSweepAdapter(),
        ScheduledWorkItemAdapter(),
        SubAgentAdapter(),
        VideoTakeAdapter(),
        VideoRenderAdapter(),
        VideoPreviewAdapter(),
        VideoClipAdapter(),
        VideoMusicAdapter(),
        VideoFigureStillsAdapter(),
        VideoStudioWakeSweepAdapter(),
    ):
        if adapter.name not in existing:
            register_adapter(adapter)
