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
import { useNavigate } from "react-router-dom";
import {
  Building2,
  Cog, Sparkles, ChevronLeft, AlertCircle, CheckCircle2,
} from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { SettingsField, settingsFieldHintId } from "@/components/ui/settings-form";
import { Switch } from "@/components/ui/switch";
import { PageSkeleton } from "@/components/ui/skeleton";
import { BackButton } from "@/components/ui/back-button";
import { TemplateGallery } from "@/components/ui/template-gallery";
import { PromptGenerator } from "@/components/ui/prompt-generator";
import { VariableInsertButton } from "@/components/ui/variable-insert-button";
import { TagInput } from "@/components/ui/tag-input";
import { IconPicker } from "@/components/ui/icon-picker";
import { useToast } from "@/components/ui/toast";
import { useAgentTemplates, useCreateAgent } from "@/hooks/api/useAgentApi";
import { useIntegrations, useProviderCatalog } from "@/hooks/api/useIntegrationApi";
import { useAuth } from "@/hooks/useAuth";
import { useCredentialSharingConfirm } from "@/hooks/useCredentialSharingConfirm";
import { cn } from "@/lib/utils";
import { randomId } from "@/lib/random-id";
import { INTEGRATION_TYPE_LABELS, providerLogoSrc } from "@/lib/constants";
import { AgenticGatePage } from "@/components/ui/license-gate-dialog";
import { useAgenticAIEnabled } from "@/hooks/usePlatformConfig";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useFormatter } from "@/i18n";
import type { AgentTemplate } from "@/types";

export default function NewAgent() {
  const aiOff = useAiActionDisabled();
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const { data: templates, isLoading } = useAgentTemplates();
  const { data: integrations } = useIntegrations();
  const { data: catalog } = useProviderCatalog("integration");
  const { isAdmin } = useAuth();
  const create = useCreateAgent();
  const [consentElement, confirmSharing] = useCredentialSharingConfirm();
  const { toast } = useToast();
  const agenticAIEnabled = useAgenticAIEnabled();

  // Pre-generate a stable agent UUID so the backend uses a known ID
  const preGeneratedId = useMemo(() => randomId(), []);

  const [step, setStep] = useState<"select" | "config">("select");
  const [selected, setSelected] = useState<AgentTemplate | null>(null);
  const [name, setName] = useState("");
  const [icon, setIcon] = useState("Bot");
  const [description, setDescription] = useState("");
  const [persona, setPersona] = useState("");
  const [bindings, setBindings] = useState<Record<string, string>>({});
  const [newTags, setNewTags] = useState<string[]>([]);
  const [isCompanyManaged, setIsCompanyManaged] = useState(false);
  // Organization context (#11533): on by default (Inherit the platform); unchecked stores Off,
  // which removes the summary and the get_organization_context tool from this agent.
  const [organizationContext, setOrganizationContext] = useState(true);
  const personaRef = useRef<HTMLTextAreaElement>(null);

  if (!agenticAIEnabled || aiOff) return <AgenticGatePage feature="Creating agents" />;
  if (isLoading) return <PageSkeleton cards={6} />;

  // Find connected integrations grouped by type (exclude bot/channel providers)
  const connectedByProvider = integrations?.filter((i) => i.status === "connected") || [];
  const getProvidersForType = (type: string) => {
    return catalog?.filter((c) => c.integration_types.includes(type)) || [];
  };
  const getConnectedForType = (type: string) => {
    const providerKeys = getProvidersForType(type).map((p) => p.provider);
    return connectedByProvider.filter((i) => providerKeys.includes(i.provider));
  };

  const handleSelect = (tmpl: AgentTemplate | null) => {
    setSelected(tmpl);
    if (tmpl) { setName(tmpl.name); setIcon(tmpl.icon || "Bot"); setDescription(tmpl.description || ""); setPersona(tmpl.default_persona || ""); }
    else { setName(""); setIcon("Bot"); setDescription(""); setPersona(""); }
    setBindings({});
    setStep("config");
  };

  const handleCreate = async () => {
    // Build integration_bindings from type selections
    const integrationBindings: Record<string, string[]> = {};
    for (const [_type, integrationId] of Object.entries(bindings)) {
      if (integrationId) {
        const integ = connectedByProvider.find((i) => i.id === integrationId);
        if (integ) integrationBindings[integ.provider] = [...(integrationBindings[integ.provider] || []), integrationId];
      }
    }
    // A company-managed agent lends what it is bound to, and its persona's
    // variables, to every user.
    if (isCompanyManaged && !(await confirmSharing(
      { entity_type: "agent", change: { name, is_company_managed: true, integration_bindings: integrationBindings, persona } },
      { confirmLabel: t_i18n("Create") },
    ))) {
      return;
    }

    create.mutate(
      {
        id: preGeneratedId,
        template_slug: selected?.slug,
        name,
        icon,
        description: description || undefined,
        persona: persona || undefined,
        tags: newTags.length > 0 ? newTags : undefined,
        is_company_managed: isCompanyManaged || undefined,
        organization_context_mode: organizationContext ? undefined : "off",
        disable_chat: selected?.disable_chat || undefined,
        create_default_assignments: !!selected,
        integration_bindings: Object.keys(integrationBindings).length > 0 ? integrationBindings : undefined,
      },
      {
        onSuccess: (agent) => {
          toast({ title: t_i18n("Agent created"), variant: "success" });
          navigate(`/agents/${agent.id}`);
        },
      },
    );
  };

  const requiredTypes = selected?.required_integration_types || [];

  if (step === "config") {
    return (
      <div className="space-y-6">
        {consentElement}
        <Button variant="ghost" size="sm" className="gap-1.5 text-default-secondary" onClick={() => setStep("select")}>
          <ChevronLeft className="h-4 w-4" /> {t_i18n("Back to Templates")}
        </Button>
        <div>
          <h1 className="text-2xl font-bold">{selected ? t_i18n("New {name} Agent", { name: selected.name }) : t_i18n("New Custom Agent")}</h1>
          <p className="mt-2 text-default-secondary">{selected ? selected.description : t_i18n("Create an empty agent and configure everything manually.")}</p>
        </div>

        <Card>
          <CardContent className="pt-6 space-y-5">
            <div className="grid gap-4 sm:grid-cols-3">
              <SettingsField id="new-agent-name" label={t_i18n("Agent Name")} required helper={t_i18n("Shown in chat, lists and fleets. The icon is optional.")}>
                <div className="flex items-center gap-2">
                  <IconPicker value={icon} onChange={setIcon} />
                  <Input id="new-agent-name" aria-label={t_i18n("Agent Name")} aria-describedby={settingsFieldHintId("new-agent-name")} className="flex-1" value={name} onChange={(e) => setName(e.target.value)} placeholder={t_i18n("e.g. Email Assistant")} />
                </div>
              </SettingsField>
              <SettingsField id="new-agent-description" className="sm:col-span-2" label={t_i18n("Description (optional)")} helper={t_i18n("One line on what it does, shown in lists and to other agents that hand work to it.")}>
                <Input id="new-agent-description" aria-label={t_i18n("Description (optional)")} aria-describedby={settingsFieldHintId("new-agent-description")} value={description} onChange={(e) => setDescription(e.target.value)} placeholder={t_i18n("What does this agent do?")} />
              </SettingsField>
            </div>
            <SettingsField id="new-agent-tags" label={t_i18n("Tags")} helper={t_i18n("Words to find and group it in lists. Press Enter after each tag.")}>
              <TagInput id="new-agent-tags" aria-label={t_i18n("Tags")} aria-describedby={settingsFieldHintId("new-agent-tags")} value={newTags} onChange={setNewTags} placeholder={t_i18n("Add tags…")} />
            </SettingsField>
            <div className="space-y-1.5">
              <div className="flex items-center justify-between">
                <Label htmlFor="new-agent-persona" className="block text-default-primary">{t_i18n("Persona / System Prompt")}</Label>
                <div className="flex items-center gap-2">
                  <VariableInsertButton textareaRef={personaRef} value={persona} onChange={setPersona} />
                  <PromptGenerator contextType="persona" currentText={persona} onGenerated={setPersona} agentName={name} />
                </div>
              </div>
              <p id="new-agent-persona-hint" className="content-compact text-default-secondary">{t_i18n("Who the agent is and how it behaves, read at the start of every run. It can be edited later.")}</p>
              <textarea id="new-agent-persona" aria-describedby="new-agent-persona-hint" ref={personaRef} className="flex min-h-[120px] w-full rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base placeholder:text-default-secondary focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus" placeholder={t_i18n("Describe who you are and how the agent should behave...")} value={persona} onChange={(e) => setPersona(e.target.value)} />
            </div>

            {/* Integration type bindings */}
            {requiredTypes.length > 0 && (
              <div className="space-y-3">
                <div className="flex items-center gap-2">
                  <Cog className="h-4 w-4 text-default-secondary" />
                  <span className="content-base font-medium">{t_i18n("Required Integrations")}</span>
                </div>
                <p className="content-compact text-default-secondary">{t_i18n("Select which connected integration to use for each requirement. You can skip and configure later.")}</p>

                {requiredTypes.map((type) => {
                  const connected = getConnectedForType(type);
                  const providers = getProvidersForType(type);
                  const hasConnected = connected.length > 0;

                  return (
                    <div key={type} className="flex items-center gap-3 rounded-sm bg-elevation-hover p-3">
                      <div className="flex-1 min-w-0 space-y-2">
                        <div className="flex items-center gap-2">
                          <span className="content-compact font-medium">{t_i18n(INTEGRATION_TYPE_LABELS[type] || type)}</span>
                          <span className="content-compact text-default-secondary">
                            ({providers.map((p) => p.name).join(", ")})
                          </span>
                        </div>

                      {hasConnected ? (
                        <div className="flex flex-wrap gap-2">
                          {connected.map((integ) => (
                            <button
                              key={integ.id}
                              className={cn(
                                "flex items-center gap-2 rounded-sm border px-3 py-2 content-compact font-medium transition-colors",
                                bindings[type] === integ.id ? "border-accent/40 bg-accent/10 text-accent" : "border-elevation-subtle-soft bg-elevation-hover text-default-secondary hover:border-elevation-subtle",
                              )}
                              onClick={() => setBindings({ ...bindings, [type]: bindings[type] === integ.id ? "" : integ.id })}
                            >
                              <img src={providerLogoSrc(integ.provider)} alt="" className="h-4 w-4" />
                              {integ.name}
                            </button>
                          ))}
                        </div>
                      ) : (
                        <div className="flex items-center gap-2 content-compact text-feedback-warning-primary">
                          <AlertCircle className="h-3.5 w-3.5" />
                          <span>{t_i18n("No connected {type} integration.", { type: INTEGRATION_TYPE_LABELS[type]?.toLowerCase() || type })} <button className="text-accent hover:underline" onClick={() => navigate("/integrations/new")}>{t_i18n("Connect one")}</button></span>
                        </div>
                      )}
                      </div>
                      <div className="shrink-0">
                        {bindings[type] ? (
                          <CheckCircle2 className="h-3.5 w-3.5 text-feedback-success-primary" />
                        ) : (
                          <Badge variant="outline" className="text-[8px]">{t_i18n("optional")}</Badge>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}

            {selected && (
              <div className="rounded-sm bg-accent/5 p-3">
                <p className="content-compact text-accent font-medium"><Sparkles className="h-3 w-3 inline mr-1" />{t_i18n("{count, plural, one {Includes # pre-configured assignment} other {Includes # pre-configured assignments}}", { count: selected.assignment_count })}</p>
              </div>
            )}

            {/* Organization context (#11533) */}
            <div className="flex items-center justify-between rounded-sm bg-elevation-hover p-3" data-testid="new-agent.organization-context">
              <div className="flex items-center gap-2">
                <Building2 className="h-4 w-4 text-accent" />
                <div>
                  <p className="content-base font-medium">{t_i18n("Organization context")}</p>
                  <p className="content-compact text-default-secondary">
                    {t_i18n("The agent knows your organization: a short profile in its prompt and tools to read the rest.")}
                  </p>
                </div>
              </div>
              <Switch aria-label={t_i18n("Organization context")} checked={organizationContext} onCheckedChange={setOrganizationContext} />
            </div>

            {/* Company-managed toggle (admin only) — last before actions */}
            {isAdmin && (
              <div className="flex items-center justify-between rounded-sm bg-elevation-hover p-3">
                <div className="flex items-center gap-2">
                  <Building2 className="h-4 w-4 text-accent" />
                  <div>
                    <p className="content-base font-medium">{t_i18n("Company-managed agent")}</p>
                    <p className="content-compact text-default-secondary">{t_i18n("Shared with all users. Only admins can edit.")}</p>
                  </div>
                </div>
                <Switch aria-label={t_i18n("Company-managed agent")} checked={isCompanyManaged} onCheckedChange={setIsCompanyManaged} />
              </div>
            )}

            <div className="flex justify-end gap-3">
              <Button variant="ghost" onClick={() => navigate("/agents")}>{t_i18n("Cancel")}</Button>
              <Button onClick={handleCreate} disabled={!name.trim() || create.isPending || aiOff}>{create.isPending ? t_i18n("Creating...") : t_i18n("Create Agent")}</Button>
            </div>
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <BackButton to="/agents/new" label="Back to Creation Modes" />
      <div>
        <h1 className="text-2xl font-bold">{t_i18n("Create New Agent")}</h1>
        <p className="mt-2 text-default-secondary">{t_i18n("Choose a template or start from scratch.")}</p>
      </div>

      <TemplateGallery templates={templates} onSelect={handleSelect} />
    </div>
  );
}
