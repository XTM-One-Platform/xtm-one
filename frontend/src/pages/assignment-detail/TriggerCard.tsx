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

import { useState, useMemo } from "react";
import {
  Zap,
  Clock,
  CheckCircle2,
  Timer,
  Filter,
  Webhook,
  Copy,
  Check,
  GitBranch,
  Plus,
  Trash2,
  Variable,
  ShieldCheck,
  ListChecks,
  Terminal,
  AlertCircle,
  X,
} from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Tip } from "@/components/ui/tooltip";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { SearchInput } from "@/components/ui/search-input";
import { Label } from "@/components/ui/label";
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { TagInput } from "@/components/ui/tag-input";
import { useUpdateAssignment } from "@/hooks/api/useAssignmentApi";
import { useCredentialSharingConfirm } from "@/hooks/useCredentialSharingConfirm";
import {
  useGitHubWebhooks,
  useCreateGitHubWebhook,
  useDeleteGitHubWebhook,
  useIntegrationElements,
} from "@/hooks/api/useIntegrationApi";
import { useToast } from "@/components/ui/toast";
import { cn } from "@/lib/utils";
import { hasPrefixAllowList, legacySiblingEvents, saveSwitchesToExact, unreadLegacyEventType } from "@/lib/event-matching";
import { TRIGGER_TYPES, TRIGGER_ICONS, TRIGGER_LABELS, CRON_PRESETS, describeCron, INTEGRATION_PROVIDER_NAMES, COPY_FEEDBACK_DELAY, providerLogoSrc } from "@/lib/constants";
import type { Assignment, TriggerType, TriggerConfig, Agent, Integration, ProviderCatalogItem, ManualTriggerParameter, ManualTriggerParameterType, IntegrationElement, WorkItemPriority } from "@/types";
import { useFormatter } from "@/i18n";
import { ConfigCard } from "./ConfigCard";
import { EventFilterFields, EventFilterSummary } from "./EventFilterFields";
import { cleanEventFilterExtra, pickEventFilterExtra, type EventFilterExtra } from "@/lib/event-filters";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { AiDisabledTip } from "@/components/ui/ai-disabled";
import { CardEditActions } from "@/components/ui/card-edit-actions";
import { useAuth } from "@/hooks/useAuth";
import { PlatformEventFields, PlatformEventSummary } from "./PlatformEventFields";
import { CtemSignalSummary, CtemSignalTriggerFields } from "./CtemSignalTriggerFields";
import {
  CTEM_SIGNAL_TILE,
  ctemSignalConfig,
  ctemSignalDraft,
  isCtemSignalTrigger,
  triggerTile,
  triggerTiles,
  type CtemSignalDraft,
} from "@/lib/ctem-signal-trigger";
import { useIsXtmOne } from "@/hooks/usePlatformConfig";
import { ProvisioningSummary } from "./ProvisioningSummary";
import { platformDeadlineSavable, platformEventConfig, platformEventDraft, type PlatformEventDraft } from "@/lib/platform-events";
import { WORK_ITEM_MAX_ITEMS_IN_PARALLEL, inboxSlots, parseItemsInParallel } from "./inbox-slots";
import { formatTimeAgo } from "@/lib/relative-time";
import { Spinner } from "@filigran/design-system";

/** The Select value standing for "no default" on a select parameter (a Select item cannot be empty). */
const NO_DEFAULT = "__no_default__";

const PARAM_TYPES: { value: ManualTriggerParameterType; label: string }[] = [
  { value: "text", label: "Text" },
  { value: "textarea", label: "Textarea" },
  { value: "number", label: "Number" },
  { value: "boolean", label: "Boolean" },
  { value: "select", label: "Select" },
];

// ---------------------------------------------------------------------------
// TriggerCard — Trigger configuration (edit + read-only)
// ---------------------------------------------------------------------------

interface TriggerCardProps {
  assignment: Assignment;
  agent: Agent | undefined;
  catalog: ProviderCatalogItem[] | undefined;
  allIntegrations: Integration[] | undefined;
  canManage: boolean;
  onUpdate: () => void;
}

export function TriggerCard({
  assignment, agent, catalog, allIntegrations, canManage, onUpdate,
}: TriggerCardProps) {
  const { t_i18n, t_text } = useFormatter();
  const { toast } = useToast();
  const update = useUpdateAssignment();
  const aiOff = useAiActionDisabled();
  const { isAdmin } = useAuth();
  const [consentElement, confirmSharing] = useCredentialSharingConfirm();

  const [editing, setEditing] = useState(false);
  const [copiedUrl, setCopiedUrl] = useState<string | null>(null);

  // ── Editable fields ──
  const [name, setName] = useState("");
  const [delay, setDelay] = useState("");
  const [triggerType, setTriggerType] = useState<TriggerType>("manual");
  const [cron, setCron] = useState("");
  const [eventProvider, setEventProvider] = useState("");
  const [eventType, setEventType] = useState("");
  const [eventIntegrationId, setEventIntegrationId] = useState("");
  const [eventFilterScope, setEventFilterScope] = useState<Record<string, string[]>>({});
  const [eventFilterFrom, setEventFilterFrom] = useState("");
  const [eventFilterExtra, setEventFilterExtra] = useState<EventFilterExtra>({});
  const [manualParams, setManualParams] = useState<ManualTriggerParameter[]>([]);
  const [ignoreSelfEvents, setIgnoreSelfEvents] = useState(false);
  // work_item inbox config
  const [workItemPrefix, setWorkItemPrefix] = useState("");
  const [workItemPriorityMin, setWorkItemPriorityMin] = useState("");
  // Items the inbox runs at once (trigger_config.max_concurrent_runs)
  const [workItemSlots, setWorkItemSlots] = useState("1");
  // platform_event config
  const [platformDraft, setPlatformDraft] = useState<PlatformEventDraft>(() => platformEventDraft(undefined));
  // The CTEM signal tile (#1409): a platform_event trigger on the ctem_signal.* events, edited on its own fields.
  const isXtmOne = useIsXtmOne();
  const [ctemTile, setCtemTile] = useState(false);
  const [ctemDraft, setCtemDraft] = useState<CtemSignalDraft>(() => ctemSignalDraft("manual", undefined));

  // Derived data
  const agentIntegrations = allIntegrations?.filter((i) =>
    agent?.integration_bindings && Object.values(agent.integration_bindings).flat().includes(i.id)
  ) ?? [];
  const eventCapableIntegrations = agentIntegrations.filter((i) =>
    catalog?.some((c) => c.provider === i.provider && c.available_events?.length > 0)
  );
  const eventDataLoading = !agent || !allIntegrations || !catalog;

  // A CTEM signal trigger reads as its own tile (#1409), a platform_event trigger underneath.
  const storedTile = triggerTile(assignment.trigger_type, assignment.trigger_config);
  const TriggerIcon = TRIGGER_ICONS[storedTile] ?? Zap;
  const cronExpr = assignment.trigger_type === "schedule" ? assignment.trigger_config.cron : null;

  // Validation: prevent saving incomplete trigger configuration
  const canSave =
    (triggerType !== "integration_event" || (!!eventProvider && !!eventType && !!eventIntegrationId))
    && (triggerType !== "platform_event" || ctemTile || (platformDraft.events.length > 0 && platformDeadlineSavable(platformDraft.expiresAt)));

  const copyUrl = (url: string) => {
    navigator.clipboard.writeText(url);
    setCopiedUrl(url);
    setTimeout(() => setCopiedUrl(null), COPY_FEEDBACK_DELAY);
  };

  // Ready-to-paste request for the direct-assignment webhook endpoint.
  // Built only when the viewer may see the secret (`canManage` — the API also
  // strips it otherwise), so the snippet never renders a broken example with a
  // missing credential.
  const webhookSecret = assignment.trigger_config?.webhook_secret;
  const curlSnippet = useMemo(() => {
    if (assignment.trigger_type !== "webhook") return null;
    if (!assignment.webhook_url || !canManage || !webhookSecret) return null;
    // Single-quoted shell literals, with any embedded quote escaped the POSIX
    // way (`'\''`). The secret is NOT guaranteed to be `secrets.token_urlsafe`
    // output: `ensure_webhook_secret` only fills in a missing one, so a
    // caller-supplied value survives assignment create/update and agent import
    // verbatim. Interpolating it raw would let a crafted secret close the quote
    // and run arbitrary commands in the shell of whoever pastes the snippet.
    const shq = (value: string) => `'${value.replace(/'/g, "'\\''")}'`;
    return [
      `curl -X POST ${shq(assignment.webhook_url)} \\`,
      `  -H 'Content-Type: application/json' \\`,
      `  -H ${shq(`X-Webhook-Token: ${webhookSecret}`)} \\`,
      `  -d '{"example": "payload"}'`,
    ].join("\n");
  }, [assignment.trigger_type, assignment.webhook_url, canManage, webhookSecret]);

  const startEditing = () => {
    const cfg = assignment.trigger_config;
    setName(assignment.name ?? "");
    setDelay(assignment.delay ?? "");
    setTriggerType(assignment.trigger_type as TriggerType);
    setCron(cfg.cron ?? "");
    setEventProvider(cfg.provider ?? "");
    setEventType(cfg.event ?? "");
    setEventIntegrationId(cfg.integration_id ?? "");
    // Load generic scope; migrate legacy filter.channels into scope keyed by provider element type
    const savedScope = { ...(cfg.filter?.scope ?? {}) };
    if (!Object.keys(savedScope).length && cfg.filter?.channels?.length) {
      const legacyType = cfg.event === "new_teams_message" || cfg.event === "new_teams_reply" ? "teams_channel"
        : cfg.event === "new_slack_message" ? "slack_channel" : "";
      if (legacyType) savedScope[legacyType] = cfg.filter.channels;
    }
    setEventFilterScope(savedScope);
    setEventFilterFrom(cfg.filter?.from_contains ?? "");
    setEventFilterExtra(pickEventFilterExtra(cfg.filter));
    setManualParams(assignment.manual_trigger_parameters ?? []);
    setIgnoreSelfEvents(assignment.ignore_self_triggered_events ?? false);
    setWorkItemPrefix(cfg.correlation_prefix ?? "");
    setWorkItemPriorityMin(cfg.priority_min ?? "");
    setWorkItemSlots(String(inboxSlots(assignment.trigger_type === "work_item" ? cfg : undefined)));
    setPlatformDraft(platformEventDraft(assignment.trigger_type === "platform_event" ? cfg : undefined));
    setCtemTile(isCtemSignalTrigger(assignment.trigger_type, cfg));
    setCtemDraft(ctemSignalDraft(assignment.trigger_type, cfg));
    setEditing(true);
  };

  const pendingTriggerConfig = (): TriggerConfig => {
    const stored = assignment.trigger_config;
    // Preserve the existing webhook config (legacy `path` used for
    // /api/hooks/{agent_id}/{path} matching, `events` filter, and the signing
    // secret) when the trigger stays a webhook — the UI does not edit those
    // fields, and dropping the secret would silently rotate it server-side
    // and break the sender.
    const triggerConfig: TriggerConfig =
      triggerType === "webhook" && assignment.trigger_type === "webhook"
        ? { ...stored }
        : {};
    if (triggerType === "schedule" && cron) triggerConfig.cron = cron;
    if (triggerType === "integration_event") {
      // Same for the keys this editor does not show while the trigger keeps its
      // provider: the `events` allow-list and the matching mode.
      // Dropping the list would widen the trigger and read as choosing its
      // events again.
      if (assignment.trigger_type === "integration_event" && stored.provider === eventProvider) {
        if (stored.events !== undefined) triggerConfig.events = stored.events;
        if (stored.event_matching) triggerConfig.event_matching = stored.event_matching;
        // The provisioning spec and what the server holds for it in the source
        // platform: dropping them would remove the trigger it created there.
        if (stored.provision !== undefined) triggerConfig.provision = stored.provision;
        if (stored.opencti_provisioning) triggerConfig.opencti_provisioning = stored.opencti_provisioning;
        if (stored.openaev_provisioning) triggerConfig.openaev_provisioning = stored.openaev_provisioning;
      }
      triggerConfig.provider = eventProvider;
      triggerConfig.event = eventType;
      if (eventIntegrationId) triggerConfig.integration_id = eventIntegrationId;
      const filter: NonNullable<TriggerConfig["filter"]> = {};
      const nonEmpty = Object.fromEntries(Object.entries(eventFilterScope).filter(([, ids]) => ids.length > 0));
      if (Object.keys(nonEmpty).length > 0) {
        filter.scope = nonEmpty;
        // Backward compat: pollers still read filter.channels for teams/slack
        const channelType = eventProvider === "microsoft" ? "teams_channel"
          : eventProvider === "slack" ? "slack_channel" : "";
        if (channelType && nonEmpty[channelType]) {
          filter.channels = nonEmpty[channelType];
        }
      }
      if (eventFilterFrom.trim()) {
        filter.from_contains = eventFilterFrom.trim();
      }
      Object.assign(filter, cleanEventFilterExtra(eventFilterExtra, eventType));
      if (Object.keys(filter).length > 0) triggerConfig.filter = filter;
    }
    if (triggerType === "work_item") {
      // Preserve owner_agent_id (the "dispatcher drains another agent's queue"
      // config): the UI doesn't expose it, so rebuilding trigger_config from
      // scratch would silently drop it and reset the inbox to its own agent.
      if (assignment.trigger_config.owner_agent_id) {
        triggerConfig.owner_agent_id = assignment.trigger_config.owner_agent_id;
      }
      if (workItemPrefix.trim()) triggerConfig.correlation_prefix = workItemPrefix.trim();
      if (workItemPriorityMin) triggerConfig.priority_min = workItemPriorityMin as WorkItemPriority;
      // One slot is the serial default: the key is left out rather than stored.
      const slots = parseItemsInParallel(workItemSlots);
      if (slots !== null && slots > 1) triggerConfig.max_concurrent_runs = slots;
      // The express lane is set through the API or the agent builder: kept as stored.
      if (assignment.trigger_config.express_lane) {
        triggerConfig.express_lane = assignment.trigger_config.express_lane;
      }
    }
    if (triggerType === "platform_event") Object.assign(triggerConfig, ctemTile ? ctemSignalConfig(ctemDraft) : platformEventConfig(platformDraft));
    // The queue keys are set through the API or the agent builder and
    // this editor does not show them: a save keeps them while the trigger keeps
    // its type, and the ordering key - a path into the payload - its provider.
    if (triggerType === assignment.trigger_type) {
      // A work-item inbox sets its slots in this editor.
      if (triggerType !== "work_item" && stored.max_concurrent_runs !== undefined) {
        triggerConfig.max_concurrent_runs = stored.max_concurrent_runs;
      }
      if (stored.ordering_key !== undefined && (triggerType !== "integration_event" || stored.provider === eventProvider)) {
        triggerConfig.ordering_key = stored.ordering_key;
      }
    }
    return triggerConfig;
  };

  // A trigger saved before exact event matching keeps its prefix match through
  // every save that leaves its events alone; this save would switch it.
  const saveSwitchesMatching = () =>
    assignment.event_matching === "legacy" &&
    saveSwitchesToExact(assignment.trigger_type, assignment.trigger_config, triggerType, pendingTriggerConfig());

  const handleSave = async () => {
    if (triggerType === "integration_event" && (!eventProvider || !eventType || !eventIntegrationId)) {
      toast({ title: t_i18n("Please select an integration and an event type"), variant: "error" });
      return;
    }
    if (triggerType === "work_item" && parseItemsInParallel(workItemSlots) === null) {
      toast({ title: t_i18n("Items in parallel must be a whole number from 1 to {max}.", { max: WORK_ITEM_MAX_ITEMS_IN_PARALLEL }), variant: "error" });
      return;
    }
    const triggerConfig = pendingTriggerConfig();
    // An event trigger's integration, and a webhook's URL, reach the agent's
    // audience through this assignment.
    if (!(await confirmSharing({
      entity_type: "assignment",
      entity_id: assignment.id,
      change: { trigger_type: triggerType, trigger_config: triggerConfig },
    }))) {
      return;
    }
    const switchesMatching = saveSwitchesMatching();
    const cleanParams = triggerType === "manual" && manualParams.length > 0
      ? manualParams.filter((p) => p.key.trim() && p.label.trim())
      : null;
    // The self-event loop guard only applies to GitHub integration events, and
    // its toggle is only shown for that case. Persist it as true only then;
    // otherwise clear it so a non-GitHub trigger never carries a hidden,
    // un-toggleable flag forward (e.g. after switching away from GitHub).
    const isGithubEvent = triggerType === "integration_event" && eventProvider === "github";
    update.mutate(
      { id: assignment.id, name, trigger_type: triggerType, trigger_config: triggerConfig, delay: delay || undefined, manual_trigger_parameters: cleanParams, ignore_self_triggered_events: isGithubEvent ? ignoreSelfEvents : false },
      {
        onSuccess: () => {
          toast({
            title: switchesMatching ? t_i18n("Trigger updated: it now matches its events exactly") : t_i18n("Trigger updated"),
            variant: "success",
          });
          setEditing(false);
          onUpdate();
        },
      },
    );
  };

  // A trigger saved before exact event matching keeps its prefix
  // match; this switches it without touching anything else in its config.
  const switchToExactMatching = () => {
    update.mutate(
      { id: assignment.id, trigger_config: { ...assignment.trigger_config, event_matching: "exact" } },
      {
        onSuccess: () => {
          toast({ title: t_i18n("Exact event matching enabled"), variant: "success" });
          onUpdate();
        },
      },
    );
  };

  return (
    <Card>
      {consentElement}
      <CardHeader className="flex-row items-center justify-between space-y-0 p-4 pb-3">
        <CardTitle className="content-base flex items-center gap-2"><Zap className="h-4 w-4 text-accent" /> {t_i18n("Trigger")}</CardTitle>
        <CardEditActions
          editing={editing}
          canEdit={canManage}
          aiGated
          onEdit={startEditing}
          onCancel={() => setEditing(false)}
          onSave={handleSave}
          saveDisabled={update.isPending || !canSave || aiOff}
        />
      </CardHeader>
      <CardContent className="px-4 pb-4 pt-0">
        {editing ? (
          <div className="space-y-5">
            {/* ── Name ── */}
            <div className="space-y-1.5">
              <Label htmlFor="assignment-trigger-name">{t_i18n("Name")}</Label>
              <Input id="assignment-trigger-name" aria-label={t_i18n("Name")} placeholder={t_i18n("Assignment name")} value={name} onChange={(e) => setName(e.target.value)} />
            </div>

            {/* ── Trigger type ── */}
            <div className="space-y-3">
              <p className="content-caption font-medium text-default-secondary uppercase tracking-wider">{t_i18n("Trigger type")}</p>
              <div className="grid grid-cols-2 sm:grid-cols-3 gap-2">
                {triggerTiles(TRIGGER_TYPES, isXtmOne).map((t) => {
                  const Icon = TRIGGER_ICONS[t] ?? Zap;
                  const disabled = t === "integration_event" && !eventDataLoading && eventCapableIntegrations.length === 0;
                  const active = t === CTEM_SIGNAL_TILE ? ctemTile : triggerType === t && !ctemTile;
                  return (
                    <Tip key={t} content={disabled ? t_i18n("No agent integrations support events") : undefined}>
                      <button
                        className={cn(
                          "flex items-center gap-2 rounded-sm border px-3 py-2 content-compact transition-colors text-left",
                          disabled && "opacity-40 cursor-not-allowed",
                          active ? "border-accent/40 bg-accent/5 text-accent" : "border-elevation-subtle-soft text-default-secondary hover:border-elevation-subtle",
                        )}
                        aria-pressed={active}
                        disabled={disabled}
                        onClick={() => {
                          setCtemTile(t === CTEM_SIGNAL_TILE);
                          setTriggerType((t === CTEM_SIGNAL_TILE ? "platform_event" : t) as TriggerType);
                        }}
                      >
                        <Icon className="h-3.5 w-3.5 shrink-0" />
                        {t_i18n(TRIGGER_LABELS[t])}
                      </button>
                    </Tip>
                  );
                })}
              </div>

              {triggerType === "schedule" && (
                <div className="space-y-2">
                  <Label htmlFor="assignment-trigger-cron">{t_i18n("Cron expression")}</Label>
                  <div className="flex flex-wrap gap-1.5">
                    {CRON_PRESETS.map((p) => (
                      <button
                        key={p.value}
                        className={cn("rounded-sm border px-2 py-1 content-caption transition-colors", cron === p.value ? "border-accent/40 bg-accent/10 text-accent" : "border-elevation-subtle-soft text-default-secondary hover:border-elevation-subtle")}
                        onClick={() => setCron(p.value)}
                      >
                        {t_i18n(p.label)}
                      </button>
                    ))}
                  </div>
                  <Input id="assignment-trigger-cron" aria-label={t_i18n("Custom cron")} className="[&_input]:font-mono-plex" placeholder={t_i18n("Custom cron (e.g. */15 * * * *)")} value={cron} onChange={(e) => setCron(e.target.value)} />
                </div>
              )}

              {triggerType === "webhook" && (
                <div className="space-y-1.5">
                  <p className="content-caption text-default-secondary">{t_i18n("Webhook URL")}</p>
                  <p className="content-caption text-default-secondary">{t_i18n("The webhook URL is automatically generated using the assignment ID. It will be displayed after saving.")}</p>
                </div>
              )}

              {triggerType === "work_item" && (
                <div className="space-y-3 rounded-sm bg-elevation-hover p-3">
                  <p className="content-caption text-default-secondary leading-relaxed">
                    {t_i18n("This assignment becomes the agent's inbox: the scheduler automatically claims open work items owned by")} <strong>{agent?.name ?? t_i18n("this agent")}</strong> {t_i18n("whose due date has arrived (or that have none) and runs this assignment for each. Items complete on success and return to the queue on failure - no manual pickup needed.")}
                  </p>
                  <div className="space-y-1.5">
                    <Label htmlFor="work-item-prefix">{t_i18n("Correlation key prefix")} <span className="font-normal text-default-secondary">{t_i18n("(optional)")}</span></Label>
                    <Input id="work-item-prefix" aria-label={t_i18n("Correlation key prefix")} className="[&_input]:font-mono-plex" placeholder={t_i18n("e.g. ticket- or order-")} value={workItemPrefix} onChange={(e) => setWorkItemPrefix(e.target.value)} />
                    <p className="content-caption text-default-secondary">{t_i18n("Only auto-pick-up items whose correlation key starts with this. Leave empty for all of the agent's items.")}</p>
                  </div>
                  <div className="space-y-1.5">
                    <Label id="work-item-priority-label" htmlFor="work-item-priority">{t_i18n("Minimum priority")} <span className="font-normal text-default-secondary">{t_i18n("(optional)")}</span></Label>
                    <Select value={workItemPriorityMin || "any"} onValueChange={(v) => setWorkItemPriorityMin(v === "any" ? "" : v)}>
                      <SelectTrigger id="work-item-priority" aria-labelledby="work-item-priority-label" className="w-56"><SelectValue /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value="any">{t_i18n("Any priority")}</SelectItem>
                        <SelectItem value="low">{t_i18n("Low and up")}</SelectItem>
                        <SelectItem value="medium">{t_i18n("Medium and up")}</SelectItem>
                        <SelectItem value="high">{t_i18n("High and up")}</SelectItem>
                        <SelectItem value="urgent">{t_i18n("Urgent only")}</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                  <div className="space-y-1.5">
                    <Label id="work-item-slots-label" htmlFor="work-item-slots">{t_i18n("Items in parallel")}</Label>
                    <Input
                      id="work-item-slots"
                      aria-labelledby="work-item-slots-label"
                      type="number"
                      inputMode="numeric"
                      min={1}
                      max={WORK_ITEM_MAX_ITEMS_IN_PARALLEL}
                      step={1}
                      className="w-32"
                      value={workItemSlots}
                      aria-invalid={parseItemsInParallel(workItemSlots) === null}
                      aria-describedby="work-item-slots-hint"
                      onChange={(e) => setWorkItemSlots(e.target.value)}
                    />
                    <p id="work-item-slots-hint" className={cn("content-caption", parseItemsInParallel(workItemSlots) === null ? "text-feedback-error-primary" : "text-default-secondary")}>
                      {parseItemsInParallel(workItemSlots) === null
                        ? t_i18n("Items in parallel must be a whole number from 1 to {max}.", { max: WORK_ITEM_MAX_ITEMS_IN_PARALLEL })
                        : t_i18n("How many items this inbox runs at once, from 1 to {max}. Each one is a full run of its own.", { max: WORK_ITEM_MAX_ITEMS_IN_PARALLEL })}
                    </p>
                  </div>
                </div>
              )}

              {triggerType === "platform_event" && !ctemTile && (
                <PlatformEventFields draft={platformDraft} onChange={setPlatformDraft} isAdmin={isAdmin} />
              )}
              {triggerType === "platform_event" && ctemTile && <CtemSignalTriggerFields draft={ctemDraft} onChange={setCtemDraft} />}

              {triggerType === "integration_event" && (
                <div className="space-y-4">
                  <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
                    {/* Left — Pick integration */}
                    <div className="space-y-1.5">
                      <p className="content-caption text-default-secondary font-medium">{t_i18n("Integration")}</p>
                      {eventDataLoading ? (
                        <div className="flex items-center gap-2 py-3 content-compact text-default-secondary">
                          <Spinner size="sm" tone="inherit" /> {t_i18n("Loading integrations...")}
                        </div>
                      ) : eventCapableIntegrations.length > 0 ? (
                        <div className="space-y-1.5">
                          {eventCapableIntegrations.map((integ) => (
                            <button
                              key={integ.id}
                              className={cn(
                                "w-full flex items-center gap-3 rounded-sm border px-3 py-2.5 text-left transition-colors",
                                eventIntegrationId === integ.id ? "border-accent/40 bg-accent/5" : "border-elevation-subtle-soft hover:border-elevation-subtle",
                              )}
                              onClick={() => {
                                setEventIntegrationId(integ.id);
                                setEventProvider(integ.provider);
                                setEventType("");
                              }}
                            >
                              <img src={providerLogoSrc(integ.provider)} alt={integ.provider} className="h-5 w-5 shrink-0" />
                              <div className="min-w-0 flex-1">
                                <p className={cn("content-compact font-medium", eventIntegrationId === integ.id ? "text-accent" : "")}>{integ.name}</p>
                                <p className="content-caption text-default-secondary">{INTEGRATION_PROVIDER_NAMES[integ.provider] ?? integ.provider}</p>
                              </div>
                              {integ.status === "connected" && <CheckCircle2 className="h-3.5 w-3.5 text-feedback-success-primary shrink-0" />}
                            </button>
                          ))}
                        </div>
                      ) : (
                        <div className="rounded-sm bg-feedback-alert-primary/5 p-3">
                          <p className="content-caption text-feedback-warning-primary">{t_i18n("No event-capable integrations bound to this agent. Bind a Microsoft, Google, or Slack integration in the agent configuration first.")}</p>
                        </div>
                      )}
                    </div>

                    {/* Right — Pick event type */}
                    {eventProvider && (
                      <div className="space-y-1.5">
                        <p className="content-caption text-default-secondary font-medium">{t_i18n("Event type")}</p>
                        <div className="space-y-1.5">
                          {(catalog ?? [])
                            .find((c) => c.provider === eventProvider)
                            ?.available_events?.map((ev) => (
                              <button
                                key={ev.event}
                                className={cn(
                                  "w-full text-left rounded-sm border p-3 transition-colors",
                                  eventType === ev.event ? "border-accent/40 bg-accent/5" : "border-elevation-subtle-soft hover:border-elevation-subtle",
                                )}
                                onClick={() => setEventType(ev.event)}
                              >
                                <p className={cn("content-compact font-medium", eventType === ev.event ? "text-accent" : "")}>{t_text(ev.label)}</p>
                                <p className="content-caption text-default-secondary mt-0.5">{t_text(ev.description)}</p>
                              </button>
                            ))}
                        </div>
                      </div>
                    )}
                  </div>

                  {/* Event-specific filters */}
                  {eventType && (
                    <div className="space-y-3 rounded-sm bg-elevation-hover p-3">
                      <p className="content-caption text-default-secondary font-medium uppercase tracking-wider">{t_i18n("Filters")} <span className="normal-case font-normal">{t_i18n("(optional)")}</span></p>

                      {/* Generic element scope pickers — driven by provider catalog */}
                      {(() => {
                        const providerCatalog = (catalog ?? []).find((c) => c.provider === eventProvider);
                        const elementTypes = providerCatalog?.element_types ?? [];
                        return elementTypes.map((et) => (
                          <ScopePicker
                            key={et.type}
                            integrationId={eventIntegrationId}
                            provider={eventProvider}
                            elementType={et.type}
                            elementLabel={et.name}
                            elementDescription={et.description}
                            selectedIds={eventFilterScope[et.type] ?? []}
                            onSelectionChange={(ids) =>
                              setEventFilterScope((prev) => ({ ...prev, [et.type]: ids }))
                            }
                          />
                        ));
                      })()}

                      {eventType === "new_email" && (
                        <div className="space-y-1.5">
                          <Label htmlFor="trigger-sender-filter" id="trigger-sender-filter-label">{t_i18n("Sender filter")}</Label>
                          <Input aria-label={t_i18n("Sender filter")}
                            id="trigger-sender-filter"
                            aria-describedby="trigger-sender-filter-help"
                            placeholder={t_i18n("e.g. events@ or noreply@github.com")}
                            value={eventFilterFrom}
                            onChange={(e) => setEventFilterFrom(e.target.value)}
                          />
                          <p id="trigger-sender-filter-help" className="content-compact text-default-secondary">{t_i18n("Only trigger for emails from addresses containing this text. Leave empty for all emails.")}</p>
                        </div>
                      )}

                      <EventFilterFields event={eventType} value={eventFilterExtra} onChange={setEventFilterExtra} />
                    </div>
                  )}

                  {/* ── Loop protection (GitHub) ── */}
                  {eventProvider === "github" && (
                    <div className="flex items-start justify-between gap-3 rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-3">
                      <div className="space-y-0.5">
                        <p className="content-compact font-medium">{t_i18n("Ignore self-triggered events")}</p>
                        <p className="content-caption text-default-secondary leading-relaxed">
                          {t_i18n("Skip events caused by this agent's own writes (title edits, comments, labels) so it doesn't re-trigger on its own changes and loop. Trade-off: a manual edit to the same issue or PR within ~3 min of the agent's write is also skipped.")}
                        </p>
                      </div>
                      <Switch aria-label={t_i18n("Ignore self-triggered events")} checked={ignoreSelfEvents} onCheckedChange={setIgnoreSelfEvents} />
                    </div>
                  )}
                </div>
              )}

              {/* ── Manual trigger parameters ── */}
              {triggerType === "manual" && (
                <div className="space-y-3">
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-2">
                      <Variable className="h-3.5 w-3.5 text-filigran-ia-primary" />
                      <p className="content-caption font-medium text-default-secondary uppercase tracking-wider">{t_i18n("Input parameters")}</p>
                    </div>
                    <Button
                      variant="ghost"
                      className="gap-1 text-accent hover:text-accent/80"
                      onClick={() => setManualParams((prev) => [
                        ...prev,
                        { key: "", label: "", type: "text", description: "", required: false, default: "", options: [] },
                      ])}
                    >
                      <Plus className="h-3 w-3" /> {t_i18n("Add parameter")}
                    </Button>
                  </div>
                  <p className="content-caption text-default-secondary">
                    {t_i18n("Define input fields shown when this assignment is triggered manually. Values are available as")} <code className="text-accent bg-accent/5 px-1 rounded-sm">{"{{trigger.<key>}}"}</code> {t_i18n("in the prompt.")}
                  </p>
                  {manualParams.length > 0 && (
                    <div className="space-y-3">
                      {manualParams.map((param, idx) => (
                        <div key={idx} className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-3 space-y-2.5">
                          <div className="flex items-center justify-between">
                            <span className="content-caption text-default-secondary font-medium">{t_i18n("Parameter {number}", { number: idx + 1 })}</span>
                            <Button aria-label={t_i18n("Remove parameter")}
                              variant="ghost"
                              size="icon"
                              className="text-feedback-error-primary hover:text-feedback-error-primary"
                              onClick={() => setManualParams((prev) => prev.filter((_, i) => i !== idx))}
                            >
                              <Trash2 className="h-4 w-4" />
                            </Button>
                          </div>
                          <div className="grid grid-cols-2 gap-2">
                            <div className="space-y-1">
                              <Label htmlFor={`manual-param-${idx}-key`}>{t_i18n("Key")}</Label>
                              <Input id={`manual-param-${idx}-key`} aria-label={t_i18n("Key")}
                                className="[&_input]:font-mono-plex"
                                placeholder={t_i18n("e.g. topic")}
                                value={param.key}
                                onChange={(e) => {
                                  const v = e.target.value.replace(/[^a-zA-Z0-9_]/g, "");
                                  setManualParams((prev) => prev.map((p, i) => i === idx ? { ...p, key: v } : p));
                                }}
                              />
                            </div>
                            <div className="space-y-1">
                              <Label htmlFor={`manual-param-${idx}-label`}>{t_i18n("Label")}</Label>
                              <Input id={`manual-param-${idx}-label`} aria-label={t_i18n("Label")}
                                placeholder={t_i18n("e.g. Research Topic")}
                                value={param.label}
                                onChange={(e) => setManualParams((prev) => prev.map((p, i) => i === idx ? { ...p, label: e.target.value } : p))}
                              />
                            </div>
                          </div>
                          <div className="grid grid-cols-2 gap-2">
                            <div className="space-y-1">
                              <Label id={`manual-param-${idx}-type-label`} htmlFor={`manual-param-${idx}-type`}>{t_i18n("Type")}</Label>
                              <Select
                                value={param.type}
                                onValueChange={(v) => setManualParams((prev) => prev.map((p, i) => i === idx ? { ...p, type: v as ManualTriggerParameterType } : p))}
                              >
                                <SelectTrigger id={`manual-param-${idx}-type`} aria-labelledby={`manual-param-${idx}-type-label`} className="w-full">
                                  <SelectValue />
                                </SelectTrigger>
                                <SelectContent>
                                  {PARAM_TYPES.map((t) => (
                                    <SelectItem key={t.value} value={t.value}>{t_i18n(t.label)}</SelectItem>
                                  ))}
                                </SelectContent>
                              </Select>
                            </div>
                            <div className="space-y-1">
                              <Label id={`manual-param-${idx}-default-label`} htmlFor={`manual-param-${idx}-default`}>{t_i18n("Default value")}</Label>
                              {param.type === "select" ? (
                                // A select parameter's default is one of its own options, never free text.
                                <Select
                                  value={param.default && (param.options ?? []).includes(param.default) ? param.default : NO_DEFAULT}
                                  onValueChange={(v) => setManualParams((prev) => prev.map((p, i) => i === idx ? { ...p, default: v === NO_DEFAULT ? "" : v } : p))}
                                >
                                  <SelectTrigger id={`manual-param-${idx}-default`} aria-labelledby={`manual-param-${idx}-default-label`} className="w-full">
                                    <SelectValue />
                                  </SelectTrigger>
                                  <SelectContent>
                                    <SelectItem value={NO_DEFAULT}>{t_i18n("No default")}</SelectItem>
                                    {(param.options ?? []).map((opt) => (
                                      <SelectItem key={opt} value={opt}>{opt}</SelectItem>
                                    ))}
                                  </SelectContent>
                                </Select>
                              ) : (
                                <Input id={`manual-param-${idx}-default`} aria-label={t_i18n("Default value")}
                                  placeholder={t_i18n("Optional")}
                                  value={param.default ?? ""}
                                  onChange={(e) => setManualParams((prev) => prev.map((p, i) => i === idx ? { ...p, default: e.target.value } : p))}
                                />
                              )}
                            </div>
                          </div>
                          <div className="space-y-1">
                            <Label htmlFor={`manual-param-${idx}-description`}>{t_i18n("Description")}</Label>
                            <Input id={`manual-param-${idx}-description`} aria-label={t_i18n("Description")}
                              placeholder={t_i18n("Help text shown in the trigger form")}
                              value={param.description ?? ""}
                              onChange={(e) => setManualParams((prev) => prev.map((p, i) => i === idx ? { ...p, description: e.target.value } : p))}
                            />
                          </div>
                          <div className="flex items-center gap-4">
                            <div className="flex items-center gap-2">
                              <Switch id={`manual-param-${idx}-required`} aria-label={t_i18n("Required")}
                                checked={param.required ?? false}
                                onCheckedChange={(v) => setManualParams((prev) => prev.map((p, i) => i === idx ? { ...p, required: v } : p))}
                              />
                              <Label htmlFor={`manual-param-${idx}-required`}>{t_i18n("Required")}</Label>
                            </div>
                          </div>
                          {param.type === "select" && (
                            <div className="space-y-1">
                              <Label htmlFor={`manual-param-${idx}-options`}>{t_i18n("Options")}</Label>
                              <TagInput id={`manual-param-${idx}-options`} aria-label={t_i18n("Options")}
                                aria-describedby={`manual-param-${idx}-options-help`}
                                placeholder={t_i18n("Add option")}
                                value={param.options ?? []}
                                onChange={(opts) => setManualParams((prev) => prev.map((p, i) => i === idx
                                  // A removed option is no longer a valid default.
                                  ? { ...p, options: opts, default: p.default && opts.includes(p.default) ? p.default : "" }
                                  : p))}
                                // The values are the parameter's own closed set, shown back as
                                // typed: no platform tag belongs to it.
                                preserveCase
                                suggestFromPlatformTags={false}
                              />
                              <p id={`manual-param-${idx}-options-help`} className="content-caption text-default-secondary">
                                {t_i18n("The values the person running it picks from. Press Enter after each option.")}
                              </p>
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {/* ── Event matching this save switches to ── */}
              {saveSwitchesMatching() && (
                <div className="flex items-start gap-2 rounded-sm border border-accent/20 bg-accent/5 px-3 py-2">
                  <Filter className="h-3.5 w-3.5 mt-0.5 shrink-0 text-accent" />
                  <p className="content-caption text-default-secondary leading-relaxed">
                    {t_i18n("Saving switches this trigger to exact event matching: it will run only on the events it selects, never on other events whose name starts with them.")}
                  </p>
                </div>
              )}

              {/* ── Delay ── */}
              <div className="space-y-1.5">
                <Label htmlFor="assignment-trigger-delay">{t_i18n("Delay before execution")}</Label>
                <Input id="assignment-trigger-delay" aria-label={t_i18n("Delay before execution")} className="w-56" placeholder={t_i18n("e.g. 10m, 1h, 30s")} value={delay} onChange={(e) => setDelay(e.target.value)} />
                <p className="content-caption text-default-secondary">{t_i18n("Wait after trigger fires before running. Leave empty for immediate execution.")}</p>
              </div>
            </div>
          </div>
        ) : (
          /* ── Trigger read-only — 3-column grid ── */
          <div className="space-y-4">
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
              {/* Col 1: Trigger type */}
              <ConfigCard icon={TriggerIcon} iconColor="text-accent" label={t_i18n("Trigger type")} value={t_i18n(TRIGGER_LABELS[storedTile] ?? assignment.trigger_type)} />

              {/* Col 2: Schedule / Webhook / Integration */}
              {cronExpr && <ConfigCard icon={Clock} iconColor="text-feedback-info-primary" label={t_i18n("Schedule")} value={describeCron(cronExpr)} />}

              {assignment.trigger_type === "integration_event" && (() => {
                const cfg = assignment.trigger_config;
                const prov = cfg.provider ?? "";
                const evt = cfg.event ?? "";
                const integId = cfg.integration_id;
                const provInfo = catalog?.find((c) => c.provider === prov);
                const evtInfo = provInfo?.available_events?.find((e) => e.event === evt);
                // A trigger with no single event that selects several: each one is named.
                const listed = evt ? [] : (Array.isArray(cfg.events) ? cfg.events : []).filter((e) => typeof e === "string" && e.trim() !== "");
                const boundInteg = allIntegrations?.find((i) => i.id === integId);
                return (
                  <>
                    {/* Col 2: Integration */}
                    <div className="flex items-start gap-3 rounded-sm bg-elevation-hover px-4 py-3.5">
                      <img src={providerLogoSrc(prov)} alt={prov} className="h-[18px] w-[18px] mt-0.5 shrink-0" />
                      <div className="min-w-0 flex-1">
                        <p className="text-[13px] text-default-secondary leading-none">{t_i18n("Integration")}</p>
                        <p className="content-base font-semibold mt-1 truncate">{boundInteg?.name ?? provInfo?.name ?? prov}</p>
                      </div>
                    </div>
                    {/* Col 3: Event */}
                    {evt && (
                      <div className="flex items-start gap-3 rounded-sm bg-elevation-hover px-4 py-3.5">
                        <div className="mt-0.5 shrink-0 text-feedback-warning-primary"><Zap className="h-[18px] w-[18px]" /></div>
                        <div className="min-w-0 flex-1">
                          <p className="text-[13px] text-default-secondary leading-none">{t_i18n("Event")}</p>
                          <p className="content-base font-semibold mt-1">{evtInfo?.label ? t_text(evtInfo.label) : evt}</p>
                          {evtInfo?.description && <p className="content-compact text-default-secondary mt-0.5 leading-relaxed">{t_text(evtInfo.description)}</p>}
                        </div>
                      </div>
                    )}
                    {listed.length > 0 && (
                      <div className="flex items-start gap-3 rounded-sm bg-elevation-hover px-4 py-3.5">
                        <div className="mt-0.5 shrink-0 text-feedback-warning-primary"><Zap className="h-[18px] w-[18px]" /></div>
                        <div className="min-w-0 flex-1">
                          <p className="text-[13px] text-default-secondary leading-none">{t_i18n("Events")}</p>
                          <ul className="mt-1 space-y-1">
                            {listed.map((e) => {
                              const info = provInfo?.available_events?.find((a) => a.event === e);
                              return (
                                <li key={e}>
                                  <p className="content-base font-semibold">{info?.label ? t_text(info.label) : e}</p>
                                  {info?.description && <p className="content-compact text-default-secondary mt-0.5 leading-relaxed">{t_text(info.description)}</p>}
                                </li>
                              );
                            })}
                          </ul>
                        </div>
                      </div>
                    )}
                  </>
                );
              })()}
            </div>

            {/* Delay */}
            {assignment.delay && (
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                <ConfigCard icon={Timer} iconColor="text-feedback-warning-primary" label={t_i18n("Delay")} value={assignment.delay} />
              </div>
            )}

            {/* Event matching: which mode the trigger uses and what it matches */}
            {(assignment.trigger_type === "integration_event" || assignment.trigger_type === "webhook") && (() => {
              const cfg = assignment.trigger_config;
              const isEvent = assignment.trigger_type === "integration_event";
              const switchButton = (tip: string) => canManage && (aiOff ? (
                <AiDisabledTip>
                  <Button variant="outline" className="shrink-0" onClick={switchToExactMatching} disabled>
                    {t_i18n("Use exact matching")}
                  </Button>
                </AiDisabledTip>
              ) : (
                <Tip content={tip}>
                  <Button variant="outline" className="shrink-0" onClick={switchToExactMatching} disabled={update.isPending || aiOff}>
                    {t_i18n("Use exact matching")}
                  </Button>
                </Tip>
              ));
              // Stored by the agent builder under the old key: not read until switched.
              const unread = isEvent ? unreadLegacyEventType(cfg) : null;
              if (unread) {
                const providerName = INTEGRATION_PROVIDER_NAMES[cfg.provider ?? ""] ?? cfg.provider ?? "";
                return (
                  <div className="flex items-start gap-3 rounded-sm border border-feedback-alert-primary/30 bg-feedback-alert-primary/5 px-4 py-3">
                    <Filter className="h-4 w-4 mt-0.5 shrink-0 text-feedback-warning-primary" />
                    <div className="min-w-0 flex-1 space-y-1">
                      <p className="content-compact font-medium">{t_i18n("Event stored under the old event_type key")}</p>
                      <p className="content-compact text-default-secondary">
                        {t_i18n("This trigger names {event} under event_type, a key the platform does not read on it: it runs on every {provider} event delivered by webhook, and the poller never runs it.", { event: unread, provider: providerName })}
                      </p>
                    </div>
                    {switchButton(t_i18n("It then runs only on {event}; a polled trigger starts with its next new event.", { event: unread }))}
                  </div>
                );
              }
              if (assignment.event_matching === "exact") {
                const selected = isEvent && typeof cfg.event === "string" ? cfg.event.trim() : "";
                return (
                  <div className="flex items-start gap-3 rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-4 py-3">
                    <Filter className="h-4 w-4 mt-0.5 shrink-0 text-accent" />
                    <div className="min-w-0 flex-1 space-y-1">
                      <p className="content-compact font-medium">{t_i18n("Exact event matching")}</p>
                      <p className="content-compact text-default-secondary">
                        {selected
                          ? t_i18n("Runs on {event} and its actions only, never on other events whose name starts with it.", { event: selected })
                          : t_i18n("Runs only on the events it selects, never on other events whose name starts with them.")}
                      </p>
                    </div>
                  </div>
                );
              }
              if (assignment.event_matching !== "legacy") return null;
              const catalogEvents = (catalog?.find((c) => c.provider === cfg.provider)?.available_events ?? []).map((e) => e.event);
              const siblings = isEvent ? legacySiblingEvents(cfg.event ?? "", catalogEvents) : [];
              // Only a trigger that selects an event is widened on its dedicated URL:
              // with no selection it runs on every event under both matchings.
              const namesEvent = typeof cfg.event === "string" && cfg.event.trim() !== "";
              const everyGithubEvent = isEvent && namesEvent && cfg.provider === "github" && !!assignment.webhook_url;
              const prefixAllowList = hasPrefixAllowList(cfg.events);
              const widens = siblings.length > 0 || everyGithubEvent || prefixAllowList;
              return (
                <div className={cn(
                  "flex items-start gap-3 rounded-sm border px-4 py-3",
                  widens ? "border-feedback-alert-primary/30 bg-feedback-alert-primary/5" : "border-elevation-subtle-soft bg-elevation-hover",
                )}>
                  <Filter className={cn("h-4 w-4 mt-0.5 shrink-0", widens ? "text-feedback-warning-primary" : "text-default-secondary")} />
                  <div className="min-w-0 flex-1 space-y-1">
                    <p className="content-compact font-medium">{t_i18n("Legacy event matching")}</p>
                    {siblings.length > 0 && (
                      <p className="content-compact text-default-secondary">
                        {t_i18n("Saved before exact event matching, this trigger also runs on: {events}", { events: siblings.join(", ") })}
                      </p>
                    )}
                    {everyGithubEvent && (
                      <p className="content-compact text-default-secondary">{t_i18n("On its dedicated webhook URL it runs on every GitHub event delivered to it.")}</p>
                    )}
                    {prefixAllowList && (
                      <p className="content-compact text-default-secondary">{t_i18n("Saved before exact event matching, each entry of its events list also matches any event whose name starts with it.")}</p>
                    )}
                    {!widens && (
                      <p className="content-compact text-default-secondary">{t_i18n("Saved before exact event matching, it keeps its prefix match: its selection matches the same events either way.")}</p>
                    )}
                    <p className="content-caption text-default-secondary/80">
                      {t_i18n("Choosing its events again, or Use exact matching, switches it to exact matching. Any other edit keeps it as it is.")}
                    </p>
                  </div>
                  {switchButton(t_i18n("From now on it runs only on the events it selects."))}
                </div>
              );
            })()}

            {/* Filters (integration event only) */}
            {assignment.trigger_type === "integration_event" && (() => {
              const filter = assignment.trigger_config.filter ?? {};
              const scope = { ...(filter.scope ?? {}) };
              const fromFilter = filter.from_contains ?? "";
              // Migrate legacy channels into scope for display
              const legacyChannels = filter.channels ?? [];
              if (!Object.keys(scope).length && legacyChannels.length) {
                const lt = assignment.trigger_config.event === "new_teams_message" || assignment.trigger_config.event === "new_teams_reply" ? "teams_channel"
                  : assignment.trigger_config.event === "new_slack_message" ? "slack_channel" : "";
                if (lt) scope[lt] = legacyChannels;
              }
              const hasScope = Object.values(scope).some((ids) => ids.length > 0);
              if (!hasScope && !fromFilter) return null;
              const prov = assignment.trigger_config.provider ?? "";
              const integId = assignment.trigger_config.integration_id ?? "";
              return (
                <div className="space-y-2">
                  {hasScope && (
                    <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                      {Object.entries(scope).filter(([, ids]) => ids.length > 0).map(([elType, ids]) => (
                        <ScopeReadOnlyCard
                          key={elType}
                          integrationId={integId}
                          provider={prov}
                          elementType={elType}
                          elementIds={ids}
                          catalog={catalog}
                        />
                      ))}
                    </div>
                  )}
                  {fromFilter && (
                    <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                      <ConfigCard icon={Filter} iconColor="text-feedback-info-primary" label={t_i18n("Sender filter")} value={fromFilter} />
                    </div>
                  )}
                </div>
              );
            })()}

            {/* Filters evaluated before any run, and how many events they dropped */}
            {assignment.trigger_type === "integration_event" && (
              <EventFilterSummary
                filter={assignment.trigger_config.filter}
                event={assignment.trigger_config.event ?? ""}
                filteredCount={assignment.events_filtered_count ?? 0}
              />
            )}

            {/* What XTM One holds in the source platform for this trigger (OpenCTI, OpenAEV) */}
            {assignment.trigger_type === "integration_event" && (
              <ProvisioningSummary
                config={assignment.trigger_config}
                enabled={assignment.enabled}
                lastTriggeredAt={assignment.last_triggered_at}
              />
            )}

            {/* Loop protection (GitHub integration events) */}
            {assignment.trigger_type === "integration_event" &&
              assignment.trigger_config.provider === "github" &&
              assignment.ignore_self_triggered_events && (
                <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                  <ConfigCard
                    icon={ShieldCheck}
                    iconColor="text-feedback-success-primary"
                    label={t_i18n("Loop protection")}
                    value={t_i18n("Ignores self-triggered events")}
                  />
                </div>
              )}

            {/* Manual trigger parameters */}
            {assignment.trigger_type === "manual" && assignment.manual_trigger_parameters && assignment.manual_trigger_parameters.length > 0 && (
              <div className="space-y-2 rounded-sm bg-elevation-hover border border-elevation-subtle-soft p-4">
                <div className="flex items-center gap-2 mb-3">
                  <Variable className="h-4 w-4 text-filigran-ia-primary" />
                  <p className="content-compact font-medium text-default-secondary uppercase tracking-wider">{t_i18n("Input Parameters")}</p>
                </div>
                <div className="grid gap-2">
                  {assignment.manual_trigger_parameters.map((p) => (
                    <div key={p.key} className="flex items-center gap-3 rounded-sm bg-elevation-hover px-3 py-2">
                      <code className="content-caption text-accent font-mono-plex shrink-0">{p.key}</code>
                      <span className="content-compact text-default-primary">{p.label}</span>
                      <span className="content-caption text-default-secondary capitalize">{p.type}</span>
                      {p.required && <span className="content-caption text-feedback-error-primary font-medium">{t_i18n("required")}</span>}
                      {p.default && <span className="content-caption text-default-secondary">= {p.default}</span>}
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Work item inbox */}
            {assignment.trigger_type === "work_item" && (
              <div className="space-y-2 rounded-sm bg-elevation-hover border border-elevation-subtle-soft p-4">
                <div className="flex items-center gap-2 mb-1">
                  <ListChecks className="h-4 w-4 text-accent" />
                  <p className="content-compact font-medium text-default-secondary uppercase tracking-wider">{t_i18n("Work item inbox")}</p>
                </div>
                <p className="content-caption text-default-secondary leading-relaxed">
                  {t_i18n("Automatically claims and runs open work items owned by this agent that are due (or have no due date). Each item completes on success and returns to the queue on failure - no manual pickup.")}
                </p>
                {(assignment.trigger_config.correlation_prefix || assignment.trigger_config.priority_min || inboxSlots(assignment.trigger_config) > 1) && (
                  <div className="flex flex-wrap gap-2 content-caption">
                    {assignment.trigger_config.correlation_prefix && (
                      <span className="rounded-sm bg-elevation-hover px-2 py-0.5">{t_i18n("key prefix:")} <code className="text-accent">{assignment.trigger_config.correlation_prefix}</code></span>
                    )}
                    {assignment.trigger_config.priority_min && (
                      <span className="rounded-sm bg-elevation-hover px-2 py-0.5 capitalize">{t_i18n("min priority: {priority}", { priority: assignment.trigger_config.priority_min })}</span>
                    )}
                    {inboxSlots(assignment.trigger_config) > 1 && (
                      <span className="rounded-sm bg-elevation-hover px-2 py-0.5">{t_i18n("{count, plural, one {up to # item in parallel} other {up to # items in parallel}}", { count: inboxSlots(assignment.trigger_config) })}</span>
                    )}
                  </div>
                )}
              </div>
            )}

            {/* Platform event */}
            {storedTile === CTEM_SIGNAL_TILE ? (
              <CtemSignalSummary config={assignment.trigger_config} />
            ) : (
              assignment.trigger_type === "platform_event" && <PlatformEventSummary config={assignment.trigger_config} />
            )}

            {/* Webhook URLs */}
            {assignment.trigger_type === "webhook" && assignment.webhook_url && (
              <div className="space-y-3 rounded-sm bg-elevation-hover border border-elevation-subtle-soft p-4">
                <div className="flex items-center gap-2">
                  <Webhook className="h-4 w-4 text-accent" />
                  <p className="content-compact font-medium text-default-secondary uppercase tracking-wider">{t_i18n("Webhook URL")}</p>
                </div>
                <div className="flex items-center gap-2">
                  <code className="flex-1 rounded-sm bg-elevation-hover px-2.5 py-1.5 content-caption text-accent font-mono-plex break-all select-all">
                    {assignment.webhook_url}
                  </code>
                  <button
                    className="shrink-0 rounded-sm p-1.5 hover:bg-elevation-hover transition-colors"
                    onClick={() => assignment.webhook_url && copyUrl(assignment.webhook_url)}
                  >
                    {copiedUrl === assignment.webhook_url
                      ? <Check className="h-3.5 w-3.5 text-feedback-success-primary" />
                      : <Copy className="h-3.5 w-3.5 text-default-secondary" />}
                  </button>
                </div>
                {/* The secret is a webhook credential — only users who can
                    manage the assignment may see it (the API also strips it
                    for non-admin viewers of company-managed assignments). */}
                {canManage && assignment.trigger_config.webhook_secret && (
                  <div className="space-y-1.5">
                    <p className="content-caption text-default-secondary flex items-center gap-1.5">
                      <ShieldCheck className="h-3.5 w-3.5 text-feedback-success-primary" />
                      {t_i18n("Webhook secret")} <span className="text-default-secondary">{t_i18n("(authenticate requests with this secret)")}</span>
                    </p>
                    <div className="flex items-center gap-2">
                      <code className="flex-1 rounded-sm bg-elevation-hover px-2.5 py-1.5 content-caption text-feedback-success-primary font-mono-plex break-all select-all">
                        {assignment.trigger_config.webhook_secret}
                      </code>
                      <button
                        className="shrink-0 rounded-sm p-1.5 hover:bg-elevation-hover transition-colors"
                        onClick={() => assignment.trigger_config.webhook_secret && copyUrl(assignment.trigger_config.webhook_secret)}
                      >
                        {copiedUrl === assignment.trigger_config.webhook_secret
                          ? <Check className="h-3.5 w-3.5 text-feedback-success-primary" />
                          : <Copy className="h-3.5 w-3.5 text-default-secondary" />}
                      </button>
                    </div>
                    <p className="content-caption text-default-secondary/80 leading-relaxed">
                      {t_i18n("Send the secret verbatim as a static token. This is the only scheme most SIEM and SOAR webhook actions can emit:")} <code className="text-accent bg-accent/5 px-1 rounded-sm">X-Webhook-Token</code>, <code className="text-accent bg-accent/5 px-1 rounded-sm">X-Webhook-Secret</code> {t_i18n("or")} <code className="text-accent bg-accent/5 px-1 rounded-sm">Authorization: Bearer</code>.
                    </p>
                    <p className="content-caption text-default-secondary leading-relaxed">
                      {t_i18n("Senders that can compute an HMAC may sign the raw body instead:")} <code className="text-accent bg-accent/5 px-1 rounded-sm">X-Signature</code> {t_i18n("(hex HMAC-SHA256), or a provider scheme:")} <code className="text-accent bg-accent/5 px-1 rounded-sm">X-Hub-Signature-256</code>, <code className="text-accent bg-accent/5 px-1 rounded-sm">X-Slack-Signature</code>, <code className="text-accent bg-accent/5 px-1 rounded-sm">X-OpenCTI-Token</code>.
                    </p>
                  </div>
                )}
                {/* Ready-to-paste request. The static-token header is the only
                    scheme most SIEM/SOAR webhook actions can emit — they cannot
                    compute a body HMAC — so it leads the example. */}
                {curlSnippet && (
                  <div className="space-y-1.5 pt-1 border-t border-elevation-subtle-soft">
                    <div className="flex items-center justify-between gap-2">
                      <p className="content-caption text-default-secondary flex items-center gap-1.5">
                        <Terminal className="h-3.5 w-3.5 text-accent" />
                        {t_i18n("How to trigger")}
                      </p>
                      <button
                        className="shrink-0 rounded-sm p-1.5 hover:bg-elevation-hover transition-colors"
                        onClick={() => copyUrl(curlSnippet)}
                      >
                        {copiedUrl === curlSnippet
                          ? <Check className="h-3.5 w-3.5 text-feedback-success-primary" />
                          : <Copy className="h-3.5 w-3.5 text-default-secondary" />}
                      </button>
                    </div>
                    <pre className="overflow-x-auto rounded-sm bg-elevation-hover px-2.5 py-2 content-caption font-mono-plex text-default-secondary leading-relaxed select-all">
                      {curlSnippet}
                    </pre>
                    <p className="content-caption text-default-secondary leading-relaxed">
                      {t_i18n("The whole request body is passed to the agent as the trigger payload.")}
                    </p>
                  </div>
                )}
              </div>
            )}

            {assignment.trigger_type === "integration_event" && (() => {
              const prov = assignment.trigger_config.provider ?? "";
              const provInfo = catalog?.find((c) => c.provider === prov);
              const supportsAssignmentWebhooks = provInfo?.supports_assignment_webhooks ?? false;
              if (!supportsAssignmentWebhooks || !assignment.webhook_url) return null;
              return (
                <div className="space-y-3 rounded-sm bg-elevation-hover border border-elevation-subtle-soft p-4">
                  <div className="flex items-center gap-2">
                    <Webhook className="h-4 w-4 text-accent" />
                    <p className="content-compact font-medium text-default-secondary uppercase tracking-wider">{t_i18n("Webhook URLs")}</p>
                  </div>

                  {/* Dedicated URL */}
                  <div className="space-y-1.5">
                    <p className="content-caption text-default-secondary flex items-center gap-1.5">
                      <span className="inline-block h-1.5 w-1.5 rounded-full bg-accent" />
                      {t_i18n("Dedicated URL")} <span className="text-default-secondary">{t_i18n("(triggers only this assignment)")}</span>
                    </p>
                    <div className="flex items-center gap-2">
                      <code className="flex-1 rounded-sm bg-elevation-hover px-2.5 py-1.5 content-caption text-accent font-mono-plex break-all select-all">
                        {assignment.webhook_url}
                      </code>
                      <button
                        className="shrink-0 rounded-sm p-1.5 hover:bg-elevation-hover transition-colors"
                        onClick={() => assignment.webhook_url && copyUrl(assignment.webhook_url)}
                      >
                        {copiedUrl === assignment.webhook_url
                          ? <Check className="h-3.5 w-3.5 text-feedback-success-primary" />
                          : <Copy className="h-3.5 w-3.5 text-default-secondary" />}
                      </button>
                    </div>
                  </div>

                  {/* Integration URL */}
                  {assignment.integration_webhook_url && (
                    <div className="space-y-1.5">
                      <p className="content-caption text-default-secondary flex items-center gap-1.5">
                        <span className="inline-block h-1.5 w-1.5 rounded-full bg-feedback-info-primary" />
                        {t_i18n("Integration URL")} <span className="text-default-secondary">{t_i18n("(triggers all matching assignments)")}</span>
                      </p>
                      <div className="flex items-center gap-2">
                        <code className="flex-1 rounded-sm bg-elevation-hover px-2.5 py-1.5 content-caption text-feedback-info-primary font-mono-plex break-all select-all">
                          {assignment.integration_webhook_url}
                        </code>
                        <button
                          className="shrink-0 rounded-sm p-1.5 hover:bg-elevation-hover transition-colors"
                          onClick={() => assignment.integration_webhook_url && copyUrl(assignment.integration_webhook_url)}
                        >
                          {copiedUrl === assignment.integration_webhook_url
                            ? <Check className="h-3.5 w-3.5 text-feedback-success-primary" />
                            : <Copy className="h-3.5 w-3.5 text-default-secondary" />}
                        </button>
                      </div>
                    </div>
                  )}

                  <p className="content-caption text-default-secondary leading-relaxed">
                    {t_i18n("Use the")} <strong className="text-default-secondary/80">{t_i18n("dedicated URL")}</strong> {t_i18n("in {provider} to trigger only this assignment. The integration URL triggers all assignments bound to this integration that match the event.", { provider: provInfo?.name ?? prov })}
                  </p>

                  {/* Auto-create webhook for GitHub */}
                  {provInfo?.supports_webhook_management && canManage && (
                    <AutoCreateWebhookSection
                      assignment={assignment}
                      integrationId={assignment.trigger_config.integration_id ?? ""}
                    />
                  )}
                </div>
              );
            })()}
          </div>
        )}
      </CardContent>
    </Card>
  );
}


// ---------------------------------------------------------------------------
// ScopePicker — generic element scope picker for trigger filters
// Uses the same integration elements API as ElementTypeEditor but tailored
// for the trigger filter context. Works for any element type: Teams channels,
// Slack channels, Jira projects, GitHub repos, etc.
// ---------------------------------------------------------------------------

function ScopePicker({
  integrationId, provider, elementType, elementLabel, elementDescription,
  selectedIds, onSelectionChange,
}: {
  integrationId: string;
  provider: string;
  elementType: string;
  elementLabel: string;
  elementDescription: string;
  selectedIds: string[];
  onSelectionChange: (ids: string[]) => void;
}) {
  const { t_i18n } = useFormatter();
  const { data, isLoading, error } = useIntegrationElements(integrationId, elementType);
  const [search, setSearch] = useState("");

  // A failed refetch keeps the last list; the refusal wins over it.
  const elements = error ? [] : (data?.elements ?? []);
  const selectedSet = useMemo(() => new Set(selectedIds), [selectedIds]);

  const needle = search.toLowerCase().trim();
  const filtered = needle
    ? elements.filter((el) => el.name.toLowerCase().includes(needle) || el.group?.toLowerCase().includes(needle))
    : elements;

  const groups = useMemo(() => {
    const m = new Map<string, IntegrationElement[]>();
    for (const el of filtered) {
      const g = el.group || elementLabel;
      if (!m.has(g)) m.set(g, []);
      m.get(g)!.push(el);
    }
    return m;
  }, [filtered, elementLabel]);

  const toggle = (id: string) => {
    onSelectionChange(selectedSet.has(id) ? selectedIds.filter((i) => i !== id) : [...selectedIds, id]);
  };

  const providerName = INTEGRATION_PROVIDER_NAMES[provider] ?? provider;

  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between">
        <label className="content-compact text-default-secondary">{t_i18n("{label} scope", { label: elementLabel })}</label>
        {selectedIds.length > 0 && (
          <span className="content-caption text-accent bg-accent/10 rounded-sm px-1.5 py-0.5">
            {t_i18n("{count} selected", { count: selectedIds.length })}
          </span>
        )}
      </div>

      {isLoading ? (
        <div className="flex items-center gap-2 py-3 rounded-sm border border-elevation-subtle-soft px-3">
          <Spinner size="sm" tone="inherit" className="text-default-secondary" />
          <span className="content-caption text-default-secondary">{t_i18n("Loading {provider} {element}s...", { provider: providerName, element: elementLabel.toLowerCase() })}</span>
        </div>
      ) : elements.length === 0 ? (
        <div className="rounded-sm bg-feedback-alert-primary/5 border border-feedback-alert-primary/10 p-3">
          <p className="content-caption text-feedback-warning-primary">{error ? error.message : t_i18n("No {element}s found. Make sure the {provider} integration is connected and has the right permissions.", { element: elementLabel.toLowerCase(), provider: providerName })}</p>
        </div>
      ) : (
        <div className="rounded-sm border border-elevation-subtle-soft overflow-hidden">
          {elements.length > 6 && (
            <div className="border-b border-elevation-subtle-soft p-2">
              <SearchInput
                className="max-w-none"
                aria-label={t_i18n("Search {element}s...", { element: elementLabel.toLowerCase() })}
                placeholder={t_i18n("Search {element}s...", { element: elementLabel.toLowerCase() })}
                value={search}
                onChange={setSearch}
              />
            </div>
          )}
          <div className="max-h-48 overflow-y-auto">
            {filtered.length === 0 ? (
              <p className="content-caption text-default-secondary italic py-3 text-center">{t_i18n('No matches for "{query}"', { query: search })}</p>
            ) : (
              [...groups.entries()].map(([groupName, groupElements]) => (
                <div key={groupName}>
                  {groups.size > 1 && (
                    <p className="content-caption text-default-secondary uppercase tracking-wider px-2.5 pt-2 pb-1">{groupName}</p>
                  )}
                  {groupElements.map((el) => (
                    <label
                      key={el.id}
                      className={cn(
                        "flex items-center gap-2.5 px-2.5 py-1.5 cursor-pointer transition-colors",
                        selectedSet.has(el.id) ? "bg-accent/5" : "hover:bg-elevation-hover",
                      )}
                    >
                      <Switch
                        checked={selectedSet.has(el.id)}
                        onCheckedChange={() => toggle(el.id)}
                        className="h-4 w-7 data-[state=checked]:bg-accent"
                      />
                      <span className={cn("content-compact", selectedSet.has(el.id) && "text-accent")}>{el.name}</span>
                    </label>
                  ))}
                </div>
              ))
            )}
          </div>
        </div>
      )}
      <p className="content-caption text-default-secondary">{elementDescription || t_i18n("Leave empty to include all {element}s.", { element: elementLabel.toLowerCase() })}</p>
    </div>
  );
}


// ---------------------------------------------------------------------------
// ScopeReadOnlyCard — resolves element IDs to display names for read-only
// ---------------------------------------------------------------------------

function ScopeReadOnlyCard({
  integrationId, provider, elementType, elementIds, catalog,
}: {
  integrationId: string;
  provider: string;
  elementType: string;
  elementIds: string[];
  catalog: ProviderCatalogItem[] | undefined;
}) {
  const { data } = useIntegrationElements(integrationId, elementType);

  const etLabel = (catalog ?? [])
    .find((c) => c.provider === provider)
    ?.element_types.find((et) => et.type === elementType)?.name ?? elementType;

  const idToName = useMemo(() => {
    const m = new Map<string, string>();
    for (const el of data?.elements ?? []) {
      m.set(el.id, el.group ? `${el.group} / ${el.name}` : el.name);
    }
    return m;
  }, [data]);

  const labels = elementIds.map((id) => idToName.get(id) ?? id);

  return (
    <div className="flex items-start gap-3 rounded-sm bg-elevation-hover px-4 py-3.5">
      <div className="mt-0.5 shrink-0 text-feedback-info-primary"><Filter className="h-[18px] w-[18px]" /></div>
      <div className="min-w-0 flex-1">
        <p className="text-[13px] text-default-secondary leading-none">{etLabel}</p>
        <div className="flex flex-wrap gap-1 mt-1.5">
          {labels.map((label, i) => (
            <span key={i} className="rounded-sm bg-elevation-hover px-2 py-0.5 content-compact">{label}</span>
          ))}
        </div>
      </div>
    </div>
  );
}


// ---------------------------------------------------------------------------
// Auto-create webhook section for GitHub integration_event triggers
// ---------------------------------------------------------------------------

function AutoCreateWebhookSection({
  assignment,
  integrationId,
}: {
  assignment: Assignment;
  integrationId: string;
}) {
  const { t_i18n, intl } = useFormatter();
  const { toast } = useToast();
  const { data: webhooksData } = useGitHubWebhooks(integrationId, !!integrationId);
  const createWebhook = useCreateGitHubWebhook();
  const deleteWebhook = useDeleteGitHubWebhook();
  const { data: repoData, isLoading: reposLoading, error: reposError } = useIntegrationElements(
    integrationId,
    "repository",
  );

  const [showForm, setShowForm] = useState(false);
  const [selectedRepo, setSelectedRepo] = useState("");
  const [manualRepo, setManualRepo] = useState("");
  const [createError, setCreateError] = useState<string | null>(null);

  const selectRepo = (repo: string) => {
    setSelectedRepo(repo);
    setCreateError(null);
  };

  const closeForm = () => {
    setShowForm(false);
    setSelectedRepo("");
    setManualRepo("");
    setCreateError(null);
  };

  const assignmentWebhooks = (webhooksData?.webhooks ?? []).filter(
    (wh) => wh.target === "assignment" && wh.assignment_id === assignment.id,
  );
  // A failed refetch keeps the last list; the refusal wins over it.
  const repos = reposError ? [] : (repoData?.elements ?? []);

  const handleCreate = () => {
    if (!selectedRepo) return;
    const assignmentEvent = assignment.trigger_config.event;
    const webhookEvents = assignmentEvent ? [assignmentEvent] : ["push"];

    setCreateError(null);
    createWebhook.mutate(
      {
        integrationId,
        repo: selectedRepo,
        events: webhookEvents,
        target: "assignment",
        assignment_id: assignment.id,
      },
      {
        onSuccess: () => {
          toast({ title: t_i18n("Webhook created on {repo}", { repo: selectedRepo }), variant: "success" });
          closeForm();
        },
        onError: (err) => setCreateError(err.message || t_i18n("Failed to create webhook")),
      },
    );
  };

  const handleDelete = (hookId: number, repo: string) => {
    deleteWebhook.mutate(
      { integrationId, hookId },
      {
        onSuccess: () => toast({ title: t_i18n("Webhook removed from {repo}", { repo }), variant: "success" }),
        onError: (err) => toast({ title: err.message || t_i18n("Failed to delete"), variant: "error" }),
      },
    );
  };

  return (
    <div className="mt-3 pt-3 border-t border-elevation-subtle-soft space-y-2">
      <div className="flex items-center justify-between">
        <p className="content-caption text-default-secondary flex items-center gap-1.5">
          <GitBranch className="h-3.5 w-3.5 text-accent" />
          <span className="font-medium">{t_i18n("Auto-create webhook")}</span>
        </p>
        {!showForm && (
          <Button
            variant="ghost"
            className="gap-1"
            onClick={() => setShowForm(true)}
          >
            <Plus className="h-3 w-3" /> {t_i18n("Add to repository")}
          </Button>
        )}
      </div>

      {/* Existing assignment-level webhooks */}
      {assignmentWebhooks.length > 0 && (
        <div className="space-y-1">
          {assignmentWebhooks.map((wh) => (
            <div
              key={wh.hook_id}
              className="flex items-center gap-2 rounded-sm bg-elevation-hover px-2.5 py-1.5 group"
            >
              <GitBranch className="h-3 w-3 text-feedback-success-primary shrink-0" />
              <span className="content-caption flex-1 truncate">{wh.repo}</span>
              <span className="content-caption text-default-secondary">{formatTimeAgo(intl, wh.created_at)}</span>
              <Tip content={t_i18n("Remove webhook")}>
                <button aria-label={t_i18n("Remove webhook")}
                  className="shrink-0 rounded-sm p-0.5 text-default-disabled hover:text-feedback-error-primary transition-colors opacity-0 group-hover:opacity-100"
                  onClick={() => handleDelete(wh.hook_id, wh.repo)}
                >
                  <Trash2 className="h-3 w-3" />
                </button>
              </Tip>
            </div>
          ))}
        </div>
      )}

      {assignmentWebhooks.length === 0 && !showForm && (
        <p className="content-caption text-default-secondary italic">
          {t_i18n("Automatically register a webhook on a GitHub repository to trigger this assignment.")}
        </p>
      )}

      {/* Add form */}
      {showForm && (
        <div className="rounded-sm border border-accent/20 bg-accent/[0.02] p-2.5 space-y-2">
          <div className="flex gap-2">
            <Input
              type="text"
              aria-label={t_i18n("Repository")}
              className="flex-1"
              placeholder="owner/repo"
              value={manualRepo}
              onChange={(e) => setManualRepo(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && manualRepo.includes("/")) {
                  selectRepo(manualRepo.trim());
                }
              }}
            />
            <Button
              variant="ghost"
              onClick={() => manualRepo.includes("/") && selectRepo(manualRepo.trim())}
              disabled={!manualRepo.includes("/")}
            >
              {t_i18n("Use")}
            </Button>
          </div>

          {/* Private repos dropdown */}
          {reposLoading ? (
            <div className="flex items-center gap-1.5 py-1 content-caption text-default-secondary">
              <Spinner size="sm" tone="inherit" /> {t_i18n("Loading repos...")}
            </div>
          ) : repos.length > 0 ? (
            <div className="max-h-28 overflow-y-auto space-y-0.5">
              {repos.slice(0, 30).map((r) => (
                <button
                  key={r.id}
                  className={cn(
                    "w-full flex items-center gap-1.5 rounded-sm px-2 py-1 text-left content-caption transition-colors",
                    selectedRepo === r.id
                      ? "bg-accent/10 text-accent"
                      : "hover:bg-elevation-hover text-default-secondary",
                  )}
                  onClick={() => { selectRepo(r.id); setManualRepo(r.id); }}
                >
                  <GitBranch className="h-2.5 w-2.5 shrink-0" />
                  <span className="truncate">{r.id}</span>
                </button>
              ))}
            </div>
          ) : reposError ? (
            <p className="content-caption text-feedback-warning-primary dark:text-feedback-warning-primary">{reposError.message}</p>
          ) : null}

          {selectedRepo && (
            <p className="content-caption text-accent">
              {t_i18n("Will create webhook on")} <strong>{selectedRepo}</strong>
            </p>
          )}

          {createError && (
            <div role="alert" className="flex items-start gap-2 rounded-sm border border-feedback-error-primary/20 bg-feedback-error-primary/[0.06] px-2.5 py-2">
              <AlertCircle className="h-3.5 w-3.5 text-feedback-error-primary shrink-0 mt-0.5" />
              <p className="flex-1 content-caption text-feedback-error-primary/90 leading-relaxed whitespace-pre-line">{createError}</p>
              <button
                className="shrink-0 text-feedback-error-primary/60 hover:text-feedback-error-primary transition-colors"
                onClick={() => setCreateError(null)}
                aria-label={t_i18n("Dismiss")}
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
          )}

          <div className="flex justify-end gap-1.5">
            <Button
              variant="ghost"
              onClick={closeForm}
            >
              {t_i18n("Cancel")}
            </Button>
            <Button
              className="gap-1"
              onClick={handleCreate}
              disabled={!selectedRepo || createWebhook.isPending} aria-busy={createWebhook.isPending || undefined}>
              {createWebhook.isPending && <Spinner size="sm" tone="inherit" />}{!createWebhook.isPending && <Webhook className="h-3 w-3" />}
              {t_i18n("Create")}
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
