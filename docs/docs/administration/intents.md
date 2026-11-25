# Intents

Intents are the mechanism that makes XTM One agents appear inside the other XTM Suite products. A connected platform declares the AI capabilities it wants — summarize this container, translate this question into a search filter, process this bundle in a playbook — and you decide which agent answers each one.

Administrators choose `Intents` from the left sidebar to open the page.

## How routing works

An intent is a named capability, written in dotted notation, such as `cti.container_summary` or `global.assistant`. It says *what* has to be done, never *who* does it. The link between the two is a binding.

The full path from a click in OpenCTI to an answer from one of your agents looks like this:

1. OpenCTI, OpenAEV, or OpenCRQ registers with XTM One and declares the intents it uses, along with its business vertical.
2. XTM One adds any missing intent to the registry and tags it with the platform that declared it.
3. You bind one or more agents to each intent, optionally scoped to a vertical and ordered by priority.
4. A user triggers the feature in the platform. The platform asks XTM One which agents are bound to that intent for its vertical, and uses the one with the highest priority.
5. The bound agent runs in XTM One with the context sent by the platform, and the result is returned into the product.

Step 3 is the one you control. Everything a user sees — or does not see — in another Suite product follows from it.

!!! note "No binding, no feature"
    An intent that no agent is bound to is shown with an `Unbound` badge. The platform that declared it treats the corresponding feature as unavailable and usually hides or disables it. Binding an agent is what turns the feature on.

## Verticals

A vertical is the business coloration of a connected platform — `cti`, `aev`, `crq`, and, depending on the deployment, labels such as `fimi` or `fraud`. It lets the same intent resolve to a different agent depending on the context the request comes from.

The `Verticals` panel at the top of the page lists them all with the number of bindings that use them:

- Verticals declared by a platform at registration are created automatically.
- Administrators can add their own with `Add Vertical`.
- Renaming a vertical updates every binding that uses it.
- A vertical still used by a binding cannot be deleted.

A binding scoped to `All verticals` applies to every platform, whatever its business context. A binding scoped to one vertical only applies to platforms declaring that vertical.

## Bind an agent to an intent

From the `Intents` page:

1. Expand the intent you want to serve.
2. Choose `Add Binding`.
3. Search for the agent.
4. Select one or more verticals, or `All verticals`. One binding is created per selected vertical.
5. Confirm.

You can do the same from the agent side: open the agent, go to `Configuration`, and use the `Intent Bindings` card. It shows every intent that agent already answers and lets you add or remove bindings without leaving the page.

An agent with the agent builder tools can do it from a conversation: `list_agent_intent_bindings` shows the intents an agent is bound to (vertical, priority, locked, built-in, default or personal) and whether a connected platform can be offered the agent at all, `bind_agent_to_intent` binds it (an intent name such as `cti.stix_transformer`, a vertical or all of them, a priority) and `unbind_agent_from_intent` removes a binding. The rules are the page's: an administrator writes the default binding, anyone else a personal override, a private agent is refused, a locked default is not overridden, and built-in bindings are not changed. `get_agent` lists the agent's bindings too.

!!! warning "The agent cannot be private"
    An agent exposed through an intent is offered to users of the connected platform, so it has to be company-managed or shared with at least one group. XTM One refuses every default binding to a private agent, whatever vertical it is scoped to, and a private agent is never offered to a connected platform even when someone binds it for their own use. Disabled agents are skipped too, even when a binding exists.

Some surfaces in the connected products list several agents for the user to choose from and filter that list by the product tag carried by the agent, such as `opencti` or `openaev`. Tagging your agent with the product identifier is what allows it to show up in those lists.

## Priorities when several agents share an intent

Several agents can be bound to the same intent. In that case the highest priority wins (in a vertical, the priority set for that vertical replaces the base priority), and on a tie the binding made first wins. Priority arrows appear next to each agent in the expanded intent card so you can reorder them. `list_agent_intent_bindings` and `bind_agent_to_intent` name the winner of each intent and vertical, with its priority, and the priority an agent needs to beat it.

Priority is evaluated per vertical. An agent can carry a base priority plus a per-vertical override, which is what you adjust when you move it up or down inside a vertical group. Bindings scoped to `All verticals` are listed inside each vertical group as well, since they compete with the vertical-specific ones.

To take over a capability that a built-in agent currently handles, bind your own agent to the same intent and vertical, then raise its priority above the built-in one.

## Built-in bindings

XTM One ships with a set of bindings that connect its built-in agents to the intents the Suite products declare. They are marked `builtin` and cannot be edited or deleted — they are restored at every startup, so a removed one comes back.

These are the shipped bindings:

| Intent | Capability | Built-in agent | Vertical |
| --- | --- | --- | --- |
| `global.assistant` | General-purpose assistant behind the in-product AI experience | `CTEM Assistant` and `XTM-One Assistant` for all verticals, plus `OpenCTI Assistant`, `OpenAEV Assistant`, `OpenCRQ Assistant`, and `FIMI Analyst` on their own vertical | all, `cti`, `aev`, `crq`, `fimi` |
| `global.summarize` | Summarize content | `OpenCTI Assistant`, `OpenAEV Assistant`, `FIMI Analyst` | `cti`, `aev`, `fimi` |
| `global.explain` | Explain content in simple terms | `OpenCTI Assistant`, `OpenAEV Assistant`, `FIMI Analyst` | `cti`, `aev`, `fimi` |
| `global.make_it_shorter` | Shorten content | `OpenCTI Assistant`, `OpenAEV Assistant`, `FIMI Analyst` | `cti`, `aev`, `fimi` |
| `global.make_it_longer` | Expand content | `OpenCTI Assistant`, `OpenAEV Assistant`, `FIMI Analyst` | `cti`, `aev`, `fimi` |
| `global.fix_spelling` | Fix spelling and grammar | `OpenCTI Assistant`, `OpenAEV Assistant`, `FIMI Analyst` | `cti`, `aev`, `fimi` |
| `global.change_tone` | Change the tone of content | `OpenCTI Assistant`, `OpenAEV Assistant`, `FIMI Analyst` | `cti`, `aev`, `fimi` |
| `cti.nlq_search` | Turn a natural language question into an OpenCTI filter | `OpenCTI NLQ` | `cti` |
| `cti.container_summary` | Summarize a container — report, grouping, case | `OpenCTI Entity Insights` | `cti` |
| `cti.container_report` | Write a report from a container — report, grouping, case | `OpenCTI Entity Insights` | `cti` |
| `cti.containers_digest` | Summarize the containers related to an entity | `OpenCTI Entity Insights` | `cti` |
| `cti.entity_activity` | Analyze the activity trend of an entity | `OpenCTI Entity Insights` | `cti` |
| `cti.entity_forecast` | Forecast the future activity of an entity | `OpenCTI Entity Insights` | `cti` |
| `cti.entity_history` | Summarize the internal history of an entity | `OpenCTI Entity Insights` | `cti` |
| `cti.stix_harvester` | Extract intelligence from a document into a STIX 2.1 bundle | `CTI STIX Harvester (Filigran AI)` by default, `CTI STIX Harvester` as the lower-priority alternative | `cti` |
| `cti.stix_transformer` | Enrich, filter, rewrite, or normalize a STIX 2.1 bundle | `CTI STIX Transformer` on `cti`, `FIMI STIX Transformer` on `fimi` | `cti`, `fimi` |
| `cti.stix_consumer` | Take a final action on a STIX 2.1 bundle at the end of a playbook | `CTI STIX Consumer` | `cti` |
| `cti.ttp_harvester` | Extract tactics, techniques, and procedures from documents | `CTI TTP Harvester (Filigran AI)` | `aev` |
| `aev.attack_path_orchestrator` | Drive a live, objective-driven attack path (AI penetration testing) in OpenAEV | `OpenAEV Attack Path Orchestrator` | `aev` |
| `aev.attack_path_additional_agent` | Specialist the attack-path orchestrator consults during an autonomous run - payload creation, code generation, recon, exploitation support | `OpenAEV Payload Creator` | `aev` |
| `aev.detection_rules_generator` | Generate detection rules from malware behavior | `AEV Detection Rules Generator` | `aev` |
| `aev.phishing_email_html_generator` | Generate phishing lure email HTML from a user prompt for authorized adversary-emulation exercises | `AEV Phishing Email Generator` | `aev` |
| `aev.phishing_landing_page_html_generator` | Generate phishing landing page HTML and CSS from a user prompt for authorized credential-capture simulations | `AEV Phishing Landing Page Generator` | `aev` |
| `aev.message_generator` | Generate messages for adversary emulation exercises | `OpenAEV Assistant` | `aev` |
| `aev.media_article_generator` | Generate media articles for adversary emulation | `OpenAEV Assistant` | `aev` |

The `Intents` page is always the authoritative view for your own platform: it reflects what the connected products actually declared and what is bound today.

!!! note "How the general assistant resolves"
    Several agents are bound to `global.assistant`. The `CTEM Assistant` carries the highest priority, so it is the one a connected product reaches first; it then hands the conversation over to the product specialist — `OpenCTI Assistant`, `OpenAEV Assistant`, or `OpenCRQ Assistant`. Bind your own agent above it if you want a different front door.

## Personal overrides

The bindings an administrator creates are the platform-wide defaults. A single intent can also carry a personal binding, which applies to that user's own requests only and leaves everyone else on the default.

A default binding can also be locked, which is how a capability is forced to resolve the same way for the whole organization. A locked default cannot be overridden, and an attempt to override it is rejected with a message stating that an administrator locked the binding.

## Custom intents

Administrators can add an intent with `Create Intent`, using dotted notation such as `product.feature.action` and a short description.

A custom intent is only useful when something asks for it by name — a platform that declares it at registration, or your own integration calling XTM One. Creating an intent no product asks for has no effect on what users see. An intent that has no binding left can be deleted; one that is still bound cannot.

## When a feature does not appear in a connected product

Work through this list when a user reports a missing AI action:

- The intent shows an `Unbound` badge — no agent is bound to it yet.
- The bound agent is disabled.
- The bound agent is private, so it is never offered to platform users.
- The agent is a copy of the bound one: a copy made with `Duplicate` or `clone_agent` has no binding of its own until you bind it.
- The binding is scoped to a vertical the platform does not declare. Check the vertical on the platform's registration and either rebind to that vertical or use `All verticals`.
- The intent is not listed at all, which means the platform never declared it. Confirm the platform is registered and running a version that requests that capability.
- The user's own permissions on the connected platform do not allow the underlying operation.

For the user-facing side of the same question, see [`XTM One in OpenCTI`](../usage/xtm-suite/opencti.md) and [`XTM One in OpenAEV`](../usage/xtm-suite/openaev.md).

## Next step

Continue with [`Settings`](settings.md) for the rest of the platform configuration, or read [`XTM One in the XTM Suite`](../usage/xtm-suite/index.md) to see how these bindings surface for end users.
