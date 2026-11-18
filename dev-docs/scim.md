# SCIM 2.0 provisioning

An identity provider (Microsoft Entra, or any RFC 7643 / RFC 7644 client)
creates, updates, deactivates and deprovisions XTM One accounts and groups on
`/api/v1/scim/v2`. Code: `backend/app/api/v1/scim.py` (routes, auth, writes),
`backend/app/services/scim.py` (resource shapes, filters, PATCH parsing; pure),
`backend/app/models/scim_token.py`. Tests: `tests/test_scim_service.py`,
`tests/test_scim_postgres.py`.

## Tokens

- `POST /api/v1/scim/tokens` (admin) answers the raw token (`scim-...`) once,
  with the endpoint URL the identity provider needs. Only its SHA-256 is stored.
- `GET /api/v1/scim/tokens` lists name, prefix, creator, created, last used,
  revoked. `DELETE /api/v1/scim/tokens/{id}` revokes: the next call is refused.
- A SCIM token opens `/api/v1/scim/v2` and nothing else; a session, an API key
  or an MCP credential never opens it.
- `GET /api/v1/scim/tokens/activity` (admin) lists the latest SCIM writes from
  the audit trail (actor `system:scim:<token name>`); the admin screen is
  Settings > Access and security > Provisioning
  (`frontend/src/pages/settings/ProvisioningSection.tsx`). User guide:
  `docs/docs/deployment/scim-provisioning.md`.
- Rate limit: `SCIM_RATE_LIMIT_PER_MINUTE` calls per token per minute (429 with
  `Retry-After`).
- `last_used_at` is stamped in its own short transaction, so a read leaves
  nothing uncommitted on the request session.

## Contract

| Route | Behaviour |
|---|---|
| `GET /ServiceProviderConfig`, `/ResourceTypes`, `/Schemas` | Discovery: PATCH and filter supported; bulk, sort, etag, password change not |
| `GET /Users` | `filter` of `eq` clauses on `userName`, `externalId`, `emails.value`, joined by `and`; `startIndex` (1-based), `count` (at most 200). Any other filter: 400 `invalidFilter` |
| `POST /Users` | A `userName` that already exists (an SSO or local account) is LINKED, never duplicated: its `externalId` is set and 201 answers that account. Only a different existing `externalId` is a 409 `uniqueness` |
| `PUT`, `PATCH /Users/{id}` | `active` false runs the deactivation cascade (`services/user_deactivation.py`, source `scim`); true reactivates and restores its snapshot |
| `DELETE /Users/{id}` | The cascade's deactivation plus `deprovisioned_at`. Nothing is purged; a later POST of the same `userName` links the account again |
| `GET`, `POST`, `PUT`, `PATCH`, `DELETE /Groups` | XTM One groups. Members SCIM adds carry `source` `scim`; a PUT or a `replace` of `members` changes only those plus the ones named, never an administrator's or SAML sync's membership |

Every refusal is a SCIM error body (`urn:ietf:params:scim:api:messages:2.0:Error`)
in `application/scim+json`, never `{"detail": ...}` (`ScimRoute`).

## Mapping

| SCIM | XTM One |
|---|---|
| `userName`, primary `emails` | `users.email` (lower case) |
| `externalId` | `users.scim_external_id`, `groups.scim_external_id` |
| `displayName`, `name.formatted`, `name.givenName` + `name.familyName` | `users.full_name` |
| `title` | `users.title` |
| enterprise `department` | `users.directory_department` |
| `active` | `users.is_active` through the cascade |
| `displayName` (Group) | `groups.name` |

Unmapped attributes (phone numbers, addresses, manager, preferred language) are
accepted and ignored, so a provisioning run never stops on them.

## Microsoft Entra request shapes

`services/scim.py` accepts what Entra sends: a PATCH `replace` with no `path`
whose value is an object of attributes (plain, dotted or URN-qualified keys),
booleans as the strings `"True"` / `"False"`, capitalised operation names, member
removal by `members[value eq "<id>"]` or by `members` with a value list.

## Guards

- The platform admin (`ADMIN_EMAIL`) cannot be deactivated: 400 `mutability`.
- Every write is audited as `system:scim:<token name>` (`log_system_audit`),
  anchored on the account it concerns, or for a group on the token's creator.
- The routes are exempt from the AI-disabled gate: provisioning is identity, not
  AI (`tests/test_ai_disabled_read_surface.py`).
