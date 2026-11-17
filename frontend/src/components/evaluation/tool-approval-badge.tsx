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

import { Hand } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { InfoLabel } from "@/components/ui/info-tip";
import { useFormatter } from "@/i18n";
import { TOOL_APPROVAL_BADGE_CONFIG } from "@/lib/constants";
import { cn } from "@/lib/utils";

/**
 * **Tool approval** on a run that held tool calls needing approval, as chat
 * holds them. Off is the default, so only that exception is marked.
 */
export function ToolApprovalBadge({
  run,
  className,
}: {
  run: { require_tool_approval?: boolean | null };
  className?: string;
}) {
  const { t_i18n } = useFormatter();
  if (run.require_tool_approval !== true) return null;
  return (
    <InfoLabel className="inline-flex shrink-0" content={t_i18n("Tool calls that need approval were held in this run, as chat holds them.")}>
      <Badge
        className={cn(
          "shrink-0 gap-1 text-[9px]",
          TOOL_APPROVAL_BADGE_CONFIG.color,
          TOOL_APPROVAL_BADGE_CONFIG.bg,
          className,
        )}
      >
        <Hand className="h-2.5 w-2.5" />
        {t_i18n("Tool approval")}
      </Badge>
    </InfoLabel>
  );
}

/** **Needed approval** on an entry whose tool calls were held, naming the tools. */
export function NeededApprovalBadge({ tools }: { tools?: string[] | null }) {
  const { t_i18n } = useFormatter();
  if (!tools || tools.length === 0) return null;
  return (
    <InfoLabel className="inline-flex shrink-0" content={t_i18n("Held for approval: {tools}.", { tools: tools.join(", ") })}>
      <Badge
        className={cn(
          "shrink-0 gap-1 text-[9px]",
          TOOL_APPROVAL_BADGE_CONFIG.color,
          TOOL_APPROVAL_BADGE_CONFIG.bg,
        )}
      >
        <Hand className="h-2.5 w-2.5" />
        {t_i18n("Needed approval")}
      </Badge>
    </InfoLabel>
  );
}
