# Getting started

This chapter introduces what XTM One does, who it is for, and how to find your way around.

## What XTM One is for

XTM One is the core platform of the XTM Platform. OpenCTI (threat intelligence), OpenAEV (adversarial exposure validation) and OpenCRQ (governance, risk and compliance) are its modules: they connect to XTM One, sign their users in through it, and reach its agents from their own screens. XTM One holds what they share: the common context of your organization, your exposure management view, the shared integrations, and the AI agents that work across all of them.

Use XTM One to work with AI assistants built for cyber, intelligence, operations and business workflows. You can ask for summaries, explanations, extractions and recommendations; follow your exposure across the connected modules; launch or monitor automated work handled by agents and assignments; reuse shared resources such as knowledge bases, prompts, skills, tools and integrations; and review what your agents did before acting.

## Who it is for

XTM One is designed for business users, analysts and operators: you do not need to be an AI specialist. It is useful for teams working with OpenCTI, OpenAEV and OpenCRQ, threat intelligence, investigations, reporting and summarization, operational triage, security operations, and email or messaging workflows.

## Key words in this guide

A few recurring terms appear throughout this documentation:

- `Agent`: an AI assistant configured for a specific use case, role, or workflow
- `Assignment`: an automation rule that tells an agent when to run and what to do
- `Knowledge Base`: searchable internal knowledge used to ground answers and actions
- `Prompt` and `Skill`: reusable instructions that shape how agents behave
- `Tool` or `MCP Server`: capabilities that let agents query, fetch, or act outside the chat itself
- `Integration`: a connection to another system such as messaging, email, or external platforms

## Sign in

Open the XTM One sign-in page and choose the login method available in your organization: a company account, email and password, or a local account if that is allowed. If your organization uses single sign-on, you are redirected to your identity provider and brought back automatically.

## Find your way around

After signing in you land on the [Dashboard](home-dashboard.md). The left navigation moves you between sections, the top bar holds search, notifications and your user menu, and the center shows the current page.

The left navigation reads from top to bottom:

- `Home` and `Chat`: your overview and your conversations with assistants.
- `Orchestration`: your agents and what they form and follow. It holds `Agents`, `Fleets`, `Agentic Flow` (the map of how agents work together) and, for administrators, `Intents`.
- `Work`: what your agents produce. It holds `Operations` (their runs, background tasks, work items, shared state, worksets and workspaces), `Value`, `Presentations`, `Videos`, `Dashboards` and `Files`, the last ones where your deployment offers them.
- `Agent Studio`: what your agents are made of. It holds `Knowledge`, `Prompts`, `Templates`, `Skills` and `Investigation packs`. `Create an agent` is a button of the hub and of the `Agents` page, and a destination of the command palette.
- `Tools & Integrations`: what your agents act through. It holds `MCP Servers`, `Tools`, `Integrations` and `Connections` (`Browser profiles`, plus `Variables` and `Relays` where your deployment offers them).
- `Administration` (administrators): `Observability` (`Model usage`, `Outcomes`, `Traces` and `Sessions`), `AI Adoption`, `Evaluations`, `Platform Health`, `Users and groups`, `Objects`, `Audit logs` and `Settings`. A group manager sees `Model usage` for the members of their groups.

`Orchestration`, `Work`, `Agent Studio` and `Tools & Integrations` are groups: click a group's name to open its hub page, which presents each of its pages as a card, and the arrow next to it to fold or unfold the group. A row that gathers several pages (`Observability`, `Users and groups`, `Connections`) opens the first of them, with one row of tabs to the others. The navigation can also shrink to a rail of icons, with the toggle at its foot.

The product switcher at the top of the navigation (`Switch product`) lists the OpenCTI, OpenAEV and OpenCRQ platforms connected to XTM One and the XTM Hub, each with its connection status, and opens the one you pick.

## What you can access

What you see depends on your role and on your organization's edition. Some users only use the shared platform experience, some use ready-made assistants, and some create and manage their own agents and resources. The `Administration` section is for administrators.

An area that needs AI stays listed when AI is not available, marked with a lock: on Community Edition (see [Editions and licensing](../administration/editions-and-licensing.md)), or when an administrator has switched AI off (see [Running without AI](../administration/running-without-ai.md)). Point at it to see why. You can still open it and read what it holds.

If a section is missing altogether, it is not enabled for your deployment or your account. This guide explains those differences only when they matter for a task.

## Search with the command palette

The search box at the left of the top bar (`Search…`) opens the command palette. Press ⌘K on a Mac, or Ctrl K elsewhere, to open it from anywhere, even while you type in a field.

- Type the name of a page to go to it: every page of the navigation is listed.
- Type `>` to run a command, such as `Play Space Invaders`.
- Type `@` to jump to a section of the page you are on.
- Type `?` for help on these modes.

## Notifications

The bell in the top bar shows how many notifications you have not read. It collects what your agents and the platform want you to know:

- runs, flows and work items waiting for your input or decision, listed first and marked `Action needed` until you answer them;
- the results of assignments set to notify you when they complete;
- flow notifications and questions, reminders and messages from agents;
- problems that need you: an integration that can no longer sign in, a resource an assignment keeps missing, a chained run that could not start, security and API key notices.

Click a notification to open the page it is about; opening it marks it read. Point at one and use `Mark as read` to mark it read without opening it, or `Mark all read` for all of them. A notification waiting for your answer stays unread until you answer it: opening it does not read it, it has no `Mark as read`, and `Mark all read` leaves it unread; while one waits, the bell's count turns red. A work item you cannot open is never listed. `View all` opens `Operations > Executions` on its `Notifications` filter. What you mark read is kept on your account, so your other devices agree.

## The top bar

Besides search and notifications, the top bar holds:

- `Quick triggers`, when you have assignments you can start by hand: start one from there with `Run now`;
- the [AI Catalog](ai-catalog.md) button, when your administrator has enabled the catalog: it opens the catalog of published agents and resources in a new tab;
- your user menu: `My Profile`, `Get XTM One Desktop` (or `My machines` once the desktop app is installed), the arcade, `Settings` for administrators, the `About` window, `Emergency stop` and `Sign out`.

When your deployment offers it, `Get XTM One Desktop` opens the page to download XTM One Desktop, the desktop app through which an agent can act on your computer. See [XTM One Desktop and runners](resources/runners.md).

## XTM One in the other modules

Depending on your deployment, you also use XTM One from inside OpenCTI, OpenAEV or OpenCRQ. There it appears as an assistant panel or an AI action inside the product where you already work, while the agents, resources and automations are still managed in XTM One. See [XTM One in the XTM Suite](xtm-suite/index.md).

## Start with the dashboard

If you are not sure where to begin, open the Dashboard first. It is the best place to see what waits for you, check your exposure and recent activity, and find the next action to take.

## Next step

Once you are signed in and comfortable with the layout, move to the [`Foundations`](foundations/index.md) section. Start with [`Foundations Overview`](foundations/index.md), then continue to [`Data model`](foundations/data-model.md) before moving into the Dashboard and the rest of the UI chapters.
