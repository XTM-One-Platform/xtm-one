# SPDX-License-Identifier: Apache-2.0
"""OpenAEV tool answers are never cut silently (xtm-one #233).

A single-object read (an inject and its status trace, an atomic test, a
dashboard, the settings) answers up to ``JSON_READ_CHARS`` characters of JSON,
and every cut answer of the package ends with a marker that says how much was
left out, instead of stopping mid-JSON.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.integrations.providers.openaev import build_tools
from app.integrations.providers.openaev._common import JSON_READ_CHARS, json_answer
from app.tools.base import CallableToolExecutor, ToolCall

TOKEN = "https://demo.openaev.io||test-token"

_PACKAGE = Path(__file__).resolve().parents[1] / "app" / "integrations" / "providers" / "openaev"


class _Resp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class _Api:
    def __init__(self, data):
        self._data = data

    async def __call__(self, base_url, headers, method, path, action, **kw):
        return _Resp(self._data), None


async def _run(name: str, arguments: dict) -> str:
    executor = CallableToolExecutor()
    build_tools(TOKEN, executor)
    result = await executor.execute_tool(ToolCall(id=f"call-{name}", name=name, arguments=arguments))
    assert not result.is_error, result.content
    return result.content


def _trace(entries: int) -> dict:
    return {
        "status_name": "SUCCESS",
        "status_traces": [
            {"execution_message": f"step {i} " + "x" * 80, "execution_status": "INFO"} for i in range(entries)
        ],
    }


def test_json_answer_whole_when_it_fits():
    value = {"a": 1, "b": "two"}
    assert json_answer(value) == json.dumps(value)


def test_json_answer_marks_the_cut():
    value = {"items": ["word " * 40 for _ in range(10)]}
    whole = json.dumps(value)
    answer = json_answer(value, 500)
    assert len(whole) > 500
    match = re.search(r"\.\.\. \[truncated, (\d+) more characters\]$", answer)
    assert match, answer[-120:]
    shown = answer[: match.start()].rstrip()
    assert whole.startswith(shown)
    assert int(match.group(1)) == len(whole) - len(shown)


async def test_inject_status_trace_reads_past_the_old_2000_cut(monkeypatch):
    from app.integrations.providers.openaev import injects

    data = _trace(30)
    assert 2000 < len(json.dumps(data)) < JSON_READ_CHARS
    monkeypatch.setattr(injects, "_api", _Api(data))
    content = await _run("manage_openaev_inject", {"action": "status", "scope": "global", "inject_id": "i1"})
    assert json.loads(content) == data


async def test_inject_status_past_the_read_size_says_it_was_cut(monkeypatch):
    from app.integrations.providers.openaev import injects

    data = _trace(400)
    assert len(json.dumps(data)) > JSON_READ_CHARS
    monkeypatch.setattr(injects, "_api", _Api(data))
    content = await _run("manage_openaev_inject", {"action": "status", "scope": "global", "inject_id": "i1"})
    assert re.search(r"\[truncated, \d+ more characters: read on with offset=\d+\]$", content)


async def test_inject_status_reads_on_by_offset_to_the_whole_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cut status trace is read on by offset windows that join back to the whole JSON."""
    from app.integrations.providers.openaev import injects

    data = _trace(400)
    whole = json.dumps(data)
    monkeypatch.setattr(injects, "_api", _Api(data))
    parts: list[str] = []
    offset = 0
    for _ in range(10):
        content = await _run(
            "manage_openaev_inject", {"action": "status", "scope": "global", "inject_id": "i1", "offset": offset}
        )
        if offset:
            header, _, content = content.partition("\n")
            assert header.startswith(f"[characters {offset} to ")
        match = re.search(r" \.\.\. \[truncated, \d+ more characters: read on with offset=(\d+)\]$", content)
        if not match:
            parts.append(content)
            break
        parts.append(content[: match.start()])
        offset = int(match.group(1))
    assert "".join(parts) == whole


async def test_inject_get_offset_past_the_end_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.integrations.providers.openaev import injects

    monkeypatch.setattr(injects, "_api", _Api({"inject_id": "i1"}))
    content = await _run(
        "manage_openaev_inject", {"action": "get", "scope": "global", "inject_id": "i1", "offset": 99999}
    )
    assert "past the end" in content


@pytest.mark.parametrize(
    ("module", "tool", "arguments"),
    [
        ("testing", "manage_openaev_atomic_testing", {"action": "get", "inject_id": "a1"}),
        ("dashboards", "manage_openaev_dashboard", {"action": "get", "dashboard_id": "d1"}),
    ],
)
async def test_single_object_reads_answer_the_whole_object(monkeypatch, module, tool, arguments):
    import importlib

    mod = importlib.import_module(f"app.integrations.providers.openaev.{module}")
    data = {"id": "x1", "fields": ["value " * 10 for _ in range(60)]}
    assert 3000 < len(json.dumps(data)) < JSON_READ_CHARS
    monkeypatch.setattr(mod, "_api", _Api(data))
    content = await _run(tool, arguments)
    assert json.loads(content) == data


def test_no_silent_json_cut_left_in_the_package():
    silent = re.compile(r"json\.dumps\(.*\)\[:\s*\d+\s*\]")
    offenders = [
        f"{path.name}:{number}"
        for path in sorted(_PACKAGE.glob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if silent.search(line)
    ]
    assert not offenders, f"cut JSON without a marker (use json_answer): {offenders}"
