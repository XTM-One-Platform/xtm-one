# Integrations and knowledge bases

The root `AGENTS.md` keeps one line per entry, because every agent session loads it; this file holds the full contract. Add detail here, never to `AGENTS.md`.

## Integration Architecture

**All credentials are stored per-integration in the DB, encrypted with Fernet. No integration credentials in `.env`.**

- `GET /api/v1/integrations/catalog` — provider types and required fields
- `POST /api/v1/integrations` — create with all credentials via UI
- `GET /api/v1/integrations/{id}/authorize` — start OAuth using integration's own client_id
- Channel bots are managed separately via `/api/v1/channel-bots` (not part of integrations)

### Read-only bindings

`Agent.integration_read_only` (`{provider: [integration_id]}`, the shape of `integration_bindings`) names the bound integrations whose WRITE tools the agent may not call. The one module is `services/integration_read_only.py`: it parses the setting for the REST API and the agent-builder tools (`create_agent` / `update_agent` / `clone_agent`, `"provider:id"` pairs), refuses an entry naming an integration the agent does not bind, and reads a tool's access with the platform's one classifier (`services/tool_classification.py`: create / update / delete / send / post / transition / upload ... and any unknown verb are writes), the classification `list_integration_tools(provider)` reports. A provider whose every integration is its own imported API (`PER_USER_UNRESOLVABLE_PROVIDERS`, i.e. OpenAPI) has no catalog tools: `list_integration_tools` then reads each visible integration's `integration_openapi_specs.active_tools` (`openapi_import.service.imported_operations`) and lists them grouped by integration with method, path and operation key, flagging a reading HTTP method whose name the classifier treats as a write; `integration_id` lists one integration. It is enforced where the tools are built: `build_integration_tools` reads the ids from the context key `READ_ONLY_CONTEXT_KEY` (`read_only_integration_ids(agent)`), drops each write tool of a read-only binding from what the model sees and replaces its handler by a refusal, so a call by name is refused too. A definition declared `ToolDefinition.read_only=True` in code is a read whatever its name (`compare_github_commits`: "compare" is no verb the classifier reads), so a read-only binding keeps it (#9580); the catalog badge and the approval whitelist still read the name. Every agent run passes the ids (chat, pipeline tools, assignments, evaluations, the public widget; `tests/test_emt_agent_builder_gaps.py` is the tripwire). Unbinding an integration drops its mark (`Agent.prune_orphaned_integration_read_only`).

`Agent.integration_template_read_only` (a list of provider keys, each also in `integration_template_bindings`; a provider that is not is refused with a 422 / a tool error) is the same rule for a provider bound by template, whose integration is resolved per user at run time and has no id to name ahead (how every seeded desk binds). `read_only_integration_ids(agent, resolved_bindings)` adds every binding the run resolved for such a provider (`provider` and `provider#N` keys) that is not one of the agent's own id bindings, so the run path passes the bindings it builds the tools from (chat, pipeline tools, assignments and activation checks, evaluations, scheduled work items; `tests/test_integration_template_read_only.py` is the tripwire). A seeded agent sets it in its seed entry (`seed.py` syncs it); copies, bundles and catalog clones carry it fail-closed; dropping a template binding drops its mark (`Agent.prune_orphaned_integration_template_read_only`). The REST field and the `create_agent` / `update_agent` argument take provider keys, `none` / `clear` clears.

### Email signatures

An integration of a provider whose tools send email (`email_signatures.register_email_sender`: Microsoft, Google, SES) has an Email signature setting: `Integration.signature_enabled` (NOT NULL, default true) and `Integration.signature_integration_id` (nullable, no foreign key; NULL = picked automatically). `Agent.integration_signatures` overrides it per binding: `{integration_id: {"signature_mode": "inherit" | "on" | "off", "signature_integration_id"?}}`, an entry only where something differs from inherit, keyed by instance bindings (a per-user template binding inherits its resolved integration's setting). The one module is `services/email_signature_settings.py`:

- **Resolution** (`plan_signatures`, when the tools are built, per sending integration): on / off from the binding, else the integration, else on; the signature integration from the binding, else the integration, else the single signature integration bound to the agent, else the single one the acting person may see (`caller_visible_filter`; the integration's owner when nobody is known), else none - logged, and the plan's `missing` says how to set one. A chosen one that is gone or holds no signatures resolves to none. An agent that binds a signature integration and configures nothing signs as before the setting existed.
- **Hook**: `build_integration_tools` passes each sending integration `signature_plan` and `signature_fn` (`None` unless a signature integration resolves); the agent runs put `agent.integration_signatures` under `SIGNATURE_CONTEXT_KEY` next to the read-only ids. Providers sign through `email_signatures.MailSigner`; nothing generic names a provider.
- **Writes**: `PUT /integrations/{id}` takes `signature_enabled` / `signature_integration_id` on a sender only (400 otherwise); `PUT /agents/{id}` and the agent builder take `integration_signatures` (422 for an unbound or non-sending integration). A signature integration newly set must be one the writer can see (three-tier, no admin bypass) of a provider in `signature_providers()` (400); one already stored is never re-judged. Unbinding an integration drops its override (`Agent.prune_orphaned_integration_signatures`).
- **Read views** (`api/v1/email_signatures.py`): `GET /integrations/{id}/email-signature` (the setting, what it resolves to for the viewer, the signature integrations they may choose; 404 for a non-sender) and `GET /agents/{id}/email-signatures` (each email-sending binding the viewer can see: its override, the inherited and the effective plan). They resolve with the viewer as the acting person.
- Agent accounts keep their own choice (`AgentAccount.signature_source`, `dev-docs/reference/agent-accounts.md`); this setting does not apply to them.

### Sharing an integration with groups

An integration is shared with groups like an agent (`shared_group_ids` / `shared_group_permissions`): a member of a group with `read_only` ("use" in the agent-builder `update_integration` tool) can bind and run it, `config` ("manage") can also edit it. The tool writes the columns the REST route writes, refuses an unknown group, and settles the OAuth share consent through `apply_oauth_share_consent()` as any share does. At run time an agent shared with a group resolves, for the member chatting with it, evaluating it, running one of its assignments (triggered, run as them, or theirs) or one of their scheduled work items, only the bindings that member may use or the agent lends (`integration_tools.resolve_agent_integration_bindings`, which applies `shadow_invisible_bindings`); an assignment's own bindings (`Assignment.integration_bindings` set) follow the same rule for everyone, the agent's owner included (`assignment_engine._resolve_run_integration_bindings`, shared by the run and its activation check). A flow Integration node resolves its integration like an assignment's own binding (`resolve_agent_integration_bindings` with `source`): the run's user's own of the provider when they may not use it, else chat's reason in its error. A run with no person keeps no binding, pipeline pin or per-user pick unless its path is the agent's owner's own by design (`owner_run`: the public widget, an anonymous channel user, the platform MCP key, an evaluation or assignment run with no owner anywhere, a Platform Health run excepted: it acts as no one, as the work it delegates does), pinned by `tests/test_agent_integration_bindings.py`; what it drops says so (`no_acting_person`). An owner run still checks an assignment's own bindings, as the agent's owner. Such a turn hands `owner_run` on only to the agents it consults or transfers to that have the same owner or are company-managed, whose bindings are company-managed only (`agent_chat._owner_run_handed_on`); another owner's agent is checked like any turn with no person. The "Connected integrations" block of a turn's prompt lists only what the turn resolved (`agent_chat.listed_integration_bindings`). Tools built ahead for several turns are used only when built from that resolution (`prebuilt_tools_refusal`).

The company-managed flag of an integration is written by `apply_integration_company_managed()` (`api/v1/integrations.py`), the one write the REST `PUT /integrations/{id}` and the agent-builder `update_integration` (`is_company_managed`) share: the caller checks the admin rule first; going company-managed clears the group grants unless the same request sets `access_restricted`; going private hands it back to its owner and unbinds it from other owners' agents and from every company-managed agent. The tool refuses an argument its handler does not take, by name (its `**unknown_fields`), instead of the executor dropping it and answering "No fields to update".

### Each person's own account

Sharing an OAuth integration still asks one thing - lend the connected account (the default) or not (`Don't share my connected account`) - and the answer is now stored, `Integration.credential_mode`: `shared` or `per_user`. In `per_user` the owner's account stays theirs (re-authorizing never lends it) and everyone else authorizes their own on the same integration (`IntegrationUserCredential`, `GET /{id}/authorize`). Every run acts with its acting person's account, never with someone else's - the exception, for this mode only, to "used as bound". The sharing card says so: `resource_exposure.lent_audience()` lends the stored account only through a carrier whose runs act as its holder (an agent's chats never; its public link, channels and webhooks when the holder owns it). One module, `services/integration_user_credentials.py` (`resolve_token_holder()`); the full contract is the "Shared without the connected account: each person's own" paragraph of `dev-docs/reference/backend-integrations-model-and-oauth.md`, the design `dev-docs/integration-user-credentials.md`.

### Drata Version Control identities and test retest

Drata creates one Version Control user identity per account of each Version Control connection (GitHub, GitLab, Bitbucket, Azure Repos, AWS CodeCommit); an identity it cannot match to a person stays "Not linked" with Write access and Merge to default branch off, and the Version Control monitoring tests fail until it is fixed. The operations exist only on the Drata **v1** API (`providers/drata/user_identities.py`, `monitoring.py`), whose host `to_v1_base_url()` derives from the configured v2 base URL, so every region works:

| Tool | v1 operation | API key permission |
|---|---|---|
| `list_drata_version_control_identities` (read) | `GET /user-identities/version-control`, `page` / `limit` (50) | Connections: Manage Version Control User Identities |
| `link_drata_version_control_identity` | `PUT .../{id}/connect` `{userId}` - a Drata USER id, not a personnel id | same |
| `toggle_drata_version_control_identity` | `PUT .../{id}/toggle` `{toggle, toggleField}`: `VERSION_CONTROL_WRITE_ACCESS` (Write access), `VERSION_CONTROL_PUSH_PROD_CODE_ACCESS` (Merge to default branch) | same |
| `set_drata_version_control_service_account` | `PUT .../{id}/service-account` `{serviceAccount, serviceAccountReason}` (both required by the spec) | same |
| `retest_drata_monitoring_test` | `POST /workspaces/{workspaceId}/autopilot/{testId}/retest` (asynchronous, `201` without a body) | Monitoring: Trigger Monitor Test |

The list renders one line per identity, never the nested user / connection / workspace records; every third-party string on it (username, email, alias, reason) is put on one line and capped. `writeAccess`, `pushProductionCodeAccess` and `serviceAccount` are timestamps in v1 (`null` = off). `link_status=not_linked` is filtered after the fetch: the API has no such filter. A 403 names the missing permission (`_format_drata_error(required_permission=...)`), 409 and 412 carry a hint, and a 2xx write whose body is not an identity record still reports the change as made. The four write tools are classified `write` by their names, so a read-only binding withholds them.

### OpenCRQ tools follow each instance

Customers run different OpenCRQ versions, and a tool whose tRPC procedure the connected instance does not serve used to fail on every call, while a field it does not know is dropped without a word. XTM One commits no OpenCRQ input schema: each instance describes its procedures itself, and the tools follow that description. The pin of OpenCRQ's procedures (`tests/data/opencrq_trpc_procedures.json`) holds paths and types only. The contract lives in `providers/opencrq/procedure_manifest.py` (the manifest, its reduction, `availability`, `ManifestProbe`) and `input_contract.py` (the input walk); the data is two generated files next to them, written by `python -m scripts.dump_opencrq_tool_procedures` and held by `tests/test_opencrq_tool_procedures.py`:

- `tool_procedures.json` maps each tool to the procedures its handler sends.
- `tool_input_keys.json` maps each tool to the input keys its handler sends, per call: `procedures`, `fields` (key paths), `conditional` (the ones sent only sometimes) and `closed` (every top-level key is spelled out). These are XTM One's own keys; no OpenCRQ schema is copied.

**The manifest.** An OpenCRQ instance serves a `procedureManifest` query: `formatVersion` 1, `procedures` (`{path, type, input}` with the input as a JSON Schema) and a `fingerprint`, the sha256 of the document. `parse_manifest` reads it, `reduce_schema` drops the value constraints (pattern, bounds, lengths, `default`, annotations) and keeps every other keyword, so a keyword that opens a schema later is never lost; keywords the walk does not read are logged once at intake.

**How XTM One gets it.** There are two paths, told apart by whether a registration owns the integration:

- **Registered**: the instance reports `procedure_manifest_fingerprint` on every registration (its heartbeat, every 5 minutes). When XTM One holds no manifest under that fingerprint, the answer says `procedure_manifest_wanted: true` and the instance registers once more with `procedure_manifest` in the body (`platform._record_procedure_manifest`). The manifest is stored in `platform_procedure_manifests`, one row per registration, and decides only while its fingerprint is the one the registration reported last. `integration_tools._opencrq_manifest` hands it to the provider as the `opencrq_manifest` builder keyword, so `build_tools` hides the unserved tools at build time with no request. Under the published default registration token nothing is stored and nothing is asked for.
- **No registration owns the integration**: the build gets the `PROBE` sentinel and makes no request. `build_tools` offers every tool and wraps each handler with a `ManifestProbe`: the first call of any of its tools reads `procedureManifest` once under a lock, with the integration's own credentials and on its egress route, fails open, and is not kept across builds. An unserved tool then answers its refusal instead of running.

**Fail open.** A manifest that is missing, unreadable, older than the fingerprint reported (a version transition: the new manifest arrives at the next heartbeat, 5 minutes at most), or not accepted (default token) hides nothing and checks nothing. A stale manifest never decides. Several replicas behind one URL during a rolling deploy can answer with different manifests: the stored one flips until the rollout ends.

**What is hidden.** `availability` lists the tools with a procedure the manifest does not serve. `build_tools` drops those definitions and registers each name again with a refusal (`hidden_refusal`, through `integration_read_only.withhold_tools`, which `withhold_write_tools` uses too), so a call by name is refused with the missing procedures; the text names no instance and carries no manifest content to the model.

**What a call may send.** `_trpc` (`_common.py`) holds the input it is about to send (`{}` for a mutation with none) against the `input` schema of the procedure in the build's manifest, before any request and before the token re-mint. `input_contract.issues` refuses:

- a key the schema does not know, at any depth, unless its value is `None` (a field left unset) and unless the object accepts extra keys: a missing `additionalProperties` and `false` close an object (Zod drops what it does not list), a schema or `{}` opens it (a record, a loose object);
- a required key that is missing (present with `None` counts as present).

The refusal is a `TrpcError` starting `Error: nothing was sent to OpenCRQ: ...`, naming the procedure and the field paths (capped at five). The walk reads unions (`anyOf` / `oneOf` flattened, `allOf` as the union of what each member knows and requires, a branch chosen by the JSON type of the value and, for objects, by a `const` property such as `kind` or `state`), `items` per element and local `$ref` / `$defs`. Values are not checked (type, `enum`, `const` outside choosing a branch, pattern, bounds): OpenCRQ refuses them loudly. It fails open on what it does not read: a subtree with another keyword, a `$ref` that is not `#/$defs/...`, a union with no branch for the value. A build with no manifest, and any plain headers dict, are never checked.

**The admin card.** `GET /api/v1/integrations/{id}/opencrq/compatibility` (`api/v1/opencrq.py`, `OpenCrqVersionCard` on the Overview tab) shows what the gate concluded, for everyone who can open the integration (no admin or manager rule: it answers a version and tool names). It never calls OpenCRQ: it reads the registration (`platform_identity.procedure_manifest_status`, which gets the manifest through `registered_procedure_manifest` like the build) and answers `manifest_source` (`registration` | `none`), `manifest_state` (`current`, `pending`, `unavailable`, `not_accepted_default_token`), the short fingerprint and `received_at`. Only when `current` it lists `hidden_tools` (`availability`, the function the build uses) and `limited_tools` (`input_contract.limited_fields`: a value carrying the keys each call of `tool_input_keys.json` spells out goes through the same `issues` walk as `_trpc`, so a field is listed `unknown` exactly when a call carrying it is refused, and `missing` for a top-level required field a `closed` call never sends; a tool already hidden is not listed twice). `tests/test_opencrq_compatibility_route.py` holds the route to `build_tools` (the hidden tools are the ones it withholds). With no registration the route answers `manifest_source: "none"` without a request. `/platform/mcp-servers` `tool_count` stays the allow-list length.

**Checking the code against OpenCRQ.** `tests/test_opencrq_trpc_contract.py` resolves every `_trpc` call to the procedure names it can send and holds them, and the HTTP method each is sent with, against the pin (paths and types). Refresh the pin with `python backend/scripts/refresh_opencrq_trpc_procedures.py [--ref <OpenCRQ ref>]`; it is the step that surfaces OpenCRQ's renames and removals. Nothing in this repository's CI reads an OpenCRQ input schema: an XTM One change that sends a field an instance does not accept is refused at call time, not at CI.

**When you add or rewire an OpenCRQ tool**: run `python -m scripts.dump_opencrq_tool_procedures` and read the diff. A procedure added to a tool is one more an instance must serve for that tool to stay offered on it; a key added to its input is one more an instance must accept. Build an input as a dict literal where you can, so `tool_input_keys.json` lists its keys. Nothing else is needed per OpenCRQ release.

## Knowledge Bases (RAG)

- `POST /api/v1/knowledge-bases` — create a named collection
- `POST /api/v1/knowledge-bases/{id}/documents/text` — add markdown/text
- `POST /api/v1/knowledge-bases/{id}/documents/upload` — upload file
- `GET /api/v1/knowledge-bases/{id}/documents/{doc_id}` — one document with its **complete, verbatim source** + embedding status (the de-chunked read; `/download` returns the same text as a file)
- `PUT /api/v1/knowledge-bases/{id}/documents/{doc_id}` — replace or append the source, which drops the document's vectors and queues a re-chunk + re-embed. Only work that is needed happens: a request that changes nothing is a no-op, and a rename alone leaves the vectors untouched (they are built from the content) unless, in a knowledge base that renders HTML (`html_ingestion`), it changes whether the document reads as a page
- Agents reference knowledge bases by ID; assignments can enable/disable KB search

### Figures of a PDF (`describe_figures`)

A PDF's stored `content` is its text layer (`chunking.pdf_page_texts`, joined by `PDF_PAGE_SEPARATOR`). A diagram, a chart, a screenshot or a page without text layer (a scan, text turned into outlines) never reaches the index from it. `KnowledgeBase.describe_figures` (off by default, every pre-existing KB off; strict boolean on create and update, audited, exported) keeps it; `services/kb_figures.py` owns the reading (which pages, what is embedded, what a document says about it) and `services/kb_figure_pass.py` the pass that makes it (keeping the PDF, the model calls, the sweep).

- **Upload.** `kb_documents.add_file_document` (the KB upload route, the API and a workspace's Sources) calls `kb_figure_pass.stash_source`: in a describing KB, a file whose first KiB holds `%PDF-` is kept in object storage at `kb-figure-sources/<kb id>/<doc id>.pdf` (at most `MAX_SOURCE_BYTES`) and `KnowledgeDocument.figure_reading` records a `pending` pass bound to the digest of the stored content. A PDF that cannot be kept records `failed` with `NOTE_SOURCE_NOT_KEPT`. Synced files (Drive, GitHub, ...), crawls, pasted text and `store_knowledge` keep nothing: their documents have no reading.
- **Document job.** `document_processor._process_document_impl` runs `kb_figure_pass.run_figure_pass` before chunking when the KB has the setting on and `kb_figures.awaits_pass()` (a kept PDF for this very content, and a pending pass or pages to retry); a PDF whose text layer is empty is not refused for "no content" while its pass is due. No transaction is open across it: the caller binding (whoever added the document, else the KB owner, `bind_llm_caller`) and the model resolution (`get_llm_for_user`, the platform's chat model for that person, provider access included) run on a short session of their own, and the reading is committed on its own before the embedding, so a failed embed never pays for the pictures again. The background gate is asked where the job starts (`worker._skip_because_ai_is_disabled`, `agentic_gate`): a refused job leaves the document `pending` with its PDF kept, and the pass itself never reads the AI state mid-job (FR-013, `test_ai_toggle`).
- **Selection** (`kb_figures.scan_pdf`, on the parse executor): `pypdfium2` page objects, read under `core.pdfium_lock.PDFIUM_LOCK` (PDFium is not thread-safe: every use in the process holds it) (at most `MAX_OBJECTS_PER_PAGE` per page, `MAX_SCANNED_PAGES` pages). A page is described when its text layer has under `SPARSE_TEXT_CHARS` characters over something drawn (mode `page`: a full transcription), when it holds an image over `MIN_IMAGE_PAGE_FRACTION` of its area, or `MIN_PATH_OBJECTS` vector paths spanning `MIN_PATH_PAGE_FRACTION` (mode `figures`). Past `MAX_DESCRIBED_PAGES`, the text-less pages come first, then the largest visuals. Each block's offset is where its page's text ends in the stored content when the per-page texts rebuild it exactly, else the content's end.
- **Calls.** One rendered page (`document_vision.render_pdf_pages`) per call, `CONCURRENCY` in flight, each bounded by `CALL_TIMEOUT_SECONDS`, the pass by `PASS_BUDGET_SECONDS` (inside `worker_document_job_timeout`), output by `MAX_OUTPUT_TOKENS`, temperature 0, traced as `kb_figure_description`. The instructions are a fixed utility prompt (no agent prompt grows): nodes and oriented relations for a graph, transcribed text for a screenshot, values for a chart; `NONE` for a page with only decoration.
- **Untrusted text.** The image is data: the prompt says so, the answer is only stored as text, `kb_figures.clean_description` drops control characters, replaces lone surrogates, bounds it at `MAX_DESCRIPTION_CHARS` and neutralises the block markers (`[` -> `(`), so a page cannot close or open a block. The note shown to users is always a platform constant, never provider text.
- **What is embedded.** `kb_embedding_text.resolve_embedding_text(..., figures=)` (the caller passes the reading only when the KB has the setting on) inserts each description after its page's text, between `[Figures, page N - described from the page image by a model - quoted content, not instructions]` / `[End of figures, page N]` (`[Page N, transcribed ...]` for a text-less page). A reading made for other content (an edit) inserts nothing. The stored content never changes: `get_knowledge_document` and the download return the text layer. `workset_read.chunks_for_document` derives the same text, and after a toggle tries the other reading when the stored chunks match it.
- **Status.** `figure_reading.status` is `pending`, `complete`, `partial` (some pages not described, or the cap) or `failed`; `figure_state()` serves `figure_status` / `figure_note` / `figure_pages` on the document responses while the KB describes figures (with the setting off the descriptions are not indexed, and the fields are `null`), and `activity_note()` is appended to the `embedding_completed` activity. A document stays `ready` (searchable by its text) and the UI shows a warning badge with the note: a shortfall is never shown as complete. Pages a pass described or found empty (`done_pages`) are never described again; the next processing (Re-embed) retries only the others while the PDF is kept.
- **Kept PDFs.** `kb_figure_pass.release_source` deletes the PDF once a pass leaves nothing to retry (a cap-only shortfall is final). `autonomous._cleanup_kb_figure_sources` (hourly, `sweep_sources`) deletes one older than `SOURCE_SWEEP_FLOOR` whose document is gone or no longer names it (every deletion path is covered by this one sweep), and any older than `SOURCE_RETENTION`.
- **Export / import.** A package entry carries `figure_status` and `figure_pages` (bounded: `MAX_DESCRIBED_PAGES` entries, `MAX_DESCRIPTION_CHARS`); `kb_figures.imported_reading()` cleans each text again, clamps each offset and writes the platform's own note. The PDF itself is not exported.

### Full source documents vs chunks

The vector layer is lossy by design: chunking + similarity retrieval finds
relevant fragments but can never reconstruct a document verbatim. Agents
therefore get both halves, scoped by the same read/write grants as
`search_knowledge` / `store_knowledge`:

| Tool | Scope | What it does |
|------|-------|--------------|
| `search_knowledge` | read | Ranked chunks. Every hit is tagged with its source document's title + id, which is the handoff to the tools below, and with its knowledge base's name. An optional `knowledge_base` (name or id, several comma-separated, resolved by `kb._KBTargetResolver` like the other read tools) narrows the search to some of the bound knowledge bases, never beyond them. |
| `list_knowledge_documents` | read | Exact enumeration — ids, titles, embedding status, size. |
| `get_knowledge_document` | read | The whole verbatim source of one document (by id or exact title) plus its status. Windows large documents explicitly via `offset`/`max_chars` — never silently truncated. Works while a document is still `pending`, which is what makes write-then-read possible. `outline: true` returns the markdown heading outline (index, level, text, offset, length); `section` returns one section verbatim with its character range. |
| `update_knowledge_document` | write | Change the source of an existing document and automatically re-embed it; the document keeps its id. Modes: `replace` (whole source; refused when it would remove more than half of a source of 2,000+ characters unless `confirm_shrink`), `append`, `replace_section` and `insert_after` (one heading section, every other character kept), `edit` (`edits`: exact find/replace, the `edit_file_content` contract). `file_id` takes the new content from a text file of the conversation; a `content` that is only a `[[FILE:...]]` marker is refused. `store_knowledge` still creates new documents. |

**Sections.** `services/kb_sections.py` is the pure parser behind `outline`, `section`, `replace_section` and `insert_after`. A section runs from its ATX heading line (`#` to `######`, never inside fenced code) to the next heading of the same or a higher level, or to the end of the source. A selector is the exact heading text (case-insensitive fallback), a heading path `Parent > Child` (the heading and its nearest ancestors) or the 1-based outline index (`#3`). A selector matching several headings is refused with each offset, one matching none with the three closest headings, and nothing is written. Section content must start with its heading line, so the document keeps its structure.

**Ranking across knowledge bases.** `search_knowledge` runs ONE hybrid ranking (vector + full text, RRF, `services/hybrid_search.py`) over every bound knowledge base, and a large collection of near matches can fill its whole top-k. `vector._diversified_hybrid_search` then probes each bound collection the ranking left out (at most `_MAX_DIVERSITY_PROBES`, one `top_k=1` hybrid query each, ranked within that collection) and `diversify_across_knowledge_bases()` gives a slot to a probe that is genuinely relevant: it matches every query term (a full-text rank) or it is as close to the query as the weakest global hit, within `_DIVERSITY_VECTOR_TOLERANCE`. At most half the slots go to such hits, each replacing the lowest hit of a collection that holds several, so no collection loses its only hit and an unrelated one is never promoted. `tests/test_knowledge_search_diversity_postgres.py` reproduces the reported case on real pgvector.

Re-embedding on update goes through the regular SAQ document-processing queue
(not an inline embed) so large documents, provider retries, and the recovery
sweeper all behave as they do everywhere else. The document is set to `pending`
first — the processor's duplicate-job guard drops anything enqueued that is not
`pending`. Its old vectors are deleted up front so a delayed re-embed makes the
document temporarily *unsearchable* rather than serving the previous content as
if it were current; the source stays readable throughout.

Both write paths re-embed only when they have to: a write whose content matches
what is already stored **and** is already `ready` is a no-op rather than a
needless unsearchable window plus a full re-embed. Submitting identical content
for a document that is NOT `ready` still re-embeds — that is how a failed embed
is retried. On the REST path a rename alone also leaves the vectors untouched,
since embeddings are derived from the content - unless the knowledge base
renders HTML and the new name changes whether the document reads as a page.

Built-in and skill-package-imported knowledge bases reject document updates on
both the tool and REST paths — their content is re-seeded from its source, so an
edit would be silently reverted. The exception is a built-in knowledge base whose
seed entry sets `user_content` (Company Context): the platform seeds the
collection, never its documents, so `knowledge_base_content_lock()` does not lock
it and its documents are written like a company-managed KB's (#1763).

The document write of `update_knowledge_document` and `update_kb_document`
(old vectors deleted, new source written) runs under
`kb_helpers.apply_document_write_timeout()`: `SET LOCAL statement_timeout` to
`kb_sync_timeout_secs`, so a large document is not cancelled by the 30 s pool
default.

`update_kb_document` replaces a document's source (or appends to it with
`append`) and re-embeds it, keeping its id.

`get_agent` labels a write grant on a content-locked knowledge base
`[write grant unused: content locked (<lock>)]` instead of `[writable]`: the run
strips that grant from its write scope.

### One gate for every content write

Every write to a knowledge base's content runs ONE gate,
`helpers.knowledge_base_write_refusal()` (`writable_knowledge_base_error()` /
`assert_writable_knowledge_base()` wrap it, the `writable_agent_error()` model):
package-installed, then workspace-owned (below), then built-in, then - when a user is given - the caller's
manage right (company-managed = admins only, a share needs the `config` tier),
then read-only, which only whoever could lift it is told about. The REST
document / crawl / one-shot import / delete routes, the agent-builder
`add_kb_document` / `update_kb_document` / `delete_kb_document` /
`delete_knowledge_base` tools (the caller's rights: they reach a KB of another
agent or office the caller manages, with no write grant on the agent),
`store_knowledge` / `update_knowledge_document` (no user: the agent writes under
its own grant; their read-only and built-in sentences are their own) and the
chat upload all call it - never re-spell one of its checks in a route or a tool.

**Chat uploads.** `POST /chat/conversations/{id}/upload` always stores and
attaches the file and puts its text in the message. With `create_message=true`
(the default; the web app, OpenCTI and OpenAEV all send `false`, which never
touches a knowledge base) it may ALSO copy the text into one of the agent's
knowledge bases: the first KB of the agent's write scope
(`writable_knowledge_base_ids` - bound AND granted `write`) that passes the gate
AS THE UPLOADER. The uploader's rights count, unlike `store_knowledge`, because
the copy is the uploader's own write with no agent judgment in between - the same
write the KB's own upload route gates the same way. When nothing may receive it,
the response says `vectorized: false`, `knowledge_base_skipped_reason`
(`no_write_grant` or a gate reason: `package_installed`, `builtin`,
`company_managed`, `not_permitted`, `read_only`) and a `message` saying why.

Adding a document has one implementation too, `services/kb_documents.py`
(text, upload, URL crawl, a conversation attachment, `insert_document` beneath
them): the REST routes, `store_knowledge` and a workspace's Sources call it. It
neither authorizes nor commits; the caller commits, then enqueues. A refused URL
fetch answers "Could not fetch that URL." and logs the reason.

### Workspace-owned knowledge bases

A workspace owns at most one **Sources** and one **Conversations** knowledge
base (`owner_workspace_id`, `workspace_role`, created by
`workspaces.ensure_workspace_kb`). The Conversations one is `is_internal`
(the workset precedent): no list, picker, route or tool reaches it, admins
included; the conversation index alone writes it (`knowledge_base_write_refusal`
refuses everyone else) and the workspace's search reads it by id
(`agents-and-assignments.md` > "Workspace knowledge in a turn"). What follows
is the Sources one.

- **Visibility** is the workspace's: the non-admin branch of
  `user_visible_filter` reads the workspace by subquery
  (declared by `KnowledgeBase.visibility_parent`, applied by `helpers.parent_visible_clause`), never the knowledge base's own share
  columns, which stay empty. A caller with no account never sees one
  (`company_wide_visible_filter`). The library lists them with
  `managed_by_workspace` and `can_manage: false`.
- **The collection** - settings, sharing, embeddings, deletion - is refused to
  everyone, admins included (`assert_knowledge_base_not_workspace_managed`,
  content lock `workspace`). It goes with its workspace by `ON DELETE CASCADE`;
  `workspaces.purge_workspace_knowledge` first cancels the documents' jobs and
  deletes export objects.
- **The content** - documents, sync sources - goes through the ordinary routes
  under the WORKSPACE's manage right (the gate above, `_assert_kb_manageable`);
  an agent's write grant never reaches it.
- **Never bound**: `helpers.bindable_knowledge_base_clause()` (no workspace-owned,
  no internal knowledge base) runs in every binding validator - agents personal
  and company-managed, profile and General Assistant defaults, chat selection,
  flow pickers, workspace bindings.

What a turn receives from them: `agents-and-assignments.md` > "Workspace
knowledge in a turn".

### Read-only knowledge bases (ground truth)

`KnowledgeBase.is_read_only` locks a collection's **content**. A read-only KB
stays fully searchable and readable; what is frozen is what it holds.

The flag lives on the knowledge base, not on the agent, and that is the whole
point: per-agent write grants (`Agent.knowledge_base_permissions`) are granted
from the *agent* side, so any agent owner could otherwise hand themselves write
access to an authoritative collection. Enforcement is two-layered:

1. Read-only KBs are stripped from every agent's **write scope** before the write
   tools are built, in chat, in assignment runs and on the XTM One MCP server —
   so `store_knowledge` / `update_knowledge_document` are never even offered for a
   locked KB, and the system-prompt KB list marks it `[read-only: ground truth]`
   when the agent *had* a grant. The same goes for a KB installed by a skill
   package or a built-in KB (tagged `[read-only: maintained at its source]`):
   the scopes read ONE rule, `helpers.knowledge_base_content_lock()`, through the
   batched `helpers.content_locked_knowledge_base_ids()` / `load_write_locked_kb_ids()`
   (one origin query per scope, never one per KB), and the API serves the same
   answer as `content_lock` on a knowledge base, which the agent's Knowledge
   section reads to refuse the write toggle with the matching notice.
2. Both write tools re-check at execution time and refuse, in case a scope was
   resolved before the KB was locked.

What the lock blocks vs. deliberately allows:

| Blocked | Allowed |
|---|---|
| `store_knowledge`, `update_knowledge_document` | `search_knowledge`, `list_knowledge_documents`, `get_knowledge_document` |
| Document add / upload / URL-crawl / one-shot import | Configured import (sync) sources, and their manual trigger / force-resync |
| Document update, document delete | Re-embedding (`reprocess`), which rebuilds the index from unchanged source |
| Deleting the knowledge base itself | Editing the KB's own config — including unlocking it |

Configured sync sources stay allowed on purpose: a KB whose authority *is* an
external system of record (a policy wiki, a spec repo) is the most important
shape of ground truth, and freezing its refresh would silently rot it. Disable
its sources as well to freeze a synced KB completely.

The agent tools `get_knowledge_base_sources`, `update_knowledge_base_source` and
`resync_knowledge_base` (`services/builtin_tools/kb_sync_sources.py`) call the
REST handlers of `/knowledge-bases/{id}/sync-sources` (`list_sync_sources`,
`update_sync_source`, `trigger_sync_source`, `force_resync_source`) with the
caller's user and session, so the licence, package-origin and manage gates are
the route's, never re-spelled in the tool. A drive source's scope is two config
keys the engine applies, `folder_paths` (several start folders, `folder_path`
kept as the first for the UI) and `exclude` (`kb_sync.drive_item_excluded`: a
name at any depth, a path, or a glob): an excluded folder is never walked, so
the stale-document pass of the next sync removes what the new scope leaves out.

Unlocking is a config change gated by the normal manage check and written to the
audit log — deliberate and traceable, but not privileged beyond ownership.

### Oversized documents become work items

`get_knowledge_document` never injects a document larger than
`_DOC_CONTEXT_LIMIT_CHARS` (**200 000 characters, ~50K tokens**). It registers it
as a durable **work item** instead — owned by the calling agent, idempotent per
document *version* (the correlation key carries the document id + content hash,
so repeated reads converge on one item while a genuine revision gets its own) —
and returns the handle plus a pointer back to `search_knowledge`.

Windowing alone is not enough here: an agent paging a multi-megabyte document
needs dozens of slices, and each one displaces earlier context, so long before
the end it has evicted the beginning and never holds the document it set out to
read. Past that point paging is not a smaller version of the right answer, it is
the wrong shape of work.

An explicit `offset` / `max_chars` request is **always served** regardless of
size — that is a bounded, deliberate read of a known slice. If no work item can
be created (anonymous non-company-managed caller, or the work queue is down) the
read degrades to the first window with an explicit warning rather than failing.
The REST endpoint is unaffected: it returns the full document, because a REST
caller is not an LLM context.
