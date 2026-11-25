# XTM One in OpenCRQ

OpenCRQ is the governance, risk and compliance module of the XTM Platform. It owns frameworks, controls, assessments, risk scenarios and the financial quantification of cyber risk. XTM One brings its agents into OpenCRQ, gives your own agents OpenCRQ tools, and keeps the primary assets of your organization profile in line with OpenCRQ's assets.

## The embedded assistant

Once OpenCRQ is registered with XTM One, the OpenCRQ header shows an `Ask Ariane` button. It opens the XTM One chat in a side panel, without leaving OpenCRQ. The button appears only when the OpenCRQ deployment is connected to XTM One and runs Enterprise Edition; a Community Edition deployment does not show it.

Each request goes through the OpenCRQ backend to XTM One, under your own identity, so the answer stays within what your OpenCRQ account can see.

| Capability | What it does | Default agent |
| --- | --- | --- |
| `global.assistant` | The conversation behind `Ask Ariane` | `CTEM Assistant`, which hands the conversation over to the `OpenCRQ Assistant` |
| `global.summarize`, `global.explain`, `global.make_it_shorter`, `global.make_it_longer`, `global.fix_spelling`, `global.change_tone` | Writing help on text content | `OpenCRQ Assistant` |
| `crq.control_gap_analysis` | Analyze control capability gaps against a framework | `OpenCRQ Assistant` |
| `crq.risk_scenario_generator` | Draft risk scenarios from business and threat context | `OpenCRQ Assistant` |

OpenCRQ declares these capabilities when it registers. Which of them has a button in OpenCRQ depends on your OpenCRQ version, and an administrator can bind a different agent to any of them on the [`Intents`](../../administration/intents.md) page.

## OpenCRQ tools for your agents

The `OpenCRQ Assistant` explores frameworks and control capabilities, analyzes compliance gaps, and manages risk scenarios with their financial figures. Your own agents get the same tools when you bind them to the OpenCRQ integration.

The tools follow the OpenCRQ instance you connect: a tool the instance cannot serve is hidden from your agents, and a field the instance does not accept is refused before anything is sent. See [OpenCRQ tools](../resources/integrations.md#opencrq-tools).

## Primary assets and OpenCRQ

The [organization profile](../../administration/organization.md) lists your primary assets, the business assets that matter most. OpenCRQ rates the same primary assets and links them to their supporting assets. XTM One keeps them in sync on a schedule: `CTEM` > `Inventory` > `Sources` lists OpenCRQ as the source of the primary assets, daily by default, with its last run, its counts and `Run now`.

- An OpenCRQ primary asset is added or updated on each run, and its supporting assets are linked to it unless the link option is off.
- A primary asset, or a link, added or edited by hand is never overwritten by the sync.

## OpenCRQ MCP server

Connecting OpenCRQ to XTM One also activates a native MCP server for it. Any MCP-compatible client can connect to `https://<your-xtm-one>/mcp/opencrq` with a personal XTM One API key and work with frameworks, requirements, control capabilities, assessments, risk scenarios, assets and vulnerability findings, with your own OpenCRQ permissions.

Find your endpoint and a ready-to-copy client configuration in `My Profile > MCP Endpoint`, and see [`Platform MCP servers`](../resources/platform-mcp-servers.md) for the full guide.

## On the home dashboard

The exposure lifecycle on the [home dashboard](../home-dashboard.md) has a `Governance & Risk` card for OpenCRQ. Today it is greyed and shows sample figures only: it does not read your OpenCRQ data yet.

## Why a feature may be unavailable

- the OpenCRQ deployment is not registered with XTM One, or runs Community Edition
- no agent is bound to the capability, or the bound agent is disabled or private
- the binding targets another business context than `crq`
- your OpenCRQ permissions do not allow the operation

## Next step

Administrators who want to fill in the primary assets first should read [Organization profile](../../administration/organization.md). To connect an MCP client, continue with [`Platform MCP servers`](../resources/platform-mcp-servers.md).
