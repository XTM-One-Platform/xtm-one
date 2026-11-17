# A2A end-to-end tests — design

What the suite covers, how it drives branches that are normally decided by a
model, and why each choice is the one it is.

The manual counterpart is [a2a-review-scenarios.md](a2a-review-scenarios.md).
That document is a person walking the feature; this one is the same walk
executed on every change, plus the several hundred refusals nobody walks by
hand because each is one line of setup and one assertion.

```bash
docker compose -f docker-compose.yml -f docker-compose.e2e.yml \
  up -d minio postgres redis scripted-llm platform worker
cd frontend && E2E_BASE_URL=http://localhost:4000 yarn test:e2e --project=a2a
```

with the A2A settings exported in that shell. Copy them from the `e2e` job
in [../.github/workflows/e2e.yml](../.github/workflows/e2e.yml), which is where
they are maintained.

One export serves both consumers: `docker compose` passes them through the
`x-e2e-environment` anchor in [docker-compose.e2e.yml](../docker-compose.e2e.yml),
and
the suite reads the same names from its own process to know what clock it is
asserting against. The anchor is a list of names with no values, so anything
left unset is absent from the container and a plain deployment is untouched.

---

## One deployment, federated with itself

**Self-federation is a supported target**
([test_a2a_client_self_federation.py](../backend/tests/test_a2a_client_self_federation.py)).
This platform's inbound half mints exactly the OAuth client-credentials pair
its outbound half presents, and serves its card at a URL its own client may
fetch. So one platform carrying an **inbound registration** and an **outbound
registration pointed at its own cards** is a complete partner relationship —
an OAuth token exchanged, a card fetched and pinned, a JSON-RPC submission, a
task polled to an outcome, a delegation row written — with every byte of it
really on the wire.

It runs the way every other e2e suite here runs — one `docker compose`, one
`E2E_BASE_URL`, one Playwright command — and every assertion reads from one
database.

An agent's **family** is decided by which registration it binds, and the two
are different roles rather than two labels:

| | family 1 | family 2 |
|---|---|---|
| binds | the inbound registration | the outbound registration |
| the binding confers | the exposure itself — a card URL, and a partner may task it | one `delegate_to_*` tool per verified target |
| can be addressed by a partner | yes | no |
| can reach a partner | no | yes |

An agent is in exactly one of them, because a direction is exclusive. Both
families hand work to peers among themselves, and the crossing between them is
a real partner exchange.

## Five lanes, not one suite

The branches divide by what has to be real for them, and the division is what
keeps the suite fast and honest.

**Lane 1 — wire (no model).** The test *is* the partner: mint a credential,
exchange it for a bearer, drive `message/send`, `tasks/get`, `tasks/cancel` and
the push-config family over JSON-RPC. Cards, discovery, every protocol refusal,
every rate limit, the uniform not-found. Seconds to run, nothing to flake on.
This is the bulk of the suite and it already surpasses the manual script,
because the negatives are exactly what a person skips on the tenth walk.

**Lane 2 — control plane.** The `/api/v1` surface: creating registrations in
each direction, minting and regenerating credentials, direction exclusivity,
verification verdicts, targets, generated tool names, delegation records.
Assertions read from the API, never from prose.

**Lane 3 — UI.** Only what is genuinely visual: the direction picker, the Setup
Guide switching procedure, which cards render for which direction, the absence
of a tool-scope button on an A2A binding. Everything else is asserted one layer
down, because a UI assertion on a fact the API already states is a slower test
of the same thing.

**Lane 4 — agentic, scripted.** The branches where an agent *decides*: a
family-2 agent delegating, a family-1 agent handing to a peer, an exposed agent
asking its caller a question, a task outliving its run. These need a model, and
that is the design problem this suite had to solve.

**Lane 5 — agentic, real.** One file, and the only one where the decision
itself is the subject rather than the plumbing behind it. Runs where the
deployment was given a tool-capable local model; skips where it was not.

## The model problem, and the two models

Nothing in the repo ships a fake LLM, there is no test-mode provider, and a
freshly created database carries no provider row at all — so on a new
deployment an agent cannot take a turn. CI has no model credentials either.
Left there, roughly half the review script is untestable.

Two seams make it work, and both are load-bearing:

- `custom_openai` is a first-class provider whose endpoint rides in the key
  itself as `key||base_url` ([custom_openai_provider.py:41](../backend/app/llm/custom_openai_provider.py#L41));
- providers are seeded from `LLM_PROVIDER_{P}_API_KEY` at startup
  ([main.py:367](../backend/app/main.py#L367)), so one survives a database
  created moments ago.

So the suite brings its own model: a stdlib HTTP server (the
`xtmone/scripted-llm` image, pinned in
[docker-compose.e2e.yml](../docker-compose.e2e.yml), source in
[e2e-tests/scripted-llm](https://github.com/XTM-One-Platform/e2e-tests/tree/main/scripted-llm)) that speaks enough of the OpenAI protocol —
streaming *and* non-streaming, both are used — to be accepted, and answers from a script the test armed rather than by
thinking. A spec says "when you see this marker in the prompt, call the
delegation tool, then reply"; the platform does the rest for real. It is a
`scripted-llm` service in the `e2e` compose profile rather than anything
A2A-specific, because any suite whose subject is something an agent decides
needs the same thing.

What this proves and what it does not is worth being exact about. It proves
every mechanism behind the decision: that the tool is generated, offered,
invoked, that the partner is reached, that the task is recorded, that the
answer comes back. It does **not** prove a real model would *choose* to call
`delegate_to_*` — only a real model proves that, and the tool descriptions
([tool.py:102](../backend/app/a2a/client/tool.py#L102)) are what would regress.

So there is a second model, and it does not replace the first. A deployment
started with `LLM_PROVIDER_OLLAMA_API_KEY` and `LLM_PROVIDER_OLLAMA_MODELS`
seeds it **alongside** the scripted endpoint — both resolved on the same fresh
database — and an agent names which one answers for it when it is created
(`createAgent(..., { model: 'real' })`). The two lanes therefore coexist in one
run against one deployment, with no mode to switch and no restart between them.
Tool calling is the whole requirement: a model that answered in prose would
fail every test in that lane for a reason that has nothing to do with A2A.

`11-real-model.spec.ts` is the whole of that lane, and it asserts on exactly
one thing: which tool the model reached for. Never on its prose, which is not a
contract and would break on the next model upgrade. It also asserts the
negative — a question outside the partner's declared subject must *not* produce
a delegation — because without it, a tool description so eager that everything
routes to the partner would pass.

The division of labour is the point. If a scripted file fails, A2A is broken.
If only the real-model file fails, a real model has stopped picking the tool
from its description — a real regression, but a different one, and not a reason
to hold a pull request. CI has no ollama, so that lane skips there; green must
mean the same thing on every machine.

A step may name its tool by regex (`^delegate_to_`). Delegation tool names are
derived from the partner's card, so a spec that hard-codes one asserts on a
string it does not own — and when no offered tool matches, the model answers
`SCRIPT-ERROR: no offered tool matches …`, which turns an invisible
mis-wiring into a named failure.

**Not every model call is an agent turn.** The platform also asks a model to
name a conversation, summarise a run and validate a provider, and those calls
carry the user's text — so they match a script's marker like any other. What
separates them is that an agent turn is offered tools, or streams, or both;
those extra calls are plain completions with no tool list at all
(`ScriptedModel.agentTurns`). Both halves are needed — a REST chat turn offers
tools without streaming, and the external-task loop streams. A spec counting
turns must use it: whether a summarisation lands before or after an assertion
is a race, and a spec that counts it fails intermittently while reading exactly
like a product bug.

## What the deployment has to be told

Three settings are the difference between "A2A works" and "every route answers
403", and each one's absence looks like a broken feature rather than a missing
setting.

They are passed as bare names rather than `${VAR:-default}` entries: an unset
compose variable interpolates to the EMPTY STRING, which is fatal for any
setting that cannot parse one, and a default written here is one that can drift
from the platform's own.

**`A2A_ALLOW_INSECURE_LOOPBACK=true`.** The partner-facing surface refuses a
transport it cannot confirm encrypted ([core/utils.py](../backend/app/core/utils.py)
`is_secure_transport`, `403 https required`), and a platform reaching its
own card does so over plain http from 127.0.0.1.
The client making that call sends no forwarded headers — this is what exempts
loopback, and it exempts nothing else. An address exemption reads the real
socket peer only: a request carrying `X-Forwarded-For` is never exempt, since
uvicorn rewrites the peer from that header for any trusted proxy.
`DEBUG=true` grants the same exemption and is what a dev build uses, but it
also turns on SQL echo.

**`TEST_ONLY_INSECURE_TRANSPORT_PEERS=172.16.0.0/12,192.168.0.0/16`.** The
suite runs on the host, so its requests arrive at a containerised platform from
the compose network's bridge gateway rather than from loopback. This names
Docker's default address pools as peers whose plain http counts as encrypted -
a setting that exists for this suite only: it is logged at every boot, and the
platform refuses to start with it unless `A2A_ALLOW_INSECURE_LOOPBACK=true` is
set too (the loopback exemption it extends), or with a network that is not
private, so a deployment cannot pick it up instead of `FORWARDED_ALLOW_IPS`. The
suite therefore sends no forwarding header at all (`WIRE_HEADERS` in
`instance.ts` is empty): an `X-Forwarded-For` would forfeit the exemption, and
an `X-Forwarded-Proto` would make `external_base` publish `https://` card URLs
that nothing is listening on. Forfeiting it on purpose is how one spec proves
the refusal: the same client sends `X-Forwarded-For` and every partner-facing
route answers 403. `FORWARDED_ALLOW_IPS` keeps the shipped default.

**`A2A_CLIENT_DEV_TRUSTED_ORIGINS=<the deployment's own origin>`.** The outbound
destination guard refuses loopback, private addresses and plain http,
deliberately. This is the operator-supplied exemption and the only one: cloud
metadata is exempt from the exemption, and origin pinning still applies.

Two more make branches observable rather than possible:

**`A2A_CLIENT_CALLBACK_BASE_URL`.** Both halves are the same loopback
deployment, so the reachability derivation reads "unreachable" and no callback
branch runs. Naming a callback address settles it
([callbacks.py](../backend/app/a2a/client/callbacks.py)
`deployment_is_reachable`), which is why the suite needs no separate flag.

**The accelerated clock.** Every A2A interval is validated only as `> 0`
([config.py:1847](../backend/app/core/config.py#L1847)), so a test run may
shorten them: the floor from 15 minutes to 5s, the delegation ceiling from 5
minutes to 20s, grace to 3s. One exception — `A2A_RUN_LEASE_SECONDS` must not
go below 60, or the heartbeat lands after the lease expires and the reaper
starts failing healthy runs ([tasks.py](../backend/app/a2a/tasks.py)
`_supervise_interval`). The suite reads the *same* environment variables, so a
timing assertion is written against what the deployment was given rather than
against a number copied into a test.

The **re-verification sweep is deliberately not accelerated**. `due_targets`
treats a NULL verdict as due whatever the staleness threshold says, so a
five-second sweep — the obvious "shorter is better for tests" setting —
verifies every registration a spec creates within seconds and makes "nothing
verifies a registration on save" unobservable even though it holds.

`x-e2e-environment` therefore omits `A2A_CLIENT_SWEEP_INTERVAL_SECONDS`
entirely, as it omits `LLM_PROVIDER_OLLAMA_*`: both are lanes a compose stack
cannot be given without adding the name back, and both skip on it. Seven specs
sit behind them — two on the sweep, five on the real model — and each says so
where it skips.

And the suite needs a **worker**, started with
`docker-compose.e2e.yml`. An inbound submission runs in-process,
but a `delegate` handover writes a `sub_agent` orchestration execution and
waits for the reconciler; without one, every delegated branch sits at `pending`
with no error to point at.

It has to share the platform's network namespace, and the reason is worth
stating precisely because the obvious one is wrong. A delegated child runs its
body *on the worker* (`orchestration_adapters/sub_agent.py`), so a child that delegates over
A2A makes that call from the worker's process. The A2A origin is **pinned** —
one string, fixed at verification and re-checked per request
([destinations.py](../backend/app/a2a/client/destinations.py)
`assert_same_origin`) — so that string has to mean "the platform" from every
process that speaks it. Measured against the compose stack:

| from inside | `localhost:4000` | `platform:4000` |
|---|---|---|
| **platform** | 404 — past the transport gate | 403 "served over HTTPS only" |
| **worker** | connection refused | reachable |

This was measured with loopback as the only transport exemption; since
`TEST_ONLY_INSECURE_TRANSPORT_PEERS` exempts the bridge network as well, the
platform reaching itself by service name is past the gate too. What still
decides the layout is the pinned origin, `localhost:4000`. A separate worker needs anything *but* `localhost`, because there that
address is the worker itself — the delegation never reaches the platform at
all, and is recorded `unreachable` rather than refused. Sharing the network
namespace is what collapses the two requirements into one.

All of it is an artefact of a deployment being its own partner — in production
the partner is elsewhere and reached over HTTPS — which is why it is one
interpolated variable rather than a second worker service. Left unset, the key
is absent from the rendered config entirely and the worker networks as it
always did.

## Shape of the suite

```
docker-compose.e2e.yml                   the stack an e2e run needs
frontend/playwright.config.ts            the `a2a` project: serial, no retries
frontend/tests_e2e/scripted-llm/         the client that arms the model
frontend/tests_e2e/a2a/
  instance.ts     where the deployment is, and what clock it is running
  fixtures.ts     the `one` fixture: page / rest / wire
  wire.ts         the A2A protocol as a test speaks it
  seed.ts         building both halves over the API, and `federation()`
  a2a.setup.ts    readiness, licence, terms, the wire, one login
  *.spec.ts       the branches
```

| File | Subject |
|---|---|
| `01-oauth-config` | the credential's whole lifecycle, both directions |
| `02-inbound-surface` | what a stranger and a partner can learn and do on the wire |
| `03-outbound-registration` | what a registration knows, and when it learns it |
| `04-ui-inbound-vs-outbound` | what the UI says about each direction |
| `05-handover` | family 1 handing work to a peer, all four flavours |
| `06-wait-cancel` | waiting, hand-back, the floor, cancellation |
| `07-input-required` | asking the caller, and continuing a conversation |
| `08-families` | family 2's own handovers, and the crossing between families |
| `10-negatives` | gates, refusals and negatives, byte for byte |
| `11-real-model` | does a real model still choose the tool? |
| `12-cancel-cascade` | a cancel reaching the run a transfer handed the turn to |

225 tests: 216 pass, 9 skip, in eight or nine minutes against the compose
stack. The skips are all deliberate and all explained where they are written —
five real-model, the sweep's own, the polling floor's, the rate limit's, and
one `test.fixme` for a known product gap.

A **fixture**, not two projects: it carries the three ways into the deployment.
`page` for the browser, `rest` for the control plane as the admin, `wire` for
the anonymous A2A surface with the headers that surface wants.

`workers: 1` and no retries. Every spec shares one scripted model, one database
and one set of per-partner rate limits, and several assert on state that is
global to a deployment — a sweep having run, a breaker being open, a task being
the only one outstanding. Run them concurrently and they arbitrate with each
other rather than with the product. And a retry that passes hides exactly the
timing bug the spec exists to catch: a flake here is a finding.

**Its own config rather than a project in the main one**, because the
*deployment* differs and not just the specs: this suite needs a platform
started with the settings above, the ordinary suite needs one without them, and
a shared config's default command would be wrong for whichever half the stack
was not started for. `workers` is a top-level Playwright option, so a merged
config would also have to force the whole suite serial. The main config
excludes `a2a/` on **both** projects, because a project-level `testIgnore`
replaces the top-level one rather than merging with it.

## The crossing, and where a hand-off lands

`08-families.spec.ts` is the file that only exists because there are two
families. A `mandatory_transfer` fires after the entry agent's loop, so which
side of the crossing it lands on is decided by which agent holds the delegation
tool — which is decided by which agent is enrolled:

- **before the crossing**: the entry is not enrolled and its stage is. The
  entry answers, the stage takes the turn and calls the partner, and the
  delegation row names the stage.
- **after the crossing**: the entry is enrolled and its stage is not. The entry
  calls the partner, and the stage writes the final answer on what came back.

Both are how an operator would wire a triage desk that escalates outward, or a
caller whose report is written by a different agent than the one that fetched
it. The same file covers a chosen
transfer and a consult on the caller's side, a background child that crosses on
its own, the partner's own pipeline / transfer / consult answering a caller
that cannot tell, hand-offs at both ends of one exchange, and a cancel arriving
on either side of the hand-back with a hand-off in the picture.

## Traps this suite is built around

Each of these produces a failure that reads as a product bug.

- **The Terms of Use gate applies to API calls**, not just page loads. On a
  fresh licensed database every `/api/v1` call answers 403 before any browser
  opens. The setup accepts over the API first.
- **A `delegate` handover needs the worker**, and the worker needs the same A2A
  settings the platform has *and* its network namespace — a pinned origin has
  to resolve to the platform from both processes, and `localhost` inside a
  separate container is that container. See above for the measurements.
- **Rate limits fail closed.** The anonymous A2A surface is 60/min per IP, and
  the partner is this same deployment, so the whole run shares one bucket. Raised for the run; a spec that wants to prove the limit drives it
  deliberately.
- **Read the tool's schema, don't guess it.** The delegation tool's parameter
  is `message`; a call with a wrongly-named argument still "succeeds" and the
  agent still answers — it simply delegates nothing.
- **A catalog tile stops showing its auth facet once something is connected.**
  A spec asserting on it after its own fixtures exist is asserting on the run
  order; read that facet from a mechanism the run has not touched.

## What the live runs turned up

Everything below was found by running the suite rather than by reading, and
each is pinned by a spec so the disagreement cannot drift further.

**An outbound registration is not verified on save.** `verify_registration` has
exactly three callers — the `/verify` endpoint, the delegation pre-flight and
the hourly sweep ([registration.py:52](../backend/app/a2a/client/registration.py#L52))
— and no provider hook or frontend effect calls it on create. A freshly saved
registration reads `usability: "unusable"`, `last_verified_at: null` until an
operator presses Verify, an agent delegates, or an hour passes. Confirmed live.
`a2a-review-scenarios.md` §2 had said it verifies on save and now says what
actually happens; `03-outbound-registration.spec.ts` pins it. It is also why
the sweep is parked, above.

**One exchange produced two delegation rows.** A single completed delegation
against a callback-reachable caller came back from
`GET /a2a/client/{id}/delegations` as two entries sharing one `submitted_at` —
one `mechanism: "asked"` with `continuation_count: 0`, one
`mechanism: "called_back"` with `continuation_count: 1`, both `completed`.
§12 of the review scenarios expects "one row for the exchange … and the same
row concluded afterwards — not a second one". Either the projection is
per-report by design and that expectation is stale, or this is the bug it was
written to catch. `06-wait-cancel.spec.ts` asserts the observed shape with the
question stated in a comment, and §12 now names the second row as a known
disagreement rather than a finding.

**A credential that reaches no agent cannot mint a token at all.** The
discovery endpoint has an empty-list branch and a partner can never reach it:
the grant refuses first, because a token is only issued while the credential
still addresses something that answers
([grants.py](../backend/app/a2a/grants.py)). "Authenticated but reaching
nothing" is not a state a partner can hold, which makes revocation need no
separate step — unbinding the last agent stops the credential itself rather
than merely emptying what it can see. The refusal is byte-identical to the one
an unknown client id gets, so a caller cannot tell the two apart.

**An unknown `contextId` answers `Task not found`** — the same string, byte for
byte, as an unknown task id. A spec expecting a distinct "Not found" was wrong
in the safe direction: the product is stricter than the test assumed, and
`02-inbound-surface.spec.ts` now asserts the two envelopes are equal rather
than matching either literal, because indistinguishability is the property.

**The destination guard checks the scheme before the address.** An
`http://169.254.169.254/…` callback is refused for being http, not for being
the cloud-metadata endpoint. Three specs had written their address cases with
`http://` and were passing while proving only that http is refused, repeatedly.
The address cases now use `https://`, and one row pins the ordering.

**A delegation tool's name is cut for the model and not for the API.** The
name is built from the registration's name and the partner agent's
([tool.py](../backend/app/a2a/client/tool.py) `tool_name_for`), and every tool
name is then sanitised on its way to a provider — which truncates at 64
characters ([tools/base.py](../backend/app/tools/base.py)
`sanitize_tool_name`). That 64 is **Gemini's** limit, applied to every
provider; Anthropic and OpenAI allow 128. `/api/v1/a2a/client/{id}` reports the
uncut name. So an operator whose
registration and partner agent both have long names reads a tool name in the
outbound tools panel that the model is never given. Found by a spec asserting
the model was offered the tool the API named, and pinned as observed in
`03-outbound-registration.spec.ts` with the question stated: either the panel
should show what the model sees, or the name should be shortened where it is
minted rather than where it is serialised — or the ceiling should follow the
provider actually in use rather than the strictest one. It is also why
`stamp()` is terse.

**A built-in can be exposed, but only through a company-managed
registration.** Every built-in agent is company-managed, and a company-managed
agent may bind only a company-managed integration — so exposing the seeded
detection-rule chain needs `is_company_managed` on the inbound registration,
and an ordinary one is refused with a 400 naming the mismatch. The carve-out
matters because every other field of a built-in is rewritten from seed data on
each startup; the binding is the one thing an operator owns.

**The two newest files turned up nothing of their own.** `08-families` (the
families and every crossing between them) and `12-cancel-cascade` (a cancel
reaching the run a transfer handed the turn to) were written against branches
nothing else covered, and both agreed with the product on the first live run.
They are here because the branches are load-bearing, not because they caught
anything.

## Known intermittency

Retries are off **for the `a2a` project**, so a flake there is a finding rather
than something the runner absorbs. `core` keeps CI's two retries, so a "flaky"
line in the merged summary is a `core` spec — the projects report into one
total. Two are worth knowing about.

The **callback-wake tests in `06-wait-cancel.spec.ts`** wait for a conversation
to be woken by the partner's callback, and are the most timing-sensitive in the
suite. A failure there is a wake that did not arrive inside a minute, not one
that arrived wrong.

**`11-real-model.spec.ts`** is a real model deciding, and it sometimes decides
differently — asking the caller a question rather than consulting its peer, for
instance. That is a fact about the model and the peer's description rather than
about A2A, which is why the file is separate and why CI skips it. Read a
failure there as "a real model changed its mind", and only then ask whether the
description that failed to earn the call has got worse.

Turn-counting specs must use `ScriptedModel.agentTurns`: counting raw model
calls counts the platform's naming and summarising calls too, and whether one
lands before or after an assertion is a race.

## What is deliberately not covered

- **Foundry and Google federated inbound.** Both need a vendor account. The
  configuration surface — the pins, the refusals, what Test Connection reports
  — is covered; the token path is not, and cannot be here.
- **A run exhausting its own clock.** `chat_loop_timeout` has a hard 1800s
  floor, and the other path never sets `error_code="loop_timeout"`.
- **`A2A_TASK_MAX_LIFETIME_SECONDS`.** Global: shortening it for one lifetime
  test makes the reaper kill every other spec's task. Needs its own run.
- **Whether a real model still chooses the delegation tool.** Lane 5, which
  skips wherever no tool-capable model is configured — CI included.
