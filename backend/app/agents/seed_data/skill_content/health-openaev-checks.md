# OpenAEV health checks

Your playbook for one registered OpenAEV platform (the registration the trigger names). Run every check, in this order, read-only. Each check ends **pass**, **warn**, **fail** or **not run**, with the evidence you read: the tool, the objects, the values and the time you read them.

## Rules

- Look at the module itself, platform-wide. Never judge a specific collector, injector, executor or third-party data source, and never name one in a verdict.
- A check whose tool is not among your read tools is **not run**, with the reason "tool not on the allow-list"; never infer it from another check.
- A value the module does not expose is **not run**, with the reason; never a pass.
- Thresholds below are the defaults; a lower threshold never applies.

## Checks

| Check | Read | Warn | Fail |
|---|---|---|---|
| `platform_version_licence` | platform version, licence, settings sanity | licence under 30 days, a missing setting | licence under 7 days, version behind the supported range |
| `simulations_state` | `search_openaev_exercises`, `get_openaev_exercise`: failed or running past their end | any failed in 24 h | running 24 h past its end |
| `scenarios_scheduled` | `search_openaev_scenarios`: scheduled scenarios and their last start | | a scheduled start missed by over 1 h |
| `expectations_due` | expectations past their due time without a result | over 10% | over 30% |
| `findings_flowing` | `search_openaev_findings`: created in the last 7 days vs the previous 7 | 0 after a non-zero week | |
| `data_volume_growth` | simulations, scenarios, assets and findings created in 7 days vs the 4-week weekly mean | under 25% of the mean | |
| `data_completeness` | scenarios with no inject, simulations with no target, assets in no group (`search_openaev_assets`, `search_openaev_asset_groups`), injects with no expectation | over 10% | over 30% |
| `data_consistency` | assets duplicated by hostname or IP | any duplicate | |
| `platform_errors` | error entries and failed internal jobs of 24 h vs the 7-day mean | over 2 x the mean | over 5 x the mean |
| `module_link` | the configured link to OpenCTI: configured and its last call result, never the linked module's data | | configured and failing |

## Report

Store every check with `record_module_check_report` before you end: its verdict, the evidence (tool, arguments, objects, values, time), a recommendation on warn and fail, a reason on not run. A warn or fail opens the check's agentic finding and a pass resolves it. Then write one line per check: name, verdict, the values read, the tool and the time. Then the recommendations you attached to open findings, and every check not run with its reason.
