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

import { Pause } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { InfoLabel } from "@/components/ui/info-tip";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";

import { type AutomaticRunsEvaluation, hasAutomaticRuns } from "./automatic-runs";

/**
 * **Automatic runs paused** on an evaluation whose schedule is paused (`enabled`
 * off). Paused only stops those: a run
 * started by hand always runs, so nothing is shown without an automatic trigger.
 */
export function AutomaticRunsPausedBadge({
  evaluation,
  className,
}: {
  evaluation: AutomaticRunsEvaluation;
  className?: string;
}) {
  const { t_i18n } = useFormatter();
  if (evaluation.enabled || !hasAutomaticRuns(evaluation)) return null;
  return (
    <InfoLabel className="inline-flex shrink-0" content={t_i18n("The schedule is paused, so no run starts on its own. A run you start still runs.")}>
      <Badge variant="outline" className={cn("shrink-0 gap-1 text-[9px]", className)}>
        <Pause className="h-2.5 w-2.5" />
        {t_i18n("Automatic runs paused")}
      </Badge>
    </InfoLabel>
  );
}
