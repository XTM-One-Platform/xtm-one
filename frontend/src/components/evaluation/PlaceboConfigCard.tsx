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
import { Check, FlaskConical, Pencil, Trash2, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { InfoTip } from "@/components/ui/info-tip";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { useToast } from "@/components/ui/toast";
import { useUpdateEvaluation } from "@/hooks/api/useEvaluationApi";
import { useSkills } from "@/hooks/api/useSkillApi";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useFormatter } from "@/i18n";
import {
  PLACEBO_TARGET_LABELS,
  configFromDraft,
  draftFromConfig,
  placeboDraftError,
  type PlaceboDraft,
} from "@/lib/placebo";
import type { EvaluationPlaceboConfig } from "@/types";
import { PlaceboConfigFields, PlaceboNoteTip } from "./PlaceboConfigFields";
import { Spinner } from "@filigran/design-system";

const HEADING = "mb-1 flex items-center gap-1 content-caption font-medium uppercase tracking-wider text-default-secondary";

/**
 * The Configuration tab's placebo control: shown, edited and saved
 * on its own, so a save sends `placebo_config` alone and an edit of the other
 * settings never depends on it.
 */
export function PlaceboConfigCard({
  evaluation,
  canManage,
}: {
  evaluation: { id: string; execution_mode: string; placebo_config?: EvaluationPlaceboConfig | null };
  canManage: boolean;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const aiOff = useAiActionDisabled();
  const update = useUpdateEvaluation();
  const [ConfirmEl, confirm] = useConfirm();
  const [draft, setDraft] = useState<PlaceboDraft | null>(null);
  const config = evaluation.placebo_config ?? null;
  // The edit form offers every skill; the read view names its single-skill targets.
  const { data: skills = [] } = useSkills({ scope: "all" });
  const skillName = (id: string | null) => skills.find((skill) => skill.id === id)?.name ?? id ?? "";

  const save = async (placeboConfig: EvaluationPlaceboConfig | null) => {
    try {
      await update.mutateAsync({ id: evaluation.id, placebo_config: placeboConfig });
      toast({ title: placeboConfig ? t_i18n("Placebo control saved") : t_i18n("Placebo control removed"), variant: "success" });
      setDraft(null);
    } catch (error) {
      toast({
        title: t_i18n("Failed to update"),
        description: error instanceof Error ? error.message : undefined,
        variant: "error",
      });
    }
  };

  const remove = async () => {
    if (
      await confirm({
        title: "Remove the placebo control?",
        description: "Past comparisons stay; this evaluation can no longer run a new one until it is set up again.",
        confirmLabel: "Remove",
      })
    ) {
      await save(null);
    }
  };

  return (
    <Card className="layer-1 bg-elevation-default backdrop-blur-sm border-elevation-subtle-soft">
      {ConfirmEl}
      <CardHeader className="pb-2">
        <div className="flex items-center justify-between">
          <CardTitle className="flex items-center gap-2 text-base font-medium">
            <FlaskConical className="h-4 w-4 text-filigran-ia-primary" />
            {t_i18n("Placebo control")}
            <PlaceboNoteTip note="control" />
          </CardTitle>
          {canManage && draft === null && (
            <div className="flex items-center gap-1">
              {config && (
                <Button variant="ghost" size="sm" className="gap-1.5 text-feedback-error-primary" onClick={remove} disabled={update.isPending || aiOff}>
                  <Trash2 className="h-3.5 w-3.5" /> {t_i18n("Remove")}
                </Button>
              )}
              <Button variant="ghost" size="sm" className="gap-1.5 text-default-secondary" onClick={() => setDraft(draftFromConfig(config))}>
                <Pencil className="h-3.5 w-3.5" /> {config ? t_i18n("Edit") : t_i18n("Set up")}
              </Button>
            </div>
          )}
          {draft !== null && (
            <div className="flex items-center gap-2">
              <Button variant="ghost" size="sm" className="gap-1 text-default-secondary" onClick={() => setDraft(null)}>
                <X className="h-3.5 w-3.5" /> {t_i18n("Cancel")}
              </Button>
              <Button
                size="sm"
                className="gap-1"
                onClick={() => save(configFromDraft(draft))}
                disabled={update.isPending || aiOff || placeboDraftError(draft, evaluation.execution_mode) !== null} aria-busy={update.isPending || undefined}>
                {update.isPending && <Spinner size="sm" tone="inherit" />}{!update.isPending && <Check className="h-3.5 w-3.5" />} {t_i18n("Save")}
              </Button>
            </div>
          )}
        </div>
      </CardHeader>
      <CardContent>
        {draft !== null ? (
          <PlaceboConfigFields draft={draft} onChange={setDraft} executionMode={evaluation.execution_mode} skills={skills} />
        ) : config ? (
          <div className="grid gap-4 sm:grid-cols-4">
            <div className="sm:col-span-2">
              <p className={HEADING}>
                {t_i18n("Targets")}
                <PlaceboNoteTip note="targets" />
              </p>
              <div className="flex flex-wrap gap-1.5">
                {config.targets.map((target) => (
                  <span
                    key={`${target.kind}:${target.skill_id ?? ""}`}
                    className="inline-flex items-center rounded-sm border border-filigran-ia-primary/20 bg-filigran-ia-primary/10 px-1.5 py-0.5 content-caption text-filigran-ia-primary"
                  >
                    {target.kind === "skill"
                      ? `${t_i18n(PLACEBO_TARGET_LABELS.skill)} · ${skillName(target.skill_id)}`
                      : t_i18n(PLACEBO_TARGET_LABELS[target.kind] ?? target.kind)}
                  </span>
                ))}
              </div>
            </div>
            <div>
              <p className={HEADING}>
                {t_i18n("Trials per entry")}
                <InfoTip content={t_i18n("Each entry runs this many times in every arm (1-30).")} label={t_i18n("What trials per entry means")} />
              </p>
              <p className="content-base">{config.trials_per_entry}</p>
            </div>
            <div>
              <p className={HEADING}>
                {t_i18n("No-target arm")}
                <PlaceboNoteTip note="noTargetArm" />
              </p>
              <p className="content-base">{config.include_no_target ? t_i18n("Yes") : t_i18n("No")}</p>
            </div>
            <div className="sm:col-span-4">
              <p className="mb-1 content-caption font-medium uppercase tracking-wider text-default-secondary">{t_i18n("Placebo text")}</p>
              <p className="content-compact text-default-secondary">
                {config.custom_text
                  ? t_i18n("Custom text ({count} characters)", { count: config.custom_text.length })
                  : t_i18n("The neutral corpus shipped with the platform")}
              </p>
            </div>
          </div>
        ) : (
          <p className="content-compact text-default-secondary">
            {t_i18n(
              "Not set up. A placebo control compares each agent with a copy of itself whose persona, Prompts or skills are replaced by neutral text of the same length, to see whether those instructions earn their place.",
            )}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
