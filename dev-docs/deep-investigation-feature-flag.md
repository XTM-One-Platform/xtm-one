# Deep Investigation - Feature Flag

Deep Investigation ships behind the `DEEP_INVESTIGATION`
feature flag because it is not ready for production. Unlike
`FLOW_BUILDER` (see `flow-builder-feature-flag.md`), this flag is **enforced by
the backend**: with the flag off the feature is dark, not merely hidden.

## Enabling it

Off by default. Enable per deployment:

```
ENABLED_FEATURES=DEEP_INVESTIGATION
```

The flag is read once at startup, so a change needs a restart, and the resolved
id list reaches the frontend through `GET /auth/config`.

**`ENABLED_FEATURES=*` enables it too** - `*` means every known flag. A
deployment that uses `*` to get `FLOW_BUILDER` turns Deep Investigation on as
well; list the ids explicitly instead.

## What the flag gates

| Surface | With the flag off |
| ------- | ----------------- |
| `/api/v1/investigations*` | Every route answers 403 naming the flag (router-level `require_feature`, evaluated before authentication), and the routes are absent from `/api/openapi.json` |
| Web chat send (`POST /api/v1/web/chat/messages`) `mode: "deep_investigation"` | Ignored - the message runs as an ordinary turn (`api/v1/chat.py`) |
| `deep_investigation` built-in tool | Never built, in chat (`agent_chat.py`) and in assignment runs (`assignment_engine.py`), even for an agent that carries the id |
| `GET /settings/builtin-tools/catalog` | The `deep_investigation` key is omitted - filtered in the endpoint and in `_scannable_tool_groups`, *not* in `build_builtin_tool_catalog`, which doubles as the registry skill-package validation reads |
| Deep Investigation Agent (built-in) | Not seeded; its handover entries (slug, type, condition) are dropped from every other built-in, and its `- **Deep Investigation Agent**` bullet is removed from their personas (`FEATURE_GATED_BUILTIN_AGENTS` in `builtin_agents.py`, `_seeded_builtin_agents()` in `seed.py`) |
| Intent bindings | Not bound to the withheld agent |
| Chat composer | The Deep Investigation toggle is not rendered |
| Built-in tool pickers | The suite is not offered (`BUILTIN_TOOLS` `gate: "deep_investigation"`, resolved by `useBuiltinTools`) |
| Persisted `[[INVESTIGATION:...]]` blocks | Not rendered; the markers are still stripped from the message text |
| MCP server detail -> Tools | The "Use for investigation" designation is neither shown nor offered |

## What is kept

- Stored data: `investigations` rows and their steps have no foreign key to
  `agents`, an agent's `builtin_tool_ids` may still list `deep_investigation`,
  and an MCP server keeps its `investigation_tool_name`. Nothing is migrated -
  turning the flag back on picks everything up again.
- `AGENT_KEYED_BUILTIN_TOOL_IDS` and the channel marker stripping are unchanged.

## Turning the flag OFF on an instance that already seeded the agent

The built-in seed is a full sync: a built-in no longer in the seeded list is
**deleted** on the next boot (phase 1 of `seed_builtin_agents`), exactly like a
retired built-in. The foreign keys decide what goes with it:

- **Kept** (`SET NULL`): conversations and their messages, activity entries,
  work items, shared state, tasks.
- **Deleted** (`CASCADE`): its assignments, delegations, sub-agent tasks,
  memory entries, config and prompt versions, annotations, evaluation results,
  intent bindings, quota rows.

Turning the flag back on seeds the agent again under a **new id**; the handovers
of the other built-ins are re-synced on the same boot.

## Removing the flag

Once the feature is production-ready, drop `DEEP_INVESTIGATION` from
`KNOWN_FEATURE_FLAGS` / `FeatureFlag` (`backend/app/core/feature_flags.py`) and
from `KNOWN_FEATURE_FLAGS` in `frontend/src/lib/utils.ts`, delete
`FEATURE_GATED_BUILTIN_AGENTS` (and, if nothing else uses them,
`_withheld_builtin_agent_slugs` / `_without_specialists` in `seed.py`), and
remove the gates listed in the first table.

`backend/tests/test_deep_investigation_feature_flag.py` pins all of it.
