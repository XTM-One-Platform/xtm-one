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

import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// Config card — visual read-only setting with icon
// ---------------------------------------------------------------------------
export function ConfigCard({
  icon: Icon, iconColor, label, value, isBoolean, active, activeTone = "emerald",
}: {
  icon: React.ElementType;
  iconColor: string;
  label: string;
  value: string;
  isBoolean?: boolean;
  active?: boolean;
  /**
   * Colour semantics of the active state. "emerald" (default) reads as a
   * desirable feature being on; "amber" reads as a caution state being on
   * (e.g. dry run, where every tool call is stubbed).
   */
  activeTone?: "emerald" | "amber";
}) {
  const dotOn = activeTone === "amber"
    ? "bg-feedback-alert-primary shadow-[0_0_6px_rgba(251,191,36,0.4)]"
    : "bg-feedback-success-primary shadow-[0_0_6px_rgba(52,211,153,0.4)]";
  const textOn = activeTone === "amber" ? "text-feedback-warning-primary" : "text-feedback-success-primary";
  return (
    <div className="flex items-start gap-3 rounded-sm bg-elevation-hover px-4 py-3.5">
      <div className={cn("mt-0.5 shrink-0", iconColor)}>
        <Icon className="h-[18px] w-[18px]" />
      </div>
      <div className="min-w-0 flex-1">
        <p className="text-[13px] text-default-secondary leading-none">{label}</p>
        {isBoolean ? (
          <div className="flex items-center gap-2 mt-1.5">
            <div className={cn("h-2 w-2 rounded-full", active ? dotOn : "bg-elevation-hover")} />
            <span className={cn("content-base font-semibold", active ? textOn : "text-default-secondary")}>
              {value}
            </span>
          </div>
        ) : (
          <p className="content-base font-semibold mt-1 truncate">{value}</p>
        )}
      </div>
    </div>
  );
}
