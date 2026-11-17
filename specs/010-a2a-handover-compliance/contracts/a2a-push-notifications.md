# Contract: Push Notifications

**Requirements**: FR-015..FR-022 | **Data model**: [../data-model.md](../data-model.md)

## Method vocabulary

The four configuration methods `protocol.py` currently refuses by name become
served. Both spellings resolve to the same four operations.

| Operation | v0.3 | v1.0 |
| --- | --- | --- |
| Register | `tasks/pushNotificationConfig/set` | `CreateTaskPushNotificationConfig` |
| Read one | `tasks/pushNotificationConfig/get` | `GetTaskPushNotificationConfig` |
| List for a task | `tasks/pushNotificationConfig/list` | `ListTaskPushNotificationConfigs` |
| Remove | `tasks/pushNotificationConfig/delete` | `DeleteTaskPushNotificationConfig` |

`is_push_notification_method` and the `PushNotificationNotSupportedError` it
routes to are removed, not left unreachable.

## Two ways a callback is registered

A partner registers a callback either with `tasks/pushNotificationConfig/set`, or
inline on the submission itself — `SendMessageConfiguration` carries a
`task_push_notification_config`. Both must be honoured (FR-016, FR-031); only
the first is served today. The inline path is the
one that resolves the ordering problem: the methods above address a task by id,
and a partner submitting work does not have that id until the submission
returns, so registering separately means either a second round trip or a window
in which the task can finish unwatched.

## Configuration object

`TaskPushNotificationConfig`, whose SDK fields are `id`, `task_id`, `url`,
`token`, `authentication`, `tenant`.

| Field | Handling |
| --- | --- |
| `url` | validated on registration and again before each send (FR-019) |
| `token` | stored Fernet-encrypted, echoed to the partner on delivery, never logged |
| `authentication` | `scheme` and `credentials` presented as registered; an unsupported scheme is refused on registration rather than at delivery |
| `tenant` | ignored, as on the card — this platform has no tenant entity |
| `id` | minted here; a partner-supplied value is not honoured |

A registration referring to a task the calling registration does not own gets the
uniform not-found outcome.

## Notification

`POST` to `url`, body carrying the task's identity and state and nothing else
(FR-017):

```json
{ "taskId": "...", "contextId": "...", "state": "completed" }
```

A partner reads the result with `tasks/get`. This is what keeps content off an
endpoint whose owner we can validate but not authenticate.

## Authenticating the sender

The partner decides how its own endpoint is protected, and we present what it
registered (research [R2](../research.md)). The platform holds no key material.

| Registered | Presented on delivery |
| --- | --- |
| `token` | echoed in the notification body, so the partner can reject a call from anyone who does not hold it |
| `authentication.scheme` + `credentials` | sent as that scheme's header |
| neither | delivered unauthenticated; the partner accepted that when it registered a bare URL |

A forged notification discloses nothing, because the body carries no content. What
authentication prevents is a partner being driven to call `tasks/get` on a false
signal, spending its credential and our task surface.

## Delivery

| Property | Rule |
| --- | --- |
| Trigger | every state transition the partner can observe, terminal included |
| Isolation | delivery never blocks, delays or changes the task's outcome (FR-021) |
| Retries | bounded (FR-020); a repeatedly failing endpoint trips the shared circuit breaker rather than retrying per task forever |
| Bound | configurations per task are capped, and the cap refuses rather than evicting (FR-022) |
| Ordering | not guaranteed; a partner treats a notification as "look at this task", never as the state itself |

## Card

`capabilities.pushNotifications` becomes `true` (FR-015), replacing the `false`
`006-a2a-server-inbound` FR-016 required. Both protocol versions spell the field
identically, so this needs no per-version branch.
