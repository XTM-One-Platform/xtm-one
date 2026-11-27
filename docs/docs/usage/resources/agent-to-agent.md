# Agent-to-agent (A2A)

A2A is an open protocol that lets agents on different platforms give each other work. With it, an agent running on a partner's platform — Azure Foundry, Google Agent Platform, or another XTM One deployment — can send a task to one of your agents, and one of your agents can send a task to a partner's agent.

This page covers both directions: exposing one of your agents to a partner, and calling a partner's agent from XTM One.

!!! note "Not the same as handovers"
    Agents on your own platform hand work to each other without A2A. That cooperation is shown in [`Agentic Flow`](../agentic-flow.md). A2A is for agents that live on another platform.

## Before you start

A2A is available while agentic chat is: AI is switched on and the platform runs in Enterprise Edition (see [Editions and licensing](../../administration/editions-and-licensing.md)). There is nothing else to enable. When agentic chat is not available, the A2A cards in the integration catalog explain what is missing, and an agent that already has A2A bindings keeps them: its agent card is not served and no delegation tool is offered until agentic chat is back.

Partners reach your agents over HTTPS only. A request XTM One cannot confirm as encrypted is refused, not redirected. If your platform sits behind a reverse proxy that terminates TLS, your administrator must name that proxy as trusted, or every partner request is refused: see `FORWARDED_ALLOW_IPS` in [Configuration](../../deployment/configuration.md#security).

## Choose a mechanism and a direction

Every A2A relationship is an integration. Open `Integrations`, search the catalog for `A2A` (the cards carry the `Agent to Agent (A2A)` tag), and pick the mechanism your partner supports:

| Catalog entry | Use it when the partner |
| --- | --- |
| `OAuth A2A` | can exchange a client ID and secret for a token on its own — Google ADK, or another XTM One deployment |
| `Foundry A2A` | runs on Azure Foundry and authenticates with Microsoft Entra |
| `Google Agent Platform A2A` | runs on Google Agent Platform (Vertex AI Agent Runtime) |

The creation form then asks for a `Direction`:

- `Inbound: a partner tasks our agents` — binding this integration to an agent exposes that agent to the partner.
- `Outbound: our agents task a partner` — binding this integration to an agent gives it a tool to delegate work to the partner.

An integration serves one direction. To change it later, unbind it from every agent first. An agent that builds the integration with the `create_integration` tool sets the direction with its `a2a_direction` argument (`inbound` when omitted), changes it with `update_integration`, and reads it with `get_integration`. The agent's `Overview` tab shows which way each A2A binding points, with an `Exposes agent` or a `Delegates to partner` badge.

## Expose one of your agents

There is no separate switch: an agent is reachable by a partner exactly while it is bound to an inbound A2A integration.

1. Create the integration with the `Inbound: a partner tasks our agents` direction.
2. Give it the partner's credential details:
    - `OAuth A2A`: save the integration, then on its `Configuration` tab, in the `Partner credential` card, choose `Generate credential`. The client secret is shown once, at that moment — copy it before you leave the page.
    - `Foundry A2A`: fill in the partner's tenant ID, the audience of a dedicated app registration that represents XTM One, and the one partner identity allowed to call. The setup guide walks through the Azure side.
    - `Google Agent Platform A2A`: fill in the audience the partner's tokens carry and the one service account allowed to call.
3. Open the agent, then `Configuration` > `Tooling`. In the `Integrations` card, choose `Edit`, click the integration to bind it, and `Save`.

The `Partner reachability` card on the integration's `Configuration` tab tells you what is still missing — no credential yet, no pins, or no agent bound — and whether the partner has connected.

Binding is a change to the agent, so it follows the same rule as editing it: the owner, an administrator, or someone the agent is shared with at configuration level. A company-managed agent, built-in agents included, can be exposed only by an administrator. A company-managed agent that is not built in can be bound only to company-managed integrations.

!!! tip "One credential per partner"
    An `OAuth A2A` credential works on every agent its integration is bound to. To keep a leak limited to one agent, create one integration per agent.

## Share the agent card URL

Each exposed agent gets its own agent card URL: the address the partner registers on its side. The card is public and tells the partner what the agent does and how to authenticate.

Find it on the integration's `Configuration` tab, in the `Agent card URLs` card: one row per bound agent, with a button to copy it. Send the partner:

- `OAuth A2A`: the card URL, the `Client ID` and the client secret. The partner exchanges them for a short-lived token at the address the card gives.
- `Foundry A2A` and `Google Agent Platform A2A`: the card URL only. No secret is stored on either side; the partner presents a token its own cloud issues, and XTM One checks it against the values you entered.

## Whose identity the work runs as

A partner's task runs as the agent's owner — never as the person who bound the integration. For a company-managed agent, that is the account that created it, often an administrator. The `Integrations` card on the agent's `Tooling` tab names that user in a line that starts with `External work runs as`.

Read that line before you send the card URL. The partner reaches everything that user can see, their personal and group-shared data included, through the agent's whole tool set and the agents it hands work to. There is no per-partner narrowing of tools. If that is too much, do not expose the agent, or duplicate it under an owner whose reach fits.

The work counts against that user's agentic quota. A partner is still treated as an outside caller: it gets no trace inspection, cannot change platform objects and cannot drive the owner's machines (XTM One Desktop runners), whatever the owner may do. See [Sharing and access](sharing-and-access.md#through-what) for how this fits with the agent's other audiences.

## What a partner can do

Once connected, the partner can:

- send a task and read its status and answer;
- cancel a task that is still running, including the work the agent passed on;
- register a callback address to be told when a task changes, instead of asking repeatedly;
- send a follow-up in the same conversation.

The agent accepts text and structured data. A message with a file is refused before any work starts. Answers are not streamed, and the partner cannot list its tasks: it keeps the ID of each task it created. Submissions are rate-limited per partner.

When the request is unclear, the agent can ask the calling agent a question. The task waits for the caller's answer, never for a person, and the agent asks at most three times. The run that asked reads `Waiting on caller` and shows the question; the caller's answer continues the task in a new run, and each of the two runs links to the other.

Work a partner submits shows on the integration's `Activity` tab — only that partner's own — and in the agent's activity with a badge naming the A2A provider that submitted it.

## Call a partner agent

1. Ask the partner for the card URL of each agent you may task, and for a credential.
2. Create the integration with the `Outbound: our agents task a partner` direction, paste the card URLs (separated by commas or new lines), and enter the credential:
    - `OAuth A2A`: the partner's token endpoint, the client ID and the client secret it issued you. From another XTM One deployment, these come from its inbound `OAuth A2A` integration.
    - `Foundry A2A`: your own Entra tenant ID, application (client) ID and client secret.
    - `Google Agent Platform A2A`: impersonation of a service account the partner names, or a service account key JSON.
3. Save. XTM One fetches and checks each card, and the `Partner verification` card shows what each partner agent declares and whether it is reachable.
4. Bind the integration to each agent that should delegate: `Configuration` > `Tooling` > `Integrations`.

Each partner agent that passes verification becomes one delegation tool for the bound agents, named after the agent it reaches. The integration also gives them a tool to stop a task still running at the partner, which the agent uses only when you ask. The `What this brings your agents` card lists these tools.

A delegation sends only the message the agent writes for it — not your conversation, nor any credential of yours. The partner's answer comes back as a quote from an outside party, not as instructions. When the partner runs XTM One, the agents your credential reaches appear on their own, without anyone typing their URLs.

To tell the partner which instance is calling, set `Send this instance's identity` to `On` on the outbound integration. Every message it sends then carries, in its metadata under `xtmOneInstance`, this platform's Instance ID (`Settings` > `Parameters`) and its name; nothing else is added. It is `Off` by default, and each integration is set on its own.

!!! warning "Card URLs must be HTTPS"
    A card URL that is not HTTPS, or that points at a private or loopback address, is refused.

## Check a partner's health

The `Partner verification` card on an outbound integration shows how many partner agents are reachable (`2 of 3 reachable`, for example), with a panel per partner agent: its verdict, what it `Accepts` and `Produces`, whether its card is signed, and when it was `Last verified`. Use `Verify now` on one agent or `Verify all`.

When the partner changes its card in a way that matters, delegation to that agent pauses until you review the change and choose `Accept card change`. The `Work sent to this partner` card, shown to the people who can manage the integration, lists each delegation and how it ended.

## Revoke access

- **Stop exposing one agent.** Unbind the integration from the agent (`Configuration` > `Tooling` > `Integrations` > `Edit`). Its card URL stops working at once. Binding it again creates a new card URL, which you must send to the partner.
- **Cut off an `OAuth A2A` partner.** Choose `Regenerate secret` in the `Partner credential` card. The old secret stops working at once on every agent the integration is bound to.
- **Change who may call a `Foundry A2A` or `Google Agent Platform A2A` integration.** Edit its values: the next partner request is checked against them.

XTM One also revokes an exposure on its own when the agent's owner changes, when the agent moves between personal and company-managed, when its sharing changes, or when it is disabled or deleted. When the agent is still enabled and bound, it gets a new card URL to send to the partner. Deactivating the user an exposure runs as pauses it; reactivating the account restores it with the same card URL.

## Next step

See [Sharing and access](sharing-and-access.md) for everyone else an agent reaches, and [Integrations](integrations.md) for the integration screens.
