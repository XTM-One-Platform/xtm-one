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
 * A workset keeps its documents in an internal knowledge base that no page
 * opens. `GET /knowledge-bases/{id}` on one answers 404 with
 * `detail.code === "workset_storage"` (backend `WORKSET_STORAGE_CODE`), so an
 * old link or an audit entry says what the id is instead of "not found".
 */
export const WORKSET_STORAGE_CODE = "workset_storage";

/** Whether a failed knowledge base read is the 404 of a workset's storage. */
export function isWorksetStorage(error: unknown): boolean {
  const failed = error as { status?: number; detail?: unknown } | null;
  if (failed?.status !== 404) return false;
  const detail = failed.detail;
  return !!detail && typeof detail === "object" && (detail as { code?: unknown }).code === WORKSET_STORAGE_CODE;
}
