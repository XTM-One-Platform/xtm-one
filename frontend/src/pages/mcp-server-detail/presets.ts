/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

// ---------------------------------------------------------------------------
// MCP Server Presets — quick-configure common MCP servers
// ---------------------------------------------------------------------------
import { withBasePath } from "@/lib/base-path";

export interface MCPPreset {
  id: string;
  name: string;
  description: string;
  logo?: string;
  iconName?:
    | "Search"
    | "HardDrive"
    | "Database"
    | "Zap"
    | "Globe"
    | "FileText"
    | "MessageSquare"
    | "Shield"
    | "Radar"
    | "Crosshair"
    | "Cloud"
    | "Terminal"
    | "Bell";
  transport: "stdio" | "sse" | "streamable_http";
  /** stdio only — remote presets omit it. */
  command?: string;
  /** stdio only — remote presets omit it. A `/path/to/...` entry is a
   *  documentation placeholder the form refuses to create until replaced. */
  args?: string[];
  /** Remote transports only: the server endpoint. Most of these authenticate
   *  with OAuth, so they need no env vars — the user connects after creating. */
  url?: string;
  envVars: { key: string; placeholder: string; description: string }[];
  notes?: string;
}

/**
 * A stdio preset launched with `docker run`. The official XTM One images ship
 * Node.js (`npx`) and uv (`uvx`), not the Docker CLI, so such a preset cannot
 * start on them as shipped: the form says so above the preset's own note.
 */
export function presetNeedsDockerCli(preset: MCPPreset): boolean {
  return preset.transport === "stdio" && preset.command === "docker";
}

export const MCP_PRESETS: MCPPreset[] = [
  {
    id: "github",
    name: "GitHub",
    description: "Repositories, issues, pull requests, code search, and more.",
    logo: withBasePath("/logos/github.svg"),
    transport: "stdio",
    command: "npx",
    args: ["-y", "@modelcontextprotocol/server-github"],
    envVars: [
      { key: "GITHUB_PERSONAL_ACCESS_TOKEN", placeholder: "ghp_...", description: "GitHub Personal Access Token" },
    ],
    notes: "Create a token at github.com/settings/tokens with repo, read:org, read:user scopes.",
  },
  {
    id: "slack",
    name: "Slack",
    description: "Channels, messages, users, reactions, and workspace management.",
    logo: withBasePath("/logos/slack.svg"),
    transport: "stdio",
    command: "npx",
    args: ["-y", "@modelcontextprotocol/server-slack"],
    envVars: [
      { key: "SLACK_BOT_TOKEN", placeholder: "xoxb-...", description: "Slack Bot Token" },
      { key: "SLACK_TEAM_ID", placeholder: "T0123456789", description: "Slack Team / Workspace ID" },
    ],
    notes: "Use a Bot Token from your Slack App (OAuth & Permissions > Bot User OAuth Token).",
  },
  {
    id: "notion",
    name: "Notion",
    description: "Pages, databases, blocks, and workspace content.",
    logo: withBasePath("/logos/notion.svg"),
    transport: "stdio",
    command: "npx",
    args: ["-y", "@notionhq/notion-mcp-server"],
    envVars: [
      { key: "NOTION_TOKEN", placeholder: "ntn_...", description: "Notion Integration Token" },
    ],
    notes: "Create an internal integration at notion.so/profile/integrations, then share pages/databases with it.",
  },
  {
    id: "google-drive",
    name: "Google Drive",
    description: "Search and access Google Drive files, docs, and spreadsheets.",
    logo: withBasePath("/logos/google.svg"),
    transport: "stdio",
    command: "npx",
    args: ["-y", "@modelcontextprotocol/server-gdrive"],
    envVars: [
      { key: "GDRIVE_CLIENT_ID", placeholder: "xxxx.apps.googleusercontent.com", description: "Google OAuth Client ID" },
      { key: "GDRIVE_CLIENT_SECRET", placeholder: "GOCSPX-...", description: "Google OAuth Client Secret" },
    ],
    notes: "Configure OAuth credentials in Google Cloud Console > APIs & Services > Credentials.",
  },
  {
    id: "google-maps",
    name: "Google Maps",
    description: "Geocoding, directions, places, and distance matrix.",
    logo: withBasePath("/logos/google.svg"),
    transport: "stdio",
    command: "npx",
    args: ["-y", "@modelcontextprotocol/server-google-maps"],
    envVars: [
      { key: "GOOGLE_MAPS_API_KEY", placeholder: "AIza...", description: "Google Maps API Key" },
    ],
    notes: "Get an API key from Google Cloud Console with Maps JavaScript API enabled.",
  },
  {
    id: "brave-search",
    name: "Brave Search",
    description: "Web search, local search, and news via Brave Search API.",
    iconName: "Search",
    transport: "stdio",
    command: "npx",
    args: ["-y", "@modelcontextprotocol/server-brave-search"],
    envVars: [
      { key: "BRAVE_API_KEY", placeholder: "BSA...", description: "Brave Search API Key" },
    ],
    notes: "Get a free API key at brave.com/search/api.",
  },
  {
    id: "filesystem",
    name: "Filesystem",
    description: "Read, write, and search local files and directories.",
    iconName: "HardDrive",
    transport: "stdio",
    command: "npx",
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/allowed/dir"],
    envVars: [],
    notes: "Replace '/path/to/allowed/dir' in the arguments with the directory you want to expose. You can add multiple paths.",
  },
  {
    id: "postgresql",
    name: "PostgreSQL",
    description: "Query databases, list schemas, tables, and run SQL.",
    iconName: "Database",
    transport: "stdio",
    command: "npx",
    args: ["-y", "@modelcontextprotocol/server-postgres"],
    envVars: [
      { key: "POSTGRES_CONNECTION_STRING", placeholder: "postgresql://user:pass@host:5432/db", description: "PostgreSQL connection URI" },
    ],
  },
  {
    id: "memory",
    name: "Memory",
    description: "Persistent memory with knowledge graph: entities, relations, and observations.",
    iconName: "Zap",
    transport: "stdio",
    command: "npx",
    args: ["-y", "@modelcontextprotocol/server-memory"],
    envVars: [],
    notes: "Stores a knowledge graph in a local JSON file. Useful for agents that need to remember things across sessions.",
  },
  {
    id: "puppeteer",
    name: "Puppeteer",
    description: "Browser automation: navigate, screenshot, click, fill forms, and extract content.",
    iconName: "Globe",
    transport: "stdio",
    command: "npx",
    args: ["-y", "@modelcontextprotocol/server-puppeteer"],
    envVars: [],
    notes: "Requires a Chromium-compatible browser installed on the host machine.",
  },
  {
    id: "everart",
    name: "EverArt",
    description: "AI image generation: create images from text descriptions.",
    iconName: "FileText",
    transport: "stdio",
    command: "npx",
    args: ["-y", "@modelcontextprotocol/server-everart"],
    envVars: [
      { key: "EVERART_API_KEY", placeholder: "ev-...", description: "EverArt API Key" },
    ],
  },
  {
    id: "sequential-thinking",
    name: "Sequential Thinking",
    description: "Dynamic problem-solving through thought sequences with branching and revision.",
    iconName: "MessageSquare",
    transport: "stdio",
    command: "npx",
    args: ["-y", "@modelcontextprotocol/server-sequential-thinking"],
    envVars: [],
    notes: "Enhances LLM reasoning by enabling step-by-step thinking with the ability to revise and branch.",
  },
  // -------------------------------------------------------------------------
  // Threat intelligence, detection & response
  //
  // These mirror the OpenCTI integrations catalogue (hub.filigran.io): where a
  // connector exists for a product, the matching MCP server is offered here so
  // an agent can query the same source live. Only servers published by the
  // vendor itself — or, where there is none, a self-hosted community server —
  // are listed: relay gateways that would receive the customer's vendor API
  // key are deliberately excluded. Community entries say so in their notes, so
  // nothing here reads as vendor-endorsed when it is not.
  // -------------------------------------------------------------------------
  {
    id: "virustotal",
    name: "VirusTotal",
    description: "File, URL, domain, and IP reputation reports plus sample submission.",
    logo: withBasePath("/logos/virustotal.svg"),
    transport: "streamable_http",
    url: "https://ai.virustotal.com/mcp",
    envVars: [],
    notes: "VirusTotal's hosted MCP server. Authenticate with OAuth after creating the server; a Premium/Enterprise account is required for most endpoints.",
  },
  {
    id: "recorded-future",
    name: "Recorded Future",
    description: "Risk Scores, threat actors, malware, ransomware, and dark web intelligence.",
    logo: withBasePath("/logos/recorded_future_mark.png"),
    transport: "streamable_http",
    url: "https://mcp.recordedfuture.com/mcp",
    envVars: [],
    notes: "Recorded Future's hosted MCP server. Authenticate with OAuth after creating the server; the subscription must include unlimited integrations, and every tool call counts against its daily API call limit. Some tools write back to Recorded Future (Watch Lists, Analyst Notes).",
  },
  {
    id: "crowdstrike-falcon",
    name: "CrowdStrike Falcon",
    description: "Detections, incidents, hosts, and Falcon threat intelligence.",
    logo: withBasePath("/logos/crowdstrike.svg"),
    transport: "stdio",
    command: "uvx",
    args: ["falcon-mcp"],
    envVars: [
      { key: "FALCON_CLIENT_ID", placeholder: "abcdef0123456789", description: "Falcon API client ID" },
      { key: "FALCON_CLIENT_SECRET", placeholder: "••••••••", description: "Falcon API client secret" },
      { key: "FALCON_BASE_URL", placeholder: "https://api.crowdstrike.com", description: "Falcon API region URL" },
    ],
    notes: "Create an API client under Support and resources > API clients and keys. Set FALCON_BASE_URL to your cloud region (us-1, us-2, eu-1, us-gov-1).",
  },
  {
    id: "greynoise",
    name: "GreyNoise",
    description: "Tell internet background noise from targeted attacks: IP reputation and RIOT.",
    logo: withBasePath("/logos/greynoise.svg"),
    transport: "stdio",
    command: "npx",
    args: ["-y", "@greynoise/greynoise-mcp-server"],
    envVars: [
      { key: "GREYNOISE_API_KEY", placeholder: "••••••••", description: "GreyNoise Enterprise API key" },
    ],
    notes: "Requires a GreyNoise Enterprise API key from viz.greynoise.io > Account > API Key.",
  },
  {
    id: "shodan",
    name: "Shodan",
    description: "Internet-wide host and device search, IP lookups, DNS, and CVE queries.",
    logo: withBasePath("/logos/shodan.png"),
    transport: "stdio",
    command: "npx",
    args: ["-y", "@burtthecoder/mcp-shodan"],
    envVars: [
      { key: "SHODAN_API_KEY", placeholder: "••••••••", description: "Shodan API key" },
    ],
    notes: "Community server (published by BurtTheCoder, not Shodan). Get your API key from account.shodan.io; covers both Shodan and Shodan InternetDB lookups.",
  },
  {
    id: "vulncheck",
    name: "VulnCheck",
    description: "Exploit and vulnerability intelligence: CVE research, exploits, and advisories.",
    logo: withBasePath("/logos/vulncheck.svg"),
    transport: "stdio",
    command: "docker",
    args: ["run", "-i", "--rm", "-e", "VULNCHECK_API_TOKEN", "ghcr.io/vulncheck-oss/mcp"],
    envVars: [
      { key: "VULNCHECK_API_TOKEN", placeholder: "vulncheck_...", description: "VulnCheck API token" },
    ],
    notes: "Create a token at vulncheck.com under your account settings.",
  },
  {
    id: "misp",
    name: "MISP",
    description: "Events, attributes, and indicators from a MISP threat-sharing instance.",
    logo: withBasePath("/logos/misp.svg"),
    transport: "stdio",
    command: "uvx",
    args: ["--from", "git+https://github.com/MISP/misp-mcp", "misp-mcp"],
    envVars: [
      { key: "MISP_URL", placeholder: "https://misp.example.com", description: "MISP instance URL" },
      { key: "MISP_API_KEY", placeholder: "••••••••", description: "MISP automation key" },
    ],
    notes: "MISP's official server is not on PyPI (checked: the name 404s), so it installs straight from Git. Append '@' and a commit hash to the repository URL to pin a revision. Find your automation key under Event Actions > Automation.",
  },
  {
    id: "thehive",
    name: "TheHive",
    description: "Cases, alerts, observables, and tasks from a TheHive instance.",
    logo: withBasePath("/logos/thehive.svg"),
    transport: "stdio",
    command: "docker",
    args: ["run", "-i", "--rm", "-e", "THEHIVE_URL", "-e", "THEHIVE_API_KEY", "ghcr.io/strangebeecorp/thehivemcp/thehivemcp:latest", "--transport", "stdio"],
    envVars: [
      { key: "THEHIVE_URL", placeholder: "https://thehive.example.com", description: "TheHive instance URL" },
      { key: "THEHIVE_API_KEY", placeholder: "••••••••", description: "TheHive API key" },
    ],
    notes: "StrangeBee's official server. Create an API key from your TheHive user profile.",
  },
  {
    id: "google-threat-intelligence",
    name: "Google Threat Intelligence",
    description: "GTI collections: threat actors, malware families, campaigns, and IOCs.",
    logo: withBasePath("/logos/gti.svg"),
    transport: "stdio",
    command: "uv",
    args: ["--directory", "/path/to/mcp-security/server/gti/gti_mcp", "run", "server.py"],
    envVars: [
      { key: "VT_APIKEY", placeholder: "••••••••", description: "Google Threat Intelligence / VirusTotal API key" },
    ],
    notes: "Clone github.com/google/mcp-security first, then point the --directory argument at your checkout. Requires uv on the host.",
  },
  {
    id: "google-secops",
    name: "Google SecOps",
    description: "Chronicle SecOps: search events, list alerts, and investigate detections.",
    logo: withBasePath("/logos/google_secops.svg"),
    transport: "stdio",
    command: "uv",
    args: ["--directory", "/path/to/mcp-security/server/secops/secops_mcp", "run", "server.py"],
    envVars: [
      { key: "CHRONICLE_PROJECT_ID", placeholder: "my-gcp-project", description: "Google Cloud project ID" },
      { key: "CHRONICLE_CUSTOMER_ID", placeholder: "01234567-abcd-4321-1234-0123456789ab", description: "Chronicle customer ID" },
      { key: "CHRONICLE_REGION", placeholder: "us", description: "Chronicle region (us, europe, asia-southeast1...)" },
    ],
    notes: "Clone github.com/google/mcp-security first, then point the --directory argument at your checkout. Authenticates through Application Default Credentials (gcloud auth application-default login).",
  },
  {
    id: "elasticsearch",
    name: "Elastic Security",
    description: "Search indices and query security data in an Elasticsearch cluster.",
    logo: withBasePath("/logos/elastic_security.svg"),
    transport: "stdio",
    command: "docker",
    args: ["run", "-i", "--rm", "-e", "ES_URL", "-e", "ES_API_KEY", "docker.elastic.co/mcp/elasticsearch", "stdio"],
    envVars: [
      { key: "ES_URL", placeholder: "https://my-cluster.es.example.com:9200", description: "Elasticsearch cluster URL" },
      { key: "ES_API_KEY", placeholder: "••••••••", description: "Elasticsearch API key" },
    ],
    notes: "Elastic's official server. Create an API key in Kibana under Stack Management > API keys.",
  },
  {
    id: "nist-nvd",
    name: "NIST NVD",
    description: "Search CVEs by keyword, severity, CWE, CPE, and CISA KEV status.",
    iconName: "Shield",
    transport: "stdio",
    command: "npx",
    args: ["-y", "@cyanheads/nist-nvd-mcp-server"],
    envVars: [
      { key: "NVD_API_KEY", placeholder: "••••••••", description: "NVD API key (optional, raises rate limits)" },
      { key: "MCP_TRANSPORT_TYPE", placeholder: "stdio", description: "Transport the server listens on" },
    ],
    notes: "Community server (published by cyanheads, not NIST). Works without a key at a lower rate limit: request one free at nvd.nist.gov/developers/request-an-api-key.",
  },
  {
    id: "cisa-kev",
    name: "CISA KEV",
    description: "Known Exploited Vulnerabilities catalogue with remediation deadlines.",
    iconName: "Crosshair",
    transport: "stdio",
    command: "uvx",
    args: ["cisa-kev-mcp"],
    envVars: [],
    notes: "Community server (published by CSOAI-ORG, not CISA). Reads the public CISA KEV feed: no credentials needed.",
  },
  {
    id: "mitre-attack",
    name: "MITRE ATT&CK",
    description: "Techniques, tactics, groups, software, and mitigations from ATT&CK.",
    logo: withBasePath("/logos/mitre_attack.svg"),
    transport: "stdio",
    command: "npx",
    args: ["-y", "@imouiche/mitre-attack-mcp-server"],
    envVars: [],
    notes: "Community server (published by imouiche, not MITRE). Queries the public ATT&CK knowledge base: no credentials needed.",
  },
  {
    id: "joe-sandbox",
    name: "Joe Sandbox",
    description: "Submit files and URLs for detonation and read back analysis reports.",
    iconName: "Search",
    transport: "stdio",
    command: "uv",
    args: ["--directory", "/path/to/joesandboxMCP", "run", "main.py"],
    envVars: [
      { key: "JBXAPIKEY", placeholder: "••••••••", description: "Joe Sandbox Cloud API key" },
      { key: "ACCEPTTAC", placeholder: "TRUE", description: "Set to TRUE to accept Joe Security's terms" },
    ],
    notes: "Clone github.com/joesecurity/joesandboxMCP first, then point --directory at your checkout. Setting ACCEPTTAC=TRUE confirms you accept the Joe Security terms and conditions.",
  },
  {
    id: "reversinglabs",
    name: "ReversingLabs",
    description: "Scan dependencies and files for malware, vulnerabilities, and policy issues.",
    iconName: "Radar",
    transport: "stdio",
    command: "docker",
    args: ["run", "--rm", "-i", "-e", "RL_TOKEN", "reversinglabs/rl-mcp-community:latest"],
    envVars: [
      { key: "RL_TOKEN", placeholder: "rlcmm-...", description: "Spectra Assure Community token" },
    ],
    notes: "Get a Community token at secure.software; enterprise tokens (rls3c-) use the enterprise image instead.",
  },
  {
    id: "vectra-ai",
    name: "Vectra AI",
    description: "Detections, entities, and prioritised threats from the Vectra platform.",
    iconName: "Radar",
    transport: "stdio",
    command: "uvx",
    args: ["vectra-ai-mcp-server"],
    envVars: [
      { key: "VECTRA_BASE_URL", placeholder: "https://123456789.ab1.portal.vectra.ai", description: "Vectra portal URL" },
      { key: "VECTRA_CLIENT_ID", placeholder: "••••••••", description: "Vectra API client ID" },
      { key: "VECTRA_CLIENT_SECRET", placeholder: "••••••••", description: "Vectra API client secret" },
    ],
    notes: "Create API credentials in the Vectra portal under Manage > API Clients.",
  },
  {
    id: "qualys",
    name: "Qualys",
    description: "VMDR findings, asset inventory, and compliance data from the Qualys portal.",
    logo: withBasePath("/logos/qualys.svg"),
    transport: "stdio",
    command: "uvx",
    args: ["qualys-mcp-full"],
    envVars: [
      { key: "QUALYS_USERNAME", placeholder: "qualys_user", description: "Qualys portal username" },
      { key: "QUALYS_PASSWORD", placeholder: "••••••••", description: "Qualys portal password" },
      { key: "QUALYS_PLATFORM_URL", placeholder: "https://qualysapi.qualys.com", description: "Qualys API platform URL" },
    ],
    notes: "Community server (published by rijul170, not Qualys). Set the platform URL to the API server for your subscription: it differs per platform, check Help > About in the portal.",
  },
  {
    id: "ipinfo",
    name: "IPinfo",
    description: "IP geolocation, ASN, carrier, privacy detection, and hosted-domain lookups.",
    iconName: "Globe",
    transport: "stdio",
    command: "uvx",
    args: ["ipinfo-mcp-server"],
    envVars: [
      { key: "IPINFO_TOKEN", placeholder: "••••••••", description: "IPinfo API token" },
    ],
    notes: "Get a free token at ipinfo.io/account/token.",
  },
  {
    id: "servicenow",
    name: "ServiceNow",
    description: "Incidents, changes, CMDB records, and SecOps tickets.",
    logo: withBasePath("/logos/servicenow.svg"),
    transport: "stdio",
    command: "npx",
    args: ["-y", "@aartiq/servicenow-mcp"],
    envVars: [
      { key: "SERVICENOW_INSTANCE_URL", placeholder: "https://acme.service-now.com", description: "ServiceNow instance URL" },
      { key: "SERVICENOW_BASIC_USERNAME", placeholder: "svc_account", description: "Integration user name" },
      { key: "SERVICENOW_BASIC_PASSWORD", placeholder: "••••••••", description: "Integration user password" },
    ],
    notes: "Community server (published by aartiq, not ServiceNow). Read-only by default: set WRITE_ENABLED=true as an extra variable only if the agent should create or update records.",
  },
  {
    id: "clickhouse",
    name: "ClickHouse",
    description: "Run SQL against ClickHouse clusters: explore schemas, tables, and data.",
    iconName: "Database",
    transport: "stdio",
    command: "uvx",
    args: ["mcp-clickhouse"],
    envVars: [
      { key: "CLICKHOUSE_HOST", placeholder: "clickhouse.example.com", description: "ClickHouse host" },
      { key: "CLICKHOUSE_USER", placeholder: "default", description: "ClickHouse user" },
      { key: "CLICKHOUSE_PASSWORD", placeholder: "••••••••", description: "ClickHouse password" },
    ],
    notes: "Read-only unless CLICKHOUSE_ALLOW_WRITE_ACCESS is set. Add CLICKHOUSE_DATABASE to scope it to one database.",
  },
  {
    id: "atlassian",
    name: "Atlassian Jira",
    description: "Jira issues and Confluence pages: search, create, and update work.",
    logo: withBasePath("/logos/jira.svg"),
    transport: "streamable_http",
    url: "https://mcp.atlassian.com/v2/mcp",
    envVars: [],
    notes: "Atlassian's hosted Rovo MCP server. Authenticate with OAuth after creating the server.",
  },
  {
    id: "datadog",
    name: "Datadog",
    description: "Query logs, metrics, traces, monitors, and incidents.",
    iconName: "Cloud",
    transport: "streamable_http",
    url: "https://mcp.datadoghq.com/v1/mcp",
    envVars: [],
    notes: "Datadog's managed server, shown for the US1 site. On another site swap the host: for example mcp.datadoghq.eu. Authenticate with OAuth after creating the server.",
  },
  {
    id: "cloudflare-radar",
    name: "Cloudflare Radar",
    description: "Internet traffic trends, attack activity, URL scanning, and domain rankings.",
    iconName: "Globe",
    transport: "streamable_http",
    url: "https://radar.mcp.cloudflare.com/mcp",
    envVars: [],
    notes: "One of Cloudflare's hosted servers. Authenticate with OAuth after creating the server.",
  },
  // -------------------------------------------------------------------------
  // SIEM, SOAR & ITSM
  //
  // The response side of the same OpenCTI catalogue: where a connector pushes
  // intel into a SIEM or opens a ticket, the matching MCP server lets an agent
  // read back what happened there.
  // -------------------------------------------------------------------------
  {
    id: "microsoft-sentinel",
    name: "Microsoft Sentinel",
    description: "Explore the Sentinel data lake: hunt across security logs and incidents.",
    logo: withBasePath("/logos/sentinel.svg"),
    transport: "streamable_http",
    url: "https://sentinel.microsoft.com/mcp/data-exploration",
    envVars: [],
    notes: "Microsoft's hosted server for the Sentinel data lake. Authenticate with OAuth after creating the server; the signed-in account needs read access to the workspace.",
  },
  {
    id: "google-secops-soar",
    name: "Google SecOps SOAR",
    description: "Cases, alerts, and playbook actions in Google SecOps SOAR (Siemplify).",
    logo: withBasePath("/logos/google_secops.svg"),
    transport: "stdio",
    command: "uv",
    args: ["--directory", "/path/to/mcp-security/server/secops-soar/secops_soar_mcp", "run", "server.py"],
    envVars: [
      { key: "SOAR_URL", placeholder: "https://yours.siemplify-soar.com:443", description: "SecOps SOAR instance URL" },
      { key: "SOAR_APP_KEY", placeholder: "01234567-abcd-4321-1234-0123456789ab", description: "SecOps SOAR application key" },
    ],
    notes: "Clone github.com/google/mcp-security first, then point the --directory argument at your checkout. Add '--integrations CSV,OKTA' to the arguments to expose only the integrations you need.",
  },
  {
    id: "sentinelone",
    name: "SentinelOne",
    description: "Purple AI: threats, alerts, and Deep Visibility hunting across the fleet.",
    logo: withBasePath("/logos/sentinelone.svg"),
    transport: "stdio",
    command: "uvx",
    args: ["--from", "git+https://github.com/Sentinel-One/purple-mcp.git", "purple-mcp", "--mode=stdio"],
    envVars: [
      { key: "PURPLEMCP_CONSOLE_TOKEN", placeholder: "••••••••", description: "SentinelOne console API token" },
      { key: "PURPLEMCP_CONSOLE_BASE_URL", placeholder: "https://your-console.sentinelone.net", description: "SentinelOne console URL" },
    ],
    notes: "SentinelOne's official Purple AI server, installed from Git. For production setups, pin a revision by appending '@' and a commit hash to the repository URL.",
  },
  {
    id: "splunk",
    name: "Splunk",
    description: "Run SPL searches and inspect indexes, saved searches, and Splunk health.",
    logo: withBasePath("/logos/splunk.svg"),
    transport: "stdio",
    command: "uv",
    args: ["run", "--directory", "/path/to/mcp-for-splunk", "fastmcp", "run", "src/server.py"],
    envVars: [
      { key: "MCP_SPLUNK_HOST", placeholder: "splunk.example.com", description: "Splunk host" },
      { key: "MCP_SPLUNK_USERNAME", placeholder: "svc_account", description: "Splunk username" },
      { key: "MCP_SPLUNK_PASSWORD", placeholder: "••••••••", description: "Splunk password or token" },
    ],
    notes: "Community server (published by deslicer, not Splunk): clone github.com/deslicer/mcp-for-splunk and run 'uv sync' in it, then point --directory at the checkout. Running through 'uv run' is what puts fastmcp on the path. Splunk's own MCP server ships as a Splunkbase app instead and cannot be launched this way.",
  },
  {
    id: "pagerduty",
    name: "PagerDuty",
    description: "Incidents, on-call schedules, escalation policies, and services.",
    iconName: "Bell",
    transport: "stdio",
    command: "uvx",
    args: ["pagerduty-mcp"],
    envVars: [
      { key: "PAGERDUTY_USER_API_KEY", placeholder: "••••••••", description: "PagerDuty user API token" },
    ],
    notes: "PagerDuty's official server. Create a user API token under My Profile > User Settings. Set PAGERDUTY_API_HOST for the EU service region.",
  },
];
