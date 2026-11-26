# My profile

`My Profile` is where you manage your personal experience in XTM One. Unlike `Settings`, which is shared platform configuration, this page is about your own account.

## What you can do here

Use `My Profile` to:

- update your account details
- set your time zone and theme
- change your password, if you use a local account
- protect your account with two-factor authentication
- choose your default chat agent
- tailor the `General Assistant` to your needs
- review the tools you let run without asking you
- control your private channel routing
- let an agent operate a messaging account or a mailbox
- review and clear memory
- check your quotas
- create API keys
- manage your personal MCP exposure
- press the emergency stop, and lift it

## The main profile tabs

The current profile page is organized into:

- `Profile`
- `Tool approvals`, once an agent asks before running tools, or while you hold an approval of your own
- `Channels & Bots`
- `Agent Accounts`
- `Memory`
- `Quotas`
- `API Keys`
- `MCP Endpoint`

!!! note "Looking for the runners tab?"
    Downloading the native app and managing your machines moved to the
    **XTM One Desktop** page: `Get XTM One Desktop` in your user menu. The old `Profile > Runners` address redirects
    there. See [XTM One Desktop and runners](resources/runners.md).

## Profile tab

The `Profile` tab contains your personal account information.

You can usually review or update:

- full name
- title
- time zone
- theme
- authentication method

Your email address is shown but not editable: the other Filigran platforms find your account by it, so an administrator changes it for you.

If you use SSO, the page shows that clearly so you know sign-in is handled by your identity provider.

`Reply suggestions` turns off the next-message suggestion the chat shows once an agent has answered (see [Chat](chat.md#use-a-suggested-reply)). It is on by default, and only works when your administrator has set a routing model.

## Password changes

If you use a local account, the `Profile` tab also includes a password change area.

If you sign in with SSO, password changes are usually managed outside XTM One by your identity provider.

## Two-factor authentication

Right below the password area, the `Profile` tab lets you protect your account with a second factor: a six-digit code from an authenticator app, asked for every time you sign in — after your password, or after your identity provider if you use SSO.

To turn it on:

1. Select `Set up`. A QR code appears.
2. Scan it with your authenticator app — Google Authenticator, Microsoft Authenticator, KeePassXC, 1Password, Bitwarden, or any other. If you cannot scan (for example KeePassXC on a desktop), choose `Enter the key manually` and type the key it shows.
3. Type the six-digit code your app displays and select `Enable two-factor`.

XTM One then shows **10 recovery codes**.

!!! warning "Save your recovery codes now"
    This is the only time they can be displayed. Each one signs you in once, in place of your app, if you lose your phone. Nobody can retrieve them for you afterwards — not even an administrator.

Once enabled, signing in becomes two steps: your password, then the code. If your phone is not to hand, choose `Use a recovery code` on the code screen.

From the same area you can:

- **Generate new recovery codes** — replaces your current set. Requires a code from your app.
- **Disable two-factor** — requires both your password and a current code, so neither an unlocked browser nor your phone alone is enough to remove it.

If you lose both your device and your recovery codes, ask an administrator to reset your two-factor enrollment. Your account then signs in with your password alone until you set it up again on a new device.

!!! info "Works with SSO too"
    If you sign in through an identity provider, you can still enable two-factor here. XTM One asks for the code after your provider signs you in, so the extra factor applies even when your organisation's identity provider does not enforce one. If your account has no XTM One password, disabling only asks for a code.

## General Assistant Tools

The `Profile` tab also contains your personal `General Assistant` tool overrides.

This is where you can review or override access to:

- built-in tools
- integrations
- MCP servers
- custom tools
- knowledge bases

This matters when you want your own `General Assistant` experience to differ from the platform default.

You can also reset your personal choices back to the platform default.

## Tool approvals tab

`Tool approvals` lists the tools you let run without asking you, and lets you take each approval back so the tool asks you again. Under `Tools that ask first`, you can always allow a tool for yourself before it ever asks you, and `Allow every tool for me` turns approvals off for you alone. Your choices never change what other people are asked. See [Human approval of tool calls](../administration/ai-governance.md#human-approval-of-tool-calls).

## Channels & Bots tab

`Channels & Bots` controls how your own chat routing works.

This tab currently lets you:

- choose your default web chat agent
- assign a different agent for your private conversations on supported channels
- fall back to default routing when you do not want a personal override

If connected channel bots are available, you can set a different direct-message agent for each channel. Group-channel behavior remains governed by the shared admin routing, described in [Channels and bots](../administration/channels/index.md).

## Agent Accounts tab

`Agent Accounts` is where you let an agent operate a real messaging account or mailbox: your own, as a delegate, or a dedicated service account, as an AI user. Each account names the integration signed in as that account, the agent that answers, and the rules for what it answers. See [Agent accounts](../administration/channels/agent-accounts.md).

## Memory tab

`Memory` shows what XTM One remembers about you.

It is split into two main parts:

- a `Cross-Agent Profile` shared across agents
- per-agent memory for agents you have interacted with

The cross-agent profile holds only facts about you: your name and role, time zone, language, writing and signature preferences, and key contacts with their roles. Notes about an agent's work for you, such as a pull request on hold, a review pending, a mail-triage rule or an incident, belong to the agent whose work produced them. No other agent reads them. A note about something that can change is shown to its agent as unverified with the date it was written, and is dropped after 72 hours.

A profile saved before profiles and agent notes were kept apart is shown as `Not reorganized yet`. No agent reads it until it is split into your profile and each agent's notes. The split happens on its own in the background, or at your next exchanges with your agents.

From this tab you can:

- review consolidated memory
- inspect recent interactions
- clear the cross-agent profile
- clear memory for one specific agent, including the notes its work left in your profile
- purge all your memory if you want a full reset

This is the best place to check when an agent seems to be remembering something you no longer want it to keep.

## Quotas tab

`Quotas` shows your personal usage against the limits that apply to your account.

In XTM One, this usually focuses on agentic executions. Some other deployments can also show additional quota types.

When your limit is the platform-wide pool of the license, the gauge shows the whole platform's usage against it, including requests with no account behind them (anonymous channel users, public chat widget visitors, calls made with the platform MCP key).

Use this tab to understand:

- your current consumption
- your limit period
- whether the limit comes from a user rule, group rule, or platform default

## API Keys tab

`API Keys` lets you create personal keys for controlled programmatic access.

From this tab you can:

- create a key: a name, and optionally its scopes, expiry and limits (see below)
- copy it when it is first shown: it is never shown again
- review your keys: each one shows its prefix, what it reaches (`Full access`, or its surfaces, `Read-only` and its agents), its expiry, its own limits with what it used of its quota this period, its request count and its last use
- edit a key's name, scopes, expiry and limits with the pencil button
- delete a key you no longer need: anything using it stops working at once

A key that expired, or that an administrator revoked, stays listed and marked as such until you delete it. A key created before an administrator set a maximum lifetime and outliving it is marked `Beyond the maximum lifetime`: it keeps working until you give it an expiry within the limit.

The page also shows the `Platform API` endpoint and basic usage guidance. In some non-XTM deployments, an additional LLM proxy section can appear.

A key acts as you: it can never do more than your account can, and what your account loses (a role, a share, the account itself) the key loses too. You can narrow it further.

### Scopes

When you create a key, you choose:

- **Where it can be used**: the REST API (`/api/v1`), the LLM proxy (`/v1`, Copilot deployments only) and the MCP endpoints (`/mcp/user`, `/mcp/xtm`, `/mcp/opencti`, `/mcp/openaev`, `/mcp/opencrq`). A request on any other surface is refused with `403`.
- **Read-only or read-write**. A read-only key never changes anything:

    | Surface | A read-only key can | It cannot |
    |---|---|---|
    | REST API | `GET`, `HEAD` and `OPTIONS` requests | any `POST`, `PUT`, `PATCH` or `DELETE`, including a `POST` that only searches |
    | LLM proxy | list the models, call a model, count tokens | run an agent (`agent:<slug>`), which acts with the agent's tools |
    | MCP endpoints | connect, list tools and call the tools that only read | call a tool that changes data; on `/mcp/user` every tool runs an agent, so it gets none |

- **Optionally, a set of agents**. The key then only reaches those agents: the `agent:<slug>` models of the LLM proxy, the tools of `/mcp/user`, and on the REST API the agent's own routes (`/api/v1/agents/<id>/...`) and the conversations with it. Everything else is refused: raw models, `/mcp/xtm`, the platform MCP servers and every other REST route.

A key created before scopes existed keeps working as it always did: every surface, read-write, every agent. Narrow it by editing it.

### Expiry

A key can carry an expiry date; from that moment it is refused everywhere (`401 API key expired`). The dialog offers 7, 30 or 90 days or a year (90 days for a new key), `Pick a date`, and `No expiry` while no maximum lifetime is set; editing a key keeps its expiry unless you choose another. Your administrator may set a maximum lifetime: a key you create or re-date must then expire within it, and a key created without a date gets the longest one allowed. You are notified in the notification bell 7 days and 1 day before a key expires, and when an administrator revokes one; the notification opens this tab.

### Limits of its own

A key can carry its own rate limit (requests per minute) and its own quota (requests per day or per month). They count every request the key makes on a surface it may use, on top of your own quotas, which keep applying. A key over its own limit is refused with `429` and a `Retry-After` header, while your other keys keep working.

### Keys made by a key

A key can create or change keys through the API, but never one that reaches further than itself: more surfaces, read-write when it is read-only, more agents, a later expiry, or looser limits than its own are refused. A key with a rate limit or a quota of its own creates no key at all, since each new key would bring a fresh budget of its own; it can still edit keys within its limits.

## MCP Endpoint tab

`MCP Endpoint` is for users who want to connect external MCP clients (Cursor, Claude Desktop, and others) to XTM One.

From this tab you can:

- copy your personal MCP endpoint URL
- review the connection snippet
- choose which agents are exposed
- remove agents from that exposed list

If you do not choose a restricted list, the endpoint can default to all eligible visible agents.

The same tab also shows the **Platform MCP Servers** card (unless an administrator has disabled the feature). Each connected OpenCTI, OpenAEV, or OpenCRQ platform gets its own native MCP server (`/mcp/opencti`, `/mcp/openaev`, `/mcp/opencrq`) exposing its business tools, with the endpoint URL, live connection status, and a ready-to-copy client configuration; platforms that are not yet connected appear dimmed as `Not connected` until they are registered. See [`Platform MCP servers`](resources/platform-mcp-servers.md) for the full guide.

When an administrator has enabled it, a **Platform Tools MCP Server** card appears too. It exposes XTM One's own tools on `/mcp/xtm` — agent traces, agents, assignments, runs, knowledge bases, prompts, skills, custom tools, integrations, and variables — scoped to what you can already see and manage. It is how you read an agent's traces from Claude Desktop or Cursor and fix its configuration there. The card also offers a downloadable installer package for Claude Code, Claude Desktop, and any Agent Plugins client — with **One-click install** on, the package already contains the endpoint URL and a freshly created API key, so there is nothing to configure (and the file is then a secret: delete it once installed). See [`Platform tools MCP server`](resources/platform-tools-mcp-server.md).

## Emergency stop

Your profile menu — your name, top right — also holds the `Emergency stop`
entry. It cancels every agentic activity that runs for you and pauses your
assignments; an administrator can extend it to the whole platform. You confirm
by typing a code shown in the dialog, and you are asked why right after.

Once a stop is in effect, the same menu offers `Resume my activity` (and, for an
administrator, `Resume the whole platform`), and a red banner on every page
offers `Resume` directly. See [Emergency stop](../administration/emergency-stop.md).

## What to change first

For most customer users, the most useful first changes are:

- name and title
- time zone
- default chat agent
- General Assistant tool preferences

## Next step

The [`Settings`](../administration/settings.md) page explains where administrators manage shared platform behavior.
