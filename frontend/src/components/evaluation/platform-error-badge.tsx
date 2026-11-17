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

import { ServerCrash } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { InfoLabel } from "@/components/ui/info-tip";
import { useFormatter } from "@/i18n";
import { PLATFORM_ERROR_BADGE_CONFIG } from "@/lib/constants";
import { cn } from "@/lib/utils";

/**
 * The entries of a run the platform could not run. They are left out of every
 * headline, so its scores are computed over fewer entries than a run that ran them all:
 * shown wherever runs are listed or compared, never only on the run page.
 */
export function PlatformErrorBadge({
  run,
  className,
}: {
  run: { platform_error_count?: number | null };
  className?: string;
}) {
  const { t_i18n } = useFormatter();
  const count = run.platform_error_count ?? 0;
  if (count <= 0) return null;
  return (
    <InfoLabel
      className="inline-flex shrink-0"
      content={t_i18n(
        "The platform could not run the agent on {count} entries. They are left out of every headline, so this run's scores are computed without them.",
        { count },
      )}
    >
      <Badge
        className={cn(
          "shrink-0 gap-1 text-[9px]",
          PLATFORM_ERROR_BADGE_CONFIG.color,
          PLATFORM_ERROR_BADGE_CONFIG.bg,
          className,
        )}
      >
        <ServerCrash className="h-2.5 w-2.5" />
        {t_i18n("{count} left out", { count })}
      </Badge>
    </InfoLabel>
  );
}
