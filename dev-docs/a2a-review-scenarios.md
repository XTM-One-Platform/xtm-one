# A2A review scenarios

Short walk-throughs for reviewing the A2A feature by hand. Each one says what
to do and what you should see; if what you see differs, that is the bug.

One deployment is enough. **Self-federation** is a supported target: this
platform's inbound half mints exactly the OAuth client-credentials pair its
outbound half presents, and serves its card where its own client can read it.
An **inbound registration** plus an **outbound registration pointed at its own
cards** is a complete partner relationship, with every byte of the protocol
really on the wire.

So there is one stack, started the way the end-to-end suite starts it:

```
docker compose -f docker-compose.yml -f docker-compose.e2e.yml \
  up -d minio postgres redis platform worker
```

with the A2A settings exported in that shell; `docker compose` interpolates
them through the `x-e2e-environment` anchor in
[docker-compose.e2e.yml](../docker-compose.e2e.yml). Copy them from
[.github/workflows/e2e.yml](../.github/workflows/e2e.yml)'s `e2e` job, then
take two things out of yours:

- **the clock lines.** A reviewer checking that a hand-back arrives at the
  grace period wants the fifteen seconds the product ships, not the three a
  suite uses. Leave them out entirely and the shipped values apply.
- **`LLM_PROVIDER_CUSTOM_OPENAI_*`.** That is the scripted model, which is
  right for a suite and the opposite of what you want when you are reading what
  an agent decided. Configure a real provider in Settings instead, and skip the
  `scripted-llm` service.

Three settings are not optional, and each one's absence looks like a broken
feature rather than a missing setting:

- **`A2A_ALLOW_INSECURE_LOOPBACK=true`** — the platform reaches its own card
  over plain http from 127.0.0.1, and the partner-facing surface refuses a
  transport it cannot confirm encrypted (`403 https required`). This exempts
  real loopback; without it the platform's own card fetch is refused.
- **`A2A_CLIENT_DEV_TRUSTED_ORIGINS=http://localhost:4000`** — the outbound
  destination guard refuses loopback outright. This is the one exemption.
- **`TEST_ONLY_INSECURE_TRANSPORT_PEERS=172.16.0.0/12,192.168.0.0/16`** — only
  if you want to reach the protocol from your own machine, with a browser or
  `curl`, which arrives from the bridge gateway rather than from loopback.
  Send no `X-Forwarded-For`: a request carrying it is never exempt and
  is answered `403 https required`. The platform refuses to start with this
  setting unless `A2A_ALLOW_INSECURE_LOOPBACK=true` is set as well.

Sign in with the `ADMIN_EMAIL` / `ADMIN_PASSWORD` you started the stack with.

Throughout, the two halves are named by the registration an agent binds:
**family 1** is bound to the inbound registration and is the partner being
reached; **family 2** is bound to the outbound registration and is the side
reaching out. An agent is in exactly one of them — a direction is exclusive,
which scenario 5 is about.

---

## 1. Exposing an agent (inbound)

1. Integrations → new → **OAuth A2A**, direction **inbound**. Save.
2. Config tab → generate the credential. Copy the secret — it is shown once.
3. Create an agent, then bind that integration in its **Tools** tab.

**Expect:** the Tools tab now shows a card URL in the Integrations list. There is no
"expose" switch anywhere — binding *is* the opt-in. Unbinding takes the
exposure away again.

**Also worth seeing:** open the card URL in a browser. It is served
anonymously, and declares the OAuth scheme, the agent's skills — every
built-in it carries, narrowed by nothing — and the partner-discovery
extension.

**If the card answers `403 https required`:** the partner-facing surface could
not confirm the transport encrypted. From the platform itself,
`A2A_ALLOW_INSECURE_LOOPBACK=true` is missing; from your own machine,
`TEST_ONLY_INSECURE_TRANSPORT_PEERS` is, or the request carried an
`X-Forwarded-For`. The platform log names the route, the peer and the
forwarded protocol it saw.

## 2. Reaching it back (outbound)

1. Integrations → new → **OAuth A2A**, direction **outbound**; the Setup Guide
   switches to the outbound procedure.
2. Paste the **card URL** from scenario 1, this deployment's own **token URL**
   (`http://localhost:4000/mcp/oauth/token`), and the client id / secret you
   copied. Save.
3. Bind it to a *different* agent — one not bound to the inbound registration
   — then chat with that agent and ask it to delegate.

**Expect:** the saved registration is **unusable** with nothing verified yet —
saving does not reach the partner. Press **Verify now** on the partner card and
it turns usable, with the protocol version and signature state shown. The bound
agent then gains one delegation tool per partner agent, named after what the
card declares. Work sent through it appears in the exposed agent's activity as
an external submission — the same platform, reached as a stranger would reach
it.

**If verification fails with a destination error:** `A2A_CLIENT_DEV_TRUSTED_ORIGINS`
is unset or names a different origin than the card URL. The guard matches on
the origin exactly — scheme, host and port.

## 3. Discovery — an agent appears without anyone pasting a URL

1. With scenario 2 working, bind the *same* inbound registration to a
   **second** agent.
2. Open the outbound registration's page and leave it a moment.

**Expect:** the second agent shows up as a target on its own, and its
delegation tool appears on any agent bound to that registration.

This is the one worth watching, because it only works if both halves of the
extension work: the inbound half's cards declare the discovery endpoint, and
the outbound half reads that declaration during verification and then asks. A
partner whose card declares nothing is never asked at all.

## 4. Handover from a tasked agent

With scenario 2 working: give the exposed agent a peer in its Configuration →
handover agents and type it **consult**, **transfer** or **delegate**. Then
delegate from the family-2 agent with a question the peer is the specialist
for.

**Expect:** the handover happens, and the peer runs its own full configuration
as the *exposed agent's* run-as user. One task is one identity all the way
down, however many hops it takes.

Two things to try:

- the peer does **not** need the inbound integration bound to it. External
  work reaches every peer the agent configures, exposed or not;
- `transfer` and `delegate` are offered alongside `consult`. A transfer's
  answer comes back under the *tasked* agent's task and card, with the result
  artifact's `metadata.respondingAgent` naming the peer that produced it —
  check that field is there. A background delegation the agent does not collect
  in-run keeps the task open instead: it stays `working` until that work
  finishes, `tasks/cancel` cancels the children with it, and it charges the
  run-as user's allowance when it finishes.

  A `delegate` peer is the one thing here that does not run in-process. The
  spawn writes a `sub_agent` orchestration execution and waits for the
  reconciler, which lives in the worker — so the task sits at `pending`
  without one. Start it with the `docker-compose.e2e.yml` overlay, for the
  reason in the Notes.

A peer typed `mandatory_transfer` behaves as it does in chat: expose the
seeded `AEV Detection Rules Generator` chain and task it, and the reply is the
CrowdStrike or Splunk rule its chain ends on, with `respondingAgent` naming the
agent that produced the rule rather than the one you addressed. It is a
built-in, so exposing it needs a **company-managed** inbound registration —
a company-managed agent may bind only a company-managed integration, and an
ordinary one is refused with a 400 naming the mismatch.

Check what the run-as user can reach before binding: that is the bound on
external work, except the platform-builder surface. An exposed agent
carrying `agent_builder` gives a partner's task only the builder's read half,
acting as a caller with no account (company-managed entities), and no trace
inspection: ask it to create an agent or to read its own traces, and it has no
tool to do either.

## 5. Direction is exclusive, and enforced

Try switching the outbound registration to inbound while an agent
still binds it.

**Expect:** refused, naming the agents to unbind first. Unbind, switch, and
the stored credentials are cleared — a credential for a direction no longer
served is a secret with no purpose. Its partner agents go too.

## 6. An A2A binding offers no tool selection

On any agent's **Tools** tab, look at a bound A2A integration next to an
ordinary one (GitHub, say).

**Expect:** the ordinary one has a tool-scope button; the A2A one has nothing
where that button would be. An A2A binding is a partner relationship rather
than a set of tools an operator picks from — inbound brings exactly one,
`ask_the_caller`, and outbound one delegation tool per verified partner agent,
which the partner's cards decide. The same holds on an assignment's integration
bindings, and A2A does not appear under **Advanced — Dynamic integrations
(per-user)** at all.

**Also worth seeing:** the Config tab says this itself, under **What this
brings your agents**. Inbound names `ask_the_caller` and nothing else, and it
is offered only while an external task is running — A2A blocks a task on its
calling *agent*, so an exposed agent missing a detail asks a machine that
answers in its own turn rather than parking the work for a person who may
never look. That is why it is not a tool you can scope: nothing an operator
picks decides whether it appears.

## 7. Federated inbound (Foundry / Google)

Both need a vendor account to exercise end to end, so review the configuration
surface instead.

Integrations → new → **Foundry A2A** or **Google Agent Platform A2A**,
direction **inbound**.

**Expect:** each asks for pins rather than a credential — Foundry for tenant,
audience and allowed caller; Google for audience and the allowed service
account. Neither mints anything. Saving one with a field missing is refused
with the reason, and for Google, leaving the allowed caller blank is refused
with *why* it matters: Google signs every identity's tokens with one keyset,
so that pin is the whole trust decision.

**Also:** Test Connection on a saved one reports what it could actually check
(the pins parse, the signing keys are reachable, an agent binds it) and says
plainly that whether the pins are the *right* ones is proven only by the
partner's first real token.

## 8. A task that outlives its own run

Give the exposed agent a peer typed **delegate**, and prompt it to hand the
work off and reply immediately. Task it from the family-2 agent. Needs the
worker, for the reason §4 gives.

**Expect:** `tasks/get` reports `working` after the agent has answered, not
`completed` — the reply is not the deliverable while the delegated work runs.
When that work finishes, its output appears as a second artifact named
`delegated-result`, naming the agent that produced it.

Then, on a second task, cancel mid-flight: the task reports `canceled` and the
child stops. Check the child actually stopped rather than merely being reported
so — the point of the cascade is that the two agree.

## 9. A conversation that spans tasks

Give the exposed agent a peer typed **transfer**. Task it and note the
`contextId` that comes back. Send a second message carrying that `contextId`.

**Expect:** the transfer target answers the follow-up, and the artifact's
`respondingAgent` names it rather than the agent whose card was addressed.

Three refusals, each worth trying, and none of which should start a run:

| Do this | Expect |
|---|---|
| Send a `contextId` this deployment never issued | The uniform not-found — identical to a foreign one, so existence is not probeable |
| Unbind the responsible agent from the inbound registration, then follow up | Refused: responsibility recorded once is not consent forever |
| Follow up while the previous task is still `working` | Refused rather than forking a second task under one conversation |

Then task the seeded detection-rule chain and follow up in its context: the
**entry** agent answers again. A pipeline stage never becomes responsible, or
every follow-up would be refused for an agent that was never reachable.

## 10. Callbacks

Both are accelerators, so the thing to verify is that failing either costs a
partner its early notice and nothing else.

**Callbacks.** Register one on the submission itself
(`configuration.pushNotificationConfig`) with a `token` you can recognise, then
stop polling. Expect a POST carrying the task's id, context and state, your
token echoed, **and no result content** — a caller reads the result through
`tasks/get`. Then try a callback pointing at an address inside the deployment's
own network: refused at registration.

Compare every notification against what `tasks/get` would have shown. Nothing
the model authored — a handover reason, a tool name, a peer's raw error —
belongs in one.

## 11. What the surface refuses

Quick negatives, all on the two registrations from scenarios 1–2:

| Do this | Expect |
|---|---|
| Fetch a card URL after unbinding the integration | 404, identical to an unknown token |
| Call `/a2a/partner/agents` with no bearer | 404 — an unauthenticated caller learns nothing |
| Point the outbound registration at a URL on another origin | Refused: the card must live where it says it does |
| Regenerate the inbound credential | The outbound half's next call fails until the new secret is pasted in |
| Disable the exposed agent | Its exposure stops resolving |

---

## 12. Work that outlasts one attempt (outbound)

The failure this replaced is reproducible: the seeded detection-rule chain took
631 seconds against a 300-second ceiling, and the caller was told it timed out
while the partner finished the work for nobody.

Expose that chain and delegate to it from the family-2 agent. Expect the
calling agent to be told the task is **still running**, named — not that it
failed — and expect the partner task to keep running, which
`a2a_tasks.cancel_requested_at` confirms is still null. Then ask the caller to
collect it on a later turn and expect the rules.

Ask it to collect a task id it never created: refused.

Then check the boundary that replaced the turn. Let the chat turn end after the
hand-back — the partner task keeps running. Cancelling at turn end would have
killed every handed-back task seconds after creating it, which is what makes
this worth checking by hand.

Stopping it is something you **ask** for rather than a control that appears.
The registration carries one `cancel_task_at_<partner>` tool, listed in the
outbound tools panel under the name the model is actually given, and the
hand-back tells the agent the work continues and can be stopped. Say so on a
later turn and expect the partner task to stop. Then hand it a task id from
another conversation: nothing is cancelled, answered exactly as a task that
already finished is, because a task id is not a capability.

`a2a_delegations` should hold one row for the exchange with a `mechanism`, no
`outcome` while it runs, and the same row concluded afterwards — not a second
one. Where the partner finishes fast enough that its callback
beats the caller's own poll, two rows appear instead, sharing one
`submitted_at`: `called_back` concluded, and `asked` inserted behind it. That
is a known disagreement between this document and the product, not something to
report fresh; `06-wait-cancel.spec.ts` pins what happens and states the
question.

## 13. Being called back (outbound)

Only meaningful where the partner can reach this deployment, and when the
partner IS this deployment that has to be settled explicitly: the derivation
reads loopback as unreachable. Naming `A2A_CLIENT_CALLBACK_BASE_URL` settles it
and is what the suite uses. `A2A_CLIENT_CALLBACKS_REACHABLE` is the operator's
override — it wins outright, and a `false` there is final
(`callbacks.py::deployment_is_reachable`).

Delegate the long chain again and expect the hand-back to arrive at the **grace
period** rather than the ceiling — seconds, not five minutes. When the partner
finishes, expect the conversation to be woken with the answer without anyone
asking.

Then break it on purpose: block the callback and delegate again. The delegation
must still conclude, on the floor's interval rather than promptly — which at
the shipped clocks is 15 minutes, so start it and come back. That
is the property worth the effort: a partner bounds its delivery attempts and
gives up, so a caller trusting a callback alone waits forever.

Point a partner's callback at another delegation's id, and at the right id with
the wrong secret: both answer exactly as an unknown delegation does.

## Notes

- Start the **`worker`** as well as `platform`, with
  the `docker-compose.e2e.yml` overlay. Only a `delegate` handover needs a
  worker at all — an inbound submission otherwise runs in-process — but that
  one sits at `pending` forever without it, with no error to point at. And it
  needs the platform's network namespace: the A2A origin is pinned to a single
  string, and `http://localhost:4000` has to mean the platform from the
  worker's process as well as the platform's own. In a separate namespace that
  address is the worker, so the delegation is never delivered at all.
  docker-compose.e2e.yml has the measurements.
- The stack keeps its database between runs (`docker compose down` without
  `-v`), so a second session picks up where you left off. `down -v` starts
  clean, and costs you the licence and the provider you configured.
- Nothing here needs a second deployment, a second database or a second port.
  If a scenario reads oddly, remember that the partner and the caller are the
  same platform wearing two registrations — that is the feature, not a
  shortcut.
- The same walk, executed on every change and several hundred refusals wider,
  is [a2a-e2e-test-design.md](a2a-e2e-test-design.md).
- Operational detail — verdict reasons, the sweep, card-change acceptance — is
  in [a2a-client-operations.md](a2a-client-operations.md).
