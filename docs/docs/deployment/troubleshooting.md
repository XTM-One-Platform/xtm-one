# Troubleshooting

This page helps you diagnose common issues on a self-hosted XTM One instance. Filigran SaaS issues are handled by Filigran support.

Start every investigation with the health probes and logs described in [Monitoring](monitoring.md).

## First steps

Before digging into a specific symptom, gather the basics:

```bash
docker compose ps                       # are all services healthy?
curl -s localhost:4000/api/health/ready # 200 = fully ready
docker compose logs platform --tail 100
docker compose logs worker --tail 100
```

Most startup and runtime problems surface in the `platform` or `worker` logs. Set `LOG_LEVEL=debug` temporarily for more detail, then revert.

## The platform will not start

### Readiness never turns green

If `/api/health/ready` keeps returning a non-200 status, the platform cannot reach one of its dependencies or migrations are still running.

- Confirm PostgreSQL, Redis, and Silo/S3 are healthy in `docker compose ps`.
- Check the connection settings (`DATABASE_URL`, `REDIS_URL`, `S3_ENDPOINT`) match the running services.
- Watch the logs for migration progress — a large existing database can take time on the first upgrade.

### Migration errors on startup

Migrations run automatically on startup. If startup fails inside a migration, the logs name the failing revision.

- On a multi-replica deployment, startup migrations are serialized — a replica may wait for another to finish. This is expected.
- Never run migrations manually against production. If a migration fails repeatedly, capture the log and see the [Upgrade](upgrade.md) rollback procedure.

### Decryption or login failures after a restart

If integrations stop working or the admin cannot log in after a restart, `SECRET_KEY` most likely changed.

!!! warning "SECRET_KEY is permanent"
    `SECRET_KEY` encrypts every stored credential and signs sessions. If it changes, all encrypted data becomes unrecoverable and existing sessions are invalidated. Restore the original value — there is no recovery path once it is lost.

Webhook deliveries are refused while the configuration that holds their secret cannot be decrypted: an unreadable configuration may hold a secret, so a delivery is never accepted unverified. The sender gets a `503` and the backend logs an error naming the integration (`Webhook delivery refused (...)`) or, for LinkedIn notifications, the LinkedIn OAuth default. Restore the original `SECRET_KEY` or re-enter the secret; senders that retry a `5xx` deliver again once it is readable. The security audit (`GET /api/v1/security-audit`) names the integrations and OAuth defaults concerned. It also names those whose stored webhook secret is not text (a JSON boolean, float, list or object): no sender can sign with it, so every delivery to them is refused with a `401` until the secret is re-entered as text.

Slack events are the exception: a Slack integration whose signing-secret configuration cannot be read is treated like one with no secret, so its deliveries keep being accepted without a verified signature and the logs name the configuration that cannot be read. With **Require verified integration notifications** on (Settings > Access and security, or `INTEGRATION_NOTIFICATIONS_REQUIRE_VERIFICATION=true`) they are answered `401` instead; do not wait for Slack to redeliver then: it retries a refused event only a few times within minutes, and until the secret is readable again the messages are polled every 5 minutes.

With that option on, the webhook URL of an OAuth default (and, for LinkedIn, the notification URL) answers `503` with `Retry-After` while the OAuth default's client ID cannot be decrypted (by default the delivery is handed out as before and the error is logged): no integration can then be matched to the admin app, so a delivery is refused rather than dropped. The backend logs one error naming the provider. An integration whose own client ID cannot be decrypted receives none of the admin app's deliveries until it is re-entered, and a warning names it.

## Admin cannot sign in

The admin account is reset to the `.env` values on every startup.

- Confirm `ADMIN_EMAIL` and `ADMIN_PASSWORD` in `.env`, then restart to re-apply them. A restart also makes the account an administrator again and re-enables it if it was disabled.
- If you changed the password in the UI, the next restart resets it back to the `.env` value — keep them in sync.
- **Local authentication is off.** The sign-in form answers *"Local authentication is disabled. Please use SSO to sign in."*, for the bootstrap account too. Sign in through SSO with another administrator account and turn `Local Authentication` back on in `Settings` → `Security` → `Authentication`.
- **The two-factor code is refused, or the device is lost.** A restart does not clear two-factor. Sign in with one of the recovery codes, or have another administrator reset the enrollment from the user's page. With `ADMIN_TOKEN` set, the bootstrap administrator's API key needs no second factor and can call `DELETE /api/v1/users/{user_id}/mfa`. See [Two-factor authentication](authentication.md#if-a-user-loses-their-device-and-their-codes).
- **Signed in, but only the Terms of Use screen shows.** The platform's acceptance of the current Terms of Use is pending. An administrator accepts it once for everyone; other users can only wait and sign out. See [Terms of Use acceptance](authentication.md#terms-of-use-acceptance).

## Password reset emails never arrive

`Forgot password?` always answers that a link was sent, whether an email went out or not, so the page cannot be used to find out which accounts exist.

- **No mail server is configured.** Without `SMTP_HOSTNAME`, no email is sent: a self-service request only logs `SMTP not configured — cannot email password reset`, and sending a reset link or an invitation from the UI fails with an error saying email delivery is not configured. Set the `SMTP_*` variables (see [Configuration](configuration.md#email-smtp)) and restart.
- **The account cannot get a link.** A disabled account, an address with no account, or an account that only signs in through SSO gets no link. An SSO user changes their password at their identity provider.
- **The mail server refuses the message.** Check the `platform` logs for the SMTP error, and that `SMTP_STARTTLS` or `SMTP_USE_TLS` matches the server's port (587 or 465).
- **The link expired.** A reset link is valid for `PASSWORD_RESET_EXPIRE_HOURS` (24 hours by default) and works once.

As an administrator, you can also set a new password directly from the user's page, which needs no email (see [Users and groups](../administration/users-and-groups.md)).

## Chat or streaming does not work behind a proxy

If chat hangs, never streams, or disconnects, the reverse proxy is usually the cause.

- Ensure the proxy does not buffer responses: chat answers stream as server-sent events (Nginx honours the `X-Accel-Buffering: no` header the platform sends; turn buffering off on other proxies).
- Raise the proxy read timeout — LLM calls can take 60 seconds or more.
- Forward `X-Forwarded-Proto` and `X-Forwarded-For` so OAuth callbacks and audit logs work, and set `FORWARDED_ALLOW_IPS` to the proxy's address so the platform honours them ([Client address and protocol behind the proxy](installation.md#client-address-and-protocol-behind-the-proxy)). While `TRUST_PROXY_HEADERS` is on, a startup warning names the same fix.

See the ingress requirements in [Installation](installation.md#exposing-the-platform).

## The audit log shows the proxy's address for every user

If every entry of the **Audit Logs** page carries the same address - your ingress controller's, your nginx or Traefik host's, or the Compose network's gateway - the platform runs behind a reverse proxy it does not trust, so it ignores the client address the proxy sends (a client could have set it itself). The platform says so: each process logs one warning naming that address (`A request carried X-Forwarded-For ...`, or `A request named its client in X-Real-IP ...`), and the [security audit](../administration/settings.md#security-audit) (`Settings` → `Security`) reports it as "The audit trail records a proxy's address, not users'", with the value to set. That includes a proxy declared under another address than the one it connects from.

- Set `FORWARDED_ALLOW_IPS` in the platform container's environment to that address, or to a network covering it (the pod network on Kubernetes), and restart the platform: it is read at startup and is not a Settings > Access and security field.
- Make sure the proxy sends `X-Forwarded-For`: the platform reads neither `X-Real-IP` nor `Forwarded`.
- Entries recorded before the change keep the proxy's address; new ones carry each user's own.

See [When the platform reports an undeclared proxy](installation.md#when-the-platform-reports-an-undeclared-proxy) and [Client address and protocol behind the proxy](installation.md#client-address-and-protocol-behind-the-proxy).

## Blank page or 404s when the platform is served under a path

If you expose the platform at `https://host/xtm-one/` and get an empty page, the browser is loading `/assets/...` and `/api/...` from the root of the host instead of from under `/xtm-one/`.

- Set `BASE_PATH=/xtm-one` and include the prefix in `BASE_URL` and `FRONTEND_URL`, then restart. The platform accepts the prefix whether the proxy forwards it or strips it.
- A startup warning `BASE_PATH is /xtm-one but BASE_URL (...) does not end with it` (or the same for `FRONTEND_URL`) means OAuth callbacks, webhook URLs and post-SSO redirects will point at the wrong place until both URLs carry the prefix too.
- The platform refuses to start with `BASE_PATH ... starts with '/api'` (or another platform namespace, or a static file name) or `BASE_PATH may only contain ASCII letters, digits ...`: pick another prefix - the refused values could not be routed unambiguously.
- The platform refuses to start with `BASE_PATH '/tools' is also the address of a platform page`: behind a stripping proxy such a prefix is ambiguous. Set `BASE_PATH_MODE=forwarded` if your proxy keeps the prefix, or `BASE_PATH_MODE=stripped` if it removes it (and make it send `X-Forwarded-Prefix`, as Traefik's `StripPrefix` does and as the nginx example shows) - or pick another prefix.
- An MCP client that cannot complete OAuth sign-in against a prefixed platform (`404` on `/.well-known/oauth-authorization-server/xtm-one`) is asking the root of the host, as RFC 8414 requires: forward `/.well-known/oauth-authorization-server/` and `/.well-known/oauth-protected-resource/` to the platform as well - see [MCP clients authenticating with OAuth](installation.md#mcp-clients-authenticating-with-oauth).

See [Serving under a path prefix](installation.md#serving-under-a-path-prefix).

## A platform is connected but every call to it fails

OpenCTI, OpenAEV or OpenCRQ registers, yet the assistant cannot read its data, or the platform's own calls to XTM One are refused with `401`. The platform and XTM One usually reach each other on internal URLs that differ from the ones your users browse (in Docker, `http://opencti:8080` and `http://xtm-one:4000` instead of `http://localhost:...`).

- Set `OPENCTI_API_URL` / `OPENAEV_API_URL` / `OPENCRQ_API_URL` on XTM One to the URL it reaches the platform on, and leave the platform registering with its public URL: XTM One fetches the platform's keys and calls its API on the internal URL, and keeps addressing its tokens, and every link it gives your users, to the public one. The change applies at the platform's next registration, a few minutes later.
- Set `BASE_URL` on the XTM One worker to the same value as on the platform container: the worker signs the tokens of background work.
- The XTM One log names the refusal: `Untrusted JWT issuer` means the platform signs with a URL other than the one it registered with; `Failed to resolve signing key for <platform> from <url>` means XTM One cannot reach that URL; `audience mismatch (this platform accepts ...)` means the platform addresses its tokens to another URL than `BASE_URL`.
- For an audience mismatch, upgrade the platform to a version that reads `GET /xtm/auth/metadata`, or accept the URL it addresses with `XTM_AUTH_ACCEPTED_AUDIENCES` (see [Public and internal URLs](xtm-suite-integration.md#public-and-internal-urls)).

## Agents give no answer or fail immediately

- Confirm at least one AI provider is configured under `Settings` → `AI Models` with a valid API key.
- Check outbound HTTPS access from the platform (and worker) containers to your AI provider. Air-gapped deployments must use a local provider such as Ollama.
- **Check your quota.** If the agentic execution quota is exhausted, chat is refused with *"You have reached your agentic execution quota for the current period"* and assignment runs fail with a `Quota exceeded` error. Users see their usage in [My Profile → Quotas](../usage/my-profile.md#quotas-tab); admins manage limits and per-user overrides in [Settings → Quotas](../administration/settings.md#quotas). Wait for the period to reset or raise the limit.
- Inspect the run in `Traces` (**View Traces** on the run or its assignment opens the list scoped to it) or the agent's `Activity` tab to see which tool or model call failed.

## AI features are unavailable

Chat, agents and AI actions are locked or refused while the rest of the platform works. Look for the cause in this order:

- **Community Edition.** Without an active Enterprise Edition license, AI entries in the navigation carry a lock. See [Editions and licensing](../administration/editions-and-licensing.md).
- **AI is switched off for the platform.** `Settings` → `Parameters` → `Artificial Intelligence` is `Disabled`, or `Deployment default` with `PLATFORM_MODE=ai_disabled`. See [Running without AI](../administration/running-without-ai.md).
- **AI is switched off for one of the person's groups.** A person in several groups is cut off as soon as one of them has `AI disabled for this group` on, administrators included. See [Users and groups](../administration/users-and-groups.md).
- **An emergency stop is in place.** A platform-wide stop switches AI off and pauses every assignment until an administrator lifts it; a personal stop affects only that person's activity. See [Emergency stop](../administration/emergency-stop.md).

## Runs show as Held

A run, a background task or the pending runs of an evaluation shown as **Held** came due while AI could not run. Nothing is charged while it is held, and it runs by itself once the cause goes away (within about five minutes when AI is turned back on):

- AI is unavailable (Community Edition, or AI switched off for the platform or for the owner's group): see [AI features are unavailable](#ai-features-are-unavailable) above.
- The quota store (Redis) cannot be reached. Quotas then fail closed, so due automations are held rather than run unmetered. Check Redis, as in [First steps](#first-steps).

A hold is bounded by `AI_UNAVAILABLE_HOLD_MAX_SECONDS` (one week by default): work held longer is recorded as failed, with the reason and how long it waited. `0` fails such work at its start instead (see [Configuration](configuration.md#platform-mode)).

## Assignments do not run

Assignments and their delays are driven by the worker's orchestration reconciler and the platform's scheduler.

- Confirm the `worker` container is running and not crash-looping.
- Confirm Redis is reachable — the task queue and scheduling rely on it.
- **Check the run's quota.** A run whose owner is over the agentic quota is marked `failed` with a `Quota exceeded` error rather than executing — verify usage in [Settings → Quotas](../administration/settings.md#quotas).
- A run stuck for a long time is swept and marked failed automatically; re-trigger it after fixing the underlying cause.

## Real-time Slack, Microsoft Graph or LinkedIn events do not arrive

By default nothing is refused that was accepted before: a delivery the platform cannot verify is still accepted, with a warning in the log and a Security Audit finding. Refusals come from a stored secret that does not prove a delivery, or from the **Require verified integration notifications** option (Settings > Access and security, or `INTEGRATION_NOTIFICATIONS_REQUIRE_VERIFICATION`). To go back to the previous behaviour at once, turn the option off (or unset the variable and restart, when it is set); every process applies the change within seconds.

- **Slack answers `401 Invalid Slack signature`** (Slack shows the delivery as failed, and the Request URL check fails): the option is on and no Signing Secret is stored for the app that sent it, or the stored secret is not that app's current Signing Secret (rotated in Slack, or another app). Copy the Signing Secret from the Slack app's `Basic Information` into the integration's `Signing secret` field, or into the Slack OAuth default's for integrations on the administrator's app. Then re-enable the app's Event Subscriptions if Slack turned them off.
- **The log says `Slack delivery ... accepted without a verified signature`**: expected by default while no Signing Secret is stored for the app. Store it to verify that app's events.
- **Slack answers `200` but an assignment does not run in real time once a Signing Secret is stored**: a verified event reaches only the integrations of the workspace it was sent for, and a message outside a public channel only the integration of its author or of the user Slack authorized the event for. An assignment connected only to integrations whose Signing Secret is stored does not receive a delivery that secret does not prove (another Slack app, for example). The poller still delivers, within 5 minutes, what the integration's own token can read.
- **The log says `Graph change notification discarded`**: the notification does not echo the `clientState` of its subscription, or that `clientState` cannot be decrypted. With the option on, a subscription record holding only the assignment id is discarded too: recreate the subscription so it is stored with a `clientState`.
- **LinkedIn answers `401 Invalid LinkedIn signature`**: the notification is not signed with the LinkedIn OAuth default's client secret, as before this release. `503` means that secret cannot be decrypted: restore the `SECRET_KEY` or re-enter it. With the option on and no LinkedIn client secret configured, notifications are answered `200` but trigger nothing: configure the LinkedIn app in Settings > OAuth defaults.
- **An admin OAuth app webhook (`/api/hooks/providers/<provider>`) no longer reaches an assignment** with the option on: its agent has no integration created on that app that its owner may use (or, for a GitHub App, none recording the delivery's installation). While the option is off, the log reports (at most every 15 minutes) the deliveries that reached an assignment it would leave out, so they can be checked before turning it on.

## Knowledge base uploads fail

- Large uploads need a permissive proxy body-size limit (`client_max_body_size 500m;` in Nginx, the size of the largest dataset or evaluation ZIP import).
- Confirm the S3 / Silo credentials and bucket are correct and the endpoint is reachable from both the platform and worker.

## PostgreSQL connection errors under load

Errors about too many connections mean the pool budget exceeds `PG_MAX_CONNECTIONS`. Recalculate using the formula in [Configuration](configuration.md#performance), accounting for every web worker and every scaled worker container.

## Getting help

When opening an issue or asking the community, include:

- the platform version (shown in the UI and container image tag)
- relevant `platform` and `worker` log excerpts (with secrets redacted)
- the output of `docker compose ps` and `/api/health/ready`

!!! tip "Community support"
    For questions the documentation does not answer, join the [Filigran Community on Slack](https://community.filigran.io) or open an issue on [GitHub](https://github.com/XTM-One-Platform/xtm-one/issues).

## Next step

Review the [Upgrade](upgrade.md) procedure before your next version bump, and keep [Monitoring](monitoring.md) alerts in place to catch issues early.
