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
 * Colours of the waiting arcade's offers: the reasoning window's
 * gamepad and the waiting panel's Take control, leaderboard trophy and status
 * line. A neutral colour at rest, the accent on hover and keyboard focus.
 *
 * Every value meets its WCAG minimum against the panels' `bg-accent/[0.03]` in
 * both modes of both themes (`waiting-offer-style.test.ts` computes it from
 * `index.css`): 4.5:1 for the 10 px text (1.4.3), 3:1 for an icon-only
 * control (1.4.11). The plain accent does not meet 4.5:1 as text in the
 * default light theme or in XTM One dark, so the accent is drawn mixed with
 * the foreground: the same hue, pulled towards the text colour of the mode.
 *
 * Tailwind reads these literals from this file; keep every class spelled out.
 */

/**
 * Under the waiting panel's controls and status line: the attract-mode canvas
 * animates beneath them (the letters under the controls, the invader along the
 * status line), so they sit on an opaque plate of the page background and
 * their contrast never depends on where the game is drawing.
 */
export const OFFER_PLATE = "rounded-sm bg-elevation-default";

/** Icon-only control at rest: `muted-foreground`, full strength in light mode. */
export const OFFER_ICON_REST = "text-default-secondary light:text-default-secondary";

/** Text at rest (`muted-foreground` stays under 4.5:1 in light mode). */
export const OFFER_TEXT_REST = "text-default-secondary";

/** The accent as readable text: an emphasis inside the status line. */
export const OFFER_ACCENT_TEXT =
  "text-[color:color-mix(in_srgb,var(--color-accent)_50%,var(--text-default-primary))]";

/** Hover and keyboard focus: the readable accent over an accent tint. */
/* 50%, not more: the library's light-theme accent is light enough that a
   heavier mix drops the hover state to 3.6:1 over the panel, under the 4.5:1
   the 10px label needs. `waiting-offer-style.test.ts` measures all four themes.
*/
export const OFFER_ACCENT_INTERACTIVE =
  "transition-colors hover:bg-accent/10 hover:text-[color:color-mix(in_srgb,var(--color-accent)_50%,var(--text-default-primary))] focus-visible:text-[color:color-mix(in_srgb,var(--color-accent)_50%,var(--text-default-primary))]";
