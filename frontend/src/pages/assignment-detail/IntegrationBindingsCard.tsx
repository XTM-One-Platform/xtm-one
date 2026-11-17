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

import { useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Plug, Filter, Link2, Unlink, CheckCircle2,
  ArrowDownFromLine, Users,
} from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import { Tip } from "@/components/ui/tooltip";
import { useUpdateAssignment } from "@/hooks/api/useAssignmentApi";
import { usePlatformRegistrations } from "@/hooks/api/usePlatformApi";
import { isA2AProvider } from "@/hooks/api/useA2AApi";
import { useToast } from "@/components/ui/toast";
import { useCredentialSharingConfirm } from "@/hooks/useCredentialSharingConfirm";
import { cn, pluralize, getAgentRequiredTemplateProviders } from "@/lib/utils";
import { effectiveAllowedTools, toolScopeKey } from "@/lib/tool-scope";
import { INTEGRATION_PROVIDER_NAMES, providerLogoSrc } from "@/lib/constants";
import { useFormatter } from "@/i18n";
import type { Assignment, Agent, Integration } from "@/types";
import { AssignmentToolScopeLoader } from "./AssignmentToolScopeLoader";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { CardEditActions } from "@/components/ui/card-edit-actions";
import { SearchableSelect } from "@/components/ui/searchable-select";

/** Platform picker value for "every platform of the provider". */
const ALL_PLATFORMS = "__all__";

// ---------------------------------------------------------------------------
// IntegrationBindingsCard — Assignment integration bindings (edit + read-only)
// ---------------------------------------------------------------------------

interface IntegrationBindingsCardProps {
  assignment: Assignment;
  agent: Agent | undefined;
  allIntegrations: Integration[] | undefined;
  canManage: boolean;
  onUpdate: () => void;
}

export function IntegrationBindingsCard({
  assignment, agent, allIntegrations, canManage, onUpdate,
}: IntegrationBindingsCardProps) {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const { toast } = useToast();
  const update = useUpdateAssignment();
  const aiOff = useAiActionDisabled();
  const [confirmSharingElement, confirmSharing] = useCredentialSharingConfirm();

  const [editing, setEditing] = useState(false);
  const [integrationBindings, setIntegrationBindings] = useState<Record<string, string[]> | null>(null);
  const [integAllowedTools, setIntegAllowedTools] = useState<Record<string, string[] | null> | null>(null);
  const [customIntegScope, setCustomIntegScope] = useState(false);
  const [toolScopeDialog, setToolScopeDialog] = useState<{ integId: string; provider: string; label: string } | null>(null);

  // Agent's bound integrations (resolved to actual integration objects)
  const agentIntegrations = allIntegrations?.filter((i) =>
    agent?.integration_bindings && Object.values(agent.integration_bindings).flat().includes(i.id)
  ) ?? [];

  // The agent's dynamic (per-user) template bindings — the only providers this
  // assignment inherits dynamically. Both the scoped and per-user buckets below
  // are derived from THIS agent's required providers so unrelated platform-scoped
  // integrations elsewhere on the instance never leak into the assignment card.
  const { data: platformRegistrations } = usePlatformRegistrations();
  const agentTemplateProviders = getAgentRequiredTemplateProviders(agent);
  // Platform scope: the agent's template providers whose integration is resolved
  // from a platform registration (integration scope_config.resolve_from_platform).
  const scopedTemplateProviders = agentTemplateProviders.filter((provider) =>
    allIntegrations?.some((i) => i.provider === provider && i.scope_config?.resolve_from_platform),
  );
  const platformsWithInteg = platformRegistrations?.filter((r) => r.integration_id) ?? [];

  // Per-user dynamic bindings inherited from the agent ("Advanced: Dynamic
  // integrations (per-user)"). These are always resolved from the agent at
  // runtime — assignments cannot override them — so they count toward the
  // agent's configured integrations even when no fixed bindings exist.
  const templateProviders = agentTemplateProviders.filter((p) => !scopedTemplateProviders.includes(p));
  const hasAgentIntegrations =
    agentIntegrations.length > 0 || scopedTemplateProviders.length > 0 || templateProviders.length > 0;

  const startEditing = () => {
    setIntegrationBindings(assignment.integration_bindings);
    setIntegAllowedTools(assignment.integration_allowed_tools ?? null);
    setCustomIntegScope(assignment.integration_bindings !== null);
    setEditing(true);
  };

  const handleSave = async () => {
    const bindingsToSave = customIntegScope ? (integrationBindings ?? {}) : null;
    const allowedToSave = customIntegScope ? integAllowedTools : null;
    const effective = bindingsToSave ?? agent?.integration_bindings ?? {};
    if (!(await confirmSharing({
      entity_type: "assignment",
      entity_id: assignment.id,
      integration_ids: Object.values(effective).flat(),
    }))) {
      return;
    }
    update.mutate(
      { id: assignment.id, integration_bindings: bindingsToSave, integration_allowed_tools: allowedToSave },
      {
        onSuccess: () => {
          toast({ title: t_i18n("Integration bindings updated"), variant: "success" });
          setEditing(false);
          onUpdate();
        },
      },
    );
  };

  return (
    <>
      {confirmSharingElement}
      <Card className="flex flex-col">
        <CardHeader className="flex-row items-center justify-between space-y-0 p-4 pb-2">
          <CardTitle className="content-base flex items-center gap-2"><Plug className="h-4 w-4 text-accent" /> {t_i18n("Integration Bindings")}</CardTitle>
          <CardEditActions
            editing={editing}
            canEdit={canManage}
            aiGated
            onEdit={startEditing}
            onCancel={() => setEditing(false)}
            onSave={handleSave}
            saveDisabled={update.isPending || aiOff}
          />
        </CardHeader>
        <CardContent className="px-4 pb-4 pt-0 flex-1">
          {editing ? (
            <div className="space-y-4">
              {hasAgentIntegrations ? (
                <>
                  {/* Inherit / Custom scope toggle */}
                  <div className="flex items-center justify-between rounded-sm bg-elevation-hover px-3 py-2.5">
                    <div className="flex items-center gap-2">
                      <ArrowDownFromLine className="h-3.5 w-3.5 text-accent" />
                      <div>
                        <p className="content-compact font-medium">{t_i18n("Custom scope")}</p>
                        <p className="text-[9px] text-default-secondary">
                          {customIntegScope
                            ? t_i18n("Manually select integrations and tools for this assignment")
                            : t_i18n("Inheriting all integrations and tools from the parent agent")}
                        </p>
                      </div>
                    </div>
                    <Switch aria-label={t_i18n("Custom scope")} checked={customIntegScope} onCheckedChange={(v) => {
                      setCustomIntegScope(v);
                      if (v) {
                        setIntegrationBindings({ ...(agent?.integration_bindings ?? {}) });
                        setIntegAllowedTools(null);
                      } else {
                        setIntegrationBindings(null);
                        setIntegAllowedTools(null);
                      }
                    }} />
                  </div>

                  {!customIntegScope ? (
                    /* Inherit mode: show agent integrations as read-only */
                    <div className="space-y-2">
                      <p className="content-caption text-default-secondary">
                        {t_i18n('This assignment uses all integrations and tools configured on the parent agent. Toggle "Custom scope" to restrict access.')}
                      </p>
                      {/* Scoped template bindings */}
                      {scopedTemplateProviders.map((provider) => (
                        <div key={`scope-inherit-${provider}`} className="flex items-center gap-3 rounded-sm border border-accent/20 bg-accent/5 px-3 py-2.5">
                          <div className="flex h-8 w-8 items-center justify-center rounded-sm bg-accent/10">
                            <img src={providerLogoSrc(provider)} alt={provider} className="h-4 w-4" />
                          </div>
                          <div className="min-w-0 flex-1">
                            <p className="content-compact font-medium text-accent">{INTEGRATION_PROVIDER_NAMES[provider] ?? provider}</p>
                            <p className="text-[9px] text-default-secondary">{t_i18n("Dynamic (template binding)")}</p>
                          </div>
                          <Tip content={t_i18n("Specifically scoped")}>
                            <Filter className="h-3.5 w-3.5 text-feedback-warning-primary shrink-0" />
                          </Tip>
                          <CheckCircle2 className="h-3.5 w-3.5 text-accent shrink-0" />
                        </div>
                      ))}
                      {/* Per-user dynamic bindings (always inherited from agent) */}
                      {templateProviders.map((provider) => (
                        <div key={`tmpl-inherit-${provider}`} className="flex items-center gap-3 rounded-sm border border-filigran-ia-primary/20 bg-filigran-ia-primary/5 px-3 py-2.5">
                          <div className="flex h-8 w-8 items-center justify-center rounded-sm bg-filigran-ia-primary/10">
                            <img src={providerLogoSrc(provider)} alt={provider} className="h-4 w-4" />
                          </div>
                          <div className="min-w-0 flex-1">
                            <p className="content-compact font-medium text-filigran-ia-primary">{INTEGRATION_PROVIDER_NAMES[provider] ?? provider}</p>
                            <p className="text-[9px] text-default-secondary">{t_i18n("Resolved per user at runtime")}</p>
                          </div>
                          <span className="flex items-center gap-1 text-[9px] text-filigran-ia-primary bg-filigran-ia-primary/10 rounded-sm px-1.5 py-0.5">
                            <Users className="h-3 w-3" /> {t_i18n("Per-user")}
                          </span>
                        </div>
                      ))}
                      {agentIntegrations.map((integ) => {
                        const agentAllowed = effectiveAllowedTools(agent?.integration_allowed_tools, integ);
                        const isScopeLimited = agentAllowed !== undefined && agentAllowed !== null;
                        // An A2A binding carries no selectable tools in either
                        // direction, so neither "All tools" nor a count is a
                        // true thing to say about it.
                        const isA2A = isA2AProvider(integ.provider);
                        return (
                          <div key={integ.id} className="flex items-center gap-3 rounded-sm border border-accent/20 bg-accent/5 px-3 py-2.5">
                            <div className="flex h-8 w-8 items-center justify-center rounded-sm bg-accent/10">
                              <img src={providerLogoSrc(integ.provider)} alt={integ.provider} className="h-4 w-4" />
                            </div>
                            <div className="min-w-0 flex-1">
                              <p className="content-compact font-medium text-accent">{integ.name}</p>
                              <p className="text-[9px] text-default-secondary">{INTEGRATION_PROVIDER_NAMES[integ.provider] ?? integ.provider}</p>
                            </div>
                            <div className="flex items-center gap-1.5">
                              {!isA2A && (
                                <span className="text-[9px] text-feedback-warning-tertiary bg-feedback-alert-primary/10 rounded-sm px-1.5 py-0.5">
                                  {isScopeLimited ? t_i18n("{count} {noun} (agent)", { count: agentAllowed.length, noun: t_i18n(pluralize(agentAllowed.length, "tool")) }) : t_i18n("All tools")}
                                </span>
                              )}
                              <CheckCircle2 className="h-3.5 w-3.5 text-accent shrink-0" />
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  ) : (
                    /* Custom scope mode: toggle integrations + tool scope per integration */
                    <div className="space-y-2">
                      <p className="content-caption text-default-secondary">
                        {t_i18n("Select which of the agent's integrations this assignment can use. Click the tool scope button to restrict individual tools.")}
                      </p>
                      {/* Scoped template bindings — platform selector */}
                      {scopedTemplateProviders.map((provider) => {
                        const boundPlatformId = integrationBindings?.[provider]?.[0] ?? null;
                        const providerPlatforms = platformsWithInteg.filter((r) => r.platform_identifier === provider || (r.platform_identifier === "openbas" && provider === "openaev"));
                        return (
                          <div key={`scope-custom-${provider}`} className="rounded-sm border border-feedback-alert-primary/30 bg-feedback-alert-primary/5 px-3 py-2.5">
                            <div className="flex items-center gap-3">
                              <div className="flex h-8 w-8 items-center justify-center rounded-sm bg-feedback-alert-primary/10">
                                <img src={providerLogoSrc(provider)} alt={provider} className="h-4 w-4" />
                              </div>
                              <div className="min-w-0 flex-1">
                                <p className="content-compact font-medium text-feedback-warning-primary">{INTEGRATION_PROVIDER_NAMES[provider] ?? provider}</p>
                                <p className="text-[9px] text-default-secondary">{t_i18n("Scoped: select a platform to pin")}</p>
                              </div>
                              {providerPlatforms.length > 1 ? (
                                <SearchableSelect
                                  aria-label={t_i18n("Platform")}
                                  searchable="auto"
                                  className="w-48 shrink-0"
                                  options={providerPlatforms.map((reg) => ({ value: reg.integration_id!, label: reg.platform_title }))}
                                  pinned={[{ value: ALL_PLATFORMS, label: t_i18n("All platforms") }]}
                                  value={boundPlatformId ?? ALL_PLATFORMS}
                                  onValueChange={(val) => {
                                    const next = { ...(integrationBindings ?? {}) };
                                    if (val !== ALL_PLATFORMS) {
                                      next[provider] = [val];
                                    } else {
                                      delete next[provider];
                                    }
                                    setIntegrationBindings(next);
                                  }}
                                />
                              ) : (
                                <Tip content={t_i18n("Specifically scoped")}>
                                  <Filter className="h-3.5 w-3.5 text-feedback-warning-primary shrink-0" />
                                </Tip>
                              )}
                            </div>
                          </div>
                        );
                      })}
                      {/* Per-user dynamic bindings — always inherited, not scopable per assignment */}
                      {templateProviders.map((provider) => (
                        <div key={`tmpl-custom-${provider}`} className="rounded-sm border border-filigran-ia-primary/30 bg-filigran-ia-primary/5 px-3 py-2.5">
                          <div className="flex items-center gap-3">
                            <div className="flex h-8 w-8 items-center justify-center rounded-sm bg-filigran-ia-primary/10">
                              <img src={providerLogoSrc(provider)} alt={provider} className="h-4 w-4" />
                            </div>
                            <div className="min-w-0 flex-1">
                              <p className="content-compact font-medium text-filigran-ia-primary">{INTEGRATION_PROVIDER_NAMES[provider] ?? provider}</p>
                              <p className="text-[9px] text-default-secondary">{t_i18n("Always inherited: resolved per user at runtime")}</p>
                            </div>
                            <span className="flex items-center gap-1 text-[9px] text-filigran-ia-primary bg-filigran-ia-primary/10 rounded-sm px-1.5 py-0.5 shrink-0">
                              <Users className="h-3 w-3" /> {t_i18n("Per-user")}
                            </span>
                          </div>
                        </div>
                      ))}
                      {agentIntegrations.map((integ) => {
                        const providerKey = Object.entries(agent?.integration_bindings ?? {}).find(
                          ([, v]) => (v || []).includes(integ.id)
                        )?.[0] ?? integ.provider;
                        const isBound = integrationBindings !== null && Object.values(integrationBindings).flat().includes(integ.id);
                        const scopedInteg = { id: integ.id, provider: providerKey };
                        const wfScopedTools = effectiveAllowedTools(integAllowedTools, scopedInteg);
                        const isScopeLimited = isBound && wfScopedTools !== undefined && wfScopedTools !== null;
                        const agentAllowed = effectiveAllowedTools(agent?.integration_allowed_tools, scopedInteg);
                        const isAgentScoped = agentAllowed !== undefined && agentAllowed !== null;
                        const isA2A = isA2AProvider(integ.provider);
                        return (
                          <div key={integ.id} className={cn(
                            "rounded-sm border transition-all",
                            isBound ? "border-accent/40 bg-accent/5" : "border-elevation-subtle-soft opacity-60",
                          )}>
                            <button
                              className="w-full flex items-center gap-3 px-3 py-2.5 text-left"
                              onClick={() => {
                                const next = { ...(integrationBindings ?? {}) };
                                if (isBound) {
                                  const key = Object.entries(next).find(([, v]) => (v || []).includes(integ.id))?.[0];
                                  if (key) {
                                    next[key] = next[key].filter((id) => id !== integ.id);
                                    if (next[key].length === 0) delete next[key];
                                  }
                                  if (integAllowedTools) {
                                    const newAllowed = { ...integAllowedTools };
                                    // An OpenAPI integration's scope is its own (lib/tool-scope.ts).
                                    delete newAllowed[toolScopeKey(scopedInteg)];
                                    setIntegAllowedTools(Object.keys(newAllowed).length > 0 ? newAllowed : null);
                                  }
                                } else {
                                  next[providerKey] = [...(next[providerKey] || []), integ.id];
                                }
                                setIntegrationBindings(next);
                              }}
                            >
                              <div className={cn("flex h-8 w-8 items-center justify-center rounded-sm", isBound ? "bg-accent/10" : "bg-elevation-hover")}>
                                <img src={providerLogoSrc(integ.provider)} alt={integ.provider} className="h-4 w-4" />
                              </div>
                              <div className="min-w-0 flex-1">
                                <p className={cn("content-compact font-medium", isBound ? "text-accent" : "")}>{integ.name}</p>
                                <p className="text-[9px] text-default-secondary">{INTEGRATION_PROVIDER_NAMES[integ.provider] ?? integ.provider}</p>
                              </div>
                              {isBound ? (
                                <Link2 className="h-3.5 w-3.5 text-accent shrink-0" />
                              ) : (
                                <Unlink className="h-3.5 w-3.5 text-default-disabled shrink-0" />
                              )}
                            </button>
                            {/* No scope control for an A2A binding: it carries
                                no tools an operator picks from, in either
                                direction. */}
                            {isBound && !isA2A && (
                              <div className="px-3 pb-2.5 pt-0">
                                <button
                                  className="flex items-center gap-1.5 rounded-sm px-2 py-1 content-caption font-medium transition-colors w-full justify-center bg-feedback-alert-primary/10 text-feedback-warning-tertiary border border-feedback-alert-primary/20 hover:bg-feedback-alert-primary/15"
                                  onClick={(e) => { e.stopPropagation(); setToolScopeDialog({ integId: integ.id, provider: providerKey, label: integ.name }); }}
                                >
                                  <Filter className="h-3 w-3" />
                                  {isScopeLimited
                                    ? t_i18n("{count} {noun} selected", { count: wfScopedTools.length, noun: t_i18n(pluralize(wfScopedTools.length, "tool")) })
                                    : isAgentScoped
                                      ? t_i18n("All tools ({count} from agent)", { count: agentAllowed.length })
                                      : t_i18n("All tools")}
                                </button>
                              </div>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  )}
                </>
              ) : (
                <div className="rounded-sm bg-feedback-alert-primary/5 p-3">
                  <p className="content-caption text-feedback-warning-primary">{t_i18n("No integrations bound to the parent agent. Add integrations to the agent first, then bind them to specific assignments here.")}</p>
                </div>
              )}
            </div>
          ) : (
            /* ── Read-only integration bindings ── */
            <div className="space-y-2">
              {(() => {
                const isInheriting = assignment.integration_bindings === null;
                const effectiveBindings = isInheriting
                  ? (agent?.integration_bindings ?? {})
                  : (assignment.integration_bindings ?? {});
                const boundIds = Object.values(effectiveBindings).flat();
                const boundIntegs = allIntegrations?.filter((i) => boundIds.includes(i.id)) ?? [];

                // Per-user template bindings are always inherited from the agent
                // and resolved at runtime, so they count even with no fixed bindings.
                const hasContent =
                  boundIntegs.length > 0 || scopedTemplateProviders.length > 0 || templateProviders.length > 0;

                if (!hasContent && !isInheriting) {
                  return (
                    <div className="flex flex-col items-center justify-center py-8 text-center">
                      <Unlink className="h-8 w-8 text-default-disabled mb-3" />
                      <p className="content-base text-default-secondary">{t_i18n("No integrations bound")}</p>
                      <p className="content-compact text-default-disabled mt-1">{t_i18n("This assignment has no access to integration tools")}</p>
                    </div>
                  );
                }

                if (!hasContent && isInheriting) {
                  return (
                    <div className="flex flex-col items-center justify-center py-8 text-center">
                      <ArrowDownFromLine className="h-8 w-8 text-default-disabled mb-3" />
                      <p className="content-base text-default-secondary">{t_i18n("Inheriting from agent")}</p>
                      <p className="content-compact text-default-disabled mt-1">{t_i18n("No integrations configured on the parent agent")}</p>
                    </div>
                  );
                }

                return (
                  <>
                    {isInheriting && (
                      <div className="flex items-center gap-2 rounded-sm bg-accent/5 border border-accent/20 px-3 py-2 mb-1">
                        <ArrowDownFromLine className="h-3 w-3 text-accent shrink-0" />
                        <p className="content-caption text-accent">{t_i18n("Inheriting all integrations from parent agent")}</p>
                      </div>
                    )}
                    {!isInheriting && agentIntegrations.length > 0 && (
                      <div className="flex items-center gap-2 rounded-sm bg-feedback-alert-primary/5 border border-feedback-alert-primary/20 px-3 py-2 mb-1">
                        <Filter className="h-3 w-3 text-feedback-warning-primary shrink-0" />
                        <p className="content-caption text-feedback-warning-primary">{t_i18n("Custom scope: {bound} of {total} agent {noun}", { bound: boundIntegs.length, total: agentIntegrations.length, noun: t_i18n(pluralize(agentIntegrations.length, "integration")) })}</p>
                      </div>
                    )}
                    {/* Scoped template providers */}
                    {scopedTemplateProviders.map((provider) => {
                      const pinnedId = (assignment.integration_bindings ?? {})[provider]?.[0] ?? null;
                      const pinnedInteg = pinnedId ? allIntegrations?.find((i) => i.id === pinnedId) : null;
                      return (
                        <div key={`scope-ro-${provider}`} className="flex items-center gap-3 rounded-sm bg-elevation-hover px-4 py-3">
                          <div className="flex h-8 w-8 items-center justify-center rounded-sm bg-accent/10">
                            <img src={providerLogoSrc(provider)} alt={provider} className="h-4 w-4" />
                          </div>
                          <div className="min-w-0 flex-1">
                            <p className="content-base font-medium">{INTEGRATION_PROVIDER_NAMES[provider] ?? provider}</p>
                            <p className="content-caption text-default-secondary">
                              {pinnedInteg ? t_i18n("Pinned: {name}", { name: pinnedInteg.name }) : t_i18n("Dynamic (resolved at runtime)")}
                            </p>
                          </div>
                          <Tip content={t_i18n("Specifically scoped")}>
                            <Filter className="h-3.5 w-3.5 text-feedback-warning-primary shrink-0" />
                          </Tip>
                        </div>
                      );
                    })}
                    {/* Per-user dynamic bindings inherited from the agent */}
                    {templateProviders.map((provider) => (
                      <div key={`tmpl-ro-${provider}`} className="flex items-center gap-3 rounded-sm bg-elevation-hover px-4 py-3">
                        <div className="flex h-8 w-8 items-center justify-center rounded-sm bg-filigran-ia-primary/10">
                          <img src={providerLogoSrc(provider)} alt={provider} className="h-4 w-4" />
                        </div>
                        <div className="min-w-0 flex-1">
                          <p className="content-base font-medium">{INTEGRATION_PROVIDER_NAMES[provider] ?? provider}</p>
                          <p className="content-caption text-default-secondary">{t_i18n("Resolved per user at runtime")}</p>
                        </div>
                        <span className="flex items-center gap-1 text-[9px] font-medium text-filigran-ia-primary bg-filigran-ia-primary/10 rounded-sm px-1.5 py-0.5 shrink-0">
                          <Users className="h-3 w-3" /> {t_i18n("Per-user")}
                        </span>
                      </div>
                    ))}
                    {boundIntegs.map((integ) => {
                      const providerKey = Object.entries(effectiveBindings).find(
                        ([, v]) => (v || []).includes(integ.id)
                      )?.[0] ?? integ.provider;
                      const wfScopedTools = effectiveAllowedTools(assignment.integration_allowed_tools, { id: integ.id, provider: providerKey });
                      const agentAllowed = effectiveAllowedTools(agent?.integration_allowed_tools, { id: integ.id, provider: providerKey });
                      // A surviving allow-list on an A2A provider is legacy
                      // data that filters nothing — see `isA2AProvider`.
                      const isA2A = isA2AProvider(integ.provider);
                      return (
                        <button
                          key={integ.id}
                          className="flex items-center gap-3 rounded-sm bg-elevation-hover px-4 py-3 w-full text-left cursor-pointer hover:bg-elevation-highlight transition-colors group"
                          onClick={() => navigate(`/integrations/${integ.id}`)}
                        >
                          <div className="flex h-8 w-8 items-center justify-center rounded-sm bg-accent/10">
                            <img src={providerLogoSrc(integ.provider)} alt={integ.provider} className="h-4 w-4" />
                          </div>
                          <div className="min-w-0 flex-1">
                            <p className="content-base font-medium">{integ.name}</p>
                            <p className="content-caption text-default-secondary">{INTEGRATION_PROVIDER_NAMES[integ.provider] ?? integ.provider}</p>
                          </div>
                          <div className="flex items-center gap-1.5">
                            {!isA2A && wfScopedTools != null && (
                              <span className="text-[9px] text-feedback-warning-tertiary bg-feedback-alert-primary/10 rounded-sm px-1.5 py-0.5">
                                {wfScopedTools.length} {t_i18n(pluralize(wfScopedTools.length, "tool"))}
                              </span>
                            )}
                            {!isA2A && wfScopedTools == null && agentAllowed != null && (
                              <span className="text-[9px] text-feedback-warning-tertiary bg-feedback-alert-primary/10 rounded-sm px-1.5 py-0.5">
                                {t_i18n("{count} {noun} (agent)", { count: agentAllowed.length, noun: t_i18n(pluralize(agentAllowed.length, "tool")) })}
                              </span>
                            )}
                            <Link2 className="h-3.5 w-3.5 text-accent shrink-0 group-hover:text-default-primary transition-colors" />
                          </div>
                        </button>
                      );
                    })}
                  </>
                );
              })()}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Assignment Tool Scope Dialog */}
      {toolScopeDialog && (
        <AssignmentToolScopeLoader
          integrationId={toolScopeDialog.integId}
          sourceLabel={toolScopeDialog.label}
          currentAllowed={integAllowedTools?.[toolScopeKey({ id: toolScopeDialog.integId, provider: toolScopeDialog.provider })] ?? null}
          agentAllowed={effectiveAllowedTools(agent?.integration_allowed_tools, { id: toolScopeDialog.integId, provider: toolScopeDialog.provider })}
          onSave={(allowed) => {
            const key = toolScopeKey({ id: toolScopeDialog.integId, provider: toolScopeDialog.provider });
            setIntegAllowedTools((prev) => {
              const next = { ...(prev ?? {}) };
              if (allowed === null) {
                delete next[key];
              } else {
                next[key] = allowed;
              }
              return Object.keys(next).length > 0 ? next : null;
            });
            setToolScopeDialog(null);
          }}
          onClose={() => setToolScopeDialog(null)}
        />
      )}
    </>
  );
}
