// SPDX-License-Identifier: Apache-2.0

/**
 * Settings. Sub-entries mirror the page's 7 sections and their sub-sections (#9173).
 */
import { Settings } from "lucide-react";
import { defineCommands } from "@/lib/commands/types";

export default defineCommands([
  {
    id: "settings.open",
    category: "Settings",
    title: "Open settings",
    icon: Settings,
    when: ["isAdmin"],
    children: [
      { id: "settings.organization", category: "Settings", title: "Organization", navigate: "/settings/organization" },
      { id: "settings.departments", category: "Settings", title: "Departments", navigate: "/settings/organization/departments" },
      { id: "settings.automations", category: "Settings", title: "Agents and automations", navigate: "/settings/automations" },
      { id: "settings.agentRuns", category: "Settings", title: "Agent runs", navigate: "/settings/automations/agent-runs" },
      { id: "settings.parameters", category: "Settings", title: "Platform", keywords: ["parameters", "licence", "branding", "language"], navigate: "/settings/platform" },
      { id: "settings.models", category: "Settings", title: "AI Models", navigate: "/settings/ai/models" },
      { id: "settings.assistant", category: "Settings", title: "General Assistant", navigate: "/settings/ai/assistant" },
      { id: "settings.channels", category: "Settings", title: "Channels & Bots", navigate: "/settings/integrations/channels" },
      { id: "settings.integrations", category: "Settings", title: "Integrations", navigate: "/settings/integrations" },
      { id: "settings.quotas", category: "Settings", title: "Quotas", navigate: "/settings/usage/quotas" },
      { id: "settings.mcp", category: "Settings", title: "MCP Endpoint", navigate: "/settings/integrations/mcp" },
      { id: "settings.security", category: "Settings", title: "Security", navigate: "/settings/security" },
      {
        id: "settings.securityAudit",
        category: "Settings",
        title: "Security audit",
        keywords: ["audit", "posture", "findings", "hardening", "vulnerabilities"],
        navigate: "/settings/security",
      },
      {
        id: "settings.securityOptions",
        category: "Settings",
        title: "Security options",
        keywords: ["proxy", "fail mode", "password", "webhook", "sandbox", "environment variables"],
        navigate: "/settings/security/options",
      },
      {
        id: "settings.securityToolApprovals",
        category: "Settings",
        title: "Tool approvals",
        keywords: ["hitl", "human in the loop", "approval", "kill switch", "ask before running"],
        navigate: "/settings/security/approvals",
      },
      {
        id: "settings.securityAuthentication",
        category: "Settings",
        title: "Authentication",
        keywords: ["sso", "oidc", "saml", "sign-in", "login"],
        navigate: "/settings/security/authentication",
      },
      {
        id: "settings.securityVaults",
        category: "Settings",
        title: "Secret vaults",
        keywords: ["vault", "hashicorp", "aws", "azure", "gcp", "1password", "secrets manager", "key vault"],
        navigate: "/settings/security/vaults",
      },
      {
        id: "settings.securityApiKeys",
        category: "Settings",
        title: "API keys",
        keywords: ["api key", "token", "revoke", "expiry", "scopes"],
        navigate: "/settings/security/api-keys",
      },
      {
        id: "settings.securityEmergencyStop",
        category: "Settings",
        title: "Emergency stop",
        keywords: ["panic", "emergency", "token", "procedure", "kill switch"],
        navigate: "/settings/security/emergency",
      },
      {
        id: "settings.securityMemory",
        category: "Settings",
        title: "Memory purge",
        keywords: ["memories", "purge", "reset"],
        navigate: "/settings/security/memory",
      },
      { id: "settings.observability", category: "Settings", title: "Observability", navigate: "/settings/usage/observability" },
      { id: "settings.pricing", category: "Settings", title: "Model pricing", navigate: "/settings/usage/pricing" },
      { id: "settings.quality", category: "Settings", title: "Quality", navigate: "/settings/automations/quality" },
    ],
  },
]);
