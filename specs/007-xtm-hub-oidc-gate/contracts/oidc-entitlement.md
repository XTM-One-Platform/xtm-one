# Contract: XTM Hub OIDC entitlement

What XTM One expects from the identity provider, and what a person gets back.
Pinned against a live Hub token on 2026-08-19; see [research.md](../research.md).

## What the identity must carry

One claim per XTM One instance the person may use, in the **userinfo response**:

```
{platform_id}_groups : string[]
```

- `{platform_id}` is this instance's platform identifier (`PLATFORM_ID`).
- **Presence is the entitlement.** There is no value to compare.
- The value lists the person's roles on that instance.

Example, for an instance configured as `<Platform Id A>`:

```json
{
  "email": "person@example.com",
  "<Platform Id A>_groups": ["Admin"],
  "<Platform Id B>_groups": ["User"]
}
```

That person is entitled here, as an administrator; they are also entitled to the
other instance, as an ordinary user, which is no concern of ours.

**The claims are unnamespaced**, so they can only appear in the userinfo
response — Auth0 does not permit unnamespaced custom claims in an ID token. An
implementation that decodes the ID token instead will not find them.

**Not read**: the namespaced corporate group and role claims the Hub also emits.
See FR-004a.

## Decision table

| Gate configured | `{platform_id}_groups` | Contains admin marker | Outcome |
|---|---|---|---|
| no | — | — | unchanged from today: sign in, provision if new |
| yes | absent | — | **refused**; no account created or modified |
| yes | present, not a list | — | **refused**; malformed is not admitted |
| yes | present | yes | signed in as administrator |
| yes | present | no | signed in as ordinary user |
| yes | present | was admin, now no | signed in, **administrator removed** |
| yes | present | any | platform admin keeps administrator regardless |

The admin marker is matched **exactly, including case** (`XTM_HUB_ADMIN_GROUP`,
currently `Admin`).

A role change in either direction writes an audit record against the account, in
the same shape as a change made from the admin UI. An unchanged role writes
nothing — every entitled sign-in passes through this path, and a row per sign-in
would bury the changes that matter.

## What a refused person receives

```
302 → {frontend_url}/login?sso_error=not_authorised
```

The callback answers every non-completion the same way, with one of a small
closed set of markers this module chooses — provider text is never echoed into
the URL:

| marker | cause |
|---|---|
| `not_authorised` | not entitled to this instance |
| `cancelled` | the person declined consent at the provider (`error=access_denied`) |
| `failed` | any other provider error, or a callback carrying neither a code nor an error |

Only the first is part of this gate; the other two apply to every OIDC
deployment and replace what were previously raw error bodies.

The page states that the account is not authorised for this instance and offers
no further detail. The reason is recorded server-side: durably, against the
account, when the address already has one; in the application log when it does
not (FR-009a).

**The message deliberately does not say why.** The person is unauthenticated to
us, and the criteria describe how the organisation's identity provider is
configured.

## Configuration

| Setting | Meaning | Default |
|---|---|---|
| `XTM_HUB_ADMIN_GROUP` | marker meaning administrator, matched exactly. **Setting it enables the gate.** | empty — feature disabled |
| `PLATFORM_ID` | names this instance; selects the claim to read | empty |

Enabling the gate with no `PLATFORM_ID` **refuses to start**: the claim is named
after this instance, so a nameless instance would refuse everyone, and each
refusal would look like an ordinary unentitled person.

The switch is the admin marker, not the identifier. `PLATFORM_ID` is set for
clone/restore identity continuity — nothing to do with XTM Hub — and an instance
that forces one must not acquire a sign-in gate as a side effect.


## Out of scope

SAML. The Hub provides OIDC only.
