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

import { ShieldCheck } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { InfoLabel } from "@/components/ui/info-tip";
import { useFormatter } from "@/i18n";
import { PROMPT_GUARD_BADGE_CONFIG } from "@/lib/constants";
import { cn } from "@/lib/utils";

/**
 * **Prompt guard** on a run that checked its prompts and answers with the
 * platform's prompt guard. Off is the default, so only that exception
 * is marked.
 */
export function PromptGuardBadge({
  run,
  className,
}: {
  run: { apply_prompt_guards?: boolean | null };
  className?: string;
}) {
  const { t_i18n } = useFormatter();
  if (run.apply_prompt_guards !== true) return null;
  return (
    <InfoLabel className="inline-flex shrink-0" content={t_i18n("The platform's prompt guard checked this run's prompts and answers, as chat does.")}>
      <Badge
        className={cn(
          "shrink-0 gap-1 text-[9px]",
          PROMPT_GUARD_BADGE_CONFIG.color,
          PROMPT_GUARD_BADGE_CONFIG.bg,
          className,
        )}
      >
        <ShieldCheck className="h-2.5 w-2.5" />
        {t_i18n("Prompt guard")}
      </Badge>
    </InfoLabel>
  );
}
