"""Every Drata tool's Python signature agrees with its input schema.

A schema that marks a parameter optional (absent from ``required``, often with a
``default``) while the wrapped function requires it positionally makes a call that
omits it fail before Drata is reached, with "missing 1 required positional
argument". ``get_drata_monitoring_test``, ``list_drata_monitoring_test_failures``
and ``get_drata_audit`` declared ``workspace_id`` with ``default: 1`` and failed that
way whenever an agent left the argument out.
"""

from __future__ import annotations

import inspect

import pytest

from app.integrations.providers import drata
from app.integrations.providers.drata import _common as drata_common
from app.tools.base import CallableToolExecutor, ToolCall

_TOKEN = "https://public-api.drata.com/public/v2||test-token"
_BASE = "https://public-api.drata.com/public/v2"


def _build() -> tuple[CallableToolExecutor, dict]:
    executor = CallableToolExecutor()
    defs = {d.name: d for d in drata.build_tools(_TOKEN, executor)}
    return executor, defs


def test_every_optional_schema_parameter_has_a_python_default() -> None:
    executor, defs = _build()
    mismatches: list[str] = []
    for name, definition in sorted(defs.items()):
        handler = executor.handler(name)
        assert handler is not None, f"{name} has a definition but no registered handler"
        sig = inspect.signature(handler)
        schema = definition.input_schema or {}
        required = set(schema.get("required") or [])
        # A parameter the schema lists in ``required`` stays required in the
        # function (create, update and delete tools keep ``workspace_id`` mandatory);
        # only an optional schema parameter must be optional in the function too.
        for prop in (schema.get("properties") or {}):
            param = sig.parameters.get(prop)
            if param is None or param.default is not inspect.Parameter.empty or prop in required:
                continue
            mismatches.append(f"{name}.{prop}: optional in the schema, required by the function")
    assert not mismatches, "\n".join(mismatches)


def test_every_schema_default_matches_the_function_default() -> None:
    executor, defs = _build()
    mismatches: list[str] = []
    for name, definition in sorted(defs.items()):
        sig = inspect.signature(executor.handler(name))
        for prop, spec in ((definition.input_schema or {}).get("properties") or {}).items():
            if not isinstance(spec, dict) or "default" not in spec:
                continue
            param = sig.parameters.get(prop)
            if param is None or param.default is inspect.Parameter.empty:
                continue
            if param.default != spec["default"]:
                mismatches.append(
                    f"{name}.{prop}: schema default {spec['default']!r}, function default {param.default!r}"
                )
    assert not mismatches, "\n".join(mismatches)


class _Resp:
    status_code = 200
    is_success = True
    content = b"{}"
    text = "{}"

    def json(self):
        return {"id": 1, "data": [], "cursor": None}


@pytest.mark.parametrize(
    ("tool", "args", "path"),
    [
        ("get_drata_monitoring_test", {"test_id": 7}, "/workspaces/1/"),
        ("list_drata_monitoring_test_failures", {"test_id": 7}, "/workspaces/1/"),
        ("get_drata_audit", {"audit_id": "81584418-17a7-44b1-b0f9-47d42b903812"}, "/workspaces/1/audits/"),
    ],
)
async def test_workspace_id_defaults_to_one_when_omitted(monkeypatch, tool, args, path) -> None:
    urls: list[str] = []

    async def _fake_safe_request(client, method, url, provider, action, *, expected_statuses=None, **kwargs):
        urls.append(url)
        return _Resp(), None

    monkeypatch.setattr(drata_common, "safe_request", _fake_safe_request)
    executor, _defs = _build()
    result = await executor.execute_tool(ToolCall(id="c1", name=tool, arguments=args))
    assert "missing 1 required positional argument" not in str(result)
    assert urls, f"{tool} sent no request: {result}"
    assert all(u.startswith(_BASE) for u in urls)
    assert any(path in u for u in urls), urls


async def test_explicit_workspace_id_is_kept(monkeypatch) -> None:
    urls: list[str] = []

    async def _fake_safe_request(client, method, url, provider, action, *, expected_statuses=None, **kwargs):
        urls.append(url)
        return _Resp(), None

    monkeypatch.setattr(drata_common, "safe_request", _fake_safe_request)
    executor, _defs = _build()
    await executor.execute_tool(
        ToolCall(id="c1", name="get_drata_monitoring_test", arguments={"test_id": 7, "workspace_id": 3})
    )
    assert any("/workspaces/3/" in u for u in urls), urls
