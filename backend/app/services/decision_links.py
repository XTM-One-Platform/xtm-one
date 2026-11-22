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

"""Every link to a human gate, built in one place (#842, S2; human-gates.md I.7).

A card, a bell entry, a push or a message opens a page that exists: a work
item ``/tasks/work-items/<id>`` (``/operations/work-items`` is gone since
#785), a run ``/operations/executions/<id>``, a flow run its flow's library
page with the run selected. Callers never build these strings themselves;
``tests/test_decision_links.py`` checks every route against ``App.tsx``.
"""

from __future__ import annotations

import uuid

#: The pages a link may open, as the frontend router declares them.
WORK_ITEM_ROUTE = "/tasks/work-items/{id}"
RUN_ROUTE = "/operations/executions/{id}"
FLOW_ROUTE = "/flows/library/{id}"
FLOWS_HOME = "/flows"
OPERATIONS_HOME = "/operations"


def _uuid(value: object) -> str | None:
    try:
        return str(uuid.UUID(str(value))) if value else None
    except ValueError:
        return None


def work_item_link(item_id: object) -> str:
    """The work item's own page; the list when the id is not one."""
    uid = _uuid(item_id)
    return WORK_ITEM_ROUTE.format(id=uid) if uid else "/tasks/work-items"


def run_link(run_id: object) -> str:
    """The run's detail page; Operations when the id is not one."""
    uid = _uuid(run_id)
    return RUN_ROUTE.format(id=uid) if uid else OPERATIONS_HOME


def flow_run_link(flow_id: object, run_id: object = None) -> str:
    """The flow's library page with the run open; the flows home without a flow."""
    fid = _uuid(flow_id)
    if not fid:
        return FLOWS_HOME
    route = FLOW_ROUTE.format(id=fid)
    rid = _uuid(run_id)
    return f"{route}?run={rid}" if rid else route


def decision_link(source_type: str, source_id: object, *, flow_id: object = None) -> str:
    """The page a decision on *source_type* ``source_id`` opens."""
    if source_type == "work_item":
        return work_item_link(source_id)
    if source_type == "run":
        return run_link(source_id)
    if source_type == "flow_run":
        return flow_run_link(flow_id, source_id)
    raise ValueError(f"unknown decision source {source_type!r}")
