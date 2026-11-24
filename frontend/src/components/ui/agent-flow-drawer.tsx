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

/**
 * AgentFlowDrawer - per-agent "what does this agent do?" slide-over.
 *
 * Opened from a node's info affordance on the Agentic Flow Map tab. Summarizes
 * one agent's assignments (with trigger cadence), bound capabilities
 * (KBs / integrations / MCP / tools / prompts / skills), typed handover targets,
 * and recent runs. Read-only - links out to the agent / run detail pages.
 */

import { useEffect } from "react";
import {
  X,
  Bot,
  Database,
  Plug,
  Server,
  Wrench,
  FileText,
  BookOpen,
  ExternalLink,
  ArrowRight,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { FlowEventRow, type FlowEvent } from "@/components/ui/flow-event-row";
import { useAllAssignmentRuns } from "@/hooks/api/useAssignmentApi";
import {
  ICON_MAP,
  TRIGGER_ICONS,
  TRIGGER_LABELS,
  HANDOVER_TYPES,
  handoverTypeConfig,
} from "@/lib/constants";
import { humanizeCron } from "@/lib/cron";
import { triggerTile } from "@/lib/ctem-signal-trigger";
import { shownHandoverType } from "@/lib/handover-types";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { Agent, Assignment, TriggerConfig } from "@/types";

interface AgentFlowDrawerProps {
  agent: Agent | null;
  assignments: Assignment[];
  activeAgentIds: Set<string>;
  agentNameMap: Map<string, string>;
  open: boolean;
  onClose: () => void;
  onOpenAgent: (agentId: string) => void;
  onOpenRun?: (runId: string) => void;
}

function SectionLabel({ children, count }: { children: React.ReactNode; count?: number }) {
  return (
    <div className="flex items-center gap-2 content-caption font-medium uppercase tracking-wider text-default-secondary">
      {children}
      {count !== undefined && <span className="text-default-secondary">{count}</span>}
    </div>
  );
}

/** The assignment's cadence in the viewer's locale, as schedule-row.tsx reads it. */
function triggerLine(a: Assignment, t_i18n: (s: string) => string, locale: string): string {
  if (a.trigger_type === "schedule") {
    const cfg = a.trigger_config as TriggerConfig;
    return humanizeCron(cfg?.cron, locale) || t_i18n("Scheduled");
  }
  return t_i18n(TRIGGER_LABELS[triggerTile(a.trigger_type, a.trigger_config)] ?? a.trigger_type);
}

function DrawerBody({
  agent,
  assignments,
  activeAgentIds,
  agentNameMap,
  onOpenAgent,
  onOpenRun,
  onClose,
}: {
  agent: Agent;
  assignments: Assignment[];
  activeAgentIds: Set<string>;
  agentNameMap: Map<string, string>;
  onOpenAgent: (agentId: string) => void;
  onOpenRun?: (runId: string) => void;
  onClose: () => void;
}) {
  const { t_i18n, locale } = useFormatter();
  const { data: runs } = useAllAssignmentRuns(
    6,
    0,
    undefined,
    undefined,
    false,
    undefined,
    agent.id,
  );

  const AgentIcon = ICON_MAP[agent.icon] ?? Bot;
  // Active = the agent currently has a run OR is processing chat
  // (see useActiveAgentIds), so this is "active", not strictly "running".
  const isActive = activeAgentIds.has(agent.id);

  const agentAssignments = assignments.filter((a) => a.agent_id === agent.id);

  const capabilities = [
    { icon: Database, label: t_i18n("Knowledge bases"), count: agent.knowledge_base_ids?.length ?? 0 },
    { icon: Plug, label: t_i18n("Integrations"), count: Object.keys(agent.integration_bindings ?? {}).length },
    { icon: Server, label: t_i18n("MCP servers"), count: agent.mcp_server_ids?.length ?? 0 },
    { icon: Wrench, label: t_i18n("Custom tools"), count: agent.custom_tool_ids?.length ?? 0 },
    { icon: FileText, label: t_i18n("Prompts"), count: agent.prompt_ids?.length ?? 0 },
    { icon: BookOpen, label: t_i18n("Skills"), count: agent.skill_ids?.length ?? 0 },
  ].filter((c) => c.count > 0);

  const handovers = (agent.handover_agent_ids ?? []).map((id) => ({
    id,
    name: agentNameMap.get(id) ?? t_i18n("Agent"),
    type: shownHandoverType(agent.handover_types, id, HANDOVER_TYPES),
  }));

  const runEvents: FlowEvent[] = (runs ?? []).map((r) => ({
    id: r.id,
    kind: "run",
    status: r.status,
    outcome: r.outcome,
    title: r.assignment_name ?? t_i18n("Agent activity"),
    timestamp: r.completed_at ?? r.started_at ?? r.created_at,
    href: `/operations/executions/${r.id}`,
  }));

  return (
    <div className="flex h-full flex-col">
      {/* Header: the close button sits in the flex row as the shared ghost
          icon Button (as in NodeConfigPanel), so a long agent name truncates
          before it. */}
      <div className="flex items-start gap-3 border-b border-elevation-subtle p-4">
        <div className="relative shrink-0">
          <div className="flex h-10 w-10 items-center justify-center rounded-sm border border-accent/30 bg-accent/10">
            <AgentIcon className="h-5 w-5 text-accent" />
          </div>
          {isActive && (
            <span className="absolute -right-0.5 -top-0.5 h-2.5 w-2.5 animate-pulse rounded-full bg-accent shadow-[0_0_6px_color-mix(in_srgb,_var(--color-accent)_calc(0.8_*_100%),_transparent)]" />
          )}
        </div>
        <div className="min-w-0 flex-1">
          <h3 className="truncate text-base font-semibold" title={agent.name}>
            {agent.name}
          </h3>
          <div className="mt-1 flex flex-wrap items-center gap-1.5">
            <Badge variant={agent.enabled ? "success" : "outline"} className="h-4 px-1.5 text-[9px]">
              {agent.enabled ? t_i18n("Enabled") : t_i18n("Disabled")}
            </Badge>
            {isActive && (
              <Badge variant="cyan" className="h-4 px-1.5 text-[9px]">
                {t_i18n("Active")}
              </Badge>
            )}
            <span className="text-[11px] text-default-secondary" data-testid="agent-flow-drawer-assignment-count">
              {t_i18n("{count, plural, one {# assignment} other {# assignments}}", {
                count: agentAssignments.length,
              })}
            </span>
          </div>
        </div>
        <Button
          variant="ghost"
          size="icon"
          className="shrink-0"
          onClick={onClose}
          aria-label={t_i18n("Close")}
        >
          <X className="h-4 w-4" />
        </Button>
      </div>

      {/* Body */}
      <div className="min-h-0 flex-1 space-y-5 overflow-y-auto p-4">
        {agent.persona && (
          <p className="line-clamp-3 content-compact leading-relaxed text-default-secondary">
            {agent.persona}
          </p>
        )}

        {/* Assignments */}
        <div className="space-y-2">
          <SectionLabel count={agentAssignments.length}>{t_i18n("Assignments")}</SectionLabel>
          {agentAssignments.length === 0 ? (
            <p className="content-compact text-default-secondary">
              {t_i18n("No assignments configured.")}
            </p>
          ) : (
            <div className="space-y-1.5">
              {agentAssignments.map((a) => {
                const TriggerIcon = TRIGGER_ICONS[triggerTile(a.trigger_type, a.trigger_config)] ?? Bot;
                return (
                  <div
                    key={a.id}
                    className="flex items-center gap-2 rounded-sm border border-elevation-subtle-soft layer-1 bg-elevation-default px-2.5 py-1.5"
                  >
                    <TriggerIcon className="h-3.5 w-3.5 shrink-0 text-accent" />
                    <div className="min-w-0 flex-1">
                      <div className="truncate content-compact font-medium">{a.name}</div>
                      <div className="truncate content-caption text-default-secondary">
                        {triggerLine(a, t_i18n, locale)}
                      </div>
                    </div>
                    {!a.enabled && (
                      <span className="text-[9px] text-default-secondary">{t_i18n("off")}</span>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {/* Handovers */}
        {handovers.length > 0 && (
          <div className="space-y-2">
            <SectionLabel count={handovers.length}>{t_i18n("Hands off to")}</SectionLabel>
            <div className="space-y-1.5">
              {handovers.map((h) => {
                const cfg = handoverTypeConfig(h.type);
                return (
                  <button
                    key={h.id}
                    type="button"
                    onClick={() => onOpenAgent(h.id)}
                    className="flex w-full items-center gap-2 rounded-sm border border-elevation-subtle-soft layer-1 bg-elevation-default px-2.5 py-1.5 text-left transition-colors hover:border-accent/30 hover:bg-elevation-hover"
                  >
                    <span
                      className="h-2 w-2 shrink-0 rounded-full"
                      style={{ background: cfg.edgeColor }}
                    />
                    <ArrowRight className="h-3 w-3 shrink-0 text-default-secondary" />
                    <span className="min-w-0 flex-1 truncate content-compact">{h.name}</span>
                    <span className="shrink-0 content-caption text-default-secondary">
                      {t_i18n(cfg.label)}
                    </span>
                  </button>
                );
              })}
            </div>
          </div>
        )}

        {/* Capabilities */}
        {capabilities.length > 0 && (
          <div className="space-y-2">
            <SectionLabel>{t_i18n("Capabilities")}</SectionLabel>
            <div className="grid grid-cols-2 gap-1.5">
              {capabilities.map((c) => {
                const CapIcon = c.icon;
                return (
                  <div
                    key={c.label}
                    className="flex items-center gap-2 rounded-sm border border-elevation-subtle-soft layer-1 bg-elevation-default px-2.5 py-1.5"
                  >
                    <CapIcon className="h-3.5 w-3.5 shrink-0 text-default-secondary" />
                    <span className="min-w-0 flex-1 truncate text-[11px]">{c.label}</span>
                    <span className="shrink-0 content-compact font-semibold text-accent">{c.count}</span>
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {/* Recent runs */}
        <div className="space-y-2">
          <SectionLabel>{t_i18n("Recent runs")}</SectionLabel>
          {runEvents.length === 0 ? (
            <p className="content-compact text-default-secondary">{t_i18n("No runs yet.")}</p>
          ) : (
            <div className="space-y-1.5">
              {runEvents.map((e) => (
                <FlowEventRow
                  key={e.id}
                  event={e}
                  onOpen={() => onOpenRun?.(e.id)}
                />
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Footer */}
      <div className="border-t border-elevation-subtle p-3">
        <Button
          variant="secondary"
          className="w-full gap-1.5"
          onClick={() => onOpenAgent(agent.id)}
        >
          <ExternalLink className="h-4 w-4" /> {t_i18n("Open agent")}
        </Button>
      </div>
    </div>
  );
}

export function AgentFlowDrawer({
  agent,
  assignments,
  activeAgentIds,
  agentNameMap,
  open,
  onClose,
  onOpenAgent,
  onOpenRun,
}: AgentFlowDrawerProps) {
  const { t_i18n } = useFormatter();
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open || !agent) return null;

  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      {/* Backdrop: clear, so the map stays in view beside the drawer with the
          selected node ringed; a click on it closes the drawer. Never an
          opaque surface: `bg-elevation-default` is the page colour itself and
          blanked the whole map. */}
      <div
        data-testid="agent-flow-drawer-backdrop"
        className="absolute inset-0 bg-transparent"
        onClick={onClose}
      />
      {/* Panel */}
      <div
        role="dialog"
        aria-modal="true"
        aria-label={t_i18n("{name} details", { name: agent.name })}
        className={cn(
          "relative h-full w-[420px] max-w-full border-l border-elevation-subtle layer-1 bg-elevation-default shadow-overlay",
          "animate-in slide-in-from-right-4 duration-200",
        )}
      >
        <DrawerBody
          agent={agent}
          assignments={assignments}
          activeAgentIds={activeAgentIds}
          agentNameMap={agentNameMap}
          onOpenAgent={onOpenAgent}
          onOpenRun={onOpenRun}
          onClose={onClose}
        />
      </div>
    </div>
  );
}
