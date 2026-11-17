# Phase 0 Research: AI-disabled platform mode

Decisions taken before design, each with what was rejected. Where a claim is about
this codebase it was checked rather than assumed, and one check contradicted the
plan's first draft.

## 1. Where the refusal lives

**Decision**: Extend the two existing dependencies — `require_agentic_ai` and
`require_chat_enabled` (`backend/app/api/deps.py:399`, `:420`) — to consult the
resolved AI state, and add a machine-readable code to the body they already
return. Do not introduce a third gating mechanism.

**Rationale**: Those two dependencies are attached at **83 route call sites** across
`api/v1/`. Reading the state inside them covers every one without touching any,
and it means a route that is gated for licensing is automatically gated for this
mode — which is the property FR-010 needs, since an entrypoint that forgot to opt
in is exactly how an execution path stays open. `platform.py:320-345` already
maintains a documented map of feature keys to these gates; extending it keeps one
artefact describing the gating surface.

**Alternatives considered**: A new dependency applied route by route — 83 places to
get right, and the failure mode is silent. A middleware matching URL patterns —
decouples the gate from the thing being gated, so a renamed route silently
un-gates itself.

## 2. Whether adding a code to the 403 body is a breaking change

**Decision**: Additive, and lower risk than the plan's first draft assumed.

**Rationale**: The draft asserted "other clients already parse these 403s" and
treated that as the riskiest part of the backend work. Checked: the frontend does
not parse them. It reads `agentic_ai_enabled` from the platform config query
(`hooks/usePlatformConfig.tsx:198,247,528`) and renders state from that, and the
403 prose is surfaced only as a message. So the established pattern is already
"state comes from config, not from error bodies" — which is also what FR-011 wants
for the pre-authentication case, where there is no request to be refused yet.

**Consequence**: The structured code is worth adding for FR-009, but it is the
*secondary* signal. The primary one is the config flag, and the frontend work
should follow the existing pattern rather than branch on errors. Anything outside
this repository that renders the prose keeps working, because the prose stays.

**Alternatives considered**: Replacing the prose with a code — breaks any consumer
displaying it, for no gain. Only adding the config flag and no code — leaves a
programmatic client unable to distinguish "no licence" from "AI disabled", which
are different remedies.

## 3. How the state is resolved

**Decision**: A nullable `ai_disabled` column on the `platform_settings` singleton.
`NULL` means "follow `PLATFORM_MODE`". One resolver — `services/ai_state.py` — is
the only code that reads `platform_mode` for this purpose; everything else reads
the resolved boolean.

**Rationale**: The tri-state is the only shape in which "no administrator has
chosen" is distinguishable from "an administrator chose to allow AI". Without it,
whatever value is written at first boot pins the platform and a later change to
`PLATFORM_MODE` appears to do nothing — a failure an operator discovers only by
its absence. `platform_name` already uses exactly this convention
(`platform_settings_helpers.py:66`), so the pattern is established rather than
invented. Concentrating the mode read in one module makes FR-004 enforceable by
grep: any other reference to the mode for this purpose is a defect visible in
review.

**Alternatives considered**: A non-null boolean defaulted from the mode — the
shadowing problem above. Deriving purely from the mode with no override — fails
FR-002, and the issue settles the runtime toggle as a requirement. A separate
settings table — a second singleton to keep consistent for one boolean.

## 4. Failure mode when the state cannot be read

**Decision**: Treat AI as **disabled**, except during boot, where the platform must
still start.

**Rationale**: Principle II. Refusing AI when the answer is unknown is recoverable
and visible; assuming it is allowed runs a model on a deployment that may prohibit
it by policy, which is the exact scenario the mode exists for. The exception
matters: the database is legitimately unavailable for part of boot, and a resolver
that refuses to boot rather than refusing AI would turn a transient condition into
an outage.

**Alternatives considered**: Fail open for availability — inverts the principle on
the one feature whose whole purpose is denial. Cache the last known value and
serve it — a stale "enabled" is indistinguishable from a fresh one, so it fails in
the same direction.

## 5. Where the state is consulted for scheduling

**Decision**: Once per tick, in `services/autonomous.py`, where all five AI
schedulers are already dispatched (`:1197-1290`, `:1651`).

**Rationale**: They already converge. `_process_scheduled_assignments`,
`process_scheduled_flows`, `process_scheduled_kb_syncs`,
`process_scheduled_nova_syncs` and `_process_scheduled_evaluations` are invoked
from one loop, so one read gates all of them and no individual scheduler needs to
know this feature exists. Per-tick rather than per-process satisfies FR-002's "no
restart" and SC-003's "within one scheduler interval" without a subscription.

**Alternatives considered**: Gating inside each scheduler — five places, five
chances to miss one, and the miss is a job that runs on an AI-disabled platform.
A pub/sub invalidation — faster propagation than one tick, for a setting changed
by hand at most a few times in a deployment's life.

## 6. Flow gating: one declared property, defaulting to blocked

**Decision**: Add `requires_ai: bool = True` to `NodeKind`
(`services/flow_catalog.py:295`) and have every non-AI node declare
`requires_ai=False`.

**Rationale**: `NodeKind` is a frozen dataclass and the single definition of a
placeable node, so the runner, the trigger path and the editor can all read one
property and cannot disagree (FR-015). The default direction is the substance of
FR-030: a node type added later by someone who has never heard of this feature
must be held back rather than run. Every other field on `NodeKind` defaults to the
permissive value, so this one looks inconsistent on purpose — and the comment must
say why, or someone will "fix" it.

**Cost accepted**: ~50 existing node kinds each gain an explicit
`requires_ai=False`, which is verbose and is the point: the exemption is visible
in review, one line per node, the same posture as a reviewed allow-list.

**Alternatives considered**: `requires_ai: bool = False` — consistent with the
other fields and silently wrong in the dangerous direction. A separate set of AI
node names held elsewhere — a second list to keep in step with the catalogue, and
the catalogue is the thing that changes. Deciding at execution time from a node's
configuration — more precise, but a flow's runnability stops being answerable from
its graph, so the editor could no longer tell a user in advance whether it will
run.

## 7. Provisioning as one idempotent operation

**Decision**: Extract the seeding sequence out of `_bootstrap_platform_data()`
(`main.py:1044-1200`) into `services/provisioning.py`, called from boot and from
the toggle.

**Rationale**: FR-012 requires turning AI on to produce the same content boot
would. The nine seed calls plus the example flow, template containers, intent
bindings and model prices currently exist only as inline lifespan code, so there
is nothing the toggle can call. Extraction is the largest structural change here
and it touches boot, so it should land and be verified **before** the toggle is
built on top of it.

**Idempotence is a precondition, not a new property**: the individual
`seed_builtin_*` functions already return counts and are safe to re-run at every
boot, which is why they can be reused. What the extraction must not do is change
their ordering, since later steps reference entities created by earlier ones.

**Alternatives considered**: Calling the seed functions again from the toggle
handler — two copies of a nine-step ordered sequence, and the copy that is not
exercised at boot is the one that rots. Requiring a restart to provision — fails
FR-002 and makes the toggle a half-measure users would not trust.

## 8. Frontend: the two obvious hooks are both wrong

**Decision**: Add a `requiresAi` marker to the navigation model rather than using
`NavLeaf.visible`, and drive the disabled affordances from the platform-config
flag rather than from caught errors.

**Rationale**: `visible` hides an entry when it returns false
(`nav-model.ts:30-38`), and FR-017 requires AI areas to stay listed and reachable
— using `visible` would deliver the opposite of the requirement while looking
like the natural fit. Driving affordances from config matches how
`agentic_ai_enabled` is already consumed (finding 2) and means a button is
disabled *before* a user clicks it, rather than after a refusal comes back.

**Alternatives considered**: Reusing `visible` — removes the FOMO surface that is
the stated reason for the feature. Rendering the disabled state from a caught 403
— the user learns only by trying, and it cannot work at all on the sign-in screen
where FR-011 needs it.

## 9. Landing screen

**Decision**: Generalise `pages/xtm-one/CtemTrialSplash.tsx` rather than write a
second splash.

**Rationale**: It already solves this problem for a neighbouring case — a
capability the deployment does not have, presented as an invitation rather than an
error — and its `VALUE_PROPS` copy is written. Principle X asks for shared
components reused rather than re-implemented, and two splash screens would drift
in tone.

**Alternatives considered**: A new page — duplicates layout and tone decisions
already made. Reusing it unchanged — its copy is CTEM-specific, so the
generalisation is the work.

## Open, and deliberately not resolved here

- **FR-031, licensing.** Whether an AI-disabled platform needs a licence is a
  commercial decision. The spec records the engineering answer (no licence, mode
  reported in telemetry) with the branch owner's sign-off, not the product
  owner's. Implementation should not treat it as settled if it is contested.
- **Copy and its translations.** Owned by product and marketing per the spec's
  assumptions. The design must leave a place for it; it must not invent it.
