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
 * Pure helpers of "How it works" on the fleet About page (#10327): the steps
 * shown before the fold, and what each trigger chip explains. Nothing here
 * calls the API.
 */
import type { FleetDocStructured } from "@/hooks/api/useFleetApi";

/**
 * The steps shown before "Show all N steps". Folded, each body is also clamped
 * to two lines, so the column never makes the mini graph's card taller than its
 * drawing (UX round 1, B1: no dead band under the graph); unfolded, every body
 * reads in full.
 */
export const STEPS_FOLD = 4;

/** A step of the documentation as the page reads it: a legacy version may carry no body. */
type DocStep = NonNullable<FleetDocStructured["steps"]>[number];

/** The steps to draw: a step without a title is dropped, a blank body reads as none (no empty row). */
export function visibleSteps(steps: readonly DocStep[] | null | undefined, expanded: boolean): { shown: DocStep[]; hidden: number } {
  const kept = (steps ?? [])
    .filter((s) => (s.title ?? "").trim() !== "")
    .map((s) => ({ ...s, detail: (s.detail ?? "").trim() }));
  if (expanded || kept.length <= STEPS_FOLD) return { shown: kept, hidden: 0 };
  return { shown: kept.slice(0, STEPS_FOLD), hidden: kept.length - STEPS_FOLD };
}

/** The most automation names a trigger chip's tooltip lists before "and N more". */
const CHIP_NAMES = 3;

/**
 * What starts one trigger kind in this fleet. `names` lists at most CHIP_NAMES
 * distinct automation names, each with how many automations carry it; `more`
 * counts the automations not covered by those names, so `more` plus the shown
 * counts always equals `count` (UX round 1, B2: true numbers).
 */
export interface TriggerKindSummary {
  kind: string;
  count: number;
  names: { name: string; count: number }[];
  more: number;
}

/** One summary per trigger kind of the fleet's automations (the chips of "How it works"), from the configuration only. */
export function triggerKindSummaries(structured: FleetDocStructured | null | undefined): TriggerKindSummary[] {
  const byKind = new Map<string, string[]>();
  for (const a of structured?.automations ?? []) {
    if (!a.trigger) continue;
    const names = byKind.get(a.trigger) ?? [];
    names.push(a.name);
    byKind.set(a.trigger, names);
  }
  return [...byKind.keys()].sort().map((kind) => {
    const all = byKind.get(kind) ?? [];
    const perName = new Map<string, number>();
    for (const n of all) perName.set(n, (perName.get(n) ?? 0) + 1);
    const names = [...perName.entries()]
      .sort(([x], [y]) => x.localeCompare(y))
      .slice(0, CHIP_NAMES)
      .map(([name, count]) => ({ name, count }));
    const covered = names.reduce((sum, n) => sum + n.count, 0);
    return { kind, count: all.length, names, more: all.length - covered };
  });
}
