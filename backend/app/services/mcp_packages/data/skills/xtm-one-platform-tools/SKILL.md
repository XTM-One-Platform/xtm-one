---
name: xtm-one-platform-tools
description: Use the {{PLATFORM_NAME}} platform tools MCP server to diagnose agent behaviour from its LLM traces and to fix the configuration behind it. Covers trace triage across agents, reading truncated payloads in full, and the agent / knowledge-base / prompt / skill management surface. Use whenever an agent misbehaved, a run failed, or a platform object needs inspecting or changing.
---

# {{PLATFORM_NAME}} platform tools

This server exposes {{PLATFORM_NAME}} itself: the **traces** its agents produce, and the **objects** those agents are built from. The two halves are meant to be used together — read the trace to find what went wrong, then change the configuration that caused it.

Everything you can reach is scoped to the identity behind the API key. There is no elevated mode: if a tool refuses, the answer is that this user cannot do it in the UI either.

## Diagnosing an agent

### 1. Find the failure before naming an agent

`inspect_agent_traces` takes an optional `target_agent_id`. **Omit it first.** Without a target it lists traces across every agent you supervise (the whole instance, for an administrator), which is how you find *which* agent failed. Naming an agent up front assumes the answer.

```
inspect_agent_traces(status_filter="error", period="24h", include_spans=false)
```

`include_spans=false` on the first pass. With spans on, the listing is capped at 5 traces — good for reading one execution in detail, useless for scanning. Scan wide and flat, pick the trace, then re-read that one with spans.

Then narrow:

```
inspect_agent_traces(target_agent_id="AGENT_ID", period="7d", include_spans=true)
```

Filters available on both forms: `query` (matches trace name, model, agent name), `period` (`24h` / `7d` / `30d` / `90d`), `status_filter` (`ok` / `error`), `source`, `model`, `limit`.

### 2. Never conclude from a preview

Trace payloads are audit material kept whole — one span can carry megabytes. The listing therefore returns **bounded previews**, and says so: a preview that was cut ends in `[truncated N chars]`, and every field held back is named in that step's `payload_truncated` list.

A diagnosis made on a clipped payload is a guess. Read the real value:

```
get_trace_payload(trace_id="TRACE_ID", span_id="SPAN_ID", field="output")
```

`field` is one of `input`, `output`, `metadata`, or `tool_arguments` (spans only). The result carries `total_chars` and `next_offset` — page with `offset` until you have what you need, rather than assuming the first page is the whole story.

### 3. Read the shape of the run

Spans are the steps: `GENERATION` (an LLM call), `TOOL` (a tool call, with its arguments and output), `ITERATION`, `ACTIVATION_GATE`, `MEMORY`. The usual culprits, in the order worth checking:

- a `TOOL` span whose output is an error the model then ignored;
- a `GENERATION` whose `stop_reason` is a length cap rather than a natural end;
- a tool called with arguments that do not match what the persona promised;
- an agent that never called the tool it needed, because it was never bound to it.

The last one is a **configuration** fault, not a reasoning fault — which is where the second half of this server comes in.

### Traces are not runs

`inspect_agent_traces` shows LLM traces: model calls, tool calls, token costs. `list_runs` / `get_run` show **assignment executions**: what was scheduled, what it produced, whether it succeeded. A scheduled job that never started leaves a run and no trace. Check both before concluding nothing happened.

## Fixing the configuration

### Look before you write

Do not invent UUIDs. Every write tool takes an id you should have listed first:

```
list_agents(detail="full")        # owner, model, bindings, knowledge bases, exposure — in one call
get_agent(agent_id="AGENT_ID")
update_agent(agent_id="AGENT_ID", ...)
```

`detail="full"` exists so a fleet-wide audit does not need one `get_agent` per agent. Administrators can add `scope="all"` to `list_agents` and `list_runs` for a platform-wide listing instead of their own visibility.

`list_available_resources` enumerates what an agent *can* be bound to — tools, knowledge bases, prompts, skills, integrations, MCP servers. Read it before an `update_agent` that changes bindings, so you bind something that exists.

### What refuses, and why

Three gates apply to every write, exactly as in the UI:

- **Built-in** objects are read-only for everyone, administrators included.
- **Company-managed** objects are read-only for non-administrators.
- **Group-shared** objects need a `config`-tier grant.

A refusal names which one applied. It is not a transient error and retrying will not change it — report it and stop.

### Writes are real

There is no dry-run and no staging. `update_agent` changes the live agent; `delete_*` is immediate. Say what you are about to change and why before changing it, and prefer a narrow update over rewriting a whole object.

## Knowledge, prompts, skills

**Knowledge bases.** Two different questions, two different tools:

- `search_knowledge(query=...)` — semantic search across everything you can read. Returns ranked fragments. Use it when you do not know which document holds the answer.
- `list_kb_documents` / `get_kb_document` — exact, deterministic lookups. Use them when you know the document and want it verbatim.

Writing: `add_kb_document` creates one, `update_knowledge_document` revises an existing one and re-fires chunking and embedding. Both are limited to knowledge bases you can manage, and the re-embedding is asynchronous — a document reads as `pending` for a moment after the write.

**Prompts and skills.** `list_prompts` / `list_skills` and their `get_*` counterparts return the stored object; to find one id by name, `list_skills(search="...")` or `compact: true` (one line per row) instead of the whole list. For skills there is more than the row: `load_skill` opens the skill as the agent would see it, and `read_skill_resource` reads its bundled files (`scripts/`, `references/`, `assets/`). Nothing else can reach those files — `get_skill` returns the record, not the bundle.

## Working habits

- **Scan, then drill.** One broad listing beats ten targeted ones when you do not yet know where the problem is.
- **Quote the evidence.** When you report a cause, quote the span field you read in full — not the preview you saw first.
- **One change at a time.** After a configuration fix, re-run and read the new trace rather than stacking a second guess on the first.
- **Respect the scope.** Tool output reflects the calling user's permissions. "Not found" often means "not visible to you", by design — it is not a reason to try another id.

See `references/tool-reference.md` for the complete tool list grouped by area.
