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
 * "Held" badge for background AI work waiting while agentic AI is unavailable
 * on this platform - Community Edition, the Filigran license missing (Copilot
 * mode), or AI turned off - or while the quota store cannot be
 * reached. The work has not failed: it runs by itself as soon as
 * what it waits for is back.
 */
import { PauseCircle } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { useTriggerTabIndex } from "@/components/ui/row-target-context";
import { Tip } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { heldCause } from "@/lib/held";
import { useIsXtmOne } from "@/hooks/usePlatformConfig";
import { useFormatter } from "@/i18n";

/**
 * The sentence a hold is explained with: the badge's tooltip, and the hover
 * card fact of a held Operations row (a pure function, callable in a list).
 */
export function heldExplanation(t_i18n: (key: string) => string, reason: string, isXtmOne: boolean): string {
  const cause = heldCause(reason, isXtmOne);
  return cause === "ai_disabled"
    ? t_i18n("Held: AI is not available on this platform (AI turned off). It runs by itself as soon as agentic AI is available again.")
    : cause === "quota_store"
      ? t_i18n("Held: agentic usage cannot be metered right now (the quota store is unreachable). It runs by itself as soon as the quota store answers again.")
      : cause === "license"
        ? t_i18n("Held: AI is not available on this platform (no valid Filigran license). It runs by itself as soon as agentic AI is available again.")
        : t_i18n("Held: AI is not available on this platform (Community Edition). It runs by itself as soon as agentic AI is available again.");
}

export function HeldBadge({ reason, className }: { reason: string; className?: string }) {
  const { t_i18n } = useFormatter();
  const explanation = heldExplanation(t_i18n, reason, useIsXtmOne());
  const tabIndex = useTriggerTabIndex();
  return (
    <Tip content={explanation}>
      <Badge
        variant="warning"
        tabIndex={tabIndex}
        className={cn(
          "gap-0.5 cursor-default focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus",
          className,
        )}
      >
        <PauseCircle className="h-2.5 w-2.5" aria-hidden="true" />
        {t_i18n("Held")}
        <span className="sr-only">{explanation}</span>
      </Badge>
    </Tip>
  );
}
