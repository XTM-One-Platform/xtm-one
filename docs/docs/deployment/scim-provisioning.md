# Provisioning with SCIM (Microsoft Entra)

XTM One serves a SCIM 2.0 endpoint (RFC 7643 and RFC 7644). An identity
provider such as Microsoft Entra ID uses it to create accounts, keep their name,
title and department current, manage group membership, and deactivate accounts
when people leave. Deactivating an account through SCIM runs the same cascade as
the **Disable** button on a user's page (see [Deactivation](#what-a-deactivation-does)).

## What you need

- An XTM One administrator account.
- In Microsoft Entra: the right to create an enterprise application and configure
  its provisioning (Application Administrator or Cloud Application Administrator).
- The XTM One URL reachable from Microsoft Entra (the provisioning service calls
  it from the internet).

## 1. Generate a token in XTM One

1. Open **Settings > Access and security > Provisioning**.
2. Copy the **Endpoint URL**. It ends with `/api/v1/scim/v2`.
3. Give the token a name (for example `Microsoft Entra`) and select **Generate token**.
4. Copy the token at once. It is shown only this time; XTM One stores only a hash of it.

The token opens the SCIM endpoint and nothing else. Revoke it from the same
screen: the identity provider's next call is refused. Each token is limited to
`SCIM_RATE_LIMIT_PER_MINUTE` calls per minute (an answer 429 with `Retry-After`
beyond it).

## 2. Create the enterprise application in Microsoft Entra

1. In the Microsoft Entra admin center, open **Enterprise applications > New application > Create your own application**.
2. Name it (for example `XTM One`) and choose **Integrate any other application you don't find in the gallery (Non-gallery)**.
3. Open the application, then **Provisioning > Get started**, and set **Provisioning Mode** to **Automatic**.
4. Under **Admin Credentials**:
    - **Tenant URL**: the Endpoint URL copied in step 1.
    - **Secret Token**: the token generated in step 1.
5. Select **Test Connection**, then **Save**.

## 3. Map the attributes

Under **Mappings > Provision Microsoft Entra ID Users**, keep these mappings and
remove the ones XTM One ignores if you want shorter provisioning logs:

| Microsoft Entra attribute | SCIM attribute | XTM One field |
|---|---|---|
| `userPrincipalName` (or `mail`) | `userName` | Email, the sign-in identity |
| `objectId` | `externalId` | Link to the Entra user |
| `Switch([IsSoftDeleted], ...)` (default) | `active` | Active or deactivated |
| `displayName` | `displayName` | Full name |
| `jobTitle` | `title` | Title |
| `department` | enterprise extension `department` | Department |

Attributes XTM One does not store (phone numbers, addresses, manager, preferred
language) are accepted and ignored, so provisioning never stops on them.

To provision groups, enable **Provision Microsoft Entra ID Groups** and keep
`displayName`, `objectId` (as `externalId`) and `members`. SCIM manages only the
memberships it adds: a member an administrator or SAML group sync added stays.

## 4. Assign people and start

1. Under **Users and groups**, assign the people (or groups) who should have an XTM One account.
2. Under **Provisioning**, set **Provisioning Status** to **On** and save.

Microsoft Entra runs a first cycle, then one about every 40 minutes. To test one
person first, use **Provision on demand**.

## How accounts are matched

When Microsoft Entra creates a user whose email already exists in XTM One (an
account created by SSO sign-in or by an administrator), XTM One **links** that
account instead of creating a second one: it stores the Entra object id and
answers with the existing account. The account's **Source** then reads **SCIM**
in **Users**.

The bootstrap administrator's email, set by `ADMIN_EMAIL`, never changes through
SCIM, and no other account takes that address.

## What a deactivation does

When Microsoft Entra sets a user inactive (the user is disabled, unassigned or
soft-deleted in Entra), XTM One deactivates the account at once:

- sign-in is refused and every session ends;
- the account's personal API keys are revoked;
- its running and queued runs, armed timers and scheduled actions stop;
- its private agents are disabled with their assignments; its private integrations are disconnected;
- its shared agents, fleets, workspaces and integrations move to the platform
  administrator (or the oldest active administrator), and assignments that ran
  as the account are paused;
- every administrator is notified, and each moved or disabled object is listed
  for review in **Settings > Access and security > Provisioning**.

Nothing is deleted. When Microsoft Entra deletes the user, the account is also
marked **removed by the identity provider**, still without purging its data.

Reactivating the user in Microsoft Entra restores the account: its agents and
assignments come back as they were, and the objects moved away return unless an
administrator changed them since. Revoked keys and stopped runs stay ended.

An administrator can preview the same cascade before confirming **Disable** on a
user's page.

## Review after a deactivation

**Settings > Access and security > Provisioning > Deactivation review** lists,
for every deactivated account, what the cascade moved, disabled or kept. Mark
each item **Keep as is**, **Reassigned** or **Deleted** once checked. The same
screen shows the latest SCIM changes (accounts and groups created, changed or
deactivated) and the tokens with their last use.

## Troubleshooting

| Symptom in the Entra provisioning logs | Cause |
|---|---|
| 401 | The token is wrong or was revoked: generate a new one and update **Secret Token** |
| 429 | The rate limit was reached: Entra retries on its own |
| 409 `uniqueness` | Another Entra object is already linked to that email in XTM One |
| 400 `mutability` or `invalidValue` naming `ADMIN_EMAIL` | The change would move the bootstrap administrator's email, or give that address to another account |
| 400 `invalidFilter` | A filter XTM One does not support; Entra's default mappings use only `eq` on `userName` and `externalId` |
