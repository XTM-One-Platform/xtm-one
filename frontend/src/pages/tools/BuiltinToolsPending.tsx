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
 * What a built-in tool list shows before the registry
 * (`GET /settings/builtin-tools/groups`) is known: a skeleton while it
 * loads, an inline error line when the read failed. Every picker and chip row
 * that reads `useBuiltinTools()` renders this instead of an empty list or raw
 * ids, so the rest of the page stays alive.
 */
import { Icon } from "@filigran/design-system";
import { Skeleton } from "@/components/ui/skeleton";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";

/**
 * Widths, in px, of the pill skeletons (agent page badges), cycled in this
 * order. Real labels vary, so equal 112 px pills packed one more chip per row
 * than the loaded badges and the Tooling card grew by 36 px at 1024 when the
 * registry landed. Classes are spelled out so
 * Tailwind keeps them.
 */
const PILL_SKELETON_WIDTHS = [96, 144, 120] as const;
const PILL_WIDTH_CLASS: Record<(typeof PILL_SKELETON_WIDTHS)[number], string> = {
  96: "w-24",
  144: "w-36",
  120: "w-30",
};
/** Square composer chips keep one width (`w-28`). */
const SQUARE_SKELETON_WIDTH = 112;

interface BuiltinToolsPendingProps {
  /** From `useBuiltinTools()`: the read failed and nothing is cached. */
  isError: boolean;
  /**
   * "cards" for a picker grid, "rows" for a borderless list of toggle rows
   * (the Profile General Assistant override), "chips" for a badge row.
   */
  variant?: "cards" | "rows" | "chips";
  /**
   * With variant "chips": the shape of the loaded chips the skeleton stands
   * in for. "square" is a toggle chip (4 px radius, 6 px apart), "pill" the
   * agent page badges (`Badge`: full radius, 8 px apart), so each row keeps
   * its shape when the data lands.
   */
  chipShape?: "square" | "pill";
  /** Grid classes of the picker the skeleton stands in for. */
  gridClassName?: string;
  count?: number;
  testId?: string;
}

export function BuiltinToolsPending({
  isError,
  variant = "cards",
  chipShape = "square",
  gridClassName,
  count = 6,
  testId = "builtin-tools-pending",
}: BuiltinToolsPendingProps) {
  const { t_i18n } = useFormatter();
  if (isError) {
    return (
      <p
        role="alert"
        data-testid={`${testId}-error`}
        className="flex items-center gap-1.5 content-compact text-feedback-error-primary"
      >
        <Icon name="triangle-alert" size={16} className="shrink-0" />
        {t_i18n("Built-in tools could not be loaded")}
      </p>
    );
  }
  if (variant === "chips") {
    const pill = chipShape === "pill";
    return (
      // 28 px tall like the real chips, so the row keeps its height when the
      // data lands, and the radius and gap
      // of the chips it stands in for.
      <div
        data-testid={`${testId}-loading`}
        data-variant="chips"
        data-chip-shape={chipShape}
        aria-busy="true"
        className={cn("flex flex-wrap", pill ? "gap-2" : "gap-1.5")}
      >
        {Array.from({ length: count }).map((_, i) => {
          const width = pill ? PILL_SKELETON_WIDTHS[i % PILL_SKELETON_WIDTHS.length] : SQUARE_SKELETON_WIDTH;
          const shape = pill
            ? cn(PILL_WIDTH_CLASS[width as keyof typeof PILL_WIDTH_CLASS], "rounded-full")
            : "w-28 rounded-sm";
          return (
            // `contents`: the wrapper carries the test attributes without
            // taking part in the flex row.
            <div key={i} data-testid={`${testId}-loading-chip`} data-width={width} className="contents">
              <Skeleton className={cn("h-7", shape)} />
            </div>
          );
        })}
      </div>
    );
  }
  if (variant === "rows") {
    return (
      // The shape of the loaded toggle rows (Profile override: 38 px, no
      // border, 2 px apart, icon, label and switch), so the group keeps its
      // height when the registry lands.
      <div data-testid={`${testId}-loading`} data-variant="rows" aria-busy="true" className="space-y-0.5">
        {Array.from({ length: count }).map((_, i) => (
          <div key={i} data-testid={`${testId}-loading-row`} className="flex h-[38px] items-center gap-2.5 rounded-sm px-2.5">
            <Skeleton className="h-4 w-4 shrink-0" />
            <Skeleton className="h-3.5 w-1/3" />
            <Skeleton className="ml-auto h-5 w-9 shrink-0 rounded-full" />
          </div>
        ))}
      </div>
    );
  }
  return (
    <div data-testid={`${testId}-loading`} data-variant="cards" aria-busy="true" className={cn("grid gap-2", gridClassName)}>
      {Array.from({ length: count }).map((_, i) => (
        <div key={i} className="flex items-center gap-3 rounded-sm border border-elevation-subtle-soft p-3">
          <Skeleton className="h-5 w-5 shrink-0" />
          <div className="min-w-0 flex-1 space-y-1.5">
            <Skeleton className="h-3.5 w-1/2" />
            <Skeleton className="h-3 w-4/5" />
          </div>
        </div>
      ))}
    </div>
  );
}
