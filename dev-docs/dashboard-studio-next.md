# Dashboard Studio: analysis and target definition

Status: draft for decision, 2026-11-24. Scope: the Dashboard Studio (`DASHBOARD_STUDIO`), meaning custom dashboards, the studio and the `custom_dashboards` built-in tool.
User contract today: `docs/docs/usage/custom-dashboards.md`. Rendering contract: `dev-docs/dashboard-rendering-reference.md`.

## 1. Summary

The renderer and the manual editor are already strong: a definition that keeps data sources, visualizations and layout apart, about 40 visualization types, inputs and tokens, interactions, an absolute canvas, history, sharing, publishing and exports. The rest is weak:

- **Making a dashboard with an agent** fails out of the box.
- **Feeding a widget** works from only three Filigran platforms, through a configuration the model cannot discover.
- **Keeping it fresh** only happens in a browser that has the page open.
- **The stored format is not OpenCTI's or OpenAEV's.** Their widgets travel inside it verbatim, but the dashboard around them is a format of its own (`dataSources` / `visualizations` / `layout`). Export and import therefore go through lossy conversions. OpenCTI's dashboard variables and presets are not read at all (§4.7).

Measured against the Presentation and Video Studios, the gap is in creation, guidance and feedback. The chat wiring is the same shared code.

The target in one sentence: **a widget draws a dataset; a dataset is fed by a source (live) or by a feed (materialized, refreshed on a pull interval, a schedule or an event, optionally consolidated in a knowledge base); the agent plans, wires and checks all three from the studio, as it does a deck.**

## 2. Analysis: why "create with an agent" does not work

Each cause below is verified in the code at `cf85ac203`.

| # | Cause | Evidence | Effect for the user |
|---|---|---|---|
| R1 | No assistant carries the tool by default. `custom_dashboards` is allowed for the General Assistant but missing from its default set. `generate_presentation` and `video_studio` are in it. | `backend/app/models/general_assistant_config.py:63-76` (`DEFAULT_GA_BUILTIN_TOOL_IDS`). The rail offers only agents that carry the tool: `frontend/src/components/custom-dashboard/DashboardStudioRail.tsx:58` (`useToolAgents`). | The studio rail says "No agent carries the dashboard tools yet". From the dashboards list, "Build with the assistant" opens `/chat` with a General Assistant that has no dashboard tool. |
| R2 | No "create with the assistant" path into the studio. `NewDashboardDialog` creates an empty dashboard from a title and opens the studio with no brief and no agent. The list's "Build with the assistant" primes `/chat` with a fixed sentence. | `NewDashboardDialog.tsx:44-58`; `CustomDashboards.tsx:342-343`; `DashboardStudioRailProps` has no `autoSendMessage` / `initialAgentId`. The Video Studio has both: `NewVideoDialog.tsx:455-469` → `VideoEditor.tsx:272-283`. | The user lands on an empty stage with an empty chat, or in a chat that is not bound to any dashboard. |
| R3 | Frozen values are the easiest path. The v1 `widgets` dialect (score, stats, feed...) takes values the agent typed at build time. The tool tells it to "compose from data you gathered with your other tools". | `builtin_tools/custom_dashboard.py:784-836` | The dashboard is a screenshot of one answer. Nothing reads its data again, so it is stale the next morning unless an assignment rewrites the whole dashboard. |
| R4 | Live widgets from integrations are guesswork for the model. The `native` configuration is described in one line ("an OpenCTI dashboard widget {type, perspective, dataSelection, parameters}"). Meanwhile the UI has a full declarative form (`dashboard_platform/forms.py`, 667 lines) and schema lookups (`dashboard_platform_schema.py`) that no tool exposes. | `dashboard_platform/__init__.py:57-61`. The only feedback is `preview_platform_widget`. | "Add a widget on my OpenCTI reports by country" fails or comes out wrong: the model has to invent OpenCTI filter groups. |
| R5 | Only three platforms can feed a widget. Live sources are OpenCTI, OpenAEV and OpenCRQ. None of the other ~150 integration providers can, and neither can a knowledge base. An `assignment` source exists, but the agent cannot create or wire one from the studio. | `dashboard_sources.py:14-24` (types `static`, `platform`, `assignment`, `variable`, `ask`, `derived`); `backend/app/integrations/providers/` (152 entries). | "A widget on my CrowdStrike detections / Jira tickets / KB" is impossible, except by hand-wiring an assignment, its JSON answer and a path. |
| R6 | Data is fresh only while someone has the page open. `refresh` and `Auto-refresh` are browser timers. Nothing is materialized on the server, nothing reacts to an event, and nothing is pushed to open viewers. | `DashboardRefresh.tsx:63` (`setInterval`); `dashboard_definition.py:262-285` | A scheduled export or a published link reads whatever it reads at that moment. A pull duration or an event cannot be chosen. |
| R7 | No feedback loop on the result. A save runs no render check (decks run a layout check, videos a quality check). There is no catalog mode (`get_presentation(reference=)`) and no dashboard skill (decks and videos have one bound to the General Assistant). `export_dashboard` can produce a PNG, but nothing uses it as a check. | `skill_content/` holds `presentation-design.md` and `video-production.md` only. The whole v2 grammar sits in the `definition` description of `build_dashboard` (7.4k characters). | The agent saves and believes it is done. A broken source, an empty chart or an overlap goes unnoticed. |
| R8 | The studio does not follow the agent. `onSaved` is a no-op, `dashboard_saved` carries no widget id, and the turn's studio block lists the layout but not the health of each source. | `DashboardStudio.tsx:181`; `dashboard_studio.py:150-171` | The user does not see what was just changed. The agent does not know which widget is broken. |
| R9 | The feature is off by default (`DASHBOARD_STUDIO`, `ENABLED_FEATURES`). | `core/feature_flags.py:66-72` | Expected while it is unannounced. It needs to be on in any environment where the studio is evaluated. |

What already works and is kept: the binding per turn (`Conversation.custom_dashboard_id`, `load_dashboard_studio_context`), the tools forced on for the turn (`STUDIO_TOOL_IDS`), the shared `StudioRail`, `dashboard_saved` invalidation, revisions with optimistic concurrency, per-viewer access on live sources, history and diff, packs, publishing and exports.

## 3. Benchmark

### 3.1 Inside XTM One

| Capability | Presentation | Video | Dashboard today | Target |
|---|---|---|---|---|
| Brief dialog → studio → first message sent automatically, agent pre-chosen | via `/chat` | yes (`NewVideoDialog`) | no | yes (§5.1) |
| Tool on in the General Assistant by default | yes | yes | **no** | yes |
| Skill with the working method | `presentation-design` | `video-production` | none | `dashboard-design` |
| Schema / catalog discovery tool | `get_presentation(reference=)` | `video_studio_capabilities` | none | `dashboard_catalog`, `describe_platform_widget` |
| Automatic check after each save | layout check (headless) | quality check | schema only | source health + render check |
| Visual feedback to the model | `capture_presentation_slides` (PNG) | `render_video_scene` (frames) | none | `check_dashboard(capture=true)` |
| Stage follows the pen | yes (`focus_slide_id`) | yes | no | yes (`focus_widget_id`) |
| Long jobs + wake + awaited-work banner | n/a | yes | n/a | yes, for feeds |
| Data grounding | inline | skill §8 | live platform widgets (best of the three) | + integrations, KB, feeds |

### 3.2 Outside (state of the art for AI-assisted BI and observability)

Patterns that leading tools share, and that this definition adopts:

1. **Data first.** The assistant builds or picks a dataset or semantic layer, then the visuals. It never pastes numbers into a chart.
2. **Plan, then build.** A natural-language request becomes a proposed page (widgets with their data), the user adjusts it, and the result is built visual by visual, each one previewed on real data.
3. **Discoverable configuration.** The assistant queries the catalog (fields, measures, filter values) instead of guessing a query language.
4. **Self-check.** The assistant looks at what it produced (empty result, error, unreadable chart) and fixes it before handing over.
5. **Freshness as a property of the data.** Each source has a refresh policy (live, interval, schedule, event or stream) and a visible "as of"; viewers get updates pushed.
6. **Narrative from the data.** Generated insights ("what changed since yesterday") are computed from the dataset and refreshed with it.
7. **Thresholds and alerts** on a widget's value notify someone instead of waiting for them to look.

XTM One already matches the market's dashboard studios on rendering and editing. Patterns 1–7 are what is missing.

## 4. Target model

### 4.1 Three layers

```
Source ──(read live, or materialized by a Feed)──▶ Dataset ──▶ Visualization
                                    ▲
               Feed = Assignment on an Agent (invariant 1)
               trigger: interval | schedule | integration_event | platform_event | webhook | manual
               sinks:   dataset snapshot (+ history)  and/or  knowledge base document
```

### 4.2 Source types (new in bold)

| Type | Reads | LLM | Access |
|---|---|---|---|
| `platform` | OpenCTI / OpenAEV / OpenCRQ widget (exists) | no | viewer (live) |
| **`integration`** | One **read** tool of an integration the writer can use, called with fixed arguments. The result is mapped by a JSON path, then optional `derived` steps. The read/write classification is the existing one (`services/tool_classification.py`), so a write tool is refused. | no | viewer when live; feed owner when materialized |
| **`knowledge`** | A knowledge base document holding a table (JSON, JSONL, CSV or a Markdown table, whole or one heading section via `kb_sections`), parsed deterministically into a shape | no | viewer (KB read grant) |
| `assignment` | Last completed run's JSON (exists). Becomes the storage of a **feed** (§4.3). | yes (in the run) | viewer's visibility of the run |
| **`dataset`** | The latest snapshot a feed wrote (with history for trends) | no | feed owner (published data, §4.5) |
| `ask`, `variable`, `static`, `derived` | unchanged | | |

`static` stays for narrative text and sample data. A numeric visualization an **agent** binds to `static` data gets a save-time warning in the tool result ("frozen value: bind a source or a feed"). The warning is written in code, not in the prompt (invariant 16).

### 4.3 Feeds: consolidating data, including through a knowledge base

A **feed** is an assignment on an agent, created from the studio by the agent or the user. Reusing assignments covers invariants 1 and 7, the quota, `agentic_gate`, holds, the run history and pausing. It carries:

- **trigger**: one of the existing assignment triggers. `schedule` covers the pull duration and cron; `integration_event` (provider event key + filter), `platform_event` and `webhook` cover the event-driven case; there is also `manual`.
- **prompt**: what to gather and how to consolidate it. For example: "read the last hour of CrowdStrike detections and the open Jira SOC tickets, merge them into the SOC knowledge base, then count by severity and by day".
- **output_schema**: the dataset shape (`number`, `series`, `distribution`, `rows`...). It is enforced by the existing `emit_repaired_json` path (`agents-and-assignments.md` > Output Format).
- **sinks**:
  - `dataset`: the run's JSON becomes a new snapshot of the dataset, with its `run_id`, `fetched_at` and producer. History is kept up to N snapshots, so a trend can be drawn without the agent recomputing it.
  - `knowledge` (optional): the run writes or updates a document of a KB the agent may write to, with the existing `store_knowledge` / `update_knowledge_document` (append, `replace_section`). The KB is the **consolidation memory**: readable by people and searchable by agents later, with provenance. The dataset is what the widget draws. One run writes both, so they cannot drift apart.

Why the KB is not the only store: a KB document is text plus vectors, and every update re-chunks and re-embeds it (`integrations-and-knowledge-bases.md` > Full source documents). That is fine at a feed's pace and wrong for a widget read on every page view. A user who curates a table in a KB by hand can still point a `knowledge` source at it directly.

A feed that needs no reasoning (one integration read, mapped) is not a feed. It is an `integration` source with a materialized refresh (§4.4): no LLM call and no quota.

### 4.4 Refresh policy (the "pull duration or event" choice)

Every source and every dataset carries one policy. The UI shows it in `Data source > Refresh`, and the agent sets it with the same field:

| `refresh.mode` | Meaning | Where it runs | Floor |
|---|---|---|---|
| `live` | Read on view, cache 60 s (today's behaviour) | viewer's request | n/a |
| `interval` + `every` ("durée de pull") | Read again every N, **on the server**, whoever is watching; the result is materialized | scheduler → `orchestration_engine` execution | 30 s platform, 5 min integration, 15 min feed |
| `schedule` + `cron` | Same as `interval`, on a cron (UTC) | scheduler | |
| `event` + `on` | Read again when the event arrives: `integration_event` (provider + key + filter), `platform_event` (name + filter) or `webhook`. Debounced (`coalesce`, 60 s by default) and capped (`max_per_hour`) | event dispatch | |
| `manual` | Only when someone presses `Refresh now` or calls `refresh_source` | | |

- **Push.** Each new snapshot publishes `dashboard.source_updated {dashboard_id, source_id, fetched_at}` on the event bus. Open pages (dashboard, studio, home dashboard) receive it over the existing SSE channel and invalidate only that source (TanStack Query, invariant 5). The browser `Auto-refresh` remains, for `live` sources only.
- **Freshness.** Each visualization can show "as of HH:MM, by <feed>" and a `stale` badge after `staleAfter` (default 3 × the interval). A failed refresh keeps the last good snapshot and shows the error code. It never shows an empty chart.
- **Consumers.** Scheduled exports, published links and the home dashboard read the materialized snapshot, so they no longer pay for a live read.

### 4.5 Access model (decision D1)

- `live` sources: unchanged. Each viewer reads with their own access.
- Materialized data (`interval`, `schedule`, `event`, feeds) is read with the access of **its publisher**: the person who set the policy, as published links do today (`custom-dashboards.md` > Published link).
- The share dialog and the source say so ("Data read by Alice every 15 min"). Changing what a materialized source reads makes the changer its publisher.
- The marking ceiling of published links applies to OpenCTI data in a materialized snapshot.
- A viewer who could not read the source live still sees the snapshot. That is the point of sharing a consolidated view, and it is the decision to confirm.

### 4.6 Guarantees

- Quota and gates: feeds go through `agentic_gate` and quota like any assignment. Deterministic `integration` and `knowledge` refreshes use no LLM, so they are not metered as agentic.
- No vendor logic (invariant 18): the `integration` source calls any read tool through the generic integration tool executor, and the mapping is declarative (JSON path + `derived` steps).
- Secrets never reach a definition, a snapshot or a tool result (invariants 2 and 4). Arguments are validated against the tool's input schema at save time.
- Untrusted content: integration and KB data reach the model only through `wrap_untrusted`, as `preview_platform_widget` already does.

### 4.7 Storage format: native OpenCTI and OpenAEV (version 3)

#### Today (version 2)

- **What is already native.** A platform widget keeps the platform's own configuration verbatim in `dataSources.<id>.options.native`: `{type, perspective, dataSelection, parameters}` for OpenCTI, `{widget_type, widget_config}` for OpenAEV.
- **What is not.** The dashboard around those widgets has a shape of its own:
  - `dataSources`, `visualizations`, `inputs` and `layout.layoutDefinitions`;
  - `$name$` tokens;
  - its own option names (`majorValue`, `stackMode`...);
  - a grid with a 36 px row pitch, where OpenCTI uses 82 px and OpenAEV 70 px (`dashboard_grid.ROW_PITCH`).
- **Consequences.** `dashboard_interop/` converts on every import and export, and a round trip loses or approximates:
  - layout heights;
  - text widgets;
  - OpenAEV parameters, which become inputs.
- **Not read at all.** OpenCTI's `variables` (13 typed dashboard variables, referenced as `$var:<uuid>` in filter values) and `presets` are ignored. Nothing in `dashboard_interop/` or `dashboard_live/` reads them. A dashboard exported from a recent OpenCTI that uses them is imported with filters naming variables nobody sets.

#### Upstream formats (read from source, OpenCTI `master` 7.261124, OpenAEV `main`)

| | OpenCTI | OpenAEV |
|---|---|---|
| Dashboard document | Workspace `manifest`, stored as base64 JSON: `{config, widgets: {id: widget}, variables?, presets?}` | Relational: `custom_dashboards` + `widgets` + `custom_dashboards_parameters`, exported as a JSON:API document in a ZIP |
| Widget | `{id, type, perspective, dataSelection[], parameters, layout{x,y,w,h,i,moved,static}}`. 17 types, `text` among them (`parameters.content`, Markdown) | `{widget_id, widget_type, widget_config (typed by widget_configuration_type), widget_layout{widget_layout_x,y,w,h}}`. 13 types |
| Grid | 12 columns, row height 62, margin 20 | 12 columns, row height 50, margin 20 |
| Dashboard inputs | `variables` (vocabulary, entity, label, marking, text, numeric, date...) + `presets` | Parameters: `simulation`, `scenario`, `timeRange`, `startDate`, `endDate`. A filter value names the parameter id |
| Filters | FilterGroup with nested `filterGroups` | FilterGroup without nesting |
| Period | `config.relativeDate` / `startDate` / `endDate` | `time_range` per widget, or parameters |
| Tabs, absolute layout, interactions, non-platform data | none | none |

The two formats share the 12-column grid, filter groups, a title per widget and a dashboard period. They do not share a document shape. **One document cannot be native to both at the dashboard level; it can be native to both at the widget level.**

#### Target

The XTM One dashboard **is an OpenCTI manifest**. OpenCTI is chosen as the envelope because it is a single JSON document keyed by widget id, with typed variables and text widgets; OpenAEV's model is relational and only becomes a document when exported.

- **OpenCTI widgets** are stored byte-identical to OpenCTI's.
- **OpenAEV widgets** are stored byte-identical to OpenAEV's (`widget_type`, `widget_config`); only their layout is written in the manifest's grid.
- **What neither platform has** lives under one extension key, `x_xtm_one`, on the STIX custom property model (`x_opencti_*`). Exports drop it and list what it held.

```json
{
  "config": {"relativeDate": "months-3", "startDate": null, "endDate": null, "refresh_interval": 0},
  "variables": [
    {"id": "7c1e…", "name": "Sector", "type": "entity", "entityTypes": ["Sector"], "restriction": {"mode": "none"}, "defaultValue": null},
    {"id": "1508…", "name": "Simulation", "type": "x_xtm_one.simulation", "restriction": {"mode": "none"}, "defaultValue": null}
  ],
  "presets": [],
  "widgets": {
    "w_text":  {"id": "w_text", "type": "text", "perspective": null, "dataSelection": [],
                "parameters": {"title": "Readme", "content": "## Weekly review"},
                "layout": {"i": "w_text", "x": 0, "y": 0, "w": 4, "h": 2}},
    "w_octi":  {"id": "w_octi", "type": "vertical-bar", "perspective": "entities",
                "dataSelection": [{"label": "Reports", "attribute": "entity_type", "date_attribute": "created_at",
                                   "perspective": "entities", "number": 10,
                                   "filters": {"mode": "and", "filterGroups": [], "filters": [
                                     {"key": "objects", "values": ["$var:7c1e…"], "operator": "eq", "mode": "or"}]}}],
                "parameters": {"title": "Reports per month", "interval": "month", "stacked": false, "legend": true},
                "layout": {"i": "w_octi", "x": 4, "y": 0, "w": 8, "h": 4}},
    "w_oaev":  {"id": "w_oaev", "widget_type": "number",
                "widget_config": {"widget_configuration_type": "flat", "title": "Vulnerable endpoints",
                                  "series": [{"name": "", "filter": {"mode": "and", "filters": [
                                    {"key": "base_simulation_side", "operator": "eq", "mode": "or", "values": ["1508…"]}]}}]},
                "layout": {"i": "w_oaev", "x": 0, "y": 2, "w": 4, "h": 2}},
    "w_feed":  {"id": "w_feed", "type": "x_xtm_one.gauge", "perspective": null, "dataSelection": [],
                "parameters": {"title": "Critical detections"},
                "layout": {"i": "w_feed", "x": 4, "y": 4, "w": 4, "h": 3}}
  },
  "x_xtm_one": {
    "version": 3,
    "title": "SOC morning brief",
    "description": "…",
    "widgets": {
      "w_octi": {"platform": "opencti", "integration_id": "…", "view": "line", "interactions": [], "visibility": null},
      "w_oaev": {"platform": "openaev", "integration_id": "…"},
      "w_feed": {"platform": "xtm_one", "source": "ds_crit", "options": {"min": 0, "max": 100, "ranges": [{"to": 50, "value": "success"}]}}
    },
    "sources": {"ds_crit": {"type": "dataset", "feed": "…", "refresh": {"mode": "event", "on": {"integration_event": "crowdstrike.detection"}}}},
    "tabs": [{"id": "main", "label": "Overview", "layout": "grid", "widgets": ["w_text", "w_octi", "w_oaev", "w_feed"]}],
    "canvas": {},
    "theme": null,
    "application": {"hideExport": false}
  }
}
```

**Rules**

1. **One widget, one platform.** `x_xtm_one.widgets.<id>.platform` says whose widget it is (`opencti`, `openaev` or `xtm_one`). The native keys are that platform's alone, and the extension never repeats or overrides them. For example, OpenCTI's `parameters.stacked` is the stack mode; there is no second `stackMode`.
2. **Variables are the inputs.** XTM One inputs become manifest variables, and filters reference them as OpenCTI does (`$var:<uuid>`).
   - OpenCTI's own types (entity, text, numeric, date, vocabulary, label...) are used as they are.
   - OpenAEV's `simulation` / `scenario` parameters and the XTM One controls OpenCTI lacks (dropdown with choices, multiselect, button) are variable types prefixed `x_xtm_one.`.
   - The period is OpenCTI's `config` (`relativeDate`, `startDate`, `endDate`). OpenAEV's `timeRange` / `startDate` / `endDate` parameters map onto it.
   - `presets` are supported: a new feature, inherited from OpenCTI.
   - Tools and the UI show variables by name. `$var:<uuid>` stays an implementation detail.
3. **The grid is OpenCTI's**: 12 columns, row height 62, margin 20.
   - XTM One's finer 36 px rows are given up, so platform widgets look the same everywhere and a layout survives a round trip.
   - OpenAEV layouts are converted by row pitch (70 ↔ 82), as `dashboard_grid` already does.
4. **XTM One's own widget types** are OpenCTI-shaped widgets with an `x_xtm_one.<type>` type: `gauge`, `sankey`, `map`, the security posture views... Their data source and options live in the extension. When an XTM One type has an equivalent among OpenCTI's 17 types (`number`, `list`, `vertical-bar`, `horizontal-bar`, `line`, `area`, `donut`, `radar`, `polar-area`, `heatmap`, `tree`, `map`, `wordcloud`, `timeline`, `text`), the OpenCTI name is used.
5. **Export is extraction, not conversion.**
   - To OpenCTI: the manifest without `x_xtm_one`, the non-OpenCTI widgets and the non-OpenCTI variables.
   - To OpenAEV: its widgets as they are, their layouts converted, the variables they use turned back into parameters.
   - The report lists everything left out, as today.
   - A custom view is the same manifest with `x_xtm_one.target_entity_type`.
6. **Import is insertion.** OpenCTI and OpenAEV widgets are kept as they are, OpenCTI variables and presets included. This closes today's gap.

**Option names.** The XTM One options (`x_xtm_one.widgets.<id>.options`) are renamed where they repeat the old vocabulary. For example, `majorValue` → `value`, `<key>EditorConfig` → `<key>Ranges`, and the dynamic expression syntax `> primary | seriesByName('x') | lastPoint()` is kept or replaced (decision D7). Rule 1 removes the options that duplicate a native parameter.

#### Migration

- **Read-time upgrade, as for version 1 → 2.** `dashboard_definition.upgrade()` reads version 1, 2 or 3 and returns version 3. A save writes version 3. Rows, history versions and packs are never rewritten in bulk, so there is no data migration and no lock (`migrations-safety.md` does not apply).
- **One adapter on the frontend.** `components/custom-dashboard/definition.ts` reads version 3 into the same widget records `normalizeWidget` draws today, so the renderers and charts do not change.
- **What changes:**
  - validation (`dashboard_definition.py`, `custom_dashboard_spec.py`);
  - the edit operations of the API and the tools (the `set` / `delete` paths);
  - the studio's editing layer (`viz-config.ts`, `canvas.ts`, `DashboardDesignBar`, `VisualizationConfig`, `DashboardInputsPanel`, `DashboardDataSourcesPanel`, `DashboardSourceDialog`);
  - interop, which gets simpler;
  - packs: pack version 2 is a manifest fragment;
  - published-link snapshots: their payload is the renderer model, so they are untouched;
  - about 30 source files and 48 test files read the version 2 keys.
- **Status (2026-11-24).** Landed:
  - **Storage.** Every save is stored as the manifest (`dashboard_native.to_native`, called by `custom_dashboards._write`), and `upgrade()` reads it back (`from_native`). The round trip is exact: every DB-backed dashboard test goes through it.
  - **API and editor.** The API serves `manifest` next to the working `spec`. The source editor shows and saves the whole manifest, and the history compare diffs manifests.
  - **Agent.** `build_dashboard` takes a manifest as `definition`.
  - **OpenCTI variables.** Export writes the inputs its widgets read as variables (`$var:<id>`). Import reads entity variables as entity inputs, and the other variable types as their default value.

  - **D8, grid rows.** The studio's grid is OpenCTI's: 62 px rows and 20 px gaps (`OPENCTI_GRID_ROWS`, `dashboard_grid.ROW_PITCH`). The platform's own dashboards (CTEM, Copilot) keep theirs.
    - Data stored before D8 is never rewritten in bulk. A dashboard or version (v1, v2, or a manifest without `x_xtm_one.grid`) is read in OpenCTI rows when the ORM loads it (`dashboard_native.current_form`, `load` / `refresh` listeners). A widget pack without `source_meta.grid` is read the same way. A pack file of version 1 is read the same way on import.
    - Conversion maps tops and bottoms, so widgets that touched still touch.
    - An OpenAEV layout now comes back within one row: its 70 px rows are finer than the grid's.

  Not yet:
  - D7: the option renames.
  - Element scopes: the source editor's single-element views and `get_dashboard` / `edit_dashboard` still address the working definition. The agent tools of §5.3 address the manifest.
  - Import and export still build pack widgets in between rather than inserting and extracting manifest widgets directly.
- **Timing.** The feature is unannounced (`DASHBOARD_STUDIO` is off by default), so this is the moment to do it. It must land **before** the new agent tools (§5.3), which would otherwise be written against version 2 and rewritten afterwards.

## 5. Target agent experience

### 5.1 Entry points

1. **Dashboards list → `Create`** opens a two-tab dialog, **With the assistant** (default) or **Blank**. The first tab collects:
   - a brief;
   - optionally, the data to use: integrations, knowledge bases, existing assignments (chips, each checked against the user's access);
   - a starting point: empty, a widget pack, or a duplicated dashboard;
   - the agent ("Made by", default from `useToolAgents`).

   It creates the dashboard, opens `/dashboards/{id}/studio` with `state {send, agentId}`, and the rail sends the first message (`autoSendMessage`, `initialAgentId` added to `DashboardStudioRailProps`). This is the Video Studio pattern.
2. **Chat** "build me a dashboard of …": the agent creates the dashboard, and the card offers `Open in the studio`, which binds the conversation (exists).
3. **Studio, empty stage**: suggestion chips built from what the user can actually read: their integrations and KBs, not fixed OpenCTI sentences.

### 5.2 Working method (skill `dashboard-design`, bound to the General Assistant)

1. **Plan.** Read the brief and discover the data with `find_data`. Propose a plan card: each widget with its question, its source, its visualization and its refresh. The user edits or approves it in the chat (a `checklist`-style card).
2. **Build widget by widget.** `add_widget` creates the source and its visualization together and places them. The stage scrolls to and selects each new widget (`focus_widget_id` on `dashboard_saved`).
3. **Check.** After each save, the tool result carries each source's health (ok / empty / error code / waiting on input). `check_dashboard(capture=true)` returns a PNG of the tab as a vision block (reusing `dashboard_export`) plus findings: an empty chart, a truncated legend, an overlap.
4. **Feed and refresh.** For data that needs reasoning or consolidation, `create_feed` (§4.3) creates the assignment and its dataset and runs it once. The awaited-work banner and the wake (`conversation_awaited_work`, `parent_wake`) resume the conversation when the first snapshot lands.
5. **Iterate surgically** with `update_widget` / `edit_dashboard`. Other widgets stay byte-identical (exists).

The craft lives in the skill and the tool results, never in the system prompt (invariant 16).

### 5.3 Tool surface (studio)

| Tool | New / changed | Purpose |
|---|---|---|
| `dashboard_catalog(shape?, type?)` | new | Visualization types per data shape and their options, generated from `dashboard_visualization_options.py` (no hand-written copy) |
| `find_data(question)` | new | Candidate sources the user can read: platform widget types, integrations with their read tools (`list_integration_tools`), knowledge bases, assignments, variables |
| `describe_platform_widget(source, type)` | new | The widget's form from `dashboard_platform/forms.py`, flattened to fields, allowed values and an example `native` |
| `lookup_platform_values(source, kind, query)` | new | `dashboard_platform_schema` lookups (entity types, attributes, filter keys, values), with the user's access |
| `preview_source(source)` | generalizes `preview_platform_widget` | Any source type: shape, sample rows, or the error code and how to fix it |
| `add_widget(source, visualization, placement?, refresh?)` | new | Source + visualization in one revision; returns health and the widget id |
| `update_widget`, `remove_widget`, `arrange` | thin wrappers over `edit_dashboard` ops | Smaller, single-purpose descriptions |
| `set_refresh(source_id, policy)` | new | §4.4 |
| `create_feed(agent_id?, trigger, prompt, output_schema, sinks)` / `run_feed(source_id)` | new | §4.3; builds the assignment through the same service as `agent_builder` (`can_manage` gates) |
| `check_dashboard(dashboard_id, capture?)` | new | Health of every source + optional PNG + layout findings |
| `build_dashboard` | narrowed | Creates a dashboard (title, description, optional pack). The `definition` grammar moves to `dashboard_catalog`, and the v1 `widgets` dialect leaves the studio (kept for chat cards, D4) |

The per-turn studio block (`dashboard_studio.py`) adds one line per source with its health and freshness, so "fix the broken widget" means something.

### 5.4 Manual parity

Each agent capability has a UI twin. `Data > New` gets **Integration** (pick the integration, then a read tool, its arguments from the tool's schema, a mapping, `Preview`) and **Knowledge base** (pick the document and section, then `Preview`). `Data source > Refresh` sets the policy, and `Data > Feeds` lists the feeds (trigger, last run, next run, pause, run now, open the assignment).

## 6. Delivery plan

| Phase | Content | Done when |
|---|---|---|
| **0 – Unblock (days)** | `custom_dashboards` in `DEFAULT_GA_BUILTIN_TOOL_IDS` + migration for existing rows (`s1925gadash01`); "With the assistant" create dialog + `autoSendMessage` / `initialAgentId` on the rail; list button opens that dialog instead of `/chat`; `focus_widget_id` + `onSaved` follow-the-pen. Source health in the studio block moves to phase 2 with `check_dashboard`: reading every source on every chat turn would make each turn wait on the platforms | A fresh install with the flag on: Create → brief → the General Assistant builds a dashboard live in the studio, with no setting touched |
| **1 – Native format (version 3)** | `upgrade()` to version 3 and validation; frontend adapter (`definition.ts`); API and tool edit paths; studio editing layer; interop rewritten as extraction / insertion, with OpenCTI variables and presets; pack version 2; user and rendering docs | Round trips lose nothing: an OpenCTI export (with variables) imported and exported again comes back byte-identical, and so does an OpenAEV ZIP, apart from ids. Every stored version 1 / 2 dashboard opens and saves as version 3. The renderer tests are unchanged |
| **2 – Discoverable live widgets** (landed 2026-11-24: `custom_dashboard_studio.py`, `dashboard_platform.describe_widget`, `dashboard_definition.visualization_catalog`, skill `dashboard-design`) | `describe_platform_widget`, `lookup_platform_values`, `dashboard_catalog`, `preview_source`, `add_widget`; `dashboard-design` skill; `check_dashboard` (health + PNG) | Eval set: 20 briefs on OpenCTI/OpenAEV/OpenCRQ data; ≥ 90 % of widgets render non-empty on first hand-over (measured with `check_dashboard`) |
| **3 – Integration and knowledge sources + refresh policy** (landed 2026-11-24: `integration` / `knowledge` sources, `refreshMode` server / manual, `dashboard_refresh` + its tick step, `dashboard.source_updated` on the activity stream, D1 as recommended; `schedule` and `event` modes stay with phase 4) | `integration` and `knowledge` source types (backend reader, UI form, agent tool), server-side materialization for `interval` / `schedule` / `manual`, `dashboard.source_updated` push, freshness badges, publisher access (D1) | A widget on a non-Filigran integration (e.g. Jira, CrowdStrike) refreshes every 15 min with the page closed, and an open page updates without reload |
| **4 – Feeds and events** (landed 2026-11-24: `schedule` / `event` refresh modes with a 60 s coalesce, integration events heard where they trigger an assignment, `create_feed` through the agent builder's own tools with the run owed to the conversation, `history` on assignment sources, a completed run pushed to the pages reading it; the KB sink is the feed's own `update_knowledge_document` step, not a second write path) | `create_feed` / `run_feed`, `dataset` source + snapshot history, KB sink, `event` refresh mode (integration_event / platform_event / webhook) with debounce and caps, awaited-work wake | "Every new CrowdStrike detection, consolidate into the SOC KB and update the severity widget" works end to end from one chat request |
| **5 – Insight and alerting** | Narrative widget computed from a dataset at each refresh (grounded: it cites the snapshot), thresholds on a value → notification / work item | Optional, after phase 4 adoption |

Each phase updates `docs/docs/usage/custom-dashboards.md` and `dev-docs/dashboard-rendering-reference.md`, adds its tests as a non-admin (rule 19), and keeps new files EE (`make edition`).

## 7. Decisions needed

| # | Question | Recommendation |
|---|---|---|
| D1 | Materialized data: whose access? | **Decided 2026-11-24 (phase 3):** the publisher's, shown in the UI, with the marking ceiling, like published links (§4.5) |
| D2 | Knowledge base as the only store, or a KB sink + a dataset snapshot? | Both, written by the same feed run (§4.3). The KB alone re-embeds on every write and cannot be queried as a table |
| D3 | Turn the tool on by default for the General Assistant (and the flag for evaluation environments)? | Yes, as for decks and videos (phase 0) |
| D4 | Remove the v1 `widgets` dialect from the studio tool? | Yes for the studio. Keep it for chat-only cards until a migration converts them to `static` + `xtm.*` (the converter exists in `dashboard_definition.upgrade`) |
| D5 | Event refresh limits | 60 s coalesce, 60 refreshes per hour per source, floors in §4.4; admin-tunable later |
| D6 | Envelope of the native format | **Decided 2026-11-24:** the OpenCTI manifest, with OpenAEV widgets verbatim inside it and one `x_xtm_one` extension (§4.7). The alternative, a neutral envelope holding both platforms' widgets verbatim, is just as lossless per widget but is native to neither |
| D7 | XTM One option names and the `> source \| step()` expression syntax | **Decided 2026-11-24:** rename the options that echo the old vocabulary (`majorValue`, `<key>EditorConfig`...) in the same read-time upgrade. Keep the expression syntax: it is the only dynamic colour mechanism, and replacing it is a separate design |
| D8 | Grid rows | **Decided 2026-11-24:** OpenCTI's (62 px + 20 px margin), giving up the 36 px rows: identical rendering across the three products and lossless round trips |

## 8. Invariant check

1. Automation stays an assignment: feeds are assignments.
2. Secrets stay encrypted (§4.6).
4. Integration credentials stay in the DB; a source names an integration id, never a credential.
5. Push goes through TanStack Query invalidation.
7. Reasoning happens only in assignment runs. `integration` / `knowledge` reads are deterministic data reads, like `platform` today.
16. Guidance lives in the skill and the tool results.
17. The `agentic_gate` covers feeds.
18. The integration source is generic over the tool registry and its classifier.
