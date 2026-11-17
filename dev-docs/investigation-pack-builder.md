# Deep Investigation pack builder

Status: built as a stack of five PRs: pack options, model, API and
runs, the builder page, free HTTP sources, the
assistant panel. The decisions below were taken with the product
owner; the defaults section lists those taken while building.

## Why

Today a Deep Investigation pack is code (`investigation_packs.py`): the
built-in packs (`web-general`, `web-research`, `disinfo-infrastructure`) and
two per-caller packs assembled from rows (`knowledge-bases`, `mcp-tools`, in
`investigation_pack_resolver.py`). A method like CheckFirst's Storm-1516
mapping took a pull request to become a pack. Analysts should be able to write
their own method, share it, and run it from the picker, without a release.

## Where

Agent Studio gets a new entry after Skills: **Investigation packs**
(`/investigation-packs`), behind the `DEEP_INVESTIGATION` feature flag like the
mode itself. The list page shows the packs the caller can see (own, shared,
company-managed) and the built-in ones (read-only, "Duplicate" only).

## Decisions taken

| Topic | Decision |
|---|---|
| Delivery of the pivot-depth choice | Its own PR: pack options (`PackOption`) are the first brick of the builder. |
| What a pack is built from | Existing source kinds, free HTTP requests, duplication of a built-in pack, and a draft written with the assistant. |
| The plan | Ordered steps. Each step is an action served by its sources; preconditions follow from the order; the cited report is always the last action. No GOAP editor. |
| Who builds and shares | Every user, with the three-tier visibility of the other Agent Studio entities: own, shared (`read_only`, `config`, `config_with_chats`), company-managed by an admin. |
| Whose access a run uses | The runner's. Every integration-backed source (VirusTotal, OpenCTI, knowledge base, MCP tool) is resolved against what the runner may see. A source they cannot use is said in the picker and in the plan, never used with the author's rights. |
| Testing while building | A plan preview (no query, no quota), and a real run that opens in a chat (queries, quota and VirusTotal credits spent). |
| Pivots | The built-in sources keep the hops they know (passive DNS → IP → co-hosted sites → OpenCTI). A free HTTP step queries the run's subject only. |
| The assistant | A chat panel beside the builder, like Presentation Studio: it edits the draft, the user reviews and saves. |
| Choices at launch | The builder can expose source settings (pivot depth, VirusTotal pages, host-name keywords) with a default, and mark steps optional, on or off by default. Both are shown in the picker. |
| Recommendation | The creator chooses whether the pack is pre-ticked for its subject types, for themselves and for the people it is shared with. |
| Editing a used pack | Version history: every save is a version, viewable and restorable; a run records the version it used. |
| Delivery | A stack of PRs (below), each green on its own before the next opens. |

## The model

### `investigation_packs` (new table, shareable entity)

Full visibility column set (rule 19): `user_id`, `shared_group_ids`,
`shared_user_ids`, share tiers, `is_company_managed`; joins
`CompanyManagedEntity`; `can_manage` through `helpers.can_manage()`.

- `id`, `name` (1-120), `description` (≤ 2000), `subject_types` (subset of
  `SUBJECT_TYPES`, at least one), `recommended` (bool, the creator's choice),
  `max_iterations` (1-50, default 10), `current_version_id`, timestamps.
- Slug used in runs and the picker: `pack:<uuid>`, never a built-in slug.

### `investigation_pack_versions` (new table)

One row per save, immutable: `id`, `pack_id`, `number` (1, 2, ...),
`definition` (JSON, below), `created_by`, `created_at`, `note` (optional).
Restoring a version writes a new version with that definition.

### The definition (JSON, validated on save)

```json
{
  "steps": [
    {
      "key": "map",
      "label": "Map the hosting",
      "description": "Read the domain's resolutions from passive DNS.",
      "icon": "network",
      "optional": false,
      "default_on": true,
      "sources": [ { "kind": "passive_dns", "hostname_keywords": null, "...": "..." } ]
    }
  ],
  "options": [
    { "step": "map", "source": 0, "setting": "pivot_scope", "label": "Pivot depth", "default": "shared" }
  ]
}
```

- A source is stored WITHOUT a caller's integration or corpus id where the
  runner's own must be used: `{"kind": "passive_dns", "provider": "virustotal"}`
  is resolved at run time to the runner's connected VirusTotal; a knowledge
  base is named by `slug` or `tags` (`KnowledgeBaseSelector`, as declared
  packs do today); an MCP tool by server + tool name, resolved among the
  runner's servers.
- Every source is checked by the same constructor a stored ref goes through
  (`SourceDefinition.from_ref`), so a definition can hold nothing a run could
  not hold.
- Limits: 12 steps, 12 sources per step, 6 exposed options. Over a limit is a
  422 from the body model, never a 500.

### Runs

`investigations.source_pack_slugs` already records the packs; a built pack is
`pack:<uuid>`. New nullable column `source_pack_versions` (JSON, slug →
version id) so a run says which version it used. Steps keep their stored
`source_ref` as today, so a continuation replays what was queried, whatever
the pack became.

## Running a built pack

1. `resolve_packs` adds, for the caller, every built pack they can see,
   converted to a `SourcePack`: steps → a `GoalPlan` (one action per step,
   action *i* requires the effect of step *i-1*, the report last), sources
   resolved against the caller's integrations. A step none of whose sources
   resolves is kept and flagged `servable: false`, the way the plan does today
   for an action no source serves.
2. Optional steps and exposed settings become `PackOption`s, so the picker
   renders them like the pivot depth, and `with_options` applies them.
3. The continuation guard compares access identities with what
   `resolve_packs` expands for the caller, as today: a pack shared then
   revoked stops resolving at once, for the picker, the run and continuations.
4. Labels written by a user are shown as written: they are not catalog keys,
   so the "every plan string is translated" tripwire stays limited to the
   built-in plans. The picker, the continuation panel, the preview and the
   goal plan render a pack's name and a step's label and description through
   `useFormatter().t_text`, never `t_i18n`, which would read a person's
   apostrophes and braces as ICU syntax.

## Free HTTP sources

A URL template with one `{value}` placeholder (and the existing `host` /
`slug` transforms). It goes through the fetch the `http` kind already uses,
which must apply, and be tested to apply: the SSRF policy on the address
actually connected (no second DNS resolution), every redirect re-checked,
`trust_env=False`, a timeout, a streamed read bounded on raw bytes and a
content-type check. Where that fetch falls short of one point, it is fixed
there, for every `http` source, not around it. No custom headers and no credentials in a template (invariant 4:
credentials live in integrations). **Open:** a JSON field to extract (a path
like `data.attributes.last_analysis_stats`) or the page text only.

## The builder page

- Left: the pack's identity (name, description, subject types, recommended,
  budget), then the steps as an ordered list (drag to reorder), each with its
  sources and the optional switch (`Switch`, never a native checkbox).
- Right: the preview. A sample subject, its detected type, the plan as the
  block will draw it, which sources apply, which are missing for this user
  and why. "Run it" opens a chat with Deep Investigation armed and the pack
  pre-selected.
- Header: version history (list, view, restore), share, duplicate, delete.
- Built-in packs open read-only with "Duplicate".

## The assistant panel

A chat bound to the draft (the Presentation Studio pattern). Its tools work
on the draft only and never save: list the sources the user can use, add,
edit, reorder or remove steps, set options, preview the plan for a subject.
The user saves. It is metered like any agent turn.

## Delivery (stack)

1. **Pack options** (on #5100): `PackOption`, the pivot depth for lists, the
   picker rendering, `source_pack_options` on the send. Ready locally.
2. **Model, API and runs**: the two tables and the migration, CRUD + versions
   + sharing routes, conversion to `SourcePack` per caller, the picker and the
   run, version recorded on the run. Tested as a non-admin on the real routes:
   `read_only`, `config`, group grant, revoked share, company-managed.
3. **Builder page**: Agent Studio entry, list, editor, preview, run from the
   builder, version history, duplicate a built-in pack.
4. **Free HTTP sources**: the SSRF table of tests on the real fetch.
5. **Assistant panel**.

## Defaults taken for the open questions (to confirm)

Work started on PR 2 before these were settled; each is the safe default and
cheap to change.

- **Free HTTP** (PR 4): the page text only; a JSON field to extract is a
  follow-up.
- **Two recommended packs** on one subject: both are pre-ticked.
- **Restoring a version**: anyone who may edit the pack (`can_manage`).
- **Quota**: 200 built packs per owner, a 422 past it.
- **Pre-ticked for others**: a recommended pack is pre-ticked for its owner
  and, when company-managed, for everyone. A colleague's shared pack is
  offered, never pre-ticked: its free HTTP sources send the subject to hosts
  that colleague chose. The picker always says whose a built pack is and
  which typed hosts it sends the subject to (see "The picker" below).

## PR 2 design (model, API, runs)

### Tables (one migration)

- `investigation_packs`: `id`, `user_id`, `name` (200), `description`,
  `subject_types` (JSON list), `recommended` (bool), `max_iterations` (int),
  `definition` (JSON, the current version), `version_number` (int, the
  current one), the visibility columns of `Prompt` (`is_company_managed`,
  `shared_group_ids`/`_permissions`, `shared_user_ids`/`_permissions`,
  `access_restricted`), timestamps. Registered in `CompanyManagedEntity`.
- `investigation_pack_versions`: `id`, `pack_id` (cascade), `version_number`
  (unique per pack, > 0), `definition`, `subject_types`, `author_id`,
  `commit_message`, `created_at`.
- `investigations.source_pack_versions` (JSON, nullable): built pack slug →
  version number, written when the run starts.
- `investigations.declared_plan` (JSON, nullable): the plan a built pack's
  run followed, serialised, so the block draws it without reading the pack
  (which may have changed or been deleted since).

### Definition (pydantic, validated in the body model: a 422, never a 500)

A step: `key` (slug, unique), `label`, `description`, `icon` (one of
`GOAL_ACTION_ICONS`), `optional`, `default_on`, `subject_types` (subset of the
pack's, default all), `sources`. A source, by kind:

| kind | fields | resolved for the runner |
|---|---|---|
| `opencti` | none | the registered OpenCTI they can see |
| `passive_dns` | `hostname_keywords`, `max_pages`, `pivot` (`one_seed`, or for a list `shared` / `all`) | their connected VirusTotal |
| `site_fingerprint` | `fingerprint_set` | none needed |
| `web_search` | `search_prompt` (with `{value}`) | their search provider, as today |
| `knowledge_base` | `slug`, `tags` or `knowledge_base_ids`, `query_template`, `min_similarity` | the corpora they can see |
| `mcp_tool` | `server_id` | that server's designated tool, if they can see it |
| `reference` | `reference` (a `web-general` source by name: RDAP, crt.sh, Shodan InternetDB, NVD, ...) | none needed |

Every source may also narrow its step's subject types (`subject_types`), e.g.
a fingerprint in a step on domains and IPs; a source answering none of them is
refused.

Pivots: a `passive_dns` source names `pivot_step`, the step its hops serve
(the IP of a domain, the sites on an IP, what OpenCTI knows of them): its own
step or a later one. A later step may have no source of its own: it is fed by
the step that names it, the way `pivot_neighbours` is fed by
`map_infrastructure` in the built-in plan. The source carries it as
`hop_action`; the built-in pack's hops are unchanged (`hop_action` unset).

Exposed options: `{step, source, setting}` where `setting` is one of
`pivot`, `max_pages`, `hostname_keywords`, each with a fixed choice list; an
optional step is an option of its own (`on` / `off`). A step switched off
takes its sources and its plan action with it, and a source pivoting into it
stops hopping (`hop_action` cleared): its own lookup still runs, its leads
(a domain's IP, a list's shared IPs, their neighbours) are not queued. A
list's passive DNS source that names no pivot step maps the seeds and hops
to nothing (it used to queue the built-in pack's neighbour lookups).

Limits: 12 steps, 12 sources per step, 6 exposed settings. The picker sends
every option of a confirmed pack, defaults included; the send reads them
whole (at most 18 per pack) or refuses the turn, since a choice dropped would
run at its default.

A `web_search` prompt is checked by the rule its run formats it by
(`_check_query_template`: `{value}` exactly once, no other field, no stray
brace), so a prompt saved is one every runner can run.

### Running

`resolve_packs` adds every built pack the caller can see as `pack:<uuid>`:
sources resolved for the caller (a source they cannot use is dropped and
named in the pack's description), steps turned into a `GoalPlan` (step *i*
requires step *i-1*'s effect among the steps switched on; the report last),
options as `PackOption`s. `recommended` packs join `recommend_packs` for
their subject types in the picker, when they are the caller's own or
company-managed.

### The picker

Each built pack carries a `PackOwner` read from its row (owner id and name,
company-managed), never from its text. `GET /investigations/source-packs`
answers, per pack, `origin` (`builtin`, `mine`, `company`, `shared`),
`owner_name` (for `shared`) and `destinations`: the hosts its free HTTP
sources applying to the subject connect to, each URL built for this subject
as the run builds it and parsed as `pinned_fetch` parses it
(`pinned_fetch.fetched_url`, `httpx.URL`: the ASCII form of the name, as it
is resolved and sent; a `{value}` in the host shows the host it becomes). The picker shows "Shared by <owner>" or
"Company-managed", and "Sends the subject to <hosts>" (amber for a
colleague's pack). A `?pack=` link ticks the pack it names only when it is
not a colleague's.

### Routes (`/api/v1/investigation-packs`)

List (`scope=visible|all`), create, get, update (a new version when the
definition or subject types change), delete, versions (list, get, restore),
duplicate a built-in (`POST /from-builtin/{slug}`), preview
(`POST /preview`: a definition or a pack id, and a subject; returns the plan
the caller would run, no query sent). Every write is audited. Update and
restore read the version number under the pack's row lock when they
allocate one, so two saves at once get consecutive versions; the lock is
taken after the share edit's account locks, the order a user deletion takes. Tested as a
non-admin on the real routes: own, `read_only`, `config`, group grant,
revoked share, company-managed.

## PR 3 design (the builder page)

- **Agent Studio** gets "Investigation packs" (`/investigation-packs`), behind
  `DEEP_INVESTIGATION` in the nav (`NavFlags.deepInvestigation`), the hub card
  and the command palette (`deepInvestigation` context key).
- **The list**: own, shared and company packs (cards and table), and the
  built-in packs to duplicate into a private copy.
- **The detail** (`/investigation-packs/:id/:tab?`, `new` for a new pack):
  *Builder* (the editor beside the preview), *Sharing* (the standard sharing
  card; a note says a shared pack runs with the runner's own access) and
  *Versions* (each version's author and message, restore as a new version).
- **The editor** edits a draft through pure helpers (`pack-draft.ts`, unit
  tested): a step key is a slug of its label, removing a step drops the pivots
  into it and the settings it exposed, moving a step never leaves a pivot
  pointing backwards. What a source can be is read from the server
  (`GET /investigation-packs/catalog`: kinds, reference sources, keyword and
  fingerprint sets, icons, the settings a source exposes, the limits), so the
  frontend names no source of its own. The save sends the whole method; the
  server's 422 is shown as it is.
- **The preview** posts the draft to `/preview` and draws the steps the caller
  would run, served or not. **Run it in a chat** opens
  `/chat?investigate=<subject>&pack=<slug>`: the chat arms Deep Investigation
  with the subject in the composer, the picker ticks that pack, and the user
  still confirms before anything runs.

## PR 4 design (free HTTP sources)

- A builder source of kind `http`: a URL template (http or https, the subject
  placed by exactly one bare `{value}`, no credentials, `host` / `slug`
  transform optional; host and credentials judged on `httpx.URL`, the parse
  the fetch connects with). It becomes an `http` `SourceDefinition` with
  `pinned=True`, carried in its identity and stored ref, so a continuation
  fetches it the same way. The built-in http sources are not pinned.
- `services/pinned_fetch.py` sends through `core/pinned_request.py`
  (`pinned_client` + `send_pinned`): the host is resolved once and every address
  must pass `destination_policy.forbidden_address_reason` (private, loopback,
  link-local, metadata refused); the request goes to that address with the
  name as `Host` and TLS server name (`sni_hostname`), so the certificate is
  checked against the name and no second resolution can turn inward; no
  connection is kept (`Connection: close`), so a redirect to another name on
  the same address is a new connection verified for that name; each
  redirect (3 at most) is resolved, checked and pinned the same way;
  `trust_env=False`; `Accept-Encoding: identity`, a compressed body refused;
  the raw body read up to 512 KiB; text, JSON and XML only.
- What a refused fetch shows is generic (`source.free_http_refused`, "this
  address could not be read"); the precise reason is logged. A timeout and an
  HTTP status keep their usual codes. What the source answered is judged like
  a shared fetch's page: an HTML page is read through the shared extractor
  (`web_fetch.extract_from_html`), never as markup, and an answer under
  `MIN_USEFUL_CHARS` (`MIN_USEFUL_JSON_CHARS` for JSON or XML) is `empty` with
  `source.thin_response`. Otherwise it is one finding quoting the start of its
  text through `_passage` (secrets redacted, then cut), partial when the read
  was capped (`source.free_http_capped`).
- Page text only: extracting a JSON field is a follow-up.
- The shared `web_fetch` path (built-in http sources, web-search leads, the
  web_fetch tool) is pinned by the same module.

## PR 5 design (the assistant panel)

- `POST /investigation-packs/assist` (behind `DEEP_INVESTIGATION`, gated by
  `require_ai_enabled`): the instruction, the draft as edited (possibly not
  yet valid) and the conversation so far (20 turns at most, user and
  assistant only). Nothing is stored. Ten rounds a minute per user (429
  past it): a round is up to four model calls on the caller's budget. A
  draft longer than 20,000 characters as JSON is a 422 before any model
  call, never cut into JSON the model is asked to keep.
- `services/investigation_pack_assistant.py`: one round on the caller's model
  (`get_llm_for_user`). The model is told the pack's JSON shape, the builder
  catalog, and the corpora and designated MCP tools THIS caller can see
  (`caller_resources`), and answers `{reply, pack}`. The pack is validated by
  `PackFields`, the model a save goes through; a refused pack goes back once
  with the reasons, then the round fails with a generic 502 (the reasons are
  logged). What it returns is therefore a pack a save accepts. A model call
  that raises (timeout, provider rate limit, auth failure) is a generic 502
  too, the cause logged; no provider the caller may use is a 403 with the
  provider rule's own sentence.
- The panel (`pack-builder/PackAssistant.tsx`) sits above the preview for
  whoever may edit the pack: the conversation lives in the panel, each answer
  replaces the draft with an undo, and nothing is saved until the person
  saves.
- Not an agent: no tool loop and no turn in a conversation, so it is not
  metered as an agentic execution, like the other "generate with AI" forms.

## Open questions

None beyond the defaults above.
