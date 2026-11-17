# Microsoft AgentSchema as an agent import/export format

XTM One can export and import agents in **[Microsoft AgentSchema](https://microsoft.github.io/AgentSchema)**
alongside its own native bundle. AgentSchema is an open, cross-vendor JSON
Schema for agent definitions, so an agent authored in XTM One can be read by
another platform that adopts it, and vice versa.

Both formats are always available — the format is a per-export choice, not a
rollout stage. The native bundle stays the default.

## The two formats

| | Native bundle | Microsoft AgentSchema |
| --- | --- | --- |
| Format id (API/audit/telemetry) | `native` | `agentschema` |
| Serialization | JSON | YAML (JSON also accepted on import) |
| Filename | `agent-<slug>.json` | `agent-<slug>.agent.yaml` |
| Envelope | `{"version": 3, "export_type": "agent", ...}` | `AgentDefinition` with `kind: prompt` |
| Upload ceiling | 10 MB | 2 MB |
| Fidelity | Lossless | Lossless **only** between XTM One instances (see [Round-tripping](#round-tripping)) |
| Use it for | Backups, moving agents between XTM One instances | Interoperating with other agent platforms |

Only `kind: prompt` (`PromptAgent`) is supported. `hosted` (`ContainerAgent`,
container-deployed agents) and `workflow` (`Workflow`, multi-step graphs) have no
XTM One counterpart and are rejected with an explicit message rather than
partially imported.

## API surface

| Endpoint | Purpose |
| --- | --- |
| `GET /api/v1/agents/{id}/export?format=native\|agentschema` | Export. Defaults to `native`. |
| `GET /api/v1/agents/{id}/export/portability?format=agentschema` | Read-only report of what the target format would not carry. Drives the pre-export confirmation. |
| `POST /api/v1/agents/import-file` | Multipart upload. Auto-detects the format, validates, then imports. What the UI uses. |
| `POST /api/v1/agents/import` | Unchanged: a native bundle as a JSON body, for existing API clients. |

The native bundle carries an optional `memories` array: the agent's
active persistent memory entries (`persistent_memory.export_memories`: no
superseded entry, no tradecraft), each `{content, memory_type, subject_key,
source, user_id, created_at}`, written only for a caller with full memory
visibility (`agent_memory.has_full_memory_visibility`) and never in a
redacted bundle. `create_agent_from_bundle` recreates them through
`persistent_memory.import_memories`, the function behind
`POST /api/v1/agents/{id}/memory` and the `import_agent_memory` tool, leaving
out with a warning an unreadable entry or one scoped to an unknown user.

Both import endpoints run through `create_agent_from_bundle`
(`backend/app/api/v1/agent_import.py`). AgentSchema is a *mapping onto* that
pipeline, not a pipeline of its own — which is why an AgentSchema-imported agent
gets the same initial version history and default built-in tools
as any other.

## Format detection

Detection is structural only; nothing in an uploaded document is ever evaluated.

- **Native** — `export_type: agent`, or an integer `version` plus an `agent`
  object (the v1/v2 fallback).
- **AgentSchema** — `kind` is one of `prompt` / `hosted` / `workflow`, or a
  `name` plus a `model` or `instructions` (the shape files take when they omit
  `kind`).
- **Neither** — rejected with both formats' markers spelled out, plus the
  top-level fields that *were* found.
- **Both** — rejected rather than guessed.

## Verification loop

In order, all before any database write:

1. **Safe parse** — `yaml.safe_load` via an alias-refusing loader subclass, so a
   YAML alias bomb cannot expand under a byte ceiling that would otherwise pass.
   Anchors alone are fine; only aliases duplicate subtrees. Also bounded on
   nesting depth (64) and UTF-8 validity.
2. **Detection** — as above.
3. **Per-format size ceiling** — 10 MB native, 2 MB AgentSchema.
4. **Schema validation** — against the vendored official schema for AgentSchema
   documents, or the native envelope checks for bundles. Failures are reported
   per field (`instructions: 12 is not of type 'string'`), not as one opaque
   string.
5. **Mapping** — onto the native bundle shape.

Rejections are written to the audit trail as `action: import_rejected` so the
import-attempt telemetry counts them.

### Why the schemas are vendored

`app/services/agentschema/schemas/` is a verbatim, commit-pinned copy of
`microsoft/AgentSchema` `schemas/v1.0/*.yaml` (MIT, attributed in that
directory's `NOTICE.md`). Validation therefore needs no network access, and an
upstream edit cannot retroactively change how an existing export validates.
`jsonschema` and `PyYAML` are already backend dependencies, so nothing new
ships. Refresh with:

```bash
python backend/scripts/refresh_agentschema_vendor.py
```

The official `agentschema` PyPI package was considered and passed over: it is a
beta that declares `ruff` and `black` as *runtime* dependencies, and its errors
are not field-path oriented.

### Import leniency

Documents we *emit* are validated strictly. Documents we *receive* are
normalized first, because real-world AgentSchema files are looser than the
schema — Microsoft's own `examples/analyst/analyst.yaml` would fail it. Every
leniency applied is reported back to the user as a note:

| Real-world shape | Normalization |
| --- | --- |
| No `kind` (which `AgentDefinition` marks required) | Read as `kind: prompt` |
| `model: gpt-4o` (string) | Expanded to `{"id": "gpt-4o"}`, the shorthand `Model.yaml` documents |
| `tools` as a mapping (`RecordTool`) | Expanded to the array form, each key becoming the tool `name` |

## Field mapping

### Direct

| XTM One | AgentSchema | Notes |
| --- | --- | --- |
| `name` | `name` + `displayName` | |
| `description` | `description` | |
| `persona` | `instructions` | |
| `guardrails` | `additionalInstructions` | Flattened — see [below](#flattened) |
| `llm_model` | `model.id` | `"default"` is emitted when the agent has no override, and read back as "no override" |
| `llm_provider` | `model.provider` | |
| `temperature` | `model.options.temperature` | `model.options` also needs `kind: chat` — `ModelOptions` marks it required |
| `reasoning_effort` | `model.options.additionalProperties.reasoning_effort` | No first-class field exists; `additionalProperties` is the spec's declared escape hatch |
| `output_schema` (when `output_format: json`) | `outputSchema` | **Translated**, not passed through: AgentSchema uses its own `PropertySchema`, not embedded JSON Schema. `number` becomes `float`; nested object/array *shapes* are not carried |
| `tags` | `metadata.tags` | Where AgentSchema's own docs put tags |

### Tools

| XTM One | AgentSchema `kind` | Notes |
| --- | --- | --- |
| Custom tools | `function` | `input_schema` is translated into `parameters` (a `PropertySchema`). A `function` tool has a signature but no body, so importing one from elsewhere produces a stub with an empty implementation and a warning |
| MCP servers | `mcp` | A `ReferenceConnection` by name plus `approvalMode: {kind: never}` (XTM One runs tool calls without per-call approval). No address or credentials travel; importing one produces a placeholder and a warning |
| Knowledge bases | `file_search` | Reference only: the KB *name* as the `vectorStoreId`. Chunking, embedding and per-KB permissions are not portable, and agent imports never re-bind knowledge bases |
| `code_interpreter` | `code_interpreter` | With `fileIds: []` — XTM One's interpreter works on conversation attachments |

**`web_fetch` / `browser` are deliberately not mapped.** AgentSchema v1.0's only
web tool is `WebSearchTool`, whose `kind` is the vendor-specific `bing_search`.
XTM One's tools retrieve a caller-supplied URL rather than querying Bing, so
mapping them onto it would misdescribe what the agent can do. (The issue that
requested this feature assumed a generic `web_search` kind; the spec has no
such thing.) Every other built-in is XTM One-specific.

### Not portable

Reported to the user before the export runs, and listed in the export audit
entry so the real-world impact is measurable:

- handover routing (`handover_agent_ids` / `handover_types` / `handover_conditions`)
- memory tiers (short-term / medium-term / persistent / user)
- assignments — schedule/webhook/integration-event triggers, activation gates,
  human-in-the-loop, digest and notify settings
- business-integration bindings (GitHub, Notion, YouTrack, ...) — AgentSchema's
  `Connection` covers model and tool credentials only
- prompts and skills — reusable, separately-versioned records. **Not** flattened
  into the instructions: that would balloon the document and misrepresent
  separately-loaded material as inline prose
- variables — references inside the persona are exported verbatim, the records
  themselves are not
- `tool_deny_list` — the spec declares what an agent *has*, with no deny layer
- `max_iterations` — `ModelOptions` has no agent-loop ceiling
- chat/run visibility, channel configuration, peer-agent bindings
  (work-item ownership, trace-supervision targets)

### Flattened

Content survives but stops being a distinct, separately-managed field:

- **`guardrails`** → `additionalInstructions`. Re-importing into XTM One
  restores them as guardrails; another platform reads them as extra
  instructions.
- **Knowledge bases** → a `file_search` reference by name only.

## Round-tripping

Everything in the "not portable" list is preserved verbatim under
`metadata['x-xtm-one']`. `metadata` is declared `RecordUnknown` (arbitrary
properties), so this keeps the document spec-valid and other platforms simply
ignore the key. An XTM One → AgentSchema → XTM One round-trip is therefore
lossless.

The namespace holds **only** what the spec cannot express — never a duplicate of
`instructions` or `model`. On import, the AgentSchema fields win and the
metadata fills in the rest. That ordering is deliberate: embedding a whole copy
of the bundle would make hand-editing an exported file silently ineffective.

A missing or malformed namespace degrades to "no extras", never to a failed
import.

Two things do not round-trip, by design rather than by limitation:

- **Peer-agent references** (`handover_agent_ids`, `work_item_owner_agent_ids`,
  `trace_supervision_target_agent_ids`) are cleared on *every* import path — the
  UUIDs belong to the source environment and would dangle.
- **Knowledge base bindings** are never re-bound on import, so retrieval needs
  the user to bind a base on the target instance. The import says so.

## Telemetry

Derived from the audit trail (`_refresh_agent_format_adoption` in
`app/services/telemetry.py`), counts only:

| Metric | Meaning |
| --- | --- |
| `xtm_one_agent_exports_by_format` | Exports, keyed by target format |
| `xtm_one_agent_imports_by_format` | Successful imports, keyed by *detected* format |
| `xtm_one_agent_imports_rejected_count` | Uploads rejected by detection or validation |

Audit entries carry more detail than the gauges: `details.omitted_fields` on an
export records which non-portable fields that specific agent actually hit, and
`details.problems` on a rejection records what failed. Both are cumulative over
*retained* audit rows, so pruning the audit trail lowers the counts.

## Code map

| File | Contents |
| --- | --- |
| `backend/app/services/agentschema/spec.py` | Vendored schema loading, the validator, safe parsing, import normalization |
| `backend/app/services/agentschema/detect.py` | Format detection, native-bundle verification |
| `backend/app/services/agentschema/mapping.py` | Field mapping both ways, `portability_report` |
| `backend/app/services/agentschema/__init__.py` | `prepare_agent_import`, `build_agentschema_export` |
| `backend/app/services/agentschema/schemas/` | Vendored `microsoft/AgentSchema` v1.0 files + `NOTICE.md` |
| `backend/app/api/v1/agent_import.py` | The shared agent-creation pipeline |
| `backend/scripts/refresh_agentschema_vendor.py` | Vendor refresh tool |
| `frontend/src/components/ui/agent-export-dialog.tsx` | Format picker + portability warning |
