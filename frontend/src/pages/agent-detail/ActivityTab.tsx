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
import { Icon } from "@filigran/design-system";
import { RunActivityList } from "@/components/ui/run-activity-list";
import { Button } from "@/components/ui/button";
import { HitlIcon } from "@/components/ui/hitl-icon";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { useToast } from "@/components/ui/toast";
import { useBulkDeleteRuns, usePendingApprovalsByTool } from "@/hooks/api/useAssignmentApi";
import { useFormatter } from "@/i18n";
import { ApproveByToolDialog } from "@/pages/ApproveByToolDialog";
import type { Agent } from "@/types";

// ---------------------------------------------------------------------------
// Activity Tab: the agent's runs on the shared runs table (#9378 slice 4a)
// ---------------------------------------------------------------------------
export function ActivityTab({ agent }: { agent: Agent }) {
  const { t_i18n } = useFormatter();
  const bulkDelete = useBulkDeleteRuns();
  const { toast } = useToast();
  const [ConfirmEl, confirm] = useConfirm();
  const [byToolOpen, setByToolOpen] = useState(false);
  /* Scoped to this agent, so the count matches the list under it rather than
     the whole queue — and so the sweep answers only what this page shows. */
  const { data: byToolSummary, isLoading: byToolLoading } =
    usePendingApprovalsByTool(true, agent.id);
  const waitingRuns =
    (byToolSummary?.tools ?? []).reduce((n, t) => n + t.run_count, 0)
    + (byToolSummary?.mixed_run_count ?? 0);

  const handleDeleteAll = async () => {
    if (await confirm({ title: "Delete all runs?", description: t_i18n("This will permanently delete all assignment runs for \"{name}\". This action cannot be undone.", { name: agent.name }), confirmLabel: "Delete All" })) {
      bulkDelete.mutate(
        { all_matching_filters: true, agent_id: agent.id, scope: "all" },
        { onSuccess: (r) => toast({ title: t_i18n("{count, plural, one {# run deleted} other {# runs deleted}}", { count: r.deleted }), variant: "success" }) },
      );
    }
  };

  return (
    <>
      {waitingRuns > 0 && (
        <div className="mb-2 flex items-center gap-3 rounded-sm border border-feedback-alert-primary/30 bg-feedback-alert-primary/[0.05] px-3 py-2">
          <HitlIcon state="waiting" className="h-4 w-4" />
          <p className="min-w-0 flex-1 content-compact text-feedback-warning-primary">
            {t_i18n(
              "{runs, plural, one {# run of this agent is} other {# runs of this agent are}} waiting for approval, using {tools, plural, one {# tool} other {# tools}}.",
              { runs: waitingRuns, tools: byToolSummary?.tools.length ?? 0 },
            )}
          </p>
          <Button
            size="sm"
            variant="outline"
            className="h-6 shrink-0 border-feedback-alert-primary/40 px-2 text-feedback-warning-tertiary hover:bg-feedback-alert-primary/10"
            onClick={() => setByToolOpen(true)}
          >
            {t_i18n("Approve by tool…")}
          </Button>
        </div>
      )}

      <ApproveByToolDialog
        open={byToolOpen}
        onOpenChange={setByToolOpen}
        data={byToolSummary}
        isLoading={byToolLoading}
        agentId={agent.id}
        agentName={agent.name}
      />

      <RunActivityList
        storageKey="agent"
        entityLabel="agent"
        agentId={agent.id}
        reviewFilter
        toolbar={
          <Button
            variant="ghost"
            size="sm"
            className="h-8 content-compact gap-1.5 text-feedback-error-primary hover:text-feedback-error-primary hover:bg-feedback-error-primary/10 shrink-0"
            onClick={handleDeleteAll}
            disabled={bulkDelete.isPending}
            data-testid="run-delete-all"
          >
            <Icon name="trash-2" size={16} /> {t_i18n("Delete All")}
          </Button>
        }
      />
      {ConfirmEl}
    </>
  );
}
