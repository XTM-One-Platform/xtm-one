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

import type { Workset } from "@/types";

/**
 * Where a workset's collection stands, one answer for the row chip, the hover
 * facts and the detail badge.
 *
 * - sealed: the caller closed the window, nothing else arrives.
 * - rolling: a corpus the runtime opened for an agent chat or an assignment
 *   (the API's `implicit`); it is never sealed by design, so "Still
 *   collecting" would read as an ingestion that never ends.
 * - collecting: a named corpus not sealed yet.
 */
export type WorksetCollection = "sealed" | "rolling" | "collecting";

export function worksetCollection(workset: Pick<Workset, "sealed_at" | "implicit">): WorksetCollection {
  if (workset.sealed_at) return "sealed";
  return workset.implicit ? "rolling" : "collecting";
}

/** The words of each state, catalog keys. @i18n-keys */
export const WORKSET_COLLECTION_LABELS = {
  sealed: "Sealed",
  rolling: "Rolling",
  collecting: "Still collecting",
} as const satisfies Record<WorksetCollection, string>;
