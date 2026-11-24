# A2A Inbound Server — Operator Guide

How to turn on the A2A inbound server, expose an agent, and hand a partner
what they need. Implements `specs/006-a2a-server-inbound/`.

## Availability

A2A is offered exactly while agentic chat is: AI switched on, and the
**Enterprise Edition** — an Enterprise product (OpenCTI, OpenAEV or OpenCRQ EE)
connected to this platform, or a Filigran license. One rule for both
directions, nothing to enable. Otherwise every A2A route answers `403` naming
the reason, and the connectivity probe on an existing A2A integration reports
the same. The catalog still lists the A2A tiles — selecting one explains what
the deployment is missing, as it does for any other gated provider. The UI
follows `chat_enabled` in `GET /platform/config`. Registering a new partner
creates an integration, which keeps the integration-creation gate (the same
Enterprise Edition answer — `require_agentic_ai_licensed`).

The gate is read per request, so a licence or AI change takes effect without a
restart. Exposures survive it and resolve again once chat is back; automatic
revocation on edition or sharing changes keeps running throughout.

## Reverse-proxy requirement

**Behind a TLS-terminating reverse proxy you must also set:**

```
FORWARDED_ALLOW_IPS=<your proxy addresses>
```

The partner-facing surface — Agent Cards, partner discovery, the JSON-RPC task
surface and delegation callbacks — is served over HTTPS only, and the
application refuses (`403 {"detail": "https required"}`) rather than
redirects any request whose connection it cannot confirm is encrypted.
Behind a proxy the original scheme arrives in `X-Forwarded-Proto`, which
uvicorn applies only from the peers this setting names (an IP, a CIDR or a
comma-separated list naming the proxy as the platform container sees it).
**Without it, every card fetch returns 403 and no partner can register an
agent.** The platform logs each refused route once per process with the peer
and the forwarding headers it saw: `x_forwarded_proto=https` means the peer is
a proxy the platform does not trust, while `x_forwarded_for=present` with no
claim means the proxy sends no `X-Forwarded-Proto` - and, if it is trusted,
that the peer is the client it named rather than the proxy. The security
audit reports an HTTPS `BASE_URL` that no proxy is trusted to confirm.

`TRUST_PROXY_HEADERS=true` also works, but it trusts the header from any peer (see `UPGRADING.md`),
so any client reaching the backend directly could assert its connection was
encrypted simply by sending it: prefer `FORWARDED_ALLOW_IPS`.

`DEBUG` and `A2A_ALLOW_INSECURE_LOOPBACK` exempt real loopback only — never a
request carrying `X-Forwarded-For`, whose peer uvicorn may have rewritten from
that header.

## Tuning

| Setting | Default | Notes |
| --- | --- | --- |
| `A2A_TASK_CREATE_RATE_LIMIT` | 10/min | Per registration. One submission starts a full agent run. |
| `A2A_TASK_READ_RATE_LIMIT` | 1800/min | Per registration. Reads are single indexed lookups, and partners poll — Foundry polls in a zero-backoff loop measured at 8–10/s and reports any 429 as a failed delegation instead of honouring `Retry-After`, so the default gives that measured rate 3× margin. |
| `A2A_UNAUTHENTICATED_RATE_LIMIT` | 60/min | Per client IP, before authentication — covers the anonymous card fetch and jsonrpc requests that fail auth, which the per-registration limits cannot bind. |
| `A2A_DEDUP_WINDOW_SECONDS` | 86400 | A repeat of the same client-minted message id returns the first task. |
| `A2A_RUN_LEASE_SECONDS` | 900 | After this without a heartbeat, the reaper fails the run — unless work the task delegated is still running, which is asked before a task is reported abandoned. |
| `A2A_TASK_MAX_LIFETIME_SECONDS` | 14400 | Total life of a task, distinct from the lease. A task waiting on delegated work keeps its lease alive, so this is the only thing that bounds one whose work never finishes. |
| `A2A_PUSH_MAX_ATTEMPTS` | 5 | Delivery attempts per callback, across the task's whole life. Exhausted callbacks are not attempted again. |
| `A2A_PUSH_CONFIGS_PER_TASK` | 5 | Callbacks one task will hold. A registration past the bound is refused, never evicting an older one a partner is relying on. |
| `A2A_BLOCKING_SEND_TIMEOUT_SECONDS` | 120 | Cap on how long a blocking send holds its HTTP response open waiting for the task to go terminal. On expiry the task returns in whatever state it reached — never an error. Blocking is the v1.0 default (`returnImmediately` opts out) and a v0.3 opt-in (`blocking: true`); the reference SDK's client has no polling loop and relies on it entirely. The create rate limit bounds how many connections one registration can hold. |
| `A2A_BLOCKING_POLL_INTERVAL_SECONDS` | 1.0 | How often a blocking send re-reads the task projection while waiting. |
| `A2A_MAX_REQUEST_BYTES` | 1048576 | Refused on the declared `Content-Length` before the body is read at all. The rate limits bound how *many* requests a caller makes and say nothing about the size of each. |
| `A2A_MAX_MESSAGE_CHARS` | 100000 | Cap on the flattened message text, checked after part validation and before any run starts (refused as `ContentTypeNotSupportedError`). This is the value persisted in `a2a_tasks.inbound_parts` and handed to the executor as the prompt, and the only cap a chunked request — which declares no `Content-Length` — has to clear. |

**Rate limiting is forced fail-closed on this surface**, diverging from the
platform-wide `RATE_LIMIT_FAIL_MODE=open` default. If Redis is unreachable,
external requests are refused with 503. This is deliberate: rate limiting is
the *only* control on external load, so failing open would remove it exactly
when the platform is already degraded. Internal behaviour is unchanged.

The OAuth **token endpoint** (`/mcp/oauth/token`) is fail-closed for the same
reason: it is unauthenticated, advertised to every partner on the agent card,
and spends a bcrypt verification per call, so admitting unbounded work there
while the A2A routes refuse would just move the hole one hop.

## Exposing an agent

> An A2A integration carries a **direction**, and only an **inbound** one
> exposes an agent. Binding an *outbound* integration gives the agent a
> delegation tool instead and exposes nothing — see
> [`a2a-client-operations.md`](./a2a-client-operations.md). Integrations
> created before direction existed are stamped `inbound` by migration. If an
> agent is unexpectedly unreachable, check the integration's direction first.

**Exposure is implicit in binding an inbound A2A integration.** There is no
separate switch: an agent is reachable by partners exactly while it binds at
least one **inbound** integration whose provider is in
`app/a2a/providers.py` — OAuth A2A, Foundry A2A or Google Agent Platform
A2A — on the agent configuration's Tools tab. Bind one and the
agent is exposed (a card token is minted); remove the last one and the
exposure is revoked, retiring the card URL exactly as the old explicit toggle
did. The invariant is maintained by a SQLAlchemy flush hook (`app/a2a/hooks.py`),
so it holds wherever bindings are edited — the agent API, an import, a flow —
rather than only on the path someone remembered to wire.

Binding authority is agent-edit authority: the same `writable_agent_error`
gate that governs editing the agent or attaching an assignment to it.

**Built-in agents can be exposed, by an administrator, on the Tools tab like
any other agent.** Exposure is not a special power here: it is an integration
binding, and in XTM One an administrator configures a built-in's integrations
generally — its OpenCTI account, its GitHub, its A2A partners. Since the
built-ins are the product's own agents, this is usually the binding you
actually want.

Everything *else* about a built-in stays read-only, admins included. The
read-only rule guards the agent's seeded definition — persona, name, tools,
model, memory — which is rewritten from seed data on every restart, so an edit
there would silently drift back. Integrations are not part of that definition;
they are the deployment's own accounts. The carve-out admits an update only
when integrations are the *only* thing it changes: one that also moves a seeded
field is refused, whatever else it carries.

> **One residual drift.** `integration_template_bindings` — the list of
> providers a built-in resolves per user — *is* rewritten by the resync. It is
> editable anyway, because the Tools tab cannot avoid sending it (binding a
> fixed integration for a per-user provider moves it out of that list). A fixed
> binding you add survives the restart and outranks the per-user entry at run
> time, so the restored entry is inert. What does come back is a provider you
> removed outright.

**Which integration to bind:**

| Provider | Credential | Use when |
| --- | --- | --- |
| **OAuth A2A** | This platform mints a client id / secret; the partner exchanges them for a short-lived token at the card's token URL | The partner can re-run an OAuth exchange itself (Google ADK / Vertex) |
| **Foundry A2A** | The partner presents its own Microsoft Entra token, verified against pinned facts | The partner cannot refresh a client-credentials token (Azure Foundry) |

Both may be bound to one agent; each admits its own partner.

### Run-as identity — inherited, not chosen

External work runs as the agent's **owner**, and for a company-managed agent —
a built-in included — that is whoever seeded it (usually an administrator).
Because exposure follows a binding, there is no moment at which to demand an
explicit choice, so the identity is inherited and then **stated plainly** on the
agent's Tools tab, in the Integrations list, and in the audit trail.

For built-ins this resolves to one account for all of them: the seed owner,
which is `ADMIN_EMAIL` where it is configured and otherwise the oldest admin.
**Check what that account can see before binding** — it is the reach every
partner tasking a built-in inherits.

**This is an accepted trade-off, and it is the one to check before binding:**
the partner sees exactly what that user sees — their personal and
group-shared data included. If that is wrong for a given agent, the agent
should not be exposed, or should be duplicated under an owner whose reach is
appropriate. An agent with no owner at all is never exposed: resolution fails
closed rather than inventing an identity.

### What a partner's task can reach

**Everything the agent can, as the run-as user, except the platform-builder
surface.** An exposed agent answers a partner with its own full configuration —
every built-in and custom tool, every MCP server, every knowledge base, both
halves of its integration bindings, and every peer it may hand over to
(consult, transfer or background delegation alike). The boundary is the
identity above, not a tool list, so **the check to make before binding is what
that user can reach**. There is no per-exposure tool allow-list.

The one narrowing: a partner is an external caller, like a channel
sender with no account, however privileged the run-as identity is. The A2A
executor runs the task in a partner-task scope (`a2a/partner_scope.py`), which
every in-process hop inherits with the asyncio context and a background
delegation carries in its execution payload (`a2a_partner_task`). In it:

| Surface | What the task gets |
| --- | --- |
| Trace inspection (`inspect_my_traces`, `inspect_agent_traces`, `get_trace_payload`) | Nothing, explicit or implied by `agent_builder` (`agent_chat._turn_trace_tool_ids`, `traces.may_read_agent_traces`): an agent's traces hold its users' conversations |
| `agent_builder` | Its reviewed read half only (`mcp_xtm.XTM_MCP_READ_TOOLS`), acting as an anonymous caller: company-managed entities, no write tool wired (`agent_chat._platform_builder_actor`) |
| `list_runs`, `get_run` | The run history of the same partner's own tasks only (`a2a/partner_runs.py`): the runs its A2A tasks started and the runs those spawned through `parent_run_id`, each naming its partner task; the delivered result only for the run a task maps to, never a prompt, tool call or trace. Scoped by the partner principal (exposure and OAuth client, carried by the scope and by a delegation's `a2a_partner_principal` payload key), never by the run-as identity; any other run answers like an unknown id |
| `list_work_items` | The tickets the same partner's own tasks filed only (`a2a/partner_runs.partner_work_items_clause`): the items whose `origin_run_id` is a run of that partner's run tree, on top of the desk's scope, so one call reads the status of every ticket the partner filed. Another partner's tickets and the run-as identity's other items are never listed |
| `flow_builder`, the evaluation tools | Not wired |
| `computer_use` (runners) | Not wired, withheld with its reason so the model is told why (`partner_scope.withhold_runner_tools`, called by the agent chat and the assignment builders): the suite would drive the run-as identity's enrolled machines. No exposure opts in |
| Everything else | Unchanged, as the run-as user |

A channel sender with no account gets the same builder surface.

Two things follow that are worth knowing before you bind:

- **Shared state is shared.** `shared_state` is read by internal users of the
  same agent. If that is wrong for a given agent, don't expose it, or
  duplicate it without that tool.
- **Handover leaves the partner's own scope.** A peer reached by handover need
  not itself be exposed to that partner, and runs its own full configuration as
  the same run-as user. A partner tasking one agent therefore reaches the
  transitive closure of its handover peers, up to the delegation depth cap (5).

**A transferred answer is attributed.** When a handover moves the work off the
agent the partner addressed, the result artifact carries
`metadata.respondingAgent` naming the agent that produced it — the peer's name
included, since the partner reaches that peer by the operator's own binding.

**A pipeline entry answers with its last stage's output.** `mandatory_transfer`
is offered like any other handover type, so an agent wired as the entry to a
chain — the seeded detection-rule agents are the shipped example — hands every
submission down that chain and returns the terminal stage's result, attributed
as above. Expect the wall clock to scale with the number of stages: each gets
its own agent-loop budget.

**A run that exhausts its clock FAILS**, with "External task exceeded its
execution budget".

**Connected integrations reach external work in both their forms.** An agent
binds a provider either to a fixed integration or "per user", the latter
resolved at run time against whoever is running — and the built-ins use the
per-user form exclusively for OpenCTI, OpenAEV and OpenCRQ. External work
resolves it against the run-as user under the platform's ordinary eligibility
rule: that user's own integrations plus genuinely company-wide ones, never a
colleague's personal account. A provider that user has no eligible integration
for is simply absent from the run.

### What the partner credential can and cannot do

An **OAuth A2A** integration mints one client id / secret when it is created;
the secret is shown exactly once (stored only as a bcrypt hash) and can be
regenerated from the integration, which retires the previous one everywhere at
once. **One credential per partner relationship, valid on every agent the
integration is bound to** — so a leaked secret reaches all of them, and a
narrower blast radius means a separate integration per agent. Credentials
minted by the older per-agent flow keep working and stay scoped to their one
exposure.

Exchanging it at `POST /mcp/oauth/token` (`grant_type=client_credentials`) yields
a bearer token valid **only** on the A2A task surface of the agents that
integration is bound to. The grant stays mintable while **any** bound agent
resolves, and the token is not narrowed to one of them: the task surface
re-checks the binding per request, which is where an unbinding takes effect.
A 401 from the token endpoint therefore means one of exactly three things —
wrong client id / secret, the integration reaches no live exposure (nothing
bound, or every run-as user deactivated), or the credential was regenerated
and the partner still holds the old secret. It is not a platform session and not an MCP credential:
the token carries `type: a2a_client`, which the MCP endpoints refuse, and
`scope: a2a:task`, which the platform session dependency refuses. Both are
properties of the token itself rather than checks on the surfaces it must not
reach, so a surface added later is closed by default.

Unbinding the integration from an agent stops that credential on the agent's
next request, exactly as it stops the Entra one — the check is against the
agent's *current* bindings, not against anything frozen at mint time.

### Microsoft Entra federation (optional, bound like any other integration)

The OAuth credential above assumes the partner's platform can re-run the
client-credentials exchange every hour. **Neither target platform can.**
Foundry's connection dialog offers only static header values or Entra
identities; a minted 60-minute token pasted as a static header dies within the
hour and surfaces on Foundry's side as failed delegations. Entra federation is
the supported answer for Azure-side partners: Azure mints and refreshes the
partner's token itself, and this platform accepts it directly.

The three pinned facts live in a **Foundry A2A** integration (Integrations catalog, provider `entra_a2a`, beside Microsoft
Entra ID; offered while agentic chat is). There is no
A2A-specific binding surface, no dedicated endpoint and no exposure-side
state: an agent opts in by binding the integration through its **ordinary
integration bindings** — the agent configuration's Tools tab — exactly like
any other integration. The binding confers no tool an operator chooses, so
it means very nearly one thing: this agent's exposure admits that partner's
Entra tokens. The exception is the same one every inbound A2A binding carries —
``ask_the_caller``, offered only while an external task is running. One integration is one partner relationship — configured once, then
bound to any number of agents — and one agent may bind several partner
configurations; the auth path tries each until one admits the token.

Setup, in order (the customer does the Azure half):

1. In the partner's Entra tenant, create a **dedicated app registration**
   representing this platform, and note its Application ID URI (`api://…`) —
   that is the **audience**. Grant the Foundry project's managed identity (or
   whichever identity will call) access to it.
2. On the Integrations page, create a **Foundry A2A** integration with `tenant_id` (the partner's directory GUID), `audience`
   (from step 1, byte-for-byte), and `client_id` (the calling identity's
   application/client GUID). The config is validated on save (422): missing
   fields, non-GUID values and tenant-wide audiences are refused here, in
   front of the operator — not as unexplained partner 401s later. Ordinary
   integration visibility applies (own / group-shared / company-managed) —
   share it however the fleet is managed.
3. Bind it to the agent: agent configuration > Tools tab > Integrations.
   Binding requires agent-edit authority — the same `writable_agent_error`
   gate that authorizes exposing the agent. Unbinding it there turns
   federation off for that agent on the partner's next request; the
   integration itself is untouched and stays bound elsewhere.
4. Once bound, the **agent card URL** — the URL the partner platform
   registers — appears on the agent's **Tools** tab, in the Integrations list. Binding is
   what exposes the agent, so there is no switch to turn on first; if the
   card shows no URL, the exposure was refused or has since been revoked
   (see *Revocation reasons* below) and re-saving the binding recreates it.
5. In Foundry, pick **Microsoft Entra** in the A2A connection dialog and
   register the card URL from step 4. No secret is stored on either side,
   and nothing ever needs re-pasting.

The integration's **Test Connection** button runs a real preflight
(`app/integrations/providers/entra_a2a.py`): the pins re-validated with the
auth path's own parser, the audience checked against the legal `aud` shapes
(GUID / `api://…` / `https://…` / `urn:…` / `spn:…` — a bare word is a typo
no token will ever match), the pinned tenant's JWKS actually fetched from
Microsoft (catching a typo'd tenant GUID or blocked egress before the
partner's first 401), and the binding state reported — valid pins bound to
no exposed agent come back as a "works, with a caveat" verdict. What no
test can prove is whether a well-shaped audience (or the allowed caller) is
the RIGHT one: that needs a credential in the partner's tenant, which this
design deliberately never stores. The green verdict says so, and the
partner's first mismatched token is logged with **both** sides
("token carries aud=…, the pinned audience is …") so the fix is one read of
the WARNING log away.

What is enforced per request: RS256 signature against the **configured**
tenant's published keys (never a URL derived from the token itself), issuer in
both spellings that tenant admits (`login.microsoftonline.com/{tid}/v2.0` and
`sts.windows.net/{tid}/` — which one arrives is decided by the customer's app
registration, not the caller), exact audience, and the pinned caller matched
against `azp`/`appid`/`oid`/`sub` across token versions. The pins are re-read
from the agent's bound integrations and re-validated on every request: an
edit re-pins every bound agent immediately, deleting the integration (or a
config edited into an invalid or shared-audience state) fails closed — the
Entra credential form stops being admitted while the OAuth credential keeps
working. Tenant-wide audiences (Microsoft Graph, ARM) are refused both at
save time and per request: any app in a tenant can mint tokens for those, so
they would let an unrelated token double as a partner credential.

Operationally it behaves exactly like the OAuth path: same registration
`client_id`, same rate budgets, same task scope and audit attribution, same
uniform refusal on any failure. The one asymmetry: a refusal's *cause*
(unreachable JWKS, wrong audience, unpinned caller) appears only in the server
log at WARNING — look there when a partner reports Entra 401s, because the
wire deliberately says nothing. Requires outbound reachability of
`login.microsoftonline.com`; the key fetch happens about once an hour per
tenant (cached JWKS, 5 s timeout, fail-closed).

Three lessons from the first live setup (Foundry, 2026-08-24), each of which
cost a debugging round:

- **Pin what the token carries, not what a portal blade shows.** The caller
  GUID Azure displays for an identity is not always the one its tokens
  present. When a partner reports 401s, decode a captured token's claims
  (`aud`, `iss`, `appid`/`azp`/`oid`) and configure from those — every
  mismatch is also named precisely in the server's WARNING log.
- **Re-exposing the agent does not touch the pins.** They live on the
  integration, not on the exposure, so an unbind/re-bind cycle rotates the
  card URL and leaves federation configured — only the new card URL needs to
  reach the partner. Turning federation off is unbinding the integration
  from the agent.
- **Expect the partner's caller GUID to have a lifecycle.** See the Foundry
  observations below: their identity follows the Foundry agent object, and
  rebuilding that object means re-pinning here. A re-pin is one edit of the
  federation integration — every agent bound to it follows on the partner's
  next request.

### What a federated partner may call

The federation integration's **Configuration** tab lists the operations a
partner may invoke and how they are used, so the answer to "what can this
partner actually do?" is on the config the operator is already looking at.
The list is the catalog entry's `available_tools` and is pinned in tests
against `_OPS_BY_METHOD` — the surface's own vocabulary — so an operation
cannot be served without being documented, or documented without being
served.

| Operation | v0.3 · v1.0 | Type | Budget (default) |
| --- | --- | --- | --- |
| submit a task | `message/send` · `SendMessage` | write | 10/min per registration |
| poll state + artifacts | `tasks/get` · `GetTask` | read | 1800/min per registration |
| cancel the run | `tasks/cancel` · `CancelTask` | write | 1800/min per registration |
| register a callback | `tasks/pushNotificationConfig/set` · `CreateTaskPushNotificationConfig` | write | 1800/min per registration |
| read, list, remove a callback | `…/get`, `…/list`, `…/delete` · `Get…`, `List…`, `Delete…` | read | 1800/min per registration |

`ListTasks` is the one method in either vocabulary this surface does not serve,
and it answers `UnsupportedOperationError`. Partners hold the ids of the tasks
they created.

These are **inbound** operations. Binding the integration to an agent is
the federation opt-in, and it adds no tool an operator selects; the one tool it
does bring, ``ask_the_caller``, is registered by the external-task executor
while a partner's task runs and is gone the moment it ends.

## What external work can reach

External work runs as **one named user**, resolved when the agent becomes
reachable, and sees exactly what that user sees — the platform's ordinary visibility rules,
with nothing A2A-specific layered on.

| Agent | Runs as |
| --- | --- |
| Personally owned | its **owner**. Not choosable — exposing an agent is not a way to run it as someone else |
| Company-managed (built-ins included) | its **stored owner** — whoever seeded it. Always inherited, never choosable: exposure follows a binding, so there is no moment at which to ask |
| Any agent with no owner at all | nothing. Resolution fails closed and the binding produces no exposure |

The inherited default is the accepted trade-off of exposure being implicit, not
a recommendation. Its stored owner is usually an administrator, so a partner
tasking that agent sees that person's personal and group-shared data. That is
why the identity is printed on the exposure card rather than left implicit —
**read it before sending a partner the URL.**

It is never the actor whose edit exposed the agent, when the two differ — a
configuration-tier group member may expose a colleague's agent, and it still
runs as the owner.

Administrator status does not widen anything: the execution path resolves
visibility with the admin bypass off, so naming an administrator gives ordinary
user visibility (their own, company-managed, and group-shared resources), not
platform-wide reach.

**Quota** is charged to whoever the exposure runs as, the same identity that
appears in run visibility and the activity feed. When that allowance is already
exhausted, `message/send` refuses with `QuotaExhaustedError` before any run
starts; an exhaustion that races a running task fails the task with the cause
in its status message. Both paths are audit-logged naming the client id, so
the consumption is attributable to the partner integration rather than to the
run-as user's own work.

## A task can outlive its own run

An agent may hand work to a peer or to a copy of itself, and that work keeps
running after the agent's own reply. The task stays `working` until it finishes,
and each finished piece arrives as its own `delegated-result` artifact,
attributed to the agent that produced it.

Three consequences for an operator:

- **A stale lease is not enough to reap a task.** The reaper asks whether
  delegated work is still running before reporting one abandoned, because
  nothing in the serving process heartbeats for work running on a worker.
  `A2A_TASK_MAX_LIFETIME_SECONDS` is what bounds a task whose work never
  finishes, and its failure reads "exceeded its maximum lifetime".
- **`tasks/cancel` stops the children too.** A task reported `canceled` has no
  work still running: the run, the runs it transferred the turn to (recursively),
  every one of their sub-agent tasks, and every partner task any of them
  delegated onward. A running turn learns of the cancel within 15 seconds.
  A background task stopped this way says the partner cancelled it
  (`cascade_cancel(reason=...)`), never that its run was deleted.
- **A run that exhausts its own budget FAILS** with "exceeded its execution
  budget", rather than completing with the agent loop's apology text as its
  result.

## An agent can ask its caller a question

A2A blocks a task on its **client**, and the client of an exposed agent is the
calling agent — not a person. An exposed agent given something underspecified
calls `ask_the_caller`, the task reports `input-required` with the question as
its status message, and the caller answers by sending a message that names
that task id. The same task continues under a new run, which is handed the
question and what prompted it, so the agent that answers is the one that
remembers asking.

The run is told who asks. The executor hands it a consult by the partner
(`tasks._PARTNER_HANDOVER_CONTEXT`), so its prompt says an external agent
asked and the answer goes back to it, and its identity line reads "Acting on
behalf of" the run-as user, as an assignment's does: that user is whom the run
acts for, not whom it talks to, so a question it needs answered goes to
`ask_the_caller` rather than into its reply. The run still reaches what the
run-as user reaches.

- **Nothing parks for a human.** No path on this surface waits for a person,
  and the tool says so: an agent that needs a human decision cannot ask for
  one. `awaiting_human` remains the assignment engine's, and no external run
  reaches it.
- **One task, however many rounds.** A continuation is the same task and the
  same context; a message naming a task the caller does not own is answered
  not-found, and one naming a task that is not asking is refused with a
  reason.
- **Bounded at three rounds.** Past it the tool is not offered and the agent
  answers with what it has — two agents can ask each other for precision
  indefinitely, and neither gets bored.
- **The answer replaces nothing.** A task waiting on its caller carries no
  result; whatever the agent wrote after asking is not sent, because it would
  read as one.
- **The run pages show the exchange.** The run that asked is `completed`, so
  the run responses carry `a2a_exchange` (`a2a/run_exchange.py`): the question
  it asked and where it stands (`waiting`, `answered`, `withdrawn` by a cancel),
  and, on the run an answer started, the question and answer. `answer_question`
  stamps `a2a_previous_run_id` on the new run and `a2a_answered_by_run_id` on
  the run that asked, so each links to the other; a linked run the viewer
  cannot open is not linked. The status reads "Waiting on caller" while asking.

## A conversation spans tasks

`contextId` identifies a conversation, and the platform mints every one — a
submission naming an identifier this deployment never issued is answered exactly
as one naming another partner's, so a partner cannot probe which exist.

A context records which agent answers it, and an agent handing the conversation
over by choice moves that. A follow-up therefore reaches the agent responsible
rather than the one originally addressed, under the card the partner first
validated against.

**A pipeline does not move it.** `mandatory_transfer` routes one message through
its stages and leaves the entry agent responsible — those stages are usually
unexposed, so recording one would refuse every follow-up in that conversation. Three refusals,
each before any run starts:

| Condition | Answer |
| --- | --- |
| the identifier was never issued, or belongs to another registration | the uniform not-found |
| the responsible agent is no longer bound to this partner's integration | `UnsupportedOperationError` — responsibility recorded once is not consent forever |
| the previous task in that conversation has not finished | `UnsupportedOperationError` |

## Callbacks

An accelerator. **Polling is the floor and always works**, so a failure here
costs a partner its early notice and never its result.

**Callbacks.** A partner registers one on a submission
(`configuration.pushNotificationConfig`) or afterwards on the task, and receives
the task's id, context and state when it moves — no content, so it reads the
result through `tasks/get` with its own credential. The platform holds no key of
its own for this: it presents back the `token` or the scheme and credentials the
partner registered, both Fernet-encrypted at rest. A callback address that
resolves inside the deployment's own network is refused at registration and
re-checked before every send, because DNS moves.

A task that ends without running to completion — cancelled, or reaped after its
replica died — is notified too, from the path that ended it. A task that
outlives its own run, because the run delegated work that is still going, is
announced by the reaper sweep instead: it stays `working` until that work
finishes, and by then nothing is executing it to notice. Expect up to
`A2A_REAP_INTERVAL_SECONDS` (60) between the child finishing and the partner
being told.

**Ticket status pushes.** A ticket (work item) created by a run of a partner
task's run tree is the partner's ticket (the same tree the bulk read of
`list_work_items` is scoped to). When it changes status (`open`, `in_progress`,
`blocked`, `done`, `cancelled`; `claimed` is dispatch bookkeeping and is not
sent), every callback the partner registered on the originating task receives
`{"kind": "work_item.status_changed", "taskId", "contextId", "workItemId",
"status", "previousStatus"}` plus its `token`: identity and status only, never
the title, a note or the result. One attempt per change, no retry, and the
callback's attempt budget (the task's own announcement) is neither spent nor
reset; a callback whose budget is spent is not called. The bulk read stays the
floor (`backend/app/a2a/ticket_push.py`).

## Automatic revocation — expect this

An exposure is revoked automatically when any of these happen. The partner's
card URL and credentials both stop working.

| Change | `revoked_reason` |
| --- | --- |
| Agent transferred to a different owner (personally-owned agents only) | `owner_change` |
| Agent switched between personal and company-managed, **either** direction | `tier_change` |
| Agent's sharing changed — group **or per-user** grants, the sets or their permission tiers | `sharing_change` |
| Agent disabled or deleted | `agent_disabled` |
| Last A2A integration unbound from the agent | `unbound` |
| Operator turned the exposure off (legacy explicit toggle — the endpoint no longer exists, so no new row gets this) | `manual` |

An owner transfer counts because a personally-owned agent's run-as identity
*is* its owner: leaving the exposure live would keep running the partner's work
inside the previous owner's data boundary. A **company-managed** agent's
`user_id` is provenance rather than boundary — the run-as identity was resolved
once and pinned on the exposure — so transferring one moves nothing a partner
can reach, and it deliberately does not revoke.

`unbound` is the ordinary way to stop being reachable now: remove the A2A
integration from the agent's Tools tab. **Upgrading also converges existing
installs**: a one-time migration (`a2a2intmodel1`) revokes any exposure whose
agent binds no A2A integration — rows created by the old explicit toggle, which
would otherwise stay reachable with no binding to show for them and invisible
in a UI that now derives "exposed" from bindings. Exposures whose agent does
bind one are left exactly as they are, so live partner integrations survive the
upgrade untouched. Re-binding mints a NEW card URL —
the retired one never resolves again — so the partner has to be sent the new
one.

Per-user grants count for the same reason group grants do: `can_manage`
honours a direct `config` grant, so the grant may be the entire authority
under which someone exposed the agent. Flipping `access_restricted`
does **not** revoke, deliberately: it narrows a company-managed agent's
internal audience, while the exposure's boundary (the run-as user) and the
authority to expose (administrator-only) both stay put.

This surprises people, so plan for it: **re-binding mints a new card URL**,
which must be sent to the partner again. The credential itself lives on the
integration and survives — only the per-agent URL rotates. Nothing from the
previous exposure ever resolves again. The agent's owner is notified in the
activity feed whenever this fires, with the reason.

## Deactivating a user stops their exposures — without revoking them

Deactivating a user immediately stops every exposure that runs as them from
resolving: the card 404s, the task surface 404s, and the token endpoint returns
`invalid_client`. This takes effect on every replica the moment the flag flips —
there is no revocation to wait for, and no cache to clear.

It is deliberately **not** a revocation, which is the one difference from the
table above: reactivating the user restores the exposure with the same card URL
and the same credentials. A suspension that permanently destroyed a partner
integration would be a worse answer than one that pauses it. So there is no
`owner_deactivated` reason to look for in `revoked_reason` — an exposure that has
gone quiet for this reason still reads as live in the UI, and `GET
/api/v1/agents/{id}/a2a` still shows it, which is what lets you find it and
unbind the integration deliberately if the person is not coming back.

Deleting the user is the permanent form: the `ON DELETE CASCADE` on
`run_as_user_id` removes the exposure row outright.

## Observability

| Series | Use |
| --- | --- |
| `a2a_tasks_submitted_total{company_managed,duplicate}` | External load, separable from internal |
| `a2a_tasks_completed_total{state}` | Terminal outcomes; a failure spike is a partner integration breaking |
| `a2a_requests_refused_total{reason}` | A spike in `unauthenticated` is what credential probing looks like; `rejected_platform_key` is an internal integration still presenting its `fcp-`/`mcp-` key here (also audit-logged); `quota_exhausted` is a partner burning the run-as user's allowance |
| `a2a_exposure_changes_total{action,reason}` | Reachability changes. `action="expose",reason="bound"` is an operator binding an A2A integration; `action="revoke"` carries the revocation reason — `unbound` for a deliberate unbinding, and `tier_change` / `sharing_change` / `owner_change` / `agent_disabled` for the automatic teardowns. Both directions are decided in the flush hook and counted only when the transaction carrying them commits, so every durable change is counted exactly once whichever path edited the bindings, and a failed flush, a rollback (a rolled-back SAVEPOINT included, or a `begin_nested()` block whose flush failed), a failed COMMIT or a session closed without committing counts nothing. Not counted at all: an exposure removed by deleting its agent (the row goes by `ON DELETE CASCADE`) and any bulk `update`/`delete` statement on agents, which never reaches the flush hook (none exists today) |

Every exposure change, external submission and denial is also audit-logged
under entity type `a2a_exposure` / `a2a_task`.

**A callback that stops working is otherwise invisible.** Delivery is
best-effort, so a partner going dark raises nothing: `a2a_push_deliveries_total`
carries `delivered` and `failed`, and `a2a_push_abandoned_total` counts the
callbacks this platform has stopped trying — that second series is the one worth
alerting on, because one failure is a blip and this is the platform giving up.
Each abandonment also writes an audit entry naming the client, the task and the
address, since "why was this partner never told" is asked about one task and one
endpoint, which a counter cannot answer.

### Whose activity is whose

An A2A integration's **Activity** tab and its **Last Activity** figure both show
only what *that* partner submitted, scoped by the registration recorded on each
run — never by "runs of the agents this integration is bound to". The
distinction is not cosmetic: binding both A2A integrations to one agent is the
ordinary case (one agent serving a Google partner and an Azure one), and an
agent-scoped view shows each of them the other's traffic plus every internal run
of that agent.

In the agent's own activity log, and anywhere else runs are listed, an
externally submitted run carries a **badge naming the A2A provider that
submitted it**, so partner traffic is never mistaken for a scheduled or manually
triggered run.

That badge is resolved **server-side, once per run**, from what the calling
registration is *owned by* — never from what its id looks like. Both credential
forms mint `a2a-<random>` ids, so a shape-based guess disagrees with itself
across pages: the same run reads "Foundry A2A" on the integration claiming it
and "OAuth A2A" in the agent's activity log. One resolution keeps every surface
consistent.

**A run can read plainly `A2A`, with no provider named.** That is an answer, not
a gap, and there are two ways to reach it:

* **A legacy per-exposure credential.** A partner federating against an exposure
  created by the *old explicit toggle* authenticates with an Entra token but
  calls under that exposure's OAuth-shaped registration — deliberately, so one
  partner keeps one rate budget and one task scope however it authenticates.
  That one registration therefore served both credential forms and no column
  records which was presented. Submissions made since record the form directly
  and are named exactly; **older ones cannot be, because nothing captured it at
  the time.**
* **A deleted credential.** Revoking an exposure deletes the credentials bound
  to it, so a run can outlive every row that said whose it was.

Neither is hidden — an unattributable run still appears on the integration whose
registration it used. It is simply not labelled with a partner the platform
cannot actually vouch for. Both are confined to history: an exposure created by
binding an integration has no shared registration to be ambiguous about, and its
credential lives on the integration rather than dying with the exposure.

## Observed partner behaviour — Azure Foundry (Preview, 2026-08)

Measured against a live Foundry agent; these observations shape the defaults
above and set operational expectations.

- **Polling is a zero-backoff loop at 8–10 `tasks/get` per second**, and any
  429 is surfaced to Foundry's user as a failed delegation — `Retry-After` is
  not honoured. This is why `A2A_TASK_READ_RATE_LIMIT` defaults to 1800/min.
- **`tasks/cancel` is never sent.** Cancelling a delegation in Foundry stops
  its polling and nothing else: the task here runs to completion and the
  run-as user's quota is still charged. A capture of ~2000 consecutive
  requests during cancelled-and-completed delegations contained zero cancels.
- **The result is read from `history`/`status.message`, not `artifacts`.**
  Completed tasks mirror the result into all three (see `_render_task`); a
  task whose answer lived only in artifacts reached Foundry's user as "no
  answer returned".
- **Card parsing is strict.** `preferredTransport` is required, and every
  object carries its v0.3 `kind` discriminator — the reference python SDK
  defaults these and so cannot catch their absence; Foundry is the parser
  that bites.

Entra connection behaviour (observed 2026-08-24, first working federation):

- **The caller is an "Agent Identity", not the project's managed identity.**
  Tokens arrive from a service principal named
  `<resource>-…-AgentIdentity` (`servicePrincipalType: ServiceIdentity`) that
  Foundry mints in the *customer's* tenant for the agent object. It is stable
  while that Foundry agent lives and **rotates when the agent or its Foundry
  resource is deleted and recreated** — each rebuild needs a re-pin of the
  federation integration's allowed caller. Editing or reconnecting the
  connection does not rotate it.
- **Tokens are v1.0** (`iss: sts.windows.net/{tid}/`), which is why the
  verifier accepts both issuer spellings. Unusually, `appid`, `oid` and `sub`
  all carry the same GUID — the four-claim caller matcher means any of them
  satisfies the pin.
- **The configured audience is honoured.** Foundry mints for the dedicated
  app registration's `api://…` URI exactly as configured in its connection
  dialog; the card fetch stays anonymous (leave Foundry's "authenticate when
  fetching the agent card" unchecked).
- **The tenant is the whole minting boundary unless the customer narrows
  it.** The resource app is created with `appRoleAssignmentRequired: false`,
  so *any* application in the tenant can obtain a token for the audience —
  the exposure's caller pin is what narrows it to the partner. Flipping
  *assignment required* on the enterprise app is worthwhile Azure-side
  hardening, and would also make caller churn visible in Entra rather than
  as 401s here.

## Observed partner behaviour — Google ADK / Vertex (2026-08)

Observed against ADK 2.6.2's `RemoteA2aAgent` (the same client Agent Engine
runs). Its posture is the OPPOSITE of Foundry's on both axes that matter:

- **It speaks the v1.0 binding** (`SendMessage`, proto-JSON shapes), which
  is also what an undeclared card fetch is served, so the card and the wire
  agree without the client declaring a version.
- **It never polls.** The reference client makes exactly ONE send call per
  delegation and renders whatever task state comes back as the final answer
  — there is no polling loop in the SDK at all. It relies on v1.0's
  blocking-send default (`returnImmediately` unset): before the server
  honoured that flag, a task returned as SUBMITTED reached the ADK user as
  an empty reply while the run completed unread. This is why blocking send
  exists and why `A2A_BLOCKING_SEND_TIMEOUT_SECONDS` must comfortably cover
  a typical agent run — a run that outlives it renders as WORKING, which
  this client treats as final.
- **A sub-agent transfer sends the TRANSCRIPT, not a question.** When the
  remote agent is wired as an ADK `sub_agent` (transfer semantics), the
  message arrives as one part per session event, interleaved with literal
  `"For context:"` parts — including the caller's own tool-call log. The
  exposed agent sees a transcript addressed to someone else and answers
  that (observed 2026-08-23: an agent with its own specialists tried to
  action the caller's `transfer_to_agent` line). The server flattens parts
  faithfully and must not special-case one client's conventions; the fix
  belongs on the consumer, which should wrap the remote agent as an ADK
  `AgentTool` so its LLM composes one self-contained question. Worth telling
  partners who report "the agent answers strangely".

## Known limits this phase

- **No API for exposing, revoking, or re-scoping.** Binding an A2A integration is the whole surface, and `GET /api/v1/agents/{id}/a2a` is read-only. A second way to reach the same state would disagree with the binding the moment either was used, and an exposure created without a binding is reachable with nothing to show for it in the UI. The gap this leaves: a run-as user cannot be nominated, so an exposed agent always runs as its owner.
- **No task listing.** Partners hold the ids of tasks they created and read them individually.
- **Cards are unsigned.** Neither target platform verifies signatures today; the representation stays forward-compatible so adding one later needs no partner re-registration.
- **The card is version-shaped, including its security requirement** (`security` under v0.3, `securityRequirements` under v1.0). A partner that reports "the agent says it needs no authentication" is reading a card rendered for the wrong version, not a misconfigured exposure.
- **On the task surface, the METHOD NAME decides the response's dialect, not the `A2A-Version` header.** The two versions have disjoint JSON-RPC vocabularies (`message/send`/`tasks/get`/`tasks/cancel` vs `SendMessage`/`GetTask`/`CancelTask`), and the reference SDK's v1.0 client sends no version header at all — the header only shapes the card route. A partner erroring with "Unsupported method" on a v1.0 name is running a build that predates the v1.0 binding (2026-08); a v1.0 partner failing to *parse* responses (enum spellings, part fields, the `SendMessageResponse` envelope) is the same vintage.
- **JSON-RPC only.** No gRPC or HTTP+JSON binding.
- **No bound on internal responsiveness under external load**, and no cap on concurrent external runs. Rate limiting is the only control; total external submission rate is bounded by (exposures x per-registration limit). If external load is later seen to affect internal users, a concurrency cap is the cheapest addition and only narrows, so it will not break a registered partner.
