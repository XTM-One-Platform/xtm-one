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
 * The descendants of a run as a delegation tree reads.
 *
 * `/assignments/runs/{id}/descendants` returns every run under a root, in
 * creation order and without depth. Listed as they come, a run delegated by a
 * delegated agent reads as the root's own delegation. Here each run follows
 * the run that delegated it - depth first, siblings in creation order - with
 * its depth under the root (1 = delegated by the root itself). A run whose
 * parent is not in the list hangs directly under the root, so nothing the API
 * returned is dropped.
 *
 * Pure (no React, no `@/` import) so `run-tree.test.ts` runs under
 * `node --test`.
 */

export interface RunTreeRun {
  id: string;
  parent_run_id?: string | null;
  created_at: string;
}

interface RunTreeEntry<T extends RunTreeRun> {
  run: T;
  depth: number;
}

function byCreation(a: RunTreeRun, b: RunTreeRun): number {
  const delta = Date.parse(a.created_at) - Date.parse(b.created_at);
  if (delta !== 0 && !Number.isNaN(delta)) return delta;
  return a.id.localeCompare(b.id);
}

export function orderRunTree<T extends RunTreeRun>(
  rootId: string,
  runs: readonly T[],
): RunTreeEntry<T>[] {
  const ids = new Set(runs.map((run) => run.id));
  const children = new Map<string, T[]>();
  for (const run of runs) {
    const parent =
      run.parent_run_id && run.parent_run_id !== run.id && ids.has(run.parent_run_id)
        ? run.parent_run_id
        : rootId;
    const siblings = children.get(parent) ?? [];
    siblings.push(run);
    children.set(parent, siblings);
  }
  for (const siblings of children.values()) siblings.sort(byCreation);

  const ordered: RunTreeEntry<T>[] = [];
  const placed = new Set<string>();
  const walk = (parentId: string, depth: number) => {
    for (const run of children.get(parentId) ?? []) {
      if (placed.has(run.id)) continue;
      placed.add(run.id);
      ordered.push({ run, depth });
      walk(run.id, depth + 1);
    }
  };
  walk(rootId, 1);
  // Runs in a cycle (a -> b -> a) each have their parent inside it, so the walk
  // down from the root never enters it: list them flat rather than lose them.
  for (const run of [...runs].sort(byCreation)) {
    if (!placed.has(run.id)) {
      placed.add(run.id);
      ordered.push({ run, depth: 1 });
    }
  }
  return ordered;
}
