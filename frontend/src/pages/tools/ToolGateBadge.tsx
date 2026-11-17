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

import { HitlIcon } from "@/components/ui/hitl-icon";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { hitlState } from "@/lib/hitl";
import { cn } from "@/lib/utils";

/**
 * Whether a tool asks first, as a chip on the tools lists and an agent's tools.
 *
 * Rendered only while approvals are in use (`useToolGateSummary` is empty
 * otherwise). "Runs without asking" is the ordinary state of a read-only tool,
 * so it is drawn muted; "Asks first" is what a reader needs to notice.
 */
export function ToolGateBadge({
  gating,
  total,
}: {
  gating: number;
  /** Omitted for a single tool; given for a builtin group's aggregate. */
  total?: number;
}) {
  const { t_i18n } = useFormatter();
  const some = gating > 0;
  const label = !some
    ? t_i18n("Runs without asking")
    : total === undefined || gating === total
    ? t_i18n("Asks first")
    : t_i18n("{gating} of {total} ask first", { gating, total });
  return (
    <Tip
      content={
        some
          ? t_i18n("On an agent that asks before running tools, a person approves this before it runs.")
          : t_i18n("Runs without asking, on every agent.")
      }
    >
      <span
        className={cn(
          "inline-flex shrink-0 items-center gap-1 rounded-sm border px-1.5 py-0 text-[9px]",
          some
            ? "border-feedback-success-primary/25 bg-feedback-success-primary/10 text-feedback-success-primary"
            : "border-elevation-subtle-soft bg-elevation-hover text-default-secondary",
        )}
      >
        <HitlIcon state={hitlState(some)} className="size-2.5 text-current" decorative />
        {label}
      </span>
    </Tip>
  );
}
