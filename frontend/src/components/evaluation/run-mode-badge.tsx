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

import { Badge } from "@/components/ui/badge";
import { InfoLabel } from "@/components/ui/info-tip";
import { useFormatter } from "@/i18n";
import { RUN_MODE_BADGE_CONFIG } from "@/lib/constants";
import { cn } from "@/lib/utils";

import { type RecordedRunMode, runModeBadges } from "./run-mode";

/**
 * A run's recorded mode: **LLM Only** when it ran in LLM Only, and how
 * many of its entries fell back to LLM Only (older runs only). Nothing otherwise.
 */
export function RunModeBadge({ run, className }: { run: RecordedRunMode; className?: string }) {
  const { t_i18n } = useFormatter();
  const { llmOnly, fallbackEntries } = runModeBadges(run);
  if (!llmOnly && fallbackEntries === 0) return null;
  const { llmOnly: llmOnlyStyle, fallback: fallbackStyle } = RUN_MODE_BADGE_CONFIG;
  return (
    <>
      {llmOnly && (
        <InfoLabel
          className="inline-flex shrink-0"
          content={t_i18n(
            "Only the agent's persona and the entry were sent to the model. Its handovers, tools and knowledge bases did not run.",
          )}
        >
          <Badge className={cn("shrink-0 text-[9px]", llmOnlyStyle.color, llmOnlyStyle.bg, className)}>
            {t_i18n("LLM Only")}
          </Badge>
        </InfoLabel>
      )}
      {fallbackEntries > 0 && (
        <InfoLabel
          className="inline-flex shrink-0"
          content={t_i18n(
            "The agent's run failed on these entries and they were answered by its persona alone. Recorded before fallbacks were removed.",
          )}
        >
          <Badge className={cn("shrink-0 text-[9px]", fallbackStyle.color, fallbackStyle.bg, className)}>
            {t_i18n("{count} fell back to LLM Only", { count: fallbackEntries })}
          </Badge>
        </InfoLabel>
      )}
    </>
  );
}

/** One entry answered in LLM Only after its Full Loop run failed, or reused from such an answer. */
export function FallbackEntryBadge() {
  const { t_i18n } = useFormatter();
  const { fallback } = RUN_MODE_BADGE_CONFIG;
  return (
    <InfoLabel className="inline-flex shrink-0" content={t_i18n("The agent's run failed on this entry and it was answered by its persona alone. Recorded before fallbacks were removed.")}>
      <Badge className={cn("shrink-0 text-[9px]", fallback.color, fallback.bg)}>
        {t_i18n("Fell back to LLM Only")}
      </Badge>
    </InfoLabel>
  );
}
