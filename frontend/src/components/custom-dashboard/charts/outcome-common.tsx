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
 * The pieces the validation views (`OutcomeViews.tsx`) share: the greyed
 * sample a view shows while its data has no count, the empty note, and an icon
 * by its Lucide name. Their hooks are `outcome-hooks.ts`.
 */
import type { CSSProperties, ReactNode } from "react";

import { DeckIcon } from "@/components/presentation/DeckIcon";
import { SampleBadge } from "@/components/ui/sample-badge";

import type { SampleDisplay } from "./outcome-hooks";

/** A view's look while its data has no count: greyed (`full`) or desaturated (`subtle`), marked "Sample", not interactive. */
export function SampleFrame({ display, children }: { display: Exclude<SampleDisplay, "off">; children: ReactNode }) {
  return (
    <div className="relative h-full w-full" data-sample={display}>
      <div
        className="pointer-events-none h-full w-full select-none"
        style={{ filter: display === "full" ? "grayscale(1)" : "grayscale(0.6)", opacity: display === "full" ? 0.45 : 0.8 }}
        aria-hidden="true"
      >
        {children}
      </div>
      <SampleBadge className="absolute right-0 top-0" />
    </div>
  );
}

/** Nothing to show: an inbox medallion over a sentence. */
export { InboxNote as OutcomeEmpty } from "./VizNote";

/** An icon by its Lucide name, in a colour. */
export function OutcomeIcon({ name, size, color, style }: { name: string | undefined; size: number; color?: string; style?: CSSProperties }) {
  if (!name) return null;
  return (
    <span className="inline-flex shrink-0" style={{ color, width: size, height: size, ...style }} aria-hidden="true">
      <DeckIcon name={name} size={size} strokeWidth={2} />
    </span>
  );
}
