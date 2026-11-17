# Data Model: Gateway Model Types and Decision Models

No stored data and no migration: the gateway's model list is read per request and nothing about it is remembered.

## GatewayModel (in memory)

One entry of the gateway's model list, as `FiligranGatewayProvider._models_with_meta()` returns it.

| Field | Meaning | Rule |
|---|---|---|
| `id` | The model id | As listed. |
| `owner` | The upstream provider | The entry's `owned_by`, else `gateway`; names the `X-Target-Provider` of non-chat calls. |
| `types` | The capabilities the model serves | Resolved as in [the list contract](contracts/gateway-model-list.md); never empty. |

Replaces the `(id, owner, type)` tuple; the callers that unpacked it read attributes.

## Capability

A name a picker matches in `types`: `chat`, `embedding`, `image`, `video`, `speech`, `decision`. Any other name is carried and matches nothing (FR-004).

## State

None. A model whose types change between two lists is offered where the second list says.
