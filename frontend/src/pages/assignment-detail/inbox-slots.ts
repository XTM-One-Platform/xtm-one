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
 * Parallel slots of a work-item inbox.
 *
 * A `work_item` assignment runs up to `trigger_config.max_concurrent_runs`
 * items at once: absent is the serial default (1), otherwise a whole number
 * from 1 to WORK_ITEM_MAX_ITEMS_IN_PARALLEL, the bound the API enforces in
 * `work_item_concurrency_error` (backend/app/services/assignment_dispatch.py).
 */

/** Mirror of the backend's WORK_ITEM_MAX_CONCURRENT_RUNS. */
export const WORK_ITEM_MAX_ITEMS_IN_PARALLEL = 10;

/** How many items the inbox runs at once, as stored (1 when absent or unreadable). */
export function inboxSlots(config: { max_concurrent_runs?: unknown } | null | undefined): number {
  const raw = config?.max_concurrent_runs;
  return typeof raw === "number" && Number.isInteger(raw) && raw >= 1 ? raw : 1;
}

/**
 * The editor's "Items in parallel" text read as a slot count, or `null` when
 * it is not a whole number from 1 to WORK_ITEM_MAX_ITEMS_IN_PARALLEL.
 */
export function parseItemsInParallel(text: string): number | null {
  const trimmed = text.trim();
  if (!/^\d+$/.test(trimmed)) return null;
  const value = Number(trimmed);
  return value >= 1 && value <= WORK_ITEM_MAX_ITEMS_IN_PARALLEL ? value : null;
}
