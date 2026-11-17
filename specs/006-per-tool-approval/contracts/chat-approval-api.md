# Contract: chat approvals for external clients

**Audience**: anyone driving XTM One agent chat from outside this repository.

Four consumers are known. One is the widget; the other three embed it behind
their own proxy, and no two proxies behave the same:

| | Consumer | Proxy | Body reaching us | Stream deadline |
|---|---|---|---|---|
| 1 | [`filigran-chatbot`](https://github.com/FiligranHQ/filigran-ui/tree/main/packages/filigran-chatbot) | — | as sent | none |
| 2 | **OpenCTI** | Node | forwarded verbatim | none (`timeout: 0`) |
| 3 | **OpenAEV** | Java | **rebuilt from 4 named keys** | **15 minutes** |
| 4 | **OpenGRC** | Node/Fastify | forwarded verbatim | none |

All three embedders pin `@filigran/chatbot` at exactly `3.7.4` and configure the
same six `apiEndpoints` remaps. The last two columns are where they diverge, and
both columns decide whether approval can work.

**Status**: proposed. Comments welcome *before* anyone builds, because all five
repositories encode it.

---

## What changed, in one paragraph

Agent tool calls now require human approval unless the tool is on a
platform-wide whitelist. A conversation that hits a gated tool needs a decision
before it can continue.

**Your existing build keeps working.** It receives a normal assistant message
saying what needs approval — ordinary text, rendered by whatever you already do
with an answer. The interactive prompt is opt-in and you adopt it when ready.

There are two ways to handle it. Pick based on whether you can send anything to
the server **during** a turn.

> **For `filigran-chatbot`: Path A applies, and the plumbing already exists.**
>
> `POST /api/v1/platform/chat/messages` (a streamed response body read with
> `res.body.getReader()`) plus `POST /api/v1/platform/chat/messages/steer` for
> mid-run messages is a streaming channel out and a POST channel back — exactly
> what a mid-turn approval needs. Approval is the same shape as steering: an
> event on the stream, a decision POSTed back, the turn carries on.
>
> Concretely: `hooks/protocols/parseRestEvent.ts` for the inbound event, and a
> decision POST beside the existing steer call in `useChat.ts`.
>
> **REST only.** OpenCTI and OpenAEV both configure `backendType="rest"`, so
> `parseLegacyEvent` and `parseAgUiEvent` need nothing. (`legacy` resolves
> to the separate `@filigran/chatbot-legacy@1.1.2` pin, which routes to Flowise
> when XTM One is not configured and therefore never meets this gate.)

---

## The host may not be XTM One

The widget does not always talk to us directly, and the contract has to survive
that.

**Every** embedding platform does this, and they arrived at it independently:
each sets `apiBaseUrl` to its own proxy, which authenticates the session, mints a
platform JWT server-side, and forwards to `/api/v1/platform/chat/messages`. Every
path is remapped through `apiEndpoints` because the XTM One-style defaults do not
exist there. The three configurations are near-identical:

```tsx
// OpenCTI                                // OpenAEV              // OpenGRC
`${APP_BASE_PATH}/chatbot`                '/api/xtmone/chat'      '/api/xtm-one/chat'
{ messages: '/messages', steer: '/messages/steer', agents, sessions, upload, download }
```

Proxying is therefore the **normal** deployment of this widget, not one
platform's quirk. Anything in this contract that assumes the widget talks to us
directly is wrong for three of the four known consumers.

Three consequences.

**A decision needs a route that does not exist yet** — on all three. There is no
approve path in `httpPlatform.js` (OpenCTI), `XtmOneChatApi.java` (OpenAEV) or
`xtm-one-http.ts` (OpenGRC). On each, it is a copy of the existing steer handler.

OpenGRC is worth copying here: its chat proxy requires a real cookie session and
explicitly refuses XTM One's own bearer JWT, on the grounds that "agents have no
business calling the chat back". Whatever route carries approval decisions should
hold that same line — a decision must come from a human's browser session, never
from an agent identity.

**The proxies do not agree on what reaches us, and that decides who can opt in.**
OpenCTI and OpenGRC both forward the request body untouched
(`JSON.stringify(req.body ?? {})`), so a new field traverses them for free.
OpenAEV rebuilds the upstream body field by field:

```java
// XtmOneClient.streamChatMessage — an allowlist, not a passthrough
body.put("content", content);
if (conversationId != null) body.put("conversation_id", conversationId);
if (agentSlug != null)      body.put("agent_slug", agentSlug);
if (context != null && !context.isEmpty()) body.put("context", context);
```

`supports_tool_approval` sent by the widget would be **silently dropped** there.
The failure direction is safe — we never see the flag, so we never pause, and the
widget gets the ordinary degraded message — but it is silent, and it looks from
the browser like the server ignored a flag that was sent. OpenAEV needs a client
signature change, not just a route; the other two need only the route.

**Whatever call the widget uses for approve must go through the same fetch path
as its siblings.** OpenAEV monkey-patches `window.fetch` to inject Spring's
`X-XSRF-TOKEN`, scoped by URL prefix, because — per its own comment — "the
bundled `@filigran/chatbot` widget uses a plain `fetch(url)` call with no way to
customize headers". OpenCTI instead relies on the `requestHeaders` prop to carry
`opencti-draft-id`. Those two only both keep working if approve is issued the
same way every other endpoint is: relative to `apiBaseUrl`, and honouring
`requestHeaders`. An approve call that takes an absolute URL breaks OpenAEV's
CSRF wrapper; one that skips `requestHeaders` breaks OpenCTI's draft context.

### So: one nullable config entry, not a fixed path

An earlier draft of this contract specified a fixed `/chat/messages/approve` and
a separate `supports_tool_approval` boolean. That does not survive a proxy. It
should follow the idiom `ApiEndpoints` already uses:

```ts
/**
 * Path for submitting tool-approval decisions. No default — a host must name
 * the path explicitly. While unset the widget never claims approval support
 * and the backend degrades to a plain message.
 */
approve?: string | null;
```

...and `supports_tool_approval` should be **derived** from whether that entry is
set, not passed as its own prop.

**Why no default, when every sibling entry has one.** Steer degrades safely: a
non-2xx rolls the optimistic bubble back into the composer. Approval cannot,
because by then the server has already paused the turn. If `approve` defaulted
to the XTM One path, either embedding platform could upgrade the widget without
adding its proxy route, advertise support, receive `approval_required`, POST the
decision into a 404 — and hang the turn forever with the user watching a spinner.
That is precisely the failure the opt-in exists to prevent, reintroduced through
a default value. With two of three consumers proxied, this is the expected case,
not an edge one.

Requiring the host to name the path makes claiming support the same deliberate
act as being able to honour it.

---

## An approval pause holds a stream open — and that is already handled

This is the constraint nobody designed for, because until now no turn ever
waited on a human.

A paused turn keeps its connection open and **silent** for as long as the
reviewer takes — through the widget, the host proxy, and whatever ingress sits in
front of it. Any read timeout in that chain is an approval deadline, whether or
not anyone chose it as one. The three known proxies already disagree:

| Proxy | Stream deadline |
|---|---|
| OpenCTI | none — `timeout: 0` |
| OpenGRC | none — its 60s cap covers only the non-streaming forwards |
| **OpenAEV** | **`setResponseTimeout(Timeout.ofMinutes(15))`** |

**Decision: we keep the stream warm rather than defining a deadline of our own.**
A reviewer reading a proposed argument list carefully is doing the thing the
feature exists for; a countdown they cannot see would punish it.

No new mechanism was needed. Both transports already heartbeat through silent
windows, and a paused turn is simply another silent window:

- **SSE** — an idle `stream_q` emits `: keepalive\n\n` every **15 seconds**. An
  SSE *comment* line, so every conformant parser drops it and no client can
  mistake it for content.
- **WebSocket** — an ungated `while True` task pings every **20 seconds**,
  independent of turn state.

That already neutralises the OpenAEV ceiling. Its proxy copies raw bytes
(`sseStream.read(buf)` → `outputStream.write(...)` → `flush()`), so keepalive
bytes traverse it and reset the read timeout long before 15 minutes elapse.

> **Worth OpenAEV confirming.** This rests on `responseTimeout` behaving as a
> per-read socket timeout during entity streaming, so traffic every 15s keeps
> resetting it. That matches our reading of Apache HttpClient 5, but you know
> your transport better than we do. If it is instead a wall-clock cap on the
> whole exchange, a long pause dies at 15 minutes regardless of keepalives and
> you would need to raise it for the streaming call specifically.

Regression-tested at `backend/tests/test_chat_approval_gate.py`, which pins the
keepalive interval against the tightest host timeout we know of — so raising it
fails a test rather than silently breaking approvals in a way that looks like a
network fault.

---

## Opting in — read this first

Mid-turn approval is **opt-in**, and the default protects you.

The wire field is `"supports_tool_approval": true` in the message body (REST) or
on the send frame (WebSocket). In the widget it should come from
`apiEndpoints.approve` being set, per above, rather than being surfaced
separately. Until then the server takes Path B and your existing build keeps
working.

That default exists because the alternative is the worst kind of failure. A
client that ignores `approval_required` never answers, so a server that paused
anyway would wait **forever** — no error, no timeout, just a turn that never ends
and a user watching a spinner. Declaring support is you promising to answer.

**So an un-updated chatbot will not break.** It degrades to a readable message,
and you adopt the interactive flow when you are ready.

---

## If you are a host, not a chat UI

Some callers reach agent chat with **nobody watching**. OpenCTI has three —
AI Insights (`/chatbot/agent/stream`), NLQ search (`/chatbot/agent`) and playbook
nodes (`callXtmAgent`) — and OpenAEV two, `/api/chatbot/agent` and
`/api/chatbot/agent/stream`. None will ever send `supports_tool_approval`, so all
of them get the degraded prose message. That is the correct outcome — but only if
you treat it as prose.

Playbooks already do: `parseStixBundle` rejects the message and the node returns
`output_port: 'unmodified'`, logging a warning. Enrichment stops; nothing breaks.

**Watch for caches.** AI Insights persists the final content to Redis keyed on
`sha256(agent_slug + draft + content)` for `ai:agents_refresh_timeout` minutes —
**24 hours by default**. `extractFinalContent` cannot tell an approval message
from an answer, so a gated tool pins *"I need approval before running X"* to that
entity and replays it to every user for a day, long after an administrator
whitelists the tool.

A degradation that is safe against a chat UI is not automatically safe against a
cache. If you cache agent output, either skip the write for a turn that ended in
approval, or shorten the TTL for it. Tell us if you need a machine-readable
marker to detect that — the current message is deliberately plain prose, and we
would rather add a field than have you regex it.

---


## Path A — mid-turn decision (recommended)

Requires a way to send something to the server while a turn is running. A
WebSocket qualifies; so does SSE streaming plus a POST endpoint, which is what
`filigran-chatbot` already does for steering.

The turn stays open. The agent stops at the gated call, you show the proposals,
the user decides, and the **same turn continues** with the tool result. No lost
context, no restart.

### 1. Server → client: approval required

Arrives on the existing stream, alongside `stream` / `status` / `thinking`
events, using the same `type`-discriminated shape — so it is another case in the
switch you already have, not a new channel:

```json
{
  "type": "approval_required",
  "conversation_id": "9f1c...",
  "proposals": [
    {
      "tool_call_id": "call_abc123",
      "tool_name": "opencti_delete_entity",
      "tool_description": "Permanently delete an entity from the platform.",
      "arguments": { "entity_id": "e-123", "cascade": true },
      "input_schema": {
        "type": "object",
        "properties": {
          "entity_id": { "type": "string", "description": "Entity to delete" },
          "cascade":   { "type": "boolean", "description": "Also delete linked entities" }
        },
        "required": ["entity_id"]
      },
      "source": "integration:opencti"
    }
  ]
}
```

**Render `input_schema` alongside the values.** The per-argument `description` is
what makes a decision informed — `cascade: true` alone is unjudgeable. If you
show only names and values, you have built a rubber stamp.

A turn can propose **several** calls at once, including the same tool twice with
different arguments. `tool_call_id` is the identity; the tool name is not unique.

### 2. Client → server: the decision

POST it the way you already POST a steering message — same idea, different
payload. The XTM One path is `POST /api/v1/platform/chat/messages/approve`; if
you sit behind a proxy, it is whatever that proxy exposes as
`apiEndpoints.approve`:

```json
{
  "conversation_id": "9f1c...",
  "decisions": [
    { "tool_call_id": "call_abc123", "decision": "approve" },
    { "tool_call_id": "call_ghi789", "decision": "reject",
      "rejection_reason": "Wrong target environment." },
    { "tool_call_id": "call_jkl012", "decision": "approve_always" }
  ]
}
```

`conversation_id` is the one the `approval_required` event carried; the calls
are matched by `tool_call_id` within it. There is no separate approval handle to
track — a conversation has at most one turn waiting at a time.

**Every proposed `tool_call_id` must appear exactly once.** A partial set is
rejected — resuming with an undecided call leaves an unanswered tool call that
the LLM providers refuse outright, so the run could not continue at all.

Three verdicts:

| `decision` | Effect |
|---|---|
| `approve` | Runs with the arguments as proposed. |
| `reject` | Does not run. The agent is told, with `rejection_reason`, and can adapt. |
| `approve_always` | Runs, **and** saves a standing approval for this user. |

A decision carries no arguments: the reviewer answers the call the agent
proposed, and a wrong call is corrected by rejecting it with a reason the agent
can act on rather than by rewriting it on their behalf.

### 3. Then

The turn resumes on the same stream — more `stream` events, ending in `done` as
usual. No new conversation turn, and no lost context.

### If the page reloads while a prompt is up

`approval_required` is a single event on a stream. A reload loses it, and with
it the `tool_call_id`s a decision has to name — so ask on mount:

```
GET /api/v1/platform/chat/conversations/{conversation_id}/pending-approvals
```

```json
{
  "conversation_id": "9f1c...",
  "proposals": [ /* exactly what the event carried */ ],
  "turn": "running"
}
```

`turn` is `running` while a turn is still going — including while it is paused —
and `idle` once it has finished. It is what tells you when to stop polling after
answering a recovered prompt, instead of guessing at a window.

An empty `proposals` is the ordinary answer: most conversations are not paused.
A non-empty one means a turn is still waiting, and you can render the same
prompt and POST the same decision as if you had never lost it.

**This matters more than it looks.** Without it a reload leaves a turn waiting
for an answer nobody can give any more, and the user sees a chat that has simply
stopped replying — no error, no prompt, nothing to retry.

A paused turn is kept alive across the disconnect, so the *work* costs nothing:
the same turn resumes with the reasoning and tool results it had already
produced, rather than starting over.

**The answer does not come back on a stream, though.** The resumed turn writes to
the connection that carried the prompt, which your reload closed — so the reply
is persisted to the conversation and nothing arrives live. After submitting a
decision on a recovered prompt, re-read the conversation rather than waiting for
`stream`/`done` events that will not come. Showing your ordinary working
indicator while you poll, and stopping as soon as the transcript grows, is the
behaviour users expect here.

Poll this endpoint rather than the transcript: it is cheap, and `turn` gives a
definite stop condition. When it reads `idle`, fetch the conversation once — the
reply is there.

*(A server-side resume-stream — reattaching a client to a running turn, or
replaying `done` to it — would remove the polling entirely. It needs turn output
on a shared bus, since it is a local queue today and a reconnect can land on
another replica, so it is deliberately not in this contract. Say if you want it
and it can be scoped properly.)*

The one bounded case is a stream that drops and never comes back: after 30
minutes with **no sign of a client** and no decision, the turn gives up and the
pause is cleared. Any request about the conversation counts as a sign — fetching
`pending-approvals` is enough — so a reload restarts that clock rather than
running it down. It is a limit on abandonment, never on the person deciding.

### About `approve_always`

It saves a preference for **that user**, not the deployment, and it applies to
their scheduled runs too, not only the conversation they are in.

**Tell them that before they confirm.** It is the only decision whose effect
outlives the turn, and an uninformed click silently removes the approval step
from unattended automation. A tooltip is not enough.

---

## Path B — end-the-turn (fallback)

Only if you have **no** way to reach the server mid-turn. The turn ends and the
decision becomes a second request.

The send response carries the proposals alongside the assistant message:

```json
{
  "assistant_message": {
    "role": "assistant",
    "content": "I need your approval before running: opencti_delete_entity."
  },
  "pending_approvals": [ /* same proposal objects as above */ ]
}
```

**On the response, not inside the message.** The proposals are also stored in
the assistant message's metadata — that copy is what the resume reads them back
from — but message metadata is not serialised to clients, so a caller reading
only the message would never learn a `tool_call_id` and could not name one in
`decisions`. Absent or `null` means the turn did not pause.

Submit decisions with the same `decisions` array to the chat endpoint —
`POST /api/v1/conversations/{id}/messages` with `decisions` and no `content` —
and the conversation continues from there.

**Decisions and text are mutually exclusive**, and sending both is a `422`. A
resume continues a turn that already has its user message, so there is nowhere
for new text to go: it would reach the model and never be persisted. Send the
decisions, then send the message.

**A resume cannot carry `file_ids`** either (`422`), for the same reason — there
is no new message to attach them to.

**If a turn is already running on the conversation, a resume answers `409`.**
Retry once it has finished. An ordinary message folds into a running turn; a
resume must not, because folding would hand the turn the *text* and drop the
decisions.

**Continuing means the approved calls run with the arguments you were shown**,
their results are appended, and the agent carries on from that point (FR-002c2).
It does not re-plan: a reviewer who approved one action must not get another.
The turn is rebuilt from the stored conversation plus the proposals attached to
it, which is why this works when the process that ran the paused turn is gone —
restarted, or a different replica. That property is the whole point of this path
(FR-002c3); handing a decision to a turn still waiting in memory is Path A.

It is still the lesser path, because the reasoning the model had done before it
stopped is not restored — only the decided calls and their results are.
`filigran-chatbot` has the mid-turn channel already and should use Path A.

**A proposal is not answerable forever.** There is no clock on it — the wait is
on a person (FR-015e) — but it is retired the moment the conversation moves on:
send a new message instead of a decision and the pending set is superseded
(FR-002c4). A decision submitted afterwards is refused, not applied.

**How you learn a set was retired depends on your surface**, and this is worth
reading carefully if you are building against the platform stream.

* **Surfaces that read messages back** — the web application, and any consumer
  of `GET /api/v1/conversations/{id}` — get the retired proposals on the
  assistant message as `expired_approvals`, and render them as expired rather
  than dropping them.
* **The platform stream does not read messages back.** `/chat/sessions` returns
  conversation metadata only (`conversation_id`, `title`, `updated_at`,
  `message_count`), and there is no endpoint that returns `MessageResponse` —
  so `expired_approvals` never reaches you. What you can rely on instead: you
  already hold the proposals from the `approval_required` event, and
  `GET /chat/conversations/{id}/pending-approvals` returning an empty
  `proposals` array is how you learn the set is no longer live. Mark your own
  copy expired at that point; do not discard it silently.

Either way, a gated call that never ran should not vanish from the conversation
that produced it — but on the stream, keeping it visible is the client's job,
because the server has no message for it to ride on.

---

## Errors worth handling

| Situation | Response | What to do |
|---|---|---|
| Partial decision set | `422`, naming the missing ids | Bug — send all of them |
| Approval submitted for a finished turn | `409` | Refresh; somebody else likely answered |
| Approval submitted after the conversation moved on | `409` | The set was retired (FR-002c4). Mark your copy expired — see *How you learn a set was retired* above, which differs by surface — and ask again if you still want it |
| Decision from anyone but the paused user | `404` on the conversation | Only that user can reach it (FR-008a) |

---

## If you would rather not build this yet

Three ways to opt out, all configured in XTM One rather than in your client:

- **Declare the capability at registration** — send
  `"supports_approval_prompts": true` to `/api/v1/platform/register` once your
  assistant can render a prompt, and your tools gate normally because there is
  somebody to ask. A platform that ships support later flips on by itself at its
  next registration.
- **Or have an administrator decide** — Settings → Parameters → *Approval for
  connected platforms*, per platform, built from live registrations.
  `TOOL_APPROVALS_PLATFORM_DEFAULT=skip` seeds that choice at install for
  first-time registrations.
- **Whitelist the tools you use** — Settings → Parameters → *Tools that skip
  human approval*. Per tool. Appropriate when the assistant only does read-style
  work and you want the rest of the integration to keep gating.
- **Turn tool approvals off for the agents you use** — per agent, built-in agents included.

Any of the three makes those calls stop gating entirely.

**Registered platforms, read the first two twice.** The whitelist covers your
platform's *read* tools — searching, listing, fetching, querying — on the same
footing as XTM One's own, so an assistant that only looks things up is largely
unaffected. Everything that changes your platform is not on it and cannot be,
and neither are your consolidated `manage_…` tools, whose behaviour depends on
their arguments. Without a declared capability or an operator's decision, those
all gate at once, across every agent.

Note the two fields, at deliberately different scopes and deliberately
different names. `supports_approval_prompts` on **registration** asks whether
your platform can display prompts at all, and decides whether your tools gate.
`supports_tool_approval` on a **chat message** asks whether that client will
answer an `approval_required` event, and decides whether the turn pauses
mid-answer or ends with the plain message. Declaring the first `true` while an
unattended caller of your own sends nothing per message is the intended
combination, not a contradiction.

Doing nothing is also fine — you get the explanatory message described above.
It is only worse in that a user cannot approve from inside your client; they see
why the agent stopped, and an administrator resolves it.

---

## Open points

1. **~~Is `approval_id` needed?~~** *Settled: no.* Dropped in favour of
   `conversation_id` + `tool_call_id`, which the implementation keys on. A
   conversation has at most one turn awaiting approval, so there was nothing for
   a separate handle to disambiguate: a decision naming calls that are no longer
   pending is answered with 409 rather than mis-applied.
2. **~~Timeout.~~** *Settled: no deadline of ours; we keep the stream warm.*
   Already shipped on both transports — see above. The only thing left is
   OpenAEV confirming its `responseTimeout` semantics.
3. **General assistant.** It has no agent, so no switch of its own: it follows
   the install default (Settings → Security → Tool approvals). Whether a gated call there
   asks or stops depends on the transport, exactly as it does for an agent: over
   the XTM One chat WebSocket it pauses mid-turn and waits for a decision, and
   over the REST message endpoint it ends the turn with the plain message naming
   the tool and where an administrator can exempt it. The platform chat endpoint
   this contract describes always runs a named agent, so nothing here changes for
   you — it matters only if you drive the general assistant over the socket.
4. **~~Which protocol adapters need this.~~** *Settled: REST only.* Both OpenCTI
   and OpenAEV configure `backendType="rest"`, and the legacy path resolves to a
   different package that never reaches this gate. `parseAgUiEvent` needs an
   equivalent only if you know of an AG-UI consumer we do not.
5. **Recovering a prompt after a reload.** The `pending-approvals` GET above is
   additive and needs no new mechanism, but a proxied host has to expose it the
   same way it exposes `approve` — so if `approve` becomes an `ApiEndpoints`
   entry, this belongs beside it.
6. **Does `approve` belong in `ApiEndpoints`?** Proposed above, because a proxied
   host cannot use a fixed path. It changes your public type, so it is your call.
