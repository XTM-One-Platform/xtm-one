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
 * One improvement proposal opened from the inbox (#8261): the change as a
 * diff, the measured comparison, the simulated writes, its evidence, and
 * Approve / Reject for who may decide. The proposal, the decision and its rule
 * are level 3's (`/improvement-proposals/{id}`, `approval_error`): this view
 * only shows what the server serves and offers what it allows.
 */
import { Check, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/toast";
import { useDecideImprovementProposal, useImprovementProposal } from "@/hooks/api/useHealthApi";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { ImprovementProposal } from "@/types";
import { ImprovementEvidence } from "./ImprovementEvidence";
import { Spinner } from "@filigran/design-system";

function Diff({ proposal }: { proposal: ImprovementProposal }) {
  const { t_i18n } = useFormatter();
  if (!proposal.diff.length) {
    return <p className="content-caption text-default-secondary">{t_i18n("This change has no text diff.")}</p>;
  }
  return (
    <div className="max-h-64 overflow-auto rounded-sm border border-elevation-subtle-soft bg-elevation-default font-mono-plex text-[11px] leading-relaxed">
      {proposal.diff.map((line, index) => (
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

function score(value: number | null) {
  return value === null ? "-" : value.toFixed(1);
}

/** The experiment's own figures, as filed: every top-level number or text, nothing inferred. */
function Measured({ proposal }: { proposal: ImprovementProposal }) {
  const { t_i18n } = useFormatter();
  const figures = Object.entries(proposal.experiment ?? {}).filter(
    ([, value]) => typeof value === "number" || typeof value === "string",
  );
  const watched = proposal.baseline_score !== null || proposal.candidate_score !== null;
  if (!figures.length && !watched) return null;
  return (
    <div className="space-y-1">
      <p className="content-caption text-default-secondary">{t_i18n("Measured")}</p>
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-0.5 content-compact">
        {figures.map(([key, value]) => (
          <div key={key} className="contents">
            <dt className="font-mono-plex text-default-secondary">{key}</dt>
            <dd className="font-mono-plex">{String(value)}</dd>
          </div>
        ))}
        {watched && (
          <div className="contents">
            <dt className="text-default-secondary">{t_i18n("Re-evaluation")}</dt>
            <dd className="font-mono-plex">
              {score(proposal.baseline_score)} → {score(proposal.candidate_score)}
              {proposal.delta !== null && ` (${proposal.delta > 0 ? "+" : ""}${proposal.delta.toFixed(1)})`}
            </dd>
          </div>
        )}
      </dl>
      {proposal.outcome_note && <p className="content-caption text-default-secondary">{proposal.outcome_note}</p>}
    </div>
  );
}

export function ImprovementProposalView({ proposalId }: { proposalId: string }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const { data: proposal, isLoading, isError } = useImprovementProposal(proposalId);
  const decide = useDecideImprovementProposal();
  if (isLoading) return <Spinner size="sm" tone="inherit" className="text-default-secondary" />;
  if (isError || !proposal) {
    return <p className="content-compact text-feedback-error-primary">{t_i18n("This proposal could not be loaded.")}</p>;
  }
  const act = (decision: "approve" | "reject") =>
    decide.mutate(
      { id: proposal.id, decision },
      {
        onSuccess: () =>
          toast({ title: t_i18n(decision === "approve" ? "Proposal approved" : "Proposal rejected"), variant: "success" }),
        onError: (error) =>
          toast({ title: t_i18n("The decision was not recorded"), description: error.message, variant: "error" }),
      },
    );
  return (
    <div className="space-y-3">
      <Diff proposal={proposal} />
      <Measured proposal={proposal} />
      <ImprovementEvidence proposalId={proposal.id} />
      {proposal.can_decide && (
        <div className="flex gap-2">
          <Button size="sm" className="gap-1.5" disabled={decide.isPending} onClick={() => act("approve")} aria-busy={decide.isPending || undefined}>
            {decide.isPending && <Spinner size="sm" tone="inherit" />}{!decide.isPending && <Check className="h-3 w-3" />}
            {t_i18n("Approve")}
          </Button>
          <Button variant="ghost" size="sm" className="gap-1.5" disabled={decide.isPending} onClick={() => act("reject")}>
            <X className="h-3 w-3" />
            {t_i18n("Reject")}
          </Button>
        </div>
      )}
    </div>
  );
}
