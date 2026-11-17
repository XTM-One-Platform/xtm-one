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
 * The agent wizard's draft, kept across in-app navigation.
 *
 * The wizard is client-side state until its one create call, so any in-app
 * transition - the sidebar, the back button, and above all the Integrations
 * step's "Connect one", which sends the user to /integrations/new on purpose -
 * used to drop the whole draft. It is now written to session storage while it
 * is dirty and restored when the user comes back.
 *
 * Session storage, not local storage: the draft lives as long as the tab, and
 * is keyed per user (and instance) so a different account signing in on the
 * same tab never sees it; an explicit sign-out forgets it. Every read is
 * validated field by field - an unreadable, older or oversized draft is
 * discarded, never half-applied.
 */

export const WIZARD_DRAFT_VERSION = 1;
const WIZARD_DRAFT_KEY_PREFIX = "xtm-one:agent-wizard-draft:";
/** Room for any real draft (a persona is a system prompt, not a document)
 *  while leaving the rest of the tab's session storage to the app. */
export const MAX_WIZARD_DRAFT_CHARS = 512 * 1024;
/** The icon of an agent started from scratch. */
export const WIZARD_DEFAULT_ICON = "Bot";

export interface WizardDraft {
  /** Slug of the template the draft started from; null = from scratch. */
  templateSlug: string | null;
  stepIndex: number;
  reviewReached: boolean;
  name: string;
  icon: string;
  description: string;
  persona: string;
  integrationIds: string[];
  toolIds: string[];
  kbIds: string[];
  modelChoice: string;
  memory: { short: boolean; medium: boolean; persistent: boolean; user: boolean };
  guardrails: string;
  skillIds: string[];
  handoverIds: string[];
  budget: { cost: string; tokens: string; calls: string; seconds: string };
}

/**
 * Whether a draft holds anything worth coming back to: a chosen template, or
 * any field moved off its from-scratch default - an icon or an advanced option
 * alone counts. A draft emptied again does not.
 */
export function hasWizardDraftContent(draft: WizardDraft): boolean {
  return (
    draft.templateSlug !== null ||
    [draft.name, draft.description, draft.persona, draft.guardrails].some((v) => v.trim() !== "") ||
    draft.icon !== WIZARD_DEFAULT_ICON ||
    [draft.integrationIds, draft.toolIds, draft.kbIds, draft.skillIds, draft.handoverIds].some(
      (ids) => ids.length > 0,
    ) ||
    draft.modelChoice !== "" ||
    !Object.values(draft.memory).every(Boolean) ||
    Object.values(draft.budget).some((v) => v !== "")
  );
}

/** The part of `Storage` the draft needs, so tests can pass a map. */
export interface DraftStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

/** A storage whose keys can be listed, as the browser's is. */
export interface ListableDraftStorage extends DraftStorage {
  readonly length: number;
  key(index: number): string | null;
}

/**
 * Who a draft belongs to: the user, on this instance. Session storage is per
 * origin, and two instances can share one under different base paths.
 */
export function wizardDraftOwner(userId: string, basePath: string): string {
  return basePath ? `${basePath}:${userId}` : userId;
}

export function wizardDraftKey(owner: string): string {
  return `${WIZARD_DRAFT_KEY_PREFIX}${owner}`;
}

export function serializeWizardDraft(draft: WizardDraft): string {
  return JSON.stringify({ version: WIZARD_DRAFT_VERSION, draft });
}

const isString = (v: unknown): v is string => typeof v === "string";
const isStringArray = (v: unknown): v is string[] => Array.isArray(v) && v.every(isString);
const isRecord = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v);

export function parseWizardDraft(raw: string | null): WizardDraft | null {
  if (!raw || raw.length > MAX_WIZARD_DRAFT_CHARS) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!isRecord(parsed) || parsed.version !== WIZARD_DRAFT_VERSION || !isRecord(parsed.draft)) {
    return null;
  }
  const d = parsed.draft;
  const memory = d.memory;
  const budget = d.budget;
  // Fields this version no longer reads - the `createdKbNames` of a draft
  // written before the catalog named created knowledge bases - are ignored,
  // never a reason to refuse the draft.
  if (
    !(d.templateSlug === null || isString(d.templateSlug)) ||
    typeof d.stepIndex !== "number" ||
    !Number.isInteger(d.stepIndex) ||
    d.stepIndex < 0 ||
    typeof d.reviewReached !== "boolean" ||
    !isString(d.name) ||
    !isString(d.icon) ||
    !isString(d.description) ||
    !isString(d.persona) ||
    !isStringArray(d.integrationIds) ||
    !isStringArray(d.toolIds) ||
    !isStringArray(d.kbIds) ||
    !isString(d.modelChoice) ||
    !isRecord(memory) ||
    !["short", "medium", "persistent", "user"].every((k) => typeof memory[k] === "boolean") ||
    !isString(d.guardrails) ||
    !isStringArray(d.skillIds) ||
    !isStringArray(d.handoverIds) ||
    !isRecord(budget) ||
    !["cost", "tokens", "calls", "seconds"].every((k) => isString(budget[k]))
  ) {
    return null;
  }
  return {
    templateSlug: d.templateSlug,
    stepIndex: d.stepIndex,
    reviewReached: d.reviewReached,
    name: d.name,
    icon: d.icon,
    description: d.description,
    persona: d.persona,
    integrationIds: d.integrationIds,
    toolIds: d.toolIds,
    kbIds: d.kbIds,
    modelChoice: d.modelChoice,
    memory: {
      short: memory.short as boolean,
      medium: memory.medium as boolean,
      persistent: memory.persistent as boolean,
      user: memory.user as boolean,
    },
    guardrails: d.guardrails,
    skillIds: d.skillIds,
    handoverIds: d.handoverIds,
    budget: {
      cost: budget.cost as string,
      tokens: budget.tokens as string,
      calls: budget.calls as string,
      seconds: budget.seconds as string,
    },
  };
}

/** Session storage when the browser offers it; null otherwise (no draft kept). */
export function browserDraftStorage(): ListableDraftStorage | null {
  try {
    return typeof window !== "undefined" ? window.sessionStorage : null;
  } catch {
    // Access to sessionStorage throws when storage is disabled.
    return null;
  }
}

export function loadWizardDraft(storage: DraftStorage | null, userId: string): WizardDraft | null {
  if (!storage) return null;
  try {
    const draft = parseWizardDraft(storage.getItem(wizardDraftKey(userId)));
    if (!draft) storage.removeItem(wizardDraftKey(userId));
    return draft;
  } catch {
    return null;
  }
}

export function saveWizardDraft(storage: DraftStorage | null, userId: string, draft: WizardDraft): void {
  if (!storage) return;
  const key = wizardDraftKey(userId);
  const value = serializeWizardDraft(draft);
  try {
    if (value.length > MAX_WIZARD_DRAFT_CHARS) {
      storage.removeItem(key);
      return;
    }
    storage.setItem(key, value);
  } catch {
    // Quota exceeded or storage disabled: this version is not kept, and the
    // previous one must not come back later as if it were the current one.
    try {
      storage.removeItem(key);
    } catch {
      // Storage unavailable: nothing was kept either.
    }
  }
}

export function clearWizardDraft(storage: DraftStorage | null, userId: string): void {
  if (!storage) return;
  try {
    storage.removeItem(wizardDraftKey(userId));
  } catch {
    // Nothing to clear when storage is unavailable.
  }
}

/** Forgets every wizard draft of the tab, whoever wrote it: an explicit
 *  sign-out leaves nothing typed behind for the next person at the machine. */
export function clearAllWizardDrafts(storage: ListableDraftStorage | null): void {
  if (!storage) return;
  try {
    const keys: string[] = [];
    for (let i = 0; i < storage.length; i++) {
      const key = storage.key(i);
      if (key?.startsWith(WIZARD_DRAFT_KEY_PREFIX)) keys.push(key);
    }
    for (const key of keys) storage.removeItem(key);
  } catch {
    // Nothing to clear when storage is unavailable.
  }
}

type DraftRestore<T> =
  | { kind: "none" }
  /** Its template is not listed yet (the list failed to load): kept for later. */
  | { kind: "wait" }
  /** Its template is gone: the draft cannot be rebuilt faithfully. */
  | { kind: "drop" }
  | { kind: "restore"; template: T | null };

export function resolveDraftRestore<T extends { slug: string }>(
  draft: WizardDraft | null,
  templates: readonly T[] | undefined,
): DraftRestore<T> {
  if (!draft) return { kind: "none" };
  if (draft.templateSlug === null) return { kind: "restore", template: null };
  if (!templates) return { kind: "wait" };
  const template = templates.find((t) => t.slug === draft.templateSlug);
  return template ? { kind: "restore", template } : { kind: "drop" };
}

/**
 * `current` without the restored ids the catalog no longer offers - deleted,
 * or no longer visible to this user. The pickers could neither show nor
 * unselect them, and the create call refuses them. Ids picked since the
 * restore are kept; `current` itself comes back when nothing is dropped.
 */
export function withoutUnavailable(
  current: string[],
  restored: readonly string[],
  offered: readonly { value: string }[],
): string[] {
  const offeredValues = new Set(offered.map((o) => o.value));
  const restoredValues = new Set(restored);
  const kept = current.filter((v) => !restoredValues.has(v) || offeredValues.has(v));
  return kept.length === current.length ? current : kept;
}
