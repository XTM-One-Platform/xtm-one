# Authentication

XTM One supports local accounts and single sign-on (SSO) via OIDC and SAML 2.0. You configure sign-in methods from `Settings` → `Security` → `Authentication` in the UI (admin-only).

A few environment variables also shape authentication. The session and link lifetimes also have a field in `Settings` → `Security` → `Security options`; when you set the variable, it wins and locks that field.

| Variable | Default | What it sets |
|---|---|---|
| `ADMIN_EMAIL`, `ADMIN_PASSWORD` | `admin@filigran.io`, none | The bootstrap administrator (see [Local accounts](#local-accounts)) |
| `JWT_EXPIRE_MINUTES`, `SESSION_IDLE_TIMEOUT_MINUTES` | `10080`, `0` | Session lifetime and idle timeout (see [Session lifetime and idle timeout](#session-lifetime-and-idle-timeout)) |
| `INVITATION_EXPIRE_HOURS` | `168` | How long an invitation link stays valid (1 to 720 hours) |
| `PASSWORD_RESET_EXPIRE_HOURS` | `24` | How long a password reset link stays valid (1 to 720 hours) |
| `SMTP_*` | none | The mail server that sends invitations and password reset links (see [Password reset and email](#password-reset-and-email)) |
| `TERMS_OF_USE_ENABLED`, `TERMS_OF_USE_VERSION`, `TERMS_OF_USE_URL`, `TERMS_OF_USE_EXEMPT_EMAILS` | see [Terms of Use acceptance](#terms-of-use-acceptance) | The Terms of Use gate |
| `XTM_HUB_ADMIN_GROUP`, `PLATFORM_ID` | none | The [XTM Hub entitlement gate](#xtm-hub-entitlement-gate) |
| `PLATFORM_REGISTRATION_TOKEN` | none | Trust between XTM One and OpenCTI, OpenAEV and OpenCRQ (see [XTM Suite integration](xtm-suite-integration.md)) |

The full list is in [Configuration](configuration.md#security).

!!! note "The first account is always an admin"
    Whichever way the very first account is created — local registration, admin bootstrap, or the first SSO login — it is granted the admin role. Every later account defaults to the standard `user` role unless promoted.

## Local accounts

By default, users authenticate with email and password.

The bootstrap admin account (`ADMIN_EMAIL` / `ADMIN_PASSWORD`) is created or reset from environment variables on **every** startup. Each startup also makes it an administrator again and re-enables it if it was disabled, so it stays a way back into the platform. If you change that password in the UI, the next restart resets it back to the `.env` value — keep them in sync.

A startup does not touch the account's two-factor enrollment: an administrator who lost their authenticator app signs in with a recovery code, or another administrator resets it (see [If a user loses their device and their codes](#if-a-user-loses-their-device-and-their-codes)).

Local authentication is governed by two toggles under `Settings` → `Security` → `Authentication`:

- **Local Authentication** (default: enabled) — allows email/password sign-in. When disabled, local login and local registration are blocked and users must sign in through SSO (an "SSO-only" deployment). The very first account can always be created, even while this is off, so you are never locked out.
- **Local Registration** (default: disabled) — lets users self-register with email and password. When disabled, only admins can create accounts.

### Restricting new accounts by email domain

**Allowed email domains** (comma-separated; empty means all domains are allowed) restricts who gets a new account: local registration refuses an email address outside the list, and so does SSO account creation, OIDC and SAML alike (see [On-the-fly user creation](#on-the-fly-user-creation)). The domain is what follows the last `@` of the address, compared case-insensitively and exactly: `example.com` does not admit `eu.example.com`. The field has its own **New accounts** section in the settings form, so it stays editable while local authentication is off.

Only account creation is restricted. An account that already exists keeps signing in whatever its domain, an SSO identity is still linked by email to an existing account, and an invitation still registers its invitee.

**Changed behaviour.** The list used to restrict local registration only, and SSO created an account for any email address the identity provider asserted. An instance that set allowed domains while SSO created accounts outside them now refuses those new accounts (`?sso_error=not_provisioned`, below).

!!! info "The domain comes from the identity provider"
    For an SSO sign-in, the domain is read from the email address the identity provider asserts, so the restriction is only as reliable as that claim. With a multi-tenant provider, make sure it asserts verified addresses only.

## Admin-managed accounts

Admins manage accounts from the **Users** page (admin-only). Two mechanisms are available:

- **Create a user directly** — add an account and set its role from `Users` → `New user`.
- **Send an invitation link** — from `Users` → `Invite user`, generate a single-use link bound to a specific email address and role. The invitee sets their own password by opening the link.
- **Let a registered product invite** — when an XTM One administrator adds someone with no XTM One account in a registered product (OpenCRQ), XTM One sends that person an invitation to create their account, with the `user` role. Once registered, they continue to the product, on the page they were added to. A pending invitation for the same email is sent again under a new link rather than doubled: the earlier link stops working. Someone who already has an account gets an email telling them they have access instead, and a deactivated account is never invited.

!!! info "How invitation links work"
    Each invitation is bound to one email address, carries the role you choose, and can be used once. The link expires after a configurable window (default **7 days**, set with `INVITATION_EXPIRE_HOURS`, capped at 30 days). Invitations bypass the Local Registration and Allowed-domains settings, so you can onboard a specific person without opening self-registration to everyone. The link is sent by email, so inviting someone from the UI needs [SMTP](#password-reset-and-email).

The day-to-day tasks (roles, disabling an account, setting a password, impersonation, groups) are described in [Users and groups](../administration/users-and-groups.md).

## Password reset and email

A local account can get a single-use link to choose a new password in two ways:

- **Self-service.** `Forgot password?` on the sign-in page asks for an email address. If an active account with a local password exists for it, a reset link is emailed. The page answers the same way whether the account exists or not.
- **From an administrator.** An administrator sends a reset link from the user's page, or sets a new password directly (see [Users and groups](../administration/users-and-groups.md)).

The link is valid for `PASSWORD_RESET_EXPIRE_HOURS` (24 hours by default) and works once. A user who only signs in through SSO has no local password and gets no reset link: their identity provider manages their password.

Both the reset links and the invitation links go out by email, through the mail server set by the `SMTP_*` variables (see [Configuration](configuration.md#email-smtp)). Without `SMTP_HOSTNAME`:

- a self-service request sends nothing and the platform logs a warning; the person sees the usual confirmation and no email arrives;
- an administrator who sends a reset link or an invitation from the UI gets an error saying email delivery is not configured. Setting a password directly from the user's page still works.

!!! tip "Set the sender name"
    `SMTP_FROM_NAME` is the display name of the emails. Set it to your platform's name (`.env.sample` uses `XTM One`).

## API keys

People and automations can also reach the API with a personal API key (`fcp-…`) instead of a web session. A key acts as the person who created it, within the scopes, expiry and limits set on it. Users create their keys in [My profile](../usage/my-profile.md#api-keys-tab); administrators review every key and set the maximum lifetime in [Settings](../administration/settings.md#api-keys). `ADMIN_TOKEN` provisions a key for the bootstrap administrator from the environment (see [Configuration](configuration.md#security)).

An API key is a machine credential: it is not a web session, so the two-factor step, the session lifetime, the idle timeout and the Terms of Use gate below do not apply to it.

## Two-factor authentication (TOTP)

Any user can add a second factor: a six-digit code from an authenticator app, entered after the first factor on every sign-in. It is enabled per user from `Profile` → `Two-factor` — there is nothing to configure at deployment level and no environment variable to set.

The factor is **XTM One's own**, and it applies whichever way the user signs in — local password, OIDC, or SAML. It is not delegated to the identity provider. That is deliberate: many deployments federate to a provider whose MFA policy they do not control (a customer tenant, a partner IdP, a legacy directory), and being able to enforce a second factor at the application boundary is the point. Where the IdP already enforces one, the two simply stack, and users are free not to enroll.

Any RFC 6238 authenticator app works, including Google Authenticator, Microsoft Authenticator, KeePassXC, 1Password, Bitwarden, Aegis and FreeOTP. XTM One uses the parameters those apps assume by default (SHA-1, 6 digits, 30-second period), so enrollment is a QR scan with no per-app tuning. The QR is rendered by your own server, so enrollment works on air-gapped deployments — no third-party service is contacted.

Enrollment is two-phase on purpose: scanning the QR is not enough, the user must type a code back to prove the app was enrolled correctly. Until they do, nothing changes about how they sign in — closing the tab halfway cannot lock anyone out.

### Recovery codes

Enabling two-factor issues **10 single-use recovery codes**, displayed once and never again. Each one signs the user in once, in place of the app, if they lose their device. Users can generate a fresh set (invalidating the old one) from their profile, which requires a current code from the app.

### If a user loses their device and their codes

An admin can clear the enrollment from the user's detail page (`Users` → select the user → **Two-factor** → **Reset two-factor**). The account then signs in with its password alone until the user enrolls a new device. The action is audited.

!!! info "Administrators never see the secret"
    The TOTP secret is encrypted at rest with the platform `SECRET_KEY`, and recovery codes are stored hashed. Neither can be read back by anyone, including admins — resetting is the only recovery path, and it deliberately forces a fresh enrollment.

!!! warning "Rotating `SECRET_KEY` invalidates every enrollment"
    The encryption key for stored secrets is derived from `SECRET_KEY`. Changing it leaves existing enrollments unreadable: affected users can still sign in with a recovery code, and admins can reset their enrollment, but their authenticator app stops working.

### How it works with SSO

After a successful OIDC or SAML assertion, an enrolled user is returned to the sign-in page for the code step instead of being signed in. No session token is issued until the code is verified, so the extra factor cannot be skipped by replaying the callback URL.

An SSO-only user disables their factor with a code alone — they have no XTM One password to confirm with. Accounts that do hold a local password are asked for both.

Until the code is accepted, the audit log records the event as *"Authenticated via OIDC, awaiting second factor"*, not as a completed login. The completed login is recorded separately, and names the provider it came through.

!!! info "Local authentication can stay disabled"
    Two-factor does not require enabling local passwords. An SSO-only deployment (`Local Authentication` off) can enroll every user in an authenticator app and keep the IdP as the sole first factor.

### Scope

Two-factor applies to interactive sign-in — local, OIDC, and SAML alike.

That includes the MCP OAuth consent page an MCP client opens to connect to XTM One: after the email and password, the page asks for the code, and the client receives no token until the code is accepted. A failed code there is recorded like one at sign-in (action `login`), with `mcp-oauth` as its context.

API keys (`fcp-…`), channel bots and cross-platform tokens from OpenCTI/OpenAEV are unaffected: they are machine credentials with nowhere to prompt for a code.

Enabling, disabling, recovery-code use and failed verifications are all recorded in the audit log, under the `mfa` entity type. Each entry is attributed to the flow it came from via its **action** — `login`, `enable`, `disable`, or `update` (regenerating recovery codes) — so the audit filters separate a failed code at sign-in from a mistyped one while turning the factor off.

That separation is what makes the log useful for detection: a run of failed second factors with action `login` and no matching successful login is the signal that someone holds a password but not the device.

## OIDC (OpenID Connect)

Use OIDC to integrate with identity providers such as Microsoft Entra ID (Azure AD), Okta, Google Workspace, or Keycloak. OIDC uses the Authorization Code flow.

### What you need

- A registered OAuth 2.0 / OIDC application in your IdP
- A **Client ID** and **Client Secret**
- Either a **Discovery URL** (the `.well-known/openid-configuration` endpoint — recommended) or the individual authorization, token, and userinfo endpoint URLs
- The **Redirect URI** registered in your IdP: `{BASE_URL}/api/auth/oidc/callback`

### Configuration

Configure OIDC from `Settings` → `Security` → `Authentication`. The fields are:

| Field | Notes |
|---|---|
| Display Name | Label shown on the sign-in button (e.g. "Microsoft") |
| Discovery URL | Recommended — auto-resolves the other endpoints. Use the provider's `/.well-known/openid-configuration` URL, not the issuer URL on its own (for Keycloak, `https://<host>/realms/<realm>/.well-known/openid-configuration`). **Test Connection** fetches it and reports the status or the missing key; saving never depends on the result. The server keeps the fetched document for five minutes and drops it when you save |
| Client ID / Client Secret | From your IdP application; stored encrypted |
| Scopes | Default `openid email profile` |
| Manual endpoint URLs | Optional — set the authorization, token, and userinfo URLs directly if you are not using discovery |

The exact Redirect URI to register in your IdP is displayed in the configuration panel.

### User provisioning

Users are created automatically on first login. XTM One matches the identity in this order:

1. By the SSO subject (`sub`) of a previously linked account.
2. Otherwise by **email** — if a local (or other) account with the same email exists, it is linked to the OIDC identity.
3. Otherwise a new account is created, unless [on-the-fly user creation](#on-the-fly-user-creation) is off or the email's domain is not one of the [allowed email domains](#restricting-new-accounts-by-email-domain).

The IdP must return an email claim; a login with no email is rejected. The user's name and job title are updated from the IdP claims on each login.

### Deactivated accounts

If step 1 or 2 resolves the identity to an account an administrator has
deactivated (**Disable** on the user's page), the sign-in is refused before
anything on the account changes: no profile update, no SSO link, no role
change, and no new account is created alongside it. No session is issued; the
person returns to the login page with `?sso_error=account_deactivated`, and the
refusal is written to the audit log as `login_refused`. Re-enabling the account
is the only way back; the next sign-in then completes as usual.

### On-the-fly user creation

An **On-the-fly user creation** toggle (default: on) controls step 3 above. When
disabled, only **pre-provisioned** accounts can sign in via OIDC: an identity
that matches no existing account (by SSO subject or email) is refused before any
account is created, and the person is sent back to the login page with a
readable message (`?sso_error=not_provisioned`). Steps 1 and 2 are unaffected —
existing accounts keep signing in and linking by email as before.

While it is on, the [allowed email domains](#restricting-new-accounts-by-email-domain),
when set, restrict it: an identity whose email domain is not in the list is
refused the same way, with the same `?sso_error=not_provisioned`, before any
account is created.

Use this when account creation must go through an explicit administrative step
(invitations, an IdP-side approval flow, or manual provisioning) rather than
being implied by IdP access.

### Automatically admin users

An optional **Automatically admin users** toggle (default: off) grants the admin role to anyone who signs in via OIDC, at every sign-in, existing accounts included: despite its stored name (`oidc_auto_admin_new_users`), it is not limited to new accounts, and the allowed email domains do not narrow it. It is **promote-only** — it never demotes existing admins, and turning it off later does not strip roles already granted. OIDC does not support group mapping; use SAML if you need group-driven roles.

### When sign-in fails

**Changed behaviour.** A failed or abandoned OIDC sign-in used to leave the
browser on a raw JSON error — an API error body, or a validation error about a
missing `code` parameter. It now returns to the login page with a readable
message and a marker in the URL:

| `?sso_error=` | When | What the person sees |
|---|---|---|
| `cancelled` | They declined the consent screen at the identity provider | Sign-in was cancelled; try again when ready |
| `not_authorised` | The XTM Hub entitlement gate declined them (see below) | Their account is not authorised for this instance |
| `not_provisioned` | No account matches their identity and none is created for it: on-the-fly user creation is disabled, or their email domain is not one of the allowed email domains | No account exists; ask an administrator to create one first |
| `account_deactivated` | Their identity matches an account an administrator deactivated (see above) | The account has been deactivated; contact an administrator |
| `misconfigured` | The Discovery URL (or the manual endpoints) gave no usable endpoint: another error status (not 408, 429 or 5xx), a redirect loop, a body that is not a discovery document, or a missing or blank authorization or token endpoint. The server log names the URL, the status and the keys received | Single sign-on is not configured correctly; contact your administrator |
| `failed` | Any other provider error, a malformed callback, or the provider being unreachable, slow or rate-limiting (a connection error, a timeout, 408, 429 or 5xx) while its discovery document is fetched | Single sign-on failed; contact your administrator |

This applies to **every OIDC deployment**, whether or not the XTM Hub gate below
is configured. Nothing about who can sign in changes — only how a failure is
presented.

The message is deliberately vague in every case. The specifics — the provider's
error code and description, or the reason an account was not entitled — are
written to the server log, because someone who has not signed in should not be
able to read your identity provider's configuration back out of an error page.
The marker is scrubbed from the URL once shown, so a reload does not re-accuse
someone who has since signed in successfully.

### XTM Hub entitlement gate

If this instance is federated through XTM Hub, the Hub can decide **which** XTM
One instances each person may reach, and whether they are an administrator on
each one. Set `XTM_HUB_ADMIN_GROUP` to switch it on — see
[Configuration](./configuration.md#xtm-hub-sign-in-gate). The gate identifies
this instance by its `PLATFORM_ID`, which must be set too.

It is off unless configured, and when on it takes precedence over
**Automatically admin users** above: that toggle only promotes, while the Hub
also demotes.

Two consequences are worth knowing before you enable it:

- A person the Hub has not entitled here is refused **before an account is
  created**, so no dormant accounts accumulate — but an existing user who loses
  their entitlement is also refused on their next sign-in.
- Administrator is removed when the Hub stops asserting it. The `ADMIN_EMAIL`
  account is never demoted for exactly this reason, and local sign-in remains
  available as a recovery path if the gate is misconfigured.
- Every role change the Hub causes is written to the audit log, in the same
  shape as one made from the admin UI, so "who became an administrator, and
  when" has one answer regardless of who decided it.

## SAML 2.0

Use SAML for enterprise identity providers that require SAML assertions. XTM One acts as the Service Provider in an SP-initiated flow.

### What you need

- Your IdP **metadata URL** (or XML)
- The **ACS (Assertion Consumer Service) URL**: `{BASE_URL}/api/auth/saml/acs`
- The **Entity ID / SP metadata URL**: `{BASE_URL}/api/auth/saml/metadata`

### Configuration

Configure SAML from `Settings` → `Security` → `Authentication`. The fields are:

| Field | Notes |
|---|---|
| Display Name | Label shown on the sign-in button (e.g. "Corporate SSO") |
| IdP Metadata URL | Your identity provider's federation metadata |
| SP Entity ID | Optional — leave empty to use the metadata URL |

### User provisioning

Users are created automatically on first login, matched by email in the same order as OIDC (SSO subject, then email linking, then create).

SAML has the same **On-the-fly user creation** toggle as OIDC (default: on),
restricted the same way by the [allowed email domains](#restricting-new-accounts-by-email-domain).
When it is disabled, or when the email's domain is not an allowed one, an
assertion that matches no existing account is refused before any account is
created or any group synced, and the browser returns to the login page with
`?sso_error=not_provisioned`.

A deactivated account is refused exactly as with OIDC
(`?sso_error=account_deactivated`, audited as `login_refused`), before its
group memberships are synced: they stay as they were until the account is
re-enabled and signs in again.

### Automatically admin users

Like OIDC, SAML has an **Automatically admin users** toggle (default: off) that grants the admin role at every SAML sign-in, existing accounts included (`saml_auto_admin_new_users`). It is promote-only.

### Group mapping

SAML supports automatic group mapping from IdP group claims (enabled by default). XTM One reads groups from the common claim URIs used by Microsoft Entra ID, Okta, ADFS, and generic SAML providers.

- Groups from the assertion are synced on **every** login: the user is added to matching XTM One groups and removed from groups no longer present in the assertion.
- With **Auto-create groups** enabled (default), groups in the assertion that do not yet exist in XTM One are created automatically.

Both settings live under the SAML section of `Settings` → `Security` → `Authentication`. See [Groups](../administration/users-and-groups.md#groups) for how groups control entity sharing.

The mapping manages only the memberships it created: it never removes someone from a group they joined by hand, through SCIM, or through [Auto join](../administration/users-and-groups.md#auto-join), which adds every single sign-on user (OIDC or SAML) to the groups that have it on, at every sign-in.

## Session lifetime and idle timeout

A sign-in - local, OIDC, SAML, or the XTM One Desktop sign-in through the browser - opens a web session. Two settings bound it, in `Settings` → `Security` → `Security options` (section **Authentication**), or with their environment variable, which then wins and locks the field:

| Field | Variable | Default | What it does |
|---|---|---|---|
| Absolute session lifetime (minutes) | `JWT_EXPIRE_MINUTES` | `10080` (7 days) | The session ends this long after the sign-in, even while it is in use. |
| Idle session timeout (minutes) | `SESSION_IDLE_TIMEOUT_MINUTES` | `0` (off) | The session ends after this many minutes without activity. Activity extends it, never past the absolute lifetime. `0` turns it off; otherwise 5 minutes at least (a smaller variable value is read as 5). |

A change applies without a restart, on every replica within 5 seconds, and is recorded in the audit log:

- **Lowering the lifetime ends the sessions already older than the new value**, on their next request. Raising it applies from the next sign-in: a session keeps the expiry it was signed in with, and a token issued for it later (a password change, an impersonation, XTM One Desktop) never outlasts it.
- **Turning the idle timeout on applies to the sessions already open.** A session with no recorded activity starts counting from its next request, so nobody is signed out the moment it is turned on.
- **Activity is what the person does**: clicking, typing, scrolling with the wheel or touch, or coming back to the tab. What the web app does on its own - refreshing a page, the activity stream, an answer streaming into the chat, keepalives - is not activity, so a tab left open goes idle. A minute before an idle sign-out the web app asks *Are you still there?*; any input keeps the session.
- **The lifetime counts from the sign-in, not from the last token.** Changing a password, impersonating a user (the impersonation ends with the administrator's own session, and they share one idle deadline) or signing in to XTM One Desktop through the browser never extends it.

XTM One Desktop follows the same rules: it keeps the session it was signed in with in the machine's OS keychain, so an update or a restart of the app does not sign the person out, and the session still ends with its lifetime or its idle timeout.

When a session ends, every surface refuses it - the API with a `401` whose `detail.code` is `session_expired`, and a stream still open (the chat, the activity stream) with a `session_expired` event - and the web app signs out and returns to the sign-in page, saying why. SSO users sign in again through their identity provider by choosing it there: the web app never opens the identity provider on its own, so an unattended browser is not signed back in. After signing in, the person lands on the page they were on.

!!! warning "The idle timeout needs Redis"
    The last activity of each session is kept in Redis. While Redis cannot be reached, sessions are **not** ended for inactivity (the absolute lifetime, carried by the session token, still ends them): refusing every session instead would sign everyone out, and refuse every sign-in, for as long as Redis is down. After a failed read, each replica leaves Redis aside for 30 seconds before trying again, so requests are not held up while it does not answer. A warning is logged, and the Security Audit reports the idle timeout as not enforced (`infrastructure.session_idle_timeout_unenforced`).

Neither setting applies to API keys (`fcp-…`), the platform MCP key, MCP OAuth tokens or the tokens OpenCTI, OpenAEV and OpenCRQ sign users in with: they are not web sessions. The Security Audit reports both settings (controls `authentication.session_lifetime`, hardened at a day or less, and `authentication.session_idle_timeout`, hardened at 5 to 30 minutes, within the NIST SP 800-63B AAL2 bound).

## XTM One Desktop sign-in through the browser

[XTM One Desktop](../usage/resources/runners.md) shows the web app in its own window. Email and password work there; single sign-on runs in the system browser instead, because identity providers often refuse or limit embedded windows. There is nothing to configure.

1. In the Desktop app, the person picks a single sign-on provider. The app shows `Continue in your browser` and opens the system browser on that provider.
2. The person signs in there, then enters their XTM One two-factor code if they enrolled one.
3. The browser asks `Sign in to XTM One Desktop?`, naming the machine that asked and who enrolled it. `Yes, sign me in there` hands the session to the app; `Not my machine` refuses the request and records the refusal in the audit log.
4. The app signs in on its own. The browser tab keeps no session.

A request expires after 5 minutes. Only the person who enrolled the machine can confirm a sign-in on it; a company-managed machine accepts any account. A browser session opened through an impersonation or an API key cannot confirm. The confirmed sign-in is recorded in the audit log. The Desktop session follows the [session lifetime and idle timeout](#session-lifetime-and-idle-timeout) below.

This requires the runner subsystem, which is on by default (`RUNNERS_ENABLED`, see [Configuration](configuration.md#runners-and-xtm-one-desktop)).

## Signing in to another product through XTM One (OpenID Connect)

Another product (OpenCRQ first) can sign its users in through XTM One with OpenID Connect, on the platform's OAuth server: the person is sent to the XTM One sign-in page (with whatever method is configured above, two-factor included), or reuses the XTM One session already open in the browser, and comes straight back to it signed in. There is no consent page: the products and XTM One are one platform. Other clients (MCP clients, dynamic registration) keep it. The registration token is what makes a product trusted: whoever holds `PLATFORM_REGISTRATION_TOKEN` can register a platform with a sign-in client on an origin of their choice, and receive the ID token (email, name, title, role, whether it is the `ADMIN_EMAIL` account) of any XTM One user who opens one of their links, without a click. The token already grants more, since a registered platform's URL is trusted to sign users in to XTM One by email, so keep it as secret as an administrator credential and set it only on the products you run. A product registered with a loopback `platform_url` (a development setup) has loopback callbacks: any local program able to open the browser on the authorization URL then completes a sign-in without a click, so keep loopback product URLs to development machines.

- **Discovery**: `/.well-known/openid-configuration` (under a URL path prefix, also `/.well-known/openid-configuration/<prefix>` at the host root, as for the OAuth documents).
- **Client**: the product gets its sign-in client from platform registration (`POST /api/v1/platform/register`, authenticated by `PLATFORM_REGISTRATION_TOKEN`): it sends its callback URLs in `sign_in_redirect_uris` (on its own `platform_url` origin, HTTPS or loopback) and the answer carries its `sign_in_client_id` (`xtm-platform-<identifier>`, a public client: PKCE `S256`, no secret). Each registration keeps the client in step: an empty list removes it, an absent field changes nothing. Unregistering the platform, or an administrator purging its registration, removes it too, and the client signs nobody in while its registration is not active. The published default token, which anyone knows, neither creates nor removes it. Clients made through dynamic registration (`/mcp/oauth/register`, open to anyone) get no OpenID Connect scope.
- **Scopes and claims**: `openid` (always: `iss`, `sub` = the XTM One user id, stable, `aud` = the client id, `iat`, `exp` 10 minutes, `nonce`), `email` (`email`), `profile` (`name`, and `xtm_title`: the person's title, `null` when they have none), `xtm_role` (`xtm_role`: `admin` or `user`, and `xtm_super_admin`: `true` for the `ADMIN_EMAIL` account only, `false` for everyone else). Only the scopes both asked and registered are granted; the token response returns them in `scope`.
- **ID token**: returned by the token endpoint beside the access token, signed `EdDSA` with the platform key published at `/xtm/auth/jwks`. A product's sign-in client needs the ID token only: it gets no refresh token, and its access token is bound to the client (`aud` = the client id), so no MCP endpoint accepts it. Authorizations without consent are audited with `"consent": false`. An administrator impersonating a user is not signed in to products. A product must check `state` and `nonce`, since any page can start a sign-in that completes without a click; `prompt` and `max_age` are not supported. If the sign-in fails, the page offers to go back to the product, which then receives `error=access_denied`, and to try again when that can help (not while impersonating, nor when the product's sign-in is no longer available).
- **Sign-in**: without an XTM One session in the browser, the person goes through the XTM One sign-in page (any method above) and comes back; the XTM One session then also serves the next product.
- A client not registered for `openid` sees no change.

## Terms of Use acceptance

The Terms of Use are accepted **once for the whole platform**, by an administrator on behalf of the organization, not by each user.

Until they are accepted, every person who signs in — with any method above — sees a blocking Terms of Use screen and cannot reach any part of the UI:

- An administrator reads the document (`Read the Terms of Use`), turns on `I have read and accept the Terms of Use on behalf of my organization.` and selects `Accept and continue`. `Decline and sign out` leaves the platform unaccepted.
- Anyone else is told that an administrator must accept the Terms of Use first, and can only sign out.

One acceptance opens the platform for everyone. It is recorded with the version, the date and the accepting administrator, and written to the audit log (action `accept`, entity type `terms_of_use`). The gate is enforced on the API too, so it cannot be skipped by calling the backend directly. It applies to web sessions only: API keys, channel bots and the tokens OpenCTI, OpenAEV and OpenCRQ sign users in with are never gated.

The gate only applies where Agentic AI is licensed. Community Edition deployments never see it. Switching AI off does not remove it.

| Variable | Default | Purpose |
|---|---|---|
| `TERMS_OF_USE_ENABLED` | `true` | Set to `false` to disable the gate entirely — for deployments that handle Terms of Use acceptance outside the product. |
| `TERMS_OF_USE_VERSION` | `2026-07-14` | Identifier of the published document. An empty value disables the gate. |
| `TERMS_OF_USE_URL` | `https://filigran.io/ai-terms/` | Where the document is published. The screen links to it; it is not embedded. |
| `TERMS_OF_USE_EXEMPT_EMAILS` | none | Comma-separated emails the gate never applies to, matched exactly and case-insensitively. For the vendor accounts that operate a SaaS instance: they sign in from the first day of the deployment without accepting the Terms of Use on the customer's behalf. The platform stays unaccepted for everyone else until a customer administrator accepts. An exempt address cannot be taken by self-registration or by a user changing their own email; an administrator, an invitation or SSO creates the account. |

!!! warning "Changing the version re-prompts everyone"
    The platform is gated whenever its recorded acceptance does not match `TERMS_OF_USE_VERSION`. Bumping it is how you roll out a revised document: the screen comes back for everyone (`Updated Terms of Use`) until an administrator accepts the new version. There is no admin UI toggle — a revision is a deliberate configuration change.

## Access provisioning summary

| Method | How users are created | Where |
|---|---|---|
| Admin creates a user | Admin adds an account and sets its role | `Users` → `New user` |
| Admin invitation link | Single-use, email-bound link the invitee opens to set a password | `Users` → `Invite user` |
| Self-registration | Users register with email and password (optionally domain-restricted) | Sign-in page, when **Local Registration** is enabled |
| SSO auto-provisioning | Users are created on first OIDC or SAML login (optionally domain-restricted), linked by email | On first SSO login |
| SCIM provisioning | An identity provider (Microsoft Entra) creates, updates and deactivates accounts; see [Provisioning with SCIM](scim-provisioning.md) | `Settings` → `Access and security` → `Provisioning` |

## Account hardening summary

| Hardening | What it adds | Where |
|---|---|---|
| Two-factor (TOTP) | A six-digit code from an authenticator app, required after the password | `Profile` → `Two-factor` (per user) |
| Admin reset of two-factor | Clears an enrollment for a user who lost their device | `Users` → user detail → **Reset two-factor** |
| Session lifetime and idle timeout | Sessions end a set time after sign-in, and after a set time without activity | `Settings` → `Security` → `Security options` |

## Next step

Once authentication is configured, manage accounts and groups in [Users and groups](../administration/users-and-groups.md). To let OpenCTI, OpenAEV and OpenCRQ users reach XTM One with their own account, see [XTM Suite integration](xtm-suite-integration.md).
