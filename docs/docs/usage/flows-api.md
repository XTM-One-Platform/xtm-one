# Flow builder API

Everything the visual builder does goes through a REST surface you can
drive from scripts — or hand to an agent through the `Flow builder`
built-in tool. This page follows the authoring lifecycle: discover the
catalog, compose the graph, validate, save, run, observe.

All routes live under `/api/v1` and authenticate with a bearer token —
create one in [`My profile` → `API Keys`](my-profile.md#api-keys-tab).

## The graph document

A flow's graph is plain JSON — nodes, edges, viewport:

```json
{
  "nodes": [
    {"id": "t1", "kind": "trigger.manual", "position": {"x": 60, "y": 60},
     "config": {"parameters": [{"name": "who", "type": "string", "required": true}]}},
    {"id": "greet", "kind": "util.cast", "position": {"x": 400, "y": 60},
     "config": {"value": "hello {{t1.payload.who}}", "to": "text"}}
  ],
  "edges": [
    {"id": "e1", "source": "t1", "source_handle": "out",
     "target": "greet", "target_handle": "in", "kind": "data"}
  ],
  "viewport": {"x": 0, "y": 0, "zoom": 1}
}
```

| Rule | Detail |
|---|---|
| Node ids | Letters, digits, `-`, `_` — no dots (ids appear in `{{id.field}}` references) |
| References | `{{node_id.output_variable}}` — each kind's `output_variables` are in the node catalog |
| Trigger parameters | A manual trigger's declared parameters read as `{{trigger_id.payload.name}}` |
| Edge handles | Default to `out` / `in`; conditions use `true` / `false` / `case:<name>`, agents add `handover` |
| Handover labels | A `handover` edge may carry `consult`, `delegate`, `transfer` or `mandatory_transfer` to route by type |
| Positions | Required on save — `POST /flows/auto-layout` fills missing ones for headless authoring |
| Limits | 200 nodes, 1000 edges |

## Discover

`GET /flows/node-catalog` returns every node kind (ports, config fields,
published output variables), the edge kinds, the limits, and your live
entities to bind — agents, flows, event types, tools, integrations. The
builder UI and the validator both read this payload, so it never drifts.

Each `output_variables` entry carries a `type`: `text`, `number`,
`boolean`, `object` or `list` when the node always publishes that kind of
value, `any` otherwise. A `text` value has no fields: a reference into it
such as `{{a1.output.severity}}` always renders empty, and the validator
warns about it (`ref.text_path`).

An agent node publishes its answer as text in `output`. When it answers in
JSON (its own output format, or a wired `Structured output` node, which
replaces the agent's schema) and the answer is an object, it also publishes
that object in `fields`: read it with `{{a1.fields.severity}}`.

`POST /flows/output-schemas` tells which structure each node of a graph
declares, without saving or running it. Send `{"graph": {...}}`, a draft
graph document as above. The answer is
`{"nodes": {"<node_id>": {"<output>": <JSON Schema>}}}` and lists only the
nodes that declare one:

| Node | Output | Declared structure |
|---|---|---|
| Agent | `fields` | The schema of a JSON answer whose root is an object: the wired `Structured output`'s schema, else the agent's own |
| Manual trigger | `payload` | Its declared parameters |
| On event / Wait for event | `fields` | The fields of its event type |

Agents and event types are read with your visibility: a node naming one you
cannot see declares nothing. Nothing is written. A graph larger than a flow
may hold (200 nodes, 1000 edges) is a `422`; a node or an edge that is not
well formed is skipped.

Two companions resolve callable signatures for typed arguments:
`GET /flows/callable-tools` (built-ins + your custom tools) and
`GET /flows/integration-operations/{integration_id}`.

In the `GET /flows/callable-tools` answer, every custom tool carries its
`id`. A `Call tool` node stores it next to the name it picks: `tool_id` beside
`tool_name`, and `candidate_tool_ids` (candidate name to id) beside
`candidate_tools` in LLM mode. The step runs that tool while it is still an
enabled tool of that name the flow can use, and resolves the name otherwise
(a node without the ids, or one from another platform). `designated` marks
the tool a node storing only the name runs for you.

## Author supporting entities

Declare typed event signatures for the `Emit event` / `On event` /
`Wait for event` nodes:

```bash
curl -X POST https://<your-xtm-one>/api/v1/flow-event-types \
  -H "Authorization: Bearer fcp-..." -H "Content-Type: application/json" \
  -d '{"name": "Ticket escalated", "fields": [
        {"name": "title", "type": "string", "required": true},
        {"name": "severity", "type": "number"}]}'
```

The name is stored without its surrounding whitespace and zero-width
characters, and must keep 1 to 120 characters once they are removed: a
blank name is a `422`, on `POST` and on `PUT /flow-event-types/{id}`.

Custom tools for the `Script` node go through `POST /custom-tools`;
`POST /custom-tools/lint` syntax-checks a script without executing it
as it runs, after the platform's setup lines (Python via `compile`, which also
refuses a top-level `return` or `await` and a `from __future__` import,
JavaScript via `node --check`); a reported line number is a line of the script. The `Run code` node's equivalent is
`POST /flows/lint-code`. `POST /custom-tools/generate-code` writes a
script with the platform's model from a `description`, a `language` and,
optionally, the tool's name, description, parameters and `current_code` to
rework; it saves nothing, and its answer says whether the code was cut at the
model's length limit and whether it passes the same syntax check.

## Validate and save

`POST /flows/validate` is a dry run — nothing is written. Issues come
back as `{code, message, severity, node_id, edge_id, field}`; errors
block saving, warnings do not.

```bash
curl -X POST https://<your-xtm-one>/api/v1/flows \
  -H "Authorization: Bearer fcp-..." -H "Content-Type: application/json" \
  -d '{"name": "Greeting", "graph": {...}}'
```

| Route | Purpose |
|---|---|
| `POST /flows` | Create (graph validated; errors → `422` with the issues) |
| `PUT /flows/{id}` | Update — pass `base_version` for optimistic locking; a stale version → `409` `flow.version_conflict` |
| `GET /flows` / `GET /flows/{id}` | List / fetch (visibility: own, group-shared, company-managed) |
| `POST /flows/auto-layout` | Fill missing node positions (topological layout) |
| `GET /flows/{id}/versions` | Version history; restore endpoints included |
| `GET /flows/{id}/export` / `POST /flows/import` | Export round-trips the graph document; import also reads LangFlow and n8n files, and its `report` counts what needs remapping (`nodes_unresolved`) or a review (`nodes_to_review`) — see [Import a flow](flows.md#import-a-flow) |

## Run and observe

```bash
curl -X POST https://<your-xtm-one>/api/v1/flows/<flow_id>/run \
  -H "Authorization: Bearer fcp-..." -H "Content-Type: application/json" \
  -d '{"parameters": {"who": "world"}}'
```

Runs work on drafts — this is the builder's test button. `parameters`
are coerced against the manual trigger's declared signature; a mismatch
returns `422` with per-field errors.

| Route | Purpose |
|---|---|
| `POST /flows/{id}/run` | Start a run now; returns the run id |
| `GET /flows/runs/{id}` | Status plus the step journal — per-node inputs, outputs, errors |
| `POST /flows/runs/{id}/respond` | Answer an `Ask user` pause (`409` when nothing is awaited) |
| `POST /flows/runs/{id}/cancel` | Cancel an active run |
| `POST /flows/{id}/activate` / `deactivate` | Put triggers live / back to draft (activation refuses invalid graphs) |

!!! note "Anything that waits, parks"
    Delays, sub-flows, events, assignments and human questions park the
    run instead of holding a connection; the orchestrator resumes it.
    Poll the run detail to follow along.

## Let an agent build flows

The `Flow builder` built-in tool wraps this same surface for agents —
enable it in an agent's tooling section. Its operations mirror the
lifecycle above: `get_node_catalog`, `get_flow_examples` (seeded graphs
as few-shot templates), `validate_flow_graph`, `create_flow` and
`update_flow_graph` (both auto-layout nodes without positions),
`create_flow_event_type`, `run_flow`, `get_flow_run` (the step journal,
for self-correction), `activate_flow`.

The same tools are offered on the [XTM One MCP server](resources/platform-tools-mcp-server.md)
(`/mcp/xtm`), so an MCP client such as Claude can create and run flows
without the builder. The node catalog it returns also lists the relay
groups an HTTP request node can be routed through. Reads (`get_node_catalog`,
`list_flows`, `get_flow`, `get_flow_examples`, `validate_flow_graph`,
`list_flow_event_types`, `get_flow_run`) are available to a read-only API
key; the rest follow the same edit rule as the REST routes. A flow with a
webhook trigger is activated from its page, which shows the delivery secret
once: it is never returned to an MCP client.

## Next step

Read [Flows](flows.md) for the canvas itself, or
[Agentic flow](agentic-flow.md) for how agents hand work to each other.
