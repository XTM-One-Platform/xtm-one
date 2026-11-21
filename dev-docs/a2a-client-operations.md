# A2A client (outbound) — operations

How to point an XTM One agent at a partner's A2A agent, and what to do when it
stops working. The inbound counterpart is
[`a2a-server-operations.md`](./a2a-server-operations.md).

Offered exactly while agentic chat is — AI switched on and **Enterprise
Edition** (a Filigran license, or a connected Enterprise Edition OpenCTI,
OpenAEV or OpenCRQ) — by the same gate as the inbound half, nothing to
enable. Otherwise the delegation tool is offered to no agent, no verification
sweep runs, and the management endpoints answer `403` naming the reason.
Registering a partner creates an integration, which keeps the
integration-creation gate (Enterprise Edition). Whether a given agent
can reach a given partner is decided by direction and binding, below.

## Direction, first

An A2A integration carries exactly **one** direction — inbound or outbound,
never both. The two need different credentials and confer opposite
capabilities, so one row serving both would be two trust relationships and two
blast radii sharing a name.

| Direction | What binding it to an agent does |
|---|---|
| `inbound` | **Exposes** that agent: a partner can task it |
| `outbound` | Gives that agent a **delegation tool**: it can task the partner |

Only an inbound integration exposes. If an agent is unexpectedly
unreachable, check the integration's direction before anything else.

**Changing direction is refused while the integration is bound to any agent.**
The refusal names them. Unbind first — that is deliberate, so a dropdown can
never mass-revoke exposures or silently hand every bound agent an outbound
tool. On a permitted change the previous direction's credentials are cleared,
because a partner credential kept for a direction no longer served is a stored
secret with no purpose and no expiry.

## The three mechanisms

| Catalog entry | Directions | Outbound target | Outbound credentials |
|---|---|---|---|
| OAuth A2A | inbound, outbound | Any partner accepting OAuth client-credentials — **including another XTM One deployment** | Client id, secret, token URL the partner issued you |
| Foundry A2A | inbound, outbound | Azure Foundry | Your Entra tenant, client id and secret |
| Google Agent Platform A2A | inbound, outbound | Vertex AI Agent Runtime, over **HTTP+JSON** — see [Transport bindings](#transport-bindings) | A service account key JSON, **or** an account to impersonate — see [Two Google credential modes](#two-google-credential-modes) |

Google serves both directions, and its inbound half is not redundant with
OAuth A2A inbound: OAuth A2A needs this platform to mint a secret and the
partner to hold it, while inbound Google stores no secret on either side and
lets Google refresh the caller's own credential forever — exactly the
distinction that puts Foundry A2A beside OAuth A2A.

Inbound Google pins two values (`app/a2a/google_federation.py`): the audience
the partner's tokens carry, and the one service account allowed to call. **The
caller pin carries the whole trust decision**, and that is the one operational
difference from Foundry A2A worth knowing. Entra's JWKS URL is built from the
pinned tenant, so only that directory's keys can verify anything; Google
publishes one keyset for every principal alive, so a valid signature proves
only "some Google identity minted this". A configuration without a caller pin
is refused rather than defaulted, and a wrong caller pin looks perfectly
healthy until the partner's first call — the rejection names both presented
values in the server log.

The OAuth entry pointed at your own deployment is the easiest end-to-end test
you can run, and the only one needing no vendor account.

### Running that test on one machine

It needs one deployment, not two: this platform's inbound half mints exactly
the credential its outbound half presents, so an outbound registration pointed
at a locally exposed agent is a complete partner relationship.
[a2a-review-scenarios.md](a2a-review-scenarios.md) walks through what to do
with it, and the rest of this section is what that setup needs, and why.

A deployment cannot reach itself by default, and the reason is the destination
guard doing its job: every address a dev box
can offer is loopback or private, and the local URL is `http`. Both are
refused, deliberately.

`A2A_CLIENT_DEV_TRUSTED_ORIGINS` names origins that are exempt from those two
checks and nothing else:

```
A2A_CLIENT_DEV_TRUSTED_ORIGINS=http://localhost:4000
```

Comma-separated, empty by default, and refused at startup if an entry is not a
parseable origin. Every deployment that has not set it behaves exactly as
before.

What it does **not** relax, all of which still applies to a listed origin:

* **Origin pinning.** A partner that later serves a card pointing elsewhere,
  or redirects off-origin, is still refused.
* **Anything not named.** Listing `http://localhost:4000` does not reach port
  4001, the `https` spelling of the same port, or `http://127.0.0.1:4000` —
  the same server by another name. The setting names an origin, not a host.
* **Cloud metadata.** `169.254.169.254` and `fd00:ec2::254` are refused even
  when explicitly listed. There is no configuration that reaches them.

It is an allow-list rather than an on/off switch on purpose: a boolean is one
environment variable away from a production deployment that will follow an
operator-supplied card URL to a private address with stored credentials
attached. A deployment running with this set logs a warning naming the origins
on every boot.

**Do not set it in production.** Nothing enforces that beyond the warning —
it is a deliberate hole, sized to one job.

## Two Google credential modes

Outbound Google asks **which one**, as a dropdown, and shows only the field
that mode needs. Switching mode **clears** the credential being abandoned: a
hidden field is omitted when saving, and an update replaces the stored config
wholesale.

The selected mode is authoritative at mint time, so a value left behind by a
previous mode can never decide anything. A registration created before the
choice existed has no mode recorded and is read by inference instead — from
whichever field is filled — with both filled in refused rather than ranked.

| | Service account key JSON | Impersonation |
|---|---|---|
| What is stored | the partner's private key | the partner's service-account **address** |
| What is presented to Google | an assertion signed with that key | this platform's own identity, asking to become that account |
| What the partner grants | nothing; they hand you a key | `roles/iam.serviceAccountTokenCreator` on that account |
| How the partner revokes | rotating or deleting the key | removing that one IAM binding |
| Needs the platform to | nothing | run with Google Application Default Credentials |
| Available when the partner's org forbids SA keys | **no** | yes |

That last row is the reason the second mode exists.
`constraints/iam.disableServiceAccountKeyCreation` is Google's own recommended
baseline and is enforced by default in many organisations; under it a key
cannot be created at all — not misconfigured, *unconfigurable* — so before
impersonation, a partner with an ordinary security posture could not be
reached outbound by any configuration.

**What "Application Default Credentials" means here.** The platform resolves
its own Google identity through the standard chain: a workload identity from
the metadata server (GKE, Cloud Run, GCE — the deployment this mode is for), a
key file named by `GOOGLE_APPLICATION_CREDENTIALS`, an external-account
(Workload Identity Federation) configuration, or a developer's
`gcloud auth application-default login`. It is resolved once per process and
refreshed as needed, so it costs one metadata call rather than one per
delegation.

A deployment with no Google identity at all cannot use this mode, and says so
in those words rather than as a credential failure — the fix is on the
deployment, not on the fields an operator typed.

### Which failure means what

All three are `credential_cannot_mint_token`, and all three name the party who
can fix them:

| The detail says | Who fixes it |
|---|---|
| "This deployment has no Google identity to impersonate from" | whoever operates this platform — it needs ADC, or use a key |
| "Google refused to impersonate …" naming `roles/iam.serviceAccountTokenCreator` **or** the `iamcredentials` API | the **partner**, at their end. Both causes share a 403, so both are named |
| "Google does not know the service account …" | whoever typed the address |

## Transport bindings

A2A defines three ways to carry the same operations: **JSON-RPC 2.0**, **gRPC**
and **HTTP+JSON** (REST). They are not variants of one wire. A client that
implements one does not reach a partner serving another, and a card says which
it serves.

This deployment **serves** JSON-RPC only, and **consumes** JSON-RPC and
HTTP+JSON. The asymmetry is deliberate: serving a second binding is a second
public surface to secure, while consuming one is a path through code that
already exists.

That is not a detail of Google's integration, it is the whole of it. Vertex AI
Agent Engine publishes an agent card and a REST task surface —
`POST {url}/v1/message:send`, `GET {url}/v1/tasks/{id}`,
`POST {url}/v1/tasks/{id}:cancel` — and **no JSON-RPC endpoint at all**.

Nothing about this is configurable, and that is on purpose:

* **The binding is chosen at verification**, from what the card declares, and
  recorded beside the endpoint. Delegation never re-derives it — a partner
  that could change our wire between verification and use would be a partner
  that could move us onto a binding nobody checked.
* **JSON-RPC wins when a partner offers both.** A partner's
  `preferredTransport` says what it would rather serve; which binding this
  platform is better at is our question, and JSON-RPC is the one it also
  serves and therefore exercises on every test run.
* **gRPC is refused**, with its own reason (`no_common_transport`) rather than
  as a version problem. A partner serving gRPC only is not reachable from
  here.
* The chosen binding shows on the integration page as a badge — but only when
  it is *not* JSON-RPC, since a badge on the common case is noise.

Two card shapes carry this, and both are read:

| Card | Endpoint | Binding | Version |
|---|---|---|---|
| v0.3 | `url` | `preferredTransport`, plus `additionalInterfaces[]` | `protocolVersion` on the card |
| v1.0 | `supportedInterfaces[].url` | `supportedInterfaces[].protocolBinding` | `supportedInterfaces[].protocolVersion`, **per interface** |

A v1.0 card has no top-level `url` at all. An interface on an origin other
than the one the card was fetched from is discarded, not followed — the origin
check runs before the binding question, so a partner cannot earn a gentler
diagnosis by also offering an attractive binding.

### Sends are blocking; polling is the fallback

Every outbound send asks the partner to hold the response until the task is
terminal — `configuration.blocking` at v0.3, and nothing at v1.0 where blocking
is already the default and the flag is inverted (`returnImmediately`).

Not an optimisation. Vertex AI Agent Engine keeps **no task store between
requests**: a non-blocking send answers `TASK_STATE_SUBMITTED` with a task id,
and every `GET /v1/tasks/{id}` for that id is 404 immediately and stays 404. A
blocking send is the only way to get an answer out of it.

Which is the mirror of something the inbound half already knew: this platform
implements blocking send *because* ADK's client never polls. Both ends of the
reference implementation assume it.

A partner that ignores the flag, or answers non-terminally before its own
window closes, is still polled as before. If that poll 404s, the delegation
fails with `task_not_retained` and the registration is **left usable** — a
partner that answered a send at the same origin seconds earlier is plainly
reachable, and suspending the registration over one lost task would be the
wrong conclusion.

The same holds for any request that names a task the partner issued — a
continuation, a collect, a poll. A 400, 404, 409, 410 or 422 there refuses that
task (it has ended, or the partner no longer knows it), so the delegation fails
`partner_failed` with the partner's words, the breaker counts nothing, and the
registration stays usable. Google Agent Engine answers a continuation of a
cancelled task `400 … is in terminal state: canceled`. A request that names no
task — a fresh submission — still classifies its 4xx as `request_rejected_by_partner`.

### The breaker write never holds the registration row for the run

What an attempt changes on its registration (breaker counts, usability) is
committed in a short session of its own (`_commit_target_outcome`), never by the
calling run's session: flushed there, the UPDATE would keep the row locked until
the run commits, and every other delegation to that partner re-verifying
meanwhile would be refused `verification_contended`. When another session holds
the row (a verifier mid-write, the sweep), the attempt polls for about a second,
then hands the write to a detached task that waits for the row under a
`lock_timeout` and commits it; shutdown drains it with the other detached A2A
work. A write that still cannot land loses those counts, logged.

### A task that outlasts an attempt is handed back, not failed

An attempt that ends with the partner still working returns a `still_running`
result naming the task, and nothing is cancelled. The calling agent carries on
and collects later, by calling the same tool again with that task id and **no
message** — the same argument it uses to answer a partner's question, because a
second tool would cost every bound run the tokens of its description on every
turn.

This replaced a timeout-and-cancel that discarded real work: a partner pipeline
measured at 631 seconds finished successfully while the caller had already been
told it failed, and the cancel threw the result away.

A partner that stops answering is still a timeout, and still cancels. The
distinction is whether the partner has answered at all: never having heard from
it, calling its task "still running" would invent a fact.

**An answer outlives the run that asked for it** (#12079). A chat delegation
is resumed through its conversation, which outlives the turn. So is a partner's
question: a chat agent relays it to its user and ends the turn to get the
answer, which the next turn sends with `continue_task_id`, so a chat turn ending
cancels no `input-required` task. Its row is concluded with the question, so a
user who never answers leaves the task to the partner's own timeout. An assignment or
scheduled run usually ends right after the hand-back, and an ended run is never
resumed, so for that path:

- the run's teardown keeps the tasks it handed back as still running when the
  run **completed**; a run that failed or was cancelled still cancels them
  (FR-005), and a task at `input-required`, which waits on that run's own
  answer, is cancelled in every case (FR-025);
- when the answer arrives for a run in a terminal status, `_resume_caller`
  raises the platform event `a2a.delegation.completed` (agent, person, run,
  assignment, partner, task id, outcome, a 500-character enveloped preview;
  event id `a2a-delegation:<delegation id>`, so a callback and the floor
  racing start one run), heard by the delegating agent's `platform_event`
  assignments for that person;
- a later run of the SAME agent for the SAME user may collect the task with
  `continue_task_id` and no message (`_scoped_to_caller`: the run that started
  it, a run of that agent in a terminal status, or, #448, a run of that agent
  in the SAME conversation, since every chat turn is its own run, or, #600, a
  run of that agent dispatched by the inbox for the SAME work item, since a
  run wound down by the iteration backstop releases its item before its own
  end is written); another agent, another user, or another conversation's or
  work item's run still going stays refused `continuation_not_owned`
  (FR-046). The refusal never says to resend: when a still-running run of the
  same agent and user holds the task, it names that run and says to collect
  again once it ends or from the `a2a.delegation.completed` run; otherwise it
  says no task the caller may continue has that id and to pass the full task
  id;
- a `continue_task_id` of 8 characters or more that matches no task exactly
  is resolved as a prefix when it names exactly ONE task the caller may
  continue, under the same `_scoped_to_caller` scoping (#978,
  `_resolve_owned_prefix`); ambiguous or shorter stays refused;
- a collect (no message) of a task whose rows for this caller all concluded
  answers the newest row from the record, nothing sent, when that row ended
  with an outcome the partner itself decided (`partner_failed`;
  `_READ_BACK_OUTCOMES`): its outcome, `partner_reason`, `terminal_at` and
  mechanism (`_recorded_conclusion`). A completed row (the answer text is not
  stored), an abandoned one (the partner may still answer), a `timeout` or
  `cancelled` one (the cancel at the partner is best effort and its result is
  not recorded) and one this side ended on a transient failure of its own call
  (`unreachable`, `credential_rejected`, a pre-flight refusal such as
  `circuit_open`) go to the partner as before: the partner's task may still
  run or be finished.
  The integration page's delegation list carries `terminal_at` too;
- the hand-back of a run with no conversation carries
  `if_this_run_ends_first`, which says the above to the model.

**A user can stop it by asking.** The hand-back tells the agent to say so, and
an outbound registration carries one `cancel_task_at_<partner>` tool alongside
its delegation tools. There is deliberately no button: every route to the chat
cancel is gated on a turn being in flight, and a hand-back is what ends one, so
the affordance would have to be a second control that appears only sometimes.
Asking is the interface the user already has.

The tool cancels within the asking run or conversation only — a task id is not
a capability, so naming another conversation's task cancels nothing and says
the same thing it says about a task that already finished. Its description
forbids the agent using it on its own judgement: the work is real and the
partner's operator has already paid for it.

The hand-back names that tool, and `list_background_tasks` carries partner work
in the same scope beside the local sub-agents — so an agent asked what is still
running answers for both, and an agent asked to stop it knows what to call.

**A cancel owed to a partner survives a crash.** When a run tree is cancelled
(a partner's `tasks/cancel`, the reaper's lifetime bound) or a user abandons a
conversation, the partners are told afterwards, off the path that decided:
reaching N partners is N network calls. The decision is stamped on each
outstanding delegation (`cancel_pending_at`) before any partner is called - for
a run tree in the decision's own transaction, for a conversation as the cancel
cascade's first commit - and telling the partner concludes the row.

A process killed in between (OOM, node eviction) leaves the row outstanding
with the stamp, and the client re-sends the cancel once the stamp is older than
one delegation ceiling. The re-send has a loop of its own, apart from card
re-verification and the floor: it runs at startup and then on a fixed cadence
of `A2A_CLIENT_SWEEP_INTERVAL_SECONDS`, each cycle starting one interval after
the previous one started, so neither the rest of the sweep nor its own partner
calls push the next check back. Each cycle drains the backlog, oldest stamp
first, for up to half an interval (plus the partner call in flight); anything
left over is the next cycle's first work. So after a quick restart the cancel
waits for the next cycle and reaches the partner at most one ceiling plus one
interval after the decision, as long as the cancels owed ahead of it can be sent
within that half interval. Like every other cancel, the re-send is teardown and
goes out whether or not chat is available. The backlog is read 25 rows at a
time and each row is claimed alone (`FOR UPDATE SKIP LOCKED`), so two replicas
never tell one partner twice; with nothing owed, a cycle is one read of
`a2a_delegations`.

A stamped row is never collected, resumed or abandoned by the floor:
abandonment cancels nothing, and the caller it would resume was cancelled. The
stamp stays once the row concludes, so a partner's answer that raced the cancel
is recorded without waking the cancelled caller.

The other ordering - the answer recorded first, the Stop after it - leaves no
outstanding row to stamp, so the conversation cascade also records the Stop on
the conversation (`conversations.stopped_at`) in the same first commit. The
value is the moment the Stop was acknowledged, not the commit, so work sent
right after the Stop is never mistaken for work it stopped. The partner wake
compares it with the delegation's submission and delivers nothing the user
stopped, and a stopped wake turn starts no follow-up for a partner answer
stranded in it; the answer stays in the history for the next turn the user
starts. A message the user sent, or a sub-agent task's wake, stranded in that
turn is still answered as before. A turn the user starts never reads the
record, so a Stop blocks nothing later.

**When the attempt ends**

| | |
|---|---|
| a callback is registered | at the grace period (`A2A_CLIENT_GRACE_SECONDS`) |
| the agent collects (a task id, no message) | at the grace period: a collect is a question, never a wait |
| none is | at the ceiling, as before |
| the task finishes first | immediately, with the result |

**Waiting on a delegation.** `wait(until=<collect_task_id>)` resolves the
partner task id of a delegation the same agent sent for the same user (the
ownership rule a collect applies; anyone else's id stays `unknown`): `running`
while the row is outstanding, `completed`, `failed` or `cancelled` from its
outcome. The wait ends on the poll that reads the row concluded (callback
received, floor collect, sweep), the card shows the partner's name, and the
result names the collect call, never the partner's text.

The grace period exists because a partner answers a submission before the work
is done — this platform's own inbound surface detaches the run and replies
`working` — so handing back the instant a task reports non-terminal would cost
the calling agent two turns for work that finished in three seconds.

### Being called back

Where a partner's card declares `pushNotifications` **and** this deployment is
reachable, the delegation registers a callback with the submission and the
partner tells us when the task is done. The calling run then resumes by
whichever mechanism its path already has: an assignment run re-arms its durable
execution, and a chat conversation is woken the way finished background work
already wakes it.

Reachability is not assumed:

| | callbacks | address |
|---|---|---|
| nothing set, `BASE_URL` public | on | `BASE_URL` |
| nothing set, `BASE_URL` loopback or private | off | — |
| `A2A_CLIENT_CALLBACK_BASE_URL` set | on | that address |
| `A2A_CLIENT_CALLBACKS_REACHABLE=false` | off | — |
| `A2A_CLIENT_CALLBACKS_REACHABLE=true` | on | `BASE_URL` |

An address the destination guard would refuse to *send* to — loopback, private,
link-local, cloud-metadata — is one no partner can reach either, so a dev
deployment reads as unreachable without anyone saying so and a self-hosted one
behind a firewall keeps working on the other mechanisms. Set `=true` where that
verdict is wrong about your `BASE_URL`.

Set `A2A_CLIENT_CALLBACK_BASE_URL` where a partner reaches this deployment at a
different address than a browser does — behind a proxy, or on a hostname served
only to partners. It is a base, so give an origin and no path: the delegation id
is appended. Naming one implies reachable.

Set `A2A_CLIENT_CALLBACKS_REACHABLE=false` deliberately if you would rather a
partner never learn this deployment's address: registering a callback tells it,
and in an outbound-only relationship it would otherwise never find out. That
refusal wins over a configured callback address.

**A callback is never the only way a delegation concludes.** A partner bounds
its delivery attempts and gives up — this platform's own sender stops after
five — so the client sweep collects anything outstanding that nobody has asked
about, and resumes the caller by the same route a callback would. A calling
agent that never collects is caught by the same floor.

Two settings decide when, and they are not the same thing:
`A2A_CLIENT_FLOOR_INTERVAL_SECONDS` is the AGE a delegation must reach before
the floor will touch it, and `A2A_CLIENT_SWEEP_INTERVAL_SECONDS` is how often
the check runs at all. Both are generous by default — 15 minutes and hourly —
so lower the sweep interval as well when you want to watch the floor work.

The age is deliberately longer than the delegation ceiling, because it is
patience for someone *else* to report: a registered callback should get its
chance before the platform starts asking instead, and an agent handed a task
id should have room to collect it inside its own reasoning rather than have
the answer arrive out of band. Set it equal to the ceiling and the floor
chases the instant an attempt gives up, pre-empting both.

The `mechanism` on each delegation says which one actually reported it:
`called_back` for a partner's callback, `asked` for a collect — including one
the floor made.

### The version decides the path; the binding decides the body

Worth stating on its own, because getting it backwards produces a partner that
refuses every submission while looking perfectly healthy.

**v0.3 has two representations of itself** — pydantic models for its JSON-RPC
binding, and a proto for its REST and gRPC ones — and they disagree about the
field that carries a message's content. So there are three dialects, not two:

| binding | version | message content | part discriminator | role |
|---|---|---|---|---|
| JSON-RPC | 0.3 | `parts` | `kind` | `user` |
| JSON-RPC | 1.0 | `parts` | — | `ROLE_USER` |
| REST / gRPC | 0.3 | **`content`** | — | `ROLE_USER` |
| REST / gRPC | 1.0 | `parts` | — | `ROLE_USER` |

A proto parser refuses an unknown field rather than ignoring it, so getting one
cell wrong fails the whole request — Agent Engine serves v0.3 over REST, the
hardest row of the table. Meanwhile that partner's *paths* follow the negotiated version, `/v1` prefix
included. Two of the three axes move independently: the container field goes
with the version, the discriminator and the role spelling go with the binding.
Each is stated in a table in `a2a.protocol` rather than derived from the
others, because every time one was inferred from another it was wrong for a
real partner.

Reads are tolerant instead: a part list is accepted under either name, with no
version logic at all. That matters most for an `input-required` task, whose
`status.message` is the partner's question.

### One wrinkle: the REST path prefix

The two versions disagree about whether the version belongs in the path. v0.3
declares `/v1/message:send`; v1.0's reference server mounts the bare
`/message:send` under a prefix the deployment chooses and the card's interface
URL carries. Both conventions are live — Google's own client appends `/v1` to
the URL its card declares.

So the first request uses the convention the negotiated version implies, and a
**404 retries once with the other one**, logging which answered. A prefix
disagreement is not allowed to make a partner permanently unusable. If a
partner answers 404 to both, that is a real 404 and it is reported as one.

## Registering a partner

1. Create the integration, choosing the mechanism and **outbound**. The Setup
   Guide follows the direction you pick.
2. Paste the partner's **card URLs** — the exact location of each agent's
   card, separated by commas or new lines. Not a base URL: neither vendor
   serves its card at the conventional well-known path.
3. Enter the credentials the partner issued you. **One credential for the
   partner**, however many agents it exposes.
4. Save. The platform fetches and verifies each card immediately and shows you
   what each agent declares: name, description, skills, and the content types
   it accepts and produces.
5. Bind it to each agent that should be able to delegate — agent
   configuration → Tools tab → Integrations.

### One partner, several agents

An A2A agent card describes exactly **one** agent, so a partner exposing three
agents publishes three cards. One registration holds the partner and its
credential; each card URL becomes a **target**, and each target that verifies
becomes its own delegation tool, named after the agent it reaches.

That matters when something breaks: a target carries its own verdict, its own
pinned origin, its own negotiated protocol version and its own circuit. One of
a partner's agents going down pauses delegation to that agent and leaves its
siblings working. "Test Connection" reports the aggregate — *2 of 3 reachable*,
naming the ones that are not — and each target has its own Verify button, so
checking one never re-opens another's cooldown.

Editing the list reconciles rather than replaces: a URL you did not touch keeps
everything verified about it, a new one arrives unverified, and a removed one
stops contributing a tool immediately.

### Editing the registration

Stored secrets never come back from the API: on the Config tab they show as
empty inputs whose placeholder says a value is stored — type to replace, leave
alone to keep. Emptying any other field keeps its stored value too. The token
endpoint and the card URLs are not secrets and read in clear.

### Following the partner's own list

Between two XTM One deployments the list maintains itself. This platform asks
the partner which agents its credential reaches and keeps a target for each —
so binding an agent on the partner's inbound side makes it a delegation tool
here, and unbinding it removes one, without anyone copying a URL.

There is no setting for this, deliberately. Which agents a credential reaches
was always the **partner's** decision, made when it binds an agent to the
registration that credential belongs to. A list typed on this side is a copy of
that decision and stale the moment the partner changes anything, so following
the source is more accurate rather than more permissive — a toggle would only
have chosen how out of date your copy is.

It is not part of the A2A specification — a card describes one agent, and there
is no standard way to ask for the rest. Any partner that does not implement it
answers 404 and the list you typed keeps running, so Foundry and Vertex
registrations behave exactly as before.

The spec does define a way to *advertise* something it does not define, and
every card this deployment serves uses it: an `AgentExtension` under
`capabilities.extensions`, named
`https://filigran.io/a2a/extensions/partner-agent-discovery/v1`, carrying the
path in its `params` and marked `required: false`. So a partner reading one of
our cards learns discovery exists, what it answers, and that a federated
(Foundry / Google) caller is not served by it — instead of finding out from a
404 that looks the same as "not implemented". The URI names the extension, not
this deployment: it is identical everywhere, which is what lets two
deployments recognise each other.

The outbound half reads the same declaration off a *partner's* cards, so it
asks only the partners that said they answer. Three cases, and the third is
the one to know about:

* a partner whose card declared the extension is asked, at the path it named;
* a partner whose cards have been read and declare nothing is **not asked at
  all** — no token minted, no request made;
* a registration nothing has verified yet is asked once at the default path,
  exactly as before. A declaration can only be known after a card has been
  read, and discovery runs before the first verification on a new
  registration. It self-corrects on the next cycle.

"Its cards have been read" means a card actually parsed, not merely that a
verification ran — a partner whose card went unreachable for a while keeps
being asked rather than being quietly written off as not supporting it.

What it does not do:

* **Confer trust.** It decides what to LOOK at. Every discovered URL still runs
  the whole verification — destination guard, TLS, signature, version
  negotiation, origin pin — because it becomes an ordinary target and nothing
  else.
* **Follow a partner off its origin.** A listed URL on another origin is
  discarded, not fetched: the list arrives authenticated, but a partner naming
  somewhere else is the redirect origin pinning already refuses.
* **Empty your registration.** A partner answering with no agents changes
  nothing, so a malformed reply cannot strip every tool you have.
* **Bootstrap a partner.** You still supply the first card URL; discovery
  extends a relationship rather than establishing one.

## Reading the health verdict

"Test Connection" reports the stored verdict rather than re-deriving one, so
it and the delegation path can never disagree about the same partner.

| Reason | What to do |
|---|---|
| `never_verified` | Not verified yet. Use Test Connection. |
| `card_unfetchable` | The card URL did not answer. Check the URL and that the partner is reachable from this deployment. Also reported when no card URL is configured at all — check the field is filled before checking the network. |
| `card_malformed` | The URL answered with something that is not an agent card — often an HTML error page. |
| `transport_unverified` | The card URL must be `https` on a connection we can verify. Refused, never downgraded to. |
| `signature_invalid` | The partner's card signature does not verify. **No override exists.** Contact the partner. |
| `no_common_protocol_version` | The partner speaks no A2A version this platform supports. |
| `no_common_transport` | The partner serves no binding this platform speaks — gRPC only, or something unrecognised. Not a version problem; there is no setting that helps. |
| `request_rejected_by_partner` | The partner refused the request we SENT — a 4xx that is not about credentials, on a request naming no task (a refused task fails only its delegation). Not a card problem: the cause is in the server log, in the partner's own words. Durable, so it persists until a **Test Connection** clears it. |
| `credential_cannot_mint_token` | The stored credentials cannot produce a token. Re-enter them. |
| `credential_rejected_by_partner` | We minted a token and the partner refused it. Check the credentials *with the partner*. |
| `destination_not_permitted` | The address is one this platform will not send to — loopback, private, link-local, cloud metadata, or anything else not globally routable (including carrier-grade NAT space, `100.64.0.0/10`). |
| `destination_blocked_by_deployment` | Your own network policy refused it. |
| `origin_mismatch` | The partner's card points at a different origin than its card URL. |
| `card_change_unaccepted` | The partner changed its card. Review and accept to resume. |
| `signature_downgraded` | The partner **stopped signing** a card it used to sign. Review and accept to resume. |
| `protocol_version_downgraded` | The partner stopped serving the version we agreed. The detail names both the version agreed and what the card now offers — compare them before contacting the partner, since a **patch-level** difference is not a downgrade. |

### On unsigned cards

An unsigned card is **accepted**, and this is a deliberate, documented
weakening. Neither vendor target signs its card today, so requiring an
acknowledgement would have gated every registration while assuring nothing.
Trust rests on confirmed-encrypted transport plus the URL you supplied — the
same basis on which the inbound half asks partners to trust *our* unsigned
cards.

What is *not* accepted: a signature that is present and does not verify. That
is fatal, in every case, with no override.

Whether a card was signature-verified or accepted unverified is recorded on
the registration, on every run that delegated under it, and in the audit
trail. The delegation tool says so in its own description, so the model
deciding whether to send work there knows too.

The two downgrade reasons exist because a partner that once proved its card
and stops is evidence rather than the state of the art.

## Where a failure's cause actually is

A calling agent reports a failure in its own words — "I could not reach the
partner" — because it is handed a machine-readable reason and paraphrases it.
The cause is in three places, in increasing order of detail:

1. **The server log.** Every attempt logs one line, failures at WARNING:

   ```
   a2a.client: delegation unreachable - integration=<id> target=<id> task=<id> reason=<partner reason> detail=<what went wrong>
   ```

   Grep `a2a.client: delegation` first. It is the fastest answer and it exists
   for every attempt, including one refused before anything was sent.
2. **The delegations list** on the integration page, which holds the same
   outcome and reason per attempt, with the run that caused it.
3. **The integration's health verdict**, for anything that made the
   registration unusable rather than failing one attempt.

The partner's own prose — its answer, or a question it asked back — is
deliberately absent from the log. It is untrusted content of unknown
sensitivity, and the reason plus our own diagnostics are what an operator acts
on.

## When delegation stops

Work through these in order:

1. **Is agentic chat enabled — AI on, Enterprise Edition?** Otherwise
   nothing is attempted, and the delegation tool is not offered to any agent
   at all. `GET /api/v1/platform/config` reports it as `chat_enabled`.
2. **Is the integration `outbound`?** An inbound one confers no tool.
3. **Is it bound to that specific agent?** Binding is the whole opt-in.
4. **What does the health verdict say?** See the table above.
5. **Is the circuit open?** After repeated *transient* failures — timeouts,
   connection errors, partner 5xx — delegation pauses for a cooldown that
   lengthens on repeat. **A successful Test Connection closes it
   immediately**, so an operator who has just fixed a credential never has to
   wait one out.
6. **Has the card changed?** A material change suspends until accepted.

Durable failures — a rejected credential, no common version, a refused
destination — never open the circuit. Retrying them cannot succeed, so they
mark the registration unusable directly with a reason you can act on.

## Bounds

| Bound | Default | Setting |
|---|---|---|
| One delegation attempt | 5 min, or the run's remaining deadline | `A2A_CLIENT_DELEGATION_CEILING_SECONDS` |
| Verdict staleness before a delegation re-verifies | 15 min | `A2A_CLIENT_VERDICT_STALENESS_SECONDS` |
| Background re-verification sweep, the floor's own cadence, and the re-send of a cancel a crash interrupted | hourly | `A2A_CLIENT_SWEEP_INTERVAL_SECONDS` |
| Partner response body | 1 MiB | `A2A_CLIENT_RESPONSE_MAX_BYTES` |
| Consecutive transient failures before pausing | 5 | `A2A_CLIENT_BREAKER_THRESHOLD` |
| First cooldown | 5 min, doubling, capped at 1 h | `A2A_CLIENT_BREAKER_COOLDOWN_SECONDS` |
| Wait before handing back, when a callback will report | 15 s | `A2A_CLIENT_GRACE_SECONDS` |
| Age at which the floor will collect an outstanding task | 15 min | `A2A_CLIENT_FLOOR_INTERVAL_SECONDS` |
| Whether a partner can call this deployment back | derived from `BASE_URL` | `A2A_CLIENT_CALLBACKS_REACHABLE` |
| Where a partner should call back | `BASE_URL` | `A2A_CLIENT_CALLBACK_BASE_URL` |

Nothing bounds how many partner tasks one conversation may leave outstanding.
The calling run's own budgets bound how many delegations it can make, and each
partner enforces its own limits; the per-run cap on background work is sized to
protect this platform's database connection pool, which a task running on a
partner's infrastructure does not consume.

The sweep is an optimisation, not the control: a delegation re-verifies for
itself once its verdict is stale, so a sweep that never runs costs freshness
on the integrations page and nothing else.

## What a partner never receives

Worth knowing when a customer asks. A delegation carries **only** the message
the agent composed for it. Not the conversation, not another integration's
credentials, not a platform token, not a session cookie. The partner is
reached only at the origin its registration was verified against, and
redirects away from it are not followed.

What comes *back* is treated as untrusted: it passes the platform's
prompt-injection defences and is presented to the model as an attributed quote
from a named external party rather than as instructions.

## Not supported this phase

Consuming a partner's event stream — we poll, and take a callback where the partner offers one. The **gRPC** binding — a partner serving only gRPC is refused with `no_common_transport`. Discovering a partner's agents
without being told where — you supply the card locations, and discovery only
extends a partner whose card declares the extension. Fan-
out to several partners as one act. Outbound work from anything other than an
agent run. Per-partner outbound rate limiting; the per-delegation ceiling and
the circuit breaker are the only load controls.
