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
 * Lucide icon by kebab-case name, for agent-authored slides.
 *
 * The catalog is one lazily loaded chunk shared by every deck (see
 * `deck-icons.ts`). Until it arrives the component renders a placeholder box of
 * the final size, so the layout does not jump when the icons appear — and so
 * the export capture can wait for `.deck-icon-pending` to disappear (it waits
 * for fonts and images on its own, but not for lazy chunks). A catalog that
 * cannot load leaves the box in place without holding the capture
 * (`useDeckIconCatalog`).
 *
 * Defensive on purpose: an unknown name renders nothing (never a broken box,
 * never a console error) — the same failure mode as the unknown-layout fallback.
 */
import { createElement, useRef } from "react";

import { cn } from "@/lib/utils";

import { resolveDeckIcon } from "./deck-icons";
import { useDeckIconCatalog } from "./useDeckIconCatalog";

/** On Lucide's 24-unit grid, so it scales with the icon. */
export const DECK_ICON_STROKE_WIDTH = 1.75;

export function DeckIcon({
  name,
  className,
  size,
  strokeWidth = DECK_ICON_STROKE_WIDTH,
}: {
  name: string | undefined | null;
  className?: string;
  /** In canvas pixels (the deck is authored at 1920x1080). */
  size?: number;
  strokeWidth?: number;
}) {
  const placeholder = useRef<HTMLSpanElement>(null);
  const { catalog, waiting } = useDeckIconCatalog(placeholder, Boolean(name));

  if (!name) return null;
  if (!catalog) {
    return (
      <span
        ref={placeholder}
        className={cn("deck-icon deck-icon-pending", className)}
        // The export capture waits for every `[data-deck-pending]` to clear.
        data-deck-pending={waiting ? "true" : undefined}
        data-deck-unfinished={waiting ? undefined : "true"}
        aria-hidden="true"
      />
    );
  }
  const icon = resolveDeckIcon(catalog, name);
  if (!icon) return null;
  // `createElement` rather than a JSX tag: the component is looked up from a
  // catalog at render time, and it is a stable module-level component (not one
  // created per render), so remounting is not a concern.
  return createElement(icon, {
    className: cn("deck-icon", className),
    size,
    strokeWidth,
    "aria-hidden": "true",
  });
}
