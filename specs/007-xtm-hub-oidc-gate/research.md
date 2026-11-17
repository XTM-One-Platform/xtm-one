# Research: XTM Hub sign-in entitlement gate

## The claim contract (resolved — was the only real unknown)

**Decision**: entitlement is the **presence** of a per-instance claim named
`{platform_id}_groups`; the role is read from that claim's list.

**How it was established**: temporary instrumentation in the OIDC callback
captured a real sign-in against the Hub development tenant on 2026-08-19, then
removed. The shape observed:

```
<Platform Id A>_groups             ["Admin"]
<Platform Id B>_groups             ["User"]
https://<Hub Namespace>/groups     [20 corporate AD groups]
https://<Hub Namespace>/roles      []
```

**Rationale**: this inverted the assumption the issue was written on. The
platform identifiers are claim-name *prefixes*, not values — the Hub emits one
claim per instance a person may use. So there is nothing to compare: absence of
the claim is the refusal.

**Alternatives considered**:

- *Compare a claim value against a configured identifier* — what the issue
  originally described. Rejected because it does not match what the Hub sends.
- *Read `https://<Hub Namespace>/roles`* — empty in the observed token; the
  per-instance claim is where the Hub puts the answer.
- *Read `https://<Hub Namespace>/groups`* — carries the person's full corporate
  directory membership. Rejected deliberately, and recorded as FR-004a: it is
  broad personal data answering a different question, and using it would couple
  XTM One's access control to an organisation's internal group naming.

**Consequence for the implementation**: the per-instance claims are
*unnamespaced*. Auth0 only permits unnamespaced custom claims in the userinfo
response, never in an ID token — so the check must read userinfo, which is the
path the callback already takes. Confirmed with the Hub team as the intended
shape.

## Where the check belongs

**Decision**: between the `if not email:` guard and `_get_or_create_sso_user(...)`.

**Rationale**: `_get_or_create_sso_user` is what writes the user row. Returning
above it is the only placement that satisfies "no account is created" without
compensating logic.

**Alternatives considered**: create-then-check-then-delete — rejected in the
spec and the issue. It leaves a window in which the row exists, and an audit
trail of accounts that should never have been created.

## Recording a refusal

**Decision**: attribute the refusal to the account it concerns when one exists,
writing an ordinary audit row; log it when no account exists. **No schema
change.**

**Rationale**: `audit_logs.user_id` and `user_email` are both `NOT NULL`, and
widening a table every part of the product writes to, in order to admit rows
belonging to nobody, is a large change for a narrow need. The refusals an
operator would actually investigate are those against accounts that exist —
someone who had access and lost it, or someone probing a real account — and
those are exactly the ones this keeps durable and queryable.

A refusal can only follow a *successful* authentication at the identity
provider, so an unknown address is a person the Hub vouched for but did not
entitle: routine rather than hostile. That is what makes the narrower durability
defensible, and it is written into the spec as FR-009a rather than left as an
unstated gap.

**Alternatives considered**:

- *Widen `audit_logs.user_id` to nullable* — the original plan. Rejected on the
  owner's instruction not to modify that table, and it is the more invasive
  option: a shared record store changed for one caller.
- *A dedicated table for refusals* — fully durable, but splits the operator's
  audit trail across two places for a single event type, and creates a table
  whose only writer is one branch of one callback.
- *A sentinel "system" user* — attributes the event to an account that did not
  act, and pollutes that account's history.

**Known limitation**: a person the Hub has never entitled, and who has never had
an XTM One account, leaves log evidence but nothing queryable. If that becomes a
compliance requirement, the dedicated table is the answer, not the shared one.

## Role management and the demotion exemption

**Decision**: set the role on every sign-in, including demotion, with the
platform admin (`settings.admin_email`) exempt.

**Rationale**: the Hub is authoritative, which is the point of reading a role
from it at all. The exemption exists because the failure it prevents is
unrecoverable: a group change made in the Hub could otherwise strip the
operator's access to their own instance, and the gate offers no way back in.

**Alternatives considered**: reusing `_apply_auto_admin`. Rejected — it is
documented as deliberately promote-only so that manually-assigned admins
survive. Changing it would alter behaviour for every existing SSO deployment,
none of which asked for demotion.

## Why not reuse `PLATFORM_ID`

**Decision**: identify the instance by the existing `PLATFORM_ID`, and enable
the gate with `XTM_HUB_ADMIN_GROUP`.

**Rationale**: the claim is named after the instance, and the instance already
has a name — `platform_id`. Introducing a second, parallel identifier would mean
an instance could be two different things at once depending on who was asking.

The *switch*, though, must not be the identifier. `platform_id` is set for
clone/restore identity continuity and predates this feature entirely; keying the
gate off it would mean every deployment that forces an ID starts refusing every
person whose IdP has never heard of XTM Hub. The administrator marker is the
switch because nothing sets it by accident.

**The one wrinkle**: `settings.platform_id` is UUID-validated at startup, while
the Hub development tenant currently issues human-readable names. That is a
constraint on *testing* against that tenant, and it resolves itself once the Hub
emits UUIDs. It is deliberately not solved by a second configuration setting.

**Alternatives considered**:

- *Relaxing the UUID validator* — rejected: it would weaken an identifier that
  exists for telemetry continuity and restore parity, to accommodate a value
  expected to be temporary.
- *A second, XTM-Hub-specific identifier setting* (the first draft of this plan)
  — rejected on the owner's instruction. A value introduced to make something
  testable must not be written up as a designed setting: documented and
  specified, it becomes one nobody dares remove. And making it the enable switch
  was worse still, since `PLATFORM_ID` is set for clone/restore continuity, so
  every instance that forces one would have begun refusing every sign-in.
