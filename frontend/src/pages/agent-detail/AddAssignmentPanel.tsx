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

import { useId, useMemo, useState } from "react";
import { Zap, CheckCircle2, Filter } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { userSelectOption } from "@/components/ui/entity-select-options";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { PromptGenerator } from "@/components/ui/prompt-generator";
import { useToast } from "@/components/ui/toast";

import { useIntegrations, useProviderCatalog } from "@/hooks/api/useIntegrationApi";
import { usePlatformRegistrations } from "@/hooks/api/usePlatformApi";
import { useCreateAssignment } from "@/hooks/api/useAssignmentApi";
import { useUsers } from "@/hooks/api/useAuthApi";
import { useAuth } from "@/hooks/useAuth";
import { Tip } from "@/components/ui/tooltip";
import { withBoundEntities } from "@/lib/entity-list-scope";
import { cn } from "@/lib/utils";
import { TRIGGER_TYPES, TRIGGER_ICONS, TRIGGER_LABELS, CRON_PRESETS, INTEGRATION_PROVIDER_NAMES, providerLogoSrc } from "@/lib/constants";
import { useFormatter } from "@/i18n";
import type { Agent, TriggerConfig, TriggerType } from "@/types";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { AiDisabledTip } from "@/components/ui/ai-disabled";
import { useCredentialSharingConfirm } from "@/hooks/useCredentialSharingConfirm";
import { PlatformEventFields } from "@/pages/assignment-detail/PlatformEventFields";
import { platformDeadlineSavable, platformEventConfig, platformEventDraft, type PlatformEventDraft } from "@/lib/platform-events";
import { CtemSignalTriggerFields } from "@/pages/assignment-detail/CtemSignalTriggerFields";
import { CTEM_SIGNAL_TILE, ctemSignalConfig, ctemSignalDraft, triggerTiles, type CtemSignalDraft } from "@/lib/ctem-signal-trigger";
import { useIsXtmOne } from "@/hooks/usePlatformConfig";

/** Platform picker value for "every platform of the provider". */
const ALL_PLATFORMS = "__all__";

// ---------------------------------------------------------------------------
// Add Assignment Panel (simplified for the detail page)
// ---------------------------------------------------------------------------
/** What a caller presets in a new assignment: the CTEM case page opens it on the case events (#1266). */
export interface AssignmentPreset {
  name?: string;
  triggerType: TriggerType;
  /** The platform events subscribed to, with `triggerType` `platform_event`. */
  platformEvents?: string[];
}

export function AddAssignmentPanel({ agent, onClose, preset }: { agent: Agent; onClose: () => void; preset?: AssignmentPreset }) {
  const { t_i18n, t_text } = useFormatter();
  const create = useCreateAssignment();
  const [consentElement, confirmSharing] = useCredentialSharingConfirm();
  const aiOff = useAiActionDisabled();
  const { isAdmin } = useAuth();
  const ids = useId();
  const integrationId = `${ids}-integration`;
  const eventTypeId = `${ids}-event-type`;
  const scopedId = `${ids}-scoped`;
  const runAsId = `${ids}-run-as`;
  const promptId = `${ids}-prompt`;
  // The viewer's integrations plus the ones the agent is bound to, so an admin
  // adding an event trigger to a colleague's agent sees its integrations.
  const { data: visibleIntegrations } = useIntegrations();
  const { data: allIntegrations } = useIntegrations({ scope: "all" });
  const integrations = useMemo(
    () => withBoundEntities(visibleIntegrations, allIntegrations, Object.values(agent.integration_bindings ?? {}).flat()),
    [visibleIntegrations, allIntegrations, agent.integration_bindings],
  );
  const { data: catalog } = useProviderCatalog("integration");
  // "Run as" applies only to company-managed agents (admin-managed).
  const showRunAs = agent.is_company_managed && isAdmin;
  const { data: users } = useUsers(showRunAs);
  const { toast } = useToast();
  const [name, setName] = useState(preset?.name ?? "");
  const [triggerType, setTriggerType] = useState<TriggerType>(preset?.triggerType ?? "manual");
  const [prompt, setPrompt] = useState("");
  const [runAsUserId, setRunAsUserId] = useState<string | null>(null);
  const [cron, setCron] = useState("0 8 * * 1-5");
  const [eventIntegrationId, setEventIntegrationId] = useState("");
  const [eventProvider, setEventProvider] = useState("");
  const [eventType, setEventType] = useState("");
  const [scopeBindings, setScopeBindings] = useState<Record<string, string[]>>({});
  const [platformDraft, setPlatformDraft] = useState<PlatformEventDraft>(() =>
    platformEventDraft(preset?.platformEvents?.length ? ({ event: preset.platformEvents } as unknown as TriggerConfig) : undefined),
  );
  // The CTEM signal tile (#1409): saved as a platform_event trigger on the ctem_signal.* events.
  const isXtmOne = useIsXtmOne();
  const [ctemTile, setCtemTile] = useState(false);
  const [ctemDraft, setCtemDraft] = useState<CtemSignalDraft>(() => ctemSignalDraft("manual", undefined));

  // Platform scope: detect scoped template bindings (from integration scope_config)
  const { data: platformRegistrations } = usePlatformRegistrations();
  const scopedProviders = integrations
    ?.filter((i) => i.scope_config?.resolve_from_platform)
    .map((i) => i.provider)
    .filter((v, idx, arr) => arr.indexOf(v) === idx)
    ?? [];
  const platformsWithInteg = platformRegistrations?.filter((r) => r.integration_id) ?? [];

  const boundIntegrationIds = agent.integration_bindings ? Object.values(agent.integration_bindings).flat() : [];
  const boundIntegrations = integrations?.filter((i) => boundIntegrationIds.includes(i.id)) || [];

  const eventCapableIntegrations = boundIntegrations.filter((integ) => {
    const entry = catalog?.find((c) => c.provider === integ.provider);
    return entry && entry.available_events.length > 0;
  });

  if (!eventIntegrationId && eventCapableIntegrations.length > 0) {
    const first = eventCapableIntegrations[0];
    const firstCatalogEntry = catalog?.find((c) => c.provider === first.provider);
    setEventIntegrationId(first.id);
    setEventProvider(first.provider);
    setEventType(firstCatalogEntry?.available_events[0]?.event || "");
  }

  const selectedCatalogEntry = catalog?.find((c) => c.provider === eventProvider);

  const handleCreate = async () => {
    const triggerConfig: TriggerConfig = {};
    if (triggerType === "schedule") triggerConfig.cron = cron;
    if (triggerType === "integration_event") {
      triggerConfig.provider = eventProvider;
      triggerConfig.event = eventType;
      if (eventIntegrationId) triggerConfig.integration_id = eventIntegrationId;
    }
    if (triggerType === "platform_event") Object.assign(triggerConfig, ctemTile ? ctemSignalConfig(ctemDraft) : platformEventConfig(platformDraft));
    // Include scope bindings if any platform was pinned
    const hasScope = Object.keys(scopeBindings).length > 0;
    // Creating an assignment on a shared agent lends what it names to the
    // agent's audience: its trigger integration, overrides, variables.
    if (!(await confirmSharing({
      entity_type: "assignment",
      parent_id: agent.id,
      change: {
        name,
        prompt,
        trigger_type: triggerType,
        trigger_config: triggerConfig,
        ...(hasScope ? { integration_bindings: scopeBindings } : {}),
      },
    }))) return;
    create.mutate(
      {
        agent_id: agent.id, name, trigger_type: triggerType, trigger_config: triggerConfig, prompt,
        ...(hasScope ? { integration_bindings: scopeBindings } : {}),
        ...(showRunAs && runAsUserId ? { run_as_user_id: runAsUserId } : {}),
      },
      { onSuccess: () => { toast({ title: t_i18n("Assignment created"), variant: "success" }); onClose(); } },
    );
  };

  const eventDisabled = eventCapableIntegrations.length === 0;

  return (
    <>
      {consentElement}
    <Card>
      <CardContent className="pt-6 space-y-4">
        <p className="content-base font-semibold">{t_i18n("New Assignment")}</p>
        <div className="grid gap-3 xl:grid-cols-2">
          <Input aria-label={t_i18n("Assignment name")} placeholder={t_i18n("Assignment name")} value={name} onChange={(e) => setName(e.target.value)} />
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-1">
            {triggerTiles(TRIGGER_TYPES, isXtmOne).map((t) => {
              const Icon = TRIGGER_ICONS[t] ?? Zap;
              const disabled = t === "integration_event" && eventDisabled;
              const active = t === CTEM_SIGNAL_TILE ? ctemTile : triggerType === t && !ctemTile;
              return (
                <Tip key={t} content={disabled ? t_i18n("No agent integrations support events") : undefined}>
                  <Button
                    variant={active ? "default" : "outline"}
                    size="sm"
                    className={cn("w-full h-10 content-caption gap-1", disabled && "opacity-40 cursor-not-allowed")}
                    aria-pressed={active}
                    disabled={disabled}
                    onClick={() => {
                      setCtemTile(t === CTEM_SIGNAL_TILE);
                      setTriggerType((t === CTEM_SIGNAL_TILE ? "platform_event" : t) as TriggerType);
                    }}
                  >
                    <Icon className="h-3 w-3" />{t_i18n(t === "integration_event" ? "Event" : t === "work_item" ? "Work item" : t === "platform_event" ? "Platform event" : t === CTEM_SIGNAL_TILE ? TRIGGER_LABELS[t] : t)}
                  </Button>
                </Tip>
              );
            })}
          </div>
        </div>
        {triggerType === "schedule" && (
          <div className="space-y-2">
            <div className="flex flex-wrap gap-1">{CRON_PRESETS.map((p) => (<button key={p.value} className={cn("rounded-sm border px-2 py-1 text-[9px] font-medium transition-colors", cron === p.value ? "border-accent/40 bg-accent/10 text-accent" : "border-elevation-subtle-soft text-default-secondary")} onClick={() => setCron(p.value)}>{t_i18n(p.label)}</button>))}</div>
            <Input aria-label={t_i18n("Custom cron")} placeholder={t_i18n("Custom cron")} value={cron} onChange={(e) => setCron(e.target.value)} className="h-8 content-compact font-mono-plex" />
          </div>
        )}
        {triggerType === "webhook" && (
          <p className="text-[11px] text-default-secondary">{t_i18n("The webhook URL will be automatically generated after creation using the assignment ID.")}</p>
        )}
        {triggerType === "work_item" && (
          <p className="text-[11px] text-default-secondary">{t_i18n("This assignment becomes the agent's inbox: the scheduler automatically claims and runs the agent's due work items. Fine-tune filters later in the trigger settings.")}</p>
        )}
        {triggerType === "platform_event" && !ctemTile && (
          <PlatformEventFields draft={platformDraft} onChange={setPlatformDraft} isAdmin={isAdmin} />
        )}
        {triggerType === "platform_event" && ctemTile && <CtemSignalTriggerFields draft={ctemDraft} onChange={setCtemDraft} />}
        {triggerType === "integration_event" && (
          <div className="space-y-3 rounded-sm bg-elevation-hover p-3">
            {eventCapableIntegrations.length > 0 ? (
              <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
                <div className="space-y-1.5" role="group" aria-labelledby={integrationId}>
                  <Label className="block" id={integrationId}>{t_i18n("Integration")}</Label>
                  <div className="space-y-1.5">
                    {eventCapableIntegrations.map((integ) => (
                      <button
                        key={integ.id}
                        className={cn(
                          "w-full flex items-center gap-3 rounded-sm border px-3 py-2.5 text-left transition-colors",
                          eventIntegrationId === integ.id ? "border-accent/40 bg-accent/5" : "border-elevation-subtle-soft hover:border-elevation-subtle",
                          integ.status !== "connected" && "opacity-50",
                        )}
                        onClick={() => {
                          setEventIntegrationId(integ.id);
                          setEventProvider(integ.provider);
                          setEventType("");
                        }}
                      >
                        <img src={providerLogoSrc(integ.provider)} alt={integ.provider} className="h-4 w-4 shrink-0" />
                        <div className="min-w-0 flex-1">
                          <p className={cn("content-compact font-medium", eventIntegrationId === integ.id ? "text-accent" : "")}>{integ.name}</p>
                          <p className="text-[9px] text-default-secondary">{INTEGRATION_PROVIDER_NAMES[integ.provider] ?? integ.provider}</p>
                        </div>
                        {integ.status === "connected" && <CheckCircle2 className="h-3.5 w-3.5 text-feedback-success-primary shrink-0" />}
                        {integ.status !== "connected" && <span className="text-[9px] text-feedback-warning-primary shrink-0">{t_i18n("(disconnected)")}</span>}
                      </button>
                    ))}
                  </div>
                </div>
                {eventProvider && selectedCatalogEntry && (
                  <div className="space-y-1.5">
                    <Label className="block" htmlFor={eventTypeId}>{t_i18n("Event Type")}</Label>
                    <SearchableSelect
                      id={eventTypeId}
                      aria-label={t_i18n("Event Type")}
                      options={selectedCatalogEntry.available_events.map((ev) => ({ value: ev.event, label: t_text(ev.label), keywords: [ev.event, ev.label] }))}
                      value={eventType}
                      onValueChange={setEventType}
                      placeholder={t_i18n("Select event...")}
                    />
                  </div>
                )}
              </div>
            ) : (
              <p className="content-compact text-default-secondary italic">{t_i18n("No integrations bound to this agent support events. Bind an integration with event support in the agent's Configuration tab first.")}</p>
            )}
          </div>
        )}
        {/* Scoped integration selector */}
        {scopedProviders.length > 0 && (
          <div className="space-y-2" role="group" aria-labelledby={scopedId}>
            <Label className="block" id={scopedId}>{t_i18n("Scoped Integrations")}</Label>
            {scopedProviders.map((provider) => {
              const boundPlatformId = scopeBindings[provider]?.[0] ?? "";
              const providerPlatforms = platformsWithInteg.filter((r) => r.platform_identifier === provider || (r.platform_identifier === "openbas" && provider === "openaev"));
              return (
                <div key={provider} className="flex items-center gap-3 rounded-sm border border-feedback-alert-primary/20 bg-feedback-alert-primary/5 px-3 py-2.5">
                  <img src={providerLogoSrc(provider)} alt={provider} className="h-4 w-4 shrink-0" />
                  <div className="min-w-0 flex-1">
                    <p className="content-compact font-medium text-feedback-warning-primary">{INTEGRATION_PROVIDER_NAMES[provider] ?? provider}</p>
                    <p className="text-[9px] text-default-secondary">{t_i18n("Select a platform to pin for this assignment")}</p>
                  </div>
                  {providerPlatforms.length > 1 ? (
                    <SearchableSelect
                      aria-label={t_i18n("Platform")}
                      searchable="auto"
                      className="w-48 shrink-0"
                      options={providerPlatforms.map((reg) => ({ value: reg.integration_id!, label: reg.platform_title }))}
                      pinned={[{ value: ALL_PLATFORMS, label: t_i18n("All platforms") }]}
                      value={boundPlatformId || ALL_PLATFORMS}
                      onValueChange={(val) => {
                        setScopeBindings((prev) => {
                          const next = { ...prev };
                          if (val !== ALL_PLATFORMS) {
                            next[provider] = [val];
                          } else {
                            delete next[provider];
                          }
                          return next;
                        });
                      }}
                    />
                  ) : (
                    <Tip content={t_i18n("Specifically scoped")}>
                      <Filter className="h-3.5 w-3.5 text-feedback-warning-primary shrink-0" />
                    </Tip>
                  )}
                </div>
              );
            })}
          </div>
        )}
        {showRunAs && (
          <div className="space-y-1.5 rounded-sm bg-elevation-hover p-3">
            <Label className="block" htmlFor={runAsId}>{t_i18n("Run as")}</Label>
            <p className="content-caption text-default-secondary">
              {t_i18n("This is a company-managed agent. Pick which user's identity automatic executions (schedule, webhook, integration events) run under (their integrations, model and memory). Manual and follow-up runs always execute as the user who triggers them. Defaults to the admin owner.")}
            </p>
            <SearchableSelect
              id={runAsId}
              aria-label={t_i18n("Run as")}
              options={(users ?? []).map((u) =>
                userSelectOption(u, u.id === agent.user_id ? { description: `${u.email} · ${t_i18n("owner")}` } : {}),
              )}
              pinned={[{
                value: "__owner__",
                label: (() => {
                  const owner = users?.find((u) => u.id === agent.user_id);
                  const label = owner
                    ? (owner.full_name?.trim() ? `${owner.full_name} (${owner.email})` : owner.email)
                    : null;
                  return label ? t_i18n("Default, run as owner: {name}", { name: label }) : t_i18n("Default: run as owner");
                })(),
              }]}
              value={runAsUserId ?? "__owner__"}
              onValueChange={(v) => setRunAsUserId(v === "__owner__" ? null : v)}
              placeholder={t_i18n("Search users...")}
            />
          </div>
        )}
        <div className="space-y-1.5">
          <div className="flex items-center justify-between">
            <Label className="block" htmlFor={promptId}>{t_i18n("Action prompt")}</Label>
            <PromptGenerator contextType="assignment_prompt" currentText={prompt} onGenerated={setPrompt} assignmentName={name} />
          </div>
          <textarea id={promptId} className="flex min-h-[120px] w-full rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base placeholder:text-default-secondary focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus" placeholder={t_i18n("What should the agent do?")} value={prompt} onChange={(e) => setPrompt(e.target.value)} />
        </div>
        <div className="flex justify-end gap-3">
          <Button variant="ghost" onClick={onClose}>{t_i18n("Cancel")}</Button>
          <AiDisabledTip><Button onClick={handleCreate} disabled={!name.trim() || !prompt.trim() || create.isPending || aiOff || (triggerType === "platform_event" && !ctemTile && (platformDraft.events.length === 0 || !platformDeadlineSavable(platformDraft.expiresAt)))}>{create.isPending ? t_i18n("Creating...") : t_i18n("Create Assignment")}</Button></AiDisabledTip>
        </div>
      </CardContent>
    </Card>
    </>
  );
}
