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

import { FlaskConical, MinusCircle } from "lucide-react";
import { InfoLabel } from "@/components/ui/info-tip";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import { PLACEBO_ARM_LABELS, isComparisonRun, isControlArm, runArm } from "@/lib/placebo";
import type { EvaluationRun, PlaceboTarget } from "@/types";
import { usePlaceboTargetLabel } from "./use-placebo-target-label";

/** The label of a control arm's target; mounted only on such an arm, so only it loads the skills. */
function TargetLabel({ target }: { target: PlaceboTarget }) {
  return <>{usePlaceboTargetLabel(target)}</>;
}

/**
 * `Placebo · <target>` / `No target · <target>` on a run of a placebo
 * comparison's control arm, `Real` on its real arm (FR-28); nothing on a run
 * outside any comparison.
 */
export function PlaceboArmTag({
  run,
  className,
}: {
  run: Pick<EvaluationRun, "arm" | "placebo_target" | "placebo_comparison_id">;
  className?: string;
}) {
  const { t_i18n } = useFormatter();
  if (!isComparisonRun(run)) return null;
  if (!isControlArm(run)) {
    return (
      <InfoLabel className="inline-flex" content={t_i18n("The agent as configured, in a placebo comparison.")}>
        <span
          className={cn(
            "inline-flex items-center gap-1 whitespace-nowrap rounded-sm border border-elevation-subtle bg-elevation-highlight px-1.5 py-0.5 content-caption font-medium text-default-primary",
            className,
          )}
        >
          <FlaskConical className="h-2.5 w-2.5" />
          {t_i18n(PLACEBO_ARM_LABELS.real)}
        </span>
      </InfoLabel>
    );
  }
  const arm = runArm(run) === "no_target" ? "no_target" : "placebo";
  const Icon = arm === "placebo" ? FlaskConical : MinusCircle;
  const hint =
    arm === "placebo"
      ? t_i18n("The agent with this target replaced by a neutral text of the same length.")
      : t_i18n("The agent with this target removed.");
  return (
    <InfoLabel className="inline-flex" content={hint}>
      <span
        className={cn(
          "inline-flex items-center gap-1 whitespace-nowrap rounded-sm border px-1.5 py-0.5 content-caption font-medium",
          arm === "placebo"
            ? "border-filigran-ia-primary/30 bg-filigran-ia-primary/10 text-filigran-ia-primary"
            : "border-elevation-subtle bg-elevation-highlight text-default-secondary",
          className,
        )}
      >
        <Icon className="h-2.5 w-2.5" />
        {t_i18n(PLACEBO_ARM_LABELS[arm])}
        {run.placebo_target && (
          <>
            {" · "}
            <TargetLabel target={run.placebo_target} />
          </>
        )}
      </span>
    </InfoLabel>
  );
}
