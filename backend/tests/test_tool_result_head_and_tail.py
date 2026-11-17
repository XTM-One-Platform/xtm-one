"""A cut tool result keeps its head AND its tail (issue #10364).

A log or an API answer prints its error summary and its continuation token
last: the agent loop's per-result cap and the declarative executor's text cut
keep the end of the text with its start, and say what was left out between.
The read (GET) operations of an OpenAPI integration declare
``workspace_ingest``, so ``to_workspace_path`` writes their whole answer.
"""

from __future__ import annotations

from app.integrations.providers import openapi as provider
from app.tools import agent_loop as al
from app.tools.base import CallableToolExecutor, clip_head_and_tail


def test_a_text_that_fits_is_returned_as_it_is():
    assert clip_head_and_tail("short", 10) == "short"
    assert al._truncate_tool_result("x" * 100, 100) == "x" * 100


def test_a_cut_keeps_three_quarters_of_head_and_one_quarter_of_tail():
    text = "H" * 1_000 + "M" * 10_000 + "T" * 1_000
    cut = clip_head_and_tail(text, 4_000, hint="Read the rest with offset.")
    head, rest = cut.split("\n\n... [TRUNCATED - ", 1)
    marker, tail = rest.split("] ...\n\n", 1)
    assert head == text[:3_000]
    assert tail == text[-1_000:] == "T" * 1_000
    assert marker.startswith("8,000 of 12,000 characters left out here")
    assert "the first 3,000 and the last 1,000" in marker
    assert marker.endswith("Read the rest with offset.")


def test_the_agent_loop_cap_keeps_the_tail_and_names_to_workspace_path():
    log = "\n".join(f"line {i}" for i in range(50_000)) + "\nERROR: 2 models failed\n"
    cut = al._truncate_tool_result(log, 20_000)
    assert cut.startswith("line 0\nline 1\n")
    assert cut.rstrip().endswith("ERROR: 2 models failed")
    assert "to_workspace_path" in cut
    assert len(cut) < 20_000 + 400


def _runtime(tools, **kwargs):
    return provider.OpenAPIRuntime(
        tools=tools,
        api_title="Demo",
        extra_config={"base_url": "https://api.example.test"},
        auth_type="bearer",
        **kwargs,
    )


def _tool(name, method):
    return {
        "name": name,
        "description": name,
        "input_schema": {"type": "object", "properties": {}},
        "execution": {"method": method, "path": f"/{name}"},
    }


def test_only_the_read_operations_of_an_openapi_integration_offer_to_workspace_path():
    definitions = provider.build_tools(
        "tk",
        CallableToolExecutor(),
        openapi_runtime=_runtime([_tool("get_log", "GET"), _tool("post_run", "POST"), _tool("delete_run", "delete")]),
    )
    ingest = {d.name: d.workspace_ingest for d in definitions}
    assert ingest == {"get_log": True, "post_run": False, "delete_run": False}


def test_a_refusing_openapi_tool_offers_no_workspace_path():
    definitions = provider.build_tools(
        "tk",
        CallableToolExecutor(),
        openapi_runtime=_runtime([_tool("get_log", "GET")], certificate_error="The certificate expired."),
    )
    assert [d.workspace_ingest for d in definitions] == [False]
