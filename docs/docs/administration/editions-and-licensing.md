# Editions and licensing

XTM One ships as one platform with two editions. Which one you are on decides a single thing: whether AI is licensed, and therefore available. Whether it actually runs is a separate, administrator-held switch (see [Running Enterprise Edition without AI](#running-enterprise-edition-without-ai)).

## The two editions

| Edition | When | What you get |
|---|---|---|
| **Community Edition** | No Filigran license is installed and no connected OpenCTI, OpenAEV or OpenCRQ platform is in Enterprise Edition | The platform runs with AI off. Identity, settings, platform connections, integrations and the exposure dashboards keep working. Agents, chat and automations are listed and reachable, marked unavailable. |
| **Enterprise Edition** | A Filigran license is installed, **or** at least one connected platform is in Enterprise Edition | Everything is on as long as AI is switched on: agentic chat across your platforms, autonomous agents and assignments, flows, knowledge bases, skills, prompts, MCP servers, custom tools, integrations, [Agent-to-Agent interoperability](../usage/resources/agent-to-agent.md), the full integration catalog and AI adoption analytics. |

There is nothing in between. A connected Enterprise Edition platform activates every capability of XTM One, exactly as a license does.

!!! note "Nothing to redeploy"
    Every deployment already contains the whole platform. Moving to Enterprise Edition never means installing something new: it is a license key, or a platform connection, away.

## What a license decides

A Filigran license carries two things and nothing else:

- **Agentic capacity** - the number of agentic executions allowed per day, per month and per year. One agentic execution is one complete request handled by the platform - a chat turn, an assignment run, or a background task - however many agents and tools it involves. Requests with no account behind them count too: anonymous channel users, visitors of a public chat widget and calls made with the platform MCP key (see [Requests with no account](settings.md#requests-with-no-account)).
- **Module editions** - which connected OpenCTI, OpenAEV or OpenCRQ platforms are in Enterprise Edition.

A license never switches individual features on or off. Every capability of XTM One is available to every Enterprise Edition deployment.

## The included allowance

When Enterprise Edition comes from a connected platform and no Filigran license is installed, the platform runs under an included agentic allowance: 30 executions per day, 100 per month and 300 per year, shared across the deployment. It is sized to discover, test and demonstrate value.

Installing a Filigran license replaces the allowance with the capacity the license carries. `Settings` > `Quotas` shows which ceilings apply and where they come from, and lets you set per-user defaults and group or user overrides within them.

A license period with no quota has no ceiling, whether the certificate carries no quota for it or a quota of `0`, and whatever the license's start date. Licenses issued from 27 September 2026 write an unlimited period as no quota.

## Activating Enterprise Edition

You have two ways to activate it, and both work without a redeploy:

- **Connect an Enterprise Edition platform.** Register an OpenCTI, OpenAEV or OpenCRQ platform that holds its own Enterprise license. XTM One inherits Enterprise Edition as soon as the registration validates.
- **Install a Filigran license.** Paste the license PEM in `Settings` > `Parameters`, or set the `ENTERPRISE_LICENSE` environment variable and restart. The instance ID shown on the same page is required to generate the certificate. Set the variable on the platform and on the worker; a worker without it still runs the background AI of the licensed deployment, and says so once in its log.

If you have neither, `Start free trial` and `Contact sales` are offered wherever an AI capability is locked - on the home page, in the navigation, on the creation buttons and in the AI settings.

## What Community Edition looks like

On Community Edition the platform tells you once, clearly, and stays out of the way:

- The home page leads with the upgrade path and the platform connection instructions.
- AI entries in the navigation carry a lock; hovering one explains why.
- A creation button opens the same explanation with the three ways forward.
- The AI Models settings say that a provider configured now is used as soon as Enterprise Edition is activated, instead of warning that the platform is blocked until one is configured.

You can prepare LLM providers, users, groups and platform connections ahead of activation. Nothing you configure is lost when the edition changes.

## When Enterprise Edition ends

A deployment returns to Community Edition when its last source of Enterprise Edition goes: the connected Enterprise Edition platform loses its license or is disconnected, or the Filigran license is removed. Within a few seconds the automations configured while it was Enterprise stop running AI too, exactly like the interactive surfaces:

- Scheduled assignments, integration events, webhooks and channel bots start no AI work. A person writing to a bot, where the bot would answer (a direct message, a mention, a channel it listens to), is told that Enterprise Edition is required and how to activate it; a webhook sender receives a `403` carrying the same explanation.
- A flow containing an AI step is held back and recorded as skipped with the reason. Flows without AI steps keep running.
- Work that was scheduled but had not started does not start: it is held. An assignment run waiting on its configured delay, a run resumed from a human-review pause (the answer is kept), a background task that had not begun and the pending runs of an evaluation are shown as **Held** and run by themselves within about five minutes of Enterprise Edition becoming active again; a conversation waiting on a held background task hears back when the task finally runs. Work held longer than `AI_UNAVAILABLE_HOLD_MAX_SECONDS` (one week by default, `0` to fail it at its start instead) is recorded as failed with the cause `AI not licensed`. Deliveries waiting in an assignment's queue stay queued and documents waiting to be indexed stay pending; both are picked up once Enterprise Edition is active again.
- Nothing is charged to the agentic quota, and nothing is disabled, paused or deleted. Schedules, integration events, webhooks, channel bots and flows resume on their own once Enterprise Edition is active again; a scheduled assignment or flow catches up with one run, not one per occurrence it missed.

Work already running when the edition changes finishes under its own time limit. If the platform briefly cannot read its license state when scheduled work comes due, for example during a database failover, that work is not failed: it is retried shortly afterwards. A channel bot written to meanwhile answers a direct message that the AI service is temporarily unavailable, and stays silent in a group channel.

## Running Enterprise Edition without AI

Enterprise Edition and the AI switch are independent. An administrator can switch AI off on a fully licensed platform; see [Running without AI](running-without-ai.md).

## Next step

Review the [Settings](settings.md) page for where the license, the quotas and the AI switch live.
