# AI catalog

The AI Catalog is where you browse published AI resources and add them to your account.

It helps you start faster by reusing ready-made content instead of building everything from scratch.

## What you can find here

The catalog is organized into these sections:

- Agents
- Skills
- Prompts
- MCP Servers
- Tools, including custom tools

You can browse all resources together or open one category at a time.

## Search

Use search to find a resource by name or description.

This is useful when the catalog grows and you want to quickly narrow down what you need.

## Resource cards

Each card shows a short summary of the resource.

You can usually see:

- the resource name
- a short description
- tags
- who shared it
- basic counts or status information

## Add to my account

Open a resource to see its details, then add it to your account.

The detail page lets you:

- review the content before adding it
- copy the resource URL when available
- download the JSON definition
- add the resource to your account

For agents, you may also see an install step that asks for a name and any setup choices that are needed before the resource is added.

## What happens when you add something

What gets copied depends on the resource type.

- Agents are added to your account as usable agents
- If an agent includes assignments, they are installed disabled by default
- Skills are copied into your account automatically
- Prompts are copied into your account automatically
- MCP Servers are copied, but you still need to configure any secrets or environment values they require, including a credential passed as a command argument or in the server URL: the catalog shows, downloads and copies it masked
- Custom Tools are copied into your account and may need follow-up setup

Company-managed resources you have access to are bound as they are, without extra binding. When one is restricted to a group you are not in, a knowledge base is recreated empty and an integration asks you to pick your own, while an MCP server, custom tool, skill or prompt is left out: it is not listed on the catalog page, installed or included in the download for you. Others need you to connect an existing integration or complete a setup step after installation.

When you publish an agent, you decide whether its downloadable JSON includes the code of your personal custom tools, the ones that are not company-managed: the `Include the code of my personal custom tools in the catalog download` switch sits under `Include in AI Catalog` on the agent's configuration. It is off for a new publish, and agents published before the switch existed keep including them until you change it. When it is off, the download leaves those tools out and says which, both in the file and on the catalog page. Company-managed custom tools are always included, except for a reader outside a restricted tool's group, and adding the agent to an account never copies a personal custom tool either way. An assistant with the agent builder can set it for you too, with the same rules.

For an agent, an integration you pick during installation must be one you can use. The publisher's own integrations, channel bots, sharing and run-as settings never come with the agent, and its run budget is lowered to the limits that apply to you. The variables named in its persona, guardrails, assignments and copied prompts and skills are copied into your account as well, a secret one without its value. The end of the installation lists the variables that still need a value, and the agent's page shows a notice until you set them in `Variables`.

Nothing is left out silently. The end of the installation also lists, per kind and with the reason, what your copy does not carry or carries only in part: an integration, MCP server, custom tool, skill or prompt that is not shared with you, an MCP server copied as yours without any credential, a knowledge base copied empty, the handovers and channel settings a catalog copy never carries, and an assignment limited to an MCP server the agent does not have, which runs without it. The same list is the `warnings` field of the install response (`POST /api/v1/public/catalog/{type}/{id}/add`).

## Agents

Agent detail pages show what the agent does and what it needs.

You can review:

- the agent persona
- assigned integrations
- knowledge bases
- MCP servers
- custom tools
- skills
- prompts
- built-in tools
- variables

If the agent includes assignments, you can also see how much work it is designed to automate.

## Skills

Skill detail pages show the skill URL and its content.

Use this when you want to inspect the instructions before importing them into your account.

## Prompts

Prompt detail pages show the full prompt content.

This helps you review reusable text before adding it to your own workspace.

## MCP Servers

MCP Server detail pages show:

- the transport type
- the command or URL
- arguments, when present
- the tools exposed by the server

These entries help you understand which external tool server the resource connects to.

## Custom Tools

Custom Tool detail pages show:

- the code size
- the input parameters
- the JavaScript function
- the timeout

This helps you understand what the tool does before you add it.

## What to remember

The AI Catalog is a starting point, not the final setup.

After adding a resource, you may still need to:

- connect an integration
- add documents to a knowledge base
- configure secrets or environment values
- review the imported setup before using it with users

## Next step

Continue with the [`Common experience across the XTM Suite`](xtm-suite/common-experience.md) page, then move to the product-specific pages that match your environment.
