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

// A run scored by another metric suite than the one the platform reads answers with now:
// the review canonicalizes each answer with the current suite, so an object it now matches
// can show as found while the run's stored figures still count it as missed. Re-scoring the
// run (the existing Re-score) makes both agree.

import { useQueryClient } from "@tanstack/react-query";
import { Alert } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/toast";
import { useRescoreRun } from "@/hooks/api/useEvaluationApi";
import { useAiActionDisabled, useAiDisabledReason } from "@/hooks/useAiDisabled";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";

interface DriftRun {
  id: string;
  name: string;
  version: string | null | undefined;
  /** Why the run's last re-score failed. */
  rescoreError?: string | null;
}

export function SuiteDriftNotice({ runs, current }: { runs: DriftRun[]; current: string | null | undefined }) {
  const { t_i18n } = useFormatter();
  const qc = useQueryClient();
  const { toast } = useToast();
  const rescore = useRescoreRun();
  const aiOff = useAiActionDisabled();
  const aiReason = useAiDisabledReason();
  const drifted = [...new Map(runs.filter((r) => current && r.version && r.version !== current).map((r) => [r.id, r])).values()];
  if (!current || drifted.length === 0) return null;

  const rescoreAll = async () => {
    try {
      await Promise.all(drifted.map((r) => rescore.mutateAsync({ runId: r.id })));
      toast({
        title: t_i18n("Re-scoring started"),
        description: t_i18n("The figures and this review agree once the re-scoring ends."),
        variant: "info",
      });
      for (const key of ["evaluation-comparison", "evaluation-bundle-review", "evaluation-bundle-reviews"]) {
        void qc.invalidateQueries({ queryKey: [key] });
      }
    } catch (error) {
      toast({ title: t_i18n("Re-scoring failed"), description: error instanceof Error ? error.message : undefined, variant: "error" });
    }
  };

  return (
    <Alert
      severity="warning"
      title={t_i18n("{runs} scored by metric suite {versions}; this review reads the answers with {current}.", {
        runs: drifted.map((r) => r.name).join(", "),
        versions: [...new Set(drifted.map((r) => r.version))].join(", "),
        current,
      })}
      description={
        drifted.find((r) => r.rescoreError)?.rescoreError
          ? t_i18n("The last re-score failed: {reason}", { reason: drifted.find((r) => r.rescoreError)?.rescoreError ?? "" })
          : t_i18n(
              "An object the newer suite matches can show as found here while the run's figures still count it as missed. Re-score the run so its figures and this review agree.",
            )
      }
      action={
        <Tip content={aiOff ? aiReason : t_i18n("Score the stored answers again with the current suite; the agents do not run again.")} wrap="auto">
          <Button variant="outline" size="sm" onClick={rescoreAll} disabled={aiOff || rescore.isPending} aria-busy={rescore.isPending || undefined}>
            {t_i18n("Re-score")}
          </Button>
        </Tip>
      }
    />
  );
}
