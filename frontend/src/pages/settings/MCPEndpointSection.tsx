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

import { useState, useMemo, useRef } from "react";
import {
  Key,
  CheckCircle2,
  Copy,
  Check,
  Trash2,
  Server,
  Globe,
  X,
  ExternalLink,
  Info,
  Building2,
} from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { SearchInput } from "@/components/ui/search-input";
import {
  PlatformMCPServerList,
  usePlatformMCPServerList,
} from "@/components/ui/platform-mcp-server-list";
import { XtmMcpPackageDownload } from "@/components/ui/xtm-mcp-package-download";
import {
  useGeneralAssistantConfig,
  useUpdateGeneralAssistantConfig,
  useGenerateMCPKey,
  useRevokeMCPKey,
} from "@/hooks/api/useSettingsApi";
import { usePlatformAgents } from "@/hooks/api/useAgentApi";
import { useAuthConfig } from "@/hooks/api/useAuthApi";
import { cn } from "@/lib/utils";
import { useCredentialSharingConfirm } from "@/hooks/useCredentialSharingConfirm";
import { useClickOutside } from "@/hooks/useClickOutside";
import { COPY_FEEDBACK_DELAY, ICON_MAP } from "@/lib/constants";
import { useIsCommunityEdition } from "@/hooks/usePlatformConfig";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { AiDisabledNotice } from "@/components/ui/ai-disabled";
import { Spinner } from "@filigran/design-system";

// ---------------------------------------------------------------------------
// MCP Endpoint Configuration Section (admin-only)
// ---------------------------------------------------------------------------
export function MCPEndpointSection() {
  const { t_i18n } = useFormatter();
  const { data: config } = useGeneralAssistantConfig();
  const { data: authConfig } = useAuthConfig();
  const updateConfig = useUpdateGeneralAssistantConfig();
  const generateKey = useGenerateMCPKey();
  /* Gated per control rather than by a blanket <fieldset disabled>: the four
     copy buttons here are how an administrator *reads* the endpoint config, and
     FR-022 wants the section readable, not merely visible. */
  const aiOff = useAiActionDisabled();
  const revokeKey = useRevokeMCPKey();
  const { data: platformAgents } = usePlatformAgents();
  // CE-only (no Filigran license, no EE platform) — every edit on this page
  // is locked because the MCP endpoint is server-side disabled until a
  // license or registered EE platform arrives. Keep everything visible
  // (read-only) so admins can still inspect the URL and existing
  // selection, but no write surface is reachable.
  const isCeOnly = useIsCommunityEdition();
  const ceOnlyTooltip = t_i18n("Activate a license or register an EE platform to manage the MCP endpoint.");

  const [generatedKey, setGeneratedKey] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [copiedUrl, setCopiedUrl] = useState<string | null>(null);
  const [copiedSnippet, setCopiedSnippet] = useState<string | null>(null);
  const [agentSearch, setAgentSearch] = useState("");
  const [pickerOpen, setPickerOpen] = useState(false);
  const [ConsentEl, confirmSharing] = useCredentialSharingConfirm();

  const pickerRef = useRef<HTMLDivElement>(null);
  const searchInputRef = useRef<HTMLInputElement>(null);
  useClickOutside(pickerRef, () => { setPickerOpen(false); setAgentSearch(""); }, pickerOpen);

  const baseUrl = authConfig?.base_url ?? "";
  const globalUrl = `${baseUrl}/mcp/global`;

  // --- Platform MCP servers (OpenCTI / OpenAEV / OpenCRQ) ---
  // Shared descriptor + mode-aware visibility: XTM One mode always lists all
  // servers (discoverability, with skeleton rows while loading); Copilot mode
  // only lists connected ones, and the whole card disappears when none is
  // connected (no dead weight). Exception: when the admin toggle is OFF the
  // card always renders — hiding it would also hide the Switch and leave no
  // way to re-enable the servers from Settings.
  const {
    servers: platformServers,
    connectedCount,
    isLoading: platformServersLoading,
    isXtmOne,
  } = usePlatformMCPServerList();
  const platformMcpEnabled = config?.platform_mcp_enabled ?? true;
  const showPlatformCard =
    isXtmOne || platformServers.length > 0 || !platformMcpEnabled;

  const handleTogglePlatformMcp = (enabled: boolean) => {
    updateConfig.mutate({ platform_mcp_enabled: enabled });
  };

  // --- XTM One's own MCP server (/mcp/xtm) ---
  // Independent of the platform servers: it exposes THIS instance's own
  // surface, so it has its own toggle and defaults to off.
  const xtmUrl = `${baseUrl}/mcp/xtm`;
  const xtmMcpEnabled = config?.xtm_mcp_enabled ?? false;

  const handleToggleXtmMcp = (enabled: boolean) => {
    updateConfig.mutate({ xtm_mcp_enabled: enabled });
  };

  const exposedIds = useMemo(() => config?.mcp_exposed_agent_ids ?? [], [config?.mcp_exposed_agent_ids]);
  const keyPrefix = config?.mcp_global_api_key_prefix;

  // All platform agents eligible for MCP exposure (enabled, not chat-disabled, has slug)
  const eligibleAgents = useMemo(
    () => (platformAgents ?? []).filter((a) => a.enabled !== false && !a.disable_chat && a.slug),
    [platformAgents],
  );

  // --- Global endpoint ---
  const exposedIdSet = useMemo(() => new Set(exposedIds), [exposedIds]);
  const exposedAgents = useMemo(
    () => eligibleAgents.filter((a) => exposedIdSet.has(a.id)),
    [eligibleAgents, exposedIdSet],
  );
  const filteredAvailable = useMemo(() => {
    const available = eligibleAgents.filter((a) => !exposedIdSet.has(a.id));
    if (!agentSearch) return available;
    const q = agentSearch.toLowerCase();
    return available.filter(
      (a) =>
        a.name.toLowerCase().includes(q) ||
        (a.slug ?? "").toLowerCase().includes(q),
    );
  }, [eligibleAgents, exposedIdSet, agentSearch]);

  // Whoever holds the key acts with the agents it exposes, and their bindings.
  const handleGenerateKey = async () => {
    if (!(await confirmSharing({ entity_type: "platform_mcp", change: { has_mcp_key: true } }, { confirmLabel: t_i18n("Generate") }))) return;
    generateKey.mutate(undefined, {
      onSuccess: (data) => {
        setGeneratedKey(data.key);
      },
    });
  };

  const handleRevokeKey = async () => {
    const ok = await confirmSharing(
      { entity_type: "platform_mcp", change: { has_mcp_key: false } },
      {
        mode: "unshare",
        confirmLabel: t_i18n("Revoke Key"),
        fallback: {
          title: "Revoke MCP API Key",
          description:
            "This will immediately invalidate the current global MCP API key. Any clients using this key will stop working.",
        },
      },
    );
    if (ok) {
      revokeKey.mutate(undefined, {
        onSuccess: () => setGeneratedKey(null),
      });
    }
  };

  const handleCopyKey = () => {
    if (generatedKey) {
      navigator.clipboard.writeText(generatedKey);
      setCopied(true);
      setTimeout(() => setCopied(false), COPY_FEEDBACK_DELAY);
    }
  };

  const handleCopyUrl = (url: string, label: string) => {
    navigator.clipboard.writeText(url);
    setCopiedUrl(label);
    setTimeout(() => setCopiedUrl(null), COPY_FEEDBACK_DELAY);
  };

  const handleAddAgent = async (agentId: string) => {
    // Defense-in-depth: although the search input is disabled when CE-only
    // (and the per-row remove buttons further down are also disabled), the
    // option <button>s inside the picker dropdown have no native disabled
    // state. If `pickerOpen` is true at the moment the tier flips to
    // CE-only — or any other state path leaves it open — a click would
    // otherwise still mutate config. Block at the handler so no mutation
    // can fire in the locked state regardless of UI state.
    if (isCeOnly) return;
    const newIds = [...exposedIds, agentId];
    setAgentSearch("");
    if (!(await confirmSharing({ entity_type: "platform_mcp", change: { mcp_exposed_agent_ids: newIds } }, { confirmLabel: t_i18n("Expose") }))) return;
    updateConfig.mutate({ mcp_exposed_agent_ids: newIds });
    searchInputRef.current?.focus();
  };

  const handleRemoveAgent = async (agentId: string) => {
    if (isCeOnly) return;
    const newIds = exposedIds.filter((id) => id !== agentId);
    if (!(await confirmSharing({ entity_type: "platform_mcp", change: { mcp_exposed_agent_ids: newIds } }, { mode: "unshare" }))) return;
    updateConfig.mutate({ mcp_exposed_agent_ids: newIds });
  };

  // Connection snippet (same JSON for both Cursor and Claude Desktop)
  const connectionSnippet = JSON.stringify(
    {
      mcpServers: {
        "xtm-one-agents": {
          url: globalUrl,
          headers: {
            Authorization: `Bearer ${keyPrefix ? "<your-mcp-key>" : "<generate-a-key-first>"}`,
          },
        },
      },
    },
    null,
    2,
  );

  return (
    <div className="space-y-6">
      <AiDisabledNotice />
      {ConsentEl}

      {/* ── Platform MCP Servers (OpenCTI / OpenAEV / OpenCRQ) ─────────── */}
      {showPlatformCard && (
        <Card>
          <CardContent className="pt-6 space-y-5">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Server className="h-4 w-4 text-accent" />
                <p className="content-base font-medium">{t_i18n("Platform MCP Servers")}</p>
                <Badge variant="secondary" className="content-caption">{t_i18n("Community")}</Badge>
                {connectedCount > 0 && (
                  <Badge variant="success" className="content-caption">
                    {t_i18n("{count} connected", { count: connectedCount })}
                  </Badge>
                )}
              </div>
              <Tip content={platformMcpEnabled ? t_i18n("Disable the platform MCP servers for all users") : t_i18n("Enable the platform MCP servers")}>
                <div>
                  <Switch aria-label={t_i18n("Platform MCP Servers")}
                    checked={platformMcpEnabled}
                    onCheckedChange={handleTogglePlatformMcp}
                    disabled={aiOff}
                  />
                </div>
              </Tip>
            </div>

            <p className="content-compact text-default-secondary leading-relaxed">
              {t_i18n("Native MCP servers for connected OpenCTI, OpenAEV, and OpenCRQ platforms. Each server is available as soon as the platform is registered with XTM One and exposes only business and knowledge tools (no platform administration). Users authenticate with their personal API key")}
              {" "}(<code className="bg-elevation-hover px-1 py-0.5 rounded-sm content-caption">fcp-...</code>){" "}
              {t_i18n("or OAuth 2.1: every call runs with the caller's own identity on the platform. Users find their endpoints and client configuration in")} <strong>{t_i18n("My Profile > MCP Endpoint")}</strong>.
            </p>

            {!platformMcpEnabled && (
              <div className="rounded-sm bg-feedback-alert-primary/5 p-3">
                <p className="content-compact text-feedback-warning-primary">
                  {t_i18n("Platform MCP servers are disabled. All platform MCP endpoints return 403 until re-enabled.")}
                </p>
              </div>
            )}

            <PlatformMCPServerList
              servers={platformServers}
              disabled={!platformMcpEnabled}
              isLoading={platformServersLoading}
            />

            <div className="rounded-sm bg-accent/5 p-3 flex items-start gap-2">
              <Info className="h-3.5 w-3.5 text-accent mt-0.5 shrink-0" />
              <p className="content-compact text-default-secondary">
                {t_i18n("A server responds 404 while its platform is not registered. Registration happens automatically when an OpenCTI, OpenAEV, or OpenCRQ instance is connected to XTM One.")}
              </p>
            </div>
          </CardContent>
        </Card>
      )}

      {/* ── XTM One MCP Server (/mcp/xtm) ──────────────────────────────── */}
      <Card>
        <CardContent className="pt-6 space-y-5">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <Building2 className="h-4 w-4 text-accent" />
              <p className="content-base font-medium">{t_i18n("Platform Tools MCP Server")}</p>
              <Badge variant={xtmMcpEnabled ? "success" : "outline"} className="content-caption">
                {xtmMcpEnabled ? t_i18n("Enabled") : t_i18n("Disabled")}
              </Badge>
            </div>
            <Tip content={xtmMcpEnabled ? t_i18n("Disable the platform tools MCP server for all users") : t_i18n("Enable the platform tools MCP server")}>
              <div>
                <Switch
                  aria-label={t_i18n("Platform Tools MCP Server")}
                  checked={xtmMcpEnabled}
                  onCheckedChange={handleToggleXtmMcp}
                  disabled={aiOff || isCeOnly}
                />
              </div>
            </Tip>
          </div>

          <p className="content-compact text-default-secondary leading-relaxed">
            {t_i18n("Exposes this platform's own tools over MCP: agent traces, agents, assignments, runs, knowledge bases, prompts, skills, custom tools, integrations and variables. External clients (Claude Desktop, Cursor, ...) authenticate with a personal API key")}
            {" "}(<code className="bg-elevation-hover px-1 py-0.5 rounded-sm content-caption">fcp-...</code>){" "}
            {t_i18n("or OAuth 2.1, and see exactly what that user can see and manage in the UI: nothing more.")}
          </p>

          <div className="space-y-1.5">
            <label className="content-compact font-medium text-default-secondary">{t_i18n("Endpoint URL")}</label>
            <div className="flex items-center gap-2">
              <code className="flex-1 rounded-sm bg-elevation-default px-3 py-2 content-compact font-mono-plex select-all">
                {xtmUrl}
              </code>
              <Button
                variant="ghost"
                size="sm"
                className="shrink-0 gap-1.5"
                aria-label={t_i18n("Copy the platform tools MCP endpoint URL")}
                onClick={() => handleCopyUrl(xtmUrl, "xtm")}
              >
                {copiedUrl === "xtm" ? (
                  <Check className="h-3.5 w-3.5 text-feedback-success-primary" />
                ) : (
                  <Copy className="h-3.5 w-3.5" />
                )}
              </Button>
            </div>
          </div>

          {xtmMcpEnabled ? (
            <>
              <div className="rounded-sm bg-feedback-alert-primary/5 p-3 flex items-start gap-2">
                <Info className="h-3.5 w-3.5 text-feedback-warning-primary mt-0.5 shrink-0" />
                <p className="content-compact text-feedback-warning-primary">
                  {t_i18n("This server can create and modify platform objects, within each caller's own permissions. Revoke a user's API keys to cut their access.")}
                </p>
              </div>
              <div className="space-y-2 border-t border-elevation-subtle-soft pt-4">
                <label className="content-compact font-medium text-default-secondary">
                  {t_i18n("Install in your client")}
                </label>
                {/* The same packages users get, so an administrator reviewing
                    what is handed out is looking at the real thing. */}
                {/* Read-only modes disable it too: this page is otherwise
                    locked with AI off or on a CE-only tier, and a download
                    that mints an API key is the least appropriate thing to
                    leave live behind a disabled toggle. */}
                <XtmMcpPackageDownload disabled={aiOff || isCeOnly} />
              </div>
            </>
          ) : (
            <div className="rounded-sm bg-elevation-hover p-3">
              <p className="content-compact text-default-secondary">
                {t_i18n("Disabled: the endpoint returns 403 until it is enabled.")}
              </p>
            </div>
          )}
        </CardContent>
      </Card>

      {/* ── Global MCP Endpoint ────────────────────────────────────────── */}
      <Card>
        <CardContent className="pt-6 space-y-5">
          <div className="flex items-center gap-2">
            <Globe className="h-4 w-4 text-accent" />
            <p className="content-base font-medium">{t_i18n("Global MCP Endpoint")}</p>
          </div>

          <p className="content-compact text-default-secondary leading-relaxed">
            {t_i18n("Expose selected agents as MCP tools via a platform-wide endpoint. External AI systems (Cursor, Claude Desktop, Jira AI, etc.) can connect using the global API key and call your agents as tools.")}
          </p>

          {/* Endpoint URL */}
          <div className="space-y-1.5">
            <label className="content-compact font-medium text-default-secondary">{t_i18n("Endpoint URL")}</label>
            <div className="flex items-center gap-2">
              <code className="flex-1 rounded-sm bg-elevation-default px-3 py-2 content-compact font-mono-plex select-all">
                {globalUrl}
              </code>
              <Button
                variant="ghost"
                size="sm"
                className="shrink-0 gap-1.5"
                onClick={() => handleCopyUrl(globalUrl, "global")}
              >
                {copiedUrl === "global" ? (
                  <Check className="h-3.5 w-3.5 text-feedback-success-primary" />
                ) : (
                  <Copy className="h-3.5 w-3.5" />
                )}
              </Button>
            </div>
          </div>

          {/* API Key */}
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Key className="h-3.5 w-3.5 text-default-secondary" />
                <label className="content-compact font-medium text-default-secondary">{t_i18n("API key")}</label>
              </div>
              {keyPrefix ? (
                <Badge variant="success" className="content-caption">
                  <CheckCircle2 className="h-3 w-3 mr-1" /> {t_i18n("Configured")}
                </Badge>
              ) : (
                <Badge variant="outline" className="content-caption">{t_i18n("Not configured")}</Badge>
              )}
            </div>

            {/* One-time key reveal */}
            {generatedKey && (
              <div className="rounded-sm bg-feedback-success-primary/10 border border-feedback-success-primary/20 p-4 space-y-2">
                <p className="content-compact text-feedback-success-primary font-medium">
                  {t_i18n("MCP API key generated: copy it now, it won't be shown again.")}
                </p>
                <div className="flex items-center gap-2">
                  <code className="flex-1 content-compact bg-elevation-hover px-3 py-2 rounded-sm font-mono-plex break-all">
                    {generatedKey}
                  </code>
                  <Button variant="outline" size="sm" className="shrink-0 gap-1.5" onClick={handleCopyKey}>
                    {copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
                    {copied ? t_i18n("Copied") : t_i18n("Copy")}
                  </Button>
                </div>
              </div>
            )}

            {/* Key prefix display */}
            {keyPrefix && !generatedKey && (
              <div className="flex items-center justify-between rounded-sm bg-elevation-default p-3">
                <div>
                  <p className="content-caption text-default-secondary">{t_i18n("Current key prefix")}</p>
                  <code className="content-base font-mono-plex">{keyPrefix}</code>
                </div>
              </div>
            )}

            <div className="flex gap-2">
              <Tip content={isCeOnly ? ceOnlyTooltip : ""} wrap={isCeOnly}>
                <Button
                  variant={keyPrefix ? "outline" : "default"}
                  size="sm"
                  className="gap-1.5"
                  onClick={handleGenerateKey}
                  disabled={isCeOnly || generateKey.isPending || aiOff} aria-busy={generateKey.isPending || undefined}>
                  {generateKey.isPending && <Spinner size="sm" tone="inherit" />}{!generateKey.isPending && <Key className="h-3 w-3" />}
                  {keyPrefix ? t_i18n("Regenerate Key") : t_i18n("Generate Key")}
                </Button>
              </Tip>
              {keyPrefix && (
                <Tip content={isCeOnly ? ceOnlyTooltip : ""} wrap={isCeOnly}>
                  <Button
                    variant="ghost"
                    size="sm"
                    className="gap-1.5 text-feedback-error-primary hover:text-feedback-error-primary"
                    onClick={handleRevokeKey}
                    disabled={isCeOnly || revokeKey.isPending || aiOff} aria-busy={revokeKey.isPending || undefined}>
                    {revokeKey.isPending && <Spinner size="sm" tone="inherit" />}{!revokeKey.isPending && <Trash2 className="h-3 w-3" />}
                    {t_i18n("Revoke")}
                  </Button>
                </Tip>
              )}
            </div>
          </div>

          {/* Agent selection */}
          <div className="space-y-3">
            <div className="flex items-center gap-2">
              <Server className="h-3.5 w-3.5 text-default-secondary" />
              <label className="content-compact font-medium text-default-secondary">{t_i18n("Exposed Agents")}</label>
              <Badge variant="secondary" className="content-caption">
                {t_i18n("{count} selected", { count: exposedAgents.length })}
              </Badge>
            </div>
            <p className="content-compact text-default-secondary">
              {t_i18n("Select which agents are available as MCP tools on the global endpoint. This list includes all agents across the platform.")}
            </p>

            {/* Autocomplete agent picker */}
            <div className="relative" ref={pickerRef}>
              <Tip content={isCeOnly ? ceOnlyTooltip : ""}>
                <div>
                  <SearchInput
                    inputRef={searchInputRef}
                    placeholder={isCeOnly ? t_i18n("MCP endpoint is disabled on this tier") : t_i18n("Search agents to add...")}
                    value={agentSearch}
                    onChange={(value) => { setAgentSearch(value); setPickerOpen(true); }}
                    onFocus={() => { if (!isCeOnly) setPickerOpen(true); }}
                    disabled={isCeOnly || aiOff}
                  />
                </div>
              </Tip>

              {pickerOpen && !isCeOnly && (
                <div className="absolute z-50 mt-1 w-full rounded-sm border border-elevation-subtle layer-2 bg-elevation-default shadow-overlay max-h-52 overflow-y-auto">
                  {filteredAvailable.length > 0 ? (
                    filteredAvailable.map((agent) => {
                      const IconComp = ICON_MAP[agent.icon as keyof typeof ICON_MAP] ?? ICON_MAP.Bot;
                      return (
                        <button
                          key={agent.id}
                          className="flex items-center gap-2.5 w-full px-3 py-2 text-left content-compact transition-colors hover:bg-elevation-hover"
                          onClick={() => handleAddAgent(agent.id)}
                        >
                          <IconComp className="h-3.5 w-3.5 text-default-secondary shrink-0" />
                          <span className="truncate flex-1">{agent.name}</span>
                          {agent.is_company_managed && (
                            <Building2 className="h-3 w-3 text-feedback-warning-primary shrink-0" />
                          )}
                          {agent.slug && (
                            <code className="content-caption text-default-secondary font-mono-plex shrink-0">
                              {agent.slug}
                            </code>
                          )}
                        </button>
                      );
                    })
                  ) : (
                    <p className="px-3 py-3 content-compact text-default-secondary text-center">
                      {agentSearch ? t_i18n("No matching agents.") : t_i18n("All eligible agents are already exposed.")}
                    </p>
                  )}
                </div>
              )}
            </div>

            {/* Current exposed agents */}
            {exposedAgents.length === 0 ? (
              <div className="rounded-sm border border-dashed border-elevation-subtle-soft p-6 text-center">
                <Server className="h-6 w-6 text-default-secondary mx-auto mb-2" />
                <p className="content-compact text-default-secondary">
                  {t_i18n("No agents exposed yet. Search and add agents above to make them available as MCP tools.")}
                </p>
              </div>
            ) : (
              <div className="space-y-1">
                {exposedAgents.map((agent) => {
                  const IconComp = ICON_MAP[agent.icon as keyof typeof ICON_MAP] ?? ICON_MAP.Bot;
                  return (
                    <div
                      key={agent.id}
                      className={cn(
                        "flex items-center gap-3 rounded-sm bg-elevation-default px-4 py-2.5",
                        "group hover:bg-elevation-hover transition-colors",
                      )}
                    >
                      <IconComp className="h-4 w-4 text-accent shrink-0" />
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2">
                          <p className="content-base font-medium truncate">{agent.name}</p>
                          {agent.is_company_managed && (
                            <Building2 className="h-3 w-3 text-feedback-warning-primary shrink-0" />
                          )}
                        </div>
                      </div>
                      {agent.slug && (
                        <code className="content-caption text-default-secondary shrink-0">{agent.slug}</code>
                      )}
                      <Tip content={isCeOnly ? ceOnlyTooltip : ""} wrap={isCeOnly}>
                        <Button aria-label={t_i18n("Remove agent")}
                          variant="ghost"
                          size="sm"
                          className="h-7 w-7 p-0 opacity-0 group-hover:opacity-100 text-default-secondary hover:text-feedback-error-primary transition-all"
                          onClick={() => handleRemoveAgent(agent.id)}
                          disabled={isCeOnly || aiOff}
                        >
                          <X className="h-3.5 w-3.5" />
                        </Button>
                      </Tip>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </CardContent>
      </Card>

      {/* ── Connection Instructions ─────────────────────────────────────── */}
      <Card>
        <CardContent className="pt-6 space-y-5">
          <div className="flex items-center gap-2">
            <ExternalLink className="h-4 w-4 text-accent" />
            <p className="content-base font-medium">{t_i18n("Connection Instructions")}</p>
          </div>

          <p className="content-compact text-default-secondary leading-relaxed">
            {t_i18n("Add the following configuration to your MCP client. Both endpoints use MCP Streamable HTTP transport (POST with JSON-RPC 2.0).")}
          </p>

          {/* Cursor */}
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <h4 className="content-compact font-medium text-default-secondary">
                Cursor (<code className="content-caption">.cursor/mcp.json</code>)
              </h4>
              <Button
                variant="ghost"
                size="sm"
                className="gap-1.5 h-6 content-caption"
                onClick={() => {
                  navigator.clipboard.writeText(connectionSnippet);
                  setCopiedSnippet("cursor");
                  setTimeout(() => setCopiedSnippet(null), COPY_FEEDBACK_DELAY);
                }}
              >
                {copiedSnippet === "cursor" ? (
                  <Check className="h-3 w-3 text-feedback-success-primary" />
                ) : (
                  <Copy className="h-3 w-3" />
                )}
                {t_i18n("Copy")}
              </Button>
            </div>
            <pre className="rounded-sm bg-elevation-default p-3 content-caption font-mono-plex overflow-x-auto whitespace-pre">
              {connectionSnippet}
            </pre>
          </div>

          {/* Claude Desktop */}
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <h4 className="content-compact font-medium text-default-secondary">
                Claude Desktop (<code className="content-caption">claude_desktop_config.json</code>)
              </h4>
              <Button
                variant="ghost"
                size="sm"
                className="gap-1.5 h-6 content-caption"
                onClick={() => {
                  navigator.clipboard.writeText(connectionSnippet);
                  setCopiedSnippet("claude");
                  setTimeout(() => setCopiedSnippet(null), COPY_FEEDBACK_DELAY);
                }}
              >
                {copiedSnippet === "claude" ? (
                  <Check className="h-3 w-3 text-feedback-success-primary" />
                ) : (
                  <Copy className="h-3 w-3" />
                )}
                {t_i18n("Copy")}
              </Button>
            </div>
            <pre className="rounded-sm bg-elevation-default p-3 content-caption font-mono-plex overflow-x-auto whitespace-pre">
              {connectionSnippet}
            </pre>
          </div>

          <div className="rounded-sm bg-accent/5 p-3 flex items-start gap-2">
            <Info className="h-3.5 w-3.5 text-accent mt-0.5 shrink-0" />
            <div className="content-compact text-default-secondary space-y-1">
              <p>
                <strong>{t_i18n("Global endpoint")}</strong> {t_i18n("uses a platform-wide")} <code className="bg-elevation-hover px-1 py-0.5 rounded-sm content-caption">mcp-...</code> {t_i18n("key and exposes admin-selected agents.")}
              </p>
              <p>
                <strong>{t_i18n("User endpoint")}</strong> {t_i18n("uses a personal")} <code className="bg-elevation-hover px-1 py-0.5 rounded-sm content-caption">fcp-...</code> {t_i18n("key and exposes agents visible to that user. Replace the URL with")}{" "}
                <code className="bg-elevation-hover px-1 py-0.5 rounded-sm content-caption">/mcp/user</code> {t_i18n("and the key with a personal API key.")}
              </p>
            </div>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
