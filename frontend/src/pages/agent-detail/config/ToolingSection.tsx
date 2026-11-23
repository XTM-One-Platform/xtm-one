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

import { useEffect, useMemo, useRef, useState } from "react";
import {
  Cog, Terminal, Sparkles, Wrench, GraduationCap,
  Building2, CheckCircle2, XCircle, Plus, Hash, Filter, Users,
  ChevronRight, AlertTriangle, Lock, ShieldAlert,
  ListTodo, Bot, Activity, Undo2 } from "lucide-react";
import { ProviderLogo } from "@/components/ui/provider-logo";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge, BadgeFocusTrigger } from "@/components/ui/badge";
import { CountBadge } from "@/components/ui/count-badge";
import { Switch } from "@/components/ui/switch";
import { DisclosureSummary } from "@/components/ui/disclosure";
import { CardEditActions } from "@/components/ui/card-edit-actions";
import { SearchInput } from "@/components/ui/search-input";
import { ElementTypeEditor } from "@/components/ui/element-type-editor";
import { ScopeEnforcementBadge, ScopeEnforcementSwitch } from "@/components/ui/scope-enforcement-badge";
import { hasScopeSelection, isElementScopeEnforceable, isScopeEnforced } from "@/lib/element-scope";
import { cn, getAgentRequiredTemplateProviders, resolvesPerUser } from "@/lib/utils";
import { effectiveAllowedTools } from "@/lib/tool-scope";
import { readsOtherAgentsTraces } from "@/lib/builtin-allowed-tools";
import { useFormatter } from "@/i18n";
import { ToolGateBadge } from "@/pages/tools/ToolGateBadge";
import { useToolGateSummary } from "@/pages/tools/useToolGateSummary";
import { useSkillPackageBindings } from "@/hooks/api/useSkillApi";
import { usePerUserReadiness } from "@/hooks/api/useIntegrationApi";
import { isA2AProvider, useA2AExposure } from "@/hooks/api/useA2AApi";
import { A2ARunAs } from "./A2ARunAs";
import { EmailSignaturesCard } from "./EmailSignaturesCard";
import { NewBuiltinToolsNotice } from "./NewBuiltinToolsNotice";
import { A2AUnavailableNotice } from "@/components/ui/a2a-unavailable-notice";
import {
  packageCarrierHint, packageOwnerHint, packageResourceCarriers, packageResourceOwners,
} from "@/lib/skillPackage";
import { Tip } from "@/components/ui/tooltip";
import { capTipText } from "@/lib/tip-text";
import {
  INTEGRATION_PROVIDER_NAMES, TRANSPORT_DISPLAY_NAMES,
  isCoordinationBuiltinTool,
} from "@/lib/constants";
import type { Agent, Integration, MCPServer, CustomTool, IntegrationElement, ProviderCatalogItem } from "@/types";
import { useCustomToolsEnabled } from "@/hooks/usePlatformConfig";
import { useBuiltinTools } from "@/hooks/useBuiltinTools";
import { BuiltinToolsPending } from "@/pages/tools/BuiltinToolsPending";
import type { Access as CoordinationAccessDraft } from "@/lib/coordination-access";
import { CoordinationAccessPanel, CoordinationAccessSummary } from "./CoordinationAccessPanel";
import { LIVE_WRITES_PROVIDERS, LiveWritesBadge, LiveWritesControl, integrationLiveWrites, type LiveWritesMode } from "./LiveWritesControl";

export type ToolScopeDialogConfig = {
  type: "integration" | "mcp" | "channel-bot" | "builtin";
  id: string;
  label: string;
};

type ToolingSectionProps = {
  agent: Agent;
  canManage: boolean;
  /**
   * Whether the INTEGRATION cards are editable. Diverges from `canManage` on
   * exactly one agent class: a built-in in XTM One mode, whose seeded
   * definition is read-only but whose connected integrations an administrator
   * still configures (the backend admits that change, and nothing else about a
   * built-in — see `_is_integration_binding_change`).
   */
  canManageIntegrations: boolean;
  isPending: boolean;
  companyManaged: boolean;
  isXtmOne?: boolean;
  // Integrations
  editingIntegrations: boolean;
  bindings: Record<string, string[]>;
  templateBindings: string[];
  /** Per-user providers whose write tools are withheld (draft). */
  templateReadOnly: string[];
  onSetTemplateReadOnly: (provider: string, on: boolean) => void;
  /** OpenCTI Live writes overrides in the draft, by integration id or provider key; absent = inherit. */
  liveWrites: Record<string, "on" | "off">;
  onSetLiveWrites: (key: string, mode: LiveWritesMode) => void;
  /** The caller is an administrator: may set any binding to Live, a per-user one included. */
  isAdmin: boolean;
  integAllowed: Record<string, string[] | null>;
  integrationSearch: string;
  setIntegrationSearch: (v: string) => void;
  connectedIntegrations: Integration[];
  availableIntegrations: Integration[];
  integrations: Integration[] | undefined;
  onToggleIntegration: (integId: string, provider: string) => void;
  onToggleTemplateBinding: (provider: string) => void;
  onStartEditIntegrations: () => void;
  onSaveIntegrations: () => void;
  onCancelIntegrations: () => void;
  // Scope(s)
  editingElements: boolean;
  elementDraft: Record<string, IntegrationElement[]>;
  onUpdateElementDraft: (key: string, items: IntegrationElement[]) => void;
  /** Element-binding keys switched to "Enforce as a boundary" in the draft. */
  enforcementDraft: string[];
  onToggleEnforcement: (key: string, enforced: boolean) => void;
  catalog: ProviderCatalogItem[] | undefined;
  onStartEditElements: () => void;
  onCancelEditElements: () => void;
  onSaveElements: (draft: Record<string, IntegrationElement[]>) => void;
  // MCP Servers
  editingMcp: boolean;
  mcpIds: string[];
  onToggleMcp: (id: string) => void;
  mcpAllowed: Record<string, string[] | null>;
  mcpSearch: string;
  setMcpSearch: (v: string) => void;
  availableMcpServers: MCPServer[];
  mcpServers: MCPServer[] | undefined;
  onStartEditMcp: () => void;
  onSaveMcp: () => void;
  onCancelMcp: () => void;
  // Built-in Tools
  editingBuiltinTools: boolean;
  builtinToolIds: string[];
  /** Per-function scoping draft of the built-in groups; missing = every function. */
  builtinAllowed: Record<string, string[] | null>;
  onToggleBuiltinTool: (id: string) => void;
  onStartEditBuiltinTools: () => void;
  onSaveBuiltinTools: () => void;
  onCancelBuiltinTools: () => void;
  // Work Items peer roster
  allAgents?: Agent[];
  workItemOwnerIds: string[];
  onToggleWorkItemOwner: (id: string) => void;
  traceSupervisionTargetIds: string[];
  onToggleTraceSupervisionTarget: (id: string) => void;
  // Coordination access of a company-managed agent
  coordinationDraft: CoordinationAccessDraft;
  onChangeCoordination: (next: CoordinationAccessDraft) => void;
  // Custom Tools
  editingCustomTools: boolean;
  customToolIds: string[];
  onToggleCustomTool: (id: string) => void;
  customToolSearch: string;
  setCustomToolSearch: (v: string) => void;
  customTools: CustomTool[] | undefined;
  onStartEditCustomTools: () => void;
  onSaveCustomTools: () => void;
  onCancelCustomTools: () => void;
  // Tool scope
  onOpenToolScope: (config: ToolScopeDialogConfig) => void;
};

export function ToolingSection({
  agent,
  canManage,
  canManageIntegrations,
  isPending,
  companyManaged,
  isXtmOne,
  editingIntegrations,
  bindings,
  templateBindings,
  templateReadOnly,
  onSetTemplateReadOnly,
  liveWrites,
  onSetLiveWrites,
  isAdmin,
  integAllowed,
  integrationSearch,
  setIntegrationSearch,
  connectedIntegrations,
  availableIntegrations,
  integrations,
  onToggleIntegration,
  onToggleTemplateBinding,
  onStartEditIntegrations,
  onSaveIntegrations,
  onCancelIntegrations,
  editingElements,
  elementDraft,
  onUpdateElementDraft,
  enforcementDraft,
  onToggleEnforcement,
  catalog,
  onStartEditElements,
  onCancelEditElements,
  onSaveElements,
  editingMcp,
  mcpIds,
  onToggleMcp,
  mcpAllowed,
  mcpSearch,
  setMcpSearch,
  availableMcpServers,
  mcpServers,
  onStartEditMcp,
  onSaveMcp,
  onCancelMcp,
  editingBuiltinTools,
  builtinToolIds,
  builtinAllowed,
  onToggleBuiltinTool,
  onStartEditBuiltinTools,
  onSaveBuiltinTools,
  onCancelBuiltinTools,
  allAgents,
  workItemOwnerIds,
  onToggleWorkItemOwner,
  traceSupervisionTargetIds,
  onToggleTraceSupervisionTarget,
  coordinationDraft,
  onChangeCoordination,
  editingCustomTools,
  customToolIds,
  onToggleCustomTool,
  customToolSearch,
  setCustomToolSearch,
  customTools,
  onStartEditCustomTools,
  onSaveCustomTools,
  onCancelCustomTools,
  onOpenToolScope,
}: ToolingSectionProps) {
  const { t_i18n } = useFormatter();
  /* Only while THIS agent asks before running tools: on an agent that does
     not, or is disabled, every tool runs without asking and an "Asks first"
     chip is false. */
  const { byGroup } = useToolGateSummary({
    agentApproval: agent.approve_tool_calls,
    agentEnabled: agent.enabled,
  });
  const customToolsEnabled = useCustomToolsEnabled();
  /* A suite behind a deployment switch (the code interpreter, runners, the
     `FLOW_BUILDER` flag) is not offered while it is off. An agent that already
     carries it keeps it, and both views list it greyed, the edit grid so it
     can be removed. */
  const {
    listedInAgentConfig: listedBuiltinTools,
    describe: describeBuiltinTool,
    carries: carriesBuiltin,
    canonicalIds: canonicalBuiltinIds,
    isReady: builtinToolsReady,
    isError: builtinToolsFailed,
  } = useBuiltinTools();
  // The agent's A2A card URL, shown once in the list below. The hook itself
  // stays quiet while A2A is not offered (it follows agentic chat).
  const bindsA2A = Object.keys(agent.integration_bindings || {}).some(isA2AProvider);
  const { data: a2aExposure } = useA2AExposure(agent.id, canManageIntegrations && bindsA2A);

  // Custom tools materialized from a packaged skill's ``scripts/`` are owned by
  // that skill: the backend keeps them in sync with the skill on every save, so
  // neither detaching one nor attaching one whose skill is missing would
  // survive — both are labelled skill-driven instead of offering a dead toggle.
  const { data: skillPackageBindings } = useSkillPackageBindings();
  const skillOwnedTools = packageResourceOwners(
    skillPackageBindings,
    agent.skill_ids,
  ).customTools;
  const skillCarriedTools = packageResourceCarriers(skillPackageBindings).customTools;

  // Per-provider readiness for "resolved per user at runtime": whether a
  // company-managed instance exists to cover users who have not connected their
  // own account. Keyed by provider for O(1) lookup in the card list.
  const { data: perUserReadiness } = usePerUserReadiness();
  const readinessByProvider = useMemo(
    () => Object.fromEntries((perUserReadiness?.providers ?? []).map((p) => [p.provider, p])),
    [perUserReadiness],
  );

  // "Advanced: Dynamic integrations" accordion open state. Seeded once when
  // entering edit mode (open if the agent already has per-user bindings) and
  // then driven solely by the user — toggling a dynamic binding on/off must
  // NOT force the accordion open or closed.
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const wasEditingIntegrationsRef = useRef(false);
  useEffect(() => {
    if (editingIntegrations && !wasEditingIntegrationsRef.current) {
      setAdvancedOpen(getAgentRequiredTemplateProviders(agent).length > 0);
    }
    wasEditingIntegrationsRef.current = editingIntegrations;
  }, [editingIntegrations, agent]);

  return (
    <>
      {/* Integration bindings */}
      <Card>
        <CardHeader className="flex-row items-center justify-between space-y-0 p-4 pb-3">
          <div>
            <CardTitle className="content-base flex items-center gap-2">
              <Cog className="h-4 w-4 text-accent" /> {t_i18n("Integrations")}
              {(() => { const ids = Object.values(agent.integration_bindings || {}).flat(); const count = (integrations ? ids.filter(id => integrations.some(i => i.id === id)).length : ids.length) + getAgentRequiredTemplateProviders(agent).length; return count > 0 && !editingIntegrations ? <CountBadge variant="secondary">{count}</CountBadge> : null; })()}
            </CardTitle>
            {companyManaged && !agent.is_builtin && <p className="content-caption text-feedback-warning-primary mt-0.5 flex items-center gap-1"><Building2 className="h-3 w-3" /> {isXtmOne ? t_i18n("Instance bindings are limited to company-managed integrations. Use dynamic templates for per-user integrations.") : t_i18n("Only company-managed integrations are shown.")}</p>}
          </div>
          <CardEditActions editing={editingIntegrations} canEdit={canManageIntegrations} onEdit={onStartEditIntegrations} onCancel={onCancelIntegrations} onSave={onSaveIntegrations} saveDisabled={isPending} />
        </CardHeader>
        <CardContent className="px-4 pb-4 pt-0">
          {editingIntegrations ? (
            <>
              <p className="content-caption text-default-secondary mb-3">{t_i18n("Click to bind or unbind integrations. Use the tool scope button to limit which tools are available.")}</p>
              {(connectedIntegrations.filter((i) => !companyManaged || i.is_company_managed).length) > 3 && (
                <SearchInput placeholder={t_i18n("Filter integrations...")} value={integrationSearch} onChange={setIntegrationSearch} className="mb-3 max-w-full" />
              )}
              {(availableIntegrations.length > 0 || (isXtmOne && connectedIntegrations.length > 0)) ? (
                <div className="space-y-4">
                  {/* Instance binding options — primary */}
                  <div>
                    <p className="content-caption text-default-secondary uppercase tracking-wider font-medium mb-2">{t_i18n("Specific integration instances")}</p>
                    <div className="grid gap-2 sm:grid-cols-3">
                      {availableIntegrations.map((integ) => {
                        const isProviderTemplate = templateBindings.includes(integ.provider);
                        const isBound = !isProviderTemplate && Object.values(bindings).flat().includes(integ.id);
                        const scopedTools = effectiveAllowedTools(integAllowed, integ);
                        const isScopeLimited = isBound && scopedTools !== undefined && scopedTools !== null;
                        // An A2A binding is a partner relationship, not a tool
                        // set: inbound EXPOSES this agent and contributes no
                        // tools at all, outbound contributes one delegation
                        // tool per partner agent that verified. Neither is
                        // something to pick from, so the card carries no
                        // scope control at all.
                        const isA2A = isA2AProvider(integ.provider);
                        const card = (
                          <div key={integ.id} data-slot="integration-binding" className={cn(
                            "rounded-sm border transition-all",
                            isProviderTemplate && "opacity-50",
                            isBound ? "border-accent/40 bg-accent/5" : "border-elevation-subtle-soft bg-elevation-hover hover:border-elevation-subtle",
                          )}>
                            <button
                              className="flex items-center gap-3 p-3 text-left w-full"
                              onClick={() => onToggleIntegration(integ.id, integ.provider)}
                              disabled={isProviderTemplate}
                            >
                              <ProviderLogo provider={integ.provider} className="h-6 w-6 shrink-0" />
                              <div className="min-w-0 flex-1">
                                <p className={cn("content-base font-medium truncate", isBound && "text-accent")}>{integ.name}</p>
                                <p className="content-caption text-default-secondary">{INTEGRATION_PROVIDER_NAMES[integ.provider] ?? integ.provider}</p>
                              </div>
                              {isProviderTemplate ? (
                                <Lock className="h-4 w-4 text-filigran-ia-primary/60 shrink-0" />
                              ) : isBound ? (
                                <CheckCircle2 className="h-4 w-4 text-accent shrink-0" />
                              ) : (
                                <Plus className="h-4 w-4 text-default-disabled shrink-0" />
                              )}
                            </button>
                            {isBound && !isA2A && (
                              <div className="px-3 pb-2.5 pt-0">
                                <button
                                  className="flex items-center gap-1.5 rounded-sm px-2 py-1 content-caption font-medium transition-colors w-full justify-center bg-feedback-alert-primary/10 text-feedback-warning-tertiary border border-feedback-alert-primary/20 hover:bg-feedback-alert-primary/15"
                                  onClick={(e) => { e.stopPropagation(); onOpenToolScope({ type: "integration", id: integ.id, label: integ.name }); }}
                                >
                                  <Filter className="h-3 w-3" />
                                  {isScopeLimited
                                    ? t_i18n("{count, plural, one {# tool} other {# tools}} selected", { count: scopedTools.length })
                                    : t_i18n("All tools")}
                                </button>
                              </div>
                            )}
                            {/* OpenCTI Live writes per binding: Inherit, Live or Draft only (#11958). */}
                            {isBound && LIVE_WRITES_PROVIDERS.includes(integ.provider) && (
                              <div className="px-3 pb-2.5 pt-0">
                                <LiveWritesControl
                                  bindingKey={integ.id}
                                  label={integ.name}
                                  inheritedLive={integrationLiveWrites(integ.extra_config)}
                                  value={liveWrites[integ.id] ?? "inherit"}
                                  canSetLive={isAdmin || integ.can_manage === true}
                                  onChange={(mode) => onSetLiveWrites(integ.id, mode)}
                                />
                              </div>
                            )}
                          </div>
                        );
                        return isProviderTemplate ? (
                          <Tip key={integ.id} content={t_i18n("Disabled: dynamic per-user binding is active for {provider}. Remove it in the Advanced section below to use a specific instance instead.", { provider: INTEGRATION_PROVIDER_NAMES[integ.provider] ?? integ.provider })}>
                            {card}
                          </Tip>
                        ) : card;
                      })}
                    </div>
                  </div>
                  {/* Dynamic (per-user) binding options — advanced, collapsible */}
                  {(() => {
                    const source = isXtmOne ? connectedIntegrations : availableIntegrations;
                    // No A2A here. Per-user mode resolves a provider to the
                    // acting user's OWN connection at runtime, and a partner
                    // relationship has no per-user form: the credential, the
                    // exposure and the partner's cards belong to the
                    // registration, not to whoever happens to be running.
                    // Same for OpenAPI: every integration of that provider is a
                    // different API, so "the user's own one" names none.
                    // Kept visible when one is ALREADY set, so an agent
                    // carrying such a binding from before can still have it
                    // turned off — hiding it would strand it on.
                    const providers = [...new Set(source.map((i) => i.provider))]
                      .filter((p) => (!isA2AProvider(p) && resolvesPerUser(p)) || templateBindings.includes(p))
                      .sort();
                    if (providers.length === 0) return null;
                    // Per-user mode is only self-sufficient for a connector that
                    // has a company-managed instance to fall back on. Surface the
                    // ones that don't, plus any personal instance shared to the
                    // team that merely *looks* like the shared one.
                    const enabledWithoutFallback = providers.filter(
                      (p) => templateBindings.includes(p) && readinessByProvider[p]?.has_company_wide_fallback === false,
                    );
                    const deFactoOwners = [...new Set(
                      enabledWithoutFallback.flatMap((p) => readinessByProvider[p]?.de_facto_shared_owners ?? []),
                    )];
                    return (
                      <details
                        className="group"
                        open={advancedOpen}
                        onToggle={(e) => setAdvancedOpen(e.currentTarget.open)}
                      >
                        <DisclosureSummary size="block" className="flex items-center gap-2 select-none py-2 content-caption text-default-secondary uppercase tracking-wider font-medium hover:text-default-primary transition-colors">
                          <ChevronRight className="h-3 w-3 transition-transform group-open:rotate-90" />
                          {t_i18n("Advanced: Dynamic integrations (per-user)")}
                        </DisclosureSummary>
                        <div className="mt-2 rounded-sm border border-feedback-alert-primary/20 bg-feedback-alert-primary/5 p-3 mb-3">
                          <p className="content-caption text-feedback-warning-primary flex items-center gap-1.5">
                            <AlertTriangle className="h-3 w-3 shrink-0" />
                            {t_i18n("Dynamic bindings resolve to each user's own integration at runtime, and override specific instance bindings for the same provider. Enabling this does not create or select a company-wide integration: a user with no connection of their own falls back to the company-managed instance, or, when the connector has none, gets no tools for that provider at all.")}
                          </p>
                        </div>
                        {enabledWithoutFallback.length > 0 && (
                          <div className="rounded-sm border border-feedback-error-primary/25 bg-feedback-error-primary/5 p-3 mb-3">
                            <p className="content-caption text-feedback-error-primary/90 flex items-start gap-1.5">
                              <ShieldAlert className="h-3 w-3 shrink-0 mt-px" />
                              <span>
                                {t_i18n("No company-managed instance exists for {providers}. Each user must connect their own account before this agent has any tools for it.", { providers: enabledWithoutFallback.map((p) => INTEGRATION_PROVIDER_NAMES[p] ?? p).join(", ") })}
                                {deFactoOwners.length > 0 && (
                                  <> {t_i18n("A personal instance shared by {owners} appears in the integrations list but is never used at runtime: ask an admin to create a company-managed instance instead.", { owners: deFactoOwners.join(", ") })}</>
                                )}
                              </span>
                            </p>
                          </div>
                        )}
                        <div className="grid gap-2 sm:grid-cols-3">
                          {providers.map((provider) => {
                            const isTemplate = templateBindings.includes(provider);
                            // Tool scoping is keyed by provider, so a dynamic (per-user)
                            // binding scopes tools exactly like a specific instance.
                            // Any connected instance of this provider can drive the tool
                            // picker — the available tools are identical provider-wide.
                            const repInteg = source.find((i) => i.provider === provider);
                            const scopedTools = integAllowed[provider];
                            const isScopeLimited = isTemplate && scopedTools !== undefined && scopedTools !== null;
                            return (
                              <div key={`tmpl-${provider}`} data-slot="integration-binding" className={cn(
                                "rounded-sm border transition-all",
                                isTemplate ? "border-filigran-ia-primary/40 bg-filigran-ia-primary/5" : "border-elevation-subtle-soft bg-elevation-hover hover:border-elevation-subtle",
                              )}>
                                <button
                                  className="flex items-center gap-3 p-3 text-left w-full"
                                  onClick={() => onToggleTemplateBinding(provider)}
                                >
                                  <ProviderLogo provider={provider} className="h-6 w-6 shrink-0" />
                                  <div className="min-w-0 flex-1">
                                    <p className={cn("content-base font-medium truncate", isTemplate && "text-filigran-ia-primary")}>{INTEGRATION_PROVIDER_NAMES[provider] ?? provider}</p>
                                    <p className="content-caption text-default-secondary">{t_i18n("Resolved per user at runtime")}</p>
                                    {/* Whether a user with no connection of their
                                        own still gets tools for this provider. */}
                                    {readinessByProvider[provider] && (
                                      readinessByProvider[provider].has_company_wide_fallback ? (
                                        <p className="text-[9px] text-feedback-success-primary/80 flex items-center gap-1 mt-0.5">
                                          <Building2 className="h-2.5 w-2.5 shrink-0" />
                                          {t_i18n("Company-managed fallback available")}
                                        </p>
                                      ) : (
                                        <p className="text-[9px] text-feedback-error-primary/80 flex items-center gap-1 mt-0.5">
                                          <ShieldAlert className="h-2.5 w-2.5 shrink-0" />
                                          {t_i18n("No company-wide fallback")}
                                        </p>
                                      )
                                    )}
                                  </div>
                                  {isTemplate ? (
                                    <CheckCircle2 className="h-4 w-4 text-filigran-ia-primary shrink-0" />
                                  ) : (
                                    <Users className="h-4 w-4 text-default-disabled shrink-0" />
                                  )}
                                </button>
                                {/* Keeps this provider to its read tools for every
                                    user it resolves to (#10741). */}
                                {isTemplate && !isA2AProvider(provider) && (
                                  <div className="px-3 pb-2 pt-0">
                                    <label className="flex items-center justify-between gap-2 content-caption text-default-secondary">
                                      {t_i18n("Read-only")}
                                      <Switch
                                        checked={templateReadOnly.includes(provider)}
                                        onCheckedChange={(on) => onSetTemplateReadOnly(provider, on)}
                                        aria-label={t_i18n("Read-only: {provider}", { provider: INTEGRATION_PROVIDER_NAMES[provider] ?? provider })}
                                      />
                                    </label>
                                  </div>
                                )}
                                {isTemplate && LIVE_WRITES_PROVIDERS.includes(provider) && (
                                  <div className="px-3 pb-2 pt-0">
                                    <LiveWritesControl
                                      bindingKey={provider}
                                      label={INTEGRATION_PROVIDER_NAMES[provider] ?? provider}
                                      inheritedLive={null}
                                      value={liveWrites[provider] ?? "inherit"}
                                      canSetLive={isAdmin}
                                      onChange={(mode) => onSetLiveWrites(provider, mode)}
                                    />
                                  </div>
                                )}
                                {/* Same reason as the instance cards above: an
                                    A2A provider contributes no tool an operator
                                    picks from, in either direction. */}
                                {isTemplate && repInteg && !isA2AProvider(provider) && (
                                  <div className="px-3 pb-2.5 pt-0">
                                    <button
                                      className="flex items-center gap-1.5 rounded-sm px-2 py-1 content-caption font-medium transition-colors w-full justify-center bg-feedback-alert-primary/10 text-feedback-warning-tertiary border border-feedback-alert-primary/20 hover:bg-feedback-alert-primary/15"
                                      onClick={(e) => { e.stopPropagation(); onOpenToolScope({ type: "integration", id: repInteg.id, label: INTEGRATION_PROVIDER_NAMES[provider] ?? provider }); }}
                                    >
                                      <Filter className="h-3 w-3" />
                                      {isScopeLimited
                                        ? t_i18n("{count, plural, one {# tool} other {# tools}} selected", { count: scopedTools.length })
                                        : t_i18n("All tools")}
                                    </button>
                                  </div>
                                )}
                              </div>
                            );
                          })}
                        </div>
                      </details>
                    );
                  })()}
                </div>
              ) : (
                <div className="py-4 text-center">
                  <Cog className="h-6 w-6 text-default-disabled mx-auto mb-1.5" />
                  <p className="content-base text-default-secondary">{t_i18n("No connected integrations")}</p>
                  <p className="content-caption text-default-secondary mt-0.5">{t_i18n("Create integrations in the Integrations page first.")}</p>
                </div>
              )}
            </>
          ) : (
            <div>
              {(() => {
                const templateBindings = getAgentRequiredTemplateProviders(agent);
                const fixedBindings = Object.entries(agent.integration_bindings || {})
                  .flatMap(([provider, integIds]) => integIds.map((integId) => ({ provider, integId, integ: integrations?.find((i) => i.id === integId) })))
                  .filter((x): x is { provider: string; integId: string; integ: NonNullable<typeof x.integ> } => !!x.integ)
                  .sort((a, b) => a.integ.name.localeCompare(b.integ.name));
                const hasAny = templateBindings.length > 0 || fixedBindings.length > 0;
                return hasAny ? (
                  <div className="space-y-1.5">
                    {fixedBindings.map(({ provider, integId, integ }) => {
                      const scopedTools = effectiveAllowedTools(agent.integration_allowed_tools, { id: integId, provider });
                      // Never for A2A: the picker that could set this is gone,
                      // so a surviving entry is legacy data describing tools
                      // that were never filtered by it. Reporting "0 tools" on
                      // a working partner binding is worse than saying nothing.
                      const isScopeLimited = !isA2AProvider(provider) && scopedTools !== undefined && scopedTools !== null;
                      return (
                        <div key={`${provider}-${integId}`} data-slot="integration-binding" className="rounded-sm bg-elevation-hover">
                          <div className="flex items-center gap-3 px-3 py-2.5">
                            <ProviderLogo provider={provider} className="h-5 w-5 shrink-0" />
                            <div className="min-w-0 flex-1">
                              <p className="content-base font-medium">{integ.name}</p>
                              <p className="content-caption text-default-secondary">{INTEGRATION_PROVIDER_NAMES[provider] ?? provider}</p>
                            </div>
                            {isScopeLimited && (
                              <span className="text-[9px] font-medium rounded-sm px-1.5 py-0.5 shrink-0 text-feedback-warning-tertiary bg-feedback-alert-primary/10">{t_i18n("{count, plural, one {# tool} other {# tools}}", { count: scopedTools.length })}</span>
                            )}
                            <LiveWritesBadge mode={agent.integration_live_writes?.[integId]} />
                            {integ.status === "connected" ? <CheckCircle2 className="h-4 w-4 text-feedback-success-primary shrink-0" /> : <XCircle className="h-4 w-4 text-feedback-error-primary shrink-0" />}
                          </div>
                        </div>
                      );
                    })}
                    {templateBindings.length > 0 && (
                      <>
                        {fixedBindings.length > 0 && <div className="border-t border-elevation-subtle-soft my-2" />}
                        {templateBindings.map((provider) => {
                          const scopedTools = (agent.integration_allowed_tools || {})[provider];
                          const isScopeLimited = !isA2AProvider(provider) && scopedTools !== undefined && scopedTools !== null;
                          return (
                            <div key={`tmpl-${provider}`} className="flex items-center gap-3 rounded-sm bg-elevation-hover px-3 py-2.5">
                              <ProviderLogo provider={provider} className="h-5 w-5 shrink-0" />
                              <div className="min-w-0 flex-1">
                                <p className="content-base font-medium">{INTEGRATION_PROVIDER_NAMES[provider] ?? provider}</p>
                                <p className="content-caption text-default-secondary">{t_i18n("Resolved per user at runtime")}</p>
                              </div>
                              {isScopeLimited && (
                                <span className="text-[9px] font-medium rounded-sm px-1.5 py-0.5 shrink-0 text-feedback-warning-tertiary bg-feedback-alert-primary/10">{t_i18n("{count, plural, one {# tool} other {# tools}}", { count: scopedTools.length })}</span>
                              )}
                              <LiveWritesBadge mode={agent.integration_live_writes?.[provider]} />
                              {(agent.integration_template_read_only || []).includes(provider) && (
                                <Tip content={t_i18n("Read tools only: write tools of this integration are withheld and refused for every user.")}>
                                  <BadgeFocusTrigger className="shrink-0" aria-label={`${t_i18n("Read-only")}. ${t_i18n("Read tools only: write tools of this integration are withheld and refused for every user.")}`}>
                                    <Badge variant="outline" className="gap-1">
                                      <Lock className="h-3 w-3" /> {t_i18n("Read-only")}
                                    </Badge>
                                  </BadgeFocusTrigger>
                                </Tip>
                              )}
                              {/* Users who have not connected their own account
                                  get nothing here unless a company-managed
                                  instance exists to fall back on. */}
                              {readinessByProvider[provider]?.has_company_wide_fallback === false && (
                                <Tip content={t_i18n("No company-managed instance exists for this connector. Users who have not connected their own account get no tools for it.")}>
                                  <span className="flex items-center gap-1 text-[9px] font-medium rounded-sm px-1.5 py-0.5 shrink-0 text-feedback-error-primary bg-feedback-error-primary/10">
                                    <ShieldAlert className="h-3 w-3" /> {t_i18n("No fallback")}
                                  </span>
                                </Tip>
                              )}
                              <span className="flex items-center gap-1 text-[9px] font-medium rounded-sm px-1.5 py-0.5 shrink-0 text-filigran-ia-primary bg-filigran-ia-primary/10">
                                <Users className="h-3 w-3" /> {t_i18n("Per-user")}
                              </span>
                            </div>
                          );
                        })}
                      </>
                    )}
                    {/* The card URL lives on the integration's Config tab,
                        one row per bound agent. */}
                    {a2aExposure?.exposed && (
                      <A2ARunAs agent={agent} exposure={a2aExposure} />
                    )}
                    {/* Why the card URL is absent while A2A is not offered —
                        the exposure query above is disabled then. Renders
                        nothing otherwise. */}
                    {bindsA2A && (
                      <A2AUnavailableNotice
                        hint={t_i18n("This agent's A2A bindings are kept, but its agent card is not served and no delegation tool is offered until agentic chat is available again.")}
                      />
                    )}
                  </div>
                ) : (
                  <p className="content-base text-default-secondary italic py-3 text-center">{t_i18n("No integrations bound")}</p>
                );
              })()}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Email signature of each bound email-sending integration;
          renders itself away when the agent binds none. */}
      <EmailSignaturesCard agent={agent} canManage={canManageIntegrations} />

      {/* Scope(s) (orgs, repos, channels) */}
      {(() => {
        const effectiveBindings = agent.integration_bindings ?? {};
        const boundProviders = Object.keys(effectiveBindings);
        const providersWithElements = (catalog ?? []).filter(
          (p) => p.element_types.length > 0 && boundProviders.includes(p.provider),
        );
        if (providersWithElements.length === 0) return null;
        const currentBindings = agent.integration_element_bindings ?? {};
        const totalBound = Object.values(currentBindings).reduce((s, items) => s + items.length, 0);
        return (
          <Card>
            <CardHeader className="flex-row items-center justify-between space-y-0 p-4 pb-2">
              <CardTitle className="content-base flex items-center gap-2">
                <Hash className="h-4 w-4 text-filigran-ia-primary" /> {t_i18n("Scope(s)")}
                {totalBound > 0 && (
                  <CountBadge variant="secondary">{totalBound}</CountBadge>
                )}
              </CardTitle>
              <CardEditActions editing={editingElements} canEdit={canManageIntegrations && providersWithElements.length > 0} onEdit={onStartEditElements} onCancel={onCancelEditElements} onSave={() => onSaveElements(elementDraft)} />
            </CardHeader>
            <CardContent className="px-4 pb-4 pt-2">
              {editingElements ? (
                <div className="space-y-3">
                  <p className="content-caption text-default-secondary">{t_i18n("Select specific organizations, repositories, channels, or other elements for this agent to focus on. Assignments can further refine from this selection.")}</p>
                  {providersWithElements.map((provider) =>
                    provider.element_types.map((et) => {
                      const integrationIds = effectiveBindings[provider.provider];
                      if (!integrationIds || integrationIds.length === 0) return null;
                      const integrationId = integrationIds[0];
                      const key = `${provider.provider}:${et.type}`;
                      return (
                        <div key={key} className="space-y-1">
                          <div className="flex items-center justify-between gap-2">
                            <ScopeEnforcementBadge enforced={isScopeEnforced(key, elementDraft, enforcementDraft)} />
                            {isElementScopeEnforceable(key) && (
                              <ScopeEnforcementSwitch
                                checked={enforcementDraft.includes(key)}
                                hasSelection={hasScopeSelection(elementDraft, key)}
                                onCheckedChange={(enforced) => onToggleEnforcement(key, enforced)}
                              />
                            )}
                          </div>
                          <ElementTypeEditor
                            integrationId={integrationId}
                            provider={provider.provider}
                            elementType={et}
                            selected={elementDraft[key] ?? []}
                            onSelectionChange={(items) => onUpdateElementDraft(key, items)}
                          />
                        </div>
                      );
                    }),
                  )}
                </div>
              ) : (
                <div className="space-y-2">
                  {totalBound === 0 ? (
                    <p className="content-compact text-default-secondary italic py-3 text-center">
                      {t_i18n("No target elements selected: agent operates on all available elements.")}
                    </p>
                  ) : (
                    <>
                      {Object.entries(currentBindings).map(([key, items]) => {
                        const colonIdx = key.indexOf(":");
                        const providerKey = colonIdx >= 0 ? key.slice(0, colonIdx) : key;
                        const etype = colonIdx >= 0 ? key.slice(colonIdx + 1) : key;
                        const providerName = INTEGRATION_PROVIDER_NAMES[providerKey] ?? providerKey;
                        const etLabel = (catalog ?? [])
                          .find((p) => p.provider === providerKey)
                          ?.element_types.find((et) => et.type === etype)?.name ?? etype;
                        return (
                          <div key={key} className="rounded-sm bg-elevation-hover p-3">
                            <div className="mb-1.5 flex items-center gap-2">
                              <p className="content-caption text-default-secondary">{providerName} — {etLabel}</p>
                              <ScopeEnforcementBadge
                                enforced={isScopeEnforced(key, currentBindings, agent.integration_element_enforcement)}
                              />
                            </div>
                            <div className="flex flex-wrap gap-1.5">
                              {items.map((item) => (
                                <span key={item.id} className="content-compact bg-filigran-ia-primary/10 text-filigran-ia-primary rounded-sm px-2 py-0.5">
                                  {item.name}{item.group ? ` (${item.group})` : ""}
                                </span>
                              ))}
                            </div>
                          </div>
                        );
                      })}
                    </>
                  )}
                </div>
              )}
            </CardContent>
          </Card>
        );
      })()}

      {/* MCP servers */}
      <Card>
        <CardHeader className="flex-row items-center justify-between space-y-0 p-4 pb-3">
          <div>
            <CardTitle className="content-base flex items-center gap-2">
              <Terminal className="h-4 w-4 text-accent" /> {t_i18n("MCP Tool Servers")}
              {(agent.mcp_server_ids || []).length > 0 && !editingMcp && (
                <CountBadge variant="secondary">{(agent.mcp_server_ids || []).length}</CountBadge>
              )}
            </CardTitle>
            {companyManaged && !agent.is_builtin && <p className="content-caption text-feedback-warning-primary mt-0.5 flex items-center gap-1"><Building2 className="h-3 w-3" /> {t_i18n("Only company-managed MCP servers are shown.")}</p>}
          </div>
          <CardEditActions editing={editingMcp} canEdit={canManage} onEdit={onStartEditMcp} onCancel={onCancelMcp} onSave={onSaveMcp} saveDisabled={isPending} />
        </CardHeader>
        <CardContent className="px-4 pb-4 pt-0">
          {editingMcp ? (
            <>
              <p className="content-caption text-default-secondary mb-3">{t_i18n("Click to attach or detach MCP tool servers. Use the tool scope button to limit which tools are available.")}</p>
              {(mcpServers ?? []).filter((s) => s.enabled && (!companyManaged || s.is_company_managed)).length > 3 && (
                <SearchInput placeholder={t_i18n("Filter MCP servers...")} value={mcpSearch} onChange={setMcpSearch} className="mb-3 max-w-full" />
              )}
              {availableMcpServers.length > 0 ? (
                <div className="grid gap-2 sm:grid-cols-3">
                  {availableMcpServers.map((srv) => {
                    const isAttached = mcpIds.includes(srv.id);
                    const scopedTools = mcpAllowed[srv.id];
                    const isScopeLimited = isAttached && scopedTools !== undefined && scopedTools !== null;
                    return (
                      <div key={srv.id} className={cn(
                        "rounded-sm border transition-all",
                        isAttached ? "border-accent/40 bg-accent/5" : "border-elevation-subtle-soft bg-elevation-hover hover:border-elevation-subtle",
                      )}>
                        <button
                          className="flex items-center gap-3 p-3 text-left w-full"
                          onClick={() => onToggleMcp(srv.id)}
                        >
                          <Terminal className={cn("h-5 w-5 shrink-0", isAttached ? "text-accent" : "text-default-secondary")} />
                          <div className="min-w-0 flex-1">
                            <p className={cn("content-base font-medium truncate", isAttached && "text-accent")}>{srv.name}</p>
                            <p className="content-caption text-default-secondary">{TRANSPORT_DISPLAY_NAMES[srv.transport] ?? srv.transport}</p>
                          </div>
                          {isAttached ? (
                            <CheckCircle2 className="h-4 w-4 text-accent shrink-0" />
                          ) : (
                            <Plus className="h-4 w-4 text-default-disabled shrink-0" />
                          )}
                        </button>
                        {isAttached && (
                          <div className="px-3 pb-2.5 pt-0">
                            <button
                              className="flex items-center gap-1.5 rounded-sm px-2 py-1 content-caption font-medium transition-colors w-full justify-center bg-feedback-alert-primary/10 text-feedback-warning-tertiary border border-feedback-alert-primary/20 hover:bg-feedback-alert-primary/15"
                              onClick={(e) => { e.stopPropagation(); onOpenToolScope({ type: "mcp", id: srv.id, label: srv.name }); }}
                            >
                              <Filter className="h-3 w-3" />
                              {isScopeLimited
                                ? t_i18n("{count, plural, one {# tool} other {# tools}} selected", { count: scopedTools.length })
                                : t_i18n("All tools")}
                            </button>
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              ) : (
                <div className="py-4 text-center">
                  <Terminal className="h-6 w-6 text-default-disabled mx-auto mb-1.5" />
                  <p className="content-base text-default-secondary">{t_i18n("No MCP servers available")}</p>
                  <p className="content-caption text-default-secondary mt-0.5">{t_i18n("Add MCP servers under Tools & Integrations > MCP Servers.")}</p>
                </div>
              )}
            </>
          ) : (
            <div>
              {(agent.mcp_server_ids || []).length > 0 ? (
                <div className="space-y-1.5">
                  {(agent.mcp_server_ids || [])
                    .map((srvId) => ({ srvId, srv: mcpServers?.find((s) => s.id === srvId) }))
                    .filter((x): x is { srvId: string; srv: NonNullable<typeof x.srv> } => !!x.srv)
                    .sort((a, b) => a.srv.name.localeCompare(b.srv.name))
                    .map(({ srvId, srv }) => {
                    const scopedTools = (agent.mcp_allowed_tools || {})[srvId];
                    const isScopeLimited = scopedTools !== undefined && scopedTools !== null;
                    return (
                      <div key={srvId} className="flex items-center gap-3 rounded-sm bg-elevation-hover px-3 py-2.5">
                        <Terminal className="h-4 w-4 text-accent shrink-0" />
                        <div className="min-w-0 flex-1">
                          <p className="content-base font-medium">{srv.name}</p>
                          <p className="content-caption text-default-secondary">{TRANSPORT_DISPLAY_NAMES[srv.transport] ?? srv.transport}</p>
                        </div>
                        {isScopeLimited && (
                          <span className="text-[9px] font-medium rounded-sm px-1.5 py-0.5 shrink-0 text-feedback-warning-tertiary bg-feedback-alert-primary/10">{t_i18n("{count, plural, one {# tool} other {# tools}}", { count: scopedTools.length })}</span>
                        )}
                      </div>
                    );
                  })}
                </div>
              ) : (
                <p className="content-base text-default-secondary italic py-3 text-center">{t_i18n("No MCP servers attached")}</p>
              )}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Built-in Tools */}
      <Card data-testid="builtin-tools-card">
        <CardHeader className="flex-row items-center justify-between space-y-0 p-4 pb-3">
          <CardTitle className="content-base flex items-center gap-2">
            <Sparkles className="h-4 w-4 text-accent" /> {t_i18n("Built-in Tools")}
            {(agent.builtin_tool_ids || []).length > 0 && !editingBuiltinTools && (
              <CountBadge variant="secondary">{canonicalBuiltinIds(agent.builtin_tool_ids).length}</CountBadge>
            )}
          </CardTitle>
          <CardEditActions editing={editingBuiltinTools} canEdit={canManage} onEdit={onStartEditBuiltinTools} onCancel={onCancelBuiltinTools} onSave={onSaveBuiltinTools} saveDisabled={isPending} testIds={{ edit: "builtin-tools-edit", cancel: "builtin-tools-cancel", save: "builtin-tools-save" }} />
        </CardHeader>
        <CardContent className="px-4 pb-4 pt-0">
          {builtinToolsReady && (
            <NewBuiltinToolsNotice
              agent={agent}
              canManage={canManage}
              groupFunctions={(groupId) => describeBuiltinTool(groupId).functions}
            />
          )}
          {editingBuiltinTools && !builtinToolsReady ? (
            <BuiltinToolsPending isError={builtinToolsFailed} gridClassName="sm:grid-cols-2 lg:grid-cols-3" testId="builtin-tools-grid" />
          ) : editingBuiltinTools ? (
            <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
              {listedBuiltinTools(agent.builtin_tool_ids ?? [], builtinToolIds).map((tool) => {
                const isEnabled = carriesBuiltin(builtinToolIds, tool.id);
                // Switched off on this deployment: listed only because the
                // agent carries it, so a click removes it, or puts back one
                // removed before the save.
                const isUnavailable = !tool.available;
                // Only an administrator adds this suite, and the viewer is not
                // one: locked unless the saved agent carries it (then it stays
                // removable, and a removal can be undone before the save).
                const isLocked = !!tool.bindLocked && !isEnabled && !carriesBuiltin(agent.builtin_tool_ids ?? [], tool.id);
                const isCoord = isCoordinationBuiltinTool(tool.id);
                const Icon = tool.icon;
                // Coordination tools (work items / shared state) get a subtle
                // violet accent so they read as a distinct cross-run category.
                const accentText = isCoord ? "text-filigran-ia-primary light:text-filigran-ia-primary" : "text-accent";
                return (
                  <div key={tool.id} className={cn(
                    "rounded-sm border transition-all",
                    isUnavailable || isLocked
                      ? "border-dashed border-elevation-subtle-soft bg-elevation-hover opacity-60 hover:opacity-80"
                      : isEnabled
                        ? (isCoord ? "border-filigran-ia-primary/40 bg-filigran-ia-primary/5 light:border-filigran-ia-primary/40 light:bg-filigran-ia-primary/5" : "border-accent/40 bg-accent/5")
                        : "border-elevation-subtle-soft bg-elevation-hover hover:border-elevation-subtle",
                  )}>
                    {/* The tooltip leads with the full name, then the
                        description, as on the General Assistant grid
                        (ResourceToggleCard): a name cut with an
                        ellipsis on a narrow card stays readable. */}
                    <Tip content={
                      <span className="block" data-testid={`builtin-tool-tip-${tool.id}`}>
                        <span className="block font-medium">{tool.label}</span>
                        {tool.description && <span className="block">{capTipText(tool.description)}</span>}
                        {isUnavailable && <span className="block">{t_i18n("Unavailable on this deployment: it stays inactive until an administrator enables it.")}</span>}
                        {isLocked && <span className="block" data-testid={`builtin-tool-locked-reason-${tool.id}`}>{t_i18n("Only an administrator can add this group")}</span>}
                      </span>
                    }>
                      {/* A locked card stays focusable (aria-disabled, not
                          disabled) so its reason opens on keyboard focus too. */}
                      <button
                        data-testid={`builtin-tool-${tool.id}`}
                        aria-pressed={isEnabled}
                        aria-disabled={isLocked || undefined}
                        className={cn(
                          "flex items-center gap-3 p-3 text-left w-full rounded-sm ring-offset-focus focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus",
                          isLocked && "cursor-not-allowed",
                        )}
                        onClick={() => { if (!isLocked) onToggleBuiltinTool(tool.id); }}
                      >
                        <Icon className={cn(
                          "h-5 w-5 shrink-0",
                          isUnavailable ? "text-default-secondary" : isEnabled ? accentText : (isCoord ? "text-filigran-ia-primary/60 light:text-filigran-ia-primary/60" : "text-default-secondary"),
                        )} />
                        <div className="min-w-0 flex-1">
                          {isUnavailable ? (
                            <div className="flex items-center gap-1.5">
                              <p className="min-w-0 truncate content-base font-medium">{tool.label}</p>
                              <span className="shrink-0 text-[8px] text-feedback-warning-primary">{t_i18n("Unavailable")}</span>
                            </div>
                          ) : (
                            <p className={cn("content-base font-medium truncate", isEnabled && accentText)}>{tool.label}</p>
                          )}
                          <p className="content-caption text-default-secondary">{tool.description}</p>
                        </div>
                        {/* Whether picking this tool means the agent stops to
                            ask (FR-045). A builtin row is a group of tool names,
                            so it answers in aggregate, exactly as the tools list
                            does. */}
                        {(() => {
                          const gate = byGroup.get(tool.id);
                          return gate ? (
                            <ToolGateBadge gating={gate.gating} total={gate.total} />
                          ) : null;
                        })()}
                        {isLocked ? (
                          <Lock className="h-4 w-4 text-default-secondary shrink-0" aria-label={t_i18n("Only an administrator can add this group")} />
                        ) : isUnavailable && !isEnabled ? (
                          <Undo2 className="h-4 w-4 text-default-secondary shrink-0" />
                        ) : isEnabled ? (
                          <CheckCircle2 className={cn("h-4 w-4 shrink-0", isUnavailable ? "text-default-secondary" : accentText)} />
                        ) : (
                          <Plus className="h-4 w-4 text-default-disabled shrink-0" />
                        )}
                      </button>
                    </Tip>
                    {/* Per-function scoping, the same control as an
                        MCP server's tool scope: only on an enabled group
                        that grants more than one function. */}
                    {isEnabled && !isUnavailable && tool.functions.length > 1 && (() => {
                      const scoped = builtinAllowed[tool.id];
                      const isScopeLimited = scoped !== undefined && scoped !== null;
                      return (
                        <div className="px-3 pb-2.5 pt-0">
                          <button
                            type="button"
                            data-testid={`builtin-tool-scope-${tool.id}`}
                            className="flex items-center gap-1.5 rounded-sm px-2 py-1 content-caption font-medium transition-colors w-full justify-center bg-feedback-alert-primary/10 text-feedback-warning-tertiary border border-feedback-alert-primary/20 hover:bg-feedback-alert-primary/15 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
                            onClick={(e) => { e.stopPropagation(); onOpenToolScope({ type: "builtin", id: tool.id, label: tool.label }); }}
                          >
                            <Filter className="h-3 w-3" />
                            {isScopeLimited
                              ? t_i18n("{selected} of {count, plural, one {# function} other {# functions}} selected", { selected: scoped.length, count: tool.functions.length })
                              : t_i18n("All functions")}
                          </button>
                        </div>
                      );
                    })()}
                  </div>
                );
              })}
            </div>
          ) : null}

          {/* Work item peers: the roster of agents this agent may
              assign work items to. Shown only while editing and only when the
              Work Items tool is enabled — a different concept from handover. */}
          {editingBuiltinTools && carriesBuiltin(builtinToolIds, "coordination") && (
            <div className="mt-3 rounded-sm border border-filigran-ia-primary/20 bg-filigran-ia-primary/5 p-3">
              <div className="flex items-center gap-2 mb-1">
                <ListTodo className="h-4 w-4 text-filigran-ia-primary light:text-filigran-ia-primary" />
                <p className="content-compact font-medium">{t_i18n("Work item peers")}</p>
              </div>
              <p className="content-caption text-default-secondary mb-2.5">
                {t_i18n("Agents this agent can assign work items to (the work item owner). The list is shown to the agent so it knows who exists; an owner outside it is rejected. Leave empty to only create items for its own backlog.")}
              </p>
              {(() => {
                const candidates = (allAgents ?? []).filter((a) => a.id !== agent.id);
                if (candidates.length === 0) {
                  return <p className="content-caption text-default-secondary italic">{t_i18n("No other agents available to assign work items to.")}</p>;
                }
                return (
                  <div className="grid gap-1.5 sm:grid-cols-2">
                    {candidates.map((a) => {
                      const selected = workItemOwnerIds.includes(a.id);
                      return (
                        <button
                          key={a.id}
                          type="button"
                          onClick={() => onToggleWorkItemOwner(a.id)}
                          className={cn(
                            "flex items-center gap-2 rounded-sm border px-2.5 py-2 text-left transition-colors",
                            selected ? "border-filigran-ia-primary/40 bg-filigran-ia-primary/10" : "border-elevation-subtle-soft bg-elevation-hover hover:border-elevation-subtle",
                          )}
                        >
                          <Bot className={cn("h-4 w-4 shrink-0", selected ? "text-filigran-ia-primary light:text-filigran-ia-primary" : "text-default-secondary")} />
                          <span className={cn("content-compact truncate flex-1", selected && "text-filigran-ia-primary light:text-filigran-ia-primary")}>{a.name}</span>
                          {selected ? <CheckCircle2 className="h-3.5 w-3.5 text-filigran-ia-primary shrink-0" /> : <Plus className="h-3.5 w-3.5 text-default-disabled shrink-0" />}
                        </button>
                      );
                    })}
                  </div>
                );
              })()}
            </div>
          )}

          {editingBuiltinTools && readsOtherAgentsTraces(builtinToolIds, builtinAllowed) && (
            <div data-testid="trace-supervision-panel" className="mt-3 rounded-sm border border-accent/20 bg-accent/5 p-3">
              <div className="flex items-center gap-2 mb-1">
                <Activity className="h-4 w-4 text-accent" />
                <p className="content-compact font-medium">{t_i18n("Trace supervision allow-list")}</p>
              </div>
              <p className="content-caption text-default-secondary mb-2.5">
                {t_i18n("Target agents this agent can read the traces of, through Traces, for non-admin users. Admin users can read the traces of any visible agent.")}
              </p>
              {(() => {
                const candidates = (allAgents ?? []).filter((a) => a.id !== agent.id);
                if (candidates.length === 0) {
                  return <p className="content-caption text-default-secondary italic">{t_i18n("No other agents available for supervision.")}</p>;
                }
                return (
                  <div className="grid gap-1.5 sm:grid-cols-2">
                    {candidates.map((a) => {
                      const selected = traceSupervisionTargetIds.includes(a.id);
                      return (
                        <button
                          key={a.id}
                          type="button"
                          onClick={() => onToggleTraceSupervisionTarget(a.id)}
                          className={cn(
                            "flex items-center gap-2 rounded-sm border px-2.5 py-2 text-left transition-colors",
                            selected ? "border-accent/40 bg-accent/10" : "border-elevation-subtle-soft bg-elevation-hover hover:border-elevation-subtle",
                          )}
                        >
                          <Bot className={cn("h-4 w-4 shrink-0", selected ? "text-accent" : "text-default-secondary")} />
                          <span className={cn("content-compact truncate flex-1", selected && "text-accent")}>{a.name}</span>
                          {selected ? <CheckCircle2 className="h-3.5 w-3.5 text-accent shrink-0" /> : <Plus className="h-3.5 w-3.5 text-default-disabled shrink-0" />}
                        </button>
                      );
                    })}
                  </div>
                );
              })()}
            </div>
          )}

          {/* Coordination access: which agents read and change the
              company-managed items and entries this agent governs. Only a
              company-managed agent governs fleet rows. */}
          {editingBuiltinTools && companyManaged && builtinToolIds.some(isCoordinationBuiltinTool) && (
            <CoordinationAccessPanel
              agent={agent}
              allAgents={allAgents}
              draft={coordinationDraft}
              onChange={onChangeCoordination}
            />
          )}

          {!editingBuiltinTools && (
            <div>
              {(agent.builtin_tool_ids || []).length > 0 && !builtinToolsReady ? (
                <BuiltinToolsPending
                  isError={builtinToolsFailed}
                  variant="chips"
                  chipShape="pill"
                  count={Math.min((agent.builtin_tool_ids || []).length, 8)}
                  testId="builtin-tool-badges"
                />
              ) : (agent.builtin_tool_ids || []).length > 0 ? (
                <div className="flex flex-wrap gap-2">
                  {/* Former ids collapse into their group's card. */}
                  {canonicalBuiltinIds(agent.builtin_tool_ids)
                    .map(describeBuiltinTool)
                    .sort((a, b) => a.label.localeCompare(b.label))
                    .map((tool) => {
                    const IconComp = tool.icon;
                    const isUnavailable = !tool.available;
                    const isCoord = isCoordinationBuiltinTool(tool.id);
                    return (
                      <Tip key={tool.id} content={isUnavailable ? t_i18n("Unavailable on this deployment: it stays inactive until an administrator enables it.") : undefined}>
                        <Badge
                          data-testid={`builtin-tool-badge-${tool.id}`}
                          tabIndex={isUnavailable ? 0 : undefined}
                          variant={isUnavailable || isCoord ? "outline" : "cyan"}
                          className={cn(
                            "content-compact gap-1.5 py-1",
                            isUnavailable && "opacity-60",
                            isCoord && !isUnavailable && "border-filigran-ia-primary/30 bg-filigran-ia-primary/10 text-filigran-ia-primary light:border-filigran-ia-primary/40 light:bg-filigran-ia-primary/10 light:text-filigran-ia-primary",
                          )}
                        >
                          <IconComp className="h-3 w-3" />
                          {tool.label}
                          {isUnavailable && <span className="text-[8px] text-feedback-warning-primary ml-0.5">{t_i18n("Unavailable")}</span>}
                          {(() => {
                            const scoped = (agent.builtin_allowed_tools || {})[tool.id];
                            return scoped !== undefined && scoped !== null ? (
                              <span data-testid={`builtin-tool-scoped-${tool.id}`} className="ml-0.5 text-[9px] font-medium text-feedback-warning-tertiary">
                                {t_i18n("{count, plural, one {# function} other {# functions}}", { count: scoped.length })}
                              </span>
                            ) : null;
                          })()}
                        </Badge>
                      </Tip>
                    );
                  })}
                </div>
              ) : (
                <p className="content-base text-default-secondary italic py-3 text-center">{t_i18n("No built-in tools enabled")}</p>
              )}
              {carriesBuiltin(agent.builtin_tool_ids, "coordination") && (agent.work_item_owner_agent_ids || []).length > 0 && (
                <p className="mt-2 content-caption text-default-secondary">
                  <span className="text-default-secondary">{t_i18n("Work item peers:")} </span>
                  {(agent.work_item_owner_agent_ids || [])
                    .map((id) => (allAgents ?? []).find((a) => a.id === id)?.name ?? t_i18n("agent"))
                    .join(", ")}
                </p>
              )}
              {agent.is_company_managed && (agent.builtin_tool_ids || []).some(isCoordinationBuiltinTool) && (
                <CoordinationAccessSummary access={agent.coordination_access} allAgents={allAgents} />
              )}
              {readsOtherAgentsTraces(agent.builtin_tool_ids || [], agent.builtin_allowed_tools) && (agent.trace_supervision_target_agent_ids || []).length > 0 && (
                <p className="mt-2 content-caption text-default-secondary">
                  <span className="text-default-secondary">{t_i18n("Trace supervision targets:")} </span>
                  {(agent.trace_supervision_target_agent_ids || [])
                    .map((id) => (allAgents ?? []).find((a) => a.id === id)?.name ?? t_i18n("agent"))
                    .join(", ")}
                </p>
              )}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Custom Tools */}
      {customToolsEnabled && (
      <Card>
        <CardHeader className="flex-row items-center justify-between space-y-0 p-4 pb-3">
          <CardTitle className="content-base flex items-center gap-2">
            <Wrench className="h-4 w-4 text-accent" /> {t_i18n("Custom Tools")}
            {(agent.custom_tool_ids || []).length > 0 && !editingCustomTools && (
              <CountBadge variant="secondary">{(agent.custom_tool_ids || []).length}</CountBadge>
            )}
          </CardTitle>
          <CardEditActions editing={editingCustomTools} canEdit={canManage} onEdit={onStartEditCustomTools} onCancel={onCancelCustomTools} onSave={onSaveCustomTools} saveDisabled={isPending} />
        </CardHeader>
        <CardContent className="px-4 pb-4 pt-0">
          {editingCustomTools ? (
            <>
              {customTools && customTools.filter((t) => t.enabled).length > 3 && (
                <SearchInput placeholder={t_i18n("Filter custom tools...")} value={customToolSearch} onChange={setCustomToolSearch} className="mb-3 max-w-full" />
              )}
              {(() => {
                const enabledTools = (customTools ?? [])
                  .filter((t) => {
                    if (!t.enabled) return false;
                    if (customToolSearch) {
                      const q = customToolSearch.toLowerCase();
                      return t.name.toLowerCase().includes(q) || (t.description ?? "").toLowerCase().includes(q);
                    }
                    return true;
                  })
                  .sort((a, b) => a.name.localeCompare(b.name));
                return enabledTools.length > 0 ? (
                <div className="grid gap-2 sm:grid-cols-3">
                  {enabledTools.map((tool) => {
                    const isAttached = customToolIds.includes(tool.id);
                    const owners = isAttached ? skillOwnedTools[tool.id] : undefined;
                    const isSkillOwned = !!owners?.length;
                    // Packaged, but no attached skill carries it — attached or not.
                    // The toggle is dead either way (the reconciliation binds it
                    // only with its skill and strips it without), including in the
                    // legacy state where the tool is bound but the skill is not.
                    const carriers = isSkillOwned ? undefined : skillCarriedTools[tool.id];
                    const needsItsSkill = !!carriers?.length;
                    const isLocked = isSkillOwned || needsItsSkill;
                    return (
                      <Tip
                        key={tool.id}
                        content={packageOwnerHint(owners, t_i18n) ?? packageCarrierHint(carriers, t_i18n)}
                      >
                      <div className={cn(
                        "rounded-sm border transition-all",
                        isLocked && "cursor-not-allowed opacity-75",
                        isAttached ? "border-accent/40 bg-accent/5" : "border-elevation-subtle-soft bg-elevation-hover hover:border-elevation-subtle",
                      )}>
                        <button
                          className="flex items-center gap-3 p-3 text-left w-full"
                          onClick={() => { if (!isLocked) onToggleCustomTool(tool.id); }}
                        >
                          <Wrench className={cn("h-5 w-5 shrink-0", isAttached ? "text-accent" : "text-default-secondary")} />
                          <div className="min-w-0 flex-1">
                            <p className={cn("content-base font-medium truncate", isAttached && "text-accent")}>{tool.name}</p>
                            <p className="content-caption text-default-secondary">{tool.description ? tool.description.slice(0, 50) : t_i18n("Custom tool")}</p>
                            {isLocked && <Badge variant="secondary" className="text-[7px] mt-0.5">{t_i18n("Skill")}</Badge>}
                          </div>
                          {isSkillOwned ? (
                            <span className="text-[8px] text-accent shrink-0">{t_i18n("Via skill")}</span>
                          ) : needsItsSkill ? (
                            <span className="text-[8px] text-default-secondary shrink-0">{t_i18n("Needs skill")}</span>
                          ) : isAttached ? (
                            <CheckCircle2 className="h-4 w-4 text-accent shrink-0" />
                          ) : (
                            <Plus className="h-4 w-4 text-default-disabled shrink-0" />
                          )}
                        </button>
                      </div>
                      </Tip>
                    );
                  })}
                </div>
              ) : (
                <div className="py-4 text-center">
                  <Wrench className="h-6 w-6 text-default-disabled mx-auto mb-1.5" />
                  <p className="content-base text-default-secondary">{t_i18n("No custom tools available")}</p>
                  <p className="content-caption text-default-secondary mt-0.5">{t_i18n("Create custom tools in Resources → Tools.")}</p>
                </div>
              );
              })()}
            </>
          ) : (
            <div>
              {(agent.custom_tool_ids || []).length > 0 ? (
                <div className="space-y-1.5">
                  {(agent.custom_tool_ids || [])
                    .map((tid) => ({ tid, tool: customTools?.find((t) => t.id === tid) }))
                    .filter((x): x is { tid: string; tool: NonNullable<typeof x.tool> } => !!x.tool)
                    .sort((a, b) => a.tool.name.localeCompare(b.tool.name))
                    .map(({ tid, tool }) => (
                      <div key={tid} className="flex items-center gap-3 rounded-sm bg-elevation-hover px-3 py-2.5">
                        <Wrench className="h-4 w-4 text-accent shrink-0" />
                        <div className="min-w-0 flex-1">
                          <p className="content-base font-medium">{tool.name}</p>
                          {tool.description && <p className="content-caption text-default-secondary truncate">{tool.description.slice(0, 60)}</p>}
                        </div>
                        {!!skillOwnedTools[tid]?.length && (
                          <Tip content={packageOwnerHint(skillOwnedTools[tid], t_i18n)}>
                            <Badge variant="secondary" className="text-[7px] gap-1 shrink-0"><GraduationCap className="h-2.5 w-2.5" /> {skillOwnedTools[tid].join(", ")}</Badge>
                          </Tip>
                        )}
                      </div>
                    ))}
                </div>
              ) : (
                <p className="content-base text-default-secondary italic py-3 text-center">{t_i18n("No custom tools attached")}</p>
              )}
            </div>
          )}
        </CardContent>
      </Card>
      )}
    </>
  );
}
