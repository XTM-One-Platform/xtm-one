# XTM One in OpenAEV

In OpenAEV, XTM One supports adversarial exposure validation work without requiring you to leave the OpenAEV experience. As in OpenCTI, the capabilities appear inside the product, attached to the scenario, inject, or exercise you are working on.

## Access points inside OpenAEV

Each access point is powered by a named capability — an intent — answered by an agent your administrators chose. These are the agents that handle them out of the box.

| Where it applies | What you can do | Capability | Default agent |
| --- | --- | --- | --- |
| The in-product AI assistant | Hold a conversation about the current OpenAEV context — scenarios, simulations, results | `global.assistant` | `CTEM Assistant`, which hands the conversation over to the `OpenAEV Assistant` |
| Text and description fields | Summarize, explain, shorten, expand, fix spelling and grammar, change the tone of the content you are writing | `global.summarize`, `global.explain`, `global.make_it_shorter`, `global.make_it_longer`, `global.fix_spelling`, `global.change_tone` | `OpenAEV Assistant` |
| Exercise content | Generate the messages used in an adversary emulation exercise | `aev.message_generator` | `OpenAEV Assistant` |
| Exercise content | Generate a media article for an adversary emulation exercise | `aev.media_article_generator` | `OpenAEV Assistant` |
| Phishing content | Generate a phishing lure email (subject, HTML body, and plain-text fallback) for an authorized phishing simulation | `aev.phishing_email_html_generator` | `AEV Phishing Email Generator` |
| Phishing content | Generate a phishing landing page (HTML and CSS) for an authorized credential-capture simulation | `aev.phishing_landing_page_html_generator` | `AEV Phishing Landing Page Generator` |
| Detection and remediation | Turn a malware behavior description into detection rules, with the processes, commands, files, and ATT&CK techniques it relies on | `aev.detection_rules_generator` | `AEV Detection Rules Generator` |
| Document analysis | Extract tactics, techniques, and procedures from a document | `cti.ttp_harvester` | `CTI TTP Harvester (Filigran AI)` |

The exact wording of buttons and menu entries depends on your OpenAEV version, and an administrator can bind a different agent to any of these capabilities.

Beyond these contextual actions, the `OpenAEV Assistant` and the scenario and payload agents can also be used from XTM One itself — to design a scenario, propose injects, or review the outcome of an exercise, with the results written back into OpenAEV.

The `OpenAEV Assistant` also turns your results into a deliverable: a validation readout as a report or a presentation for a review meeting, in your report and deck templates. See [`Presentations`](../presentations.md).

## Autonomous attack path

On OpenAEV Enterprise Edition, a chained scenario can be launched as an autonomous attack path: you give OpenAEV an objective and a scope, and XTM One drives the run. The `OpenAEV Attack Path Orchestrator` (capability `aev.attack_path_orchestrator`) plans the attack path, launches techniques in OpenAEV, reads the findings as they arrive, records each step on the run's timeline, and adapts the next step to what it found.

- **You stay in control from OpenAEV.** The orchestrator follows the directions you send during the run, asks you a question when it needs a decision, and stops when you cancel the run.
- **Slow results cost nothing while they wait.** A step whose result takes hours, such as a phishing email waiting for a click, parks the run until the finding arrives or a deadline passes: no model call runs in between.
- **Plan without running.** A run can design the attack path only, never executing a technique, so you can review it before a live run follows it.
- **Specialists.** The orchestrator consults the `OpenAEV Payload Creator` (capability `aev.attack_path_additional_agent`) for payloads, code and exploitation support, plus any other agent selected for the run in OpenAEV.

XTM One accepts an autonomous run only with Enterprise features: a connected Enterprise Edition platform or a Filigran license (see [`Editions and licensing`](../../administration/editions-and-licensing.md)). An administrator's [emergency stop](../../administration/emergency-stop.md) on the whole platform also cancels autonomous runs.

## React to OpenAEV events

An assignment can run its agent when something happens in OpenAEV. Give the assignment an `Integration event` trigger on the OpenAEV integration and pick one or more events:

| Event | When it fires |
| --- | --- |
| `Simulation finished` | A simulation reaches the finished status |
| `Inject or atomic test finished` | An inject or atomic test ends executed, partial or in error |
| `Finding created` | A new finding is created |

XTM One sets up OpenAEV to call it: under the integration's own OpenAEV account, it creates a webhook notifier and one notification trigger per event, named `XTM One - <fleet> - <purpose>`. It checks them every 15 minutes, recreates one that was deleted in OpenAEV, restores one that was edited, and removes them when the assignment stops asking, is disabled, or is deleted.

- An administrator first sets the `webhook_secret` field on the OpenAEV integration in XTM One. OpenAEV sends it with every event; without it, nothing is set up and the assignment never receives an event.
- The assignment's trigger must ask for this setup in its configuration. The assignment page does not offer it yet: an assignment created through the API or by an agent can.

## What to expect when you use it

When you trigger an XTM One action from OpenAEV:

- the current OpenAEV context is reused, so you do not have to restate it
- XTM One routes the request to the agent bound to that capability for the `aev` business context
- the result comes back as part of the OpenAEV experience

Longer-running work can continue in the background; those runs appear in [`Operations`](../work-history.md) in XTM One.

## Why a feature may be unavailable

An action can be hidden or disabled if:

- the OpenAEV instance is not registered with XTM One
- no agent is bound to that capability, or the bound agent is disabled
- the bound agent is private rather than company-managed or shared with a group
- the binding targets another business context than `aev`
- your OpenAEV permissions do not allow the related operation

Administrators can review and fix the routing on the [`Intents`](../../administration/intents.md) page.

## Permissions and review

As with other suite products, you should expect:

- the action to respect your current OpenAEV permissions
- results to be treated as operational assistance, not final truth by default
- some sensitive workflows to include a review or validation step

## Good practices

- Start the action from the scenario or exercise that already contains the relevant context.
- Review generated proposals before applying them to an operational workflow.
- Ask your administrator if the expected action is missing or unavailable.

## OpenAEV MCP server

Connecting OpenAEV to XTM One also activates a native **MCP server** for the platform.

Any MCP-compatible client (Cursor, Claude Desktop, custom agents) can connect to `https://<your-xtm-one>/mcp/openaev` with a personal XTM One API key and search, read, and manage scenarios, simulations, payloads, injects, atomic tests, and findings — with your own OpenAEV permissions.

Find your endpoint and a ready-to-copy client configuration in `My Profile > MCP Endpoint`, and see [`Platform MCP servers`](../resources/platform-mcp-servers.md) for the full guide.

## Next step

Return to the main XTM One guide for the broader platform chapters, starting with [Home Dashboard](../home-dashboard.md) when needed. Administrators who need to change which agent answers a capability should read [`Intents`](../../administration/intents.md).
