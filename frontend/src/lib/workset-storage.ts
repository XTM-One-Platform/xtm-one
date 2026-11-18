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
 * `detail.code === "workset_storage"` (backend `WORKSET_STORAGE_CODE`) and the
 * owning workset's `workset_id` and `workset_name`, so an old link or an audit
 * entry names the workset and leads to it instead of saying "not found".
 */
const WORKSET_STORAGE_CODE = "workset_storage";

/** The workset a knowledge base id belongs to, as its 404 names it. */
export interface WorksetStorageOwner {
  id: string;
  name: string;
}

/** The owning workset when a failed knowledge base read is the 404 of a workset's storage, else null. */
export function worksetStorageOf(error: unknown): WorksetStorageOwner | null {
  const failed = error as { status?: number; detail?: unknown } | null;
  if (failed?.status !== 404) return null;
  const detail = failed.detail as { code?: unknown; workset_id?: unknown; workset_name?: unknown } | null | undefined;
  if (!detail || typeof detail !== "object" || detail.code !== WORKSET_STORAGE_CODE) return null;
  const id = typeof detail.workset_id === "string" ? detail.workset_id : "";
  const name = typeof detail.workset_name === "string" && detail.workset_name ? detail.workset_name : id;
  return { id, name };
}

/** Operations > Worksets narrowed to one workset: every visibility, searched by its name (`wks_q`). */
export function worksetLink(owner: WorksetStorageOwner): string {
  const params = new URLSearchParams({ vis: "all", wks_q: owner.name });
  return `/operations/worksets?${params.toString()}`;
}
