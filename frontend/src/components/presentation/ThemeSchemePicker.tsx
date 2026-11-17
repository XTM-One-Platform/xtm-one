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
 * The deck THEME picker: Dark or Light, and nothing else.
 *
 * A theme is only the colour scheme a deck is shown in; the brand comes from
 * the template. So the picker offers the two schemes and greys out the one the
 * chosen template cannot dress (a dark-only brand has no light palette),
 * saying why on hover — a control that silently accepts a choice the save
 * would change is how "theme" became a mystery in the first place.
 *
 * Shared by the deck editor's Design section, the "New presentation" dialog
 * and the Templates surfaces, so the two words mean the same thing everywhere.
 *
 * It is a real radio group to the keyboard as well: one tab stop (the
 * checked option, else the first enabled one), and the arrow keys, Home and
 * End move focus AND select, skipping an option the template cannot dress.
 */
import { useId, useRef, type KeyboardEvent } from "react";
import { Moon, Sun } from "lucide-react";

import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { PresentationTheme, PresentationThemeScheme } from "@/types";

import { THEME_SCHEMES } from "./previewSpec";
import { useSchemeLabel } from "./scheme-label";

const SCHEME_ICON: Record<PresentationThemeScheme, typeof Moon> = { dark: Moon, light: Sun };

/** Where a key moves the selection within the enabled options, wrapping. */
function stepFor(key: string, index: number, count: number): number | null {
  switch (key) {
    case "ArrowRight":
    case "ArrowDown":
      return (index + 1) % count;
    case "ArrowLeft":
    case "ArrowUp":
      return (index - 1 + count) % count;
    case "Home":
      return 0;
    case "End":
      return count - 1;
    default:
      return null;
  }
}

export function ThemeSchemePicker({
  value,
  onChange,
  supported,
  templateName,
  size = "md",
  className,
}: {
  value: PresentationTheme;
  onChange: (theme: PresentationTheme) => void;
  /** The schemes the current template allows; both for no template. */
  supported: readonly PresentationThemeScheme[];
  /** Named in the "not supported" tooltip; `null` reads as "this template". */
  templateName?: string | null;
  size?: "sm" | "md";
  className?: string;
}) {
  const { t_i18n } = useFormatter();
  const schemeLabel = useSchemeLabel();
  const noteId = useId();
  const reason = t_i18n("Not supported by {template}", { template: templateName ?? t_i18n("this template") });
  const buttons = useRef(new Map<PresentationThemeScheme, HTMLButtonElement>());
  const enabled = THEME_SCHEMES.filter((scheme) => supported.includes(scheme));
  // The group's single tab stop: the checked option when it can be chosen,
  // otherwise the first option that can.
  const tabStop = enabled.find((scheme) => scheme === value) ?? enabled[0];

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const index = enabled.findIndex((scheme) => buttons.current.get(scheme) === event.target);
    if (index < 0) return; // Not on a radio (the tooltip wrapper of a disabled one).
    const step = stepFor(event.key, index, enabled.length);
    if (step === null) return;
    event.preventDefault();
    const next = enabled[step];
    buttons.current.get(next)?.focus();
    if (next !== value) onChange(next);
  };

  return (
    <div
      role="radiogroup"
      aria-label={t_i18n("Theme")}
      className={cn("grid grid-cols-2 gap-1 rounded-sm border border-elevation-subtle bg-elevation-default p-1", className)}
      onKeyDown={onKeyDown}
    >
      {THEME_SCHEMES.map((scheme) => {
        const Icon = SCHEME_ICON[scheme];
        const isSupported = supported.includes(scheme);
        const active = value === scheme;
        const reasonId = `${noteId}-${scheme}`;
        const button = (
          <button
            ref={(element) => {
              if (element) buttons.current.set(scheme, element);
              else buttons.current.delete(scheme);
            }}
            type="button"
            role="radio"
            aria-checked={active}
            aria-describedby={isSupported ? undefined : reasonId}
            disabled={!isSupported}
            tabIndex={isSupported && scheme === tabStop ? 0 : -1}
            onClick={() => onChange(scheme)}
            className={cn(
              "flex w-full items-center justify-center gap-1.5 rounded-sm content-compact transition-colors",
              size === "sm" ? "h-7" : "h-8",
              active
                ? "bg-accent/15 text-accent ring-1 ring-accent/40"
                : "text-default-secondary hover:bg-elevation-highlight hover:text-default-primary",
              !isSupported && "cursor-not-allowed opacity-40 hover:bg-transparent hover:text-default-secondary",
            )}
          >
            <Icon className="h-3.5 w-3.5" />
            {schemeLabel(scheme)}
          </button>
        );
        return isSupported ? (
          <div key={scheme} className="flex">{button}</div>
        ) : (
          // A disabled button swallows pointer events: `wrap` gives the tooltip
          // a hoverable trigger that says why the scheme is off. The wrapper
          // stays out of the tab order - the group owns its one tab stop - and
          // the same reason is the radio's description for assistive tech.
          <Tip key={scheme} wrap wrapTabIndex={-1} wrapClassName="flex w-full" content={reason}>
            {button}
            <span id={reasonId} className="sr-only">
              {reason}
            </span>
          </Tip>
        );
      })}
    </div>
  );
}
