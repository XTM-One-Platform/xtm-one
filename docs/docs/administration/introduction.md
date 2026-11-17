# Introduction

This chapter is for administrators. It covers the areas used to manage the platform, monitor usage, and configure shared settings.

## Administration is for

Use the administration sections when you need to:

- review and route intents
- manage users and groups
- describe your organization for every agent
- inspect platform objects and logs
- adjust shared platform settings
- stop every agentic activity at once with the [emergency stop](emergency-stop.md)

## Intents

Intents connect user needs to agents. They are also how an agent becomes available inside OpenCTI, OpenAEV, or OpenCRQ.

Use this section when you want to:

- review which intents exist
- see which agents are bound to them
- check which platforms declared them
- adjust routing priorities
- make one of your own agents answer a capability in a connected product

For most teams, this is the page that explains why one request reaches one agent instead of another. The [`Intents`](intents.md) page covers bindings, verticals, and priorities in detail.

## Users

The Users section is where administrators manage accounts: create a user or send an invitation, promote or demote, disable, enable or delete an account, reset a password or a two-factor enrollment, impersonate a user, set their quotas and review their API keys and activity.

See [Users and groups](users-and-groups.md) for every task.

## Groups

Groups organize users: a resource shared with a group is shared with its members. A group also carries quotas, an AI switch and managers who follow its members' AI usage.

See [Users and groups](users-and-groups.md#groups) for every task.

### Switching AI off for a group

The `AI access` card of a group turns AI off for its members. See [Switching AI off for a group](users-and-groups.md#switching-ai-off-for-a-group).

## Objects

Objects gives administrators a cross-user view of the platform's library entities, whoever owns them: agents, knowledge bases, prompts, skills, MCP servers, custom tools, integrations, presentations, videos (the Video Studio's, opened in their editor), templates, machines (the computers enrolled with XTM One Desktop or the headless runner), flows, datasets, evaluations, variables, channel bots and fleets (archived fleets included, marked Archived, the others with no status since whether a fleet runs is shown on the Fleets page; a deleted fleet is no longer an object and stays in the audit log). A type whose pages your deployment does not offer (Flows, Evaluations, custom variables, XTM One Desktop) is not listed either.

The library pages (**Agents**, **Presentations**, **Templates**, **Flows**, **Evaluations**, **Variables**, the **XTM One Desktop** page, and so on) list the same three tiers for everyone, administrators included: your own items, the items shared with you, and the items published to the whole company. Objects is where you find an item of one of these types that another person owns and has not shared.

Once you open an item, you see it whole, whoever owns it: an agent shows every integration, knowledge base, tool, skill, prompt, handover and assignment it is bound to, its runs, its chats and its delegations, and each of them opens and can be edited, as can the item itself. Reading the delegations of an agent that other users started (the consultations, transfers and background tasks on its **Delegations** tab carry their task descriptions and results) is recorded in **Logs** (action `break_glass`, entity `Agent Delegations`), naming the agent and the users whose delegations you read, one entry per administrator, agent and fifteen-minute window; your own delegations are not counted. The same holds the other way round: an integration, a knowledge base or a tool lists every agent that uses it. When you edit a colleague's agent, its pickers keep what the agent is already bound to, next to the items you can see yourself.

Use it to:

- search any of these entities by name
- filter by type or by owner
- open any item directly (a row leads to the item's own page)

This is helpful when you need a cross-platform inventory, or when a user asks you to look at something they built, rather than a page-by-page review.

### Machines

Filter by `Machines` to see every computer enrolled with the platform, whoever enrolled it: how many there are, who owns each one, and whether it is still deployed. Each row shows the machine's operating system, whether it is `Online`, `Offline` (with the time it was last seen) or `Revoked`, and the version of the runner it runs. The connection state is the live one the XTM One Desktop page shows, not a stored flag, so a machine that stopped without disconnecting reads as offline here too.

Opening a row shows the machine's own page: its host details, its permission model, its pending approvals and its recent commands, with the same edit, revoke and delete actions an administrator has on the XTM One Desktop page. Reading the command history of a machine you neither own nor hold a configure-level share on (a view-only share is not enough - the full history then comes from your administrator role; a company-managed machine is yours to manage and is not counted) is recorded in **Logs** (action `break_glass`, entity `Runner Commands`): the commands carry shell output and file contents, so the trail says who looked at whose machine, one entry per administrator, machine and fifteen-minute window. Machines are not listed when the runner feature is disabled for the deployment.

## Logs

Logs show the audit trail of user actions and important platform events: what was created, changed or deleted, by whom and when, and the configuration changes. When something changed and no one is sure why, this is usually the first admin page to check.

Narrow the list with:

- the search box, which matches an entry's summary, the name of the item it is about and the email of the person who acted;
- `User`, `Action` (for example `create`, `update`, `delete`) and `Entity` (the kind of item: agent, user, group and so on);
- a date range.

The filters are kept in the address, so you can share a filtered view. Select an entry to open it as an `Audit Log Event`, with its full detail and the `Raw Event (JSON)`, which you can copy.

Entries with the action `break_glass` record an administrator reading what belongs to someone else: the files of another person's conversation, the delegations of an agent other people started (entity `Agent Delegations`, see [Objects](#objects)), or the command history of a machine (entity `Runner Commands`, see [Machines](#machines)).

Audit log entries are kept for the `Audit log retention (days)` set in [Settings > Observability](settings.md#observability), 90 days by default; the page says how long. An hourly job deletes older entries, except each user's most recent sign-in, which keeps their last login known. Deleting a user also deletes the entries of that user's own actions (see [Users and groups](users-and-groups.md#disable-enable-or-delete-an-account)). A user's own entries are also on the `Activity` tab of their page. Scripts read the log with `GET /api/v1/audit-logs` (administrators only; `search`, `user_id`, `action`, `entity_type`, `date_from`, `date_to`, `limit` up to 200 and `offset`).

## Settings

Settings is where administrators configure the shared behavior of XTM One.

Common areas include:

- AI models
- general assistant behavior
- channels and bots
- integrations
- quotas
- MCP endpoint settings
- security
- observability
- platform parameters
- the organization profile (see [Organization profile](organization.md))

The [`Settings`](settings.md) page covers those tabs in more detail.

## What may vary by deployment

The standard XTM One administration menu focuses on intents, users, groups, objects, logs, and settings.

Some deployments may include additional administration modules beyond this core set.

## What regular users should know

Regular users usually do not need the administration sections.

If you cannot see them, that is normal. Access is usually limited to administrators.

## Next step

Continue with [Intents](intents.md) to route capabilities to agents, [Settings](settings.md) for the detailed platform configuration areas, or [My profile](../usage/my-profile.md) for the settings users manage themselves.
