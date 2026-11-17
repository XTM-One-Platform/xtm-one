# Create an agent

You create an agent in three ways: with a guided wizard, with a single form, or by importing a file. All three start from the same page, and all three give you an agent you can keep changing afterwards on its detail page (see [Agents](agents.md)).

## Before you start

Creating agents needs Enterprise Edition and AI switched on for the platform (see [Editions and licensing](../administration/editions-and-licensing.md)). When either is missing, the creation buttons say so instead of opening the page.

An agent you create is yours: it is private until you share it (see [Sharing and access](resources/sharing-and-access.md)). Any integration, knowledge base, tool or skill you attach must be one you can already see. To use a service that is not connected yet, connect it first under `Integrations` (see [Integrations](resources/integrations.md)).

## Open the creation page

Two entry points lead to the same `Create New Agent` page:

- `New Agent` at the top of the `Agents` list.
- `Create an agent`, the featured card of the `Agent Studio` hub, also listed first under `Agent Studio` in the sidebar. The hub gathers what agents are made of: `Knowledge`, `Prompts`, `Templates` and `Skills`.

The page offers three cards:

| Card | What it does |
| --- | --- |
| `Wizard` | Builds the agent step by step, with instructions and a tutorial at every step. The easiest way to start. |
| `Advanced` | Picks a template or starts from scratch, then shows one form for the name, persona and required integrations. You set up the rest on the agent's page. |
| `Import` | Creates the agent from an XTM One native bundle or a Microsoft AgentSchema document, in JSON or YAML. |

## Pick a starting point

`Wizard` and `Advanced` both open on the template gallery. The first card, `From Scratch`, gives you an empty agent. Every other card is an agent template: a ready-made role with its persona and, often, pre-configured assignments; the card shows how many. Search the gallery with `Search templates...` or narrow it with the category pills (`All` shows everything).

What each template category holds, and what a template brings with it, is described in [What an assignment or a roster may name](agents.md#what-an-assignment-or-a-roster-may-name).

## Build it with the wizard

The wizard shows a live preview of the agent on one side and the current step on the other. The preview follows what you fill in and highlights the part the step is about. `Tutorial` opens a short explanation of the step, and a step counter tells you where you are.

The wizard walks you through these steps in order:

1. `Identity`: the `Agent Name` (required), an icon and an optional one-line description. The name and icon are how the agent appears everywhere: the agents list, the chat picker, run traces and audit entries.
2. `Persona`: the agent's standing instructions (required), written as "You are...". `Generate with AI` drafts one from a short description, `Improve with AI` refines what you wrote, and `Prompt library` lets you start from one of your [prompts](resources/prompts.md).
3. `Tools`: the built-in and custom tools the agent can call. If you pick nothing, the agent starts with the platform's default tool set.
4. `Integrations`: the connected services the agent acts through. A template can require one service type, marked `required`: you bind a matching connection before you can go on. `Connect one` takes you to the integration form, and your draft waits for you (see [Your draft is kept](#your-draft-is-kept)).
5. `Knowledge bases`: the collections the agent searches when it answers. Skip this step if the agent does not need them, unless the template requires one.
6. `Review & create`: `Create the agent` creates it. If a required step is incomplete, the page lists it and the button stays off. With a template, the page says how many pre-configured assignments come with it.

Once you reach the review, the wizard becomes a hub: you can open any step again, and six `Advanced options` appear below the create button. Each is optional and returns to the hub when you are done:

- `Model`: pin the model the agent runs on, or keep `Platform default`, which follows your administrator's choice.
- `Memory`: the four memory tiers (`Short-term Memory`, `Medium-term Memory`, `Persistent Memory`, `User Memory`), all on by default. Turn them off for an agent that must not keep anything between runs.
- `Guardrails`: hard rules the agent must never break, on top of the persona.
- `Skills`: packaged instructions the agent loads when a task calls for them (see [Skills](resources/skills.md)). A template's default skills that you can use are already selected; remove the ones you do not want.
- `Handovers`: the agents this one may hand a conversation over to (see [Agentic flow](agentic-flow.md)).
- `Budget`: caps on what one run may spend. Empty fields inherit the platform limits (see [Run budgets](agent-budgets.md)).

## Your draft is kept

Nothing is saved on the platform until you create the agent, but the wizard keeps your draft in the browser tab while you work. If you leave the wizard, for example through `Connect one` or the sidebar, and come back in the same tab, the draft is restored and a message says `Your unsaved draft was restored.` `Discard draft` starts over.

The draft belongs to your account and to that tab only: closing the tab or signing out forgets it, and nobody else signing in on the same browser sees it. A tool, knowledge base, skill, agent or model that is no longer available to you when the draft comes back is marked `Unavailable`, so you can remove it before you create the agent.

## Use the Advanced form

`Advanced` opens the same template gallery, then one form:

- `Agent Name`, `Description (optional)` and `Tags`.
- `Persona / System Prompt`, prefilled from the template when you picked one.
- `Required Integrations`, when the template asks for some: pick the connected integration to use for each one, or skip and set it up later.
- `Company-managed agent`, for administrators only: the agent is shared with all users, and only administrators can edit it. Before creating it, the platform shows what the agent lends to every user and asks you to confirm.

`Create Agent` creates it. Everything else (tools, knowledge bases, model, memory, assignments) is set up afterwards on the agent's `Configuration` tab.

## Import an agent from a file

`Import`, on the creation page or at the top of the `Agents` list, opens `Import an agent`. Drop a `.json`, `.yaml` or `.yml` file, or click to browse. The platform checks the file before anything is created and shows each check: `Reading the file`, `Detecting the format`, `Validating against the schema`, `Resolving references`.

- `This file is ready to import.` means you can confirm with `Import agent`.
- `This platform cannot satisfy everything the file references:` lists what will be left out, such as an integration or a knowledge base this platform does not have. `Suggest fixes with AI` explains how to finish the setup.
- `Corrections you can apply before importing:` offers to remove a tool that arrives without its code or an MCP server that arrives as an empty placeholder.
- `This file cannot be imported.` names what failed. Nothing is created.

What each format carries, and what an import never brings across, is described in [Importing an agent](agents.md#importing-an-agent).

## What you get

After `Create the agent`, `Create Agent` or `Import agent`, the agent's detail page opens. The new agent is enabled.

- From a template, its pre-configured assignments are created with the agent and switched on. Review them in the agent's `Assignments` card before you rely on them (see [Assignments and triggers](assignments.md)).
- From an imported file, the assignments start switched off, so nothing runs before you have reviewed it.

You can also have an assistant build an agent for you in a conversation (see [Building agents from a conversation](agents.md#building-agents-from-a-conversation)).

## Next step

Give your agent work to do on its own with [Assignments and triggers](assignments.md).
