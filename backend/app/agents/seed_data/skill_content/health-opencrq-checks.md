# OpenCRQ health checks

Your playbook for one registered OpenCRQ platform (the registration the trigger names). Run every check, in this order, read-only. Each check ends **pass**, **warn**, **fail** or **not run**, with the evidence you read: the tool, the objects, the values and the time you read them.

## Rules

- Look at the module itself, platform-wide. Never judge a specific connector or third-party data source, and never name one in a verdict.
- A check whose tool is not among your read tools is **not run**, with the reason "tool not on the allow-list"; never infer it from another check.
- A value the module does not expose is **not run**, with the reason; never a pass.
- Thresholds below are the defaults; a lower threshold never applies.

## Checks

| Check | Read | Warn | Fail |
|---|---|---|---|
| `platform_version` | `get_opencrq_platform_version` vs the supported range | | behind the supported range |
| `jobs_state` | `list_opencrq_jobs`, `get_opencrq_job`: failed or stuck, platform-wide | any failed in 24 h | running over 6 h |
| `crq_run_fresh` | `list_opencrq_crq_run_history`, `get_opencrq_crq_run`: the last successful run and its age | over 7 days | over 30 days, or none |
| `scenario_inputs` | `list_opencrq_risk_scenarios`, `get_opencrq_tef_input`, `get_opencrq_financial_profile`: scenarios missing an input | any | over 25% |
| `data_completeness` | scenarios never scored, or scored before their inputs last changed (`get_opencrq_crq_settings` for the scoring setup) | any | over 25% |
| `data_volume_growth` | risk scenarios and CRQ runs created in 30 days vs the previous 30 | 0 after a non-zero month | |
| `platform_errors` | failed internal jobs and error entries of 24 h vs the 7-day mean | over 2 x the mean | over 5 x the mean |
| `module_link` | the configured links to OpenCTI and OpenAEV: configured and their last call result, never the linked modules' data | | configured and failing |

## Report

Store every check with `record_module_check_report` before you end: its verdict, the evidence (tool, arguments, objects, values, time), a recommendation on warn and fail, a reason on not run. A warn or fail opens the check's agentic finding and a pass resolves it. Then write one line per check: name, verdict, the values read, the tool and the time. Then the recommendations you attached to open findings, and every check not run with its reason.
