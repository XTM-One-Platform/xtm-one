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

"""Whether the work in progress answers an A2A partner's task.

An A2A task runs as its exposure's run-as identity (``exposure.resolve_run_as``):
for a built-in agent, the account it was seeded with, usually an administrator.
The caller is still an external partner, like a channel sender with no
account, so the platform-builder surface must not act with that identity's
reach for it: no trace inspection (an agent's traces hold its users'
conversations), and only the read half of the agent builder, acting as an
anonymous caller. ``agent_chat._turn_trace_tool_ids`` and
``agent_chat._platform_builder_actor`` read this scope.

The A2A executor opens the scope around its run, so every in-process hop of
the task (a consult, a transfer) inherits it with the asyncio context. A
background delegation crosses to the worker, so its execution payload carries
``PARTNER_TASK_PAYLOAD_KEY`` and the sub-agent adapter reopens the scope. An
assignment run the task starts records the same keys on its ``trigger_payload``
(``partner_scoped_payload``) and the assignment adapter reopens the scope.

The scope also carries the partner principal that sent the task (its exposure
and OAuth client), so the one read the anonymous builder lends a partner task,
the run history of that same partner's own tasks (``a2a.partner_runs``), is
scoped by the partner and never by the run-as identity's reach. A delegation
carries it under ``PARTNER_PRINCIPAL_PAYLOAD_KEY``.

Nor does a partner task drive the run-as identity's enrolled machines: the
``computer_use`` suite is withheld (``withhold_runner_tools``), whatever the
agent carries. No exposure opts in.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.tools.base import CompositeToolExecutor

#: Sub-agent execution payload key: the delegating run answered a partner's task.
PARTNER_TASK_PAYLOAD_KEY = "a2a_partner_task"
#: Sub-agent execution payload key: the partner principal that sent that task.
PARTNER_PRINCIPAL_PAYLOAD_KEY = "a2a_partner_principal"
#: Why a partner task has no ``computer_use`` function, as the model is told.
RUNNERS_WITHHELD_REASON = "an external A2A partner's task never drives an enrolled machine"


@dataclass(frozen=True)
class PartnerPrincipal:
    """The partner a task answers: the exposure it reached and the OAuth client it
    authenticated as. Task reads are already scoped to this pair."""

    exposure_id: uuid.UUID
    client_id: str

    def as_payload(self) -> dict[str, str]:
        return {"exposure_id": str(self.exposure_id), "client_id": self.client_id}


_PARTNER_TASK: ContextVar[bool] = ContextVar("a2a_partner_task", default=False)
_PARTNER_PRINCIPAL: ContextVar[PartnerPrincipal | None] = ContextVar("a2a_partner_principal", default=None)


@contextlib.contextmanager
def partner_task_scope(active: bool = True, *, principal: PartnerPrincipal | None = None) -> Iterator[None]:
    """Scope in which the work answers an A2A partner's task; a no-op when *active* is false.

    *principal* names the partner; without one the scope still narrows the
    builder, and lends no run history."""
    if not active:
        yield
        return
    token = _PARTNER_TASK.set(True)
    principal_token = _PARTNER_PRINCIPAL.set(principal)
    try:
        yield
    finally:
        _PARTNER_PRINCIPAL.reset(principal_token)
        _PARTNER_TASK.reset(token)


def in_partner_task() -> bool:
    """Whether the current work answers an A2A partner's task."""
    return _PARTNER_TASK.get()


def current_partner() -> PartnerPrincipal | None:
    """The partner the current work answers, ``None`` outside a partner task or when it is not known."""
    return _PARTNER_PRINCIPAL.get() if _PARTNER_TASK.get() else None


def partner_principal_payload() -> dict[str, str] | None:
    """The current partner as a delegation payload value, ``None`` when there is none."""
    partner = current_partner()
    return partner.as_payload() if partner is not None else None


def partner_scoped_payload(payload: dict | None) -> dict | None:
    """*payload* as an assignment run records it: the current partner-task scope, and only that.

    An assignment run started while answering a partner's task (``delegate_to_assignment``,
    then whatever that run chains) records the scope on its ``trigger_payload``, and the
    assignment worker reopens it (``queued_partner_task``, ``queued_partner_principal``).
    The scope keys a caller's payload carries (a webhook body, a flow's data) are
    dropped first: a principal named there would lend that partner's run history."""
    if payload is not None and (PARTNER_TASK_PAYLOAD_KEY in payload or PARTNER_PRINCIPAL_PAYLOAD_KEY in payload):
        payload = {
            k: v for k, v in payload.items() if k not in (PARTNER_TASK_PAYLOAD_KEY, PARTNER_PRINCIPAL_PAYLOAD_KEY)
        }
    if not in_partner_task():
        return payload
    return {
        **(payload or {}),
        PARTNER_TASK_PAYLOAD_KEY: True,
        PARTNER_PRINCIPAL_PAYLOAD_KEY: partner_principal_payload(),
    }


def queued_partner_principal(payload: Any) -> PartnerPrincipal | None:
    """The partner a queued sub-agent task answers, read back from its payload.

    Anything malformed answers ``None``: no partner, so no run history lent."""
    if not queued_partner_task(payload):
        return None
    value = payload.get(PARTNER_PRINCIPAL_PAYLOAD_KEY)
    if not isinstance(value, Mapping):
        return None
    client_id = value.get("client_id")
    try:
        exposure_id = uuid.UUID(str(value.get("exposure_id")))
    except ValueError:
        return None
    if not isinstance(client_id, str) or not client_id:
        return None
    return PartnerPrincipal(exposure_id=exposure_id, client_id=client_id)


def queued_partner_task(payload: Any) -> bool:
    """Whether a queued sub-agent task was delegated by a run answering a partner's task."""
    return isinstance(payload, Mapping) and payload.get(PARTNER_TASK_PAYLOAD_KEY) is True


def withhold_runner_tools(composite: CompositeToolExecutor) -> bool:
    """Withhold the ``computer_use`` suite from a partner's task, and say whether it did.

    The suite drives the run-as identity's enrolled machines (shell, files,
    screenshots, input), a reach an external caller is never lent. Outside a
    partner task nothing is recorded and the answer is ``False``: the builder
    wires the suite as usual."""
    if not in_partner_task():
        return False
    from app.services.builtin_tools import build_computer_use_tools

    defs, _ = build_computer_use_tools(
        user_id=None, is_admin=False, user_group_ids=[], agent_runner_ids=[], agent_runner_labels=[]
    )
    composite.withhold([d.name for d in defs], RUNNERS_WITHHELD_REASON)
    return True
