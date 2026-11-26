# Sharing and access

When you share something, you may also be lending what it carries. An agent bound to your GitHub token lets everyone who can start one of its runs act with your token; an MCP server with your API key in its headers answers for everyone whose agent uses it. XTM One always says so, and never blocks you.

## What can be lent

| Resource | What the people it reaches act with |
|---|---|
| Integration | its stored credential: an OAuth connected account, a personal access token, an API key, a bot token |
| MCP server | its headers, its environment variables, or its own OAuth token |
| Custom tool | its code, run on their behalf |
| Channel bot | its bot token (bots are platform-wide and managed by administrators) |
| Variable | its value, secret or not |
| Knowledge base | its documents, searched and read in full, and written to where the agent may write |
| Skill | its instructions and files |
| Prompt | its text |

A knowledge base, a skill or a prompt is lent by what attaches it: an agent reads the ones it attaches as their owner may, for everyone it serves, whoever is chatting - and so do a flow's knowledge base and skill nodes and the General Assistant's knowledge bases. The confirmation names each one a change lends to new people. A change to the knowledge base, skill or prompt itself (sharing, unsharing or deleting it) does not list it, only what it carries.

## Through what

A resource reaches people in two ways:

- **Directly**: it is shared with people, with groups, or with everyone (company-managed, optionally restricted to named people and groups).
- **Through something that carries it**: an agent bound to it, an assignment override or trigger, a flow node, a knowledge base sync source, a skill's private source, a prompt or custom tool that references a variable, or the General Assistant's platform bindings. What an agent or the General Assistant attaches carries its resources into their runs too: an attached prompt's or skill's variables, a skill's private source, a knowledge base's sync source (`attached prompt`, `attached skill`, `attached knowledge base` on the card).
- **Through a per-user binding**: an agent bound "per user" to a provider has each person use their own integration in a chat, but its assignments run as one person - the agent's owner, or the run-as user of a company-managed assignment - and use that person's own integration of the provider for everyone who sees their work, or, while that person has none connected, the company-wide integration of the provider they may use. The integration's card lists those assignments (`per-user binding`); a per-user fan-out assignment (`Run for all users`) uses each user's own.

An agent's audience includes more than the people it is shared with: anyone with its [public chat link](../web-widget.md), anyone who messages it on a channel that accepts people without an account, its [A2A partners](agent-to-agent.md), anyone holding the platform MCP key when the agent is exposed there, and anyone holding the URL of one of its webhook assignments. A flow's audience includes anyone holding the URL of its webhook trigger once it is active. An A2A partner's task runs as the identity its exposure records, but it is an outside caller like a channel sender without an account: it gets no trace inspection, and the platform builder only reads, as a caller without an account (company-managed objects), and never writes, whatever that identity may do. What the people without an account run counts against the platform-wide agentic limits (see [Requests with no account](../../administration/settings.md#requests-with-no-account)).

Two honest caveats:

- In a chat, people who cannot see an integration bound to an agent, or whose owner's connected account is not shared with them, use their own connected integration of the same provider instead; when they have none, a note under the answer says the integration was not used and why. The agent's runs, public link, channels and A2A partners act with the bound one. So an integration bound to an agent is counted as usable by the agent's owner, its callers without an account, the people who can configure the agent (they can add an assignment), and, when the agent has an enabled assignment that uses the agent's integrations and that they may trigger, everyone who sees the agent - not by the people who only chat with it. Adding someone to a group the agent is shared with announces the integration only on those terms.
- A variable resolves only for someone who can see it, or in a run acting as someone who can (the agent's owner by default).
- An agent bound to an integration its owner can no longer see (the share was revoked, or the owner left the group it was shared with) holds an **orphaned binding**: none of its runs (chats, assignments, public link, channels, A2A, scheduled work items) uses the integration, its tools are refused with the reason `owner_lost_access`, and the owner is no longer counted among the people who can use the integration. The integration's `Who can use it` card flags the agent (`Owner no longer has access`) with an `Unbind` action for whoever can edit the agent, the agent's settings page names the integrations its owner lost, and `get_integration` and `get_agent` report it. Restore the owner's access or remove the binding to clear it.
- When an agent stops reaching someone (their share or their group's removed, they leave the group or it is deleted, a company-managed agent made private, the agent given to another owner), their agents stop handing over to it and filing work items for it: it is removed from their handover targets and work-item owners. An agent you keep reaching stays where you named it.
- Before a change that would orphan bindings (unsharing an integration from a group or a person, making it private, removing a member from a group), the confirmation lists the agents that would lose the integration under `These agents would lose this integration`, with their owner, and `Unbind them too` (checked by default) removes those bindings in the same step for every agent you can edit. The preview API reports them per item as `orphaned_agents`.

An OAuth connected account shared without its owner's consent is shared without the account: the owner keeps it, and the people it reaches authorize the integration with their own - on the integration itself, and every run acts with the account of the person it runs for (see [Integrations](integrations.md#shared-without-the-connected-account-each-person-authorizes-their-own)).

## Where you see it

- **On the resource's page**: a `Sharing and access` card lists every path - shared with a group, with people, company-managed, through an agent, an assignment, a flow, a knowledge base, a skill, a prompt, a custom tool or the General Assistant - with who it reaches and a link to each one you can open. Something you cannot open is named by its owner, without its name. Integrations, MCP servers and custom tools show it on their overview, variables in their edit dialog, channel bots in their tools dialog (`Settings > Channels & Bots`).
- **On an integration's overview**: the card is `Who can use it`, one list grouped by access path with one total at the top: the direct shares (company, groups with their member count, people), then each agent bound to it once (its owner, who it reaches, and what runs with the integration: chats, its assignments, its public link, its channels), then the other entities that carry it. It replaces the former `Sharing and access` and `Agents Using This Integration` cards, which listed every bound agent twice.
- **On the resource's card**: a `Shared - N ways` badge, with the breakdown in its tooltip.

Both are shown to the people who can manage the resource, and both follow every change as soon as it is saved.

## When a person or a group is deleted

When an administrator deletes an account or a group, it leaves the sharing of everything it was given, with its access level, and a resource shared with nobody else reads as not shared. A share with that person saved at the same moment either lands first and is removed with the rest, or is refused because the account is gone. Group shares are not checked that way yet: a sharing form opened before a group was deleted can still save that group back onto its resource, where it grants nothing (the group has no members left) and stays listed until someone removes it. A flow run keeps the audience its flow had when it ran.

## When you are asked

Before a change that lends something to new people, or takes it from people who use it, one dialog lists each resource concerned: whose it is ("your MCP server Tickets and its headers", "Alice's GitHub connected account"), who newly gets it or loses it, through what, what they can do with it and how to avoid it (a company-managed resource instead of a personal one, per-user integrations, `Don't share my connected account`). One click proceeds; nothing is refused and there is no setting. The dialog only opens when someone other than you and the resource's owner is concerned.

You are asked when you:

- **share** a resource or something that carries one: people, groups, company-managed (a flow's too), lifting an access restriction, an owner transfer (an agent, a flow, a skill, a knowledge base, a prompt, an integration, an MCP server, a custom tool), an agent's public web widget, an agent's channels, sharing an agent's personal integrations with its groups from its `Sharing` card;
- **bind** a resource to something shared: an agent's integrations (fixed or per user), pipeline steps, MCP servers, custom tools, channel bots, knowledge bases, skills and prompts, persona, guardrails and human-in-the-loop prompt (variables), an assignment's overrides, trigger, channels, prompt and run-as identity, a flow node, a knowledge base import source, a skill's private source, a prompt's or skill's content, the General Assistant's bindings, knowledge bases and channels;
- **open a public entry point**: activating a flow with a webhook trigger, resuming a webhook assignment, letting a channel bot answer people without an account, exposing an agent on the platform MCP endpoint or generating its key;
- **change a group's members**: an administrator adding someone to a group (or removing them) is told what its shares lend them, by name;
- **create** something already shared: a company-managed integration, agent, prompt, skill, MCP server or custom tool, a channel bot, an assignment on a shared agent;
- **unshare or unbind**: the dialog says who stops having access, with `Unshare` and `Keep sharing` - switching an agent to per-user integrations, unrouting an agent from a channel bot, removing an agent from the platform MCP endpoint or revoking its key included;
- **delete** an agent, an assignment, a flow, an integration, an MCP server, a custom tool, a prompt, a skill, a knowledge base, a channel bot or a variable that others use: the dialog says who loses what, and replaces the usual confirmation.

## API

Every route is read-only and additive:

- `GET /api/v1/exposure/{resources}/{id}`: every path of one resource, with its reason, its audience and, for a carrier, who acts with the resource through it (`lent`). For the people who can manage the resource (an administrator who opens it from a direct link or `Administration > Objects` included).
- `GET /api/v1/exposure/{resources}/summary`: the number of ways each resource you can see and manage is shared, for the list cards.
- `POST /api/v1/exposure/preview`: before a change, which resources it lends to new people (`gained`) and takes from people who could use them (`lost`). Body: `entity_type` (a resource type, or `agent`, `assignment`, `flow`, `knowledge_base`, `skill`, `prompt`, `general_assistant`, and for administrators `group` and `platform_mcp`), `entity_id` (omitted when creating), `parent_id` (the agent, when creating an assignment) and `change` (only the fields the change sets, plus `new_owner_id` for an owner transfer and `delete: true` for a deletion; `remove_channel` for an agent unrouted from a channel; `add_user_ids` / `remove_user_ids` for a group; `mcp_exposed_agent_ids` / `has_mcp_key` for the platform MCP endpoint; `status: "active"` for a flow's activation).

`{resources}` is `integrations`, `mcp-servers`, `custom-tools`, `channel-bots` or `variables`; a preview item may also be a `knowledge_base`, a `skill` or a `prompt` an agent, a flow or the General Assistant attaches. `GET /api/v1/integrations/{id}/exposure`, `GET /api/v1/integrations/exposure/summary` and `POST /api/v1/integrations/exposure/preview` give the same answer for integrations, computed by the same engine, with integration-specific names (`credential`, `credential_usable`, `credentials`, `reached`). The routes never return a secret value, and a carrier you cannot open is listed without its name.
