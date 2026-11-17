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
 * The one look of a compact chip in an answer: the working-file chip and the
 * inline chip of a marker in a table cell, a list item or a quote (#11697).
 * Both read their classes here so they never drift apart: same surface, same
 * hover and the design system's focus ring on keyboard focus.
 */
import { cn } from "@/lib/utils";

const INLINE_CHIP_BASE =
  "inline-flex min-w-0 max-w-[280px] items-center gap-2 rounded-sm border border-accent/25 bg-accent/10 px-3 py-1.5 align-middle content-compact text-default-secondary";

const INLINE_CHIP_INTERACTIVE =
  "cursor-pointer transition-colors hover:border-accent/60 hover:bg-accent/20 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus";

/** The classes of a compact answer chip; *interactive* for a link or a button. */
export function inlineChipClass(interactive = false): string {
  return cn(INLINE_CHIP_BASE, interactive && INLINE_CHIP_INTERACTIVE);
}
