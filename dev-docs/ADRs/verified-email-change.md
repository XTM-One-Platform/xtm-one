# ADR Verified Email Change for XTM One Users

Status: Implemented: the administrator change and the 422 on the preferences endpoint (step 1), and the self-service change with a code (step 2)
Scope: `backend/` (account settings, user management, password reset, transactional email), `frontend/` (Profile page, user management)
Reviewed in refinement: not yet

## Context

Any signed-in XTM One user can change their own email from the Profile page (`PUT /api/v1/settings/preferences`, field `email`). The address is trimmed and lowercased, and refused with a 400 when another user has it, case-insensitive (`update_preferences` in `backend/app/api/v1/settings.py`). The `ADMIN_EMAIL` account is refused with a 400: platform administrator standing follows that address (`is_platform_admin`, `xtm_super_admin` in the ID token). Nothing checks that the user controls it. The route also skips what registration checks: the allowed email domains, and it neither asks for the current password nor refuses an impersonated session. SSO accounts can change it too, although their IdP owns it. The audit entry ("Updated user preferences") records neither the old nor the new address. User management has no email change.

Products now sign their users in through XTM One with OpenID Connect (#5435, #5712, #5869). The ID token names the person by `sub`, the XTM One user id, and carries `email`. OpenCRQ is the first product doing it (OpenCRQ-Platform/opencrq#2582). It links an OpenCRQ account to an XTM One user by `sub`, but it has to find the account by email the first time: OpenCRQ accounts already exist, created by OpenCRQ's setup form or invitations, when sign-in through XTM One is turned on. OpenCRQ also matches the cross-platform JWTs XTM One sends it by email. OpenCTI and OpenAEV are likely in the same position for their own XTM One integration, to be checked.

The consequence, with OpenCRQ as it stands:

1. `boss@corp.com` is the OpenCRQ administrator and has not signed in to OpenCRQ through XTM One yet, so their OpenCRQ account is not linked to any `sub`.
2. Any XTM One user, whatever their role, sets their XTM One email to `boss@corp.com`. XTM One accepts it.
3. That user opens OpenCRQ. OpenCRQ sends the browser to XTM One with no click (#5869), receives an ID token with `email: boss@corp.com`, finds the administrator's account and links it to that user's `sub`.
4. The user is now OpenCRQ administrator, and stays so after restoring their own email, since the link is by `sub`. The real administrator's next sign-in through XTM One is refused.

Every product account is exposed from its creation until its owner's first sign-in through XTM One. OpenCRQ documents the risk as a known limitation and asks operators to keep XTM One self-service registration off, its default in XTM One mode, or restricted to their domains. That mitigation does not cover the email change, whose cause is in XTM One.

What already exists and is reused:

- Uniqueness is enforced by the database: a unique index on `lower(email)` (`ix_users_email_lower`, #6299), after a migration that merged the accounts differing only by case.
- The password change asks for the current password (`PUT /api/v1/auth/password`).
- Transactional email over SMTP when `SMTP_HOSTNAME` is set (`is_email_configured()`), with the password reset as the closest flow: a token in Redis with a lifetime, sent by email, no token without SMTP except in debug builds.
- Per-user rate limits in Redis (`enforce_rate_limit(..., bucket=...)`), HMAC with the server `secret_key` (image tokens), and the audit log (`log_audit`).

A password reset link is bound to the user id only: a link already sent to an address stays valid after the account's email changes.

## Decision

A user's email changes only once the new address is proven, or by an administrator.

This ADR covers how an existing user's email changes. It does not cover self-service registration, which takes an email without proof too, nor an `email_verified` claim in the ID token. Both are listed as follow-ups.

### Self-service change, with a code

From the Profile page, the user enters the new address and their current password.

`POST /api/v1/settings/email-change` with `{ email, current_password }`:

- Refused with 400 for an SSO account (`sso_provider` set): its IdP owns the email, and the Profile page shows it read-only with the reason.
- Refused with 403 under impersonation.
- Refused with 401 when the current password is wrong.
- Refused with 400 when the address is not a valid email or its domain is not allowed (same rule as registration).
- Refused with 503 when SMTP is not configured.
- Otherwise 202, whether or not the address is free. The address is trimmed and lowercased. When it is free, a code is sent to it. When another user has it, no code is sent, and that user gets a notice that someone tried to use their address, without a link and without the requester's identity.

The code has 6 digits, is single-use and lives 10 minutes. It is stored in Redis as an HMAC-SHA256 with the server `secret_key`, bound to the user and to the new address. A new request replaces the pending one, even for the same address: the previous code stops working. After 5 wrong codes the request is dropped and a new one is needed. The attempt cap is the real protection against guessing; the HMAC only keeps a leaked Redis from giving the codes away. Requests are limited to 3 per hour per user and 3 per hour per target address (bucket keyed by an HMAC of the address), so the endpoint cannot be used to flood someone's inbox.

`POST /api/v1/settings/email-change/confirm` with `{ code }`:

- 200 when the code matches: the email is written, the pending password reset links of the account stop working, a notice goes to the old address, and the change is audited.
- 400 for a wrong, expired or dropped code, and when the address was taken in between (the unique index refuses the write).

The notice to the old address says that the email changed, to which address, and to contact an administrator if the owner did not do it. It carries no link to act on.

### Administrator change, without a code

`PUT /api/v1/users/{user_id}/email` with `{ email }`, administrators only, from user management. Same normalisation, domain rule and uniqueness (409 when taken: the administrator already sees every account). No code: the administrator is trusted with the account, as for its role or password. It applies to SSO accounts too, to align an email with the IdP. When SMTP is configured, a notice goes to the old and the new address. This is also the way to change an email on a deployment without SMTP.

### Common to both paths

- The `ADMIN_EMAIL` account's email never changes, on either path: 400, as for deleting, demoting or disabling that account. Platform administrator standing follows the address and the boot finds the account by it: another address would hand that standing to whoever registers it, and the next boot would create a second administrator account.
- The session stays valid: the `sub` does not change.
- Pending password reset links stop working: the reset token records the address it was sent to, and consuming it is refused when the account's email is no longer that address.
- The audit entry (`entity_type="email"`) records the old and the new address and who made the change.
- `PUT /api/v1/settings/preferences` answers 422 when the body carries `email`, naming the new endpoint.
- No code and no link is written to the logs.

### Order

The administrator change and the 422 on the preferences endpoint come first: they close the hole on their own. The self-service code comes second.

With step 1, `UPGRADING.md` states that only an administrator changes an email and that the preferences endpoint answers 422 to a new address. Once step 2 ships, it states that changing one's email asks for the current password and a code, is not possible from the profile without SMTP, and that SSO accounts change their email in their IdP.

## Rationale

A code sent to the new address is the standard proof that a user controls it. With it, the email XTM One asserts to products is no longer something any user can choose freely, which is what products need to find an existing account by email safely.

| Attacker | Capability | Mitigation |
| --- | --- | --- |
| Any XTM One user | Sets another person's address as their email to take over a product account | Code sent to the new address; administrator change only by administrators |
| Holder of a stolen session (XSS, shared machine) | Moves the account to their own mailbox, then resets the password | Current password asked to start a change; notice to the old address; pending reset links stop working |
| Any XTM One user | Learns whether an address has an account | 202 in every case; the owner of a taken address is told, not the requester |
| Any XTM One user | Floods a third party's inbox with codes | 3 requests per hour per target address and per user |
| Reader of Redis | Recovers pending codes | HMAC with the server secret; 10-minute lifetime; 5 attempts |
| Administrator impersonating a user | Changes the user's email as that user | Refused under impersonation; the administrator path is audited as the administrator |

The flow keeps the self-service users have today. Restricting the change to administrators only would close the hole too, but would turn every change into a request to an administrator.

No revert link is sent to the old address. The current password is asked to start a change, so a session alone is not enough; an attacker who holds the password and the session owns the account anyway, and a revert link would not take it back from them. The owner contacts an administrator, who has the email change and the password reset.

The code lifetime is fixed at 10 minutes rather than reusing the password reset lifetime (24 hours by default): 6 digits are only safe with a short lifetime and few attempts.

Reusing the shape of the password reset (Redis store, lifetime, SMTP-dependent sending) keeps one pattern for secrets sent by email.

## Consequences

Products can rely on the email of an XTM One user not being an address someone else picked after this change. OpenCRQ's first link by email stops being a way to take over an account through an email change. Self-service registration remains the one way to hold an unproven address, which stays documented as an operator choice.

Emails changed before this decision cannot be found out: the audit entries of the preferences endpoint do not record the address. Products keep their own mitigation (link accounts early; OpenCRQ documents how to undo a wrong link).

Deployments without SMTP lose the self-service change; their administrators change emails from user management.

SSO users can no longer change their email in XTM One themselves; an administrator aligns it with the IdP when needed. XTM One does not update it from the IdP at sign-in: that stays as it is, out of scope here.

A change of email is auditable, visible to the previous owner, and invalidates the reset links sent before it.

## Alternatives Considered

Restricting email changes to administrators only was considered. It is smaller and does not depend on SMTP, but users lose a self-service they have today and administrators get every change request. It is kept as the first step and as the path without SMTP.

A revert link in the notice to the old address, valid for a few days, was considered. It is rejected for now: with the current password asked to start a change, it adds a second token flow without taking the account back from an attacker who holds the password.

Treating every existing email as unverified until confirmed was considered, to cover the changes made before this decision. It is rejected: it needs a verification state and a confirmation campaign for every user, while the products concerned link accounts by `sub` after the first sign-in.

Keeping the current behaviour and documenting the risk in each product was considered and rejected: every product that links by email would carry the same known flaw, and no operator setting removes it.

Relying on an `email_verified` claim alone, without changing how emails change, was considered and rejected for now: XTM One tracks no verification, so the claim would have nothing true to say until addresses are proven somewhere. The proposal makes such a claim possible later.

## References

- OpenCRQ-Platform/opencrq#2582: sign in to OpenCRQ through XTM One, known limitation on the first link by email
- #5435, #5712, #5869: OpenID Connect for products, product sign-in clients, sign-in without a consent page
- #6299: unique index on `lower(email)` and merge of the accounts differing by case
- `backend/app/api/v1/settings.py` (`update_preferences`), `backend/app/api/v1/users.py` (user management), `backend/app/models/user.py` (`is_platform_admin`), `backend/app/api/v1/auth.py` (password change and reset), `backend/app/services/token_store.py`, `backend/app/services/email.py`, `backend/app/core/rate_limit.py`, `backend/app/services/audit.py`, `frontend/src/pages/Profile.tsx`
- [OpenID Connect Core 1.0, standard claims (`email_verified`)](https://openid.net/specs/openid-connect-core-1_0.html#StandardClaims)
