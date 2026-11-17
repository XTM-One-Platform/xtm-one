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
 * An agent's coordination access: which agents read and write the
 * company-managed work items it creates and the company-managed shared state
 * it owns. `null` is the default - everyone reads them, every company-managed
 * agent writes them - and a narrowing only ever takes agents out.
 *
 * `normalizeAccess` mirrors `normalize_coordination_access` in
 * `backend/app/services/company_managed_writes.py`, so the form shows what the
 * server stores: the agent itself is never listed (it is always admitted), a
 * list only counts for the `agents` scope it belongs to, and writing implies
 * reading.
 *
 * No imports on purpose: `yarn test:unit` runs it under plain `node --test`.
 */

export type Scope = "all" | "agents" | "self";

export interface Access {
  read: Scope;
  write: Scope;
  read_agent_ids: string[];
  write_agent_ids: string[];
}

export const SCOPES: readonly Scope[] = ["all", "agents", "self"];

const DEFAULT_ACCESS: Access = Object.freeze({
  read: "all",
  write: "all",
  read_agent_ids: [],
  write_agent_ids: [],
}) as Access;

/** How wide a scope is: a read scope may never be narrower than the write one. */
const WIDTH: Record<Scope, number> = { self: 0, agents: 1, all: 2 };

function unique(ids: readonly string[], selfId?: string): string[] {
  const out: string[] = [];
  for (const id of ids) {
    if (id && id !== selfId && !out.includes(id)) out.push(id);
  }
  return out;
}

/** The draft a form starts from: the stored value, or the default. */
export function accessDraft(stored: Access | null | undefined): Access {
  if (!stored) return { ...DEFAULT_ACCESS, read_agent_ids: [], write_agent_ids: [] };
  return {
    read: SCOPES.includes(stored.read) ? stored.read : "self",
    write: SCOPES.includes(stored.write) ? stored.write : "self",
    read_agent_ids: [...(stored.read_agent_ids ?? [])],
    write_agent_ids: [...(stored.write_agent_ids ?? [])],
  };
}

/** Whether a read scope may go with a write scope (readers include writers). */
export function readScopeAllowed(read: Scope, write: Scope): boolean {
  return WIDTH[read] >= WIDTH[write];
}

/** The value to send: `null` for the default, the server's stored form otherwise. */
export function normalizeAccess(draft: Access, selfId?: string): Access | null {
  let read = draft.read;
  const write = draft.write;
  let readIds = unique(draft.read_agent_ids, selfId);
  const writeIds = unique(draft.write_agent_ids, selfId);
  if (write === "all") read = "all";
  else if (write === "agents") {
    if (read === "self") read = "agents";
    if (read === "agents") readIds = unique([...readIds, ...writeIds]);
  }
  if (read === "all" && write === "all") return null;
  return {
    read,
    write,
    read_agent_ids: read === "agents" ? readIds : [],
    write_agent_ids: write === "agents" ? writeIds : [],
  };
}

/** A new write scope, with the read scope widened when it would be narrower. */
export function withWriteScope(draft: Access, write: Scope): Access {
  const read = readScopeAllowed(draft.read, write) ? draft.read : write;
  return { ...draft, write, read };
}

/** Whether two values store the same thing. */
export function sameAccess(a: Access | null, b: Access | null): boolean {
  if (a === null || b === null) return a === b;
  return (
    a.read === b.read &&
    a.write === b.write &&
    a.read_agent_ids.length === b.read_agent_ids.length &&
    a.read_agent_ids.every((id) => b.read_agent_ids.includes(id)) &&
    a.write_agent_ids.length === b.write_agent_ids.length &&
    a.write_agent_ids.every((id) => b.write_agent_ids.includes(id))
  );
}
