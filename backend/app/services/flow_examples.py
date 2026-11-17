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

"""Built-in example flows — the educational pack.

A curated set of small, self-explanatory flows seeded at startup so a fresh
platform has material to open, read and RUN in the builder. Every example
carries Note blocks that teach the mechanism it demonstrates; most are fully
deterministic (no agent, no LLM) so pressing Run always works, and the two
agent-based playbooks explain — in their notes — the one thing to configure
(pick one of your agents) before running.

Seeding is create-if-absent, keyed by name on company-managed flows: an
example is never overwritten (admins may tweak it live), and deleting one
brings it back on the next restart. The declared event types the event
examples bind to are seeded the same way (company-owned, by name).

``example_triage_graph`` doubles as a functional smoke test executed verbatim
by the test suite (single source of truth).
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

EXAMPLE_FLOW_NAME = "Example: Incident triage"

EXAMPLE_FLOW_DESCRIPTION = (
    "A small, self-contained demo — run it as-is. It parses the JSON you "
    "type at Run time, routes on the severity field through the Switch's "
    "per-case sockets, and ends with a severity-specific summary. No agent "
    "or LLM involved, so it also works as a quick health check of the flow "
    "engine itself."
)

# The default incident the Run dialog offers — edit severity to see the
# other branches fire.
_DEFAULT_INCIDENT = '{"severity": "high", "title": "Suspicious login burst"}'


def example_triage_graph() -> dict:
    """The example's graph — also executed verbatim by the test suite."""
    return {
        "nodes": [
            {
                "id": "note",
                "kind": "util.note",
                "position": {"x": 40, "y": -160},
                "config": {
                    "text": (
                        "DEMO — press Run and keep (or edit) the JSON "
                        "prompt. severity: high / medium / low picks the "
                        "branch; anything else falls through to Default; "
                        "non-JSON input shows the error path."
                    )
                },
            },
            {
                "id": "t1",
                "kind": "trigger.manual",
                "label": "Incident (JSON)",
                "position": {"x": 40, "y": 40},
                "config": {"prompt": _DEFAULT_INCIDENT},
            },
            {
                "id": "b1",
                "kind": "util.break",
                "label": "Parse the incident",
                "position": {"x": 360, "y": 40},
                "config": {"value": "{{t1.prompt}}"},
            },
            {
                "id": "stop_bad",
                "kind": "util.stop",
                "label": "Bad input",
                "position": {"x": 360, "y": 240},
                "config": {
                    "result": (
                        "The input must be a JSON object like "
                        '{"severity": "high", "title": "..."}.'
                    )
                },
            },
            {
                "id": "sw",
                "kind": "condition.switch",
                "label": "Route by severity",
                "position": {"x": 680, "y": 40},
                "config": {
                    "value": "{{b1.fields.severity}}",
                    "cases": {"high": "high", "medium": "medium", "low": "low"},
                },
            },
            {
                "id": "st_high",
                "kind": "util.stop",
                "label": "Page the on-call",
                "position": {"x": 1020, "y": -120},
                "config": {
                    "result": "URGENT — page the on-call about “{{b1.fields.title}}”."
                },
            },
            {
                "id": "st_med",
                "kind": "util.stop",
                "label": "Open a ticket",
                "position": {"x": 1020, "y": 20},
                "config": {"result": "Open a ticket for “{{b1.fields.title}}”."},
            },
            {
                "id": "st_low",
                "kind": "util.stop",
                "label": "Log and watch",
                "position": {"x": 1020, "y": 160},
                "config": {
                    "result": (
                        "Logged — “{{b1.fields.title}}” goes into the "
                        "morning sweep."
                    )
                },
            },
            {
                "id": "st_def",
                "kind": "util.stop",
                "label": "Unknown severity",
                "position": {"x": 1020, "y": 300},
                "config": {
                    "result": (
                        "Unknown severity “{{b1.fields.severity}}” — use "
                        "high, medium or low."
                    )
                },
            },
        ],
        "edges": [
            {
                "id": "e1",
                "source": "t1",
                "source_handle": "out",
                "target": "b1",
                "target_handle": "in",
                "kind": "data",
            },
            {
                "id": "e_err",
                "source": "b1",
                "source_handle": "error",
                "target": "stop_bad",
                "target_handle": "in",
                "kind": "error",
            },
            {
                "id": "e2",
                "source": "b1",
                "source_handle": "out",
                "target": "sw",
                "target_handle": "in",
                "kind": "data",
            },
            {
                "id": "e_high",
                "source": "sw",
                "source_handle": "case:high",
                "target": "st_high",
                "target_handle": "in",
                "kind": "condition_branch",
            },
            {
                "id": "e_med",
                "source": "sw",
                "source_handle": "case:medium",
                "target": "st_med",
                "target_handle": "in",
                "kind": "condition_branch",
            },
            {
                "id": "e_low",
                "source": "sw",
                "source_handle": "case:low",
                "target": "st_low",
                "target_handle": "in",
                "kind": "condition_branch",
            },
            {
                "id": "e_def",
                "source": "sw",
                "source_handle": "default",
                "target": "st_def",
                "target_handle": "in",
                "kind": "condition_branch",
            },
        ],
        "viewport": {"x": 60, "y": 220, "zoom": 0.85},
    }



# ─── Graph-building helpers (compact node/edge literals) ─────────────────────


def _n(nid: str, kind: str, x: int, y: int, label: str | None = None, **config):
    node = {"id": nid, "kind": kind, "position": {"x": x, "y": y}, "config": config}
    if label:
        node["label"] = label
    return node


def _e(src: str, tgt: str, sh: str = "out", th: str = "in", kind: str = "data", label: str | None = None):
    edge = {
        "id": f"e_{src}_{sh}_{tgt}",
        "source": src,
        "source_handle": sh,
        "target": tgt,
        "target_handle": th,
        "kind": kind,
    }
    if label:
        edge["label"] = label
    return edge


def _note(nid: str, x: int, y: int, text: str):
    return _n(nid, "util.note", x, y, text=text)


# ─── The example catalogue ───────────────────────────────────────────────────

EXAMPLE_EVENT_TYPES = {
    "ticket": {
        "name": "Example: Ticket escalated",
        "fields": [
            {"name": "title", "type": "string", "required": True},
            {"name": "severity", "type": "number", "required": False},
        ],
    },
    "delegation": {
        "name": "Example: Task delegated",
        "fields": [
            {"name": "task", "type": "string", "required": True},
            {"name": "target", "type": "string", "required": False},
        ],
    },
    "thehive_alert": {
        "name": "Example: TheHive alert received",
        "fields": [
            {"name": "alert_id", "type": "string", "required": True},
            {"name": "title", "type": "string", "required": False},
            {"name": "alert", "type": "string", "required": True},
        ],
    },
    "cti_analysis": {
        "name": "Example: CTI analysis completed",
        "fields": [
            {"name": "task", "type": "string", "required": True},
            {"name": "result", "type": "string", "required": False},
            {"name": "status", "type": "string", "required": False},
        ],
    },
}


def example_loops_graph() -> dict:
    """While + flow variables + Expression — the counting classic."""
    return {
        "nodes": [
            _note(
                "note", -320, -40,
                "LOOPS & VARIABLES — the While re-checks its condition before "
                "every pass, so the body must move the value it reads: here "
                "Set variable increments a counter and wires BACK into Get "
                "variable. The loop's body and wires highlight in orange. "
                "Press Run: three passes, then Done.",
            ),
            _n("t1", "trigger.manual", 40, 40),
            _n(
                "init", "util.set_variable", 300, 40, label="counter = 0",
                name="counter", mode="set", value="0",
            ),
            _n(
                "get", "util.get_variable", 560, 40, label="Read counter",
                name="counter", default="0",
            ),
            _n(
                "w1", "util.while", 820, 40, label="counter < 3",
                left="{{get.value}}", operator="lt", right="3",
                max_iterations=25,
            ),
            _n(
                "inc", "util.set_variable", 820, 260, label="counter + 1",
                name="counter", mode="increment", value="1",
            ),
            _n(
                "expr", "util.expression", 1120, 40, label="× 10",
                expression="{{get.value}} * 10",
            ),
            _n(
                "done", "util.stop", 1380, 40, label="Done",
                result=(
                    "Counted to {{get.value}} — and the Expression node "
                    "computed {{get.value}} × 10 = {{expr.value}} without "
                    "any LLM."
                ),
            ),
        ],
        "edges": [
            _e("t1", "init"),
            _e("init", "get"),
            _e("get", "w1"),
            _e("w1", "inc", sh="body"),
            _e("inc", "get"),
            _e("w1", "expr", sh="done"),
            _e("expr", "done"),
        ],
        "viewport": {"x": 320, "y": 220, "zoom": 0.65},
    }


def example_event_emitter_graph(event_type_id: str) -> dict:
    """Declare → EMIT: fires the typed event every listener binds to."""
    return {
        "nodes": [
            _note(
                "note", -320, -40,
                "TYPED EVENTS (1/2) — an event is DECLARED once (a name plus "
                "typed fields), then emitters and listeners bind to the same "
                "declaration, so the payload is always well-formed. Run this "
                "flow, then open “Example: Typed events — listener” and look "
                "at its run history: one run per emission.",
            ),
            _n(
                "t1", "trigger.manual", 40, 40, label="Ticket (typed inputs)",
                parameters=[
                    {"name": "title", "type": "string", "required": True},
                    {"name": "severity", "type": "number", "required": False},
                ],
            ),
            _n(
                "emit", "action.emit_event", 340, 40, label="Emit the event",
                event_type_id=event_type_id, scope="global",
                mappings={
                    "title": "{{t1.payload.title}}",
                    "severity": "{{t1.payload.severity}}",
                },
            ),
            _n(
                "done", "util.stop", 640, 40,
                result=(
                    "Emitted “{{t1.payload.title}}” — {{emit.listeners_started}} "
                    "listener run(s) started. Open the listener example's "
                    "run history."
                ),
            ),
        ],
        "edges": [_e("t1", "emit"), _e("emit", "done")],
        "viewport": {"x": 320, "y": 220, "zoom": 0.65},
    }


def example_event_listener_graph(event_type_id: str) -> dict:
    """Declare → LISTEN: starts its own run on every matching emission."""
    return {
        "nodes": [
            _note(
                "note", -320, -40,
                "TYPED EVENTS (2/2) — this flow is ACTIVE and bound to the "
                "same declaration as the emitter: every emission starts a "
                "run here, with the typed fields available as "
                "{{t1.fields.NAME}}. It rings the notification bell so "
                "you see it fire.",
            ),
            _n(
                "t1", "trigger.event", 40, 40, label="On “Ticket escalated”",
                event_type_id=event_type_id,
            ),
            _n(
                "notif", "action.notify", 340, 40, label="Ring the bell",
                title="Ticket escalated: {{t1.fields.title}}",
                message="Severity {{t1.fields.severity}} — delivered by the listener example.",
            ),
            _n(
                "done", "util.stop", 640, 40,
                result="Handled “{{t1.fields.title}}”.",
            ),
        ],
        "edges": [_e("t1", "notif"), _e("notif", "done")],
        "viewport": {"x": 320, "y": 220, "zoom": 0.65},
    }


def example_subflow_child_graph() -> dict:
    """The callable: declared parameters make it a typed function."""
    return {
        "nodes": [
            _note(
                "note", -320, -40,
                "SUB-FLOW (the callee) — the Manual trigger DECLARES a typed "
                "parameter, so calling flows pass arguments like a function "
                "call and mismatches fail before the run starts. The Stop "
                "node's result is what the caller receives.",
            ),
            _n(
                "t1", "trigger.manual", 40, 40, label="text (declared param)",
                parameters=[{"name": "text", "type": "string", "required": True}],
            ),
            _n(
                "up", "util.cast", 340, 40, label="Analyze",
                value="ANALYZED — {{t1.payload.text}}", to="text",
            ),
            _n("done", "util.stop", 640, 40, result="{{up.value}}"),
        ],
        "edges": [
            _e("t1", "up"),
            _e("up", "done", sh="ok", kind="condition_branch"),
        ],
        "viewport": {"x": 320, "y": 220, "zoom": 0.65},
    }


def example_subflow_parent_graph(child_flow_id: str) -> dict:
    """flow.call in Wait mode: pause, get the result, continue."""
    return {
        "nodes": [
            _note(
                "note", -320, -40,
                "CALLING A SUB-FLOW — the Flow node passes typed arguments "
                "to the child's declared parameters, this run pauses, and "
                "{{call.output}} carries the child's result when it "
                "returns. Tip: select several nodes and use “Collapse to "
                "sub-flow” to create callees from existing graphs.",
            ),
            _n(
                "t1", "trigger.manual", 40, 40, label="subject",
                parameters=[{"name": "subject", "type": "string", "required": True}],
            ),
            _n(
                "call", "flow.call", 340, 40, label="Call the analyzer",
                flow_id=child_flow_id, input="analyze",
                arguments={"text": "{{t1.payload.subject}}"},
            ),
            _n(
                "done", "util.stop", 640, 40,
                result="The sub-flow returned: {{call.output}}",
            ),
        ],
        "edges": [_e("t1", "call"), _e("call", "done")],
        "viewport": {"x": 320, "y": 220, "zoom": 0.65},
    }


def example_hitl_graph() -> dict:
    """Ask user: the run pauses until a human answers from the bell."""
    return {
        "nodes": [
            _note(
                "note", -320, -40,
                "HUMAN IN THE LOOP — Ask user pauses the run and rings the "
                "notification bell with the question (urgent, orange). "
                "Clicking the notification deep-links back here with an "
                "answer box; the reply becomes {{ask.response}} and the "
                "flow continues. Try it: press Run, then open the bell.",
            ),
            _n("t1", "trigger.manual", 40, 40),
            _n(
                "ask", "action.ask_user", 300, 40, label="Ask for approval",
                title="Approval needed (example)",
                question="Approve publishing this report? Answer freely — your words flow into the graph.",
            ),
            _n(
                "dec", "util.cast", 620, 40, label="The decision",
                value="Decision by {{ask.responded_by}}: {{ask.response}}",
                to="text",
            ),
            _n(
                "notif", "action.notify", 920, 40, label="Confirm",
                title="Decision received",
                message="{{dec.value}}",
            ),
            _n("done", "util.stop", 1220, 40, result="{{dec.value}}"),
        ],
        "edges": [
            _e("t1", "ask"),
            _e("ask", "dec"),
            _e("dec", "notif", sh="ok", kind="condition_branch"),
            _e("notif", "done"),
        ],
        "viewport": {"x": 320, "y": 220, "zoom": 0.65},
    }


def example_primitives_graph() -> dict:
    """Constants (Value nodes) wired over dotted DATA edges."""
    return {
        "nodes": [
            _note(
                "note", -320, -60,
                "CONSTANTS & DATA WIRES — Value nodes sit OUTSIDE control "
                "flow (they never execute): reference them anywhere as "
                "{{node.value}}. The dotted wires into a node's Values pin "
                "are data, not execution — connecting one auto-fills the "
                "first empty field. A Value node can also read a platform "
                "Variable instead of a literal.",
            ),
            _n(
                "p_name", "util.primitive", 40, 300, label="company",
                value_type="text", value="Acme Corp",
            ),
            _n(
                "p_thr", "util.primitive", 300, 300, label="threshold",
                value_type="number", value="3",
            ),
            _n("t1", "trigger.manual", 40, 60),
            _n(
                "cond", "condition.if", 340, 60, label="threshold > 2 ?",
                left="{{p_thr.value}}", operator="gt", right="2",
            ),
            _n(
                "yes", "util.stop", 680, -20, label="Above",
                result="{{p_name.value}}: threshold {{p_thr.value}} is above 2.",
            ),
            _n(
                "no", "util.stop", 680, 160, label="Not above",
                result="{{p_name.value}}: threshold {{p_thr.value}} is 2 or less.",
            ),
        ],
        "edges": [
            _e("t1", "cond"),
            _e("cond", "yes", sh="true", kind="condition_branch"),
            _e("cond", "no", sh="false", kind="condition_branch"),
            _e("p_thr", "cond", sh="value", th="values", kind="value"),
            _e("p_name", "yes", sh="value", th="values", kind="value"),
            _e("p_name", "no", sh="value", th="values", kind="value"),
        ],
        "viewport": {"x": 320, "y": 220, "zoom": 0.65},
    }


def example_object_values_graph(event_type_id: str) -> dict:
    """An object constant conforming to a declared type — the UE struct pin."""
    return {
        "nodes": [
            _note(
                "note", -320, -60,
                "OBJECT VALUES — a Value node can be a typed OBJECT: pick a "
                "declared type (the same declarations events bind to) and "
                "fill its fields — they are coerced against the signature, "
                "so severity below really is a number. The pill, its pin and "
                "its wire turn GREEN, and downstream you read sub-paths: "
                "{{ticket.value.title}}, {{ticket.value.severity}}.",
            ),
            _n(
                "ticket", "util.primitive", 40, 280, label="ticket",
                value_type="object",
                object_type_id=event_type_id,
                object_type_name="Example: Ticket escalated",
                object_fields={"title": "Disk full on prod-3", "severity": "2"},
            ),
            _n("t1", "trigger.manual", 40, 60),
            _n(
                "cond", "condition.if", 340, 60, label="severity > 1 ?",
                left="{{ticket.value.severity}}", operator="gt", right="1",
            ),
            _n(
                "hot", "util.stop", 680, -20, label="Escalate",
                result=(
                    "Escalate “{{ticket.value.title}}” — severity "
                    "{{ticket.value.severity}} is above 1."
                ),
            ),
            _n(
                "cold", "util.stop", 680, 160, label="Routine",
                result=(
                    "Routine: “{{ticket.value.title}}” (severity "
                    "{{ticket.value.severity}})."
                ),
            ),
        ],
        "edges": [
            _e("t1", "cond"),
            _e("cond", "hot", sh="true", kind="condition_branch"),
            _e("cond", "cold", sh="false", kind="condition_branch"),
            _e("ticket", "cond", sh="value", th="values", kind="value"),
            _e("ticket", "hot", sh="value", th="values", kind="value"),
        ],
        "viewport": {"x": 320, "y": 220, "zoom": 0.65},
    }


_STIX_EXAMPLE_BUNDLE = (
    '{"type": "bundle", "id": "bundle--9f0725cb-2fb0-4a29-8712-22a5de78e2c1", '
    '"objects": [{"type": "indicator", "spec_version": "2.1", '
    '"id": "indicator--8e2e2d2b-17d4-4cbf-938f-98ee46b3cd3f", '
    '"created": "2026-01-01T00:00:00.000Z", "modified": "2026-01-01T00:00:00.000Z", '
    '"name": "Suspicious domain", '
    '"pattern": "[domain-name:value = \'evil.example.com\']", '
    '"pattern_type": "stix", "valid_from": "2026-01-01T00:00:00Z"}]}'
)

_STIX_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "bundle_json": {
            "type": "string",
            "description": "The STIX 2.1 bundle as a JSON string.",
        },
    },
    "required": ["bundle_json"],
}


def example_stix_pipeline_graph() -> dict:
    """JSON constant → built-in STIX validator → switch on the verdict."""
    return {
        "nodes": [
            _note(
                "note", -320, -60,
                "STIX PIPELINE — a JSON Value holds the bundle, the Call "
                "tool node runs the built-in STIX 2.1 validator (its "
                "arguments form is generated from the tool's signature), "
                "Break parses the verdict, and the Switch routes on "
                "{{parse.fields.valid}}. Break the JSON in the Value node "
                "(remove `pattern`, say) and run again to see the rejection "
                "branch fire.",
            ),
            _n(
                "bundle", "util.primitive", 40, 320, label="bundle",
                value_type="json", value=_STIX_EXAMPLE_BUNDLE,
            ),
            _n("t1", "trigger.manual", 40, 60),
            _n(
                "tool", "action.call_tool", 320, 60, label="Validate STIX 2.1",
                tool_name="stix_bundle_validator",
                arguments={"bundle_json": "{{bundle.value}}"},
                arguments_schema=_STIX_TOOL_SCHEMA,
            ),
            _n(
                "parse", "util.break", 620, 60, label="Parse the verdict",
                value="{{tool.result}}",
            ),
            _n(
                "sw", "condition.switch", 920, 60, label="Valid?",
                value="{{parse.fields.valid}}",
                cases={"true": "true", "false": "false"},
            ),
            _n(
                "ok", "util.stop", 1240, -40, label="Accepted",
                result=(
                    "Valid STIX 2.1 — the bundle passed with "
                    "{{parse.fields.error_count}} error(s)."
                ),
            ),
            _n(
                "bad", "util.stop", 1240, 120, label="Rejected",
                result=(
                    "Rejected — {{parse.fields.error_count}} error(s): "
                    "{{parse.fields.errors}}"
                ),
            ),
            _n(
                "odd", "util.stop", 1240, 280, label="Unexpected verdict",
                result="The validator answered: {{parse.text}}",
            ),
        ],
        "edges": [
            _e("t1", "tool"),
            _e("bundle", "tool", sh="value", th="values", kind="value"),
            _e("tool", "parse"),
            _e("parse", "sw"),
            _e("sw", "ok", sh="case:true", kind="condition_branch"),
            _e("sw", "bad", sh="case:false", kind="condition_branch"),
            _e("sw", "odd", sh="default", kind="condition_branch"),
        ],
        "viewport": {"x": 320, "y": 220, "zoom": 0.65},
    }


def example_cti_delegate_join_graph(
    event_type_id: str, assistant_agent_id: str, child_flow_id: str
) -> dict:
    """The complete delegate story: spawn, branch, and JOIN the result."""
    return {
        "nodes": [
            _note(
                "note", -340, -120,
                "CTI DELEGATE & JOIN — the full delegation how-to, on "
                "OpenCTI. (1) The OpenCTI Assistant DELEGATES an insight "
                "analysis to its Entity Insights peer "
                "(spawn_background_task): a background run, out fires "
                "immediately. (2) The delegate-labelled handover branch "
                "fires with the TASK — never the result — and relays a "
                "trace sub-flow. (3) The \u201cOn delegate\u201d attachment below is "
                "set to EMIT ON COMPLETION, scope THIS RUN: when the "
                "background run ends, it emits \u201cExample: CTI analysis "
                "completed\u201d carrying {{delegation.result}} and "
                "{{delegation.status}}. (4) Wait for event JOINS: the run "
                "parks until that emission, then reads the analysis from "
                "the EVENT payload. Requires the OpenCTI platform "
                "registration (Integrations).",
            ),
            _n(
                "t1", "trigger.manual", 40, 40, label="entity",
                parameters=[
                    {"name": "entity", "type": "string", "required": True}
                ],
            ),
            _n(
                "ag", "agent", 360, 40, label="OpenCTI Assistant",
                agent_id=assistant_agent_id,
                prompt=(
                    "Delegate exactly one background task to the OpenCTI "
                    "Entity Insights agent: an activity insight for "
                    "\u201c{{t1.payload.entity}}\u201d based only on data present in "
                    "the platform. Use spawn_background_task once, then "
                    "reply with a one-line acknowledgement. Do not run the "
                    "analysis yourself."
                ),
            ),
            _n(
                "cap", "capability.on_delegate", 360, 320,
                label="Emit result on completion",
                event_type_id=event_type_id, emit_on="completion",
                scope="run",
                mappings={
                    "task": "{{delegation.task}}",
                    "result": "{{delegation.result}}",
                    "status": "{{delegation.status}}",
                },
            ),
            _n(
                "trace", "flow.call", 740, -120, label="Trace the spawn",
                flow_id=child_flow_id,
                input="delegate continuation",
                arguments={"text": "Delegated: {{ag.handover.task}}"},
            ),
            _n(
                "wait", "util.wait_event", 740, 40, label="Join the result",
                event_type_id=event_type_id,
            ),
            _n(
                "ok", "condition.if", 1040, 40, label="completed ?",
                left="{{wait.fields.status}}", operator="equals",
                right="completed",
            ),
            _n(
                "notif", "action.notify", 1340, -40, label="Insight ready",
                title="CTI insight: {{t1.payload.entity}}",
                message="{{wait.fields.result}}",
            ),
            _n(
                "done", "util.stop", 1620, -40,
                result="{{wait.fields.result}}",
            ),
            _n(
                "failed", "util.stop", 1340, 140, label="Delegation failed",
                result=(
                    "Delegated analysis ended \u201c{{wait.fields.status}}\u201d: "
                    "{{wait.fields.result}}"
                ),
            ),
        ],
        "edges": [
            _e("t1", "ag"),
            _e("cap", "ag", sh="provides", th="on_delegate", kind="capability"),
            _e("ag", "trace", sh="handover", kind="handover", label="delegate"),
            _e("ag", "wait"),
            _e("wait", "ok"),
            _e("ok", "notif", sh="true", kind="condition_branch"),
            _e("notif", "done"),
            _e("ok", "failed", sh="false", kind="condition_branch"),
        ],
        "viewport": {"x": 340, "y": 220, "zoom": 0.55},
    }


def example_handover_graph(child_flow_id: str) -> dict:
    """Agent handover → flow: the relay, in Wait mode (synchronous cycle)."""
    return {
        "nodes": [
            _note(
                "note", -320, -80,
                "AGENT HANDOVER (transfer) — pick one of YOUR agents in "
                "the Agent node (the yellow warning disappears) and give "
                "it a peer typed `transfer`. When it hands the "
                "conversation over, the Handover branch fires. This "
                "example sets Handover mode = WAIT: the run pauses and "
                "`out` resumes only after the relay returned. The default "
                "is Detached — a parallel life, `out` immediate.",
            ),
            _note(
                "note2", -320, 420,
                "Edge labels ROUTE: a `consult` edge fires only when a "
                "consult occurred, `delegate` on a delegation, unlabelled "
                "edges on any handover — each occurrence is published as "
                "{{ag.handover}} / {{ag.handovers}}. A delegation's RESULT "
                "stays asynchronous — join it with events (see “Example: "
                "Delegation events”).",
            ),
            _n(
                "t1", "trigger.manual", 40, 40, label="subject",
                parameters=[{"name": "subject", "type": "string", "required": True}],
            ),
            _n(
                "ag", "agent", 320, 40, label="Pick one of your agents",
                agent_id="",
                prompt=(
                    "Work on: {{t1.payload.subject}}. If your instructions "
                    "say to transfer, call transfer_to_agent."
                ),
                handover_mode="wait",
            ),
            _n(
                "call", "flow.call", 660, 260, label="The relay",
                flow_id=child_flow_id, input="analyze",
                arguments={"text": "{{ag.output}}"},
            ),
            _n(
                "relay_done", "action.notify", 980, 260, label="Relay returned",
                title="Relay returned (example)",
                message="{{call.output}}",
            ),
            _n(
                "fin", "util.cast", 660, -80, label="After the return",
                value="out fired after the relay returned — agent said: {{ag.output}}",
                to="text",
            ),
            _n("done", "util.stop", 980, -80, result="{{fin.value}}"),
        ],
        "edges": [
            _e("t1", "ag"),
            _e("ag", "call", sh="handover", kind="handover", label="transfer"),
            _e("call", "relay_done"),
            _e("ag", "fin"),
            _e("fin", "done", sh="ok", kind="condition_branch"),
        ],
        "viewport": {"x": 320, "y": 220, "zoom": 0.65},
    }


def example_delegation_graph(event_type_id: str) -> dict:
    """On-delegate attachment: every delegation becomes a flow event."""
    return {
        "nodes": [
            _note(
                "note", -320, -40,
                "DELEGATION EVENTS — a DELEGATE handover is a background "
                "task: the handover branch fires with the task "
                "({{ag.handover.task}}), but the RESULT arrives later. The "
                "“On delegate” capability below emits one declared event "
                "per spawned task ({{delegation.task}}, "
                "{{delegation.target_agent_name}}…) and the ACTIVE "
                "“Delegation handler” flow picks every emission up as its "
                "own run — the clean way to fan out and join. Pick one of "
                "your agents (with delegate-typed peers) to try it.",
            ),
            _n(
                "t1", "trigger.manual", 40, 40, label="topic",
                parameters=[{"name": "topic", "type": "string", "required": True}],
            ),
            _n(
                "ag", "agent", 340, 40, label="Pick one of your agents",
                agent_id="",
                prompt=(
                    "Split the work about {{t1.payload.topic}} into "
                    "independent subtasks and delegate them to your peer "
                    "agents with spawn_background_task; then summarise what "
                    "you delegated."
                ),
            ),
            _n(
                "cap", "capability.on_delegate", 340, 320, label="Signal delegations",
                event_type_id=event_type_id, scope="global",
                mappings={
                    "task": "{{delegation.task}}",
                    "target": "{{delegation.target_agent_name}}",
                },
            ),
            _n(
                "done", "util.stop", 700, 40,
                result=(
                    "{{ag.output}} — {{ag.delegation_events}} delegation "
                    "event(s) emitted; see the Delegation handler flow."
                ),
            ),
        ],
        "edges": [
            _e("t1", "ag"),
            _e("cap", "ag", sh="provides", th="on_delegate", kind="capability"),
            _e("ag", "done"),
        ],
        "viewport": {"x": 320, "y": 220, "zoom": 0.65},
    }


def example_delegation_listener_graph(event_type_id: str) -> dict:
    return {
        "nodes": [
            _note(
                "note", -320, -40,
                "DELEGATION HANDLER — ACTIVE and bound to “Example: Task "
                "delegated”: every delegation the instrumented agent makes "
                "starts a run here, in parallel with the delegating flow.",
            ),
            _n(
                "t1", "trigger.event", 40, 40, label="On “Task delegated”",
                event_type_id=event_type_id,
            ),
            _n(
                "notif", "action.notify", 340, 40, label="Ring the bell",
                title="Delegated: {{t1.fields.task}}",
                message="Assigned to {{t1.fields.target}}.",
            ),
            _n("done", "util.stop", 640, 40, result="Tracked “{{t1.fields.task}}”."),
        ],
        "edges": [_e("t1", "notif"), _e("notif", "done")],
        "viewport": {"x": 320, "y": 220, "zoom": 0.65},
    }


CLM_INTAKE_FLOW_NAME = "Example: TheHive alert intake (CLM 1/2)"
CLM_TRIAGE_FLOW_NAME = "Example: TheHive alert triage (CLM 2/2)"

CLM_INTAKE_FLOW_DESCRIPTION = (
    "TheHive pushes every new alert to this flow's Webhook; it only emits one "
    "“Example: TheHive alert received” event per alert, so its run lasts "
    "milliseconds. Each event starts its own run of the triage flow, and "
    "those runs execute in parallel — no dispatcher agent, no script."
)

CLM_TRIAGE_FLOW_DESCRIPTION = (
    "One run per TheHive alert, in parallel. A decision model (CLM) labels "
    "the alert: only the ones to investigate, and the ones it is unsure "
    "about, reach your TheHive Case Manager agent; noise gets a comment in "
    "TheHive and spends no LLM token. Pick your agent and your TheHive "
    "integration, activate both flows, then point a TheHive notification "
    "(AlertCreated → Webhook) at the intake flow's webhook URL."
)


def example_clm_alert_intake_graph(event_type_id: str) -> dict:
    """Webhook → one event per alert: the fan-out, with no model involved."""
    return {
        "nodes": [
            _note(
                "note", -360, -200,
                "FAN-OUT, no dispatcher agent (1/2) — TheHive calls this "
                "webhook once per new alert (a notification on AlertCreated "
                "that calls a webhook). The flow emits one typed event per "
                "alert and stops: with Concurrent runs left at 1 the flow "
                "takes one delivery at a time, so the intake stays this "
                "short, and the work happens "
                "in “Example: TheHive alert triage (CLM 2/2)”, where every "
                "event starts its own run, in parallel. Pulling alerts on a "
                "Schedule instead? Loop over them and emit one event each.",
            ),
            _n(
                "t1", "trigger.webhook", 40, 40, label="TheHive: alert created",
                path="thehive-alert-created",
            ),
            _n(
                "emit", "action.emit_event", 360, 40, label="One event per alert",
                event_type_id=event_type_id, scope="global",
                mappings={
                    "alert_id": "{{t1.payload.object._id}}",
                    "title": "{{t1.payload.object.title}}",
                    "alert": "{{t1.payload.object}}",
                },
            ),
            _n(
                "done", "util.stop", 680, 40,
                result=(
                    "Alert {{t1.payload.object._id}} handed to "
                    "{{emit.listeners_started}} triage run(s)."
                ),
            ),
        ],
        "edges": [_e("t1", "emit"), _e("emit", "done")],
        "viewport": {"x": 360, "y": 240, "zoom": 0.65},
    }


def example_clm_alert_triage_graph(event_type_id: str) -> dict:
    """A CLM in front of an agent: one TheHive alert, filtered before the LLM."""
    alert_id = "{{t1.fields.alert_id}}"
    return {
        "nodes": [
            _note(
                "note", -360, -220,
                "CLM TRIAGE (2/2) — a classifier language model (Jev, Clef, "
                "...) reads the alert and picks a label in milliseconds, for "
                "a fraction of an agent's cost. Only `investigate` and "
                "Uncertain (confidence under the floor) reach the agent; "
                "`noise` is commented in TheHive and stops. Set up: pick "
                "your TheHive Case Manager agent and your TheHive "
                "integration (the yellow warnings), keep or change the "
                "decision model (an OpenRouter provider serves "
                "typesafe/jev-1.13; pick it in Provider when several LLM "
                "providers are configured), then activate both flows.",
            ),
            _note(
                "note2", -360, 380,
                "Every “Example: TheHive alert received” event starts its "
                "own run of this flow, and runs execute in parallel: 50 "
                "alerts are 50 runs, each spending agent tokens only if the "
                "CLM says so. A failed classification takes the error path: "
                "the alert is flagged to a person, never dropped.",
            ),
            _n(
                "t1", "trigger.event", 40, 40, label="On “TheHive alert received”",
                event_type_id=event_type_id,
            ),
            _n(
                "clm", "condition.classify", 360, 40, label="Worth an analyst? (CLM)",
                model="typesafe/jev-1.13",
                input="{{t1.fields.alert}}",
                question=(
                    "Does this TheHive alert need a SOC analyst to "
                    "investigate it?"
                ),
                cases={
                    "investigate": (
                        "A plausible true positive: suspicious activity on "
                        "a real asset or account, malicious indicators, or "
                        "anything that needs a human decision."
                    ),
                    "noise": (
                        "A known false positive, a test, a duplicate, an "
                        "informational event or expected benign activity."
                    ),
                },
                min_confidence=0.7,
            ),
            _n(
                "ag", "agent", 760, -120, label="Pick your TheHive Case Manager",
                agent_id="",
                prompt=(
                    "A new TheHive alert needs triage (CLM label: "
                    "{{clm.label}}, confidence {{clm.confidence}}). "
                    "Investigate it: enrich its observables, then promote "
                    "it to a case or close it, and document your findings "
                    "in TheHive.\n\nAlert " + alert_id + ":\n"
                    "{{t1.fields.alert}}"
                ),
            ),
            _n(
                "comment", "integration", 760, 160, label="Comment: noise",
                integration_id="",
                operation="add_thehive_comment",
                parameters={
                    "alert_id": alert_id,
                    "message": (
                        "Auto-triage: classified as noise by the CLM "
                        "(confidence {{clm.confidence}}). No analyst run."
                    ),
                },
            ),
            _n(
                "noise_done", "util.stop", 1100, 160, label="Filtered",
                result="Noise — no agent run (confidence {{clm.confidence}}).",
            ),
            _n(
                "failed", "action.notify", 760, 380, label="Triage it by hand",
                title="Alert triage failed",
                message=(
                    "The CLM could not classify TheHive alert " + alert_id
                    + " — triage it by hand."
                ),
            ),
        ],
        "edges": [
            _e("t1", "clm"),
            _e("clm", "ag", sh="case:investigate", kind="condition_branch"),
            _e("clm", "ag", sh="default", kind="condition_branch"),
            _e("clm", "comment", sh="case:noise", kind="condition_branch"),
            _e("comment", "noise_done"),
            _e("clm", "failed", sh="error", kind="error"),
        ],
        "viewport": {"x": 360, "y": 240, "zoom": 0.65},
    }


async def _seed_event_type(db: AsyncSession, spec: dict) -> str:
    """Create-if-absent a company-owned example event type; return its id."""
    from app.models.flow_event import FlowEventType

    existing = await db.scalar(
        select(FlowEventType.id).where(
            FlowEventType.name == spec["name"],
            FlowEventType.user_id.is_(None),
        )
    )
    if existing is not None:
        return str(existing)
    et = FlowEventType(user_id=None, name=spec["name"], fields=spec["fields"])
    db.add(et)
    await db.flush()
    return str(et.id)


async def _seed_one_flow(
    db: AsyncSession,
    *,
    name: str,
    description: str,
    graph: dict,
    status: str,
) -> int:
    from app.models.flow import Flow
    from app.services.flow_graph import normalize_graph, validate_graph
    from app.services.flow_versioning import create_flow_version

    existing = await db.scalar(
        select(Flow.id).where(
            Flow.name == name,
            Flow.is_company_managed.is_(True),
        )
    )
    if existing is not None:
        return 0

    normalized = normalize_graph(graph)
    result = validate_graph(normalized)
    if not result.is_valid:
        # Never seed something the validator refuses — that would greet every
        # fresh install with a broken example. (Warnings are fine: the two
        # agent playbooks deliberately ship with "pick your agent" warnings.)
        logger.error(
            "Example flow %r failed validation, not seeded: %s",
            name,
            [i.message for i in result.issues if i.severity == "error"],
        )
        return 0

    flow = Flow(
        user_id=None,
        name=name,
        description=description,
        status=status,
        graph=normalized,
        version_number=1,
        tags=["example"],
        is_company_managed=True,
    )
    db.add(flow)
    await db.flush()
    await create_flow_version(
        db,
        flow_id=flow.id,
        graph=normalized,
        author_id=None,
        commit_message="Built-in example",
    )
    return 1


async def seed_example_flow(db: AsyncSession) -> int:
    """Seed the educational pack (create-if-absent). Returns how many flows
    were created on this boot."""
    from app.models.flow import FLOW_STATUS_ACTIVE, FLOW_STATUS_DRAFT, Flow

    # The CTI example ships wired to the builtin OpenCTI Assistant when it
    # exists (xtm_one platforms) — its Entity Insights peer is the
    # delegate-typed target the how-to needs. Absent (other modes, test
    # databases without the agents table), the node ships unassigned like
    # the other agent playbooks. Resolved FIRST: a failed SELECT poisons
    # the transaction, and the rollback must have nothing to discard.
    try:
        from app.models.agent import Agent

        cti_assistant_id = str(
            await db.scalar(
                select(Agent.id).where(
                    Agent.slug == "opencti-assistant",
                    Agent.is_builtin.is_(True),
                )
            )
            or ""
        )
    except Exception:
        await db.rollback()
        cti_assistant_id = ""

    ticket_type = await _seed_event_type(db, EXAMPLE_EVENT_TYPES["ticket"])
    delegation_type = await _seed_event_type(db, EXAMPLE_EVENT_TYPES["delegation"])
    cti_done_type = await _seed_event_type(db, EXAMPLE_EVENT_TYPES["cti_analysis"])
    thehive_alert_type = await _seed_event_type(
        db, EXAMPLE_EVENT_TYPES["thehive_alert"]
    )

    # The analyzer sub-flow is a callee: seed (or find) it first so the
    # callers can reference its id.
    child_name = "Example: Sub-flow — Text analyzer"
    created = await _seed_one_flow(
        db,
        name=child_name,
        description=(
            "The callee half of the sub-flow example: its Manual trigger "
            "declares a typed `text` parameter, so callers pass arguments "
            "like a function call. Its Stop result is what the caller "
            "receives."
        ),
        graph=example_subflow_child_graph(),
        status=FLOW_STATUS_DRAFT,
    )
    child_id = str(
        await db.scalar(
            select(Flow.id).where(
                Flow.name == child_name, Flow.is_company_managed.is_(True)
            )
        )
    )

    examples: list[dict] = [
        {
            "name": EXAMPLE_FLOW_NAME,
            "description": EXAMPLE_FLOW_DESCRIPTION,
            "graph": example_triage_graph(),
            "status": FLOW_STATUS_DRAFT,
        },
        {
            "name": "Example: Loops & variables",
            "description": (
                "While + flow variables + the Expression node: a counter "
                "incremented three times, with the loop body highlighted in "
                "orange. Fully deterministic — press Run."
            ),
            "graph": example_loops_graph(),
            "status": FLOW_STATUS_DRAFT,
        },
        {
            "name": "Example: Typed events — emitter",
            "description": (
                "Declare → emit → listen (1/2): emits the “Example: Ticket "
                "escalated” event with typed, coerced fields. Run it, then "
                "open the listener's run history."
            ),
            "graph": example_event_emitter_graph(ticket_type),
            "status": FLOW_STATUS_DRAFT,
        },
        {
            "name": "Example: Typed events — listener",
            "description": (
                "Declare → emit → listen (2/2): ACTIVE and bound to the same "
                "declaration — every emission starts a run here and rings "
                "the notification bell."
            ),
            "graph": example_event_listener_graph(ticket_type),
            "status": FLOW_STATUS_ACTIVE,
        },
        {
            "name": "Example: Calling a sub-flow",
            "description": (
                "flow.call with typed arguments: the run pauses, the "
                "analyzer sub-flow computes, and {{call.output}} carries "
                "its result back. Deterministic — press Run."
            ),
            "graph": example_subflow_parent_graph(child_id),
            "status": FLOW_STATUS_DRAFT,
        },
        {
            "name": "Example: Ask a human (HITL)",
            "description": (
                "The run pauses on a question, the bell rings (urgent), and "
                "the answer flows back into the graph as {{ask.response}}. "
                "Press Run, then open the notification."
            ),
            "graph": example_hitl_graph(),
            "status": FLOW_STATUS_DRAFT,
        },
        {
            "name": "Example: Constants & data wires",
            "description": (
                "Value nodes (typed literals or platform variables) wired "
                "over dotted data edges — referenced anywhere as "
                "{{node.value}}, never executed. Deterministic — press Run."
            ),
            "graph": example_primitives_graph(),
            "status": FLOW_STATUS_DRAFT,
        },
        {
            "name": "Example: Object values (declared types)",
            "description": (
                "A Value node as a typed OBJECT: it conforms to a declared "
                "type, its fields are coerced (severity is a real number), "
                "and pin + wire draw green. Deterministic — press Run."
            ),
            "graph": example_object_values_graph(ticket_type),
            "status": FLOW_STATUS_DRAFT,
        },
        {
            "name": "Example: STIX validation pipeline",
            "description": (
                "A JSON constant feeds the built-in STIX 2.1 validator "
                "through the Call tool node, and a Switch routes on the "
                "parsed verdict. Deterministic — press Run; break the JSON "
                "to see the rejection branch."
            ),
            "graph": example_stix_pipeline_graph(),
            "status": FLOW_STATUS_DRAFT,
        },
        {
            "name": "Example: Agent handover (transfer)",
            "description": (
                "An agent transfers the conversation and the Handover branch "
                "relays into a sub-flow — in Wait mode here, so `out` "
                "resumes only after the relay returned (the default is "
                "Detached: a parallel life). Pick one of your agents first."
            ),
            "graph": example_handover_graph(child_id),
            "status": FLOW_STATUS_DRAFT,
        },
        {
            "name": "Example: Delegation events (On delegate)",
            "description": (
                "The On-delegate attachment turns every background task the "
                "agent spawns into a typed flow event — handled by the "
                "ACTIVE “Example: Delegation handler”. Pick one of your "
                "agents (with delegate-typed peers) first."
            ),
            "graph": example_delegation_graph(delegation_type),
            "status": FLOW_STATUS_DRAFT,
        },
        {
            "name": "Example: Delegation handler",
            "description": (
                "ACTIVE listener for “Example: Task delegated”: every "
                "delegation the instrumented agent makes starts a run here, "
                "in parallel with the delegating flow."
            ),
            "graph": example_delegation_listener_graph(delegation_type),
            "status": FLOW_STATUS_ACTIVE,
        },
        {
            "name": CLM_INTAKE_FLOW_NAME,
            "description": CLM_INTAKE_FLOW_DESCRIPTION,
            "graph": example_clm_alert_intake_graph(thehive_alert_type),
            "status": FLOW_STATUS_DRAFT,
        },
        {
            "name": CLM_TRIAGE_FLOW_NAME,
            "description": CLM_TRIAGE_FLOW_DESCRIPTION,
            "graph": example_clm_alert_triage_graph(thehive_alert_type),
            "status": FLOW_STATUS_DRAFT,
        },
        {
            "name": "Example: CTI delegate & join (OpenCTI)",
            "description": (
                "The complete delegate how-to on OpenCTI: the Assistant "
                "delegates an insight analysis to Entity Insights, the "
                "delegate branch fires with the task, and the On-delegate "
                "attachment (emit on COMPLETION, scope THIS RUN) lets Wait "
                "for event JOIN the result from the event payload. Needs "
                "the OpenCTI platform registration."
            ),
            "graph": example_cti_delegate_join_graph(
                cti_done_type, cti_assistant_id, child_id
            ),
            "status": FLOW_STATUS_DRAFT,
        },
    ]

    seeded = 0
    for spec in examples:
        seeded += await _seed_one_flow(
            db,
            name=spec["name"],
            description=spec["description"],
            graph=spec["graph"],
            status=spec["status"],
        )
    seeded += created
    await db.commit()
    if seeded:
        logger.info("Provisioned %d built-in example flow(s)", seeded)
    return seeded
