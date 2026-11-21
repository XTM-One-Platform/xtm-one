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
  Clock, CheckCircle2, CircleDot,
  AlertTriangle, FileText, GitFork, ArrowRightLeft,
  MessageSquare, ArrowRight, Timer, Workflow,
  ExternalLink, ChevronDown, ChevronRight, Bot, RotateCcw,
} from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { StatCard } from "@/components/ui/stat-card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Tip } from "@/components/ui/tooltip";
import { useAgentDelegations } from "@/hooks/api/useAgentApi";
import { useAgents } from "@/hooks/api/useAgentApi";
import { cn, formatShortDateTime, stripMarkdown } from "@/lib/utils";
import { DelegationFlowDialog } from "@/components/ui/delegation-flow-dialog";
import { DisclosureSummary } from "@/components/ui/disclosure";
import { AnnotationButtons } from "@/components/ui/annotation-buttons";
import { useAuth } from "@/hooks/useAuth";
import { ICON_MAP, runStatusConfig } from "@/lib/constants";
import { useFormatter } from "@/i18n";
import type { Agent, AgentDelegation } from "@/types";

// ---------------------------------------------------------------------------
// Delegation type config
// ---------------------------------------------------------------------------
const DELEGATION_TYPE_CONFIG: Record<string, { icon: React.ElementType; color: string; bg: string; label: string; description: string }> = {
  consult: {
    icon: MessageSquare,
    color: "text-feedback-info-primary",
    bg: "bg-feedback-info-primary/10",
    label: "Consult",
    description: "Asked a peer agent a question",
  },
  transfer: {
    icon: ArrowRight,
    color: "text-feedback-warning-primary",
    bg: "bg-feedback-alert-primary/10",
    label: "Transfer",
    description: "Transferred conversation to peer agent",
  },
  background_task: {
    icon: GitFork,
    color: "text-filigran-ia-primary",
    bg: "bg-filigran-ia-primary/10",
    label: "Background Task",
    description: "Delegated async work to peer agent",
  },
};

// Max characters for description preview in collapsed state
const DESC_PREVIEW_LIMIT = 150;

// ---------------------------------------------------------------------------
// Delegations Tab — unified inter-agent interaction audit trail
// ---------------------------------------------------------------------------
export function DelegationsTab({ agent }: { agent: Agent }) {
  const { t_i18n } = useFormatter();
  const { data: delegations, isLoading } = useAgentDelegations({
    agent_id: agent.id,
    limit: 100,
  });
  const { data: agents } = useAgents({ scope: "all" });
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [typeFilter, setTypeFilter] = useState<string | null>(null);
  const [flowOpen, setFlowOpen] = useState(false);

  if (isLoading) return <p className="content-caption text-default-secondary py-4">{t_i18n("Loading delegations...")}</p>;

  const hasHandover = agent.handover_agent_ids && agent.handover_agent_ids.length > 0;

  // Build agent name + icon lookup
  const agentMap = new Map<string, string>();
  const agentIconMap = new Map<string, string | undefined>();
  const agentFlowMap = new Map<string, { name: string; icon?: string }>();
  if (agents) {
    for (const a of agents) {
      agentMap.set(a.id, a.name);
      agentIconMap.set(a.id, a.icon ?? undefined);
      agentFlowMap.set(a.id, { name: a.name, icon: a.icon ?? undefined });
    }
  }

  // Filter by type
  const filtered = typeFilter
    ? delegations?.filter((d) => d.type === typeFilter)
    : delegations;

  // Stats
  const total = delegations?.length ?? 0;
  const consultCount = delegations?.filter((d) => d.type === "consult").length ?? 0;
  const transferCount = delegations?.filter((d) => d.type === "transfer").length ?? 0;
  const bgTaskCount = delegations?.filter((d) => d.type === "background_task").length ?? 0;

  return (
    <div className="space-y-4">
      {/* Stats + Flow button */}
      {total > 0 && (
        <>
          <div className="flex items-center justify-between">
            <div className="grid gap-3 sm:grid-cols-4 flex-1">
              <StatCard icon={ArrowRightLeft} label="Total" value={total} subtitle="All delegations" />
              <StatCard icon={MessageSquare} label="Consultations" value={consultCount} subtitle="Ask & return" />
              <StatCard icon={ArrowRight} label="Transfers" value={transferCount} subtitle="Permanent handoff" />
              <StatCard icon={GitFork} label="Background" value={bgTaskCount} subtitle="Async tasks" />
            </div>
          </div>
          <div className="flex justify-end">
            <Button
              variant="outline"
              size="sm"
              onClick={() => setFlowOpen(true)}
              className="gap-1.5 content-compact"
            >
              <Workflow className="h-3 w-3" />
              {t_i18n("View Agent Flow")}
            </Button>
          </div>
          <DelegationFlowDialog
            open={flowOpen}
            onOpenChange={setFlowOpen}
            delegations={filtered ?? []}
            agentMap={agentFlowMap}
            title={t_i18n("Agent Flow: {name}", { name: agent.name })}
          />
        </>
      )}

      {/* Type filter */}
      {total > 0 && (
        <div className="flex items-center gap-2">
          <span className="content-caption text-default-secondary uppercase tracking-wider">{t_i18n("Filter:")}</span>
          <button
            onClick={() => setTypeFilter(null)}
            className={cn(
              "px-2 py-0.5 rounded-sm content-caption transition-colors",
              typeFilter === null ? "bg-accent/20 text-accent" : "bg-elevation-hover text-default-secondary hover:text-default-primary",
            )}
          >
            {t_i18n("All")}
          </button>
          {(["consult", "transfer", "background_task"] as const).map((t) => {
            const cfg = DELEGATION_TYPE_CONFIG[t];
            const count = delegations?.filter((d) => d.type === t).length ?? 0;
            if (count === 0) return null;
            return (
              <button
                key={t}
                onClick={() => setTypeFilter(typeFilter === t ? null : t)}
                className={cn(
                  "px-2 py-0.5 rounded-sm content-caption transition-colors flex items-center gap-1",
                  typeFilter === t ? `${cfg.bg} ${cfg.color}` : "bg-elevation-hover text-default-secondary hover:text-default-primary",
                )}
              >
                {t_i18n(cfg.label)} ({count})
              </button>
            );
          })}
        </div>
      )}

      {/* Delegation list */}
      {filtered && filtered.length > 0 ? (
        <div className="space-y-2">
          {filtered.map((d) => (
            <DelegationCard
              key={d.id}
              delegation={d}
              agentMap={agentMap}
              agentIconMap={agentIconMap}
              currentAgentId={agent.id}
              expanded={expandedId === d.id}
              onToggle={() => setExpandedId(expandedId === d.id ? null : d.id)}
            />
          ))}
        </div>
      ) : (
        <Card className="border-dashed">
          <CardContent className="flex flex-col items-center justify-center py-12 text-center">
            <ArrowRightLeft className="h-10 w-10 text-default-disabled mb-3" />
            <p className="content-base font-medium mb-1">
              {typeFilter ? t_i18n("No delegations of this type") : t_i18n("No delegations yet")}
            </p>
            <p className="content-compact text-default-secondary max-w-md">
              {hasHandover
                ? t_i18n("When this agent consults, transfers to, or delegates background work to peer agents, all interactions will appear here.")
                : t_i18n("Configure handover agents in the Configuration tab to enable agent delegation.")
              }
            </p>
          </CardContent>
        </Card>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Agent pill — small inline agent identifier with icon
// ---------------------------------------------------------------------------
function AgentPill({ name, icon, className }: { name: string; icon?: string; className?: string }) {
  const IconComp = icon ? (ICON_MAP[icon] ?? Bot) : Bot;
  return (
    <span className={cn("inline-flex items-center gap-1 px-1.5 py-0.5 rounded-sm bg-elevation-hover content-caption font-medium", className)}>
      <IconComp className="h-2.5 w-2.5 shrink-0" />
      <span className="truncate max-w-[120px]">{name}</span>
    </span>
  );
}

// ---------------------------------------------------------------------------
// Individual delegation card
// ---------------------------------------------------------------------------
function DelegationCard({
  delegation: d,
  agentMap,
  agentIconMap,
  currentAgentId,
  expanded,
  onToggle,
}: {
  delegation: AgentDelegation;
  agentMap: Map<string, string>;
  agentIconMap: Map<string, string | undefined>;
  currentAgentId: string;
  expanded: boolean;
  onToggle: () => void;
}) {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const { user } = useAuth();
  const typeCfg = DELEGATION_TYPE_CONFIG[d.type] || DELEGATION_TYPE_CONFIG.consult;
  // One status config for a run on every surface: an interrupted delegation reads "Interrupted".
  const statusCfg = runStatusConfig(d);
  const interrupted = d.outcome === "interrupted";
  const TypeIcon = typeCfg.icon;
  const StatusIcon = statusCfg.icon;

  // Determine direction relative to current agent
  const isOutgoing = d.parent_agent_id === currentAgentId;
  const sourceName = agentMap.get(d.parent_agent_id) ?? d.parent_agent_id.slice(0, 8);
  const targetName = agentMap.get(d.target_agent_id) ?? d.target_agent_id.slice(0, 8);
  const sourceIcon = agentIconMap.get(d.parent_agent_id);
  const targetIcon = agentIconMap.get(d.target_agent_id);

  // Extract assignment_run_id from metadata for "View Run" link
  const assignmentRunId = d.metadata?.assignment_run_id as string | undefined;

  // Truncate description for preview
  const descPreview = d.description.length > DESC_PREVIEW_LIMIT
    ? d.description.slice(0, DESC_PREVIEW_LIMIT) + "..."
    : d.description;

  const ExpandIcon = expanded ? ChevronDown : ChevronRight;

  return (
    <Card
      className={cn(
        "hover:bg-elevation-hover transition-all overflow-hidden",
        d.status === "failed" && !interrupted && "border-feedback-error-primary/10",
        d.status === "running" && "border-accent/10",
      )}
    >
      <CardContent className="p-0">
        {/* Header — always visible, clickable to expand */}
        <button
          type="button"
          className="w-full text-left p-3 cursor-pointer"
          onClick={onToggle}
        >
          <div className="flex items-start gap-3">
            {/* Type icon */}
            <div className={cn("mt-0.5 rounded-sm p-1.5 shrink-0", typeCfg.bg)}>
              <TypeIcon className={cn("h-3.5 w-3.5", typeCfg.color)} />
            </div>

            <div className="min-w-0 flex-1">
              {/* Row 1: Source → Target + badges */}
              <div className="flex items-center gap-2 mb-1 flex-wrap">
                <AgentPill
                  name={sourceName}
                  icon={sourceIcon}
                  className={isOutgoing ? "ring-1 ring-accent/20" : undefined}
                />
                <ArrowRight className={cn("h-3 w-3 shrink-0", typeCfg.color)} />
                <AgentPill
                  name={targetName}
                  icon={targetIcon}
                  className={!isOutgoing ? "ring-1 ring-accent/20" : undefined}
                />
                <Badge
                  variant="outline"
                  className={cn("text-[8px] shrink-0", typeCfg.color)}
                >
                  {t_i18n(typeCfg.label)}
                </Badge>
                <Badge
                  variant={statusCfg.badge}
                  className="text-[8px] shrink-0"
                >
                  <StatusIcon className={cn("h-2 w-2 mr-0.5", d.status === "running" && "animate-spin")} />
                  {t_i18n(statusCfg.label)}
                </Badge>
              </div>

              {/* Row 2: Truncated description */}
              <p className="content-compact text-default-secondary leading-relaxed line-clamp-2">
                {stripMarkdown(descPreview)}
              </p>

              {/* Row 3: Metadata */}
              <div className="flex items-center gap-3 content-caption text-default-secondary mt-1">
                {d.created_at && (
                  <span className="flex items-center gap-1">
                    <Clock className="h-2.5 w-2.5" />
                    {formatShortDateTime(new Date(d.created_at))}
                  </span>
                )}
                {d.duration_ms != null && (
                  <span className="flex items-center gap-1">
                    <Timer className="h-2.5 w-2.5" />
                    {d.duration_ms < 1000 ? `${d.duration_ms}ms` : `${(d.duration_ms / 1000).toFixed(1)}s`}
                  </span>
                )}
                <span className="flex items-center gap-1">
                  <CircleDot className="h-2.5 w-2.5" />
                  {d.id.slice(0, 8)}
                </span>
                <ExpandIcon className="h-3 w-3 ml-auto shrink-0" />
              </div>
            </div>
          </div>
        </button>

        {/* Expanded content */}
        {expanded && (
          <div className="px-3 pb-3 space-y-3">
            <div className="border-t border-elevation-subtle-soft" />

            {/* Full description (if longer than preview) */}
            {d.description.length > DESC_PREVIEW_LIMIT && (
              <div>
                <div className="flex items-center gap-1.5 mb-1.5">
                  <FileText className="h-3 w-3 text-default-secondary" />
                  <p className="content-caption font-medium text-default-secondary uppercase tracking-wider">{t_i18n("Task Description")}</p>
                </div>
                <div className="rounded-sm bg-elevation-hover p-3 content-compact text-default-secondary leading-relaxed max-h-40 overflow-y-auto whitespace-pre-wrap">
                  {d.description}
                </div>
              </div>
            )}

            {/* Result */}
            {d.has_result && d.result && (
              <div>
                <div className="flex items-center gap-1.5 mb-1.5">
                  <CheckCircle2 className="h-3 w-3 text-feedback-success-primary/60" />
                  <p className="content-caption font-medium text-default-secondary uppercase tracking-wider">{t_i18n("Result")}</p>
                  {/* Only the caller's own consultation is rated: an admin also
                      sees colleagues' delegations here, whose answers
                      were not theirs. A background task's result is rated as
                      the task (or its run), a transfer's answer in the chat. */}
                  {d.type === "consult" && d.user_id === user?.id && (
                    <AnnotationButtons
                      targetType="agent_delegation"
                      targetId={d.id}
                      label="Was this answer useful?"
                      className="ml-auto"
                    />
                  )}
                </div>
                <div className="rounded-sm bg-elevation-hover p-3 content-compact text-default-secondary leading-relaxed max-h-60 overflow-y-auto whitespace-pre-wrap">
                  {d.result}
                </div>
              </div>
            )}

            {/* Error */}
            {/* An interrupted delegation: the platform ended it, so its text reads neutral, as its badge does. */}
            {d.status === "failed" && d.error && interrupted && (
              <div>
                <div className="flex items-center gap-1.5 mb-1.5">
                  <RotateCcw className="h-3 w-3 text-default-secondary" />
                  <p className="content-caption font-medium text-default-secondary uppercase tracking-wider">{t_i18n("Interrupted")}</p>
                </div>
                <p className="content-compact text-default-secondary mb-1.5">{t_i18n("It was interrupted: the platform restarted or stopped the process running it.")}</p>
                {/* The platform's own words, in English: behind a disclosure, under the translated line. */}
                <details data-testid="delegation-interrupted-details">
                  <DisclosureSummary>{t_i18n("Details")}</DisclosureSummary>
                  <div className="mt-1.5 rounded-sm bg-elevation-hover p-3 content-compact text-default-secondary leading-relaxed max-h-40 overflow-y-auto">
                    {d.error}
                  </div>
                </details>
              </div>
            )}
            {d.status === "failed" && d.error && !interrupted && (
              <div>
                <div className="flex items-center gap-1.5 mb-1.5">
                  <AlertTriangle className="h-3 w-3 text-feedback-error-primary/60" />
                  <p className="content-caption font-medium text-feedback-error-primary/80 uppercase tracking-wider">{t_i18n("Error")}</p>
                </div>
                <div className="rounded-sm bg-feedback-error-primary/[0.03] p-3 content-compact text-feedback-error-primary/80 leading-relaxed max-h-40 overflow-y-auto">
                  {d.error}
                </div>
              </div>
            )}

            {/* View Run button — only for background_task delegations with a linked run */}
            {assignmentRunId && (
              <Tip content={t_i18n("Open the full run detail with tool traces and reasoning")}>
                <Button
                  variant="outline"
                  size="sm"
                  className="gap-1.5 content-compact"
                  onClick={(e) => {
                    e.stopPropagation();
                    navigate(`/operations/executions/${assignmentRunId}`);
                  }}
                >
                  <ExternalLink className="h-3 w-3" />
                  {t_i18n("View Run Details")}
                </Button>
              </Tip>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
