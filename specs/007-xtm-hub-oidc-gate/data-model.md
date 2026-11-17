# Data Model: XTM Hub sign-in entitlement gate

**No new entities and no schema change.** Existing tables are written through
their current shape.

## Unchanged: `audit_logs`

`user_id` and `user_email` are both `NOT NULL`, and stay that way. That
constraint is what shapes the refusal record below rather than something to be
worked around.

## Refusal record

A refusal is written as an ordinary audit row **when the address it names
already has an account**:

| Field | Value |
|---|---|
| `user_id` | the existing account the refused sign-in concerns |
| `user_email` | that account's address |
| `action` | a distinct verb for a refused sign-in, not the existing `login` |
| `entity_type` | `user` |
| `summary` | states that a sign-in was refused, for which address, and why — including the instance claim the identity failed to carry |
| `details` | not used; left null |

The reason lives in `summary` rather than `details` so that one column answers
the operator's question. An empty `details` is deliberate: a verification step
that reads it would pass whatever the code did.

**When no account exists**, the refusal is written to the application log with
the same fields, and nothing is stored. No account is created merely to have
something to attribute the record to.

**What must never be recorded**: the person's wider corporate group membership,
which the identity also carries (FR-004a). It is broad personal data with no
bearing on the decision.

## Unchanged: `users`

`users.role` is written on an entitled sign-in exactly as it is today. Only the
rule for *when* it is written changes, and that lives in code rather than in the
model.
