---

description: "Task list for surfacing the approval gate on the pages it governs (FR-041–FR-048)"

---

# Tasks: Surface the approval gate on the pages it governs

**Input**: `specs/006-per-tool-approval/` — `spec.md` (FR-041–FR-048, and FR-015, FR-020, FR-022–FR-024, FR-039, FR-040a–FR-040b it builds on)

**Why this is a separate file**: these tasks satisfy requirements of the `006-per-tool-approval`
spec, so they are not a new feature and get no new spec. Task IDs continue the sequence (last used:
**T230**, in `tasks-3298.md`).

**Notes that are not reconstructable from the diff**:

* **Additive by default (FR-041).** `settings/PlatformApprovalSection.tsx`, the profile's per-tool list and the agent/assignment switches all stay as they are; these tasks add second doors onto the same state. The one consolidation is the whitelist editor, which Phase 8 moves onto the Tools page under the superset condition FR-051 states — the profile card is NOT a candidate: it is the only place a standing approval for a tool the catalogue cannot name can be revoked, and the only home for the recently-invalidated list.
* **Allow-all already defaults off** — `user.approve_all_tools` is `default=False, server_default="false"` (`backend/app/models/user.py:204`). FR-043's default half needs no change; the sweep is the work.
* **A caller-scoped whitelist was rejected.** Whitelisting a chatbot is the registration's `approval_policy` set to `skip` (FR-047), not a new whitelist layer — membership stays platform-wide and the sole source of truth for what gates.
* **`_build_builtin_tool_catalog` (`backend/app/api/v1/settings.py:5329`) is the only tool catalogue that exists.** MCP, integration and platform-contributed tools are resolved per agent at bind time and have no aggregate listing, which is what T237 has to build — and why FR-045 gives them their own section rather than a row among builtins.
* **The env var is seeded, not applied.** FR-044 copies the `TOOL_APPROVALS_PLATFORM_DEFAULT` contract already documented in `.env.sample`: read once at row creation, then ignored. A variable re-read on boot would undo an administrator's choice on every restart.

## Format: `[ID] [P?] Description`

- **[P]**: Can run in parallel (different files, no dependencies)

---

## Phase 1: The user's own posture (FR-042, FR-043)

**Independent Test**: with runs of your own paused on two tools, switch "Allow all tools" on. The
confirmation is followed by a list of those two tools; tick one and only its runs resume, with the
other reported as still waiting. Dismissing the list leaves the switch on.

- [X] T231 Group the profile HITL controls into a card in `frontend/src/pages/profile/ToolApprovalsRow.tsx`, keeping every control, its wording and its confirmations, per FR-041, FR-042
- [X] T232 Add an approve-only mode to `frontend/src/pages/ApproveByToolDialog.tsx`: no reject and no "approve always" — the switch just granted the durable form, so offering it again would be a second grant, per FR-043
- [X] T233 Scope the by-tool summary and sweep to the caller's **own** runs, in `backend/app/api/v1/assignments.py` beside the `agent_id` scope of FR-040h. Allow-all reaches exactly the runs carrying that user's identity, so an admin switching it on must not answer somebody else's paused runs
- [X] T234 Offer the dialog after the allow-all confirmation in `ToolApprovalsRow.tsx`, own-runs scope, and leave the switch on when it is dismissed — the grant and the sweep are two decisions, per FR-043
- [X] T235 [P] Add the strings to `frontend/src/i18n/lang/{en,fr,es,ja}.json`
- [X] T236 Cover the own-runs scope in `backend/tests/test_approve_by_tool.py`: an admin's sweep in that scope leaves another user's paused run untouched, and a mixed run is still reported as held back

**Checkpoint**: turning allow-all on clears the backlog it made moot, and only for the tools the user ticked.

---

## Phase 2: The platform switch from deployment configuration (FR-044)

**Independent Test**: with no settings row, boot with the variable false — the gate is off and
Settings shows it off. Set the variable back to true and reboot: the stored value still reads off.

- [X] T237 Seed `platform_settings.tool_approvals_enabled` from `TOOL_APPROVALS_ENABLED` when the row is first created, in `backend/app/api/v1/platform_settings_helpers.py:93`, and ignore the variable when a row exists, per FR-044
- [X] T238 [P] Document it in `.env.sample` beside `TOOL_APPROVALS_PLATFORM_DEFAULT`, stating that it is read once and never overrides a stored choice — and in `docs/docs/administration/ai-governance.md`, which the `check-new-env-vars-documented` CI gate requires
- [X] T239 Cover it in `backend/tests/`: seeded at creation, ignored on a second boot, and absent or unparseable leaves the gate on — fail-closed, per FR-023

**Checkpoint**: a deployment can land the feature already off without an administrator's first act being to turn it off.

---

## Phase 3: Tools (FR-045)

**Independent Test**: open the tools list. Each row says whether it gates. Grant "always" on one as a
user: only that row changes for you. As an admin, whitelist one: it stops gating for everyone. An MCP
tool appears in the unclassified section, never among the builtins.

- [X] T240 Add a gate-status catalogue endpoint: builtin (from `_build_builtin_tool_catalog`), custom, and the unclassified sources — MCP servers, integrations, registered platforms — each carrying its whitelist membership, its source layer, and whether the caller holds a standing approval
- [X] T241 Per-tool indicator and the two controls on `frontend/src/pages/CustomTools.tsx`: "always approve" for the caller (`useToolApprovals`) and whitelist for an admin (`useHitlWhitelist`), kept visibly distinct — different reach, different owner, per FR-045
- [X] T242 Add the unclassified section below the list, explaining why these can never be on the shipped whitelist, per FR-045
- [X] T243 [P] Add the strings to `frontend/src/i18n/lang/{en,fr,es,ja}.json`
- [X] T244 Cover the catalogue endpoint in `backend/tests/`: every source layer reported, membership reflected, and a non-admin seeing their own standing approvals rather than everyone's

**Deviation (T241)**: a user can self-approve a builtin or a custom tool only. An MCP or
integration tool has no definition until its server is bound, and a standing approval is
fingerprinted against the schema so a parameter change withdraws consent (FR-019a) — granting
one without a schema would promise "this will not ask again" and then ask again on the first
real call. Those rows keep the indicator and the admin whitelist, and the API refuses the grant
with that reason.

**Checkpoint**: an operator can tell from the tools list what pauses, and act on it at the right level.

---

## Phase 4: Agents (FR-046)

**Independent Test**: with one gated and one ungated agent, the cards differ at a glance and the
recap reads 1. Opening it lists the gated agent and links to the profile allow-all.

- [X] T245 Gate picto on the card in `frontend/src/pages/Agents.tsx` — the effective `approve_tool_calls` (the agent's own, else the install default, `effectiveApproval` in `frontend/src/lib/hitl.ts`), per FR-046
- [X] T246 Recap control carrying the gated count, opening the list of gated agents with a link to the profile allow-all (FR-024) — the one answer to "why is everything asking me?", per FR-046
- [X] T247 [P] Add the strings to `frontend/src/i18n/lang/{en,fr,es,ja}.json`

**Checkpoint**: the fleet's gate posture is readable from the list, and the way out is one click from the count.

---

## Phase 5: Chatbot and integrations (FR-047, FR-048)

**Independent Test**: a registered platform that cannot ask for approval reads as such, with its
policy beside it and changeable there. An integration's tool gating flips from the integration's own
page and the change is audited exactly as the settings screen's is.

- [X] T248 Platform gate indicator on `frontend/src/pages/Integrations.tsx` and the integration detail header, reporting both halves — `supports_approval_prompts` (what the platform reports) and `approval_policy` (what an operator set) — reusing `usePlatformRegistrations`, per FR-047
- [X] T249 Change that policy in place with `useSetPlatformApproval`: this is what whitelisting a chatbot means, and the confirmation must say it stops the platform's tools pausing for everyone, per FR-047
- [X] T250 Change an integration's `requires_approval` in place from `frontend/src/pages/integration-detail/ConfigurationCard.tsx`, through the existing `PATCH` path so the audit entry is identical, per FR-048 — **already present** (the card's edit mode already writes this field); what was missing was the platform half beside it, which T249 adds
- [X] T251 [P] Add the strings to `frontend/src/i18n/lang/{en,fr,es,ja}.json`

**Checkpoint**: the gate is decided where the connection is managed, not two screens away.

---


---

## Phase 6: The gate on the pages a tool owns (FR-045)

**Why this is here**: Phase 3 answered "what pauses?" for the deployment; a reader who opens one tool
still had to go back to the list to ask it about that tool.

**Independent Test**: open a builtin tool's page — the header says how many of its tools ask first and
the card names each one. Open a custom tool's page — the same card, one row. Edit an agent's built-in
tools — each row says whether picking it means the agent stops to ask.

- [X] T252 Extract the gate row and its wording (`ToolGateRow.tsx`, `useHitlWords.ts`) so the list and the tool pages render one row, not two that can drift
- [X] T253 Add `ToolGateCard` and mount it on the builtin tool page (every name in its group, plus the aggregate in the header) and on the custom tool page, per FR-045
- [X] T254 Show the aggregate on each row of the agent configuration's built-in tool picker, so the gate is visible where the tool is chosen, per FR-045
- [X] T255 [P] Add the strings to `frontend/src/i18n/lang/{en,fr,es,ja}.json`

- [X] T256 Scope the catalogue to what the caller may see — custom tools through `user_visible_query`, and the history harvest to their own and company-managed runs. A flat `SELECT name FROM custom_tools` named other people's private tools to everyone
- [X] T257 Attribute the unclassified half: MCP server (from `discovered_tools`), integration provider (from `build_provider_tools`), or neither, and group the section by that — the answer to "can I do anything about this?" differs per type
- [X] T258 One picto for the gate everywhere, tooltip included by construction (`components/ui/hitl-icon.tsx`, `lib/hitl.ts`), replacing the orange `HandMetal` in the agent and assignment config; and a resolved picto on the agent page's assignment list, where null means inherit rather than unknown
- [X] T259 Offer "Re-gate" whatever the exemption's layer, and confirm the un-gating direction as the Settings screen does — the first was disabled on a wrong assumption, the second fired with no warning at all

- [X] T260 Harden the read/write classifier before widening its reach: a conjunction naming a write beats the leading read verb, so `get_or_create_entity` and `search_and_delete` stop classifying as reads. Deliberately narrower than "any write token anywhere", which would re-gate genuine reads where the token is a noun
- [X] T261 Derive the exempt reads of the integrations and MCP servers connected here, as the platform half already does (`hitl_whitelist_derived.py`), and list them on the whitelist screen under their own heading. Channel bots are excluded: reading a channel is reading people's conversations, which is not the classifier's decision
- [X] T262 Give a channel bot its own `requires_approval`, the counterpart of the integration's — keyed by bot rather than by provider, since two Slack bots can disagree. Bot tool definitions are stamped `channel_bot:<id>` so the gate's exempt-source mechanism can address them, which also gives them the attribution they never had
- [X] T263 Point the no-approval-channel refusal at the reader's own way out: since T241 a user can allow a tool for themselves from the Tools page, so naming only the administrator's screen described the lever they cannot pull

**Checkpoint**: the gate is legible from the tool, not only from the list of tools.

---

## Phase 7: One rule for what is exempt, and a switch per bot (FR-049, FR-050)

**Why this is here**: `search_opencti` ran without asking while `search_notion` paused, for no reason
anybody could state except which registry defined the tool. And a bot contributed tools that send,
edit and delete messages in somebody's Slack with no per-connection switch at all — while the surface
it runs on cannot collect an approval, so a gated tool asked for there was unavailable rather than
delayed.

**Independent Test**: connect a provider and confirm its reads stop asking while its writes do not,
and that a tool an administrator re-gated stays gated. Configure two bots of one provider, switch off
the one an agent is bound to, and confirm that agent's conversations run uninterrupted while the
other bot's still pause.

- [X] T264 Derive the exempt reads of connected integrations and company-managed MCP servers (`hitl_whitelist_derived.py`), per provider so a failed introspection is not memoised as an empty one, and memoised per session rather than per process — a global cache would keep a just-connected provider gating, per FR-049, FR-049b
- [X] T265 Keep the three things that outrank the classifier: `HAND_GATED`, an administrator's re-gate, and channel bots entirely. The first was reversed by the widening on every deployment with an OpenCTI integration, silently — the tool classifies as a read and is one, of an arbitrary document pulled into the model's context, per FR-049a
- [X] T266 List every derived entry under its own heading, attributed to what contributes it, and keep a re-gated one out of "shipped, re-gated here" — the whole point is that nobody chose these individually, per FR-049c
- [X] T267 Stamp a bot's tools `channel_bot:<provider>:<id>`: the gate keys its per-bot switch on the id, while the activity trail, the loop's dynamic-tool accounting and the evaluation engine all read the provider — the id alone dropped bot calls out of all three, per FR-050
- [X] T268 Resolve the governing bot from `channel_config[<channel>].channel_bot_id` rather than from the provider-wide pick, which is arbitrary among several connected bots, per FR-050a
- [X] T269 Record `auto_run_bot_uninterrupted` for a call let through by a bot, carrying the reason with the decision through one `hitl_off_outcome` parameter rather than beside it as a bool, per FR-050b
- [X] T270 Fold the per-connection switches and the platform switch into the catalogue's `gates`, so the tools list stops disagreeing with a switch that works at the gate, and disable the per-tool controls while approvals are off platform-wide — the convention Settings already follows

**Checkpoint**: what is exempt follows one rule, and a bot can be trusted without trusting every bot.

## Phase 8: One place to rule on a tool (FR-051 – FR-056)

**Goal**: the gate is legible and changeable from where each tool already appears, and every surface
says whose answer it is showing.

**Manual test**: with "Allow all tools" on, open Tools → Approvals and the Agents list — both must
say nothing pauses for you while still reporting what pauses for everyone else. Turn it off, approve
one custom tool, and check the group header counts three of four rather than four of four. Then edit
that tool's parameters and confirm the editor names how many other people the change reaches.

- [X] T271 Split the tools page into built-in, custom and approvals tabs, keyed on a query parameter — `/tools/:id/:tab?` already owns the path, which is what makes `/tools/new` work — and resolve the requested tab against the tabs that exist, so `?tab=custom` on a deployment with custom tools disabled cannot highlight nothing above an empty body, per FR-051
- [X] T272 Move the whitelist editor's whole surface onto that tab and retire the Settings copy: add-by-name with suggestions and the consequential-verb escalation, per-platform bulk re-gate, a re-gated shipped default identified and restorable, and the refusal of every edit while a platform registration is unreadable, per FR-051
- [X] T273 Report `regated` and `hand_gated` from the catalogue, the first gated on current default membership — a removal outlives the membership that justified it, and offering to "restore the shipped default" then pins an `add` instead, per FR-051, FR-054
- [X] T274 List an administrator's own additions in the catalogue even when nothing here provides the name: whitelisting ahead of connecting an integration is supported, and a typo is the other way to get here — both are invisible without a row, per FR-051
- [X] T275 Add an approval column to the integration and channel-bot tool tables, answering per connection and per bot, linking to the control with `?gate=<tool>` rather than hosting it, per FR-052
- [X] T276 Count for the reader wherever a surface speaks to them — the approvals group headers, `useToolGateSummary`'s badges and `ToolGateCard`'s description — folding in their approvals, their allow-all and the platform switch; and say so in the wording, per FR-053
- [X] T277 Say that allow-all suspends the agents' gate, above the list and in each picto's tooltip, and stop the gated-agents recap offering the profile switch the reader has already flipped, per FR-053
- [X] T278 Explain the three self-contradicting states in the surface's own words: a hand-gated read, a re-gated shipped default, and a bot's read tool, per FR-054
- [X] T279 Add `GET /tool-approvals/holders`, counting the OTHER people whose live approval still describes the tool, and name that count in the parameters editor. Invalidation is lazy, so an approval stale since an earlier edit is un-revoked and counting it promises to surprise somebody already being re-asked, per FR-055
- [X] T280 Give a custom tool's own page its gate state and controls, and say on a shared one that the platform control reaches everyone who can use it — but keep both off the create form: authoring needs no administrator and exempting does, per FR-045, FR-055
- [X] T281 Filter the approvals list to what the reader already allows, with the count on the control, and report the approvals whose tool the catalogue cannot list rather than dropping them, per FR-056
- [X] T282 [P] Add the strings to `frontend/src/i18n/lang/{en,fr,es,ja}.json`

**Checkpoint**: one screen to rule on a tool, and no surface claiming an answer that is not the
reader's.

## Dependencies

Phase 1's T232 precedes T234; T233 precedes both T234 and T236. Phase 3's T240 precedes T241 and
T242. Phase 2, Phase 4 and Phase 5 are independent of the rest and of each other. Every `[P]`
string task follows the code that introduces its strings. In Phase 8, T271 precedes T272 (the tab
has to exist before the editor moves onto it) and T275 (the deep link lands on that tab); T273
precedes T278; T279 precedes T280's warning.
