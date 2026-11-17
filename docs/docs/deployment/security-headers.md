# Security headers

XTM One sends a **Content-Security-Policy** (CSP) on every HTML page it serves: the web application itself, the API reference, the MCP OAuth consent page, the Microsoft Teams tab and the Office add-in task pane. API responses, file downloads and streams carry none.

A CSP tells the browser which scripts, styles, images and connections a page may use. It is defence in depth: if some injected content ever slipped past the platform's own output checks, the browser would still refuse to run a script or load a resource the page never needed.

## Report-only first

The policy is sent in **report-only** mode by default (`CONTENT_SECURITY_POLICY_MODE=report-only`), as a `Content-Security-Policy-Report-Only` header. In this mode the browser blocks nothing. It only reports what the policy *would* have blocked, so turning the policy on cannot break any deployment. The reports tell you whether enforcing it would.

| `CONTENT_SECURITY_POLICY_MODE` | Header | Effect |
|---|---|---|
| `report-only` (default) | `Content-Security-Policy-Report-Only` | Nothing is blocked; violations are reported. |
| `enforce` | `Content-Security-Policy` | The browser blocks whatever the policy does not allow, and still reports it. |
| `off` | none | No policy at all. |

Any other value stops the platform at startup rather than silently meaning another mode. With the policy enforced, the pages also carry `Referrer-Policy: strict-origin-when-cross-origin`, the browser's own default stated explicitly. In `report-only` and `off` modes the platform sends no Referrer-Policy, so one your proxy or CDN adds keeps applying.

The platform never sends `X-Frame-Options`: it cannot name the Teams and Office hosts or the embedders a deployment declares. The `frame-ancestors` directive of the policy does that job once the policy is enforced (see [Who may frame the platform](#who-may-frame-the-platform)).

## Reading the reports

Browsers send their reports to `/api/v1/csp-report`, the policy's `report-uri` (under `BASE_PATH` when one is set). The policy names no `report-to`: a browser that supports it would then ignore `report-uri` and deliver only to an absolute HTTPS endpoint, which the platform cannot name correctly for every deployment. The endpoint needs no authentication, accepts both the `report-uri` format (`application/csp-report`) and the Reporting API format (`application/reports+json`), and is bounded: 64 KiB per request, 32 violations per request, 120 requests per minute per client address. Behind a reverse proxy, the client address is known only once the platform trusts that proxy (`FORWARDED_ALLOW_IPS`, or `TRUST_PROXY_HEADERS`); otherwise every browser shares one budget of 120 requests a minute, and the reports past it are refused and not counted.

Reports are never stored, and never logged one per report:

- the **first time** a violation is seen, the platform logs one warning line:

    ```
    CSP violation first seen: directive=img-src blocked=https://cdn.example.com document=/chat disposition=report source=https://xtm.example.com/assets/index-abc123.js
    ```

- every five minutes, one line summarises the counts of the violations seen in that window:

    ```
    CSP violation summary: 42 violations (3 distinct) in the last 300s, 5 from browser extensions, 0 not tracked; top: img-src blocked https://cdn.example.com on /chat (report) x40; ...
    ```

Every URL is cut down before anything reads it: the query string, the fragment and any credentials are dropped (a signed download link carries its token in the query), a blocked resource is reduced to its origin (or to `inline`, `eval`, `data:`, `blob:`), a page to its path with identifiers replaced by `:id`.

The Prometheus counter `csp_violation_reports_total{directive, disposition, origin}` counts violations per directive, per disposition (`report` or `enforce`) and per origin (`page`, or `extension` when a browser extension caused it). Like the platform's other counters it is per process.

Browser extensions inject scripts and styles into the pages they run on, and those violate the policy too. They are not the platform's: they are counted under `origin="extension"` and in the summary, but never logged one by one.

You can also watch the browser itself: with the developer tools open, a report-only violation appears in the console as `[Report Only] Refused to ...`.

## Deciding when to enforce

The security audit (**Settings > Access and security**, or `GET /api/v1/security-audit`) reports the policy's mode and the violations browsers reported over the last 7 days, summed across every replica:

- `off` is a warning: no policy is sent at all, so framing is not restricted either.
- `report-only` lists the violations of the last 7 days by directive, largest first, browser extensions counted apart. When there is none, enforcing would have blocked nothing the platform's pages loaded in that period. When Redis cannot be read, the audit says the counts are unknown, never zero.
- `enforce` is a pass; a violation reported since is a load the browser blocked, which users may have hit.

Switch to `enforce` once the violations it lists are understood.

The audit also says who may frame the platform (see [Who may frame the platform](#who-may-frame-the-platform)). Only an enforced policy restricts framing: a report-only one blocks no frame, and the platform never sends `X-Frame-Options`.

| Mode | `CONTENT_SECURITY_POLICY_FRAME_ANCESTORS` | Finding |
|---|---|---|
| `off` | anything | none - the `off` warning says framing is not restricted |
| `report-only` | admits every site (`*`, a bare scheme such as `https:`, `https://*`) | `web.csp_frame_ancestors_any`, warning: once enforced, it would still admit every site |
| `report-only` | anything else, empty included | `web.csp_frame_ancestors_not_enforced`, info: any site can frame the platform until the policy is enforced; it names who may frame it then |
| `enforce` | admits every site | `web.csp_frame_ancestors_any`, warning |
| `enforce` | anything else, empty included | `web.csp_frame_ancestors_restricted`, pass: it names who may frame the platform |

While the policy is report-only, a page another site frames is reported as a `frame-ancestors` violation and counted under `frame-ancestors` with the other violations.

Only these counts are kept: one Redis hash per UTC day (`csp:violations:<YYYYMMDD>`), a number per directive, for 8 days. A Redis that does not answer within 2 seconds is read as unknown; the report is still accepted and logged.

The audit's `Controls` view lists both settings as security controls: `web.content_security_policy` (`CONTENT_SECURITY_POLICY_MODE`) and `web.csp_frame_ancestors` (`CONTENT_SECURITY_POLICY_FRAME_ANCESTORS`). Both read `Monitoring` in `report-only` mode, since browsers then report what the policy would block without blocking it, and `Enforced` once the policy is enforced; the framing control reads `Off` whenever an entry admits every site.

## Switching to enforce

1. Run in `report-only` for a while under normal use: the chat, presentations, reports, settings, the Desktop app if you use it, and any embedding (the Teams tab, the Office add-in, an intranet portal).
2. Read the security audit (see [Deciding when to enforce](#deciding-when-to-enforce)), then the `CSP violation first seen` log lines (browser extension violations never appear there). Each one names a resource that `enforce` would block on that page; if it is something the platform legitimately uses, report it rather than working around it: the policy is part of the platform and is fixed there.
3. Set `CONTENT_SECURITY_POLICY_MODE=enforce` and restart. The pages now carry `Content-Security-Policy` instead of the report-only header.
4. To roll back, set `report-only` again and restart. Nothing is stored; the change is immediate.

Three pages stay report-only even in `enforce` mode, because the platform cannot guarantee what they load: the **API reference** (`/api/reference`, which runs a pinned, integrity-checked Scalar release from its CDN whose run-time loads no test exercises), the **Microsoft Teams tab** and the **Office add-in** task pane (both run inside Microsoft hosts). They keep reporting.

## The policy

The web application's policy:

| Directive | Sources | Why |
|---|---|---|
| `default-src` | `'self'` | Everything else comes from the platform itself. |
| `script-src` | `'self'` | The application's own bundle. Inline scripts are allowed by their SHA-256 hash, never by `'unsafe-inline'`; no `eval`. |
| `style-src` | `'self' 'unsafe-inline'` | Diagrams, the diff viewer and the branding colours use inline style elements. |
| `img-src` | `'self' data: blob: https: http:` | The chat shows images an agent links to, integration pickers show provider avatars, previews and generated images use `data:` and `blob:`. |
| `font-src` | `'self' data:` | The typefaces are bundled with the application. |
| `connect-src` | `'self' ipc: http://ipc.localhost wss://<host> ws://<host>` | The API and its streams, the platform's own WebSocket origin (named explicitly for older Safari) and the XTM One Desktop bridge. |
| `media-src` | `'self' blob:` | Text-to-speech playback. |
| `worker-src` | `'self' blob:` | The diff viewer's worker. |
| `frame-src` | `'self'` | The application frames no other site. |
| `object-src` | `'none'` | No plugins. |
| `base-uri` | `'self'` | |
| `form-action` | `'self'` | Every form is submitted by the application; single sign-on starts with a redirect, not a form. |
| `frame-ancestors` | see below | |

The other pages add what they need to it: the MCP OAuth consent page loads its typeface from Google Fonts, cannot be framed by another site, and allows its sign-in form to reach the platform's own `BASE_URL` and nothing else; once the user is signed in, the platform answers with a page that sends the browser on to the MCP client rather than with a redirect, because browsers check every redirect a form submission leads to against `form-action` and a client's callback often redirects again, to an address the platform cannot know. The API reference loads Scalar from `cdn.jsdelivr.net` (whose bundle needs `'unsafe-eval'`, allowed on that page alone), the Teams tab loads the Teams SDK from `res.cdn.office.net`, and the Office add-in loads Office.js from `appsforoffice.microsoft.com` / `ajax.aspnetcdn.com` (which reports telemetry to Microsoft) and its typefaces from Google Fonts.

## Who may frame the platform

With the policy enforced, `frame-ancestors` decides which sites may show a page in an iframe. In `report-only` mode it decides nothing: the browser reports a page framed by a site the policy does not allow, but shows it, and since the platform never sends `X-Frame-Options`, any site can frame the platform until the policy is enforced.

| Page | May be framed by, once the policy is enforced |
|---|---|
| The web application | The platform itself, the origins listed in `CONTENT_SECURITY_POLICY_FRAME_ANCESTORS`, and - only when `PLATFORM_REGISTRATION_TOKEN` is a value of your own, not unset and not the published default, which lets anyone register - the platforms registered with it (their registered URLs, picked up at registration, dropped as soon as a platform unregisters, is purged or moves to another origin, and re-read every five minutes). |
| MCP OAuth consent page | Only the platform itself - it is opened in a browser window, never framed. |
| API reference | Any site: the page stays report-only in every mode, so its `frame-ancestors` is only reported. |
| Microsoft Teams tab, Office add-in | Any site (their Microsoft hosts). |

If another site embeds XTM One in an iframe, an intranet portal for example, list its origin before enforcing:

```
CONTENT_SECURITY_POLICY_FRAME_ANCESTORS=https://portal.example.com,https://*.intranet.example.com
```

The same goes for an OpenCTI or OpenAEV instance that frames XTM One while `PLATFORM_REGISTRATION_TOKEN` is unset or the published default: a registration then proves nothing, so its origin is not trusted until you list it here. Registered platforms past the first 20 (in sorted order) are not named either, since each lengthens the header of every page; the platform logs a warning with their number, and you list the others here.

A malformed entry stops the platform at startup. An entry that admits every site - `*`, a bare scheme such as `https:`, or `https://*` - is accepted, but the platform logs a warning at every startup: once the policy is enforced, any such site may show the platform in an iframe, which is what `frame-ancestors` is there to prevent. Name the sites that embed the platform instead.

## Behind a reverse proxy

- The platform sends the headers itself; the proxy does not need to add any. If it adds its own `Content-Security-Policy`, the browser enforces both policies, and a request must satisfy both.
- Do not add `X-Frame-Options` at the proxy: it would break the Teams and Office embeddings and any portal you declared.
- A script the proxy or a CDN injects into the pages (an analytics snippet, Cloudflare Rocket Loader or email obfuscation) is not in the policy: it is reported in `report-only` mode and blocked once the policy is enforced.
- Under `BASE_PATH`, the report endpoint carries the prefix automatically.

## The embeddable chat widget

The [public chat widget](../usage/web-widget.md) (`/api/v1/public/widget.js`) runs inside the page of the site that embeds it, not in a page of the platform, so the platform's policy does not apply to it. If that site sends its own Content-Security-Policy, it must allow what the widget loads - the XTM One origin, Google Fonts and inline styles among others: see [Embedding the chat widget on a site with its own Content-Security-Policy](configuration.md#security-feature-flags).

## Development mode

In development (`docker-compose.dev.yml`), the Vite development server serves the web application itself, so no policy is sent. The policy applies to the platform image, where the backend serves the built application.
