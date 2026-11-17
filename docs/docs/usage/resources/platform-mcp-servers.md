# Platform MCP servers

XTM One embeds a native **Model Context Protocol (MCP) server** for every connected Filigran platform. This means any MCP-compatible client — Cursor, Claude Desktop, Claude Code, or your own agents — can connect to **OpenCTI**, **OpenAEV**, and **OpenCRQ** through XTM One and work with your threat intelligence, exposure validation, and governance-risk-compliance content directly.

This page explains what the platform MCP servers are, where to find them, and how to connect a client.

## What you get

One dedicated MCP endpoint per platform:

| Endpoint | Platform | What it exposes |
| --- | --- | --- |
| `https://<your-xtm-one>/mcp/opencti` | OpenCTI | Threat intelligence knowledge: search, entities (SDOs), observables (SCOs), relationships, indicators, containers (reports, cases, groupings), notes, labels, markings, statistics |
| `https://<your-xtm-one>/mcp/openaev` | OpenAEV | Adversarial exposure validation: scenarios, simulations, payloads, injects, atomic tests, expectations, findings, vulnerabilities, assets, media content, lessons learned, reports |
| `https://<your-xtm-one>/mcp/opencrq` | OpenCRQ | Governance, risk & compliance: frameworks, requirements, control capabilities, compliance assessments, risk scenarios, attack outcomes, CRQ financial summaries, assets, vulnerability findings and posture, CVE search, threats, treatment plans, budget reviews, risk dashboard |

All endpoints use the MCP **Streamable HTTP** transport, the modern standard supported by all major MCP clients, and speak the protocol revisions 2026-07-28, 2025-11-25, 2025-06-18 and 2025-03-26: your client gets the revision it asks for. A call from a web page is accepted only from the platform's own address or one listed in `CORS_ORIGINS`; desktop, command-line and hosted clients send no such origin and are not concerned.

## Availability

The platform MCP servers are a **Community Edition** feature. There is nothing to install and nothing extra to license:

- A server becomes available **as soon as the corresponding platform is registered** with XTM One — the same registration that powers the embedded "Ask AI" experience in OpenCTI, OpenAEV, and OpenCRQ.
- While no platform of that type is connected, the endpoint simply answers that the platform is not registered.
- Administrators can turn all platform servers off globally with the **Platform MCP Servers** toggle in `Settings > MCP Endpoint`.

## Where to find your endpoints

Open `My Profile > MCP Endpoint`.

The **Platform MCP Servers** card is always visible there (as long as an administrator has not disabled the feature) and lists every platform server with its live status. For each **connected** platform it shows:

- the endpoint URL, with a copy button
- the live connection status (`Connected` or `Registered`)
- a `Config` button that copies a ready-to-paste MCP client configuration

A platform that is not yet connected appears dimmed as `Not connected`, with a reminder that its MCP server activates automatically once the platform is registered with XTM One.

Administrators see the same information, along with the global enable/disable toggle, in `Settings > MCP Endpoint`.

## Authentication

The servers accept a standard bearer token:

- a **personal API key** (`fcp-...`), created in `My Profile > API Keys`, or
- an **OAuth 2.1** access token — MCP clients that support OAuth discover the authorization flow automatically. Clients such as claude.ai, Claude Code and VS Code identify themselves with a client metadata document and need no registration; others register themselves. Either way you sign in and approve the client on an XTM One page that names it.

Every tool call runs **as you** on the platform. XTM One signs a short-lived token for your user (matched by email on the platform side), so your platform permissions, data markings, and audit trail all apply exactly as if you performed the action yourself. The platform's own API token is never stored in or proxied through XTM One.

## Connect a client

Copy the configuration from the `Config` button in your profile, or use this template (keep only the servers you need):

```json
{
  "mcpServers": {
    "opencti": {
      "url": "https://<your-xtm-one>/mcp/opencti",
      "headers": {
        "Authorization": "Bearer fcp-..."
      }
    },
    "openaev": {
      "url": "https://<your-xtm-one>/mcp/openaev",
      "headers": {
        "Authorization": "Bearer fcp-..."
      }
    },
    "opencrq": {
      "url": "https://<your-xtm-one>/mcp/opencrq",
      "headers": {
        "Authorization": "Bearer fcp-..."
      }
    }
  }
}
```

Where to paste it:

- **Cursor**: `.cursor/mcp.json` in your project (or the global MCP settings)
- **Claude Desktop**: `claude_desktop_config.json`
- **Other clients**: any client supporting MCP Streamable HTTP with custom headers

You can verify the connection from a terminal:

```bash
curl -s -X POST https://<your-xtm-one>/mcp/opencti \
  -H "Authorization: Bearer fcp-..." \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}'
```

## What the tools cover

The tools are the same battle-tested tools used by XTM One agents through the native platform integrations.

For OpenCTI, you can:

- search and read any STIX entity, observable, relationship, or container
- create and update entities, observables, indicators, relationships, containers, and notes
- manage labels, markings, and container membership
- promote observables to indicators
- retrieve statistics, top related entities, and modification history

For OpenAEV, you can:

- search and read scenarios, simulations, payloads, injects, findings, assets, and ATT&CK content
- create and manage scenarios, simulations, injects, expectations, and atomic tests
- create and manage payloads, findings, and vulnerabilities
- work with documents, media channels, articles, challenges, lessons learned, and reports

For OpenCRQ, you can:

- browse tenants, organizations, compliance frameworks, and their requirements
- read control capabilities (strength, coverage) and declare their assessment, declare or clear a capability's posture for one organization's scope, add, remove, restore or reset the ATT&CK techniques it covers, and read and answer compliance assessments
- create and manage risk scenarios, set a scenario's or an organization's risk tolerance (a scenario's can be cleared), and read attack outcomes and CRQ financial summaries
- list primary and supporting assets, set every field of a supporting asset (subcategory, URL, linked person, tags, machine, identity or AI target properties) and clear the optional ones with an empty string, change the roles of dependency links, delete a manually added vulnerability evidence, hand an evidence state or a whole asset back to its connectors, and search CVEs
- read vulnerability findings, the top vulnerabilities, asset posture scores, and what attack simulations prevented and detected
- browse the threat catalog, the threats relevant to a risk scenario, and the threats exploiting a CVE
- read risk treatment plans and budget reviews with their simulated loss reduction, the risk dashboard, and the posture attack simulations proved for a control

OpenCRQ is multi-tenant: every tool accepts an optional tenant identifier and resolves your sole tenant automatically when omitted.

The OpenCRQ tools follow the OpenCRQ instance that is connected: an instance that does not serve a tool does not have it offered to your client. The tool count shown for OpenCRQ is the full list. See [OpenCRQ tools follow each instance](../../deployment/xtm-suite-integration.md#opencrq-tools-follow-each-instance).

Every tool comes with a title and the standard MCP annotations, so your client knows what a call does before making it:

- reads (search, get, list) are marked **read-only**;
- writes that only add something (create, add, run a simulation) are marked **not destructive**;
- writes that change or remove what exists (update, delete, remove, manage) are marked **destructive**, and most clients ask you to confirm them.

The annotations are hints for your client. What a call may actually do is still decided by your permissions on the platform.

## What the tools deliberately do not cover

The platform MCP servers expose **business and knowledge tools only**. Platform administration is out of scope by design:

- no user, group, role, tenant, or organization management
- no playbooks, dashboards, or workspace administration
- no notification, customization, ingestion, or data-sharing configuration
- no connector or platform settings management

This is enforced server-side with a strict allow-list: a tool that is not explicitly allowed can never be listed or executed through these endpoints.

!!! note "OpenCTI writes are direct"

    Knowledge changes made through the OpenCTI MCP server are applied directly to the live knowledge graph, with your user's permissions. The draft-review workflow used by XTM One agents does not apply to MCP clients — review changes on the client side before running write tools.

## Troubleshooting

| Symptom | Meaning | What to do |
| --- | --- | --- |
| `401 Unauthorized` | Missing or invalid token | Check the `Authorization` header and that your API key is active |
| `403 Forbidden` | Platform MCP servers are disabled | Ask an administrator to enable them in `Settings > MCP Endpoint` |
| `404 Not Found` | The platform is not registered with XTM One | Connect the OpenCTI / OpenAEV / OpenCRQ instance to XTM One first |
| Tool result contains an error message | The platform rejected the call (permissions, invalid input, connectivity) | Read the message — it reflects your own permissions on the platform |

## Good practices

- Create a dedicated API key per MCP client so you can revoke access independently.
- Remember that every action is performed as you — write tools modify real platform data.
- If a tool you expect is missing, it is most likely excluded on purpose (platform administration) rather than misconfigured.

## Next step

If you also want to expose XTM One **agents** (not platform tools) to your MCP clients, continue with the `MCP Endpoint` tab described in [`My Profile`](../my-profile.md).

To read agent traces and manage XTM One's own objects from an MCP client, see [`Platform tools MCP server`](platform-tools-mcp-server.md).
