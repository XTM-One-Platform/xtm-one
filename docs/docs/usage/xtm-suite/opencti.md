# XTM One in OpenCTI

In OpenCTI, XTM One is the AI layer on top of the threat intelligence workflow you already use. You do not open XTM One to benefit from it: the capabilities appear inside OpenCTI, on the objects you are working on.

This page lists the concrete places where the two products meet, so you know where and when you can use XTM One.

## Access points inside OpenCTI

Each access point is powered by a named capability — an intent — and each intent is answered by an agent that your administrators chose. Out of the box, the agents below handle them.

| Where you find it | What you can do | Capability | Default agent |
| --- | --- | --- | --- |
| The in-product AI assistant | Hold a conversation with the current OpenCTI context already loaded | `global.assistant` | `CTEM Assistant`, which hands the conversation over to the `OpenCTI Assistant` |
| Text and description fields | Summarize, explain, shorten, expand, fix spelling and grammar, change the tone of the content you are writing | `global.summarize`, `global.explain`, `global.make_it_shorter`, `global.make_it_longer`, `global.fix_spelling`, `global.change_tone` | `OpenCTI Assistant` |
| Search and filters | Describe what you are looking for in plain language and get an OpenCTI filter in return | `cti.nlq_search` | `OpenCTI NLQ` |
| Containers — reports, groupings, cases | Get a structured summary of the container, including the content of its attached files | `cti.container_summary` | `OpenCTI Entity Insights` |
| Containers — `Ask AI` report generation | Write an HTML report from the container's knowledge, with the requested length, tone and language | `cti.container_report` | `OpenCTI Entity Insights` |
| Entity pages — threat actors, intrusion sets, malware, organizations, sectors, and others | Get a digest of the containers that mention the entity, an activity trend analysis, a forecast of future activity, or a summary of the entity's own modification history | `cti.containers_digest`, `cti.entity_activity`, `cti.entity_forecast`, `cti.entity_history` | `OpenCTI Entity Insights` |
| Automation playbooks | Extract intelligence from documents into a STIX bundle, transform a bundle, or act on a bundle as the last step of a playbook | `cti.stix_harvester`, `cti.stix_transformer`, `cti.stix_consumer` | `CTI STIX Harvester (Filigran AI)` — or `CTI STIX Harvester` — then `CTI STIX Transformer` and `CTI STIX Consumer` |

The exact wording of buttons and menu entries depends on your OpenCTI version, and an administrator can bind a different agent to any of these capabilities. What each access point *does* is stable; who answers it is a configuration choice.

!!! tip "Not the same as the MCP server"
    The access points above are AI actions inside OpenCTI. Connecting OpenCTI to XTM One also exposes OpenCTI's own tools to external MCP clients, which is a separate capability — see [`OpenCTI MCP server`](#opencti-mcp-server) below.

## Using XTM One from a playbook

Playbooks are the access point that matters most for automation, because the agent runs without anyone watching. Three distinct roles are available, and they differ by what the step receives and what it must return:

- A **harvester** receives a document — a PDF, an office file, a text report — and returns a STIX 2.1 bundle. Use it to turn incoming reports into structured knowledge. The built-in `CTI STIX Extractor (Grounded)` agent is another harvester: it extracts in several checked steps (entities, then their kinds and names, then the relationships between them), keeps only what it finds back in the text, and records for each object where it was read and for each relationship the sentence that states it. An address, a domain or a file hash the report ties to a threat becomes an indicator of that threat, every relationship is one OpenCTI accepts, and the links a report only cites are kept as its references, not as indicators. It is not bound to `cti.stix_harvester` by default; an administrator can bind it.
- A **transformer** receives a STIX 2.1 bundle and returns a STIX 2.1 bundle. Use it to enrich, filter, rewrite, or normalize what is flowing through the playbook.
- A **consumer** receives a STIX 2.1 bundle and takes a final action instead of returning a bundle — summarize it, alert on it, post it somewhere. It is a terminal step.

Because the playbook step expects a specific shape of answer, an agent bound to one of these capabilities has to respect it. If your administrators bind a custom agent to `cti.stix_harvester` or `cti.stix_transformer`, that agent must return a valid STIX 2.1 bundle; a consumer is free to return a readable report.

The built-in `CTI STIX Transformer` does not rewrite the bundle it receives: it describes only the changes your instruction asks for, and XTM One applies them to the incoming bundle. Every value the instruction does not touch (a report's name, a long description) leaves the step exactly as it came in, and a large bundle costs no more to transform than a small one. When the validation finds a value of an incoming object that changed, it reports it, so a change nobody asked for does not pass unnoticed. A custom transformer agent gets the same behaviour with the `output_mode` setting `patch` in its configuration.

In that mode the agent's tools receive the incoming bundle as `$input`, and any part of it by path: `$input.objects[3].description` (a key, then a position in a list), `$input.objects[0].extensions["extension-definition--UUID"].content` (a key holding a dash, dot, colon or space, in quoted brackets). A path that does not match the bundle stops the tool call with a message naming the part that did not match, so a placeholder never reaches OpenCTI as text.

An agent a playbook step calls also gets `$input` whenever the step's message carries a STIX bundle, even without that setting: the bundle the playbook sends with the instructions is the input. An agent that must never see its input this way sets `input_reference` to `false` in its configuration. A search text that happens to read `$input` is never taken for the placeholder.

A large input (over 100,000 characters) is not sent to the model: the platform replaces it in the message by a short summary (its size, its object count and the counts by type) and the agent works on it through `$input`, or reads it whole in the code interpreter at `attachments/input.json`. A bundle of several million characters therefore fits the model's context window and is not re-sent at every step.

Results and failures of playbook-triggered runs are visible in XTM One under [`Operations`](../work-history.md), which is where to look when a playbook step did not produce what you expected.

## Using OpenCTI from XTM One

The integration works in both directions. Once OpenCTI is registered, you can also start from XTM One:

- **Chat with an OpenCTI-aware agent.** The `OpenCTI Assistant` and the other threat intelligence agents query your OpenCTI instance while they answer.
- **Capitalise information manipulation the VIGINUM way.** The `FIMI Analyst` follows the SGDSN/VIGINUM doctrine for modelling foreign information manipulation in OpenCTI. Two built-in knowledge bases ground it: `VIGINUM FIMI Doctrine` (the OpenCTI doctrine, the information manipulation set concept and the DISARM framework in French and English) and `VIGINUM FIMI Publications` (VIGINUM's public reports, with every diagram, screenshot and chart described in text). Ask it, for example, "how should I model this Telegram channel and the narrative it pushes?" or "which TTPs did VIGINUM document for Storm-1516?". Its procedures are built-in skills it loads when the task comes up: `FIMI Report Capitalisation`, `FIMI IMS Assessment`, `FIMI Attribution Review`, `FIMI DISARM Mapping`, `FIMI Infrastructure Pivoting`, `FIMI Coordination Indicators`, `FIMI Threat Brief` and `FIMI Corpus Review`. With `FIMI Corpus Review`, it goes through many documents of one source, such as every case of a quarter, by storing them in its own corpus for the conversation and reading only what your question needs. It then tells you how much of the source it covered. For the technical pivots of a list of domains, URLs, accounts or IP addresses, it consults the `FIMI Infrastructure Investigator`, which reads OpenCTI and the web but never writes to OpenCTI and never attributes: it returns rated links, and the `FIMI Analyst` shows you what it would capitalise before writing.
- **Turn the intelligence into a deliverable.** The `OpenCTI Assistant`, the `FIMI Analyst` and the `CTEM Assistant` write a report or build a presentation from what they find, in your report and deck templates: ask for "a threat brief on this intrusion set" or "a five-slide deck on this week's ransomware activity". See [`Presentations`](../presentations.md).
- **Build your own agents on OpenCTI data.** The OpenCTI integration gives agents read and write tools for entities, observables, relationships, indicators, and containers. See [`Integrations`](../resources/integrations.md).
- **Choose where agents write.** By default an agent stages every knowledge change in an OpenCTI draft for a person to review. `Live writes` stays off by default, and while it is off agents see no tool that writes outside a draft and no mention of the setting: they list labels (`list_opencti_labels`), add labels to entities through the draft (`add_opencti_label`) and delete a draft they created in the same run (`delete_opencti_draft`). `Live writes` is set at two levels. On the integration, it is the default of every agent bound to it. An administrator turns it on in `Integrations`, on the OpenCTI integration's page, `Configuration` card: `Edit`, then `Live writes` set to `On`, then `Save changes` (on an integration the platform registered, it is one of the few fields left editable, and the next registration keeps it). Changes then go to the live graph with no review, and agents also get `apply_opencti_draft` (apply a draft), `create_opencti_label` (create a label like Settings > Labels, returning the existing label when the value is already there), `manage_opencti_label` (change a label's value or color, or delete it, the label named by its id or its value) and may delete any draft. `delete_opencti_entity` deletes STIX objects only: given a label's id, it answers that the id is a label and names `manage_opencti_label`. On an agent, each bound OpenCTI integration can override that default: on the agent's `Configuration` > `Tooling` tab, `Integrations` card, `Edit`, the bound OpenCTI integration shows a `Writes` control with `Inherit` (follows the integration, its current value named), `Live` (writes to the live graph even when the integration is off) or `Draft only` (always the draft, even when the integration is on), then `Save`. This lets some agents work autonomously while others only prepare drafts. `Inherit` and `Draft only` need only the right to configure the agent; `Live` needs an administrator or the integration's manager, and on a per-user OpenCTI binding an administrator. A read-only binding never writes live, whatever its override. The same override is the `integration_live_writes` field of `create_agent`, `update_agent` and `get_agent`. No tool takes a per-call option to write live.
- **Work with workflow statuses.** An agent reads the statuses of each entity type's workflow with their Status ids, the ids a playbook "change status" step and a status filter need (they differ from the status template ids). In a filter, the agent can also write a status name such as `PUBLISHED`: it is turned into the Status id of the entity types the filter targets, and a name no workflow has is refused with the list of valid names.
- **Let agents work on a schedule.** An agent can run on its own — for example to produce a recurring exposure briefing — and its runs appear in [`Operations`](../work-history.md).
- **Let agents react to OpenCTI.** An assignment with an `Integration event` trigger on OpenCTI runs its agent when an entity or relationship is created, updated or deleted. See [React to OpenCTI events](#react-to-opencti-events).
- **Connect an external MCP client** to work with OpenCTI knowledge from your own tooling.

## React to OpenCTI events

An assignment with an `Integration event` trigger on the OpenCTI integration runs on the events you pick: `Entity / relationship created`, `Entity / relationship updated`, or `Entity / relationship deleted`. OpenCTI delivers them through a webhook notifier and a live trigger, which you can create in OpenCTI yourself, pointing at the webhook URL the assignment shows, or let XTM One create.

When XTM One creates them, it does so under the integration's own OpenCTI account: a webhook notifier pointing at the assignment and a live trigger named `XTM One - <fleet> - <purpose>`, filtered on the entity types, labels, minimum score and event types the assignment declares. It checks them every 15 minutes, recreates one deleted in OpenCTI, restores one that was edited, and removes both when the assignment stops asking, is disabled, or is deleted.

- **Filters from your organization profile.** The trigger can derive its filters from the [organization profile](../../administration/organization.md): the priority intelligence requirements (an entity flagged in one of them), the regions and the sector (a container or relationship involving the matching OpenCTI country or sector). The trigger follows the profile when it changes. A field the profile leaves empty, or a region or sector OpenCTI has no entity for, stops the setup with an error naming it, rather than watching wider than asked.
- **Never on every change.** A trigger with no filter at all is refused.
- **A webhook secret is required.** An administrator sets the `webhook_secret` field on the OpenCTI integration in XTM One; OpenCTI sends it with every event. Without it, XTM One sets nothing up.
- **Not on the assignment page yet.** The assignment's trigger must ask for this setup in its configuration, which an assignment created through the API or by an agent can do.

## What to expect when you use it

When you trigger an XTM One action from OpenCTI:

- the current OpenCTI context is the starting point, so you do not have to re-explain it
- XTM One routes the request to the agent bound to that capability, for the business context declared by your OpenCTI instance — CTI, FIMI, or Fraud-oriented workflows can resolve to different agents
- the result is returned inside the OpenCTI experience rather than as a separate generic chat

This makes the feature feel more like an OpenCTI capability than a detached AI assistant.

## Why a feature may be unavailable

You may not see a given XTM One action in OpenCTI if:

- no agent is bound to that capability, or the bound agent is disabled
- the bound agent is private, since an agent offered to platform users has to be company-managed or shared with a group
- the binding targets a business context different from the one your OpenCTI instance declares
- the action is not enabled for your deployment, or your OpenCTI version does not request that capability
- your OpenCTI permissions do not allow the underlying operation

The product intentionally disables a feature when no suitable XTM One agent is available, rather than failing when you click. Administrators can check and fix all of this on the [`Intents`](../../administration/intents.md) page.

## Permissions and trust

As an end user, you should expect XTM One to act within the boundaries of your OpenCTI access.

That means:

- the analysis is based on what your current session can access
- results should be reviewed as contextual assistance, not as an automatic truth
- if a workflow requires validation, your product can still ask for human review

## Good practices

- Launch the action from the most relevant report, case, or document.
- Use the output to speed up investigation, not to skip validation.
- Prefer a playbook step over a manual action when the same treatment has to apply to every incoming document.
- If an action is missing, ask whether the relevant XTM One capability has been configured for your environment.

## OpenCTI MCP server

Connecting OpenCTI to XTM One also activates a native **MCP server** for the platform.

Any MCP-compatible client (Cursor, Claude Desktop, custom agents) can connect to `https://<your-xtm-one>/mcp/opencti` with a personal XTM One API key and search, read, create, and update threat intelligence knowledge — entities, observables, relationships, indicators, and containers — with your own OpenCTI permissions.

Find your endpoint and a ready-to-copy client configuration in `My Profile > MCP Endpoint`, and see [`Platform MCP servers`](../resources/platform-mcp-servers.md) for the full guide.

## Next step

Continue with [`XTM One in OpenAEV`](openaev.md) if your organization also uses adversarial exposure validation workflows. Administrators who need to change which agent answers a capability should read [`Intents`](../../administration/intents.md).
