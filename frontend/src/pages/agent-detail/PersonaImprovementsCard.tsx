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
import { Sparkles } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { useToast } from "@/components/ui/toast";
import { useEvaluations } from "@/hooks/api/useEvaluationApi";
import {
  useApprovePersonaImprovement,
  usePersonaImprovements,
  useProposePersonaImprovement,
  useRejectPersonaImprovement,
} from "@/hooks/api/usePersonaImprovementApi";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { IMPROVEMENT_PROPOSAL_STATUS_CONFIG } from "@/lib/constants";
import { isPersonaRewrite } from "@/lib/persona-improvements";
import { cn, formatDateTime } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import type { Agent, ImprovementProposal } from "@/types";
import { Spinner } from "@filigran/design-system";

function score(value: number | null): string {
  return value === null ? "-" : value.toFixed(1);
}

function Changes({ item }: { item: ImprovementProposal }) {
  return (
    <div className="max-h-64 overflow-auto rounded-sm border border-elevation-subtle-soft bg-elevation-default font-mono-plex text-[11px] leading-relaxed">
      {item.diff.map((line, index) => (
        <div
          key={index}
          className={cn(
            "whitespace-pre-wrap px-2",
            line.type === "add" && "bg-feedback-success-primary/10 text-feedback-success-primary",
            line.type === "remove" && "bg-feedback-error-primary/10 text-feedback-error-primary",
            line.type === "context" && "text-default-secondary",
          )}
        >
          {line.type === "add" ? "+ " : line.type === "remove" ? "- " : "  "}
          {line.content}
        </div>
      ))}
    </div>
  );
}

function ProposeDialog({ agent, onClose }: { agent: Agent; onClose: () => void }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const aiOff = useAiActionDisabled();
  const { data: evaluations = [], isLoading } = useEvaluations(agent.id, { scope: "all" });
  const propose = useProposePersonaImprovement(agent.id);
  const [evaluationId, setEvaluationId] = useState("");

  const submit = () =>
    propose.mutate(evaluationId, {
      onSuccess: () => {
        toast({ title: t_i18n("Rewrite ready for approval"), variant: "success" });
        onClose();
      },
      onError: (error) =>
        toast({ title: t_i18n("Could not ask for a rewrite"), description: error.message, variant: "error" }),
    });

  return (
    <>
      <div className="space-y-4">
        {!isLoading && evaluations.length === 0 ? (
          <p className="content-base text-default-secondary">{t_i18n("No evaluation includes this agent yet.")}</p>
        ) : (
          <SearchableSelect
            aria-label={t_i18n("Evaluation")}
            options={evaluations.map((evaluation) => ({ value: evaluation.id, label: evaluation.name }))}
            value={evaluationId}
            onValueChange={setEvaluationId}
            placeholder={t_i18n("Select an evaluation")}
          />
        )}
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>
            {t_i18n("Cancel")}
          </Button>
          <Button
            onClick={submit}
            disabled={!evaluationId || propose.isPending || aiOff}
            className="bg-accent text-negative-primary hover:bg-accent/90" aria-busy={propose.isPending || undefined}>
            {propose.isPending && <Spinner size="sm" tone="inherit" />}
            {propose.isPending ? t_i18n("Writing the rewrite...") : t_i18n("Ask for a rewrite")}
          </Button>
        </div>
      </div>
    </>
  );
}

function ImprovementRow({ item, agentId }: { item: ImprovementProposal; agentId: string }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const aiOff = useAiActionDisabled();
  const approve = useApprovePersonaImprovement(agentId);
  const reject = useRejectPersonaImprovement(agentId);
  const pending = item.status === "pending_approval";
  const [showChanges, setShowChanges] = useState(pending);
  const status = IMPROVEMENT_PROPOSAL_STATUS_CONFIG[item.status];
  const deciding = approve.isPending || reject.isPending;

  const onApprove = () => {
    approve.mutate(item.id, {
      onSuccess: () => toast({ title: t_i18n("Rewrite applied"), variant: "success" }),
      onError: (error) =>
        toast({ title: t_i18n("Could not apply the rewrite"), description: error.message, variant: "error" }),
    });
  };

  const onReject = () => {
    reject.mutate(
      { improvementId: item.id },
      {
        onSuccess: () => toast({ title: t_i18n("Rewrite rejected"), variant: "success" }),
        onError: (error) =>
          toast({ title: t_i18n("Could not reject the rewrite"), description: error.message, variant: "error" }),
      },
    );
  };

  return (
    <div className="space-y-2 border-t border-elevation-subtle-soft px-4 py-3 first:border-t-0" data-testid="persona-improvement">
      <div className="flex flex-wrap items-center gap-2 content-compact">
        <Badge variant={status.badge}>{t_i18n(status.label)}</Badge>
        {item.evaluation_name && <span className="font-medium">{item.evaluation_name}</span>}
        <span className="text-default-secondary">{formatDateTime(item.created_at)}</span>
        {item.requested_by && (
          <span className="text-default-secondary">{t_i18n("Asked by {name}", { name: item.requested_by })}</span>
        )}
        {item.decided_by && (
          <span className="text-default-secondary">{t_i18n("Decided by {name}", { name: item.decided_by })}</span>
        )}
      </div>
      {item.summary && <p className="content-base">{item.summary}</p>}
      {item.baseline_score !== null && (
        <p className="content-compact text-default-secondary">
          {item.candidate_score === null
            ? t_i18n("Quality before the rewrite: {baseline}", { baseline: score(item.baseline_score) })
            : t_i18n("Quality {baseline} then {candidate} ({delta} points)", {
                baseline: score(item.baseline_score),
                candidate: score(item.candidate_score),
                delta: item.delta === null ? "-" : `${item.delta > 0 ? "+" : ""}${item.delta.toFixed(2)}`,
              })}
        </p>
      )}
      {item.outcome_note && <p className="content-compact text-default-secondary">{item.outcome_note}</p>}
      <button
        type="button"
        onClick={() => setShowChanges((shown) => !shown)}
        className="content-compact text-accent hover:underline"
      >
        {showChanges
          ? t_i18n("Hide the changes")
          : t_i18n("Show the changes (+{added} -{removed})", { added: item.added, removed: item.removed })}
      </button>
      {showChanges && <Changes item={item} />}
      {pending &&
        (item.can_decide ? (
          <div className="flex justify-end gap-2">
            <Button variant="ghost" size="sm" onClick={onReject} disabled={deciding || aiOff} aria-busy={reject.isPending || undefined}>
              {reject.isPending && <Spinner size="sm" tone="inherit" />}
              {t_i18n("Reject")}
            </Button>
            <Button
              size="sm"
              onClick={onApprove}
              disabled={deciding || aiOff}
              className="bg-accent text-negative-primary hover:bg-accent/90" aria-busy={approve.isPending || undefined}>
              {approve.isPending && <Spinner size="sm" tone="inherit" />}
              {t_i18n("Approve")}
            </Button>
          </div>
        ) : (
          <p className="content-compact text-default-secondary">
            {t_i18n("Waiting for the approval of someone who may manage this agent.")}
          </p>
        ))}
    </div>
  );
}

/**
 * The persona self-improvement loop of one agent: its rewrite cycles,
 * the approval of a pending one, and - for administrators - a new request.
 */
export function PersonaImprovementsCard({ agent }: { agent: Agent }) {
  const { t_i18n } = useFormatter();
  const aiOff = useAiActionDisabled();
  const { data } = usePersonaImprovements(agent.id);
  const [proposeOpen, setProposeOpen] = useState(false);
  const items = (data?.items ?? []).filter(isPersonaRewrite);
  // A rewrite is measured by an evaluation: the backend's can_propose already
  // says whether evaluation is on (every mode where AI runs) and the caller may write.
  const canPropose = !!data?.can_propose;

  if (!data || (items.length === 0 && !canPropose)) return null;

  return (
    <Card className="glass-card border-elevation-subtle-soft" data-testid="persona-improvements-card">
      <CardHeader>
        <div className="flex items-start justify-between gap-4">
          <div>
            <CardTitle className="flex items-center gap-2 content-base">
              <Sparkles className="h-4 w-4 text-accent" />
              {t_i18n("Persona self-improvement")}
            </CardTitle>
            <CardDescription className="mt-1">
              {t_i18n(
                "A rewrite of the persona, measured against one of the agent's evaluations. Someone who may manage the agent approves it; the evaluation then runs again, and a quality drop of more than 2 points rolls the persona back.",
              )}
            </CardDescription>
          </div>
          {canPropose && (
            <Button size="sm" onClick={() => setProposeOpen(true)} disabled={aiOff} className="shrink-0">
              {t_i18n("Ask for a rewrite")}
            </Button>
          )}
        </div>
      </CardHeader>
      {items.length > 0 && (
        <CardContent className="p-0">
          {items.map((item) => (
            <ImprovementRow key={item.id} item={item} agentId={agent.id} />
          ))}
        </CardContent>
      )}
      <Dialog open={proposeOpen} onOpenChange={setProposeOpen}>
        <DialogContent className="max-w-md">
          {/* Written here rather than in ProposeDialog: the library finds the
              title by walking DialogContent's own JSX. */}
          <DialogHeader>
            <DialogTitle>{t_i18n("Ask for a rewrite")}</DialogTitle>
            <DialogDescription>
              {t_i18n(
                "Choose the evaluation that measures the rewrite. The agent's latest completed run of it is the baseline, so it must have measured the current persona.",
              )}
            </DialogDescription>
          </DialogHeader>
          <ProposeDialog agent={agent} onClose={() => setProposeOpen(false)} />
        </DialogContent>
      </Dialog>
    </Card>
  );
}
