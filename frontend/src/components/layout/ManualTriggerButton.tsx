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

import { useState, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { Play, Bot, Zap, Variable } from "lucide-react";
import { useAgents } from "@/hooks/api/useAgentApi";
import { useAssignments, useAllAssignmentRuns, useTriggerAssignment } from "@/hooks/api/useAssignmentApi";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Tip } from "@/components/ui/tooltip";
import { useToast } from "@/components/ui/toast";
import { ManualTriggerDialog } from "@/components/ui/manual-trigger-dialog";
import { useClickOutside } from "@/hooks/useClickOutside";
import { useAuth } from "@/hooks/useAuth";
import { cn, pluralize } from "@/lib/utils";
import { ICON_MAP } from "@/lib/constants";
import { triggerableManualAssignments } from "@/lib/manual-triggers";
import { useFormatter } from "@/i18n";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { AiDisabledTip } from "@/components/ui/ai-disabled";
import type { Assignment } from "@/types";
import { Spinner } from "@filigran/design-system";

export function ManualTriggerButton() {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const { toast } = useToast();
  const [open, setOpen] = useState(false);
  const [paramDialogAssignment, setParamDialogAssignment] = useState<Assignment | null>(null);
  const ref = useRef<HTMLDivElement>(null);

  const { data: assignments } = useAssignments();
  const { data: agents } = useAgents();
  const { data: runningRuns } = useAllAssignmentRuns(50, 0, "running");
  const triggerWf = useTriggerAssignment();
  const { isAdmin } = useAuth();
  /* Every assignment runs an agent, so it always needs AI — unlike a flow,
     where only an AI-bearing graph is held back. The legacy ``wf`` naming here
     is assignments, not flows. */
  const aiOff = useAiActionDisabled();

  // Close on outside click
  useClickOutside(ref, () => setOpen(false));

  const triggers = triggerableManualAssignments(assignments ?? [], agents ?? [], isAdmin);

  // Running assignment IDs set
  const runningAssignmentIds = new Set(
    (runningRuns ?? []).map((r) => r.assignment_id),
  );

  // Count running manual assignments for the badge
  const runningCount = triggers.filter(({ assignment }) => runningAssignmentIds.has(assignment.id)).length;

  const handleTrigger = (wf: Assignment, e: React.MouseEvent) => {
    e.stopPropagation();
    const hasParams = wf.manual_trigger_parameters && wf.manual_trigger_parameters.length > 0;
    if (hasParams) {
      setParamDialogAssignment(wf);
      setOpen(false);
    } else {
      triggerWf.mutate(
        { assignmentId: wf.id },
        { onSuccess: () => toast({ title: t_i18n("Assignment triggered"), variant: "success" }) },
      );
    }
  };

  // Don't render button if no manual assignments
  if (triggers.length === 0) return null;

  return (
    <div className="relative" ref={ref}>
      <Tip content={t_i18n("Quick triggers")}>
        {/* Not an IconButton: a running-count badge sits beside the icon, and
            IconButton renders exactly one. */}
        <Button variant="ghost" size="icon" aria-label={t_i18n("Quick triggers")} className="relative h-8 w-8" onClick={() => setOpen(!open)}>
          <Play className="h-4 w-4" />
          {runningCount > 0 && (
            <span className="absolute -top-0.5 -right-0.5 flex h-4 w-4 items-center justify-center rounded-full bg-accent text-[9px] font-bold text-negative-primary">
              {runningCount}
            </span>
          )}
        </Button>
      </Tip>

      {open && (
        <div className="absolute right-0 top-full mt-2 w-80 rounded-sm layer-1 bg-elevation-default shadow-overlay animate-fade-in">
          {/* Header */}
          <div className="flex items-center justify-between p-3 border-b border-elevation-subtle-soft">
            <p className="content-compact font-semibold flex items-center gap-1.5">
              <Zap className="h-3 w-3 text-feedback-warning-primary" />
              {t_i18n("Manual Triggers")}
            </p>
            <span className="content-caption text-default-secondary">
              {t_i18n(pluralize(triggers.length, "assignment"))}
            </span>
          </div>

          {/* Assignment list */}
          <div className="max-h-80 overflow-y-auto p-1.5">
            {triggers.map(({ assignment: wf, agent }) => {
              const AgentIcon = ICON_MAP[agent.icon] ?? Bot;
              const isRunning = runningAssignmentIds.has(wf.id);

              return (
                <div
                  key={wf.id}
                  className={cn(
                    "flex items-center justify-between rounded-sm px-3 py-2.5 cursor-pointer transition-all hover:bg-elevation-hover",
                    isRunning ? "bg-accent/[0.03]" : "bg-transparent",
                  )}
                  onClick={() => { navigate(`/agents/assignments/${wf.id}`); setOpen(false); }}
                >
                  <div className="flex items-center gap-2.5 min-w-0">
                    <div className={cn(
                      "flex h-8 w-8 shrink-0 items-center justify-center rounded-sm",
                      isRunning ? "bg-accent/20" : "bg-elevation-hover",
                    )}>
                      <AgentIcon className={cn("h-3.5 w-3.5", isRunning ? "text-accent" : "text-default-secondary")} />
                    </div>
                    <div className="min-w-0">
                      <p className="content-compact font-medium truncate">{wf.name}</p>
                      <div className="flex items-center gap-1.5 mt-0.5">
                        {isRunning ? (
                          <Badge variant="cyan" className="text-[7px] gap-0.5 px-1.5 py-0">
                            <Spinner size="sm" tone="inherit" /> {t_i18n("Running")}
                          </Badge>
                        ) : (
                          <span className="content-caption text-default-secondary truncate">
                            {agent.name}
                          </span>
                        )}
                        {isRunning && (
                          <span className="content-caption text-default-secondary truncate">
                            {agent.name}
                          </span>
                        )}
                      </div>
                    </div>
                  </div>
                  <div className="flex items-center shrink-0 ml-2 gap-1" onClick={(e) => e.stopPropagation()}>
                    {wf.manual_trigger_parameters && wf.manual_trigger_parameters.length > 0 && (
                      <Tip content={t_i18n("{count, plural, one {# input parameter} other {# input parameters}}", { count: wf.manual_trigger_parameters.length })}>
                        <Variable className="h-3 w-3 text-filigran-ia-primary" />
                      </Tip>
                    )}
                    <AiDisabledTip>
                    <Tip content={isRunning ? t_i18n("Already running") : t_i18n("Run now")}>
                      <Button
                        variant="ghost"
                        size="sm"
                        className={cn(
                          "h-7 w-7 p-0",
                          !isRunning && "text-accent hover:text-accent hover:bg-accent/10",
                        )}
                        disabled={isRunning || aiOff}
                        onClick={(e) => handleTrigger(wf, e)} aria-busy={isRunning || undefined}>
                        {isRunning && <Spinner size="sm" tone="inherit" />}{!isRunning && <Play className="h-3.5 w-3.5" />}
                      </Button>
                    </Tip>
                    </AiDisabledTip>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {paramDialogAssignment && paramDialogAssignment.manual_trigger_parameters && (
        <ManualTriggerDialog
          open={!!paramDialogAssignment}
          onOpenChange={(v) => { if (!v) setParamDialogAssignment(null); }}
          parameters={paramDialogAssignment.manual_trigger_parameters}
          assignmentName={paramDialogAssignment.name}
          isPending={triggerWf.isPending}
          onTrigger={(payload) => {
            triggerWf.mutate(
              { assignmentId: paramDialogAssignment.id, payload },
              {
                onSuccess: () => {
                  toast({ title: t_i18n("Assignment triggered"), variant: "success" });
                  setParamDialogAssignment(null);
                },
              },
            );
          }}
        />
      )}
    </div>
  );
}
