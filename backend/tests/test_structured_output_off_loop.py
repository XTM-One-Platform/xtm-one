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

"""The structured-output repair of a large answer never blocks the event loop.

A playbook AI step sent a large STIX report to an agent with an output schema:
the repair and validation of its answer ran on the web worker's event loop for
over 200 s, the liveness probe failed, the worker was restarted and the caller
saw a dropped connection with no trace. The chat turn and the assignment run now
repair in a worker thread, and the validation stops after a bounded number of
errors.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest

from app.services import structured_output
from app.services.structured_output import (
    MAX_SAP_VALIDATION_ERRORS,
    deliver_json_answer_in_thread,
    emit_repaired_json_in_thread,
    validate_and_parse,
)

_SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "string"}}}}


@pytest.mark.asyncio
async def test_repair_runs_off_the_event_loop(monkeypatch):
    """While a slow repair runs, the loop keeps serving other coroutines."""
    loop_thread = threading.get_ident()
    seen: dict[str, int] = {}

    def slow_repair(raw_output, schema, *, log_context):
        seen["thread"] = threading.get_ident()
        time.sleep(0.3)
        return raw_output

    monkeypatch.setattr(structured_output, "emit_repaired_json", slow_repair)

    ticks = 0

    async def ticker():
        nonlocal ticks
        for _ in range(10):
            await asyncio.sleep(0.02)
            ticks += 1

    result, _ = await asyncio.gather(
        emit_repaired_json_in_thread('{"items": []}', _SCHEMA, log_context="t"),
        ticker(),
    )
    assert result == '{"items": []}'
    assert seen["thread"] != loop_thread
    assert ticks == 10


@pytest.mark.asyncio
async def test_deliver_in_thread_matches_the_sync_rule():
    raw = '```json\n{"items": ["a"]}\n```'
    out = await deliver_json_answer_in_thread(raw, "json", _SCHEMA, log_context="t")
    assert out == structured_output.deliver_json_answer(raw, "json", _SCHEMA, log_context="t")
    assert json.loads(out) == {"items": ["a"]}


@pytest.mark.asyncio
async def test_deliver_in_thread_leaves_other_formats_on_the_loop(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("no thread hop for a non-JSON answer")

    monkeypatch.setattr(structured_output.asyncio, "to_thread", boom)
    assert await deliver_json_answer_in_thread("text", "markdown", None, log_context="t") == "text"
    assert await deliver_json_answer_in_thread("", "json", _SCHEMA, log_context="t") == ""


def test_validation_errors_are_bounded():
    """A payload with thousands of schema errors reports the first ones only."""
    schema = {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "integer"}}}}
    payload = {"items": [f"not-a-number-{n}" for n in range(5000)]}
    data, errors = validate_and_parse(json.dumps(payload), schema)
    assert data is not None
    assert 0 < len(errors) <= MAX_SAP_VALIDATION_ERRORS
