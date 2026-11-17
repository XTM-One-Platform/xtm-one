"""The n8n importer's coverage of n8n's generic nodes and app nodes.

`test_flow_import_n8n.py` pins the importer's contract on the nodes it mapped
first. This file pins the rest of n8n's node catalog:

- every generic node (data transformation, files, commands, forms, error
  workflows, legacy LangChain chains) lands on an XTM One node, never on an
  unknown one — the data nodes as a Code node running a generated script,
  which the tests below also RUN on the item shapes n8n hands them;
- an app node of an XTM One provider becomes an Integration node (operation
  picked from `flow_import_n8n_apps.N8N_APPS`, every tool name checked against
  the provider's real tools), its trigger an Integration event, a schedule or
  a webhook, its `*Tool` variant an Integration capability;
- an app with no provider stays unresolved with its CTEM verdict.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json

import pytest

from app.services import flow_import_n8n_scripts as n8n_scripts
from app.services.flow_graph import normalize_graph, pending_import_review, validate_graph
from app.services.flow_import import import_flow_document
from app.services.flow_import_n8n import parse_reference
from app.services.flow_import_n8n_apps import CTEM_CANDIDATES, N8N_APPS, outside_catalog_hint

BASE = "n8n-nodes-base."
LC = "@n8n/n8n-nodes-langchain."


def _n(name, node_type, params=None, *, version=1, **extra):
    return {
        "id": f"id-{name}",
        "name": name,
        "type": node_type,
        "typeVersion": version,
        "position": [0, 0],
        "parameters": params or {},
        **extra,
    }


def _main(*outputs):
    return {"main": [[{"node": target, "type": "main", "index": 0} for target in output] for output in outputs]}


def _wf(nodes, connections=None):
    return {"name": "Imported", "nodes": nodes, "connections": connections or {}}


def _import(workflow):
    imported = import_flow_document(workflow)
    return imported, normalize_graph(imported["graph"])


def _node(graph, node_id):
    return next(n for n in graph["nodes"] if n["id"] == node_id)


def _edges(graph):
    return {(e["source"], e["source_handle"], e["target"], e["target_handle"]) for e in graph["edges"]}


def _errors(graph):
    return [i.code for i in validate_graph(graph).issues if i.severity == "error"]


def _single(node_type, params=None, *, version=1):
    """Import `Start → Step` and return the converted step."""
    workflow = _wf(
        [_n("Start", BASE + "manualTrigger"), _n("Step", node_type, params, version=version)],
        {"Start": _main(["Step"])},
    )
    _imported, graph = _import(workflow)
    return _node(graph, "step"), graph


def _run(code, inputs):
    """Run a generated script as the Code node does: `inputs` is a dict."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        # The importer's own scripts, run here as the sandbox runs them.
        exec(compile(code, "<generated>", "exec"), {"inputs": dict(inputs)})  # noqa: S102
    return json.loads(out.getvalue())


def _settings(node):
    return json.loads(node["config"]["inputs"]["settings"])


# ── Generic nodes: never unknown ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("node_type", "params", "version", "kind"),
    [
        (BASE + "code", {"jsCode": "return items;"}, 2, "util.code"),
        (BASE + "function", {"functionCode": "return items;"}, 1, "util.code"),
        (BASE + "functionItem", {"functionCode": "return item;"}, 1, "util.code"),
        (BASE + "aiTransform", {"jsCode": "return $input.all();"}, 1, "util.code"),
        (BASE + "aggregate", {"aggregate": "aggregateAllItemData"}, 1, "util.code"),
        (BASE + "limit", {"maxItems": 3}, 1, "util.code"),
        (BASE + "sort", {"sortFieldsUi": {"sortField": [{"fieldName": "a"}]}}, 1, "util.code"),
        (BASE + "removeDuplicates", {}, 2, "util.code"),
        (BASE + "summarize", {"fieldsToSummarize": {"values": [{"field": "a"}]}}, 1, "util.code"),
        (BASE + "renameKeys", {"keys": {"key": [{"currentKey": "a", "newKey": "b"}]}}, 1, "util.code"),
        (BASE + "splitOut", {"fieldToSplitOut": "body"}, 1, "util.loop"),
        (BASE + "itemLists", {"fieldToSplitOut": "links"}, 1, "util.loop"),
        (
            BASE + "itemLists",
            {"operation": "sort", "sortFieldsUi": {"sortField": [{"fieldName": "a"}]}},
            3,
            "util.code",
        ),
        (BASE + "dateTime", {}, 2, "util.code"),
        (BASE + "crypto", {"action": "generate"}, 1, "util.code"),
        (BASE + "html", {"operation": "extractHtmlContent"}, 1.2, "util.code"),
        (BASE + "htmlExtract", {}, 1, "util.code"),
        (BASE + "markdown", {"html": "={{ $json.html }}"}, 1, "util.code"),
        (BASE + "xml", {}, 1, "util.code"),
        (BASE + "extractFromFile", {}, 1, "util.code"),
        (BASE + "extractFromFile", {"operation": "pdf"}, 1, "util.reroute"),
        (BASE + "convertToFile", {"operation": "toJson"}, 1.1, "util.code"),
        (BASE + "convertToFile", {"operation": "xlsx"}, 1.1, "util.reroute"),
        (BASE + "spreadsheetFile", {}, 1, "util.code"),
        (BASE + "moveBinaryData", {}, 1, "util.code"),
        (BASE + "compareDatasets", {}, 2.3, "util.code"),
        (BASE + "totp", {}, 1, "util.code"),
        (BASE + "executeCommand", {"command": "ls"}, 1, "action.runner_command"),
        (BASE + "ssh", {"command": "uptime"}, 1, "action.runner_command"),
        (BASE + "readWriteFile", {"fileSelector": "reports/a.txt"}, 1, "action.runner_command"),
        (BASE + "readBinaryFile", {"filePath": "reports/a.txt"}, 1, "action.runner_command"),
        (BASE + "writeBinaryFile", {"fileName": "reports/a.txt"}, 1, "action.runner_command"),
        (BASE + "git", {"operation": "pull"}, 1, "action.runner_command"),
        (BASE + "graphql", {"endpoint": "https://api.example/graphql", "query": "{ a }"}, 1, "action.http_request"),
        (BASE + "rssFeedRead", {"url": "https://feed.example/rss"}, 1, "integration"),
        (BASE + "form", {"formFields": {"values": [{"fieldLabel": "Code"}]}}, 1, "action.ask_user"),
        (BASE + "form", {"operation": "completion", "completionMessage": "Thanks"}, 1, "util.stop"),
        (BASE + "wait", {"resume": "webhook"}, 1.1, "util.wait_event"),
        (BASE + "wait", {"resume": "form"}, 1.1, "action.ask_user"),
        (BASE + "executionData", {}, 1, "util.reroute"),
        (BASE + "debugHelper", {}, 1, "util.reroute"),
        (BASE + "iCal", {}, 1, "util.reroute"),
        (BASE + "openAi", {"resource": "chat", "prompt": {"messages": [{"content": "Hi"}]}}, 1, "agent"),
        (LC + "chainSummarization", {}, 2, "agent"),
        (LC + "chainRetrievalQa", {"text": "={{ $json.q }}"}, 1.4, "agent"),
        (LC + "openAiAssistant", {"text": "Hi"}, 1.1, "agent"),
        (LC + "openAi", {"resource": "image", "operation": "analyze", "text": "What?"}, 1.8, "agent"),
    ],
)
def test_every_generic_node_lands_on_an_xtm_one_node(node_type, params, version, kind):
    step, graph = _single(node_type, params, version=version)
    assert step["kind"] == kind
    assert _errors(graph) == []


@pytest.mark.parametrize(
    ("node_type", "kind"),
    [
        (BASE + "start", "trigger.manual"),
        (BASE + "formTrigger", "trigger.manual"),
        (BASE + "errorTrigger", "trigger.event"),
        (BASE + "emailReadImap", "trigger.integration_event"),
    ],
)
def test_every_generic_trigger_lands_on_an_xtm_one_trigger(node_type, kind):
    _imported, graph = _import(_wf([_n("Begin", node_type)]))
    assert _node(graph, "begin")["kind"] == kind


def test_a_javascript_code_node_keeps_its_source_as_comments_and_waits_for_a_rewrite():
    step, _graph = _single(BASE + "code", {"jsCode": "const a = 1;\nreturn items;"}, version=2)
    assert step["config"]["code"] == (
        "# The n8n node ran the JavaScript below; this platform runs Python.\n"
        '# Rewrite it: the upstream output is in inputs["json"], and whatever this\n'
        "# script prints (JSON for an object) becomes the node's output.\n"
        "#\n"
        "# const a = 1;\n"
        "# return items;\n"
        'raise NotImplementedError("Rewrite the n8n JavaScript above in Python.")\n'
    )
    assert step["config"]["inputs"] == {"json": "{{start.payload}}"}
    assert pending_import_review(step) == [
        "n8n ran JavaScript here; this platform runs Python. The JavaScript is kept as comments: "
        "rewrite it, reading the input from `inputs['json']` and printing the result."
    ]


def test_the_legacy_item_node_reference_reads_that_nodes_first_item():
    assert parse_reference('$item("0").$node["Get Task"].json["Template Name"]') == ("Get Task", ["Template Name"])
    assert parse_reference("$item(1).$node['Get Task'].json.a") is None


# ── Data nodes: the generated script, run ────────────────────────────────────


def test_no_generated_script_holds_a_reference_delimiter():
    """The importer breaks every `{{` / `}}` it writes: a script must hold none."""
    for name, body in n8n_scripts.SCRIPTS.items():
        code = n8n_scripts.script(body)
        assert "{{" not in code and "}}" not in code, name
        compile(code, name, "exec")


def test_an_imported_data_node_runs_the_script_unchanged():
    step, _graph = _single(BASE + "aggregate", {"aggregate": "aggregateAllItemData"})
    assert step["config"]["code"] == n8n_scripts.script(n8n_scripts.AGGREGATE)


_ITEMS = [
    {"id": 3, "name": "b", "at": "2024-01-31T10:00:00Z"},
    {"id": 1, "name": "a", "at": "2024-02-29T23:30:00Z"},
    {"id": 3, "name": "b", "at": "2024-01-31T10:00:00Z"},
]


@pytest.mark.parametrize(
    ("node_type", "params", "version", "expected"),
    [
        (
            BASE + "aggregate",
            {"fieldsToAggregate": {"fieldToAggregate": [{"fieldToAggregate": "id"}]}},
            1,
            {"id": [3, 1, 3]},
        ),
        (
            BASE + "aggregate",
            {"aggregate": "aggregateAllItemData", "include": "specifiedFields", "fieldsToInclude": "id"},
            1,
            {"data": [{"id": 3}, {"id": 1}, {"id": 3}]},
        ),
        (BASE + "limit", {"maxItems": 2, "keep": "lastItems"}, 1, _ITEMS[1:]),
        # One item prints as the item: the Code node publishes its keys as `fields`.
        (BASE + "limit", {"maxItems": 1, "keep": "lastItems"}, 1, _ITEMS[2]),
        # More than the items: n8n keeps them all.
        (BASE + "limit", {"maxItems": 5, "keep": "lastItems"}, 1, _ITEMS),
        (
            BASE + "sort",
            {"sortFieldsUi": {"sortField": [{"fieldName": "id"}, {"fieldName": "name", "order": "descending"}]}},
            1,
            [_ITEMS[1], _ITEMS[0], _ITEMS[2]],
        ),
        (BASE + "removeDuplicates", {}, 2, _ITEMS[:2]),
        (
            BASE + "removeDuplicates",
            {"operation": "removeItemsSeenInPreviousExecutions", "dedupeValue": "={{ $json.name }}"},
            2,
            _ITEMS[:2],
        ),
        (
            BASE + "summarize",
            {
                "fieldsToSplitBy": "name",
                "fieldsToSummarize": {"values": [{"field": "id", "aggregation": "sum"}, {"field": "id"}]},
            },
            1.1,
            [{"name": "b", "sum_id": 6, "count_id": 2}, {"name": "a", "sum_id": 1, "count_id": 1}],
        ),
        (
            BASE + "renameKeys",
            {"keys": {"key": [{"currentKey": "name", "newKey": "label.text"}]}},
            1,
            [
                {"id": 3, "at": "2024-01-31T10:00:00Z", "label": {"text": "b"}},
                {"id": 1, "at": "2024-02-29T23:30:00Z", "label": {"text": "a"}},
                {"id": 3, "at": "2024-01-31T10:00:00Z", "label": {"text": "b"}},
            ],
        ),
        (
            BASE + "dateTime",
            {
                "operation": "addToDate",
                "magnitude": "={{ $json.at }}",
                "duration": 1,
                "timeUnit": "months",
                "outputFieldName": "later",
            },
            2,
            [
                {**_ITEMS[0], "later": "2024-02-29T10:00:00+00:00"},
                {**_ITEMS[1], "later": "2024-03-29T23:30:00+00:00"},
                {**_ITEMS[2], "later": "2024-02-29T10:00:00+00:00"},
            ],
        ),
        (
            BASE + "dateTime",
            {"value": "={{ $json.at }}", "toFormat": "DD/MM/YYYY [at] HH:mm", "dataPropertyName": "day"},
            1,
            [
                {**_ITEMS[0], "day": "31/01/2024 at 10:00"},
                {**_ITEMS[1], "day": "29/02/2024 at 23:30"},
                {**_ITEMS[2], "day": "31/01/2024 at 10:00"},
            ],
        ),
        (
            BASE + "crypto",
            {"type": "SHA256", "value": "={{ $json.name }}", "dataPropertyName": "hash"},
            1,
            [
                {**_ITEMS[0], "hash": "3e23e8160039594a33894f6564e1b1348bbd7a0088d42c4acb73eeaed59c009d"},
                {**_ITEMS[1], "hash": "ca978112ca1bbdcafac231b39a23dc4da786eff8147c4e72b9807785afee48bb"},
                {**_ITEMS[2], "hash": "3e23e8160039594a33894f6564e1b1348bbd7a0088d42c4acb73eeaed59c009d"},
            ],
        ),
        (
            BASE + "convertToFile",
            {},
            1.1,
            {"data": "id,name,at\n3,b,2024-01-31T10:00:00Z\n1,a,2024-02-29T23:30:00Z\n3,b,2024-01-31T10:00:00Z\n"},
        ),
    ],
)
def test_a_data_node_does_what_n8n_did_with_the_items(node_type, params, version, expected):
    step, _graph = _single(node_type, params, version=version)
    inputs = {**step["config"]["inputs"], "items": json.dumps(_ITEMS)}
    assert _run(step["config"]["code"], inputs) == expected


@pytest.mark.parametrize(
    ("start", "end", "units", "expected"),
    [
        # Luxon's diff: whole calendar steps, the rest as a fraction of the next step.
        ("2024-01-31", "2024-02-01", ["month"], {"months": pytest.approx(1 / 29)}),
        ("2023-12-31", "2024-01-01", ["year"], {"years": pytest.approx(1 / 366)}),
        ("2024-01-01", "2024-01-18", ["week"], {"weeks": pytest.approx(2 + 3 / 7)}),
        (
            "2024-01-31T10:00:00Z",
            "2024-03-01T12:30:00Z",
            ["month", "day", "hour"],
            {"months": 1, "days": 1, "hours": 2.5},
        ),
        ("2024-01-01T00:00:00Z", "2024-01-01T01:30:00Z", ["hour", "minute"], {"hours": 1, "minutes": 30}),
        ("2024-02-01", "2024-01-31", ["day"], {"days": -1}),
    ],
)
def test_time_between_dates_counts_like_luxon(start, end, units, expected):
    step, _graph = _single(
        BASE + "dateTime",
        {
            "operation": "getTimeBetweenDates",
            "startDate": start,
            "endDate": end,
            "units": units,
            "outputFieldName": "d",
        },
        version=2,
    )
    assert _run(step["config"]["code"], {**step["config"]["inputs"], "items": "{}"}) == {"d": expected}


@pytest.mark.parametrize(
    ("value", "item"),
    [
        # Rendered once: hashed as written, never re-serialised.
        ('{"a":1, "b": [1,2]}', {}),
        # Read in the item: serialised compactly, as n8n's expressions do.
        ("={{ $json.user }}", {"user": {"a": 1, "b": [1, 2]}}),
    ],
)
def test_a_hash_reads_json_looking_text_as_n8n_wrote_it(value, item):
    step, _graph = _single(BASE + "crypto", {"type": "SHA256", "value": value, "dataPropertyName": "h"})
    text = value if not value.startswith("=") else '{"a":1,"b":[1,2]}'
    out = _run(step["config"]["code"], {**step["config"]["inputs"], "items": json.dumps(item)})
    assert out["h"] == hashlib.sha256(text.encode()).hexdigest()


def test_a_per_item_parameter_becomes_a_path_read_in_each_item():
    step, _graph = _single(BASE + "crypto", {"value": "={{ $json.user.email }}", "dataPropertyName": "h"})
    assert _settings(step)["paths"] == {"value": "user.email"}
    assert "value" not in step["config"]["inputs"]


def test_a_parameter_reading_another_node_is_rendered_once():
    workflow = _wf(
        [
            _n("Start", BASE + "manualTrigger"),
            _n("Hash", BASE + "crypto", {"value": "={{ $('Start').item.json.token }}"}),
        ],
        {"Start": _main(["Hash"])},
    )
    _imported, graph = _import(workflow)
    hash_node = _node(graph, "hash")
    assert hash_node["config"]["inputs"]["value"] == "{{start.payload.token}}"
    assert "paths" not in _settings(hash_node)


def test_an_hmac_literal_secret_never_crosses_over():
    step, _graph = _single(
        BASE + "crypto", {"action": "hmac", "type": "SHA256", "value": "x", "secret": "s3cr3t-value"}
    )
    assert "s3cr3t-value" not in json.dumps(step)
    assert pending_import_review(step) == [
        "The literal value of the `secret` parameter was not imported: store it as a secret variable and reference it."
    ]


def test_html_extraction_reads_css_selectors_like_n8n():
    page = (
        '<html><head><meta property="og:title" content="Title"></head><body>'
        '<div id="main" class="list"><h3><span class="t-item">One</span></h3>'
        '<a href="/episode/1">E1</a><a href="/other">O</a><a href="/episode/2">E2</a></div>'
        "<p>Two <b>bold</b></p></body></html>"
    )
    params = {
        "operation": "extractHtmlContent",
        "extractionValues": {
            "values": [
                {"key": "title", "cssSelector": "div#main > h3 span.t-item"},
                {
                    "key": "episodes",
                    "cssSelector": 'a[href*="/episode/"]',
                    "returnValue": "attribute",
                    "attribute": "href",
                    "returnArray": True,
                },
                {
                    "key": "og",
                    "cssSelector": 'meta[property^="og:title"], meta[name="og:title"]',
                    "returnValue": "attribute",
                    "attribute": "content",
                },
                {"key": "para", "cssSelector": "p", "returnValue": "html"},
                {"key": "first", "cssSelector": "a:first-child"},
            ]
        },
    }
    step, _graph = _single(BASE + "html", params, version=1.2)
    assert pending_import_review(step) == []
    result = _run(step["config"]["code"], {**step["config"]["inputs"], "items": json.dumps({"data": page})})
    assert result == {
        "title": "One",
        "episodes": ["/episode/1", "/episode/2"],
        "og": "Title",
        "para": "Two <b>bold</b>",
        "first": "",
    }


def test_an_html_selector_this_engine_cannot_read_is_flagged():
    params = {
        "operation": "extractHtmlContent",
        "extractionValues": {"values": [{"key": "x", "cssSelector": "li:nth-of-type(2) + li"}]},
    }
    step, _graph = _single(BASE + "html", params, version=1.2)
    assert pending_import_review(step) == [
        "The CSS selector `li:nth-of-type(2) + li` uses a form this extraction does not read."
    ]


def test_xml_and_markdown_round_their_values_like_n8n():
    xml_step, _graph = _single(BASE + "xml", {})
    parsed = _run(
        xml_step["config"]["code"],
        {**xml_step["config"]["inputs"], "items": json.dumps({"data": '<feed v="1"><i>a</i><i>b</i></feed>'})},
    )
    assert parsed == {"feed": {"v": "1", "i": ["a", "b"]}}

    md_step, _graph = _single(BASE + "markdown", {"html": "={{ $json.html }}", "destinationKey": "md"})
    converted = _run(
        md_step["config"]["code"],
        {**md_step["config"]["inputs"], "items": json.dumps({"html": "<h2>T</h2><p>a <a href='/x'>link</a></p>"})},
    )
    assert converted == {"html": "<h2>T</h2><p>a <a href='/x'>link</a></p>", "md": "## T\n\na [link](/x)"}


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        # n8n's defaults: attributes merged, whitespace-only text dropped, other text as written.
        ({}, {"Feed": {"v": "1", "i": [" a ", {"k": "2", "_": "b"}]}}),
        ({"mergeAttrs": False}, {"Feed": {"$": {"v": "1"}, "i": [" a ", {"$": {"k": "2"}, "_": "b"}]}}),
        ({"ignoreAttrs": True, "trim": True}, {"Feed": {"i": ["a", "b"]}}),
        ({"normalizeTags": True, "explicitArray": True}, {"feed": {"v": ["1"], "i": [" a ", {"k": ["2"], "_": "b"}]}}),
        ({"explicitRoot": False}, {"v": "1", "i": [" a ", {"k": "2", "_": "b"}]}),
    ],
)
def test_xml_to_json_parses_like_xml2js(options, expected):
    step, _graph = _single(BASE + "xml", {"options": options})
    document = '<Feed v="1">\n  <i> a </i>\n  <i k="2">b</i>\n</Feed>'
    assert (
        _run(step["config"]["code"], {**step["config"]["inputs"], "items": json.dumps({"data": document})}) == expected
    )
    assert pending_import_review(step) == []


@pytest.mark.parametrize(
    ("params", "text", "expected"),
    [
        # Under the destination field, one item, as n8n's Extract from File writes it.
        ({"operation": "fromJson"}, '[{"a": 1}, {"a": 2}]', {"data": [{"a": 1}, {"a": 2}]}),
        ({"operation": "fromJson", "destinationKey": "doc.body"}, '{"a": 1}', {"doc": {"body": {"a": 1}}}),
        ({"operation": "fromJson"}, "", {"data": {}}),
        (
            {"operation": "fromIcs", "destinationKey": "calendar"},
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:Patch day\nEND:VEVENT\nEND:VCALENDAR",
            {"calendar": [{"summary": "Patch day"}]},
        ),
        ({"operation": "text", "destinationKey": "page.text"}, "hello", {"page": {"text": "hello"}}),
        # Text stays as written, even when it looks like JSON.
        ({"operation": "text"}, '{"a":1,  "b":2}', {"data": '{"a":1,  "b":2}'}),
    ],
)
def test_file_extraction_writes_under_the_destination_field(params, text, expected):
    step, _graph = _single(BASE + "extractFromFile", params)
    assert _run(step["config"]["code"], {**step["config"]["inputs"], "data": text}) == expected


@pytest.mark.parametrize(
    ("node_type", "params", "version", "expected"),
    [
        # n8n's JSON.stringify: compact unless Format is on.
        (BASE + "convertToFile", {"operation": "toJson"}, 1.1, {"data": '[{"a":1,"b":"é"}]'}),
        (
            BASE + "convertToFile",
            {"operation": "toJson", "options": {"format": True}},
            1.1,
            {"data": '[\n  {\n    "a": 1,\n    "b": "é"\n  }\n]'},
        ),
        (BASE + "moveBinaryData", {"mode": "jsonToBinary"}, 1, {"data": '{"a":1,"b":"é"}'}),
    ],
)
def test_json_written_to_a_file_is_serialised_like_n8n(node_type, params, version, expected):
    step, _graph = _single(node_type, params, version=version)
    items = json.dumps([{"a": 1, "b": "é"}])
    assert _run(step["config"]["code"], {**step["config"]["inputs"], "items": items}) == expected


@pytest.mark.parametrize("operation", ["fromJson", "text"])
def test_a_downloaded_file_is_read_as_written_even_with_one_text_field(operation):
    workflow = _wf(
        [
            _n("Start", BASE + "manualTrigger"),
            _n("Download", BASE + "httpRequest", {"url": "https://example.com/status.json"}, version=4.2),
            _n("Parse", BASE + "extractFromFile", {"operation": operation}),
        ],
        {"Start": _main(["Download"]), "Download": _main(["Parse"])},
    )
    _imported, graph = _import(workflow)
    parse = _node(graph, "parse")
    assert parse["config"]["inputs"]["data"] == "{{download.body}}"
    out = _run(parse["config"]["code"], {**parse["config"]["inputs"], "data": '{"status":"ok"}'})
    assert out == {"data": {"status": "ok"} if operation == "fromJson" else '{"status":"ok"}'}


@pytest.mark.parametrize(
    ("writer", "params"),
    [
        ("convertToFile", {"operation": "toJson", "binaryPropertyName": "file"}),
        ("spreadsheetFile", {"operation": "toFile", "fileFormat": "csv", "binaryPropertyName": "file"}),
        ("moveBinaryData", {"mode": "jsonToBinary", "destinationKey": "file"}),
    ],
)
def test_a_file_written_upstream_is_read_in_its_binary_property(writer, params):
    workflow = _wf(
        [
            _n("Start", BASE + "manualTrigger"),
            _n("Write", BASE + writer, params, version=1.1 if writer == "convertToFile" else 1),
            _n("Parse", BASE + "extractFromFile", {"operation": "text", "binaryPropertyName": "file"}),
        ],
        {"Start": _main(["Write"]), "Write": _main(["Parse"])},
    )
    _imported, graph = _import(workflow)
    write, parse = _node(graph, "write"), _node(graph, "parse")
    assert parse["config"]["inputs"]["data"] == "{{write.fields.file}}"
    written = _run(write["config"]["code"], {**write["config"]["inputs"], "items": json.dumps([{"a": "x"}])})
    assert set(written) == {"file"}
    out = _run(parse["config"]["code"], {**parse["config"]["inputs"], "data": written["file"]})
    assert out == {"data": written["file"]}


def test_xml_keeps_qualified_names_and_namespace_declarations_like_xml2js():
    step, _graph = _single(BASE + "xml", {})
    document = (
        '<feed xmlns="http://www.w3.org/2005/Atom" xmlns:dc="http://purl.org/dc/elements/1.1/">'
        "<entry><dc:creator>Ann</dc:creator><media:title>T</media:title></entry></feed>"
    )
    parsed = _run(step["config"]["code"], {**step["config"]["inputs"], "items": json.dumps({"data": document})})
    assert parsed == {
        "feed": {
            "xmlns": "http://www.w3.org/2005/Atom",
            "xmlns:dc": "http://purl.org/dc/elements/1.1/",
            "entry": {"dc:creator": "Ann", "media:title": "T"},
        }
    }


@pytest.mark.parametrize(
    ("upstream", "params", "expected"),
    [
        # A file read on the runner into the property the extraction reads: the result's `content`.
        ("readBinaryFile", {"filePath": "reports/a.json", "dataPropertyName": "file"}, "{{upstream.result.content}}"),
        (
            "readWriteFile",
            {"fileSelector": "reports/a.json", "options": {"dataPropertyName": "file"}},
            "{{upstream.result.content}}",
        ),
        # Another property than the one the extraction reads: the whole input, as written.
        ("readWriteFile", {"fileSelector": "reports/a.json"}, "{{upstream.result}}"),
    ],
)
def test_a_file_read_on_the_runner_is_extracted_from_its_content(upstream, params, expected):
    workflow = _wf(
        [
            _n("Start", BASE + "manualTrigger"),
            _n("Upstream", BASE + upstream, params),
            _n("Parse", BASE + "extractFromFile", {"operation": "fromJson", "binaryPropertyName": "file"}),
        ],
        {"Start": _main(["Upstream"]), "Upstream": _main(["Parse"])},
    )
    _imported, graph = _import(workflow)
    assert _node(graph, "parse")["config"]["inputs"]["data"] == expected


@pytest.mark.parametrize(
    ("upstream", "params", "content"),
    [
        ("convertToFile", {"operation": "toText", "sourceProperty": "body"}, "{{upstream.fields.data}}"),
        ("httpRequest", {"url": "https://example.com/a.txt"}, "{{upstream.body}}"),
    ],
)
def test_a_file_write_takes_the_file_in_its_binary_property(upstream, params, content):
    workflow = _wf(
        [
            _n("Start", BASE + "manualTrigger"),
            _n("Upstream", BASE + upstream, params, version=4.2 if upstream == "httpRequest" else 1.1),
            _n("Save", BASE + "writeBinaryFile", {"fileName": "reports/a.txt"}),
        ],
        {"Start": _main(["Upstream"]), "Upstream": _main(["Save"])},
    )
    _imported, graph = _import(workflow)
    assert _node(graph, "save")["config"]["payload"]["content"] == content


def test_csv_extraction_reads_the_text_that_reaches_it():
    workflow = _wf(
        [
            _n("Start", BASE + "manualTrigger"),
            _n("Download", BASE + "httpRequest", {"url": "https://example.com/a.csv"}, version=4.2),
            _n("Parse", BASE + "extractFromFile", {"options": {"delimiter": ";"}}),
        ],
        {"Start": _main(["Download"]), "Download": _main(["Parse"])},
    )
    _imported, graph = _import(workflow)
    parse = _node(graph, "parse")
    assert parse["config"]["inputs"]["data"] == "{{download.body}}"
    rows = _run(parse["config"]["code"], {**parse["config"]["inputs"], "data": "a;b\n1;2\n3;4"})
    assert rows == [{"a": "1", "b": "2"}, {"a": "3", "b": "4"}]


def test_a_binary_file_step_passes_its_input_through_to_review():
    step, _graph = _single(BASE + "extractFromFile", {"operation": "pdf"})
    assert pending_import_review(step) == [
        "n8n extracted the content of a `pdf` file. XTM One flows pass text, not files: agents read "
        "the files attached to a run and the files of their integrations directly, so this step "
        "usually goes; it passes its input through meanwhile."
    ]


def test_split_out_iterates_the_field_and_hangs_the_next_nodes_from_each_item():
    workflow = _wf(
        [
            _n("Start", BASE + "manualTrigger"),
            _n("Fetch", BASE + "httpRequest", {"url": "https://api.example/items"}, version=4.2),
            _n("Split", BASE + "splitOut", {"fieldToSplitOut": "data.items"}),
            _n("Each", BASE + "httpRequest", {"url": "=https://api.example/{{ $json.id }}"}, version=4.2),
        ],
        {"Start": _main(["Fetch"]), "Fetch": _main(["Split"]), "Split": _main(["Each"])},
    )
    _imported, graph = _import(workflow)
    assert _node(graph, "split")["config"] == {"items": "{{fetch.body.data.items}}"}
    assert ("split", "each", "each", "in") in _edges(graph)
    assert _node(graph, "each")["config"]["url"] == "https://api.example/{{split.item.id}}"
    assert pending_import_review(_node(graph, "split")) == []


def test_compare_datasets_reads_its_two_inputs_apart():
    workflow = _wf(
        [
            _n("Start", BASE + "manualTrigger"),
            _n("A", BASE + "httpRequest", {"url": "https://a.example"}, version=4.2),
            _n("B", BASE + "httpRequest", {"url": "https://b.example"}, version=4.2),
            _n("Compare", BASE + "compareDatasets", {"mergeByFields": {"values": [{"field1": "id", "field2": "id"}]}}),
        ],
        {
            "Start": _main(["A", "B"]),
            "A": _main(["Compare"]),
            "B": {"main": [[{"node": "Compare", "type": "main", "index": 1}]]},
        },
    )
    _imported, graph = _import(workflow)
    compare = _node(graph, "compare")
    assert compare["config"]["inputs"]["input_a"] == "{{a.body}}"
    assert compare["config"]["inputs"]["input_b"] == "{{b.body}}"
    result = _run(
        compare["config"]["code"],
        {
            **compare["config"]["inputs"],
            "input_a": json.dumps([{"id": 1, "v": 1}, {"id": 2, "v": 2}]),
            "input_b": json.dumps([{"id": 2, "v": 3}, {"id": 4, "v": 4}]),
        },
    )
    assert result == {
        "in_a_only": [{"id": 1, "v": 1}],
        "same": [],
        "different": [{"input_a": {"id": 2, "v": 2}, "input_b": {"id": 2, "v": 3}}],
        "in_b_only": [{"id": 4, "v": 4}],
    }


def test_graphql_becomes_a_json_post_with_the_query_escaped():
    query = 'query {\n  country(code: "FR") { name }\n}'
    step, _graph = _single(BASE + "graphql", {"endpoint": "https://api.example/graphql", "query": query})
    assert step["config"]["method"] == "POST"
    assert json.loads(step["config"]["body"]) == {"query": query, "variables": {}}


def test_graphql_variables_given_as_an_object_have_their_references_translated():
    params = {"endpoint": "https://api.example/graphql", "query": "query{a}", "variables": {"id": "={{ $json.id }}"}}
    step, _graph = _single(BASE + "graphql", params)
    body = json.loads(step["config"]["body"])
    assert body == {"query": "query{a}", "variables": {"id": "{{start.payload.id}}"}}


# ── Commands and files: a runner ─────────────────────────────────────────────


def test_execute_command_runs_on_a_runner_and_its_error_output_is_the_fail_pin():
    workflow = _wf(
        [
            _n("Start", BASE + "manualTrigger"),
            _n("Run", BASE + "executeCommand", {"command": "=ls {{ $json.dir }}"}, onError="continueErrorOutput"),
            _n("Ok", BASE + "noOp"),
            _n("Failed", BASE + "noOp"),
        ],
        {"Start": _main(["Run"]), "Run": _main(["Ok"], ["Failed"])},
    )
    _imported, graph = _import(workflow)
    run = _node(graph, "run")
    assert run["config"] == {
        "command_kind": "run_shell",
        "payload": {"command": "ls {{start.payload.dir}}"},
        "mode": "wait",
    }
    assert _edges(graph) >= {("run", "ok", "ok", "in"), ("run", "fail", "failed", "in")}
    assert pending_import_review(run) == [
        "Read the imported command before a runner executes it on your machine.",
        "Values from other nodes are inserted into the command as they are, unquoted: "
        "quote them, or check that no caller can put shell syntax in them.",
    ]


@pytest.mark.parametrize(
    ("node_type", "params"),
    [
        (BASE + "executeCommand", {"command": "uptime"}),
        (BASE + "ssh", {"command": "uptime"}),
        (BASE + "git", {"operation": "log"}),
        (BASE + "readWriteFile", {"operation": "read", "fileSelector": "/home/me/.ssh/id_rsa"}),
        (BASE + "readBinaryFile", {"filePath": "/home/me/.aws/credentials"}),
        (BASE + "readWriteFile", {"operation": "write", "fileName": "reports/a.txt"}),
    ],
)
def test_every_imported_runner_command_waits_for_a_review(node_type, params):
    step, _graph = _single(node_type, params)
    assert step["kind"] == "action.runner_command"
    assert pending_import_review(step) != []


def test_a_literal_command_needs_only_the_reading_review():
    step, _graph = _single(BASE + "executeCommand", {"command": "uptime"})
    assert pending_import_review(step) == ["Read the imported command before a runner executes it on your machine."]


def test_the_execute_command_item_reads_the_runner_result():
    workflow = _wf(
        [
            _n("Start", BASE + "manualTrigger"),
            _n("Run", BASE + "executeCommand", {"command": "uptime"}),
            _n("Mail", BASE + "emailSend", {"toEmail": "a@example.com", "text": "={{ $json.stdout }}"}),
            _n("Mail2", BASE + "emailSend", {"toEmail": "a@example.com", "text": "={{ $('Run').item.json.exitCode }}"}),
            _n("Mail3", BASE + "emailSend", {"toEmail": "a@example.com", "text": "={{ $('Run').item.json.stderr }}"}),
        ],
        {"Start": _main(["Run"]), "Run": _main(["Mail"]), "Mail": _main(["Mail2"]), "Mail2": _main(["Mail3"])},
    )
    _imported, graph = _import(workflow)
    assert _node(graph, "mail")["config"]["message"] == "{{run.result.stdout}}"
    assert _node(graph, "mail2")["config"]["message"] == "{{run.result.exit_code}}"
    assert _node(graph, "mail3")["config"]["message"] == "{{run.result.stderr}}"


@pytest.mark.parametrize(
    ("node_type", "params", "command"),
    [
        (BASE + "ssh", {"command": "ls", "cwd": "/srv/My Reports"}, "cd '/srv/My Reports' && ls"),
        (BASE + "ssh", {"command": "ls", "cwd": "/"}, "ls"),
        (BASE + "git", {"operation": "log", "repositoryPath": "/home/me/my repo"}, "git -C '/home/me/my repo' log"),
        (BASE + "git", {"operation": "log; rm -rf ~", "repositoryPath": "/repo"}, "git -C /repo status"),
    ],
)
def test_a_literal_path_is_quoted_into_the_shell_command(node_type, params, command):
    step, _graph = _single(node_type, params)
    assert step["config"]["payload"] == {"command": command}


# ── Forms, error workflows ───────────────────────────────────────────────────


def test_a_form_trigger_starts_the_flow_by_hand_with_a_review():
    params = {
        "formTitle": "Report a phishing",
        "formDescription": "Paste the suspicious URL",
        "formFields": {"values": [{"fieldLabel": "URL"}, {"fieldLabel": "Email"}]},
    }
    _imported, graph = _import(_wf([_n("Form", BASE + "formTrigger", params, version=2.2)]))
    form = _node(graph, "form")
    assert form["config"] == {"prompt": "Report a phishing\n\nPaste the suspicious URL"}
    assert pending_import_review(form) == [
        "n8n served a public form (fields: URL, Email); XTM One has no public form: a person runs "
        "the flow by hand and the fields arrive in the run's payload, or a caller posts them to a "
        "Webhook trigger."
    ]


def test_send_and_wait_becomes_an_ask_user():
    step, _graph = _single(
        BASE + "slack",
        {"operation": "sendAndWait", "message": "=Approve {{ $json.cve }}?", "subject": "Approval"},
        version=2.3,
    )
    assert step["kind"] == "action.ask_user"
    assert step["config"] == {"title": "Approval", "question": "Approve {{start.payload.cve}}?"}


# ── App nodes ────────────────────────────────────────────────────────────────


def test_every_app_operation_names_a_real_tool_of_its_provider():
    from app.api.v1.integration_catalog import build_provider_tools
    from app.services.integration_tools import PROVIDER_TOOLS

    tools = build_provider_tools()
    missing = {
        (key, tool)
        for key, app in N8N_APPS.items()
        for tool in app.operations.values()
        if tool not in {t.name for t in tools.get(app.provider, [])}
    }
    assert {app.provider for app in N8N_APPS.values()} - set(PROVIDER_TOOLS) == set()
    assert missing == set()


def test_an_app_node_of_a_provider_becomes_an_integration_node():
    params = {"owner": "acme", "repository": "infra", "title": "=CVE {{ $json.cve }}", "body": "Patch it"}
    step, graph = _single(BASE + "github", params)
    assert step["kind"] == "integration"
    assert step["config"] == {
        "operation": "create_github_issue",
        "parameters": {
            "owner": "acme",
            "repository": "infra",
            "title": "CVE {{start.payload.cve}}",
            "body": "Patch it",
        },
    }
    assert pending_import_review(step) == [
        "n8n's GitHub node did `issue: create`, the `create_github_issue` operation here: its "
        "parameters keep their n8n names under Parameters; rename them to the operation's arguments."
    ]
    assert _errors(graph) == []


@pytest.mark.parametrize(
    ("node_type", "params", "operation"),
    [
        (BASE + "googleSheets", {"operation": "append", "sheetName": "Assets"}, "append_google_sheet_rows"),
        (BASE + "googleSheets", {"sheetName": "Assets"}, "get_google_sheet_values"),
        (BASE + "gitlab", {"resource": "file", "operation": "edit"}, "update_gitlab_file"),
        (BASE + "zendesk", {"operation": "getAll"}, "search_zendesk_tickets"),
        (BASE + "pagerDuty", {"operation": "update"}, "update_pagerduty_incident"),
        (
            BASE + "securityScorecard",
            {"resource": "report", "operation": "generate"},
            "generate_securityscorecard_report",
        ),
        (BASE + "mattermost", {"message": "New critical exposure"}, "post_mattermost_message"),
    ],
)
def test_a_former_catalog_candidate_with_a_provider_now_becomes_an_integration_node(node_type, params, operation):
    step, _graph = _single(node_type, params)
    assert step["kind"] == "integration"
    assert step["config"]["operation"] == operation
    assert node_type.removeprefix(BASE) not in CTEM_CANDIDATES


def test_an_app_operation_with_no_tool_leaves_the_operation_to_pick():
    step, _graph = _single(BASE + "googleDrive", {"operation": "share", "fileId": "abc"}, version=3)
    assert step["kind"] == "integration"
    assert step["config"] == {"parameters": {"fileId": "abc"}}
    assert pending_import_review(step) == [
        "n8n's Google Drive node did `file: share`: pick the Google Drive operation that does the "
        "same and set its parameters (n8n's are kept under Parameters)."
    ]


def test_a_literal_secret_in_app_parameters_never_crosses_over():
    step, _graph = _single(BASE + "jira", {"summary": "x", "apiKey": "abc-123-secret"}, version=1)
    assert "abc-123-secret" not in json.dumps(step["config"])


@pytest.mark.parametrize(
    ("node_type", "hint"),
    [
        (BASE + "linear", outside_catalog_hint("linear")),
        (BASE + "spotify", outside_catalog_hint("spotify")),
        (BASE + "telegram", None),
    ],
)
def test_an_app_with_no_provider_stays_unresolved_with_its_ctem_verdict(node_type, hint):
    step, _graph = _single(node_type, {"resource": "file", "operation": "get"})
    assert step["kind"] == "unresolved"
    if hint is not None:
        assert step["source_payload"]["notes"][0] == hint


def test_the_ctem_verdict_tells_a_candidate_from_an_app_outside_the_catalog():
    assert outside_catalog_hint("linearTool").startswith("XTM One has no Linear integration yet")
    assert "linear" in CTEM_CANDIDATES
    assert outside_catalog_hint("spotify").startswith("The n8n `spotify` app is outside XTM One's integration catalog")


@pytest.mark.parametrize(
    ("node_type", "kind", "config"),
    [
        (BASE + "gmailTrigger", "trigger.integration_event", {"event_type": "new_email"}),
        (BASE + "microsoftOutlookTrigger", "trigger.integration_event", {"event_type": "new_email"}),
        (BASE + "githubTrigger", "trigger.webhook", {"path": "github"}),
        (BASE + "typeformTrigger", "trigger.webhook", {"path": "typeform"}),
        (BASE + "googleSheetsTrigger", "trigger.schedule", {"cron": "* * * * *"}),
        (BASE + "kafkaTrigger", "unresolved", {"source_type": BASE + "kafkaTrigger"}),
    ],
)
def test_an_app_trigger_becomes_its_event_a_poll_or_a_webhook(node_type, kind, config):
    _imported, graph = _import(_wf([_n("Begin", node_type)]))
    begin = _node(graph, "begin")
    assert (begin["kind"], begin["config"]) == (kind, config)


def test_an_app_tool_gives_the_agent_its_integration():
    workflow = _wf(
        [
            _n("Chat", LC + "chatTrigger"),
            _n("Agent", LC + "agent", {"text": "={{ $json.chatInput }}", "promptType": "define"}, version=1.7),
            _n("Mail", BASE + "gmailTool", {"operation": "getAll"}),
            _n("Base", BASE + "airtableTool", {}),
            _n("Docs", LC + "retrieverVectorStore", {}),
        ],
        {
            "Chat": _main(["Agent"]),
            "Mail": {"ai_tool": [[{"node": "Agent", "type": "ai_tool", "index": 0}]]},
            "Base": {"ai_tool": [[{"node": "Agent", "type": "ai_tool", "index": 0}]]},
            "Docs": {"ai_retriever": [[{"node": "Agent", "type": "ai_retriever", "index": 0}]]},
        },
    )
    _imported, graph = _import(workflow)
    assert (_node(graph, "mail")["kind"], _node(graph, "base")["kind"], _node(graph, "docs")["kind"]) == (
        "capability.integration",
        "capability.tool",
        "capability.knowledge_base",
    )
    assert _edges(graph) >= {
        ("mail", "provides", "agent", "integrations"),
        ("base", "provides", "agent", "tools"),
        ("docs", "provides", "agent", "knowledge_bases"),
    }


def test_an_ai_sub_node_attached_to_nothing_keeps_its_capability():
    _imported, graph = _import(
        _wf([_n("Model", LC + "lmChatOpenAi", {"model": "gpt-4o"}), _n("Memory", LC + "memoryBufferWindow")])
    )
    assert (_node(graph, "model")["kind"], _node(graph, "memory")["kind"]) == ("capability.llm", "capability.memory")
