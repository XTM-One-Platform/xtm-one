# Cross-platform token identity on internal URLs

The products of the XTM Suite sign users in to each other with short-lived
EdDSA JWTs (`app/core/xtm_auth.py`): XTM One mints one per user for every call
it makes to OpenCTI, OpenAEV or OpenCRQ, and they mint one for every call they
make to XTM One. This page is for the people implementing either side.

## One product, three URLs

A product is known by ONE URL, its **identity**: the `iss` of every token it
signs and the `aud` of every token addressed to it. For a registered platform
that is the `platform_url` it registers with; for XTM One it is `BASE_URL`.

`POST /api/v1/platform/register` refuses, with a 422, a `platform_url` that
names no identity (the registration token is checked first, before the body
is read): not an http(s) URL with a host, or one carrying userinfo,
a query string, a fragment, whitespace, a control character or a backslash,
or longer than 2048 characters
(`RegisterRequest._platform_url_names_an_identity`, the strict
`url_identity()`). The value is stored as sent. A row stored before that check
is not migrated: it names no identity, so no token is ever accepted from it,
its next heartbeat is refused, and every surface that links to it goes
through `safeHttpUrl()` (`frontend/src/lib/safe-url.ts`) and shows the text.

Where the other side **reaches** it is another URL whenever the containers share
a network. In the standard Docker deployments (`OpenCTI-Platform/docker`,
`OpenAEV-Platform/docker`) OpenCTI is `http://localhost:8080` for its users and
`http://opencti:8080` for XTM One, and XTM One is `http://localhost:8090` for
its users and `http://xtm-one:4000` for the platforms. Using one URL for
the three roles on each side lets registration succeed while every
signed call is refused with 401.

The rule on both sides: **trust, addressing and every link shown to a person
follow the identity, the network follows the reach URL.** Nothing a token carries, and nothing a user can edit,
ever chooses where keys are fetched from or who receives an identity-addressed
token.

## What XTM One does

`app/core/platform_identity.py` is the one place that reads it:

- **The reach URL** of a platform is `PlatformRegistration.api_url`: the
  `{PROVIDER}_API_URL` override of XTM One's OWN environment, captured by the
  registration (also the heartbeat) and never read from the request nor from
  the integration's editable `extra_config.base_url`. `NULL` = reached on
  `platform_url`. `captured_api_url()` drops an override that names the
  identity itself or names no identity, and the stored value is re-validated
  whenever it is read, so PyJWKClient's urllib is never handed a non-http(s)
  key location.
- **Incoming tokens** (`xtm_auth._verify_xtm_jwt`): the `iss` must be the
  identity of an active registration (`platform_for_issuer`, SQL-filtered,
  exact spelling first, then normalized, the most recently seen first); the
  keys are fetched at `{reach URL}/xtm/auth/jwks`, then at
  `{identity as registered}/xtm/auth/jwks` when the internal URL does not serve
  them (where they were always fetched before), never at the token's own
  spelling of its `iss`; the `aud` (a string or an array, RFC 7519) must name
  `BASE_URL` or an entry of `XTM_AUTH_ACCEPTED_AUDIENCES`, compared
  normalized. The internal URL is not an identity: a token signed as
  `http://opencti:8080` is refused. The token must carry `exp` and `iat`
  (PyJWT only checks an `exp` that is present, so a token without one would
  never expire) and live at most `XTM_JWT_MAX_LIFETIME` (15 minutes,
  `exp - iat`, read off the claims alone, so the leeway below never widens
  it; an `iat` more than `JWT_CLOCK_SKEW_LEEWAY` in the future is refused
  first, so this caps `exp` at that leeway past XTM One's clock plus the
  maximum). `exp`, `nbf` and `iat` are checked with
  `JWT_CLOCK_SKEW_LEEWAY` (60 s, `app/core/security.py`): a token minted by a
  clock up to 60 s ahead of XTM One's (OpenCTI signs `nbf` = `iat` = its own
  now) or expired less than 60 s ago is accepted. The two leeways add up: a
  token is accepted from its `iat` minus 60 s to its `exp` plus 60 s, so at
  most 17 minutes of XTM One's clock (the 15-minute cap plus twice the
  leeway). Every verifier of a token minted on another host passes the same
  constant (the A2A Entra and Google federation, and XTM One's own HS256
  tokens, which one replica or the worker mints and another verifies); only
  the Teams and Google Chat webhooks keep the 5 minutes their vendors document.
- **Outgoing tokens** (`platform_token_audience`): the platform tools call the
  integration's `base_url` and address the token to the `platform_url` of the
  registration that owns the integration, only when that `base_url` is a URL
  the registration vouches for (its identity or its captured internal URL). An
  integration no registration owns (a user's own OpenCTI integration in
  Copilot mode), or whose `base_url` was edited to another host, gets a token
  addressed to that `base_url`, as before, so no edit can forward a token the
  real platform would accept.
- **OpenAEV autonomous run callbacks** (`resolve_platform_call`, called by the
  status, scope, directives and events callbacks of
  `services/autonomous_pentest.py` and the worker's `_post_run_status`): the
  `callback_base_url` OpenAEV sends at start is its identity; the calls go to
  its reach URL, addressed to that identity as registered. A callback naming a
  registration's internal URL is addressed to its identity too; any other URL
  is called and addressed as given, and so is every URL when the registrations
  cannot be read - a status callback is never lost to the lookup.
- **Links shown to people** (`RegisteredPlatform.shown`, one rule for every
  site): the internal URL is only ever called server to server; every URL a
  person is handed is on the identity. A URL on the reach URL (scheme and host
  in any case, the default port written or not, the path as recorded) is moved
  onto the identity with its path, query and fragment; another host, port,
  path or a URL with userinfo is left alone. The identity is written in its
  canonical, link-safe form, never as the registration's raw text
  (`_shown_identity`): an internationalized host is written in its IDNA form,
  a path byte a link cannot carry (space, quote, parenthesis, angle bracket,
  control character, non-ASCII letter) is percent-encoded, and a `%` that
  starts no valid escape is encoded as `%25`. A registered URL that names no
  identity (userinfo, query, fragment, port 0, not http(s)) or whose host is
  not a host name or an IP literal is never shown, and its platform's links
  stay as they are. `platforms_reached_internally()`
  batches the lookup (one query, none without a platform-managed integration;
  the registration `platform_for_integration` would pick). The sites: the
  platform tools' results (`integration_tools._show_tool_results_on_public_url`,
  wrapped around every handler of an integration whose registration recorded
  an internal URL - the calls still go to the internal URL), the
  "Connected integrations" URL the agent builds links from
  (`get_integration_public_url(integ, platform)`), the answer's links
  (`url_guard.finalize_platform_links`: moved outside code spans, then
  guarded; the final answers of agent chat, the General Assistant and
  assignment runs, and every mid-turn segment; off with `URL_GUARD_ENABLED`),
  and `trigger.platform_url` of the webhooks (`webhooks._shown_platform_url`).
  The scope matcher (`resolve_integration_by_scope`) takes the identity and
  the internal URL of the owning registration as the same platform. Without a
  recorded internal URL every site behaves as before.
- **Strict comparison** (`app/core/url_identity.py`, RFC 3986 sections 6.2.2
  and 6.2.3): scheme and host case, the default port and the trailing slash are
  ignored; another scheme, host, port or path is another identity, and a URL
  carrying userinfo, a query or a fragment, or naming port 0, names none.
  `BASE_URL` and a run's callback base URL are read leniently
  (`xtm_auth._normalize_url`: userinfo dropped), as they always were.
- **Configuration errors never print a credential**: a refused
  `XTM_AUTH_ACCEPTED_AUDIENCES` entry raises `URLSettingError` (not a
  `ValueError`, which pydantic would wrap with the raw input), and every
  message and log line quotes a URL through `redact_url()`.

## Who an incoming token acts as

An incoming token acts as the XTM One account holding its `email`, compared
case-insensitively (`deps._resolve_trusted_platform_jwt`), never by `sub`; an
account no one holds is created with the `user` role. The account keeps its
own role: a token naming an administrator's email acts as that administrator.
This is an accepted trust delegation: registering a platform trusts it to say
who its user is, so whoever controls the email of a platform user (that
platform's administrators, its signing key) controls the XTM One account
holding that email, and these tokens skip XTM One's two-factor and session
limits. Register only platforms trusted at that level, and only while they do
not let their users set their own email without proving it: one that does lets
any of its users act as any XTM One user, administrators included. Routes that rely on it
for administrators include the invitation an access notice makes
(`platform_access_notices.may_invite`).

## What XTM One publishes

`GET /xtm/auth/metadata` (public, next to `/xtm/auth/jwks`, served under
`BASE_PATH` in both proxy shapes, `Cache-Control: public, max-age=3600`):

```json
{
  "issuer": "http://localhost:8090",
  "audience": "http://localhost:8090",
  "jwks_uri": "http://localhost:8090/xtm/auth/jwks",
  "token_signing_alg_values_supported": ["EdDSA"]
}
```

`issuer` and `audience` are `BASE_URL`, normalized, with the `BASE_PATH` prefix
it carries. `jwks_uri` is the public URL of the keys, which a platform on the
internal network may not reach.

## What a platform does (OpenCTI, OpenAEV, OpenCRQ)

1. Read `{configured XTM One URL}/xtm/auth/metadata` (e.g.
   `http://xtm-one:4000/xtm/auth/metadata`) and cache it (an hour). A `404` is
   an XTM One without the document: keep treating the configured URL as its
   identity.
2. Verifying a token from XTM One: trust it when its `iss` equals `issuer`
   (normalized), and fetch the keys at `{configured XTM One URL}/xtm/auth/jwks`,
   never at the token's `iss` nor, when it is not reachable, at `jwks_uri`.
3. Minting a token for XTM One: `iss` is the URL the platform registers with
   (its `platform_url`), `aud` is `audience`, `iat` is now and `exp` is at most
   10 minutes later (OpenCTI, OpenAEV and OpenCRQ all sign for 10 minutes; XTM
   One refuses a token without `exp` or `iat`, or one living more than 15
   minutes). `sub`, `email` and `jti` are required too.
4. Verifying the `aud` of a token from XTM One: compare it, normalized, with the
   URL the platform registers with. XTM One addresses its tokens there even when
   it calls an internal URL.
5. OpenAEV: keep sending its own base URL (the registered one) as the
   autonomous run's `callback_base_url`.
6. Telling someone they were given access (OpenCRQ: a member added to a
   tenant): `POST /api/v1/platform/access-notices` with the acting
   administrator's token, `recipient_email` or `recipient_user_id` (exactly
   one), `scope_name` and `path`. `recipient_user_id` is the person's XTM One
   id, the `sub` of their sign-in; `scope_name` is what was opened (one
   printable line, 120 characters at most); `path` is a path on the platform
   (`/` by default).
   - An active account holder gets an email from a fixed template whose link
     is `path` on the platform's registered URL, never another host:
     `{"outcome": "notified", "user_id", "email_sent", "link": null}`.
   - An email no account holds, when the acting user may invite (an XTM One
     administrator, `may_invite()`): XTM One makes one of its invitations
     (role `user`) and emails it from a fixed "You're invited to {platform}"
     template: `{"outcome": "invited", "email_sent", "link"}`. `link` is the
     invitation's `/register?token=` link when it could not be emailed, so the
     platform shows it to the administrator who added the person (XTM One
     keeps only its hash), and `null` once emailed, as `POST
     /api/v1/invitations` returns its link only with `send_email=false`. A
     pending invitation a notice made for that email (role `user`, with a
     destination) is sent again under a new link (the earlier one stops
     working; `link` is the new one), never doubled, valid for the longer of
     what it had left and the invitation lifetime; any other pending
     invitation for that email, one an administrator made on the Invitations
     page, answers 409 and is left as it is, as `POST /api/v1/invitations`
     answers.
     Registering from it answers a `redirect_url`, `path` on the platform's
     public URL as registered at that moment, and the web app goes there.
   - An email no account holds and an acting user who may not invite: 403,
     nothing created.
   - A deactivated account is never invited and answers as the directory
     shows it: by id, the 404 of an unknown id (`GET
     /api/v1/users/directory?ids=` tells as much); by email, the 403 of an
     unknown email to a user who may not invite, a 404 to one who may (an
     administrator's `GET /api/v1/users` lists deactivated accounts). A
     malformed id is the same 404. Answering `invited` tells the platform
     that an email has no account, which the directory does not: only an
     administrator acting through a registered platform gets it.

   `email_sent` is false when XTM One could not send (no email server
   configured included), so the platform tells the person another way: for
   an invitation, with its `link`.
   `scope_name` and the administrator's name are shown as text no mail
   client turns into a link. Only a token signed by an active registration is
   accepted (403 for a session or an API key). Each hour, one administrator
   sends 200 notices or invitations, one platform 1,000, and one person
   receives 5: past that, 429 with `Retry-After`.
7. Forwarding the current user to another registered platform (OpenCTI or
   OpenAEV calling OpenCRQ's lookup with `X-XTM-User-Token`): `POST
   /xtm/auth/exchange` with the user's own token (the one of step 3) as
   bearer and `{"audience": "<target platform_url>"}`. XTM One verifies the
   bearer as at sign-in and answers `{"access_token", "token_type":
   "Bearer", "expires_in": 600, "audience"}` (`Cache-Control: no-store`):
   an XTM One token (EdDSA, `/xtm/auth/jwks`) whose `sub` is the user's XTM
   One id, `email` their XTM One email and `aud` the target's registered
   identity, valid 10 minutes. The user is the one the bearer names, never
   one the body names. Refused: 401 for anything but an active
   registration's valid user token (session, API key, unknown, suspended or
   revoked registration, expired, another audience); 403 for an audience
   that is not another active registration (the caller itself and XTM One
   included) and for a user with no active XTM One account (nothing is
   provisioned). One platform makes 600 exchanges a minute, then 429. The
   token is never logged. A platform that gets no token keeps its behaviour
   without it (OpenCRQ: links only).

Until a platform does 1 to 3, an operator can accept the internal URL it
addresses its tokens to with `XTM_AUTH_ACCEPTED_AUDIENCES=http://xtm-one:4000`
on XTM One; the Security Audit reports it
(`authentication.platform_token_audiences`) and XTM One logs it at startup.
