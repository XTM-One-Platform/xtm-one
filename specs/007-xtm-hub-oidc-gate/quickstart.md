# Quickstart: XTM Hub sign-in entitlement gate

Runnable checks that prove the feature works. Each maps to Success Criteria in
[spec.md](./spec.md). Shapes and decisions live in
[contracts/oidc-entitlement.md](./contracts/oidc-entitlement.md) and are not
repeated here.

## Prerequisites

- The dev stack running (`docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build`), with OIDC configured against the Hub
  development tenant in **Settings → SSO**.
- The instance's own platform identifier, as the Hub knows it. It appears as the
  prefix of a `{platform_id}_groups` claim on a Hub identity.
- `local_auth_enabled` left **on**, so a misconfiguration is recoverable
  (FR-012). Do not turn it off while testing.

## Scenario 1 — Nothing configured changes nothing (SC-004)

The most important one, and the easiest to skip.

```bash
grep -E '^XTM_HUB_' .env    # expect nothing
```

Sign in through the Hub. Expect a normal sign-in, provisioning a new account if
the address is unknown — exactly as before the feature existed.

```bash
cd backend && pytest tests/ -q -m "not integration" -k "sso or auth"
```

## Scenario 2 — An unentitled person is refused, and no account appears (SC-001, SC-002)

Enable the gate, naming this instance something **no Hub identity carries** —
any fresh UUID will do, since no Hub instance is named after one of yours:

```bash
XTM_HUB_ADMIN_GROUP=Admin              # setting this is what enables the gate
PLATFORM_ID=<a fresh UUID>             # the instance this gate is for
```

Restart, note the account count, sign in through the Hub, then check it again:

```sql
SELECT count(*) FROM users;
```

**Expect**: the browser lands on the login page with a readable "not authorised"
message — not raw JSON — and the count is **unchanged**. Repeat with an address
that already has an account: also refused, and that account is untouched (FR-003).

## Scenario 3 — The refusal is recorded (SC-003)

Refuse someone who **already has an account** — the case an operator would
investigate:

```sql
SELECT created_at, user_id, user_email, action, summary
FROM audit_logs
WHERE action LIKE '%refus%'
ORDER BY created_at DESC LIMIT 5;
```

**Expect**: a row attributed to that account, naming the address and the reason.

Then refuse an address with **no** account and confirm two things: the
application log carries the refusal, and no row and no user were created for it
(FR-009a). Checking that second half matters — the point of this design is that
nothing is created merely to have something to write against.

Finally, confirm the corporate group claim appears **nowhere** in what was
written — neither in the `summary` above nor in the application log (FR-004a).
The reason names the claim this instance looked for and nothing about the
person's wider organisational membership.

Note that `details` is not where to look: refusals leave that column null, so
checking it would pass whatever the code did.

## Scenario 4 — An entitled person gets the Hub's role (SC-005)

Set `PLATFORM_ID` to the identifier the Hub really knows this instance by,
restart, and sign in with an identity whose claim contains the admin marker.

**Expect**: signed in, with administrator functions available.

Then have the Hub drop the admin marker for that identity and sign in again:

**Expect**: signed in, administrator functions **gone**. Demotion is the half
that usually goes untested.

```sql
SELECT email, role FROM users WHERE email = '<the test identity>';
```

## Scenario 5 — The platform admin cannot be locked out (SC-006)

With the gate enabled, ensure the Hub does **not** assert the admin marker for
the account matching `ADMIN_EMAIL`, and sign in as them.

**Expect**: they remain an administrator. This is the one case where the Hub is
deliberately not authoritative; without it a change made elsewhere costs an
operator their own instance.

## Scenario 6 — A half-configured gate fails loudly (SC-007)

```bash
XTM_HUB_ADMIN_GROUP=Admin              # and neither PLATFORM_ID nor the override
```

**Expect**: the backend **refuses to start**, naming `PLATFORM_ID`. The claim is
named after this instance, so a nameless instance would refuse everyone — and
each refusal would be indistinguishable from an ordinary unentitled person,
which is the silent outage this prevents.

Then check the inverse, which is the common case and must be allowed:

```bash
PLATFORM_ID=<any valid UUID>           # and no XTM_HUB_ADMIN_GROUP
```

**Expect**: the backend starts normally and no gate is applied. Forcing a
platform identifier is an ordinary thing to do for clone/restore continuity, and
must never enable a sign-in gate by itself.

## Automated coverage

```bash
cd backend
pytest tests/test_xtm_hub_entitlement.py -v                       # decision table
pytest tests/test_xtm_hub_entitlement_postgres.py -v -m integration  # refusal attribution
pytest tests/ -q -m "not integration"                             # no regressions
cd ../frontend && npx tsc --noEmit && npx playwright test tests_e2e/login.spec.ts
```

The decision function is pure, so the table in the contract is testable
exhaustively without a browser or an identity provider. The integration test
exists for one reason: whether a refusal attaches to an existing account, and
creates nothing when there is none, is the only part that cannot be proven
against a mock.
