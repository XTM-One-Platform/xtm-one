# Mobile companion - Feature Flag

The mobile companion (user docs: `docs/docs/usage/mobile-companion.md`) ships
behind the `MOBILE_COMPANION` feature flag, off by default. Like
`VIDEO_STUDIO`, the flag is **enforced by the backend**: with the flag off the
companion is dark, not merely hidden.

## Enabling it

```
ENABLED_FEATURES=MOBILE_COMPANION
```

The flag is read once at startup: set it on the platform (it serves the routes
and the PIN lock) and the worker (it pushes the notifications and builds the
`phone` tool in assignment runs), and restart them. The resolved id list
reaches the frontend through `GET /auth/config`.

## What the flag gates

| Surface | With the flag off |
| ------- | ----------------- |
| `/api/v1/mobile-devices*`, `/api/v1/mobile/push-actions`, `/api/v1/app-lock*` | Every route answers 404 with the body of an unknown route (router-level `require_feature_or_404`, evaluated before authentication), and the routes are absent from `/api/openapi.json` |
| Phone notifications | Never queued (`mobile_push.queue_push`), never delivered (`deliver_queued_push`, a job queued before the flag went off included), never sent (`push_item_to_user`) |
| Companion PIN lock | `app_lock.enforce` never refuses with 423: a phone locked when the flag went off is not locked out. A session its PIN ended (too many wrong PINs) stays ended: the person signs in again |
| `phone` built-in tool | Never built, in agent chats (`agent_chat.py`), General Assistant chats (`general_chat.py`) and assignment runs (`assignment_engine.py`), even for an agent that carries the id |
| `GET /settings/builtin-tools/catalog` and `/groups` | The `phone` key is omitted (`_offered_builtin_tool_catalog`, `_scannable_tool_groups`); the group keeps its card with `gate: "mobile_companion"`, shown disabled by `useBuiltinTools` |
| Agent builder tool | `phone` is not named in the ids it advertises to the model (`_FEATURE_GATED_BUILTIN_TOOL_IDS`); its validation still accepts it |
| Flow builder tool list | `phone` is not listed (`flow_access`) |
| Phone app shell | A phone gets the web app (`useCompanionDevice` is false): no companion shell, no PIN lock screen, no phone-only chat layout |
| `/inbox`, `/share` | Redirect home (`FeatureFlagGate` in `App.tsx`) |
| Profile | No Phones tab |

## What is kept

- Stored data: linked phones, PINs, companion sessions, notification read
  state. Nothing is migrated: turning the flag back on picks everything up.
- The notification feed (`/api/v1/notifications`) and its server-side read
  state stay: the web app's bell reads them.
- The two companion security options (`phone_notification_content`,
  `companion_pin_required`) stay in Settings > Security: they are part of the
  security audit, and they govern the companion again when it comes back.
- The service worker (`public/sw.js`) is still registered: it caches nothing
  and only answers an offline page; with nothing pushed and `/share` gone, its
  push and share handlers never run.

Pinned by `backend/tests/test_mobile_companion_feature_flag.py`.
