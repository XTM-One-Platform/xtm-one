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

import { useMemo, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  Bot, MessageSquare, Zap, Cog, Sparkles,
  BookOpen, Power, PowerOff, Play, Plus, Activity,
  BarChart3, Settings2, CheckCircle2, XCircle,
  Globe, Code2, Brain, ArrowLeftRight,
  Terminal, Tag, ChevronDown, Users, Filter,
  ShieldAlert, Repeat, Thermometer, GraduationCap, ExternalLink, Pencil,
} from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { StatCard } from "@/components/ui/stat-card";
import { Badge } from "@/components/ui/badge";
import { CountBadge } from "@/components/ui/count-badge";
import { isEmergencyStopped } from "@/lib/emergency-stop";
import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/toast";
import { useAgents } from "@/hooks/api/useAgentApi";
import { useCustomTools } from "@/hooks/api/useCustomToolApi";
import { isA2AProvider } from "@/hooks/api/useA2AApi";
import { useA2ADirection } from "@/hooks/api/useA2AClientApi";
import { useChatSendEnabled } from "@/hooks/usePlatformConfig";
import { useIntegrations, useIntegrationTools } from "@/hooks/api/useIntegrationApi";
import { useKnowledgeBases } from "@/hooks/api/useKnowledgeBaseApi";
import { useMCPServers } from "@/hooks/api/useMCPServerApi";
import { useSkills } from "@/hooks/api/useSkillApi";
import { useAvailableModels, useGeneralAssistantConfig, useLLMProviders } from "@/hooks/api/useSettingsApi";
import { useUpdateAssignment, useTriggerAssignment, useAllAssignmentRuns, useAssignmentRunCount } from "@/hooks/api/useAssignmentApi";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useCredentialSharingConfirm } from "@/hooks/useCredentialSharingConfirm";
import { AiDisabledTip } from "@/components/ui/ai-disabled";
import { TimestampGrid } from "@/components/ui/timestamp-grid";
import { cn, formatCompactNumber, formatDate, formatKBStats, formatShortDate, formatTime, getAgentRequiredTemplateProviders, providerConfigName, providerDisplayName } from "@/lib/utils";
import { effectiveAllowedTools } from "@/lib/tool-scope";
import { canStandInForGeneralAssistant } from "@/lib/general-assistant-stand-in";
import {
  ICON_MAP, TRIGGER_ICONS, TRIGGER_LABELS, describeCron,
  INTEGRATION_PROVIDER_NAMES, TRANSPORT_DISPLAY_NAMES,
  HANDOVER_TYPES, MAX_ITERATIONS_DEFAULT, clampMaxIterations, handoverTypeConfig, providerLogoSrc } from "@/lib/constants";
import { shownHandoverType } from "@/lib/handover-types";
import { AddAssignmentPanel, type AssignmentPreset } from "./AddAssignmentPanel";
import { HealthImprovementRow } from "@/components/health/HealthImprovementRow";
import { AgentFleetCard } from "./AgentFleetCard";
import { ComingUpCard } from "./ComingUpCard";
import { a2aBindingLabel } from "./a2a-binding-label";
import { assignmentDisplayStatus, assignmentStatusTint } from "../assignment-detail/assignment-status";
import { AssignmentRowBadge } from "./AssignmentRowBadge";
import { useOwnerDeactivatedRemedy } from "@/components/ui/owner-deactivated-notice";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { useRoutingModelLabel } from "@/hooks/useRoutingModelLabel";
import type { Agent, Assignment } from "@/types";
import { triggerTile } from "@/lib/ctem-signal-trigger";

// ---------------------------------------------------------------------------
// Overview: Integration row with tool count
// ---------------------------------------------------------------------------
function OverviewIntegrationRow({
  integration,
  allowedTools,
  onClick,
}: {
  integration: { id: string; provider: string; name: string; status: string };
  allowedTools: string[] | null;
  onClick: () => void;
}) {
  const { t_i18n } = useFormatter();
  // An A2A binding is a partner relationship, not a tool set, so the tools
  // endpoint would answer an empty list and "0/0 tools" would read as a
  // broken binding. The badge says what the binding does instead, which
  // depends on the registration's direction - see `a2aBindingLabel`. The
  // lookup is optional to the page, so a failed one only leaves the badge out,
  // and it is not polled: a bound registration cannot change direction.
  const isA2A = isA2AProvider(integration.provider);
  const a2aOffered = useChatSendEnabled();
  const a2aDirection = useA2ADirection(isA2A ? integration : undefined, { throwOnError: false, refetchInterval: false });
  const a2aLabel = a2aBindingLabel(a2aOffered, a2aDirection);
  const { data: toolsData } = useIntegrationTools(integration.id, !isA2A);
  const totalTools = toolsData?.tools?.length ?? null;
  const enabledCount = allowedTools !== null ? allowedTools.length : totalTools;

  return (
    <div data-slot="integration-binding" className="flex items-center gap-3 rounded-sm bg-elevation-hover px-3 py-2.5 cursor-pointer hover:bg-elevation-highlight transition-colors" onClick={onClick}>
      <img src={providerLogoSrc(integration.provider)} alt={integration.provider} className="h-5 w-5 shrink-0" />
      <div className="min-w-0 flex-1">
        <p className="content-base font-medium truncate">{integration.name}</p>
        <p className="content-caption text-default-secondary">{INTEGRATION_PROVIDER_NAMES[integration.provider] ?? integration.provider}</p>
      </div>
      {isA2A ? (
        a2aLabel && (
          <span className="text-[9px] font-medium rounded-sm px-1.5 py-0.5 shrink-0 text-accent bg-accent/10">
            {t_i18n(a2aLabel)}
          </span>
        )
      ) : enabledCount !== null && (
        <span className="text-[9px] font-medium rounded-sm px-1.5 py-0.5 shrink-0 text-feedback-warning-tertiary bg-feedback-alert-primary/10">
          {t_i18n("{shown} {count, plural, one {tool} other {tools}}", {
            shown: totalTools !== null ? `${formatCompactNumber(enabledCount as number)}/${formatCompactNumber(totalTools)}` : formatCompactNumber(enabledCount as number),
            count: enabledCount as number,
          })}
        </span>
      )}
      {integration.status === "connected" ? <CheckCircle2 className="h-4 w-4 text-feedback-success-primary shrink-0" /> : <XCircle className="h-4 w-4 text-feedback-error-primary shrink-0" />}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Overview Tab
// ---------------------------------------------------------------------------
export function OverviewTab({ agent, assignments, canManage }: { agent: Agent; assignments?: Assignment[]; canManage: boolean }) {
  const { t_i18n } = useFormatter();
  const routingModel = useRoutingModelLabel();
  const navigate = useNavigate();
  const update = useUpdateAssignment();
  const [consentElement, confirmSharing] = useCredentialSharingConfirm();
  // Resuming a webhook assignment answers whoever holds its URL again.
  const toggleAssignment = async (assignment: Assignment) => {
    if (!assignment.enabled && !(await confirmSharing({ entity_type: "assignment", entity_id: assignment.id, change: { enabled: true } }, { confirmLabel: t_i18n("Resume") }))) return;
    update.mutate({ id: assignment.id, enabled: !assignment.enabled });
  };
  const triggerWf = useTriggerAssignment();
  /* An assignment always runs an agent, so Run always needs AI. */
  const aiOff = useAiActionDisabled();
  const ownerPausedRemedy = useOwnerDeactivatedRemedy("assignment");
  const { toast } = useToast();
  // Everything the agent is bound to, whoever owns it: an admin sees a
  // colleague's private bindings here (the admin scope).
  const { data: integrations } = useIntegrations({ scope: "all" });
  const { data: mcpServers } = useMCPServers({ scope: "all" });
  const { data: customTools } = useCustomTools({ scope: "all" });
  const { data: kbs } = useKnowledgeBases({ scope: "all" });
  const { data: allAgents } = useAgents({ scope: "all" });
  const { data: skills } = useSkills({ scope: "all" });
  const { data: availableModels } = useAvailableModels({ scope: "all" });
  const { data: llmProviders } = useLLMProviders();
  const { data: gaConfig } = useGeneralAssistantConfig();
  const platformMaxIterations = clampMaxIterations(gaConfig?.max_iterations ?? MAX_ITERATIONS_DEFAULT);
  const { data: runningRuns } = useAllAssignmentRuns(
    20, 0, "running", undefined, undefined, undefined, agent.id, undefined, undefined, true, undefined, "all",
  );
  // `?add-assignment=ctem-case` (the CTEM case page, #1266): the panel opens on the case events.
  // Read once: the preset applies to the first open only, and closing the panel drops it and the parameter.
  const [searchParams, setSearchParams] = useSearchParams();
  const [preset, setPreset] = useState<AssignmentPreset | undefined>(() =>
    searchParams.get("add-assignment") === "ctem-case"
      ? { triggerType: "platform_event", platformEvents: ["ctem_case.opened", "ctem_case.reopened"], name: t_i18n("Work CTEM cases") }
      : undefined,
  );
  const [addingAssignment, setAddingAssignment] = useState(() => Boolean(preset) && canManage);
  const closeAddAssignment = () => {
    setAddingAssignment(false);
    if (!preset) return;
    setPreset(undefined);
    setSearchParams((params) => {
      const next = new URLSearchParams(params);
      next.delete("add-assignment");
      return next;
    }, { replace: true });
  };
  const assignmentsCardRef = useRef<HTMLDivElement>(null);
  // The Fleet card's "Add Assignment" (#9539 S5, UX round 1 B1): opens this page's panel and brings it into view.
  const openAddAssignment = () => {
    setAddingAssignment(true);
    assignmentsCardRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  };
  const [personaExpanded, setPersonaExpanded] = useState(false);

  // Map assignment IDs that have a running run for this agent
  const runningAssignmentIds = useMemo(() => new Set(
    (runningRuns ?? [])
      .filter((r) => r.agent_id === agent.id)
      .map((r) => r.assignment_id)
  ), [runningRuns, agent.id]);

  // "Active" counts what still starts runs: an ended platform trigger is not.
  const enabledAssignments = useMemo(() => assignments?.filter((a) => assignmentDisplayStatus(a) === "active").length || 0, [assignments]);

  // Runs and last activity come from the agent's actual runs, NOT from
  // summing its assignments' counters. Plenty of real work carries no
  // assignment at all — an external A2A task, a sub-agent task, a run
  // spawned from chat — and those rows have `assignment_id = NULL`, so an
  // assignment-derived total reports "No runs yet" for an agent a partner
  // has been tasking all day. Both queries are already agent-scoped and
  // top-level only ("none"), matching what the Activity tab lists.
  const { data: runCount } = useAssignmentRunCount(
    undefined,        // status
    undefined,        // search
    undefined,        // notifications
    undefined,        // ownership
    agent.id,         // agentId
    undefined,        // assignmentId
    undefined,        // digests
    "none",           // parentRunId — top-level only; children are nested in the tree
    undefined,        // a2aIntegrationId
    "all",            // scope — every run of the agent for an admin
  );
  const totalRuns = runCount?.count ?? 0;
  // limit 1: `/assignments/runs/all` orders by created_at desc, so the head
  // of the list IS the most recent run.
  const { data: latestRuns } = useAllAssignmentRuns(
    1, 0, undefined, undefined, undefined, undefined, agent.id, undefined, "none", true, undefined, "all",
  );
  const lastRun = useMemo(() => {
    const created = latestRuns?.[0]?.created_at;
    return created ? new Date(created) : undefined;
  }, [latestRuns]);

  // Resolve bound resources
  const templateBindings = useMemo(() => getAgentRequiredTemplateProviders(agent).sort(), [
    agent,
  ]);
  const boundIntegrations = useMemo(() => integrations?.filter((i) =>
    agent.integration_bindings && Object.values(agent.integration_bindings).flat().includes(i.id)
  ) || [], [integrations, agent.integration_bindings]);
  const totalIntegrationCount = boundIntegrations.length + templateBindings.length;
  const boundKBs = useMemo(() => kbs?.filter((kb) => agent.knowledge_base_ids?.includes(kb.id)) || [], [kbs, agent.knowledge_base_ids]);
  const boundMCPs = useMemo(() => mcpServers?.filter((s) => agent.mcp_server_ids?.includes(s.id)) || [], [mcpServers, agent.mcp_server_ids]);
  const boundTools = useMemo(() => customTools?.filter((t) => agent.custom_tool_ids?.includes(t.id)) || [], [customTools, agent.custom_tool_ids]);
  const boundSkills = useMemo(() => skills?.filter((s) => agent.skill_ids?.includes(s.id)) || [], [skills, agent.skill_ids]);

  // Resolve the provider to display. Names the *config* rather than the vendor:
  // two configs of one vendor both read "Anthropic", so the vendor alone cannot
  // identify which one the agent saved.
  const modelProvider = useMemo(() => {
    if (!agent.llm_model) return null;
    const byReference = agent.llm_provider_id
      ? llmProviders?.find((p) => p.id === agent.llm_provider_id)
      : undefined;
    if (byReference) return providerConfigName(byReference, llmProviders);
    const kind = (availableModels ?? []).find(
      (m) => m.model === agent.llm_model,
    )?.provider;
    return kind ? providerDisplayName(kind, null, llmProviders) : null;
  }, [agent.llm_model, agent.llm_provider_id, availableModels, llmProviders]);
  // Resolve handover agents
  const handoverAgents = useMemo(() => allAgents?.filter((a) => agent.handover_agent_ids?.includes(a.id)) || [], [allAgents, agent.handover_agent_ids]);

  return (
    <div className="space-y-6">
      {consentElement}
      {/* ── Agent KPIs ── */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
        <StatCard icon={Zap} label="Assignments" value={assignments?.length || 0} subtitle={t_i18n("{count} active", { count: formatCompactNumber(enabledAssignments) })} />
        <StatCard icon={BarChart3} label="Total Runs" value={totalRuns} subtitle="assignments, external tasks and delegations" />
        <StatCard icon={MessageSquare} label="Chat Queries" value={agent.chat_query_count ?? 0} subtitle="total user messages" />
        <StatCard icon={Cog} label="Integrations" value={totalIntegrationCount} subtitle={templateBindings.length > 0 ? t_i18n("{connected} connected · {perUser} per-user", { connected: formatCompactNumber(boundIntegrations.filter((i) => i.status === "connected").length), perUser: formatCompactNumber(templateBindings.length) }) : t_i18n("{count} connected", { count: formatCompactNumber(boundIntegrations.filter((i) => i.status === "connected").length) })} />
        <StatCard icon={Activity} label="Last Activity" value={lastRun ? formatShortDate(lastRun) : "—"} subtitle={lastRun ? formatTime(lastRun) : "No runs yet"} />
      </div>

      {/* ── Coming up: one-off scheduled work items + next recurring triggers ── */}
      <ComingUpCard agent={agent} />

      {/* ── Fleet: home fleet and connecting fleets, read only ── */}
      <AgentFleetCard agent={agent} assignments={assignments} onAddAssignment={canManage ? openAddAssignment : undefined} />

      {/* ── General Assistant Badge ── */}
      {gaConfig?.linked_agent_id === agent.id && canStandInForGeneralAssistant(agent) && (
        <div className="rounded-sm border border-accent/20 bg-accent/5 px-4 py-3 flex items-center gap-2.5">
          <Sparkles className="h-4 w-4 text-accent shrink-0" />
          <div>
            <p className="content-base font-medium">{t_i18n("General Assistant")}</p>
            <p className="content-caption text-default-secondary">{t_i18n("This agent is configured as the platform-wide General Assistant.")}</p>
          </div>
        </div>
      )}

      {/* ── Persona (expandable) ── */}
      {agent.persona && (
        <Card>
          <CardHeader
            className="p-4 pb-0 cursor-pointer select-none"
            onClick={() => setPersonaExpanded(!personaExpanded)}
          >
            <div className="flex items-center justify-between">
              <CardTitle className="content-base flex items-center gap-2">
                <Sparkles className="h-4 w-4 text-accent" /> {t_i18n("Persona")}
              </CardTitle>
              <ChevronDown className={cn("h-4 w-4 text-default-secondary transition-transform", personaExpanded && "rotate-180")} />
            </div>
          </CardHeader>
          <CardContent className="px-4 pb-4 pt-2">
            <div className={cn("overflow-hidden transition-all", personaExpanded ? "max-h-[2000px]" : "max-h-[72px]")}>
              <div className="prose prose-invert prose-xs max-w-none content-compact text-default-secondary leading-relaxed [&_h1]:content-base [&_h1]:font-semibold [&_h1]:text-default-primary [&_h2]:content-compact [&_h2]:font-semibold [&_h2]:text-default-primary [&_h3]:content-compact [&_h3]:font-medium [&_h3]:text-default-primary [&_p]:content-compact [&_p]:text-default-secondary [&_li]:content-compact [&_li]:text-default-secondary [&_strong]:text-default-primary [&_code]:content-caption [&_code]:bg-elevation-hover [&_code]:px-1 [&_code]:py-0.5 [&_code]:rounded-sm">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{agent.persona}</ReactMarkdown>
              </div>
            </div>
            {!personaExpanded && agent.persona.length > 200 && (
              <div className="relative -mt-4 h-4 bg-gradient-to-t from-[var(--bg-elevation-default)] to-transparent pointer-events-none" />
            )}
          </CardContent>
        </Card>
      )}

      {/* ── Configuration Overview ── */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="content-base flex items-center gap-2">
            <Settings2 className="h-4 w-4 text-accent" />
            {t_i18n("Configuration")}
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {/* Model & Parameters */}
          <div className="rounded-sm bg-elevation-hover p-3 space-y-3">
            <div className="content-compact font-medium text-default-secondary">{t_i18n("Model & Parameters")}</div>
            <div className="grid gap-3 sm:grid-cols-3">
              <div className="flex items-center gap-2.5 rounded-sm bg-elevation-hover px-3 py-2">
                <Sparkles className="h-3.5 w-3.5 text-accent shrink-0" />
                <div className="min-w-0">
                  <p className="content-caption text-default-secondary uppercase tracking-wider leading-none">{t_i18n("Model")}</p>
                  <p className="content-compact mt-1 truncate">
                    {agent.llm_model_source === "routing" ? (
                      <span className="text-default-secondary">{routingModel.label}</span>
                    ) : agent.llm_model ? (
                      <>
                        <span className="font-mono-plex text-default-secondary">{agent.llm_model}</span>
                        {modelProvider && (
                          <span className="ml-1 text-default-secondary">({modelProvider})</span>
                        )}
                      </>
                    ) : (
                      <span className="text-default-secondary italic">{t_i18n("Platform default")}</span>
                    )}
                  </p>
                </div>
              </div>
              <div className="flex items-center gap-2.5 rounded-sm bg-elevation-hover px-3 py-2">
                <Thermometer className="h-3.5 w-3.5 text-accent shrink-0" />
                <div className="min-w-0">
                  <p className="content-caption text-default-secondary uppercase tracking-wider leading-none">{t_i18n("Temperature")}</p>
                  <p className="content-compact mt-1">
                    {agent.temperature !== null && agent.temperature !== undefined ? (
                      <span className="font-mono-plex text-default-secondary">{agent.temperature.toFixed(1)}</span>
                    ) : (
                      <span className="text-default-secondary italic">{t_i18n("Provider default")}</span>
                    )}
                  </p>
                </div>
              </div>
              <div className="flex items-center gap-2.5 rounded-sm bg-elevation-hover px-3 py-2">
                <Repeat className="h-3.5 w-3.5 text-accent shrink-0" />
                <div className="min-w-0">
                  <p className="content-caption text-default-secondary uppercase tracking-wider leading-none">{t_i18n("Max Iterations")}</p>
                  <p className="content-compact mt-1">
                    {agent.max_iterations !== null && agent.max_iterations !== undefined ? (
                      <span className="font-mono-plex text-default-secondary">{agent.max_iterations}</span>
                    ) : (
                      <span className="text-default-secondary italic">{t_i18n("Default ({value})", { value: platformMaxIterations })}</span>
                    )}
                  </p>
                </div>
              </div>
            </div>
          </div>

          {/* Feature indicators */}
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
            {[
              { icon: Brain, label: "Memory", active: agent.memory_short_term_enabled || agent.memory_medium_term_enabled || agent.memory_persistent_enabled || agent.memory_user_enabled, detail: (agent.memory_short_term_enabled || agent.memory_medium_term_enabled || agent.memory_persistent_enabled || agent.memory_user_enabled) ? `${[agent.memory_short_term_enabled && t_i18n("Short"), agent.memory_medium_term_enabled && t_i18n("Medium"), agent.memory_persistent_enabled && t_i18n("Persistent"), agent.memory_user_enabled && t_i18n("User")].filter(Boolean).join(" + ")}` : t_i18n("Disabled") },
              { icon: Globe, label: "Web Exposure", active: agent.web_enabled, detail: agent.web_enabled ? t_i18n("Public widget active") : t_i18n("Disabled") },
              { icon: MessageSquare, label: "Chat", active: !agent.disable_chat, detail: agent.disable_chat ? t_i18n("Assignments only") : t_i18n("Available") },
              { icon: ShieldAlert, label: "Guardrails", active: !!agent.guardrails, detail: agent.guardrails ? t_i18n("Configured") : t_i18n("Not set") },
            ].map((f) => (
              <div key={f.label} className="flex items-center gap-2.5 rounded-sm bg-elevation-hover px-3 py-2.5">
                <div className={cn("flex h-7 w-7 shrink-0 items-center justify-center rounded-sm", f.active ? "bg-accent/10" : "bg-elevation-hover")}>
                  <f.icon className={cn("h-3.5 w-3.5", f.active ? "text-accent" : "text-default-secondary")} />
                </div>
                <div className="min-w-0">
                  <p className="content-compact font-medium">{t_i18n(f.label)}</p>
                  <p className={cn("content-caption", f.active ? "text-feedback-success-primary" : "text-default-secondary")}>{f.detail}</p>
                </div>
              </div>
            ))}
          </div>

          {/* Tags & Timestamps */}
          <div className="flex items-center justify-between pt-1">
            {/* Tags */}
            <div className="flex items-center gap-2 min-w-0">
              {agent.tags && agent.tags.length > 0 ? (
                <>
                  <Tag className="h-3 w-3 text-default-secondary shrink-0" />
                  <div className="flex flex-wrap gap-1.5">
                    {agent.tags.map((tag) => (
                      <Badge key={tag} variant="outline" className="content-caption">{tag}</Badge>
                    ))}
                  </div>
                </>
              ) : (
                <div className="flex items-center gap-1.5">
                  <Tag className="h-3 w-3 text-default-disabled" />
                  <span className="content-caption text-default-secondary">{t_i18n("No tags")}</span>
                </div>
              )}
            </div>

            {/* Timestamps */}
            <TimestampGrid createdAt={agent.created_at} updatedAt={agent.updated_at} />
          </div>
        </CardContent>
      </Card>

      {/* ── Assignments (full width) ── */}
      <Card ref={assignmentsCardRef} className="scroll-mt-20">
        <CardHeader className="flex-row items-center justify-between space-y-0 p-4">
          <CardTitle className="content-base flex items-center gap-2"><Zap className="h-4 w-4 text-feedback-warning-primary" /> {t_i18n("Assignments")}</CardTitle>
          <div className="flex items-center gap-2">
            <span className="content-caption text-default-secondary">{t_i18n("{enabled} of {total} active", { enabled: formatCompactNumber(enabledAssignments), total: formatCompactNumber(assignments?.length || 0) })}</span>
            {canManage && (
              <Button size="sm" className="h-7 content-compact gap-1.5" onClick={() => (addingAssignment ? closeAddAssignment() : setAddingAssignment(true))}>
                <Plus className="h-3 w-3" /> {t_i18n("Add Assignment")}
              </Button>
            )}
          </div>
        </CardHeader>
        <CardContent className="pt-0 px-4 pb-4">
          {addingAssignment && <div className="mb-4"><AddAssignmentPanel agent={agent} preset={preset} onClose={closeAddAssignment} /></div>}

          {assignments && assignments.length > 0 ? (
            <div className="space-y-2">
              {[...assignments].sort((a, b) => a.name.localeCompare(b.name)).map((wf) => {
                const tile = triggerTile(wf.trigger_type, wf.trigger_config);
                const TriggerIcon = TRIGGER_ICONS[tile] ?? Zap;
                const cronExpr = wf.trigger_type === "schedule" ? wf.trigger_config.cron : null;
                const isWfRunning = runningAssignmentIds.has(wf.id);
                const status = assignmentDisplayStatus(wf);
                const isActive = status === "active";
                const tint = assignmentStatusTint(status);
                return (
                  <Link
                    key={wf.id}
                    to={`/agents/assignments/${wf.id}`}
                    className={cn(
                      "flex items-center justify-between rounded-sm border px-4 py-3 cursor-pointer transition-all hover:bg-elevation-hover",
                      isWfRunning ? "border-accent/30 bg-accent/[0.03]" : isActive ? "border-elevation-subtle-soft bg-elevation-hover" : "border-elevation-subtle-soft opacity-50",
                    )}
                  >
                    <div className="flex items-center gap-3 min-w-0">
                      <div className={cn("flex h-9 w-9 shrink-0 items-center justify-center rounded-sm", isWfRunning ? "bg-accent/20" : tint.tile)}>
                        <TriggerIcon className={cn("h-4 w-4", isWfRunning ? "text-accent" : tint.icon)} />
                      </div>
                      <div className="min-w-0">
                        <p className="content-base font-medium truncate">{wf.name}</p>
                        <div className="flex items-center gap-2 mt-0.5">
                          <AssignmentRowBadge assignment={wf} running={isWfRunning} ownerPausedRemedy={ownerPausedRemedy} />
                          <span className="content-caption text-default-secondary">{t_i18n(TRIGGER_LABELS[tile] ?? wf.trigger_type)}</span>
                          {cronExpr && <span className="content-caption text-default-secondary">· {describeCron(cronExpr)}</span>}
                          <span className="content-caption text-default-secondary">· {t_i18n("{count, plural, one {{shown} run} other {{shown} runs}}", { count: wf.execution_count, shown: formatCompactNumber(wf.execution_count) })}</span>
                        </div>
                      </div>
                    </div>
                    <div className="flex items-center gap-1.5 shrink-0" onClick={(e) => { e.preventDefault(); e.stopPropagation(); }}>
                      {wf.last_triggered_at && (
                        <span className="text-[9px] text-default-secondary mr-2">{formatDate(wf.last_triggered_at)}</span>
                      )}
                      {canManage && wf.trigger_type !== "integration_event" && wf.trigger_type !== "webhook" && (
                        <AiDisabledTip><Tip content={wf.paused_by_owner_deactivation ? t_i18n("Paused: the owner's account is deactivated") : isWfRunning ? t_i18n("Already running") : t_i18n("Run now")} wrap={!aiOff && (isWfRunning || !!wf.paused_by_owner_deactivation)}><Button aria-label={wf.paused_by_owner_deactivation ? t_i18n("Paused: the owner's account is deactivated") : isWfRunning ? t_i18n("Already running") : t_i18n("Run now")} variant="ghost" size="sm" className="h-7 w-7 p-0" disabled={isWfRunning || aiOff || wf.paused_by_owner_deactivation} onClick={() => triggerWf.mutate({ assignmentId: wf.id }, { onSuccess: () => toast({ title: t_i18n("Triggered"), variant: "success" }) })}><Play className="h-3.5 w-3.5" /></Button></Tip></AiDisabledTip>
                      )}
                      {canManage && (
                        <AiDisabledTip><Tip content={isEmergencyStopped(wf) ? t_i18n("Paused by the emergency stop: lift the stop to resume it.") : wf.enabled ? t_i18n("Pause") : t_i18n("Resume")} wrap={isEmergencyStopped(wf)}><Button variant="ghost" size="sm" className="h-7 w-7 p-0" disabled={isEmergencyStopped(wf) || aiOff} aria-label={wf.enabled ? t_i18n("Pause") : t_i18n("Resume")} onClick={() => void toggleAssignment(wf)}>{wf.enabled ? <Power className="h-3.5 w-3.5" /> : <PowerOff className="h-3.5 w-3.5" />}</Button></Tip></AiDisabledTip>
                      )}
                    </div>
                  </Link>
                );
              })}
            </div>
          ) : !addingAssignment ? (
            <div className="py-6 text-center">
              <Zap className="h-8 w-8 text-default-disabled mx-auto mb-2" />
              <p className="content-base text-default-secondary">{t_i18n("No assignments yet. Add one to automate this agent.")}</p>
            </div>
          ) : null}
        </CardContent>
      </Card>

      {/* ── Resources: 6 cards in 3 columns ── */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {/* Integrations */}
        <Card className="flex flex-col">
          <CardHeader className="flex-row items-center justify-between space-y-0 p-4 pb-3">
            <CardTitle className="content-base flex items-center gap-2"><Cog className="h-4 w-4 text-accent" /> {t_i18n("Integrations")}</CardTitle>
            <CountBadge variant="outline">{totalIntegrationCount}</CountBadge>
          </CardHeader>
          <CardContent className="flex-1 px-4 pb-4 pt-0">
            {totalIntegrationCount > 0 ? (
              <div className="space-y-2">
                {templateBindings.map((provider) => {
                  const logoSrc = providerLogoSrc(provider);
                  const hasScopeConfig = integrations?.some((i) => i.provider === provider && i.scope_config);
                  return (
                    <div key={`tmpl-${provider}`} className="flex items-center gap-3 rounded-sm bg-elevation-hover px-3 py-2.5">
                      <img src={logoSrc} alt={provider} className="h-5 w-5 shrink-0" />
                      <div className="min-w-0 flex-1">
                        <p className="content-base font-medium truncate">{INTEGRATION_PROVIDER_NAMES[provider] ?? provider}</p>
                        <p className="content-caption text-default-secondary">{t_i18n("Resolved per user at runtime")}</p>
                      </div>
                      <span className="flex items-center gap-1 text-[9px] font-medium rounded-sm px-1.5 py-0.5 shrink-0 text-filigran-ia-primary bg-filigran-ia-primary/10">
                        <Users className="h-3 w-3" /> {t_i18n("Per-user")}
                      </span>
                      {hasScopeConfig && (
                        <Tip content={t_i18n("Specifically scoped")}>
                          <Filter className="h-3.5 w-3.5 text-feedback-warning-primary shrink-0" />
                        </Tip>
                      )}
                    </div>
                  );
                })}
                {[...boundIntegrations].sort((a, b) => a.name.localeCompare(b.name)).map((i) => (
                  <OverviewIntegrationRow
                    key={i.id}
                    integration={i}
                    allowedTools={effectiveAllowedTools(agent.integration_allowed_tools, i)}
                    onClick={() => navigate(`/integrations/${i.id}`)}
                  />
                ))}
              </div>
            ) : (
              <div className="py-4 text-center">
                <Cog className="h-6 w-6 text-default-disabled mx-auto mb-1.5" />
                <p className="content-base text-default-secondary">{t_i18n("No integrations bound")}</p>
                <p className="content-caption text-default-secondary mt-0.5">{t_i18n("Configure in the agent settings.")}</p>
              </div>
            )}
          </CardContent>
        </Card>

        {/* MCP Servers */}
        <Card className="flex flex-col">
          <CardHeader className="flex-row items-center justify-between space-y-0 p-4 pb-3">
            <CardTitle className="content-base flex items-center gap-2"><Terminal className="h-4 w-4 text-accent" /> {t_i18n("MCP Servers")}</CardTitle>
            <CountBadge variant="outline">{boundMCPs.length}</CountBadge>
          </CardHeader>
          <CardContent className="flex-1 px-4 pb-4 pt-0">
            {boundMCPs.length > 0 ? (
              <div className="space-y-2">
                {[...boundMCPs].sort((a, b) => a.name.localeCompare(b.name)).map((srv) => (
                  <div key={srv.id} className="flex items-center gap-3 rounded-sm bg-elevation-hover px-3 py-2.5 cursor-pointer hover:bg-elevation-highlight transition-colors" onClick={() => navigate(`/mcp-servers/${srv.id}`)}>
                    <Terminal className="h-5 w-5 text-accent shrink-0" />
                    <div className="min-w-0 flex-1">
                      <p className="content-base font-medium truncate">{srv.name}</p>
                      <p className="content-caption text-default-secondary">{TRANSPORT_DISPLAY_NAMES[srv.transport] ?? srv.transport}</p>
                    </div>
                    {srv.enabled ? <CheckCircle2 className="h-4 w-4 text-feedback-success-primary shrink-0" /> : <XCircle className="h-4 w-4 text-default-secondary shrink-0" />}
                  </div>
                ))}
              </div>
            ) : (
              <div className="py-4 text-center">
                <Terminal className="h-6 w-6 text-default-disabled mx-auto mb-1.5" />
                <p className="content-base text-default-secondary">{t_i18n("No MCP servers bound")}</p>
                <p className="content-caption text-default-secondary mt-0.5">{t_i18n("Attach MCP servers in Configuration > Tooling.")}</p>
              </div>
            )}
          </CardContent>
        </Card>

        {/* Custom Tools */}
        <Card className="flex flex-col">
          <CardHeader className="flex-row items-center justify-between space-y-0 p-4 pb-3">
            <CardTitle className="content-base flex items-center gap-2"><Code2 className="h-4 w-4 text-accent" /> {t_i18n("Custom Tools")}</CardTitle>
            <CountBadge variant="outline">{boundTools.length}</CountBadge>
          </CardHeader>
          <CardContent className="flex-1 px-4 pb-4 pt-0">
            {boundTools.length > 0 ? (
              <div className="space-y-2">
                {[...boundTools].sort((a, b) => a.name.localeCompare(b.name)).map((tool) => (
                  <div key={tool.id} className="flex items-center gap-3 rounded-sm bg-elevation-hover px-3 py-2.5 cursor-pointer hover:bg-elevation-highlight transition-colors" onClick={() => navigate(`/tools/${tool.id}`)}>
                    <Code2 className="h-5 w-5 text-accent shrink-0" />
                    <div className="min-w-0 flex-1">
                      <p className="content-base font-medium truncate">{tool.name}</p>
                      <p className="content-caption text-default-secondary">{tool.enabled ? t_i18n("Enabled") : t_i18n("Disabled")}</p>
                    </div>
                    {tool.enabled ? <CheckCircle2 className="h-4 w-4 text-feedback-success-primary shrink-0" /> : <XCircle className="h-4 w-4 text-default-secondary shrink-0" />}
                  </div>
                ))}
              </div>
            ) : (
              <div className="py-4 text-center">
                <Code2 className="h-6 w-6 text-default-disabled mx-auto mb-1.5" />
                <p className="content-base text-default-secondary">{t_i18n("No custom tools bound")}</p>
                <p className="content-caption text-default-secondary mt-0.5">{t_i18n("Attach custom tools in Configuration > Tooling.")}</p>
              </div>
            )}
          </CardContent>
        </Card>

        {/* Skills */}
        <Card className="flex flex-col">
          <CardHeader className="flex-row items-center justify-between space-y-0 p-4 pb-3">
            <CardTitle className="content-base flex items-center gap-2"><GraduationCap className="h-4 w-4 text-accent" /> {t_i18n("Skills")}</CardTitle>
            <CountBadge variant="outline">{boundSkills.length}</CountBadge>
          </CardHeader>
          <CardContent className="flex-1 px-4 pb-4 pt-0">
            {boundSkills.length > 0 ? (
              <div className="space-y-2">
                {[...boundSkills].sort((a, b) => a.name.localeCompare(b.name)).map((skill) => (
                  <div key={skill.id} className="flex items-center gap-3 rounded-sm bg-elevation-hover px-3 py-2.5 cursor-pointer hover:bg-elevation-highlight transition-colors" onClick={() => navigate(`/skills/${skill.id}`)}>
                    <GraduationCap className="h-5 w-5 text-accent shrink-0" />
                    <div className="min-w-0 flex-1">
                      <p className="content-base font-medium truncate">{skill.name}</p>
                      <p className="content-caption text-default-secondary">{skill.compatibility || t_i18n("Universal")}</p>
                    </div>
                    {skill.enabled ? <CheckCircle2 className="h-4 w-4 text-feedback-success-primary shrink-0" /> : <XCircle className="h-4 w-4 text-default-secondary shrink-0" />}
                  </div>
                ))}
              </div>
            ) : (
              <div className="py-4 text-center">
                <GraduationCap className="h-6 w-6 text-default-disabled mx-auto mb-1.5" />
                <p className="content-base text-default-secondary">{t_i18n("No skills attached")}</p>
                <p className="content-caption text-default-secondary mt-0.5">{t_i18n("Add skills in the Knowledge configuration.")}</p>
              </div>
            )}
          </CardContent>
        </Card>

        {/* Knowledge Bases */}
        <Card className="flex flex-col">
          <CardHeader className="flex-row items-center justify-between space-y-0 p-4 pb-3">
            <CardTitle className="content-base flex items-center gap-2"><BookOpen className="h-4 w-4 text-accent" /> {t_i18n("Knowledge Bases")}</CardTitle>
            <CountBadge variant="outline">{boundKBs.length}</CountBadge>
          </CardHeader>
          <CardContent className="flex-1 px-4 pb-4 pt-0">
            {boundKBs.length > 0 ? (
              <div className="space-y-2">
                {[...boundKBs].sort((a, b) => a.name.localeCompare(b.name)).map((kb) => (
                  <div key={kb.id} className="flex items-center gap-3 rounded-sm bg-elevation-hover px-3 py-2.5 cursor-pointer hover:bg-elevation-highlight transition-colors" onClick={() => navigate(`/knowledge/${kb.id}`)}>
                    <BookOpen className="h-5 w-5 text-accent shrink-0" />
                    <div className="min-w-0 flex-1">
                      <p className="content-base font-medium truncate">{kb.name}</p>
                      <p className="content-caption text-default-secondary">{formatKBStats(kb.document_count, kb.chunk_count)}</p>
                    </div>
                    {agent.knowledge_base_permissions?.[kb.id] === "write" && (
                      <Tip content={t_i18n("The agent can write to this knowledge base (store_knowledge)")}>
                        <Badge variant="secondary" className="text-[8px] gap-1"><Pencil className="h-2.5 w-2.5" /> {t_i18n("Write")}</Badge>
                      </Tip>
                    )}
                    {kb.is_system_managed && <Badge variant="cyan" className="text-[8px]">{t_i18n("System")}</Badge>}
                  </div>
                ))}
              </div>
            ) : (
              <div className="py-4 text-center">
                <BookOpen className="h-6 w-6 text-default-disabled mx-auto mb-1.5" />
                <p className="content-base text-default-secondary">{t_i18n("No knowledge bases")}</p>
                <p className="content-caption text-default-secondary mt-0.5">{t_i18n("Add in configuration or auto-created by assignments.")}</p>
              </div>
            )}
          </CardContent>
        </Card>

        {/* Agent Handover */}
        <Card className="flex flex-col">
          <CardHeader className="flex-row items-center justify-between space-y-0 p-4 pb-3">
            <CardTitle className="content-base flex items-center gap-2"><ArrowLeftRight className="h-4 w-4 text-accent" /> {t_i18n("Agent Handover")}</CardTitle>
            <div className="flex items-center gap-2">
              <Button
                variant="ghost"
                size="sm"
                className="h-6 content-caption gap-1 text-default-secondary hover:text-default-primary"
                onClick={() => navigate(`/flows?focus=${agent.id}`)}
              >
                <ExternalLink className="h-3 w-3" />
                {t_i18n("View in flow")}
              </Button>
              <CountBadge variant="outline">{handoverAgents.length}</CountBadge>
            </div>
          </CardHeader>
          <CardContent className="flex-1 px-4 pb-4 pt-0">
            {handoverAgents.length > 0 ? (
              <div className="space-y-2">
                {[...handoverAgents].sort((a, b) => a.name.localeCompare(b.name)).map((ha) => {
                  const HaIcon = ICON_MAP[ha.icon] ?? Bot;
                  const hCfg = handoverTypeConfig(shownHandoverType(agent.handover_types, ha.id, HANDOVER_TYPES));
                  return (
                    <div key={ha.id} className="flex items-center gap-3 rounded-sm bg-elevation-hover px-3 py-2.5 cursor-pointer hover:bg-elevation-highlight transition-colors" onClick={() => navigate(`/agents/${ha.id}`)}>
                      <HaIcon className="h-5 w-5 text-accent shrink-0" />
                      <div className="min-w-0 flex-1">
                        <p className="content-base font-medium truncate">{ha.name}</p>
                        {ha.slug && <p className="content-caption text-default-secondary font-mono-plex">@{ha.slug}</p>}
                      </div>
                      <Badge variant="outline" className={cn("text-[8px] shrink-0", hCfg.color)}>{t_i18n(hCfg.label)}</Badge>
                    </div>
                  );
                })}
              </div>
            ) : (
              <div className="py-4 text-center">
                <ArrowLeftRight className="h-6 w-6 text-default-disabled mx-auto mb-1.5" />
                <p className="content-base text-default-secondary">{t_i18n("No handover configured")}</p>
                <p className="content-caption text-default-secondary mt-0.5">{t_i18n("Configure in the General settings.")}</p>
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      {/* ── Platform Health improvement: one compact row after the configuration (#101 S10) ── */}
      <HealthImprovementRow agentId={agent.id} canManage={canManage} />
    </div>
  );
}
