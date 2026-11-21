# Flow Builder — Feature Flag

The visual flow builder ships behind the `FLOW_BUILDER` feature flag while it
is not announced externally. Two things the flag is **not**:

- **It is not a switch on the flow engine.** UI, documentation and the example
  flow's seeding are gated; the REST surface stays mounted and existing flows
  keep executing.
- **It does not hide all of `/flows`.** The `Agentic Flow` page has three tabs:
  `Agent-to-agent map` (with its `Graph` and `Schedule` views),
  `Fleets` and `Flows`. Only `Flows`, the library that leads into the builder,
  is the builder's. The map and the fleets predate it or do not depend on it:
  they read agents, fleets, assignments and delegations, with no flow endpoint
  between them. They stay live, and so does the `Agentic Flow` nav entry that
  leads to them.

## Enabling it

Off by default. Enable per deployment:

```
ENABLED_FEATURES=FLOW_BUILDER
```

The flag is read once at startup, so a change needs a restart, and the resolved
id list reaches the frontend through `GET /auth/config`. **Local development
needs it in your `.env`** — without it the flow library and the canvas are gone.

## What the flag hides

| Surface | With the flag off |
| ------- | ----------------- |
| `/flows` → `Flows` tab (the library) | Not listed; `/flows/library` normalizes to the `Agent-to-agent map` |
| `/flows/library/:id` (the canvas) | Redirects to `/` |
| ⌘K `flows.library` and every `flowBuilder.*` command | Not listed |
| Operations → Executions | Flow feed and its `Flows` type filter are gone; a stale `?type=flows` URL falls back to `All` |
| Chat agent picker | The `Flows` section (active chat-flows) is not fetched |
| Header notifications | `flow` / `flow_hitl` items are dropped — they deep-link into the canvas |
| Agent → Tooling | The `Flow Builder` built-in tool card is not offered |
| LLM Analytics → source filter | The `Flows` option is not listed (a stale `?source=flow` URL still works) |
| Agent wizard → Tools | `Flow builder (built-in)` is filtered out of `GET /flows/node-catalog`, so it is not a selectable card |
| `GET /settings/builtin-tools/catalog` | The `flow_builder` key (13 flow-authoring sub-tools) is omitted — filtered in the endpoint, *not* in `build_builtin_tool_catalog`, which doubles as the registry skill-package validation reads |
| Fresh install / AI switched on | The example flow is not seeded (see below) |
| `/api/openapi.json` | The 28 `/api/v1/flows*` operations, the `/api/hooks/flows/...` receiver and the `flows` tag are absent — the routes still answer |
| Documentation site | `usage/flows.md` and `usage/flows-api.md` are excluded from the mkdocs build; `usage/agent-budgets.md` had its `Budget` node sections removed |

## What stays live

| Surface | Why |
| ------- | --- |
| `Agentic Flow` nav entry, `/flows` page | The map and the fleets live there |
| `Agent-to-agent map` tab, the landing tab whatever the flag says | Predates the builder; no flow endpoint behind it |
| Its `Schedule` view (`/flows/map/schedule`; the old `/flows/schedule` and `/flows/map?view=schedule` redirect there) | Same |
| `Fleets` tab and fleet pages (`/flows/fleets`) | Read `/api/v1/fleets`, not a flow endpoint |
| Agent → Overview → `View in flow` | Opens the map focused on that agent |
| ⌘K `flows.map`, `flows.schedule`, `flows.fleets` | Same |
| `usage/agentic-flow.md` | Documents the map, its focus field and the fleets |

Plus, deliberately:

- **`/api/v1/flows` stays mounted.** The agent-creation wizard reads the flow
  node catalog (`GET /flows/node-catalog`), so unmounting the router would
  break a shipped feature. Anything holding an API key can still reach the flow
  endpoints — the flag is not an authorization boundary, it only stops the
  platform from *advertising* the feature. Undocumented is done with
  `include_in_schema=False` on the routers (plus the one per-route gate in
  `webhooks.py`); dropping the `flows` entry from `OPENAPI_TAGS` alone would
  remove nothing but the group's description.
- **The engine keeps running.** Schedules, work-item and webhook triggers, the
  runner and the reconciler are all untouched, so an existing flow still
  executes. The one exception is **seeding**: the example flow is *not* created
  while the flag is off, because it is seeded `ACTIVE` and would manufacture
  exactly the unanswerable runs described below on every fresh install. The
  agent-creation wizard is unaffected — it reads the node catalog, not the
  example flow.
- **The `flow_builder` built-in tool still works** for an agent that already
  carries the id; only the picker card is hidden.
- **Platform Health's Flow Lab does not read this flag** (#8260). Its `flow_lab`
  tools, its shadow runs and an approved flow replacing an assignment ride the
  engine, which the flag never switches off; they follow `CONCIERGE`. A flow a
  switch-over activates while the builder is hidden runs like any existing
  flow, and its rollback deactivates it the same way.

If you need the feature genuinely dark rather than hidden — no REST surface, no
triggers — that is a larger change: it has to keep the agent-creation wizard
working without the node catalog.

## Turning the flag OFF on an instance that already has flows

The engine keeps running, but with the builder hidden there is **no UI left
that can answer a paused run**. A flow that stops on an `Ask user` node parks
in `awaiting_human`, and all three routes to `POST /flows/runs/{id}/respond`
are gated together: the HITL bell item, the Executions flow feed and the
builder canvas.

Un-gating the bell would not help on its own — its deep link lands on
`/flows/library/<id>`, which redirects home — so the affordance cannot be
restored without un-hiding the canvas, which is the point of the flag.

New installs are safe: nothing is seeded, and nothing can be authored through
the UI. The exposure is instances that **already** have flows — a dev or
staging platform that ran with the flag on, or flows authored through the
`flow_builder` agent tool or the still-mounted REST API. Before turning the
flag off there, drain or deactivate them: `GET /api/v1/flows/runs/all` still
answers and `POST /api/v1/flows/{id}/deactivate` still works.

## Known residue

**The bell can under-report, and is left that way.** `GET /activity` returns
the 20 most recent notification rows; flow rows still occupy slots in that
window while the builder is hidden and are only discarded client-side, so
assignment items can be pushed just outside it.

Two client-side compensations were tried and reverted, both costing more than
the symptom:

- *a flag-conditional limit* lands in the query key, so a flag-ON instance
  fetched 40 rows and then re-fetched 20 on every cold load, discarding the
  first;
- *an unconditional wider fetch plus a cap on the merged list* shortened the
  bell on every instance — assignment runs and activity rows merge in the
  header, so that list was never capped at 20 to begin with, and capping it
  dropped completed-run and analytics items that used to be listed and counted.

So the header keeps exactly the window it had before the flag. The real fix is
server-side: a `notification_source` filter on `GET /activity`, which needs a
JSON predicate (the endpoint filters on the `activity_type` column, while
`notification_source` lives in a JSON metadata blob) on a route the whole app
shares. Worth doing if flow activity on flag-off instances ever stops being
rare — with the example flow no longer seeded, it only comes from flows that
predate the flag.

*(Resolved: `agent-budgets.md` was carrying a full description of the `Budget`
node — the node library, the sockets, the wiring — while staying published. The
flow-specific sections are gone; the general rules they happened to carry
(per-dimension precedence, and the typed-versus-copied authority rule) were
re-homed on the same page rather than lost, since they govern the agent
`Budget` tab too. Restoring them means putting back the `## The Budget node in
the flow builder` section and the flow layers in `## How the layers resolve`.)*

## Reading the flag in the UI

`useFeatureFlag(flag)` for anything that only affects what is rendered.

`useFeatureFlagResolved(flag)` where the flag decides a **first-render** action
— which route to allow, which tab to mount, which query to fire. It reports
three states, and the difference matters:

| State | Meaning | What a caller should do |
| --- | --- | --- |
| `isResolved` | The flag is known | Act on `enabled` |
| neither | Still in flight | Wait — acting on the fail-closed `false` redirects deep links and fetches the wrong feed |
| `isError` | No config in hand *and* the fetch failed — the flag is **unknown** | Caller's choice, and never a silent "treat as off" |

Both flags derive from **whether a config is in hand**, never from the query's
status. TanStack Query v5's `isError` is `isLoadingError || isRefetchError`, so
a background refetch failure flips it while `data` stays cached and usable —
and with a 60s `staleTime` and `refetchOnWindowFocus` on, alt-tabbing away and
back is enough to cause one. Reading it raw tore down whatever the flag gated:
on `/flows/library/<id>` that unmounted the canvas and took unsaved graph edits
with it, over a flag that was known and enabled the whole time. Any new
consumer must do the same (`isError && !data`), which is why the hook does it
once for everyone.

`isError` exists because `useAuthConfig` sets `throwOnError: false`. It has to:
`Header` and `CommandsProvider` read it, so it runs under the *fullPage*
ErrorBoundary, where throwing replaces the whole app over one transient 5xx.
The cost of silencing it is that consumers must surface failure themselves —
`FeatureFlagGate` (`App.tsx`, the builder route's gate, shared with the Video
Studio routes) renders an error state instead of redirecting (a redirect
would read as "this feature does not exist"), `Login` shows a banner (an
undefined config falls back to "local auth, no SSO", i.e. a form that cannot
succeed), and `Flows` / Executions carry on fail-closed because their other
content does not depend on the flag. Retries are left at the app default, not
raised: two routes now hold a skeleton until this query settles.

## Removing the flag

Feature flags here are temporary. Once the builder is announced, drop
`FLOW_BUILDER` from `KNOWN_FEATURE_FLAGS` / `FeatureFlag`
(`backend/app/core/feature_flags.py`) and from `KNOWN_FEATURE_FLAGS` in
`frontend/src/lib/utils.ts`, then remove the `flowBuilder` command-context key,
the `include_in_schema` arguments, the node-catalog filter in
`api/v1/flows.py`, the built-in-tools catalog filter in `api/v1/settings.py`,
the two `seed_example_flow` gates and the call sites listed in the first table.
The `Header.tsx` notification window needs no change — it was deliberately
left exactly as it was before the flag.

For the documentation, delete the `exclude_docs` block from `docs/mkdocs.yml`
and `docs/mkdocs_serve.yml` and restore the two nav entries under
`Work with agents` (`Flows`, then `Flow builder API`, right after
`Agentic flow`). One published page had a link rewritten when they were pulled:
`usage/agent-budgets.md`, whose "Next step" pointed at `flows.md`.

`backend/tests/test_flow_builder_feature_flag.py` pins all of it.
