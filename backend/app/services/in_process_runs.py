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

"""Process-local registry of in-process tracing AssignmentRuns.

In-process tracing runs (``assignment_id IS NULL``) are created by
``agent_chat`` for chat-originated handovers (the lazy parent run plus
consult / transfer children). They live entirely inside the creating
process — no orchestration execution backs them, so the worker reconciler
can never recover them if the process dies mid-turn.

This registry records which of those runs THIS process created and has not
yet finalized, so the shutdown path can proactively fail exactly its own
interrupted runs (``autonomous.fail_orphaned_in_process_runs``) without
touching runs owned by other replicas or by the worker:

* another web replica's live chat turn also has ``assignment_id IS NULL``
  and ``status = 'running'`` — a global DB sweep would falsely fail it
  during every rolling deploy;
* background-delegation child runs (``sub_agent.create_sub_agent_task``)
  are ``assignment_id IS NULL`` too but ARE engine-managed — their
  execution is deduped on ``sub_agent_task:<task_id>``, not
  ``assignment_run:<run_id>``, so they must never enter this registry.

Tracking is best-effort bookkeeping: the shutdown sweep re-checks each
run's status in the database and only fails runs still in an active
status, so a missed ``untrack`` never double-fails a finished run.

A chat-handover parent run is tracked with its conversation
(``track_in_process_run(run_id, conversation_id=...)``): a turn cut by a
platform stop names it in its queued resume (``cut_turn_resume.CutTurn.
parent_run_id``), so the shutdown sweep keeps it for the resumed turn
to adopt instead of failing it.

Stdlib-only on purpose — imported by ``agent_chat``, ``sub_agent``,
``autonomous``, ``main`` and ``worker`` without creating import cycles.
"""

from __future__ import annotations

_tracked_run_ids: set[str] = set()
#: Chat-handover parent runs by id: the conversation whose turn created them.
_chat_parent_conversations: dict[str, str] = {}


def track_in_process_run(run_id: str | object, *, conversation_id: str | object | None = None) -> None:
    """Register an in-process tracing run created by this process.

    *conversation_id* marks a chat-handover parent run: the conversation
    whose turn created (or adopted) it, read back by :func:`chat_parent_run_for`.
    """
    _tracked_run_ids.add(str(run_id))
    if conversation_id:
        _chat_parent_conversations[str(run_id)] = str(conversation_id)


def untrack_in_process_run(run_id: str | object) -> None:
    """Drop a run from the registry once it reaches a terminal state."""
    _tracked_run_ids.discard(str(run_id))
    _chat_parent_conversations.pop(str(run_id), None)


def chat_parent_run_for(conversation_id: str | object | None) -> str | None:
    """The chat-handover parent run this process holds for *conversation_id*, the latest one; ``None`` when none."""
    if not conversation_id:
        return None
    cid = str(conversation_id)
    found = None
    # Dicts keep insertion order: the last match is the latest parent.
    for run_id, conv in _chat_parent_conversations.items():
        if conv == cid and run_id in _tracked_run_ids:
            found = run_id
    return found


def get_tracked_in_process_runs() -> set[str]:
    """Snapshot of run IDs this process created and has not finalized."""
    return set(_tracked_run_ids)
