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
 * The measured half of the `profiles` block, apart from the component
 * so it can be unit-tested and so the renderer file exports components only.
 */
import { markupToPlainText } from "../markup.ts";

/** The class of the one-line text run the fit pass and the layout audit both read. */
export const PROFILE_LINE_TEXT_CLASS = "deck-profile-line-text";
/** The person's name and role: one line each, fitted and audited like a fact. */
export const PROFILE_NAME_CLASS = "deck-profile-name";
export const PROFILE_TITLE_CLASS = "deck-profile-title";

/** Every one-line run of a card the fit pass shrinks before the ellipsis cuts it. */
const ONE_LINE_RUNS = [PROFILE_LINE_TEXT_CLASS, PROFILE_NAME_CLASS, PROFILE_TITLE_CLASS]
  .map((name) => `.${name}`)
  .join(", ");

/** Four cards side by side is the widest a 16:9 slide reads. */
const PROFILES_MAX_COLUMNS = 4;

/**
 * The columns of a balanced grid for `count` people when the block sets none:
 * the fewest rows four columns allow, then the fewest columns that keep that
 * row count - 5 and 6 are 3 + 2 and 3 + 3, 9 is 3 x 3, 10 is 4 + 4 + 2 -
 * so the last row is never a lone card under a full row of four.
 */
export function profileColumns(count: number): number {
  if (count <= 1) return 1;
  const rows = Math.ceil(count / PROFILES_MAX_COLUMNS);
  return Math.ceil(count / rows);
}

/** The initials drawn in the portrait circle when no photo is given. */
export function profileInitials(name: string): string {
  const words = markupToPlainText(name)
    .split(/[\s-]+/)
    .filter(Boolean);
  if (!words.length) return "";
  const first = words[0][0] ?? "";
  const last = words.length > 1 ? (words[words.length - 1][0] ?? "") : "";
  return (first + last).toUpperCase();
}

/**
 * Shrink a one-line run one step before letting the ellipsis cut it.
 *
 * Measured, not guessed: after every layout the run is reset to its base
 * size; a run wider than its box gets `data-fit="tight"` (the stylesheet's
 * smaller step) and is measured again; one that still does not fit keeps the
 * ellipsis and is marked `data-truncated`, the attribute the audit's own
 * measurement corroborates. The runs are a fact line, the name and the
 * title. `clientWidth` / `scrollWidth` are layout metrics, untouched by the
 * canvas transform.
 *
 * The identity is ONE type step per block: when one name (or one title)
 * needs the smaller step, every name (or title) of the block takes it, so a
 * row never sets two sizes side by side and the baselines line up. A fact
 * line keeps its own step: it sits beside its tile, not beside its peers.
 */
export function fitProfileLines(root: HTMLElement): void {
  const runs = Array.from(root.querySelectorAll<HTMLElement>(ONE_LINE_RUNS));
  for (const run of runs) {
    delete run.dataset.fit;
    delete run.dataset.truncated;
    if (run.scrollWidth <= run.clientWidth + 1) continue;
    run.dataset.fit = "tight";
    if (run.scrollWidth > run.clientWidth + 1) run.dataset.truncated = "true";
  }
  for (const name of [PROFILE_NAME_CLASS, PROFILE_TITLE_CLASS]) {
    const peers = runs.filter((run) => run.matches(`.${name}`));
    if (!peers.some((run) => run.dataset.fit === "tight")) continue;
    for (const run of peers) run.dataset.fit = "tight";
  }
}
