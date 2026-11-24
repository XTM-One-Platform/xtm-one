# Anonymous Usage Telemetry

OpenCRQ collects **anonymous usage statistics** to help Filigran understand fleet adoption and prioritize product development, like the sibling products OpenCTI, OpenAEV and XTM One.

## Privacy guarantees

- **Aggregate counts only** — cross-tenant totals, never per-tenant breakdowns. Tenant identity is customer data and never leaves the platform.
- **No names, no content** — no user emails, tenant names, framework names, asset names, or any document/assessment content is ever collected.
- **Anonymous instance identity** — the instance is identified by a random UUID generated on first boot (stored in the `platform_metadata` table), with no link to your organization.
- **Self-disabling** — at startup the platform probes the Filigran telemetry endpoint once; if it is unreachable (air-gapped deployment, blocked egress), telemetry silently disables itself for the lifetime of the process. No retries, no error noise.

## How to disable telemetry

Set `TELEMETRY__USAGE__ENABLED=false` on the platform and restart it. In the XTM Docker bundle, add it to the `environment` of the `opencrq` service, in a `docker-compose.override.yml` that an update of the bundle leaves alone. The platform then makes no connection to the collector at all: no startup check, no export. The startup log says `Anonymous usage telemetry is disabled by TELEMETRY__USAGE__ENABLED=false, nothing is sent`.

Blocking outbound access to the collector and restarting has the same effect. Air-gapped platforms never send telemetry.

## Transport

Metrics are exported using the OpenTelemetry protocol (OTLP/HTTP). When the application environment resolves to `development`, export happens every 3 hours and database counts are refreshed every minute; in all other environments, export happens every 6 hours and database counts are refreshed hourly. The collector endpoint is `https://telemetry.ocrq.filigran.io`, for an explicit `NODE_ENV=production` only (set by the official Docker image); any other value — including unset — reports to a separate non-production collector, so non-production data can never pollute fleet analytics. This pipeline is fully separate from the operator observability settings (`TELEMETRY__OBSERVABILITY__*`), which target your own collector.

## Resource attributes

Attached to every export:

| Attribute | Description |
|:----------|:------------|
| `service.name` | Always `opencrq-telemetry`. |
| `service.version` | The platform version: `0.YYMMDD.z` for a release, `0.YYMMDD.0-dev.<sha>` for any other image, `0.0.0-dev` from source. |
| `service.instance.id` | The anonymous instance UUID. |
| `service.instance.creation` | First-boot timestamp of the instance. |
| `filigran.telemetry.tags` | Optional deployment tags from `TELEMETRY__USAGE__TAGS` (comma-separated, normalized: trimmed, lowercased, deduplicated, sorted). Omitted when unset. |

## Collected metrics

| Metric | Description |
|:-------|:------------|
| `is_enterprise_edition` | `1` when the platform is Enterprise Edition entitled, `0` otherwise. The entitlement is platform-wide, so every process exports the same value. |
| `total_users_count` | Total number of users. |
| `service_accounts_count` | Total number of service accounts. |
| `total_tenants_count` | Total number of tenants. |
| `organizations_count` | Total number of organizations. |
| `groups_count` | Total number of groups. |
| `roles_count` | Total number of tenant roles. |
| `platform_roles_count` | Total number of platform roles. |
| `control_capabilities_count` | Total number of control capabilities. |
| `control_capability_assessments_count` | Total number of capability assessments. |
| `findings_count` | Total number of findings. |
| `risk_scenarios_count` | Total number of risk scenarios. |
| `crq_indicator_runs_count` | Total number of CRQ indicator runs. |
| `loss_drivers_count` | Total number of loss drivers. |
| `attack_outcomes_count` | Total number of attack outcomes. |
| `scenario_loss_estimates_count` | Total number of scenario loss estimates. |
| `primary_assets_count` | Total number of primary assets. |
| `supporting_assets_count` | Total number of supporting assets. |
| `attack_surfaces_count` | Total number of attack surfaces. |
| `support_to_primary_links_count` | Total number of supporting-to-primary asset dependencies. |
| `support_to_support_links_count` | Total number of supporting-to-supporting asset dependencies. |
| `integration_settings_count` | Total number of configured connector integrations. |
| `integration_<source>_count` | Number of configured integrations of one connector source, one metric per source: `first`, `nist`, `cisa`, `microsoft_graph`, `openaev`, `drata`. |
| `user_login_count` | Number of user sessions created since the previous export (sign-ins and renewals through XTM One). No user identity is recorded. |
| `risk_scenario_created_count` | Number of risk scenarios created since the previous export. |
| `crq_simulation_launched_count` | Number of CRQ simulations launched since the previous export. |
| `connector_sync_launched_count` | Number of connector sync jobs launched since the previous export. |

## Deployment tags

Set `TELEMETRY__USAGE__TAGS` to attach deployment dimensions to every metric, e.g.:

```bash
TELEMETRY__USAGE__TAGS=saas,eu-west
```

Tags are freeform, comma-separated, and normalized before export. They allow Filigran to slice fleet analytics by deployment dimension (SaaS vs on-prem, region, ...).
