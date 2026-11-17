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
 * A handful of colours so two readers side by side are told apart; the pick is
 * a stable hash of the user id, so one person keeps one colour across
 * heartbeats and sessions - their avatar (`PresenceAvatars`), and the outline
 * of the block they have selected on the stage (`SlideEditLayer`).
 *
 * A categorical palette - six hues carrying no meaning - so it names the
 * design system's colour PRIMITIVES, as the deck's chart series do (the
 * library has no categorical palette, LIBRARY-FEEDBACK F24). Not the semantic
 * tokens: those collide (in XTM One mode the accent IS the IA purple, in
 * Copilot dark mode the accent and the info tone are the same blue) and would
 * lend a reader the meaning of an error or a warning.
 */
const SWATCHES = [
  { swatch: "bg-[var(--blue-500)]/20 text-[var(--blue-500)]", outline: "border-[var(--blue-500)]", dot: "bg-[var(--blue-500)]" },
  { swatch: "bg-[var(--purple-400)]/20 text-[var(--purple-200)]", outline: "border-[var(--purple-300)]", dot: "bg-[var(--purple-300)]" },
  { swatch: "bg-[var(--yellow-400)]/20 text-[var(--yellow-300)]", outline: "border-[var(--yellow-400)]", dot: "bg-[var(--yellow-400)]" },
  { swatch: "bg-[var(--green-500)]/20 text-[var(--green-300)]", outline: "border-[var(--green-400)]", dot: "bg-[var(--green-400)]" },
  { swatch: "bg-[var(--red-500)]/20 text-[var(--red-200)]", outline: "border-[var(--red-300)]", dot: "bg-[var(--red-300)]" },
  { swatch: "bg-[var(--turquoise-500)]/20 text-[var(--turquoise-300)]", outline: "border-[var(--turquoise-400)]", dot: "bg-[var(--turquoise-400)]" },
] as const;

type PresenceColors = (typeof SWATCHES)[number];

/** One person's colours: the avatar swatch, the outline of their selection, its dot. */
export function presenceColors(userId: string): PresenceColors {
  let hash = 0;
  for (let i = 0; i < userId.length; i += 1) hash = (hash * 31 + userId.charCodeAt(i)) >>> 0;
  return SWATCHES[hash % SWATCHES.length];
}
