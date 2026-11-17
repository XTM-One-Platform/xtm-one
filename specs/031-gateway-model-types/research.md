# Research: Gateway Model Types and Decision Models

## R1. Reading `types`

- **Decision**: `_models_with_meta()` returns one `GatewayModel(id, owner, types)` per entry, `types` a tuple resolved as [the list contract](contracts/gateway-model-list.md) states (FR-003, FR-004).
- **Rationale**: the OpenAI SDK's `Model` keeps unknown fields, which is how `type` is read today (`test_list_models_sends_jwt_and_filters_chat`), so `types` needs no client change. When both fields are present `types` is the set and `type` is derived from it by the gateway, so they disagree only through a gateway defect; reading `types` first is the only choice that does not drop a second type.
- **Alternatives considered**: keeping the third tuple slot a string and adding a parallel `types` (two shapes for one fact); lower-casing type names (today's `t == "chat"` is exact, and the gateway declares them lower-case).

## R2. One membership test

- **Decision**: every picker asks `capability in model.types`; video and speech keep their `or is_video_model(id)` / `or is_speech_model(id)` clause, which is today's behaviour and outside this spec (Assumptions).
- **Rationale**: the pickers differ today only in the string they compare; membership is that comparison over a set, so no picker needs its own handling of a multi-type model (FR-002).
- **Alternatives considered**: a `models_of(capability)` helper on the provider; rejected, each picker is one line over `_models_with_meta()` and a helper would be the fifth abstraction over a list comprehension.

## R3. Decision models: membership, no probe

- **Decision**: the gateway's `list_decision_models()` is `"decision" in types`, and a custom OpenAI-compatible provider's is the same rule over its raw entries (R6). The classification fallback never yields `decision`, so a gateway that states no type lists none and is offered for a typed model id. Nothing is sent but the list the other pickers already read (FR-006 to FR-009).
- **Rationale**: the gateway refuses an unknown model id itself and answers OpenAI's 400 as a 502, so the OpenAI probe (spec 028) cannot tell a decision model from another through it, and its listing is authoritative. `types` replaces 028's `type == "decision"`, which hid a model typed chat and decision from the decision picker.
- **Alternatives considered**: the 028 probe (does not work through the gateway); `type == "decision"` (the case this feature exists for).

## R4. No wire per model

- **Decision**: spec 028's one route stands: the gateway, however configured, asks OpenAI's Decisions route (`decisions_api` per provider class). No `_decisions_api_for` hook, no `wire` field.
- **Rationale**: spec 028 merged the gateway on that route alone and left the upstream to the gateway. There is nothing to choose: an open gateway change makes `/v1/decisions` the only decision route and translates for System One providers.
- **Alternatives considered**: an overridable per-model wire with a System One default (the first design here, withdrawn); mapping `owned_by` to a wire (copies the gateway's route support into XTM One).

## R5. Order against spec 028 (merged)

- **Decision**: 028 landed first, so this branch merged `main` and replaced its `type == "decision"` test by membership in the same change as the pickers; 028's text is revised by replacement (T024).
- **Rationale**: the merge carried a silent break: `list_decision_models` unpacked the old `(id, owner, type)` tuple, which the new three-field record also satisfies, so the comparison never matched. `tests/test_gateway_decision_models.py` caught it.
- **Risk**: with the gateway a decision provider, an unpinned Classify node (flow) that sees a gateway beside another decision provider can hit the ambiguous-provider refusal; 028 records it (Assumptions).

## R6. One resolution for both providers

- **Decision**: `declared_model_types(types, type)` lives in `openai_compat.py`, which both providers subclass: `types` when it holds a text entry, else `type`, else none. The gateway adds the classification fallback; the custom provider adds none.
- **Rationale**: both read the same `/models` shape, so two copies would drift (constitution VIII), and the comment saying why `types` outranks `type` has one home.
- **Alternatives considered**: the custom provider importing the gateway's function (a generic provider depending on one vendor's module).
