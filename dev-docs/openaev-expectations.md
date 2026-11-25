# OpenAEV expectations - the model our tooling has to get right

Internal reference for anyone touching
[`backend/app/integrations/providers/openaev/expectations.py`](../backend/app/integrations/providers/openaev/expectations.py)
or the expectation formatters in
[`_common.py`](../backend/app/integrations/providers/openaev/_common.py).

This document exists because the semantics are counter-intuitive in three
places, and every one of them has produced a wrong agent answer:

1. inject execution status is not a security outcome;
2. `VULNERABILITY` expectations have inverted polarity;
3. `PENDING` is silently excluded from the aggregate average.

Per architecture invariants #15 / #16 the fix for all of this is deterministic
code (formatters + tool descriptions + enums), not system-prompt text. Nothing
here should be copied into a persona or a skill.

## The one-line model

An **inject** is the attack. An **expectation** is the scored verdict on the
defense. `inject_status` says whether the attack ran; `inject_expectation_*`
says whether the security control worked. They come from disjoint code paths
and can disagree in every combination. Every score, ATT&CK coverage figure and
posture percentage in OpenAEV is computed from expectations - never from
execution status.

A fully `EXECUTED` simulation whose detection expectations all `FAILED` is a
security failure: the attacks ran and nobody noticed. That is exactly what a
BAS platform exists to reveal.

## Types and families

Six stored types live on `injects_expectations` (the discriminator column):
`PREVENTION`, `DETECTION`, `VULNERABILITY`, `MANUAL`, `ARTICLE`, `CHALLENGE`.

Results aggregate into four **families**, because `MANUAL` / `ARTICLE` /
`CHALLENGE` all collapse into `HUMAN_RESPONSE`. A results payload therefore
never contains those three names. Our constants mirror both lists:
`EXPECTATION_TYPES` and `EXPECTATION_RESULT_TYPES`.

What SUCCESS proves, per family:

- `PREVENTION` - the attack was **blocked**.
- `DETECTION` - the attack was **seen** (an alert fired). It still executed.
- `VULNERABILITY` - **inverted**: SUCCESS means the target is *not* vulnerable;
  FAILED means a scanner proved it *is*. The failure is the interesting finding.
- `HUMAN_RESPONSE` - a person or team reacted as the playbook expects. Validated
  manually, never by a security product.

## Status derivation

`InjectExpectationHelper.computeStatus(score, expectedScore)`:

- `expectedScore == null` -> `UNKNOWN`
- `score == null` -> `PENDING`
- `score >= expectedScore` -> `SUCCESS`
- otherwise -> `FAILED`

So **`PARTIAL` never appears on an individual row**. It exists only in
aggregates. And SUCCESS is `>=`, not `== 100`: the expected score is
configurable (100 for technical expectations, 50 for manual ones by default).

## Multi-source scoring: max wins, but only once everyone answered

`ExpectationResultBuilder.computeScore` keeps the score `null` while *any*
expected source still has an empty result. Once they have all answered, the
**highest** result wins.

Two consequences the formatters spell out:

- one EDR reporting "Detected" does not immediately make the row SUCCESS - the
  row stays PENDING until the other expected platforms report or the window
  expires;
- once complete, one platform detecting is enough for SUCCESS even if three
  missed it. OpenAEV measures effective layered protection, not per-tool
  accuracy. Per-tool detail lives in the per-source `results` list.

## Expiration: silence becomes a verdict, with different polarity

A built-in pseudo-collector sweeps expectations past
`created_at + expiration_time` and writes the literal result text `"Expired"`
on every still-empty per-source row. The verdict it then applies depends on
the type:

- `DETECTION` / `PREVENTION` / human types: silence = **failure**.
- `VULNERABILITY`: silence = **success** ("Not vulnerable") - no scanner
  reported a finding, so the target defaults to clean.

This is why `_fmt_expectation_row` calls out `Expired` sources explicitly: a
`FAILED` detection may mean "the EDR missed it" *or* "no EDR was ever
connected". Those are very different findings and the agent must not conflate
them. Defaults: 6 h technical, 24 h human.

Parent rows are never force-failed by expiry - they are recomputed from their
children.

## Hierarchy and validation modes

There is no explicit level column; the level is implied by which target field
is set. Agent rows are the ground truth written by collectors. Asset rows are
derived (an asset needs **all** its agents to succeed) unless the asset is
agentless, in which case the asset row is itself the leaf. Asset-group rows are
always derived and **cannot be validated directly** - the API raises
`IllegalArgumentException`. Team rows aggregate their players.

`inject_expectation_group` is a **validation mode**, not "this row is a group":

- `false` = all targets must validate (compliance semantics)
- `true` = at least one target must validate (SOC-detection semantics)

Misreading it inverts group verdicts. Note it also shows up as `true` on
ordinary asset rows simply because the asset belongs to a group.

Manual validation fans out: validating an asset that has agents writes the
verdict onto every agent row underneath it; validating a team overwrites every
player row with the team score. Our `validate` action says so in its response.

## Aggregation: the two different rates

Per family, `ExpectationResultsByType` carries `{type, avgResult,
distribution:[{id,label,value}]}` - camelCase, unlike the snake_case entity
fields.

- `avgResult` averages only rows that **already have a score**. Pending rows
  are excluded, so one success plus ninety-nine pending averages to `SUCCESS`.
- the success rate divides by **every** row including pending, so it can be far
  lower than `avgResult` suggests.

`_fmt_expectation_results_by_type` prints the distribution counts, computes the
pending-inclusive rate, and adds an explicit caution line when pending rows
could still change a SUCCESS or PARTIAL verdict. `avgResult: "UNKNOWN"` with an
empty distribution means the inject *declares* that expectation type but has no
rows yet - almost always "not executed", not "inconclusive".

Row counts are not target counts: an asset targeted directly and through two
groups produces three asset rows plus three sets of agent rows, and the
simulation-level results include team, asset and asset-group rows.

## Views that legitimately disagree

`simulation_results` includes every non-player, non-agent row.
`by_attack_pattern` uses only expectations whose target is a **direct relation**
of the inject, and repeats an inject once per mapped technique. The two numbers
can differ for the same simulation without either being wrong; the
`by_attack_pattern` output says so inline.

`target_results_merged` keeps one row per type, recomputes the score as the max
across results, and **drops collector-sourced results** - so never use it to
enumerate which security platform reported.

## Findings are not expectations

A `Finding` is an artifact parsed from the attacker's own structured output (a
CVE on a host, a captured credential, an open port). It has no score, no
threshold and no status. An expectation is a scored pass/fail judgement on a
control. The only overlap is that CVE findings and `VULNERABILITY` expectations
consume the same structured output.

## Simulation FINISHED does not mean the data is complete

A simulation flips to `FINISHED` once every enabled inject is terminal and
every non-error inject has finished *collecting* - and an expectation counts as
collected when it has no placeholders, or all are filled, **or it expired**.
Expiry counts as resolved, deliberately, so one silent collector cannot keep a
simulation RUNNING forever. So `FINISHED` means "the collection window closed",
not "every expectation was answered by a real source".

## Drift

Drift compares an inject's **stored** expectations against the predefined
expectations its injector contract exposes today, on validation-defining
attributes only (type, group semantics, expected security platforms). A tuned
score, name or expiration is never drift. `drift_detected: true` means the
contracts evolved or the expectations were customised on purpose - it does not
mean anything mis-scored. `realign` overwrites customisation and resets the
expected score to 100, so prefer `dismiss` when the difference is deliberate.
`total_inject_count` counts only injects whose contract declares expectations.

## Why the collector endpoints are excluded from our tooling

`/api/injects/expectations`, `/api/injects/expectations/{sourceId}`,
`/api/injects/expectations/prevention|detection`, `.../ai/{sourceId}`,
`PUT /api/injects/expectations[/bulk]` and `/api/inject-expectations-traces`
(POST) are **collector-facing**. They return only unfilled agent-level rows -
a work queue, not results - and the write side exists so an EDR can post its
own verdict.

The previous `manage_openaev_expectation` called exactly those routes, which
meant the agent read a queue instead of results and, via `update_score`, wrote
verdicts while impersonating a security platform. `expectations.py` uses the
same endpoints the OpenAEV UI uses, and its only write path is the documented
manual-validation endpoint. `test_expectation_tool_does_not_expose_collector_endpoints`
locks this in.

## Reading traces: one call per collector source

`GET /api/inject-expectations-traces` (the read the UI uses) requires BOTH
`injectExpectationId` and `sourceId`: traces are stored per security platform
and the endpoint resolves the collector's platform first. A call without
`sourceId` is a bare Spring 400 (#2371). `action=traces` therefore always sends
both: with an explicit `source_id` it makes one call; without one it reads the
row (through `inject_id` + `target_id` + `target_type`, or `exercise_id`), takes
the results whose `sourceType` is `collector` and calls once per source, the
traces merged and labelled by source. A row with no collector result answers
that traces exist only for collectors, without any traces call. With no locator
it probes the collectors bound to a security platform (at most 10). Manual,
media-pressure and vulnerability-manager results never carry traces, and a
collector uploads traces only for the alerts it matched, so a Not Detected row
normally has none. `tests/test_openaev_expectation_traces.py` locks this in.
