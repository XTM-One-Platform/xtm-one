# Prompt safety (Nova)

The Nova Prompt Safety integration screens what reaches your agents against a set of detection rules, and blocks or logs what they match: jailbreaks, prompt injections, attempts to extract secrets and other attacks on a language model. It applies to every agent on the platform as soon as an administrator connects it.

## Built on NOVA

The integration is built on two open-source projects by Thomas Roccia, both released under the MIT license:

- [NOVA](https://github.com/Nova-Hunting/nova-framework) (`Nova-Hunting/nova-framework`), the prompt pattern matching framework. Its rule format, `.nov`, is the one you write and read on the `Rules` tab. Project site: [novahunting.ai](https://novahunting.ai).
- [NOVA rules](https://github.com/Nova-Hunting/nova-rules) (`Nova-Hunting/nova-rules`), the community rule collection the platform syncs by default.

The integration's setup guide credits NOVA under its title, with a link to the repository.

!!! tip "Contributing back"
    A detection rule that would help others belongs upstream: the NOVA rules repository accepts contributions. Rules you write on the `Rules` tab stay on your platform.

## Connect Nova

1. Open `Integrations` and choose `New Integration`.
2. Pick `Nova Prompt Safety`. It needs no credentials.
3. Make the connection company-managed. Nova's rules and its guard are platform-wide, so only a company-managed connection syncs rules and screens messages. A personal connection syncs nothing, and `Test Connection` says so.

Connecting adds the community rule set (`Nova-Hunting/nova-rules`) as the rule source and syncs it once a day. Disconnecting stops the guard at once and removes the rule source and the rules it synced, including synced rules you edited. Rules you wrote yourself on the `Rules` tab are kept.

## Check what the guard runs

`Test Connection` on the integration tells you whether the guard is actually protecting anything: how many rules are active, when the rule source last synced, and which rule tiers can fire. It reports a problem when no rule is active (the guard then allows every message), when the last sync failed, or when syncing is turned off.

A NOVA rule can match a message in three ways:

- `keywords` - exact words and regular expressions. Always available.
- `semantics` - meaning similarity, scored with the platform's embedding model.
- `llm` - a yes/no question put to a language model.

!!! warning "The semantics and llm tiers need the full NOVA engine"
    The `semantics` and `llm` tiers run only when the NOVA engine is installed with the platform. Without it, every rule is evaluated on its keywords only: a rule that rests on a `semantics` or `llm` section cannot match. `Test Connection` says "keyword/regex tier only" in that case, and the `Rules` tab shows that these tiers need the optional engine.

## Manage the rules

The rules are managed in the `Rules` tab of the integration, which only administrators see: open the Nova integration from `Tools & Integrations > Integrations`.

- `Rule source`: the repository the community rules come from, when it last synced and how many rules it brought. Pick how often it syncs (`Manual`, `Every 6h`, `Every 12h`, `Daily` or `Weekly`), or select `Sync now`.
- `Models` (shown only when the full NOVA engine is installed): the language model the `llm` tier asks and the embedding model the `semantics` tier scores with. Both follow the platform defaults unless you pick one.
- The rule list: every rule, with its tiers and its action. Select `New rule` to write one in the `.nov` format, or open a rule to edit, disable or delete it.

Each rule has an action:

- `Log` records each match and lets the message through.
- `Block` refuses the message: the agent never sees it and the person gets a short refusal instead of an answer. Every match is recorded as well.

Synced community rules always arrive as `Log`. Watch what they match before you switch a rule to `Block`; your choice of action, and whether a rule is enabled, is kept across every sync.

!!! note "Uncalibrated semantics"
    The community rules' `semantics` thresholds were tuned for one embedding model. The platform rescales the scores of the models it has a calibration for. A rule scored by any other model shows an `Uncalibrated semantics` badge: keep such rules on `Log` until the model is calibrated. [Configuration](../deployment/configuration.md#semantic-threshold-calibration) explains how to calibrate one.

## What the guard screens

While Nova is connected, the guard evaluates:

- Every message sent to an agent, before the model is called. A `Block` rule refuses it.
- Messages sent to the General Assistant and to a public chat widget, and the text extracted from documents a widget visitor uploads. Image uploads are not screened. By default matches are only recorded; turn on the matching switch in `Security options` to refuse content matched by a `Block` rule.
- What an agent saves to its memory, after it is stored. A match is recorded and the memory is kept, so a `Block` rule never blocks a memory.

If the guard fails or does not answer in time, the content goes through: the failure is written to the platform log, not to the audit log.

You can extend it, in `Settings` > `Security` > `Security options`:

- `Tools screened by the prompt guard`: the tools whose results are evaluated, including the text of their attached files (images excluded). No tools are screened by default. Unless withholding is enabled, evaluation runs in the background without delaying the agent.
- Withholding a tool result that a `Block` rule matches, instead of only recording the match.
- Evaluating the model's answers (`off`, `log` or `block`). Off by default. `block` withholds an answer the platform delivers whole. An answer shown as it is written, as in the web chat, has already been seen when it is evaluated, so a match on it is only recorded.

Each option is described in [Settings](settings.md#security-options), with its environment variable in [Configuration](../deployment/configuration.md#settings-security-options).

## Review matches

Every match is written to the audit log, in `Admin` > `Logs`, as a `Security alert`. The entry names the rule, its action, whether the message was refused, and where the message came from (a chat message, a tool result, a memory write, a widget upload or a model answer).

The [security audit](settings.md#security-audit) also reports the state of the guard: which surfaces it covers, whether the `llm` tier asks every condition, and whether `semantics` rules are scored by an uncalibrated model.

## Next step

Tune the guard's cost and calibration in [Configuration](../deployment/configuration.md#nova-prompt-safety-engine), or review the other platform-wide protections in [Settings](settings.md#security).
