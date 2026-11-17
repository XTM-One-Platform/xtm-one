# Receiving a partner's callback

This platform already *sends* these. What it sends is what it should expect to
receive, so this contract is the inbound mirror of `010`'s push sender.

## The route

Token in the path, mirroring the anonymous card route that already exists on the
inbound A2A surface. The token identifies the delegation; it is not a bearer
credential and is not sufficient on its own.

```
POST /a2a/callbacks/{delegation_token}
```

| Guard | Behaviour | Why |
| --- | --- | --- |
| Transport | refuse an unconfirmed one (`403 https required`), first | the same treatment the inbound A2A surface gives it: the secret travels in the request |
| Pre-auth rate limit | before any lookup | an unauthenticated route is reachable by anyone who learns the path |
| Secret comparison | constant time, before any state is read | authenticate before consulting anything, the ordering the webhook receivers already use |
| Uniform not-found | a bad token, an unknown delegation and another partner's delegation are indistinguishable | a partner must not be able to probe for which delegations exist |

## The body

The A2A shape, which is what we send:

```json
{ "taskId": "…", "contextId": "…", "state": "completed", "token": "…" }
```

**It carries no result.** A callback is an invitation to read the task, never
the answer itself — so receiving one triggers a read of the partner's task, and
the read is what produces the result. A body claiming a result would be
partner-authored content arriving on an unauthenticated route.

`state` is read only to decide whether to act; the authoritative state comes
from the read that follows.

## What happens next

| Caller | Action |
| --- | --- |
| an assignment run, parked | emit the resume event its orchestration execution is waiting on |
| a chat conversation | wake it the way finished background work already wakes it |
| a delegation already concluded | nothing, and answer as if it had worked |

The last row is the conclude-once rule (FR-019). A callback and the polling floor
can both report the same transition, on different replicas, at the same moment.
The delegation row is the claim: whoever moves it out of `outstanding` proceeds,
the other stops.

## What it must not do

- **Resume anything on a failed authentication.** An unauthenticated callback is
  refused and nothing is read, woken or resumed.
- **Be the only way a delegation concludes.** A partner bounds its delivery
  attempts and then gives up — this platform's own sender stops after five and
  writes an audit entry. The polling floor beneath a registered callback is what
  makes that survivable (FR-018).
- **Trust `state`.** It routes; it does not decide. The task read after it does.

## Registering

A callback is registered on submission where the target declares support and the
deployment records itself reachable, using the protocol's own push-config
operation. The secret is minted per delegation, stored encrypted like every
other partner credential, and never reused across delegations — so a leaked one
exposes one exchange.
