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
 * Who else has this deck open right now.
 *
 * A short stack of initials in the toolbar, the way a shared document shows its
 * readers: hover one for the name and what they are doing ("editing", "editing
 * with an assistant", "on slide 7"), the overflow circle for everyone it hides.
 * The badge keys on the entry's `activity`: a pencil for unsaved manual
 * edits, a spark for an assistant turn the server saw in flight — never for a
 * panel that is merely open. Fed by `usePresentationPresence`, which already
 * leaves the caller out — a person is never shown to themself. Renders nothing
 * for an empty room, so the toolbar keeps its shape when the reader is alone.
 */
import { Pencil, Sparkles } from "lucide-react";

import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { PresentationKind, PresentationPresenceEntry } from "@/types";

import { presenceColors } from "./presence-colors";

/** Circles drawn before the rest folds into a `+N`. */
const MAX_SHOWN = 4;

function swatchOf(userId: string): string {
  return presenceColors(userId).swatch;
}

/** "Jane Doe" -> "JD", "jane@x.io" -> "J", nothing -> "?". */
function initialsOf(name: string | null): string {
  const clean = (name ?? "").trim();
  if (!clean) return "?";
  const local = clean.includes("@") ? clean.split("@")[0] : clean;
  const parts = local.split(/[\s._-]+/).filter(Boolean);
  const letters = parts.slice(0, 2).map((part) => part[0]?.toUpperCase() ?? "");
  return letters.join("") || clean[0].toUpperCase();
}

/**
 * One person as a coloured initials circle — the unit of the presence stack,
 * reused by the comments panel for authors so a reader is the same colour
 * wherever they appear. `userId` seeds the colour; a missing one (a deleted
 * account) falls back to the neutral swatch.
 */
export function UserInitials({
  userId,
  name,
  size = "md",
  className,
}: {
  userId: string | null;
  name: string | null;
  size?: "sm" | "md";
  className?: string;
}) {
  return (
    <span
      aria-hidden="true"
      className={cn(
        "inline-flex shrink-0 items-center justify-center rounded-full font-semibold uppercase",
        size === "sm" ? "h-5 w-5 text-[8px]" : "h-6 w-6 text-[9px]",
        userId ? swatchOf(userId) : "bg-elevation-hover text-default-secondary",
        className,
      )}
    >
      {initialsOf(name)}
    </span>
  );
}

export function PresenceAvatars({
  viewers,
  kind = "deck",
  compact = false,
  portalContainer,
  className,
}: {
  viewers: PresentationPresenceEntry[];
  /** Names the unit in the tooltip: "on slide 7" vs "on page 2". */
  kind?: PresentationKind;
  compact?: boolean;
  /** Tooltip portal — the fullscreen stage while it is fullscreen. */
  portalContainer?: HTMLElement | null;
  className?: string;
}) {
  const { t_i18n } = useFormatter();
  if (viewers.length === 0) return null;

  const shown = viewers.slice(0, MAX_SHOWN);
  const hidden = viewers.slice(MAX_SHOWN);
  const size = compact ? "h-6 w-6 text-[9px]" : "h-7 w-7 content-caption";

  const describe = (viewer: PresentationPresenceEntry) => {
    const name = viewer.name?.trim() || t_i18n("Someone");
    if (viewer.activity === "assistant") return t_i18n("{name} is editing with an assistant", { name });
    if (viewer.activity === "editing") return t_i18n("{name} is editing", { name });
    if (viewer.slide_index !== null) {
      const position = viewer.slide_index + 1;
      return kind === "page"
        ? t_i18n("{name} is on page {position}", { name, position })
        : t_i18n("{name} is on slide {position}", { name, position });
    }
    return t_i18n("{name} is here", { name });
  };

  return (
    <div
      role="group"
      aria-label={t_i18n("Also viewing")}
      className={cn("flex items-center -space-x-1.5", className)}
    >
      {shown.map((viewer) => (
        <Tip key={viewer.user_id} content={describe(viewer)} container={portalContainer}>
          <span
            tabIndex={0}
            aria-label={describe(viewer)}
            className={cn(
              "relative inline-flex shrink-0 items-center justify-center rounded-full font-semibold uppercase",
              "ring-2 ring-[var(--bg-elevation-default)] focus-visible:outline-hidden focus-visible:ring-focus",
              size,
              swatchOf(viewer.user_id),
            )}
          >
            {initialsOf(viewer.name)}
            {viewer.activity !== "viewing" && (
              <span
                aria-hidden="true"
                className="absolute -bottom-0.5 -right-0.5 inline-flex h-3 w-3 items-center justify-center rounded-full bg-accent text-default-primary ring-1 ring-[var(--bg-elevation-default)]"
              >
                {viewer.activity === "assistant" ? <Sparkles className="h-2 w-2" /> : <Pencil className="h-2 w-2" />}
              </span>
            )}
          </span>
        </Tip>
      ))}
      {hidden.length > 0 && (
        <Tip
          content={hidden.map((viewer) => viewer.name?.trim() || t_i18n("Someone")).join(", ")}
          container={portalContainer}
        >
          <span
            tabIndex={0}
            aria-label={t_i18n("{count, plural, one {# more viewer} other {# more viewers}}", {
              count: hidden.length,
            })}
            className={cn(
              "inline-flex shrink-0 items-center justify-center rounded-full bg-elevation-hover font-medium text-default-secondary",
              "ring-2 ring-[var(--bg-elevation-default)] focus-visible:outline-hidden focus-visible:ring-focus",
              size,
            )}
          >
            +{hidden.length}
          </span>
        </Tip>
      )}
    </div>
  );
}
