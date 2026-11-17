# Contract: Page Action Frame (XTM One ⇄ widget)

The widget and XTM One must both implement this exactly. Hosts proxy it unchanged (FR-009).

## 1. Declaring support: `POST /api/v1/platform/chat/messages`

The request body gets one new field:

```json
{ "content": "...", "supports_page_actions": true }
```

The widget sends it only when `apiEndpoints.approve` is set and the host passes `onRelativeLinkClick` (the host's opt-in: its proxy must forward the approve body whole). It omits the field otherwise rather than sending `false`. When the field is missing or `false`, `act_on_page` is not offered (FR-005).

## 2. Frame: on the turn's SSE stream

The frame is a `status` frame, the existing envelope of `emit_tool_event`:

```json
{
  "type": "status",
  "status": "tool_progress",
  "tool": "act_on_page",
  "tool_call_id": "call_…",
  "event": "page_action_required",
  "conversation_id": "…",
  "action_id": "…",
  "action": { "type": "snapshot" }
}
```

The `action` field takes one of these forms:

| `action.type` | Other fields | Widget does |
|---|---|---|
| `snapshot` | none | nothing, then answers with a snapshot |
| `navigate` | `path` (relative, starts with `/`) | `onRelativeLinkClick(path)` |
| `click` | `ref` | `refs.get(ref).click()` |
| `open` | `ref` | hovers the element the way a pointer does, so a menu that opens on hover lists its items |
| `type` | `ref`, `text` | native value setter, then an `input` event |

The stream stays open and silent, apart from keep-alives, until the answer is accepted or the step times out (20 s).

## 3. Answer: `POST /api/v1/platform/chat/messages/approve`

```json
{
  "conversation_id": "…",
  "page_action": {
    "action_id": "…",
    "ok": true,
    "error": null,
    "url": "/dashboard/analyses/reports",
    "title": "Reports | OpenCTI",
    "snapshot": "[e1] link \"Reports\"\n[e2] button \"Create\"\n…"
  }
}
```

- `error` is set only when `ok` is false. The values are `unknown_ref`, `navigation_unavailable`, or `failed: <message>`.
- `snapshot` is always taken after the action, whether it succeeded or not.

**Body rules**: exactly one of `decisions` and `page_action` must be present, checked in the handler. `snapshot` holds at most 20000 characters and `url` at most 2048, as field caps.

| Status | When |
|---|---|
| 200 `{"status":"accepted"}` | `action_id` is the action this conversation is waiting on |
| 400 | both or neither of `decisions` and `page_action` are present |
| 422 | a field is over its cap |
| 404 | the conversation is not the caller's (the same rule as for decisions) |
| 409 | nothing is waiting, or another `action_id` is waiting |

On a 409 the widget drops the action and does not retry.

## Snapshot text

```text
# <title> — <url>
[e1] link "Reports" -> /dashboard/analyses/reports
[e2] button "Create" (disabled)
[e3] textbox "Search" = "apt28"
heading "APT28" (level 1)
[e4] tab "Overview" (selected)
```

- The widget writes one line per element, in document order, and stops at the 20000-character cap.
- Element states are written in parentheses: `disabled`, `selected`, `checked`, `expanded`, `menu` (a menu that is closed: `aria-haspopup` set and not expanded), `pressed`, `current`. A heading is a context line with no reference: `heading "Reports" (level 1)`.
- After `=` comes the current value of a text field. Password fields never get one.
- After `->` comes where an element with an address within the host application leads (path and query): a link or a menu item. One to another origin gets none.
- Refs are valid until the next snapshot.
