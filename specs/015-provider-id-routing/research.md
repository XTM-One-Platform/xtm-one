# Phase 0 Research: Route by provider config, not by model name

All decisions are grounded in existing code; no unknowns remain.

## D1 — Where the reference lives: three new nullable UUID columns

**Decision**: add `general_assistant_config.llm_provider_id`,
`platform_settings.routing_provider_id`, `platform_settings.fallback_provider_id`,
each beside the model column it qualifies. The other four surfaces already have
theirs (`Agent.llm_provider_id`, `PlatformSettings.agent_default_provider_id` /
`embedding_provider_id` / `image_provider_id`).

**Rationale**: mirrors the four columns that already exist, so resolution has one
shape everywhere.

**Rejected**: encoding `provider_id::model` into the existing model column — every
reader of a model string (prefix inference, chat-capability classification, the
price catalog) would need to strip it, and the columns are `String(100)`.

## D2 — Bare UUID, no foreign key

**Decision**: `UUID(as_uuid=True)`, nullable, no `ForeignKey`, matching all four
existing reference columns.

**Rationale**: FR-007 requires a dangling reference to be logged. `ON DELETE SET
NULL` would erase the evidence, and `RESTRICT` would block deleting a provider
config. The dangling row *is* the diagnostic.

## D3 — Resolution threading

`get_llm_for_user(provider_id_override=…)` (`router.py:1150`) already prefers a
UUID and already handles a stale one (`router.py:1310-1336`: log, fall back to the
name). FR-005 and FR-007 are therefore satisfied by passing the stored id at the
three call sites that do not:

| Surface | Call site | Change |
|---|---|---|
| GA chat | `services/general_chat.py:286` | pass `provider_id_override=ga_provider_id` |
| Routing | `services/routing_evaluator.py:98`, `services/flow_llm.py:166` | `routing_model()` returns `(model, provider_id)` |
| Fallback | `router.py:718` `resolve_configured_fallback` | prefer `ps.fallback_provider_id` over `_resolve_model_owner_config` |

`_resolve_model_owner_config` and its recency sort stay untouched as the
no-reference path (FR-006).

## D4 — Picker value encoding: `<provider_id>::<model_id>`

**Decision**: model selects carry a composite value and use it as the React key.
`NewAgentWizard.tsx:242,319` already does exactly this, with the same rationale in
a comment. Encode/decode goes in one shared helper (Principle VIII) rather than
six copies.

**Rationale**: one control for one choice, and the composite is unique, which is
what fixes the duplicate-key symptom.

**Rejected**: keeping `value={m.model}` and adding a provider dropdown beside it —
two controls for one decision, and it leaves the duplicate keys.

`get_available_models` (`router.py:2116`) already emits one entry per
(config, model) with `provider_id`, so FR-008 needs no new data.

FR-009 did: this decision first reused `getProviderDisplayName` (`Settings.tsx`),
which named the provider *kind* and so read identically for two configs of one
vendor. **Superseded by D10** — `providerConfigName` (`lib/utils.ts`) replaced it
and `getProviderDisplayName` is gone.

## D5 — Overlap warning: a computed field on the provider response

**Decision**: add `model_overlaps` to `LLMProviderResponse`
(`api/v1/schemas/settings.py:26`), computed over all configs.

**Rationale**: the list endpoint already feeds the pickers, so one field serves
both the save-time warning and a banner that survives a reload. Configs number in
the handful, so the pairwise comparison is free.

**Rejected**: a warning returned only from POST/PUT — invisible after a reload,
and invisible to an admin who inherited the deployment.

The create/update responses deliberately carry an empty list: the client
invalidates the provider list on success, so the refetched list delivers the
save-time warning. Computing it on each save instead meant a full-table scan per
write, and it broke three existing tests whose fake sessions expect one query —
a signal that the extra query did not belong there.

## D6 — No backfill

Existing rows keep a null reference and today's resolution (FR-006). A backfill
would have to pick a config by recency, which is the defect.

## D7 — Migration shape

One migration off head `k5p3358studio`: three `ADD COLUMN ... NULL`
(metadata-only in PG 17, no rewrite), `IF NOT EXISTS` guarded, with a `downgrade()` dropping them —
Principle V, verified by the migrations CI job.

## D8 — A name-only payload keeps today's save-time pin

**Decision**: `settings.py:2129` is left alone. When a payload sends a provider
name with no id and two configs share that name, the id stays pinned to the
newest config for that name, as today.

**Rationale**: the pin is decided once at save time and then holds, so the route
does not drift when configs are later created or deleted. It records a config the
admin did not explicitly choose — but in a name-only payload the admin chose no
config at all, so there is no selection to honour, and stability beats a
per-call re-guess.

**Rejected**: rejecting the ambiguous payload (422) — fail-closed is the better
instinct, but it breaks API clients that only ever sent names, for an input the
pickers will never produce once US2 lands. Also rejected: storing null and
letting read-time recency decide, which re-guesses on every call and is the least
stable of the three.

**Scope**: this path is unreachable from the UI after US2 — every picker sends
the id. It only governs direct API callers.

## D9 — The mismatch auto-correct had to become reference-aware

**Found during implementation, and it is the crux of this feature.** Passing the id at
the call sites was not sufficient.

`get_llm_for_user` compares the resolved provider against `model_owner_config`
(the newest-config-first guess) **by provider name** and auto-corrects to the
owner on a mismatch (`router.py:1428`, tested). So an explicit
reference survived only while both configs shared a provider name — two Anthropic
rows, say. In the issue's headline case, a Filigran gateway (`litellm`) beside a
direct `anthropic` key, the names differ, so the admin's gateway reference was
auto-corrected straight back to the recency winner.

**Decision**: the auto-correct no longer fires when the referenced config itself
advertises the effective model (`reference_serves_model`). A reference that
cannot serve the model still auto-corrects, because calling it would 404 on the
provider.

**Rejected**: dropping the auto-correct for any resolved reference — it exists to
prevent an invalid provider/model pair, and a reference to a config that does not
offer the model is exactly that.

Applies to every config-id channel: the per-call `provider_id_override`,
`PlatformSettings.agent_default_provider_id`, and `GeneralAssistantConfig`'s own
reference on the router's last tier. A provider *name* override keeps
auto-correcting unchanged, since a name cannot identify a config instance.

**A reference is scoped to the model it was saved with** (found by code review).
`agent_default_provider_id` was saved with `agent_default_model`, so it must not
outrank the model owner for a model that arrived from an agent override or a
user preference — that would move someone else's chosen model onto the platform
default's credentials and bill. The routing and flow paths already scoped theirs;
this one did not. The same reasoning is why the env seeder and the orphan cleanup
must clear a reference whenever they clear the model it belongs to.

## D10 — Identically-named configs get an ordinal, and one name everywhere

`display_name` is optional, so several configs of one vendor can all fall back to
the vendor's name and read identically — FR-009 unmet, even though the saved
reference is correct and the duplicate-key symptom is gone.

**Decision**: `providerConfigName` is the single formatter for a config's name,
used by the providers list and every model picker. A config whose name collides
with an older one gets an ordinal in brackets — `Anthropic`, `Anthropic (2)` —
following the familiar "name", "name (2)" convention so the oldest keeps the name
an admin already recognises. The ordinal is the config's position among the
identically-named ones in the list as received — the endpoint orders by
`created_at, id`, so re-deriving that order on the client would be a second
source for one ordering, free to disagree with the list it labels.

The providers list is therefore rendered in the endpoint's order too, rather than
sorted by provider kind for display: the ordinal counts in one order, so the cards
have to be shown in it or the `(2)` badge can disagree with the card above it.
User-visible on every install, not only those with namesake configs.

**Rejected**: a short config id (`Anthropic · 3f7a91`) — an id in a picker tells
a reader nothing. Auto-assigning `display_name` at creation — it would store a
generated name indistinguishable from one the admin chose, and env seeding names
nothing. Two formatters, one per surface — the same config would appear under two
names.

## D11 — Pickers group models under their provider config

**Decision**: model pickers render one branch per provider config
(`ModelSelectItems`), the branch labelled with `providerConfigName` and the leaves
carrying the bare model id.

**Rationale**: a flat list repeats a shared model id with no way to tell the
entries apart, and suffixing every leaf with its provider makes the common case
noisy. The branch carries the provider once, so leaves need no suffix.

The *closed* trigger cannot use the branch: Radix does not copy a `SelectLabel`
into `SelectValue`, so the collapsed picker would read as a bare model id and
FR-009 would fail on exactly the ambiguous case. `ModelSelectValue` renders
`model (provider)` for that one state — the provider appears once when the list is
open and once when it is closed, never twice.

**Rejected**: a collapsible tree — Radix Select does not nest; `SelectGroup` +
`SelectLabel` is the grouping it supports, and it reads as one.
