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
 * DelegationFlowDialog — Shows the delegation flow between agents
 * as a simple visual chain diagram inside a dialog.
 */

import { Fragment } from "react";
import {
  Bot, ArrowRight, MessageSquare, GitFork,
  CheckCircle2, XCircle, Clock, Loader2, Timer, Hourglass,
} from "lucide-react";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { Badge } from "@/components/ui/badge";
import { ICON_MAP } from "@/lib/constants";
import { cn, formatShortDateTime } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import type { AgentDelegation } from "@/types";

// ── Delegation type config ─────────────────────────────────────────
const DELEGATION_CONFIG: Record<string, { icon: React.ElementType; color: string; bg: string; label: string; lineColor: string }> = {
  consult: {
    icon: MessageSquare,
    color: "text-feedback-info-primary",
    bg: "bg-feedback-info-primary/10",
    label: "Consult",
    lineColor: "stroke-feedback-info-primary/40",
  },
  transfer: {
    icon: ArrowRight,
    color: "text-feedback-warning-primary",
    bg: "bg-feedback-alert-primary/10",
    label: "Transfer",
    lineColor: "stroke-feedback-alert-primary/40",
  },
  background_task: {
    icon: GitFork,
    color: "text-filigran-ia-primary",
    bg: "bg-filigran-ia-primary/10",
    label: "Background Task",
    lineColor: "stroke-filigran-ia-primary/40",
  },
};

const STATUS_ICONS: Record<string, { icon: React.ElementType; color: string }> = {
  completed: { icon: CheckCircle2, color: "text-feedback-success-primary" },
  failed: { icon: XCircle, color: "text-feedback-error-primary" },
  incomplete: { icon: Hourglass, color: "text-feedback-warning-primary" },
  running: { icon: Loader2, color: "text-accent" },
  pending: { icon: Clock, color: "text-default-secondary" },
};

interface DelegationFlowDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  delegations: AgentDelegation[];
  agentMap: Map<string, { name: string; icon?: string }>;
  title?: string;
}

export function DelegationFlowDialog({
  open,
  onOpenChange,
  delegations,
  agentMap,
  title = "Agent Delegation Flow",
}: DelegationFlowDialogProps) {
  const { t_i18n } = useFormatter();
  if (!delegations.length) return null;

  // Collect unique agents involved
  const agentIds = new Set<string>();
  for (const d of delegations) {
    agentIds.add(d.parent_agent_id);
    agentIds.add(d.target_agent_id);
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <ArrowRight className="h-4 w-4 text-accent" />
            {t_i18n(title)}
          </DialogTitle>
          <DialogDescription>
            {t_i18n("Visual flow of delegations between agents: {delegations} involving {agents}.", {
              delegations: `${delegations.length} ${t_i18n(delegations.length === 1 ? "delegation" : "delegations")}`,
              agents: `${agentIds.size} ${t_i18n(agentIds.size === 1 ? "agent" : "agents")}`,
            })}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3 mt-2">
          {delegations.map((d, idx) => {
            const cfg = DELEGATION_CONFIG[d.type] || DELEGATION_CONFIG.consult;
            const statusCfg = STATUS_ICONS[d.status] || STATUS_ICONS.pending;
            const TypeIcon = cfg.icon;
            const StatusIcon = statusCfg.icon;

            const parentAgent = agentMap.get(d.parent_agent_id);
            const targetAgent = agentMap.get(d.target_agent_id);
            const parentName = parentAgent?.name ?? d.parent_agent_id.slice(0, 8);
            const targetName = targetAgent?.name ?? d.target_agent_id.slice(0, 8);
            const ParentIcon = ICON_MAP[parentAgent?.icon ?? ""] ?? Bot;
            const TargetIcon = ICON_MAP[targetAgent?.icon ?? ""] ?? Bot;

            return (
              <Fragment key={d.id}>
                <div className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-3">
                  {/* Flow visualization: Parent → Target */}
                  <div className="flex items-center gap-3 mb-2">
                    {/* Step number */}
                    <div className="flex h-6 w-6 items-center justify-center rounded-full bg-accent/10 text-accent content-caption font-bold shrink-0">
                      {idx + 1}
                    </div>

                    {/* Parent agent */}
                    <div className="flex items-center gap-1.5 shrink-0">
                      <div className="flex h-7 w-7 items-center justify-center rounded-sm bg-elevation-hover">
                        <ParentIcon className="h-3.5 w-3.5 text-default-disabled" />
                      </div>
                      <span className="content-compact font-medium">{parentName}</span>
                    </div>

                    {/* Arrow with type */}
                    <div className="flex items-center gap-1.5 px-2">
                      <div className={cn("flex items-center gap-1 rounded-sm px-2 py-0.5", cfg.bg)}>
                        <TypeIcon className={cn("h-2.5 w-2.5", cfg.color)} />
                        <span className={cn("text-[9px] font-medium", cfg.color)}>{t_i18n(cfg.label)}</span>
                      </div>
                      <ArrowRight className="h-3 w-3 text-default-disabled" />
                    </div>

                    {/* Target agent */}
                    <div className="flex items-center gap-1.5 shrink-0">
                      <div className="flex h-7 w-7 items-center justify-center rounded-sm bg-elevation-hover">
                        <TargetIcon className="h-3.5 w-3.5 text-default-disabled" />
                      </div>
                      <span className="content-compact font-medium">{targetName}</span>
                    </div>

                    {/* Status */}
                    <Badge
                      variant={d.status === "completed" ? "success" : d.status === "failed" ? "destructive" : "outline"}
                      className="text-[8px] shrink-0 ml-auto gap-0.5"
                    >
                      <StatusIcon className={cn("h-2 w-2", d.status === "running" && "animate-spin", statusCfg.color)} />
                      {d.status}
                    </Badge>
                  </div>

                  {/* Description + metadata */}
                  <div className="ml-9 space-y-1">
                    <p className="content-compact text-default-secondary truncate">{d.description}</p>
                    <div className="flex items-center gap-3 content-caption text-default-secondary">
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
                    </div>
                  </div>
                </div>

                {/* Connector between steps */}
                {idx < delegations.length - 1 && (
                  <div className="flex justify-center">
                    <div className="w-px h-3 bg-elevation-hover" />
                  </div>
                )}
              </Fragment>
            );
          })}
        </div>
      </DialogContent>
    </Dialog>
  );
}
