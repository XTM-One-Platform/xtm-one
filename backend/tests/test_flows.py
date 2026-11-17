"""Tests for the visual flow builder backend.

Covers the four things that would silently rot otherwise:

* the **node catalog** stays internally consistent (a `select` with no options
  and no source would render an empty dropdown with no error anywhere);
* the **port compatibility matrix** — the rule that decides which connections
  the canvas may draw at all;
* **``{{node_id.output}}`` validation**, including the exemption for the two
  prefixes the existing assignment renderer already owns;
* the **LangFlow importer**, whose whole contract is "never lose a node
  silently".
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import deps
from app.api.v1 import flows as flows_api
from app.core.database import get_db
from app.services import flow_versioning as versioning
from app.services.flow_catalog import (
    CATEGORY_ORDER,
    DYNAMIC_SOURCES,
    EDGE_CAPABILITY,
    EDGE_CONDITION_BRANCH,
    EDGE_DATA,
    EDGE_ERROR,
    EDGE_HANDOVER,
    EDGE_KIND_BY_PORT_TYPE,
    FIELD_SELECT,
    FIELD_TYPES,
    NODE_KINDS,
    allowed_edge_kinds,
    field_applies,
    get_node_kind,
    get_port,
    has_llm_socket,
    static_catalog,
)
from app.services.flow_detach import split_graph
from app.services.flow_graph import (
    MAX_NODES,
    normalize_graph,
    validate_graph,
)
from app.services.flow_import import (
    FlowImportError,
    detect_format,
    import_flow_document,
)
from app.services.flow_references import called_flow_ids, check_flow_references

# ── Graph fixtures ────────────────────────────────────────────────────────────


def _node(node_id: str, kind: str, **config):
    return {
        "id": node_id,
        "kind": kind,
        "position": {"x": 0, "y": 0},
        "config": config,
    }


def _edge(edge_id, source, target, *, sh="out", th="in", kind=EDGE_DATA):
    return {
        "id": edge_id,
        "source": source,
        "source_handle": sh,
        "target": target,
        "target_handle": th,
        "kind": kind,
    }


def _straight_flow():
    """trigger → agent → send message, fully configured."""
    return {
        "nodes": [
            _node("t1", "trigger.schedule", cron="0 9 * * 1-5"),
            _node("a1", "agent", agent_id="x", prompt="Summarise {{t1.payload}}"),
            _node(
                "s1",
                "action.send_message",
                channel="slack",
                target="#ops",
                message="{{a1.output}}",
            ),
        ],
        "edges": [_edge("e1", "t1", "a1"), _edge("e2", "a1", "s1")],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }


def _codes(result):
    return [i.code for i in result.issues]


# ── Catalog consistency ───────────────────────────────────────────────────────


def test_catalog_kinds_are_unique_and_categorised():
    kinds = [nk.kind for nk in NODE_KINDS]
    assert len(kinds) == len(set(kinds)), "duplicate node kind"
    for nk in NODE_KINDS:
        assert nk.category in CATEGORY_ORDER, f"{nk.kind} has an unlisted category"
        assert nk.label and nk.icon and nk.description


def test_catalog_fields_are_renderable():
    """A field the builder cannot render is a silently broken config form."""
    for nk in NODE_KINDS:
        names = [f.name for f in nk.config_fields]
        assert len(names) == len(set(names)), f"{nk.kind} has a duplicate field"
        for cfg in nk.config_fields:
            assert cfg.type in FIELD_TYPES, f"{nk.kind}.{cfg.name}: {cfg.type}"
            if cfg.type == FIELD_SELECT:
                assert cfg.options or cfg.source, (
                    f"{nk.kind}.{cfg.name} is a select with neither options "
                    "nor a dynamic source"
                )
            if cfg.source:
                assert cfg.source in DYNAMIC_SOURCES


def test_catalog_ports_use_known_types():
    from app.services.flow_catalog import PORT_PARAM

    for nk in NODE_KINDS:
        for port in (*nk.inputs, *nk.outputs):
            assert port.types, f"{nk.kind}.{port.id} declares no type"
            for port_type in port.types:
                # PORT_PARAM is a data affordance: legal on a port, but
                # deliberately absent from the edge matrix (no edge kind).
                assert (
                    port_type in EDGE_KIND_BY_PORT_TYPE or port_type == PORT_PARAM
                )


def test_annotation_nodes_have_no_ports():
    """A port-less annotation is what makes notes safe to drop edges onto."""
    for nk in NODE_KINDS:
        if nk.is_annotation:
            assert not nk.inputs and not nk.outputs


def test_static_catalog_is_ordered_by_category():
    seen = [entry["category"] for entry in static_catalog()]
    positions = [CATEGORY_ORDER.index(c) for c in seen]
    assert positions == sorted(positions)


# ── Port compatibility ────────────────────────────────────────────────────────


def _pair(source_kind, source_port, target_kind, target_port):
    src = get_port(get_node_kind(source_kind), source_port, output=True)
    dst = get_port(get_node_kind(target_kind), target_port, output=False)
    return allowed_edge_kinds(src, dst)


def test_trigger_connects_to_agent_as_data():
    assert _pair("trigger.schedule", "out", "agent", "in") == {EDGE_DATA}


def test_agent_to_agent_offers_both_data_and_handover():
    assert _pair("agent", "out", "agent", "in") == {EDGE_DATA}
    assert _pair("agent", "handover", "agent", "in") == {EDGE_HANDOVER}


def test_condition_branch_is_typed_as_a_branch():
    assert _pair("condition.if", "true", "agent", "in") == {EDGE_CONDITION_BRANCH}


def test_error_port_is_typed_as_an_error():
    assert _pair("agent", "error", "action.send_message", "in") == {EDGE_ERROR}


def test_handover_cannot_target_a_non_agent():
    assert _pair("agent", "handover", "action.send_message", "in") == set()


def test_trigger_has_no_input_port():
    assert get_port(get_node_kind("trigger.manual"), "in", output=False) is None


def test_chat_trigger_only_plugs_into_an_agent():
    """The Chat start point's own channel: an agent accepts it, nothing else."""
    assert _pair("trigger.chat", "out", "agent", "in") == {EDGE_DATA}
    assert _pair("trigger.chat", "out", "condition.if", "in") == set()
    assert _pair("trigger.chat", "out", "action.send_message", "in") == set()
    assert _pair("trigger.chat", "out", "util.break", "in") == set()


def test_agent_behind_chat_trigger_warns_when_chat_toggle_is_off():
    graph = {
        "nodes": [
            {"id": "c1", "kind": "trigger.chat", "position": {"x": 0, "y": 0}},
            {
                "id": "ag",
                "kind": "agent",
                "position": {"x": 1, "y": 0},
                "config": {"agent_id": str(uuid.uuid4()), "prompt": "réponds"},
            },
        ],
        "edges": [
            {
                "id": "e1",
                "source": "c1",
                "source_handle": "out",
                "target": "ag",
                "target_handle": "in",
                "kind": "data",
            }
        ],
    }
    result = validate_graph(graph)
    assert result.is_valid, "a warning, never a save blocker"
    assert "agent.chat_disabled_behind_chat_trigger" in {
        i.code for i in result.issues
    }

    graph["nodes"][1]["config"]["chat_enabled"] = True
    result = validate_graph(graph)
    assert "agent.chat_disabled_behind_chat_trigger" not in {
        i.code for i in result.issues
    }


def test_edge_without_handles_validates_as_default_ports():
    """A hand-written import may omit handles entirely — the walker reads a
    missing source handle as `out`, so validation must resolve the same
    default (and never crash on the None)."""
    graph = {
        "nodes": [
            {
                "id": "t1",
                "kind": "trigger.manual",
                "position": {"x": 0, "y": 0},
                "config": {},
            },
            {
                "id": "c1",
                "kind": "util.cast",
                "position": {"x": 200, "y": 0},
                "config": {"value": "x", "to": "text"},
            },
            {
                "id": "c2",
                "kind": "util.cast",
                "position": {"x": 400, "y": 0},
                "config": {"value": "y", "to": "text"},
            },
        ],
        "edges": [
            # No handles at all: defaults to out → in, which both kinds have.
            {"id": "e1", "source": "t1", "target": "c1"},
            # Cast has no `out` output (its outputs are ok/fail): the default
            # resolves to nothing — a validation issue, not a 500.
            {"id": "e2", "source": "c1", "target": "c2"},
        ],
    }
    result = validate_graph(graph)
    codes = {i.code for i in result.issues}
    assert "edge.unknown_source_port" in codes
    blocking = {i.code for i in result.issues if i.edge_id == "e1"}
    assert "edge.unknown_source_port" not in blocking


def test_handover_delegate_warning_is_contextual():
    """Unified handover: every labelled branch FIRES now. The delegate
    warning is CONTEXTUAL — it only appears when a node downstream of the
    branch references the delegation's ANSWER (guaranteed empty), traversing
    intermediate nodes and nested config (flow.call arguments). Trace-only
    branches, event-join patterns, and the other labels stay silent."""
    import uuid as _uuid

    def graph_with(label, call_config=None, extra_nodes=(), extra_edges=()):
        return {
            "nodes": [
                {"id": "t1", "kind": "trigger.manual",
                 "position": {"x": 0, "y": 0}, "config": {}},
                {"id": "ag", "kind": "agent", "position": {"x": 200, "y": 0},
                 "config": {"agent_id": str(_uuid.uuid4()), "prompt": "go"}},
                {"id": "c1", "kind": "flow.call",
                 "position": {"x": 400, "y": 0},
                 "config": call_config or {"flow_id": str(_uuid.uuid4())}},
                *extra_nodes,
            ],
            "edges": [
                {"id": "e1", "source": "t1", "source_handle": "out",
                 "target": "ag", "target_handle": "in", "kind": "data"},
                {"id": "e2", "source": "ag", "source_handle": "handover",
                 "target": "c1", "target_handle": "in", "kind": "handover",
                 "label": label},
                *extra_edges,
            ],
        }

    def delegate_warnings(graph):
        result = validate_graph(graph)
        assert result.is_valid, "a warning, never a save blocker"
        return [i for i in result.issues
                if i.code == "handover.delegate_not_awaited"]

    # Trace-only branch (task/handovers referenced, answer untouched) — silent.
    assert not delegate_warnings(graph_with(
        "delegate",
        call_config={"flow_id": str(_uuid.uuid4()),
                     "arguments": {"task": "{{ag.handover.task}}",
                                   "all": "{{ag.handovers}}"}},
    ))

    # Direct answer reference (nested in flow.call arguments) — warned.
    warns = delegate_warnings(graph_with(
        "delegate",
        call_config={"flow_id": str(_uuid.uuid4()),
                     "arguments": {"res": "{{ag.handover.answer}}"}},
    ))
    assert len(warns) == 1
    assert warns[0].severity == "warning"
    assert warns[0].node_id == "ag"

    # Answer read further downstream (through an intermediate node) — warned.
    assert delegate_warnings(graph_with(
        "delegate",
        extra_nodes=(
            {"id": "c2", "kind": "util.cast", "position": {"x": 600, "y": 0},
             "config": {"value": "{{ag.handover.answer}}", "to": "text"}},
        ),
        extra_edges=(
            {"id": "e3", "source": "c1", "source_handle": "out",
             "target": "c2", "target_handle": "in", "kind": "data"},
        ),
    ))

    # An answer read OUTSIDE the branch (on the out path) — silent: the
    # warning is about THIS branch, not the flow at large.
    assert not delegate_warnings(graph_with(
        "delegate",
        extra_nodes=(
            {"id": "c2", "kind": "util.cast", "position": {"x": 400, "y": 200},
             "config": {"value": "{{ag.handover.answer}}", "to": "text"}},
        ),
        extra_edges=(
            {"id": "e3", "source": "ag", "source_handle": "out",
             "target": "c2", "target_handle": "in", "kind": "data"},
        ),
    ))

    # The other labels never carry the warning, answer reference or not.
    for silent in ("consult", "transfer", "mandatory_transfer"):
        issues = validate_graph(graph_with(
            silent,
            call_config={"flow_id": str(_uuid.uuid4()),
                         "arguments": {"res": "{{ag.handover.answer}}"}},
        )).issues
        assert not any(i.code.startswith("handover.") for i in issues), silent


def test_static_select_values_are_validated_against_their_options():
    """A STATIC select is an enum: an out-of-range value (hand-written JSON,
    API callers) BLOCKS the save — the runtime treats unknown enum members
    as silent no-ops (condition.if operator="eq" evaluates false forever).
    Dynamic selects (source=...) and templated values stay untouched."""
    import uuid as _uuid

    def graph_with(operator):
        return {
            "nodes": [
                {"id": "t1", "kind": "trigger.manual",
                 "position": {"x": 0, "y": 0}, "config": {}},
                {"id": "cond", "kind": "condition.if",
                 "position": {"x": 200, "y": 0},
                 "config": {"left": "a", "operator": operator, "right": "a"}},
                {"id": "yes", "kind": "util.stop",
                 "position": {"x": 400, "y": 0}, "config": {"result": "ok"}},
            ],
            "edges": [
                {"id": "e1", "source": "t1", "source_handle": "out",
                 "target": "cond", "target_handle": "in", "kind": "data"},
                {"id": "e2", "source": "cond", "source_handle": "true",
                 "target": "yes", "target_handle": "in",
                 "kind": "condition_branch"},
            ],
        }

    # The observed bug: "eq" instead of "equals" — now a save blocker.
    result = validate_graph(graph_with("eq"))
    assert not result.is_valid
    bad = next(i for i in result.issues if i.code == "node.invalid_option")
    assert bad.node_id == "cond" and bad.field == "operator"
    assert "equals" in bad.message

    # The declared member passes.
    result_ok = validate_graph(graph_with("equals"))
    assert not any(
        i.code == "node.invalid_option" for i in result_ok.issues
    )

    # Dynamic selects (source=...) are out of scope: a bogus agent_id is
    # its resolver's problem, not an enum mismatch.
    agent_graph = {
        "nodes": [
            {"id": "t1", "kind": "trigger.manual",
             "position": {"x": 0, "y": 0}, "config": {}},
            {"id": "ag", "kind": "agent", "position": {"x": 200, "y": 0},
             "config": {"agent_id": str(_uuid.uuid4()), "prompt": "go"}},
        ],
        "edges": [
            {"id": "e1", "source": "t1", "source_handle": "out",
             "target": "ag", "target_handle": "in", "kind": "data"},
        ],
    }
    assert not any(
        i.code == "node.invalid_option"
        for i in validate_graph(agent_graph).issues
    )


def test_flow_lint_code_mirrors_the_code_interpreter_gate(monkeypatch):
    """POST /flows/lint-code: parse-only verdicts for the Run-code
    node's expanded editor, refused (403) exactly when the node itself is
    hidden — the kill-switches stay in lockstep."""
    from app.core.config import settings as _settings

    with _client(_user()) as client:
        monkeypatch.setattr(_settings, "disable_code_interpreter", False)
        ok = client.post("/api/v1/flows/lint-code", json={"code": "print(1)"})
        assert ok.status_code == 200 and ok.json() == {"status": "ok"}

        bad = client.post("/api/v1/flows/lint-code", json={"code": "def x(:"})
        assert bad.status_code == 200
        assert bad.json()["status"] == "error" and bad.json()["line"] == 1

        monkeypatch.setattr(_settings, "disable_code_interpreter", True)
        refused = client.post(
            "/api/v1/flows/lint-code", json={"code": "print(1)"}
        )
        assert refused.status_code == 403


def test_lint_endpoint_syntax_checks_without_executing(monkeypatch):
    """POST /custom-tools/lint: Python parses in-process, verdicts
    come back as 200 payloads (never HTTP errors), and the kill-switch 403s.
    The code must NOT run — a syntactically valid bomb still returns ok."""
    import asyncio

    from app.services.custom_tool_executor import lint_tool_code

    async def run():
        ok = await lint_tool_code("python", "print('hi')")
        assert ok == {"status": "ok"}

        bad = await lint_tool_code("python", "def broken(:\n    pass")
        assert bad["status"] == "error"
        assert bad["line"] == 1

        # Parsed, never executed: a valid script with side effects returns
        # ok without touching the filesystem.
        import os
        import tempfile

        marker = os.path.join(tempfile.gettempdir(), "lint_must_not_run")
        bomb = f"open({marker!r}, 'w').write('ran')"
        assert (await lint_tool_code("python", bomb)) == {"status": "ok"}
        assert not os.path.exists(marker)

    from app.core.config import settings as _settings

    monkeypatch.setattr(_settings, "disable_custom_tools", False)
    asyncio.run(run())

    # Kill-switch: the executor refuses before parsing anything.
    monkeypatch.setattr(_settings, "disable_custom_tools", True)
    with pytest.raises(RuntimeError):
        asyncio.run(lint_tool_code("python", "print(1)"))


def test_base_version_guard_is_an_optimistic_lock():
    """A graph save built on a stale version 409s with the current version;
    a matching (or absent) base_version passes — historical callers keep
    last-write-wins."""
    from types import SimpleNamespace

    from fastapi import HTTPException

    from app.api.v1.flows import _assert_base_version

    flow = SimpleNamespace(version_number=7)
    _assert_base_version(flow, None)
    _assert_base_version(flow, 7)

    try:
        _assert_base_version(flow, 6)
        raise AssertionError("expected 409")
    except HTTPException as exc:
        assert exc.status_code == 409
        assert exc.detail["code"] == "flow.version_conflict"
        assert exc.detail["current_version"] == 7


def test_chat_trigger_is_a_start_point_publishing_message():
    chat = get_node_kind("trigger.chat")
    assert chat is not None
    assert chat.category == "triggers"
    # A start point: an output to continue from, and no input to arrive on.
    assert get_port(chat, "out", output=True) is not None
    assert get_port(chat, "in", output=False) is None
    # It publishes the visitor's message for `{{<chat>.message}}` downstream.
    assert "message" in {v.name for v in chat.output_variables}


def test_agent_node_exposes_per_flow_chat_toggle():
    fields = {f.name: f for f in get_node_kind("agent").config_fields}
    assert "chat_enabled" in fields
    assert fields["chat_enabled"].type == "boolean"


# ── Layout vs semantics (silent layout auto-save) ─────────────────────


def test_semantic_shape_ignores_positions_and_viewport():
    """Moving nodes / panning must not read as a semantic change — the builder
    auto-saves layout silently and the server mints no version for it."""
    base = {
        "nodes": [
            {"id": "t1", "kind": "trigger.manual", "position": {"x": 0, "y": 0}, "config": {}},
        ],
        "edges": [],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    moved = {
        "nodes": [
            {"id": "t1", "kind": "trigger.manual", "position": {"x": 400, "y": 250}, "config": {}},
        ],
        "edges": [],
        "viewport": {"x": -120, "y": 80, "zoom": 1.5},
    }
    reconfigured = {
        "nodes": [
            {
                "id": "t1",
                "kind": "trigger.manual",
                "position": {"x": 0, "y": 0},
                "config": {"prompt": "changé"},
            },
        ],
        "edges": [],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    assert flows_api._semantic_shape(base) == flows_api._semantic_shape(moved)
    assert flows_api._semantic_shape(base) != flows_api._semantic_shape(reconfigured)


def test_semantic_shape_ignores_chat_test_message():
    """The Chat trigger's embedded test message is scratch content — typing
    one must not gate Run behind a save nor mint a version."""
    base = {
        "nodes": [
            {
                "id": "t1",
                "kind": "trigger.chat",
                "position": {"x": 0, "y": 0},
                "config": {"greeting": "Salut !"},
            }
        ],
        "edges": [],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    typed = {
        **base,
        "nodes": [
            {
                **base["nodes"][0],
                "config": {"greeting": "Salut !", "default_message": "essai"},
            }
        ],
    }
    regreeted = {
        **base,
        "nodes": [{**base["nodes"][0], "config": {"greeting": "Bonjour"}}],
    }
    assert flows_api._semantic_shape(base) == flows_api._semantic_shape(typed)
    assert flows_api._semantic_shape(base) != flows_api._semantic_shape(regreeted)


# ── Coordination capabilities (shared state + work items) ─────────────


def test_agent_has_coordination_sockets():
    agent = get_node_kind("agent")
    for socket in ("shared_state", "work_items"):
        port = get_port(agent, socket, output=False)
        assert port is not None, socket
        # `multiple` is the sharing story: one node fans out to several agents.
        assert port.multiple


def test_coordination_nodes_plug_into_their_own_sockets_only():
    assert _pair("capability.shared_state", "provides", "agent", "shared_state") == {
        EDGE_CAPABILITY
    }
    assert _pair("capability.work_items", "provides", "agent", "work_items") == {
        EDGE_CAPABILITY
    }
    # The port matrix itself refuses a cross-plug.
    assert _pair("capability.shared_state", "provides", "agent", "work_items") == set()


def test_work_item_trigger_is_a_start_point():
    trigger = get_node_kind("trigger.work_item")
    assert trigger is not None
    assert trigger.category == "triggers"
    assert get_port(trigger, "in", output=False) is None
    assert {v.name for v in trigger.output_variables} >= {
        "payload",
        "work_item_id",
        "title",
    }


# ── LLM-powered conditions ───────────────────────────────────────────────────


def test_condition_nodes_accept_an_llm_on_a_capability_socket():
    # Call tool joins the two conditions as an LLM consumer: same
    # socket, same capability wire, same first-wired-wins semantics.
    for kind in ("condition.if", "condition.switch", "action.call_tool"):
        assert has_llm_socket(kind), kind
        # An LLM node plugs in; the edge is a capability wire, not control flow.
        assert _pair("capability.llm", "provides", kind, "llm") == {EDGE_CAPABILITY}


def test_only_the_active_modes_fields_apply():
    fields = {f.name: f for f in get_node_kind("condition.if").config_fields}
    # Deterministic: the string test applies, the question does not.
    assert field_applies(fields["operator"], llm_wired=False)
    assert not field_applies(fields["question"], llm_wired=False)
    # LLM: the reverse.
    assert field_applies(fields["question"], llm_wired=True)
    assert not field_applies(fields["operator"], llm_wired=True)


def _if_graph(*, wire_llm: bool, config: dict) -> dict:
    nodes = [
        {"id": "t1", "kind": "trigger.manual", "position": {"x": 0, "y": 0}},
        {"id": "c1", "kind": "condition.if", "position": {"x": 1, "y": 0}, "config": config},
    ]
    edges = [
        {
            "id": "e1",
            "source": "t1",
            "source_handle": "out",
            "target": "c1",
            "target_handle": "in",
            "kind": "data",
        }
    ]
    if wire_llm:
        nodes.append(
            {"id": "m1", "kind": "capability.llm", "position": {"x": 1, "y": 1}}
        )
        edges.append(
            {
                "id": "e2",
                "source": "m1",
                "source_handle": "provides",
                "target": "c1",
                "target_handle": "llm",
                "kind": "capability",
            }
        )
    return {"nodes": nodes, "edges": edges}


def _missing_fields(graph: dict) -> set[str]:
    return {
        i.field
        for i in validate_graph(graph).issues
        if i.code == "node.missing_field" and i.node_id == "c1"
    }


def _call_tool_graph(*, wire_llm: bool, config: dict, llm_nodes: int = 1) -> dict:
    """A Call-tool node, optionally with LLM node(s) wired into its socket."""
    nodes = [
        {"id": "t1", "kind": "trigger.manual", "position": {"x": 0, "y": 0}},
        {
            "id": "c1",
            "kind": "action.call_tool",
            "position": {"x": 1, "y": 0},
            "config": config,
        },
    ]
    edges = [
        {
            "id": "e1",
            "source": "t1",
            "source_handle": "out",
            "target": "c1",
            "target_handle": "in",
            "kind": "data",
        }
    ]
    if wire_llm:
        for index in range(llm_nodes):
            nodes.append(
                {
                    "id": f"m{index}",
                    "kind": "capability.llm",
                    "position": {"x": 1, "y": 1 + index},
                }
            )
            edges.append(
                {
                    "id": f"e-llm-{index}",
                    "source": f"m{index}",
                    "source_handle": "provides",
                    "target": "c1",
                    "target_handle": "llm",
                    "kind": "capability",
                }
            )
    return {"nodes": nodes, "edges": edges}


def test_call_tool_required_fields_follow_the_wired_mode():
    """Wiring an LLM in swaps WHICH fields the node cannot run without.

    Deterministic mode must stay exactly as it was — asking a node whose tool
    the model picks for a static `tool_name` would be noise, and the reverse
    would break every flow authored before the socket existed.
    """
    assert _missing_fields(_call_tool_graph(wire_llm=False, config={})) == {
        "tool_name"
    }
    assert _missing_fields(_call_tool_graph(wire_llm=True, config={})) == {
        "objective",
        "candidate_tools",
    }
    # A filled LLM-mode node is complete even with no `tool_name` stored.
    assert (
        _missing_fields(
            _call_tool_graph(
                wire_llm=True,
                config={
                    "objective": "Fetch the advisory",
                    "candidate_tools": ["web_fetch"],
                },
            )
        )
        == set()
    )


def test_an_empty_candidate_list_is_still_missing():
    """`[]` is not a choice — the model would have nothing to call."""
    assert "candidate_tools" in _missing_fields(
        _call_tool_graph(
            wire_llm=True,
            config={"objective": "Fetch it", "candidate_tools": []},
        )
    )


def test_a_second_wired_llm_warns_that_only_the_first_applies():
    graph = _call_tool_graph(
        wire_llm=True,
        config={"objective": "Fetch it", "candidate_tools": ["web_fetch"]},
        llm_nodes=2,
    )
    result = validate_graph(graph)
    assert result.is_valid
    warnings = [w for w in result.warnings if w.code == "node.multiple_llms"]
    assert [w.node_id for w in warnings] == ["c1"]


def test_one_wired_llm_does_not_warn():
    graph = _call_tool_graph(
        wire_llm=True,
        config={"objective": "Fetch it", "candidate_tools": ["web_fetch"]},
    )
    assert "node.multiple_llms" not in [
        w.code for w in validate_graph(graph).warnings
    ]


def test_switch_per_case_handles_validate_and_warn_when_stale():
    """A Switch grows one output socket per case (`case:<name>`). The edge is
    legal, and it survives a case rename only as a flagged warning."""
    graph = {
        "nodes": [
            {"id": "t1", "kind": "trigger.manual", "position": {"x": 0, "y": 0}},
            {
                "id": "sw",
                "kind": "condition.switch",
                "position": {"x": 1, "y": 0},
                "config": {"value": "{{t1.prompt}}", "cases": {"incident": "incident"}},
            },
            {
                "id": "c1",
                "kind": "condition.if",
                "position": {"x": 2, "y": 0},
                "config": {"left": "x", "operator": "is_not_empty"},
            },
        ],
        "edges": [
            {
                "id": "e1",
                "source": "t1",
                "source_handle": "out",
                "target": "sw",
                "target_handle": "in",
                "kind": "data",
            },
            {
                "id": "e2",
                "source": "sw",
                "source_handle": "case:incident",
                "target": "c1",
                "target_handle": "in",
                "kind": "condition_branch",
            },
        ],
    }
    result = validate_graph(graph)
    assert result.is_valid
    assert "switch.stale_case" not in {i.code for i in result.issues}

    # Rename the case: the edge's socket no longer exists → warning, not error.
    graph["nodes"][1]["config"]["cases"] = {"alerte": "alerte"}
    result = validate_graph(graph)
    assert result.is_valid, "a stale case warns; it must not block saving"
    stale = [i for i in result.issues if i.code == "switch.stale_case"]
    assert len(stale) == 1 and stale[0].edge_id == "e2"


def test_wiring_an_llm_switches_which_fields_are_required():
    # Deterministic: the operator/value are required, the question is not.
    assert _missing_fields(_if_graph(wire_llm=False, config={})) == {"left", "operator"}
    # LLM mode: only the question is — the string test no longer applies.
    assert _missing_fields(_if_graph(wire_llm=True, config={})) == {"question"}
    # And a filled-in question satisfies it.
    assert (
        _missing_fields(_if_graph(wire_llm=True, config={"question": "Is it urgent?"}))
        == set()
    )


# ── Structural validation ─────────────────────────────────────────────────────


def test_valid_flow_has_no_issues():
    result = validate_graph(_straight_flow())
    assert result.is_valid
    assert result.issues == []


def test_empty_graph_is_valid():
    assert validate_graph({"nodes": [], "edges": []}).is_valid


def test_non_object_graph_is_rejected():
    assert not validate_graph([]).is_valid


def test_node_id_with_a_dot_is_rejected():
    """A dot would make `{{a.b.output}}` ambiguous, so ids may not contain one."""
    graph = {"nodes": [_node("a.b", "trigger.manual")], "edges": []}
    assert "node.invalid_id" in _codes(validate_graph(graph))


def test_duplicate_node_id_is_rejected():
    graph = {
        "nodes": [_node("n1", "trigger.manual"), _node("n1", "trigger.manual")],
        "edges": [],
    }
    assert "node.duplicate_id" in _codes(validate_graph(graph))


def test_unknown_node_kind_is_rejected():
    graph = {"nodes": [_node("n1", "does.not.exist")], "edges": []}
    assert "node.unknown_kind" in _codes(validate_graph(graph))


def test_dangling_edge_is_rejected():
    graph = {
        "nodes": [_node("t1", "trigger.manual")],
        "edges": [_edge("e1", "t1", "ghost")],
    }
    assert "edge.dangling" in _codes(validate_graph(graph))


def test_self_loop_is_rejected():
    graph = {
        "nodes": [_node("a1", "agent")],
        "edges": [_edge("e1", "a1", "a1")],
    }
    assert "edge.self_loop" in _codes(validate_graph(graph))


def test_edge_into_a_trigger_is_rejected():
    graph = {
        "nodes": [_node("a1", "agent"), _node("t1", "trigger.manual")],
        "edges": [_edge("e1", "a1", "t1")],
    }
    assert "edge.unknown_target_port" in _codes(validate_graph(graph))


def test_edge_kind_must_match_the_ports():
    """Claiming `handover` on a data connection must not be taken at face value."""
    graph = {
        "nodes": [_node("t1", "trigger.manual"), _node("a1", "agent")],
        "edges": [_edge("e1", "t1", "a1", kind=EDGE_HANDOVER)],
    }
    assert "edge.invalid_kind" in _codes(validate_graph(graph))


def test_graph_larger_than_the_cap_is_rejected():
    graph = {
        "nodes": [_node(f"n{i}", "util.note") for i in range(MAX_NODES + 1)],
        "edges": [],
    }
    assert "graph.too_large" in _codes(validate_graph(graph))


# ── Variable references ───────────────────────────────────────────────────────


def test_reference_to_an_upstream_output_is_accepted():
    assert validate_graph(_straight_flow()).is_valid


def test_reference_to_an_unknown_node_is_an_error():
    graph = _straight_flow()
    graph["nodes"][1]["config"]["prompt"] = "{{nope.payload}}"
    assert "ref.unknown_node" in _codes(validate_graph(graph))


def test_reference_to_a_downstream_node_is_an_error():
    """The issue requires this to fail at save time, not mid-run."""
    graph = _straight_flow()
    graph["nodes"][1]["config"]["prompt"] = "{{s1.result}}"
    assert "ref.not_upstream" in _codes(validate_graph(graph))


def test_reference_to_an_undeclared_output_is_an_error():
    graph = _straight_flow()
    graph["nodes"][1]["config"]["prompt"] = "{{t1.nope}}"
    assert "ref.unknown_output" in _codes(validate_graph(graph))


def test_malformed_reference_is_an_error():
    graph = _straight_flow()
    graph["nodes"][1]["config"]["prompt"] = "{{t1}}"
    assert "ref.malformed" in _codes(validate_graph(graph))


def test_a_path_into_a_text_output_warns_and_points_to_fields():
    """`{{a1.output.severity}}` renders empty: `output` is the answer's text."""
    graph = _straight_flow()
    graph["nodes"][2]["config"]["message"] = "{{a1.output.severity}}"
    result = validate_graph(graph)
    text_paths = [i for i in result.issues if i.code == "ref.text_path"]
    assert [(i.severity, i.node_id, i.field) for i in text_paths] == [("warning", "s1", "message")]
    assert "`{{a1.fields.severity}}`" in text_paths[0].message
    assert result.is_valid, "a warning, the flow still saves"


def test_a_path_into_a_text_output_without_fields_gets_no_hint():
    graph = _straight_flow()
    graph["nodes"].insert(0, _node("m1", "trigger.manual"))
    graph["edges"].append(_edge("e0", "m1", "a1"))
    graph["nodes"][2]["config"]["prompt"] = "{{m1.prompt.first}}"
    text_paths = [i for i in validate_graph(graph).issues if i.code == "ref.text_path"]
    assert len(text_paths) == 1
    assert "fields" not in text_paths[0].message


@pytest.mark.parametrize(
    "reference",
    [
        "{{a1.output}}",  # the text itself
        "{{a1.fields.severity}}",  # a path into an object output
        "{{a1.handover.type}}",
        "{{t1.payload.issue.id}}",  # an output of any type keeps its paths
    ],
)
def test_references_that_can_resolve_do_not_warn(reference):
    graph = _straight_flow()
    graph["nodes"][2]["config"]["message"] = reference
    assert "ref.text_path" not in _codes(validate_graph(graph))


def test_platform_variable_and_trigger_prefixes_are_left_alone():
    """`{{var_id.UUID}}` / `{{trigger.*}}` belong to the assignment renderer.

    They resolve at run time from a different source, so treating them as flow
    node references would reject every prompt that uses a platform variable.
    """
    graph = _straight_flow()
    graph["nodes"][1]["config"]["prompt"] = (
        "{{var_id.7b1e6a93-0000-0000-0000-000000000000}} and {{trigger.body}}"
    )
    result = validate_graph(graph)
    assert result.is_valid
    assert not [c for c in _codes(result) if c.startswith("ref.")]


def test_references_are_only_scanned_in_variable_capable_fields():
    """A literal `{{` in a plain text field is not a flow reference."""
    graph = _straight_flow()
    # `persona_override` is a plain textarea, not a variable_* field.
    graph["nodes"][1]["config"]["persona_override"] = "{{ghost.output}}"
    assert validate_graph(graph).is_valid


def test_reference_inside_a_nested_config_value_is_still_checked():
    graph = _straight_flow()
    graph["nodes"][2]["config"]["message"] = ["ok", {"deep": "{{ghost.output}}"}]
    assert "ref.unknown_node" in _codes(validate_graph(graph))


# ── Completeness warnings (must NOT block a draft save) ───────────────────────


def test_missing_required_field_warns_but_does_not_block():
    graph = _straight_flow()
    graph["nodes"][0]["config"] = {}  # cron is required
    result = validate_graph(graph)
    assert result.is_valid
    assert "node.missing_field" in [w.code for w in result.warnings]


def test_flow_without_a_trigger_warns():
    graph = {"nodes": [_node("a1", "agent", agent_id="x", prompt="hi")], "edges": []}
    result = validate_graph(graph)
    assert result.is_valid
    assert "flow.no_trigger" in [w.code for w in result.warnings]


def test_node_no_trigger_reaches_warns():
    graph = _straight_flow()
    graph["nodes"].append(_node("orphan", "agent", agent_id="x", prompt="hi"))
    warnings = [w for w in validate_graph(graph).warnings if w.code == "node.unreachable"]
    assert [w.node_id for w in warnings] == ["orphan"]


def test_unresolved_node_warns_instead_of_erroring():
    graph = {"nodes": [_node("u1", "unresolved", source_type="Chroma")], "edges": []}
    result = validate_graph(graph)
    assert result.is_valid
    assert "node.unresolved" in [w.code for w in result.warnings]


def test_note_is_never_reported_as_unreachable():
    graph = _straight_flow()
    graph["nodes"].append(_node("note1", "util.note", text="hello"))
    assert "node.unreachable" not in [w.code for w in validate_graph(graph).warnings]


# ── Normalisation ─────────────────────────────────────────────────────────────


def test_normalize_strips_transient_react_flow_state():
    """Persisted graphs must not carry `selected` / `dragging` / `measured`."""
    node = _node("t1", "trigger.manual")
    node.update({"selected": True, "dragging": False, "measured": {"width": 320}})
    normalized = normalize_graph({"nodes": [node], "edges": []})
    assert set(normalized["nodes"][0]) == {"id", "kind", "position", "config"}


def test_normalize_is_idempotent():
    once = normalize_graph(_straight_flow())
    assert normalize_graph(once) == once


def test_normalize_keeps_the_imported_source_payload():
    """The foreign definition is what makes an unresolved node re-mappable."""
    node = _node("u1", "unresolved")
    node["source_payload"] = {"format": "langflow", "type": "Chroma", "node": {}}
    normalized = normalize_graph({"nodes": [node], "edges": []})
    assert normalized["nodes"][0]["source_payload"]["type"] == "Chroma"


def test_normalize_defaults_a_missing_viewport():
    assert normalize_graph({"nodes": [], "edges": []})["viewport"] == {
        "x": 0,
        "y": 0,
        "zoom": 1,
    }


# ── Import ────────────────────────────────────────────────────────────────────


def _langflow_doc(nodes, edges=()):
    return {
        "name": "Demo",
        "description": "A demo flow",
        "is_component": False,
        "data": {"nodes": list(nodes), "edges": list(edges)},
    }


def _lf_node(node_id, component_type, *, template=None, description=""):
    return {
        "id": node_id,
        "type": "noteNode" if component_type == "note" else "genericNode",
        "position": {"x": 10, "y": 20},
        "data": {
            "id": node_id,
            "type": component_type,
            "node": {
                "display_name": component_type,
                "description": description,
                "template": template or {},
            },
        },
    }


def test_detect_format_native_envelope():
    assert detect_format({"format": "native", "graph": {"nodes": [], "edges": []}}) == (
        "native"
    )


def test_detect_format_bare_native_graph():
    assert detect_format({"nodes": [], "edges": []}) == "native"


def test_detect_format_langflow():
    assert detect_format(_langflow_doc([_lf_node("ChatInput-a1", "ChatInput")])) == (
        "langflow"
    )


def test_detect_format_rejects_anything_else():
    with pytest.raises(FlowImportError):
        detect_format({"hello": "world"})


def test_langflow_known_components_are_mapped():
    doc = _langflow_doc(
        [
            _lf_node("ChatInput-a1", "ChatInput"),
            _lf_node(
                "Agent-b2",
                "Agent",
                template={
                    "system_prompt": {"value": "Be helpful"},
                    "max_iterations": {"value": "12"},
                },
            ),
            _lf_node("ChatOutput-c3", "ChatOutput"),
        ],
        [
            {"source": "ChatInput-a1", "target": "Agent-b2"},
            {"source": "Agent-b2", "target": "ChatOutput-c3"},
        ],
    )
    imported = import_flow_document(doc)
    kinds = [n["kind"] for n in imported["graph"]["nodes"]]
    assert kinds == ["trigger.manual", "agent", "action.send_message"]

    agent = imported["graph"]["nodes"][1]
    assert agent["config"]["prompt"] == "Be helpful"
    assert agent["config"]["max_iterations"] == 12
    # No LangFlow component names an XTM One agent — leaving the binding empty
    # (and warning about it) beats inventing one.
    assert "agent_id" not in agent["config"]

    assert imported["report"]["nodes_unresolved"] == 0
    assert imported["report"]["edges_imported"] == 2
    assert imported["report"]["edges_dropped"] == 0


def test_langflow_unknown_component_becomes_unresolved_with_its_payload():
    doc = _langflow_doc([_lf_node("Chroma-x1", "Chroma", template={"k": {"value": 1}})])
    imported = import_flow_document(doc)
    node = imported["graph"]["nodes"][0]
    assert node["kind"] == "unresolved"
    assert node["config"]["source_type"] == "Chroma"
    assert node["source_payload"]["type"] == "Chroma"
    assert imported["report"]["nodes_unresolved"] == 1


def test_langflow_note_becomes_an_annotation():
    doc = _langflow_doc([_lf_node("note-n1", "note", description="Set your key")])
    node = import_flow_document(doc)["graph"]["nodes"][0]
    assert node["kind"] == "util.note"
    assert node["config"]["text"] == "Set your key"


def test_langflow_edge_into_a_note_is_dropped_and_counted():
    """A note has no ports, so the edge cannot survive — but it must be reported."""
    doc = _langflow_doc(
        [_lf_node("ChatInput-a1", "ChatInput"), _lf_node("note-n1", "note")],
        [{"source": "ChatInput-a1", "target": "note-n1"}],
    )
    report = import_flow_document(doc)["report"]
    assert report["edges_imported"] == 0
    assert report["edges_dropped"] == 1


def test_langflow_ids_are_sanitised_and_stay_unique():
    doc = _langflow_doc(
        [_lf_node("weird.id!", "Chroma"), _lf_node("weird?id@", "Chroma")]
    )
    ids = [n["id"] for n in import_flow_document(doc)["graph"]["nodes"]]
    # `.` `!` `?` `@` all become `_`, trailing separators are trimmed, and the
    # collision the two ids now share is broken with a numeric suffix.
    assert ids == ["weird_id", "weird_id_2"]
    assert len(set(ids)) == 2


def test_langflow_import_produces_a_valid_graph():
    doc = _langflow_doc(
        [
            _lf_node("ChatInput-a1", "ChatInput"),
            _lf_node("Agent-b2", "Agent"),
            _lf_node("Chroma-c3", "Chroma"),
            _lf_node("note-n1", "note"),
        ],
        [
            {"source": "ChatInput-a1", "target": "Agent-b2"},
            {"source": "Agent-b2", "target": "Chroma-c3"},
            {"source": "Chroma-c3", "target": "note-n1"},
        ],
    )
    imported = import_flow_document(doc)
    result = validate_graph(normalize_graph(imported["graph"]))
    assert result.is_valid, _codes(result)


def test_native_export_round_trips():
    graph = normalize_graph(_straight_flow())
    document = {
        "format": "native",
        "version": 1,
        "name": "Round trip",
        "description": "",
        "graph": graph,
    }
    imported = import_flow_document(document)
    assert imported["name"] == "Round trip"
    assert normalize_graph(imported["graph"]) == graph
    assert imported["report"]["edges_dropped"] == 0


def test_import_refuses_a_graph_over_the_node_cap():
    doc = _langflow_doc(
        [_lf_node(f"Chroma-{i}", "Chroma") for i in range(MAX_NODES + 1)]
    )
    with pytest.raises(FlowImportError):
        import_flow_document(doc)


# ── Versioning ────────────────────────────────────────────────────────────────


class _FakeResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return self

    def all(self):
        return list(self._values)

    def scalar_one_or_none(self):
        return self._values[0] if self._values else None


class _FakeDb:
    def __init__(self, execute_values=None):
        self.added = []
        self.executed = []
        self.scalar_value = None
        self.next_execute_values = list(execute_values or [])

    async def execute(self, statement):
        self.executed.append(statement)
        values = self.next_execute_values.pop(0) if self.next_execute_values else []
        return _FakeResult(values)

    async def flush(self):
        return None

    def add(self, value):
        self.added.append(value)

    async def scalar(self, _statement):
        return self.scalar_value


def _version_row(version_number: int, *, graph=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        version_number=version_number,
        author_id=None,
        commit_message=None,
        is_locked=False,
        created_at=datetime.now(UTC),
        graph=graph or {"nodes": [], "edges": []},
    )


def test_normalize_commit_message():
    assert versioning._normalize_commit_message(None) is None
    assert versioning._normalize_commit_message("   ") is None
    assert versioning._normalize_commit_message("  saved  ") == "saved"


@pytest.mark.asyncio
async def test_create_version_numbers_from_the_current_max():
    db = _FakeDb()
    db.scalar_value = 7
    version = await versioning.create_flow_version(
        db, flow_id=uuid.uuid4(), graph={"nodes": []}, author_id=None
    )
    assert version.version_number == 8


@pytest.mark.asyncio
async def test_create_version_starts_at_one_on_an_empty_history():
    db = _FakeDb()
    db.scalar_value = None
    version = await versioning.create_flow_version(
        db, flow_id=uuid.uuid4(), graph={"nodes": []}, author_id=None
    )
    assert version.version_number == 1


@pytest.mark.asyncio
async def test_restore_snapshots_the_target_graph_as_a_new_version():
    """Restoring must not destroy the state that was current beforehand."""
    old_graph = {"nodes": [_node("t1", "trigger.manual")], "edges": []}
    flow = SimpleNamespace(
        id=uuid.uuid4(),
        graph={"nodes": [], "edges": []},
        version_number=9,
    )
    # 1st execute → get_flow_version, 2nd → the row lock in create_flow_version.
    db = _FakeDb(execute_values=[[_version_row(3, graph=old_graph)], []])
    db.scalar_value = 9

    version = await versioning.restore_flow_version(
        db, flow=flow, version_number=3, author_id=None
    )

    assert version is not None
    assert version.version_number == 10, "restore creates a NEW version"
    assert version.commit_message == "Restored version 3"
    assert flow.graph == old_graph
    assert flow.version_number == 10


@pytest.mark.asyncio
async def test_restore_of_a_missing_version_returns_none():
    db = _FakeDb(execute_values=[[]])
    flow = SimpleNamespace(id=uuid.uuid4(), graph={}, version_number=1)
    assert (
        await versioning.restore_flow_version(
            db, flow=flow, version_number=42, author_id=None
        )
        is None
    )


# ── API surface ───────────────────────────────────────────────────────────────


def _user(is_admin: bool = False):
    return SimpleNamespace(
        id=uuid.uuid4(),
        is_admin=is_admin,
        role="admin" if is_admin else "user",
        full_name="Test User",
    )


def _client(user):
    app = FastAPI()
    app.include_router(flows_api.router, prefix="/api/v1/flows")
    app.dependency_overrides[deps.get_current_user] = lambda: user
    # The Agentic AI licence gate is a platform concern, not flow logic, and it
    # would hit the database on every write route. Verified against a real
    # licence + real DB instead; stubbed here so these tests stay hermetic.
    app.dependency_overrides[deps.require_agentic_ai] = lambda: None
    app.dependency_overrides[get_db] = lambda: _FakeDb()
    return TestClient(app)


def test_list_flows_works_for_a_non_admin_caller():
    """The list must build its visibility predicate for an ordinary user.

    ``user_visible_filter`` short-circuits to ``TRUE`` for admins and only
    touches ``access_restricted`` / ``shared_user_ids`` on the non-admin branch,
    so a ``Flow`` missing those columns 500ed exclusively for non-admins — which
    is how ``/flows`` reached production broken while every admin saw it work.
    Keep this caller non-admin.
    """
    with _client(_user(is_admin=False)) as client:
        resp = client.get("/api/v1/flows")

    assert resp.status_code == 200, resp.text
    assert resp.json() == []


def test_chat_entries_works_for_a_non_admin_caller():
    """Same predicate, second entry point: this one 500ed ``/chat`` itself,
    since the conversation picker reads it on every page load.
    """
    with _client(_user(is_admin=False)) as client:
        resp = client.get("/api/v1/flows/chat-entries")

    assert resp.status_code == 200, resp.text
    assert resp.json() == []


def test_validate_endpoint_reports_node_addressed_issues():
    graph = _straight_flow()
    graph["nodes"][1]["config"]["prompt"] = "{{ghost.output}}"
    with _client(_user()) as client:
        resp = client.post("/api/v1/flows/validate", json={"graph": graph})

    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is False
    issue = next(i for i in body["issues"] if i["code"] == "ref.unknown_node")
    # Node-addressed, so the builder can pin it on the canvas.
    assert issue["node_id"] == "a1"
    assert issue["field"] == "prompt"


def test_validate_endpoint_accepts_a_good_graph():
    with _client(_user()) as client:
        resp = client.post("/api/v1/flows/validate", json={"graph": _straight_flow()})
    assert resp.json() == {"valid": True, "issues": []}


def test_node_catalog_serves_kinds_and_the_callers_own_entities(monkeypatch):
    async def _sources(_db, _user):
        return {
            "agents": [{"value": "a-1", "label": "Mail Desk"}],
            "integrations": [],
            "assignments": [],
            "knowledge_bases": [],
        }

    monkeypatch.setattr(flows_api, "catalog_sources", _sources)
    with _client(_user()) as client:
        resp = client.get("/api/v1/flows/node-catalog")

    assert resp.status_code == 200
    body = resp.json()
    assert {c["id"] for c in body["categories"]} == set(CATEGORY_ORDER)
    # `util.code` rides the code interpreter's kill-switch: hidden when the
    # admin disabled it (the default in this test env).
    from app.core.config import settings as _settings

    expected = {nk.kind for nk in NODE_KINDS}
    if _settings.disable_code_interpreter:
        expected.discard("util.code")
    # Same contract for the Script node vs the custom-tools kill-switch.
    if _settings.disable_custom_tools:
        expected.discard("action.script")
    # And for the Runner command node vs the runners kill-switch.
    if not _settings.runners_enabled:
        expected.discard("action.runner_command")
    # And for the nodes of a feature whose flag is off.
    from app.core.feature_flags import is_feature_enabled
    from app.services.flow_catalog import FEATURE_GATED_NODE_KINDS

    expected -= {kind for kind, flag in FEATURE_GATED_NODE_KINDS.items() if not is_feature_enabled(flag)}
    assert {nk["kind"] for nk in body["node_kinds"]} == expected
    # description/icon are optional list-view metadata (null for agents here);
    # provider_id is model-option-only metadata and null everywhere else.
    assert body["sources"]["agents"] == [
        {
            "value": "a-1",
            "label": "Mail Desk",
            "description": None,
            "icon": None,
            "provider_id": None,
        }
    ]
    assert body["limits"]["max_nodes"] == MAX_NODES


def test_activating_a_flow_is_refused_until_the_runner_exists():
    """Phase 1 persists graphs but cannot run them; `active` must not be storable."""
    from fastapi import HTTPException

    assert flows_api._resolve_status(None, "draft") == "draft"
    assert flows_api._resolve_status("archived", "draft") == "archived"
    with pytest.raises(HTTPException) as exc:
        flows_api._resolve_status("active", "draft")
    assert exc.value.status_code == 400


# ── Seeding a flow from the handover topology that already exists ────────────


def _agent(agent_id: str, name: str, handovers=None, types=None, prompt=None):
    return SimpleNamespace(
        id=uuid.UUID(agent_id),
        name=name,
        handover_agent_ids=handovers or [],
        handover_types=types or {},
        handover_prompt=prompt,
    )


_A = "11111111-1111-1111-1111-111111111111"
_B = "22222222-2222-2222-2222-222222222222"
_C = "33333333-3333-3333-3333-333333333333"


# ── Detaching a selection into its own flow ───────────────────────────────────


def _detach_fixture():
    """trigger → a1 → a2 → send, where a2 reads from a1."""
    return {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node("a1", "agent", agent_id="x", prompt="Do the thing"),
            _node("a2", "agent", agent_id="y", prompt="Refine {{a1.output}} now"),
            _node("s1", "action.send_message", channel="slack", target="#ops",
                  message="{{a2.output}}"),
        ],
        "edges": [
            _edge("e1", "t1", "a1"),
            _edge("e2", "a1", "a2"),
            _edge("e3", "a2", "s1"),
        ],
    }


def test_detach_moves_the_selection_and_leaves_no_reference():
    """The whole point: no sub-flow node, no parent -> child link."""
    parent, child, report = split_graph(_detach_fixture(), ["a2"])

    assert [n["id"] for n in child["nodes"]] == ["a2"]
    assert sorted(n["id"] for n in parent["nodes"]) == ["a1", "s1", "t1"]
    assert report["nodes_detached"] == 1
    # Nothing in the parent points at the child.
    assert not any("a2" in str(n) for n in parent["nodes"])


def test_detach_keeps_interior_edges_and_drops_the_boundary_ones():
    parent, child, report = split_graph(_detach_fixture(), ["a1", "a2"])

    # a1 -> a2 is interior, so it travels with them.
    assert [e["id"] for e in child["edges"]] == ["e2"]
    # t1 -> a1 and a2 -> s1 crossed the boundary and cannot survive.
    assert report["edges_dropped"] == 2
    assert [e["id"] for e in parent["edges"]] == []


def test_detach_strips_references_that_can_no_longer_resolve():
    """`a2` reads `{{a1.output}}`; detaching a2 alone leaves a1 behind."""
    _parent, child, report = split_graph(_detach_fixture(), ["a2"])

    assert report["references_removed"] == ["a1.output"]
    assert "{{a1.output}}" not in child["nodes"][0]["config"]["prompt"]
    # The rest of the prompt is untouched — only the placeholder goes.
    assert "Refine" in child["nodes"][0]["config"]["prompt"]


def test_detach_keeps_references_that_travel_with_the_selection():
    _parent, child, report = split_graph(_detach_fixture(), ["a1", "a2"])
    assert report["references_removed"] == []
    prompts = {n["id"]: n["config"].get("prompt", "") for n in child["nodes"]}
    assert "{{a1.output}}" in prompts["a2"]


def test_detach_leaves_platform_variable_prefixes_alone():
    """`var_id.` / `trigger.` belong to the assignment renderer, not the graph."""
    graph = _detach_fixture()
    graph["nodes"][2]["config"]["prompt"] = "{{var_id.abc}} {{trigger.body}} {{a1.output}}"
    _parent, child, report = split_graph(graph, ["a2"])
    prompt = child["nodes"][0]["config"]["prompt"]
    assert "{{var_id.abc}}" in prompt and "{{trigger.body}}" in prompt
    assert report["references_removed"] == ["a1.output"]


def test_both_halves_of_a_detach_validate():
    """A split that produced an invalid graph would be refused on write."""
    parent, child, _ = split_graph(_detach_fixture(), ["a1", "a2"])
    assert validate_graph(normalize_graph(parent)).is_valid
    assert validate_graph(normalize_graph(child)).is_valid


def test_detaching_unknown_ids_detaches_nothing():
    parent, child, report = split_graph(_detach_fixture(), ["ghost"])
    assert report["nodes_detached"] == 0
    assert len(parent["nodes"]) == 4 and child["nodes"] == []


def test_detach_also_repairs_the_parent_side_of_a_broken_reference():
    """`s1` stays behind reading `{{a2.output}}`; detaching a2 must not leave
    the PARENT invalid — the first version of this split only cleaned the
    child, which failed the whole operation with a 422."""
    parent, _child, report = split_graph(_detach_fixture(), ["a2"])

    assert report["parent_references_removed"] == ["a2.output"]
    message = next(n for n in parent["nodes"] if n["id"] == "s1")["config"]["message"]
    assert "{{a2.output}}" not in message
    assert validate_graph(normalize_graph(parent)).is_valid


# ── Calling another flow (the composition primitive) ─────────────────────────


def _call_node(node_id: str, flow_id: str):
    return _node("" + node_id, "flow.call", flow_id=flow_id)


def test_a_flow_can_target_another_flow_instead_of_an_agent():
    """The point: a destination is an agent OR a flow, so composing does not
    require flattening every downstream agent into one canvas."""
    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node("a1", "agent", agent_id="ctem", prompt="Assess"),
            _call_node("f1", "11111111-1111-1111-1111-111111111111"),
        ],
        "edges": [_edge("e1", "t1", "a1"), _edge("e2", "a1", "f1")],
    }
    result = validate_graph(graph)
    assert result.is_valid, [i.code for i in result.errors]


def test_the_called_flow_publishes_an_output_to_reference():
    graph = {
        "nodes": [
            _call_node("f1", "11111111-1111-1111-1111-111111111111"),
            _node("s1", "action.send_message", channel="slack", target="#ops",
                  message="{{f1.output}}"),
        ],
        "edges": [_edge("e1", "f1", "s1")],
    }
    assert validate_graph(graph).is_valid


def test_called_flow_ids_are_extracted_from_a_graph():
    graph = {
        "nodes": [
            _call_node("f1", "aaaaaaaa-0000-0000-0000-000000000001"),
            _call_node("f2", "aaaaaaaa-0000-0000-0000-000000000002"),
            _node("a1", "agent"),
        ],
        "edges": [],
    }
    assert called_flow_ids(graph) == [
        "aaaaaaaa-0000-0000-0000-000000000001",
        "aaaaaaaa-0000-0000-0000-000000000002",
    ]


def test_a_graph_without_calls_extracts_nothing():
    assert called_flow_ids(_straight_flow()) == []


class _FlowRow(SimpleNamespace):
    pass


def _reference_db(rows: dict[str, list[str]]):
    """A DB stub whose flows call each other as described by *rows*."""
    flows = [
        _FlowRow(
            id=uuid.UUID(fid),
            graph={
                "nodes": [
                    {"id": f"c{i}", "kind": "flow.call", "config": {"flow_id": t}}
                    for i, t in enumerate(targets)
                ],
                "edges": [],
            },
        )
        for fid, targets in rows.items()
    ]

    class _Result:
        def __init__(self, values):
            self._v = values

        def scalars(self):
            return self

        def all(self):
            return self._v

    class _Db:
        async def execute(self, statement):
            # Return every stub; the production filter is exercised separately.
            return _Result(flows)

    return _Db()


@pytest.mark.asyncio
async def test_a_flow_calling_itself_is_refused():
    me = uuid.UUID("11111111-1111-1111-1111-111111111111")
    graph = {"nodes": [_call_node("f1", str(me))], "edges": []}
    issues = await check_flow_references(
        _reference_db({str(me): [str(me)]}), _user(True), flow_id=me, graph=graph
    )
    assert [i["code"] for i in issues] == ["flow.call_cycle"]
    # A warning, not an error: the fleet genuinely hands work both ways, and
    # refusing would stop the builder describing the platform it models.
    assert issues[0]["severity"] == "warning"
    assert issues[0]["node_id"] == "f1"


@pytest.mark.asyncio
async def test_an_indirect_cycle_is_refused():
    """A calls B, B calls A — each document is fine on its own, which is
    exactly why this check cannot live in the single-graph validator."""
    a = uuid.UUID("11111111-1111-1111-1111-111111111111")
    b = uuid.UUID("22222222-2222-2222-2222-222222222222")
    graph = {"nodes": [_call_node("f1", str(b))], "edges": []}
    issues = await check_flow_references(
        _reference_db({str(b): [str(a)]}), _user(True), flow_id=a, graph=graph
    )
    assert [i["code"] for i in issues] == ["flow.call_cycle"]


@pytest.mark.asyncio
async def test_a_deep_chain_without_a_cycle_is_allowed():
    a = uuid.UUID("11111111-1111-1111-1111-111111111111")
    b = uuid.UUID("22222222-2222-2222-2222-222222222222")
    c = uuid.UUID("33333333-3333-3333-3333-333333333333")
    graph = {"nodes": [_call_node("f1", str(b))], "edges": []}
    issues = await check_flow_references(
        _reference_db({str(b): [str(c)], str(c): []}), _user(True), flow_id=a, graph=graph
    )
    assert issues == []


@pytest.mark.asyncio
async def test_calling_a_flow_that_is_gone_warns_rather_than_blocks():
    """Refusing would strand a flow whose callee was deleted under it."""
    a = uuid.UUID("11111111-1111-1111-1111-111111111111")
    graph = {"nodes": [_call_node("f1", "44444444-4444-4444-4444-444444444444")],
             "edges": []}
    issues = await check_flow_references(
        _reference_db({}), _user(True), flow_id=a, graph=graph
    )
    assert [i["code"] for i in issues] == ["flow.call_missing"]
    assert issues[0]["severity"] == "warning"
    assert issues[0]["field"] == "flow_id"




# ── Capability wiring validation ─────────────────────────────────────────────


def _capability_graph():
    return {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node("a1", "agent", agent_id="x", prompt="fais"),
            _node("cap1", "capability.tool", tool_ids=["tool-1"]),
        ],
        "edges": [
            _edge("e1", "t1", "a1"),
            _edge(
                "e2", "cap1", "a1", sh="provides", th="tools",
                kind="capability",
            ),
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }


def test_capability_edge_into_the_agent_port_is_legal():
    result = validate_graph(_capability_graph())
    assert result.is_valid, [i.to_dict() for i in result.issues]


def test_capability_edge_cannot_land_on_a_flow_input():
    graph = _capability_graph()
    # Rewire the capability into the agent's ordinary `in` port.
    graph["edges"][1]["target_handle"] = "in"
    result = validate_graph(graph)
    codes = {i.code for i in result.issues}
    assert "edge.kind_not_allowed" in codes or "edge.incompatible" in codes, codes


def test_capability_socket_refuses_the_wrong_family():
    graph = _capability_graph()
    # Un TOOL branché sur la prise `skills` : les canaux ne s'intersectent pas.
    graph["edges"][1]["target_handle"] = "skills"
    result = validate_graph(graph)
    codes = {i.code for i in result.issues}
    assert "edge.incompatible" in codes or "edge.kind_not_allowed" in codes, codes


def test_capability_nodes_are_not_flagged_unreachable():
    result = validate_graph(_capability_graph())
    codes = {i.code for i in result.warnings}
    assert "node.unreachable" not in codes


def test_unwired_capability_warns():
    graph = _capability_graph()
    graph["edges"] = [graph["edges"][0]]  # drop the capability edge
    result = validate_graph(graph)
    warning = next(
        i for i in result.warnings if i.code == "capability.unwired"
    )
    assert warning.node_id == "cap1"
    assert result.is_valid, "unwired is a warning, not an error"


def test_capability_sharing_one_node_two_agents_is_legal():
    graph = _capability_graph()
    graph["nodes"].append(_node("a2", "agent", agent_id="y", prompt="aussi"))
    graph["edges"].append(_edge("e3", "a1", "a2"))
    graph["edges"].append(
        _edge(
            "e4", "cap1", "a2", sh="provides", th="tools",
            kind="capability",
        )
    )
    result = validate_graph(graph)
    assert result.is_valid, [i.to_dict() for i in result.issues]


def test_structured_output_edge_into_the_output_socket_is_legal():
    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node("a1", "agent", agent_id="x", prompt="fais"),
            _node(
                "so",
                "capability.structured_output",
                schema={"type": "object"},
            ),
        ],
        "edges": [
            _edge("e1", "t1", "a1"),
            _edge(
                "e2", "so", "a1", sh="provides", th="output", kind="capability",
            ),
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    result = validate_graph(graph)
    assert result.is_valid, [i.to_dict() for i in result.issues]


def test_two_structured_outputs_on_one_agent_warns():
    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node("a1", "agent", agent_id="x", prompt="fais"),
            _node("so1", "capability.structured_output", schema={"type": "object"}),
            _node("so2", "capability.structured_output", schema={"type": "array"}),
        ],
        "edges": [
            _edge("e1", "t1", "a1"),
            _edge("e2", "so1", "a1", sh="provides", th="output", kind="capability"),
            _edge("e3", "so2", "a1", sh="provides", th="output", kind="capability"),
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    result = validate_graph(graph)
    codes = {i.code for i in result.warnings}
    assert "agent.multiple_output_schemas" in codes
    assert result.is_valid, "a second schema is a warning, not an error"


def test_structured_output_cannot_land_on_the_tools_socket():
    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node("a1", "agent", agent_id="x", prompt="fais"),
            _node("so", "capability.structured_output", schema={"type": "object"}),
        ],
        "edges": [
            _edge("e1", "t1", "a1"),
            _edge("e2", "so", "a1", sh="provides", th="tools", kind="capability"),
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    result = validate_graph(graph)
    codes = {i.code for i in result.issues}
    assert "edge.incompatible" in codes or "edge.kind_not_allowed" in codes, codes


def test_memory_and_guardrails_edges_are_legal():
    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node("a1", "agent", agent_id="x", prompt="fais"),
            _node("mem", "capability.memory", persistent=True),
            _node("gr", "capability.guardrails", guardrails="regle"),
        ],
        "edges": [
            _edge("e1", "t1", "a1"),
            _edge("e2", "mem", "a1", sh="provides", th="memory", kind="capability"),
            _edge("e3", "gr", "a1", sh="provides", th="guardrails", kind="capability"),
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    result = validate_graph(graph)
    assert result.is_valid, [i.to_dict() for i in result.issues]


def test_two_memory_nodes_on_one_agent_warns():
    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node("a1", "agent", agent_id="x", prompt="fais"),
            _node("m1", "capability.memory", persistent=True),
            _node("m2", "capability.memory", short_term=True),
        ],
        "edges": [
            _edge("e1", "t1", "a1"),
            _edge("e2", "m1", "a1", sh="provides", th="memory", kind="capability"),
            _edge("e3", "m2", "a1", sh="provides", th="memory", kind="capability"),
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    result = validate_graph(graph)
    codes = {i.code for i in result.warnings}
    assert "agent.multiple_memory" in codes
    assert result.is_valid


def test_guardrails_cannot_land_on_the_memory_socket():
    graph = {
        "nodes": [
            _node("t1", "trigger.manual"),
            _node("a1", "agent", agent_id="x", prompt="fais"),
            _node("gr", "capability.guardrails", guardrails="regle"),
        ],
        "edges": [
            _edge("e1", "t1", "a1"),
            _edge("e2", "gr", "a1", sh="provides", th="memory", kind="capability"),
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    result = validate_graph(graph)
    codes = {i.code for i in result.issues}
    assert "edge.incompatible" in codes or "edge.kind_not_allowed" in codes, codes


# ── Integration operations (the Integration node's callable actions) ─────────


def test_coerce_tool_arguments_primitives_and_passthrough():
    from app.services.flow_runner import coerce_tool_arguments

    schema = {
        "properties": {
            "count": {"type": "integer"},
            "ratio": {"type": "number"},
            "dry_run": {"type": "boolean"},
            "labels": {"type": "array"},
            "note": {"type": "string"},
        }
    }
    out = coerce_tool_arguments(
        schema,
        {
            "count": "5",
            "ratio": "0.5",
            "dry_run": "true",
            "labels": '["a", "b"]',
            "note": "hello",
            "unknown": "kept",
        },
    )
    assert out == {
        "count": 5,
        "ratio": 0.5,
        "dry_run": True,
        "labels": ["a", "b"],
        "note": "hello",
        "unknown": "kept",
    }
    # Unparseable values stay text — the provider reports the real error.
    out = coerce_tool_arguments(schema, {"count": "beaucoup"})
    assert out == {"count": "beaucoup"}


def test_integration_operations_endpoint_serves_tool_definitions(monkeypatch):
    integration = SimpleNamespace(id=uuid.uuid4(), provider="github")

    async def _fake_build(db, bindings, *, context=None):
        assert bindings == {"github": str(integration.id)}
        return (
            [
                SimpleNamespace(
                    name="github_create_issue",
                    description="Open an issue.",
                    input_schema={
                        "type": "object",
                        "properties": {"title": {"type": "string"}},
                        "required": ["title"],
                    },
                )
            ],
            None,
        )

    monkeypatch.setattr(
        "app.services.integration_tools.build_integration_tools", _fake_build
    )
    user = _user()
    with _client(user) as client:
        app = client.app
        # First execute() feeds get_user_group_ids (no groups), the second
        # the integration lookup itself.
        app.dependency_overrides[get_db] = lambda: _FakeDb(
            execute_values=[[], [integration]]
        )
        resp = client.get(
            f"/api/v1/flows/integration-operations/{integration.id}"
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body[0]["name"] == "github_create_issue"
    assert body[0]["input_schema"]["required"] == ["title"]


def test_primitive_is_exempt_from_upstream_and_reachability_checks():
    """A Value node is a constant: referenceable from anywhere, never
    "unreachable", but an empty one earns a warning."""
    graph = {
        "nodes": [
            {"id": "t1", "kind": "trigger.manual", "position": {"x": 0, "y": 0}, "config": {}},
            {"id": "p1", "kind": "util.primitive", "position": {"x": 0, "y": 200},
             "config": {"value_type": "text", "value": "constante"}},
            {"id": "p2", "kind": "util.primitive", "position": {"x": 0, "y": 400},
             "config": {"value_type": "text"}},
            {"id": "a1", "kind": "condition.if", "position": {"x": 300, "y": 0},
             "config": {"left": "{{p1.value}}", "operator": "is_not_empty"}},
        ],
        "edges": [
            {"id": "e1", "source": "t1", "source_handle": "out",
             "target": "a1", "target_handle": "in", "kind": "data"},
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    result = validate_graph(graph)
    codes = [i.code for i in result.issues]
    assert "ref.not_upstream" not in codes
    assert "node.unreachable" not in codes
    # The empty one is flagged, the filled one is not.
    empty = [i for i in result.issues if i.code == "primitive.empty"]
    assert [i.node_id for i in empty] == ["p2"]


def test_value_edges_wire_primitives_without_granting_execution():
    """A primitive plugs into a consumer's `values` socket (multiple allowed),
    a declared parameter pin does too — and neither makes a node reachable."""
    graph = {
        "nodes": [
            {"id": "t1", "kind": "trigger.manual", "position": {"x": 0, "y": 0},
             "config": {"parameters": [{"name": "sujet", "type": "string"}]}},
            {"id": "p1", "kind": "util.primitive", "position": {"x": 0, "y": 200},
             "config": {"value_type": "number", "value": "3"}},
            {"id": "p2", "kind": "util.primitive", "position": {"x": 0, "y": 300},
             "config": {"value_type": "text", "value": "abc"}},
            {"id": "c1", "kind": "util.cast", "position": {"x": 300, "y": 0},
             "config": {"value": "{{p1.value}}", "to": "text"}},
        ],
        "edges": [
            # Exec chain.
            {"id": "e0", "source": "t1", "source_handle": "out",
             "target": "c1", "target_handle": "in", "kind": "data"},
            # TWO primitives into the same values socket + a parameter pin.
            {"id": "e1", "source": "p1", "source_handle": "value",
             "target": "c1", "target_handle": "values", "kind": "value"},
            {"id": "e2", "source": "p2", "source_handle": "value",
             "target": "c1", "target_handle": "values", "kind": "value"},
            {"id": "e3", "source": "t1", "source_handle": "param:sujet",
             "target": "c1", "target_handle": "values", "kind": "value"},
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    result = validate_graph(graph)
    errors = [i.code for i in result.issues if i.severity == "error"]
    assert errors == []

    # A node fed ONLY by a value edge stays unreachable — data is not exec.
    orphan = {
        "nodes": [
            {"id": "t1", "kind": "trigger.manual", "position": {"x": 0, "y": 0},
             "config": {"parameters": [{"name": "s", "type": "string"}]}},
            {"id": "c1", "kind": "util.cast", "position": {"x": 300, "y": 0},
             "config": {"value": "x", "to": "text"}},
        ],
        "edges": [
            {"id": "e1", "source": "t1", "source_handle": "param:s",
             "target": "c1", "target_handle": "values", "kind": "value"},
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    codes = [i.code for i in validate_graph(orphan).issues]
    assert "node.unreachable" in codes


def test_values_port_injected_on_consumers_only():
    from app.services.flow_catalog import get_node_kind

    for kind in ("util.cast", "condition.if", "action.notify", "flow.call", "integration"):
        nk = get_node_kind(kind)
        assert any(p.id == "values" for p in nk.inputs), kind
    for kind in ("trigger.manual", "util.primitive", "capability.llm", "agent", "util.note"):
        nk = get_node_kind(kind)
        assert not any(p.id == "values" for p in (nk.inputs or ())), kind


def test_ref_unknown_output_flags_typos():
    """The "compile" pass: a ref must name a DECLARED output of its target."""
    graph = {
        "nodes": [
            {"id": "t1", "kind": "trigger.manual", "position": {"x": 0, "y": 0}, "config": {}},
            {"id": "a1", "kind": "condition.if", "position": {"x": 300, "y": 0},
             # `outupt` is the classic typo; `payload` on t1 is legal.
             "config": {"left": "{{t1.outupt}}", "operator": "is_not_empty",
                        "right": "{{t1.payload.x}}"}},
        ],
        "edges": [
            {"id": "e1", "source": "t1", "source_handle": "out",
             "target": "a1", "target_handle": "in", "kind": "data"},
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    issues = validate_graph(graph).issues
    unknown = [i for i in issues if i.code == "ref.unknown_output"]
    assert len(unknown) == 1
    assert unknown[0].node_id == "a1" and unknown[0].field == "left"
    assert "outupt" in unknown[0].message


def test_collapse_graph_leaves_a_wired_flow_call():
    """The UE "Collapse to Function": trigger + minted params in the child,
    a flow.call with arguments in the parent, exec edges rewired."""
    from app.services.flow_detach import CHILD_FLOW_SENTINEL, collapse_graph

    graph = {
        "nodes": [
            {"id": "t1", "kind": "trigger.manual", "position": {"x": 0, "y": 0}, "config": {}},
            {"id": "pre", "kind": "util.cast", "position": {"x": 200, "y": 0},
             "config": {"value": "contexte", "to": "text"}},
            {"id": "a", "kind": "util.cast", "position": {"x": 400, "y": 0},
             "config": {"value": "A:{{pre.value}}", "to": "text"}},
            {"id": "b", "kind": "util.cast", "position": {"x": 600, "y": 0},
             "config": {"value": "B:{{a.value}}", "to": "text"}},
            {"id": "post", "kind": "util.cast", "position": {"x": 800, "y": 0},
             "config": {"value": "fin:{{b.value}}", "to": "text"}},
        ],
        "edges": [
            {"id": "e1", "source": "t1", "source_handle": "out", "target": "pre", "target_handle": "in", "kind": "data"},
            {"id": "e2", "source": "pre", "source_handle": "ok", "target": "a", "target_handle": "in", "kind": "condition_branch"},
            {"id": "e3", "source": "a", "source_handle": "ok", "target": "b", "target_handle": "in", "kind": "condition_branch"},
            {"id": "e4", "source": "b", "source_handle": "ok", "target": "post", "target_handle": "in", "kind": "condition_branch"},
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    parent, child, report = collapse_graph(graph, ["a", "b"])

    # Parent: selection replaced by ONE flow.call, exec rewired through it.
    parent_ids = {n["id"] for n in parent["nodes"]}
    assert parent_ids == {"t1", "pre", "post", "subflow"}
    call = next(n for n in parent["nodes"] if n["id"] == "subflow")
    assert call["kind"] == "flow.call"
    assert call["config"]["flow_id"] == CHILD_FLOW_SENTINEL
    # The outside ref the selection used became an argument.
    assert call["config"]["arguments"] == {"pre_value": "{{pre.value}}"}
    assert any(e["source"] == "pre" and e["target"] == "subflow" for e in parent["edges"])
    assert any(e["source"] == "subflow" and e["target"] == "post" for e in parent["edges"])
    # The parent node that read {{b.value}} now reads the call's output.
    post = next(n for n in parent["nodes"] if n["id"] == "post")
    assert post["config"]["value"] == "fin:{{subflow.output}}"

    # Child: a Manual trigger with the minted parameter, refs rewritten.
    trigger = child["nodes"][0]
    assert trigger["kind"] == "trigger.manual"
    assert trigger["config"]["parameters"] == [
        {"name": "pre_value", "type": "string", "required": False}
    ]
    a_node = next(n for n in child["nodes"] if n["id"] == "a")
    assert a_node["config"]["value"] == "A:{{t_in.payload.pre_value}}"
    assert any(
        e["source"] == "t_in" and e["target"] == "a" for e in child["edges"]
    )
    assert report["nodes_detached"] == 2

    # Both halves validate clean (no dangling refs / ports).
    parent["nodes"] = [
        {**n, "config": {**n["config"], "flow_id": "0" * 8 + "-0000-0000-0000-" + "0" * 12}}
        if n["id"] == "subflow" else n
        for n in parent["nodes"]
    ]
    assert not [i for i in validate_graph(parent).issues if i.severity == "error"]
    assert not [i for i in validate_graph(child).issues if i.severity == "error"]


# ── The tool a Call-tool node picked ──────────────────────────────────


def _pinned_call_tool(**config):
    return {"id": "n", "kind": "action.call_tool", "position": {"x": 0, "y": 0}, "config": config}


def test_a_call_tool_nodes_pins_are_references_of_the_custom_tools_source():
    """A run executes the pinned ids, so the write checks and a copy hold
    them like every other id a node names: catalog fields of the
    custom tools source, the candidates' values read out of their mapping."""
    from app.services.flow_references import graph_source_refs

    tool, candidate = str(uuid.uuid4()), str(uuid.uuid4())
    graph = {
        "nodes": [
            _pinned_call_tool(
                tool_name="lookup",
                tool_id=tool,
                candidate_tools=["enrich"],
                candidate_tool_ids={"enrich": candidate},
            )
        ],
        "edges": [],
    }

    assert graph_source_refs(graph) == [
        ("n", "tool_id", "custom_tools", tool),
        ("n", "candidate_tool_ids", "custom_tools", candidate),
    ]


def test_a_node_written_before_pins_reads_no_pin():
    from app.services.flow_runner import candidate_tool_pins, tool_pin

    old = {"tool_name": "lookup", "candidate_tools": ["lookup"]}
    assert tool_pin(old) == {}
    assert candidate_tool_pins(old) == {}
    assert tool_pin({"tool_name": " lookup ", "tool_id": "abc"}) == {"lookup": "abc"}
    assert tool_pin({"tool_name": "lookup", "tool_id": None}) == {}
    assert candidate_tool_pins({"candidate_tool_ids": ["abc"]}) == {}
    assert candidate_tool_pins({"candidate_tool_ids": {"lookup": "abc", "": "x", "other": 3}}) == {"lookup": "abc"}


def test_a_pin_designates_its_tool_only_under_the_name_it_was_picked_for():
    """A renamed tool is not the node's tool anymore: the name decides."""
    from app.services.flow_runner import pinned_custom_tools

    picked = SimpleNamespace(id=uuid.uuid4(), name="lookup")
    renamed = SimpleNamespace(id=uuid.uuid4(), name="lookup_v2")
    tools = {picked.id: picked, renamed.id: renamed}

    assert pinned_custom_tools({"lookup": str(picked.id)}, tools) == {"lookup": picked}
    assert pinned_custom_tools({"lookup": str(renamed.id)}, tools) == {}
    assert pinned_custom_tools({"lookup": str(uuid.uuid4()), "other": "not-an-id"}, tools) == {}


def test_a_flows_export_carries_its_pins():
    """An id from another install names no tool there, and the node falls
    back to its name: the export keeps the pins as they are."""
    from app.services.flow_import import export_flow

    config = {"tool_name": "lookup", "tool_id": str(uuid.uuid4()), "candidate_tool_ids": {"lookup": str(uuid.uuid4())}}
    flow = SimpleNamespace(
        name="Pinned", description="", tags=[], graph={"nodes": [_pinned_call_tool(**config)], "edges": []}
    )

    imported = import_flow_document(export_flow(flow))

    assert normalize_graph(imported["graph"])["nodes"][0]["config"] == config
